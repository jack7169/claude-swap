"""Usage-health transition logging (B3) and per-account dead-reprobe backoff (B4).

A dead-credential backup used to fail silently for days: every refresh-failure
diagnostic logs at DEBUG while the app runs at INFO, and the merge kept retrying
the known-dead credential every round. These tests pin:

  * B3 — the merge loop logs per-account health TRANSITIONS (edge-only) at INFO/
    WARNING, so a credential going DEAD produces exactly one unmissable line and
    a chronic rate-limit is logged once, not once per attempt.
  * B4 — once a credential is classified DEAD, it is excluded from the next fetch
    round for DEAD_REPROBE seconds (per-account, never a global stall), re-probed
    afterwards, and a forced refresh bypasses the backoff.
"""

from __future__ import annotations

import json
import logging

import pytest

from claude_swap import oauth as _oauth
from claude_swap import switcher as _switcher
from claude_swap.json_output import USAGE_RATE_LIMITED, USAGE_TOKEN_EXPIRED
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _make_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


_CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})


def _info():
    return [
        (1, "a@x.com", "", "", False, _CREDS),
        (2, "b@x.com", "", "", False, _CREDS),
    ]


class _Harness:
    """Drives _collect_usage rounds with a controllable clock + scripted fetches."""

    def __init__(self, s, monkeypatch):
        self.s = s
        self.now = 1000.0
        self.calls: list[tuple[float, str]] = []
        self._responses: dict[str, object] = {}
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        monkeypatch.setattr("claude_swap.switcher.time.time", lambda: self.now)

        def fake_fetch(num, email, creds, is_active=False, persist_credentials=None):
            self.calls.append((self.now, str(num)))
            return self._responses[str(num)]

        monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake_fetch)

    def round(self, responses: dict[str, object], *, force=False):
        self._responses = responses
        return self.s._collect_usage(_info(), only=set(responses), force=force)


class TestHealthTransitionLogging:
    def test_healthy_to_dead_logs_one_warning(self, temp_home, monkeypatch, caplog):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            h.round({"1": {"five_hour": {"pct": 5.0}}, "2": {"five_hour": {"pct": 9.0}}},
                    force=True)              # both healthy -> no warning
            h.round({"1": {"five_hour": {"pct": 5.0}}, "2": USAGE_TOKEN_EXPIRED},
                    force=True)              # slot 2 HEALTHY -> DEAD  (edge)
            h.round({"1": {"five_hour": {"pct": 5.0}}, "2": USAGE_TOKEN_EXPIRED},
                    force=True)              # still DEAD -> NO new log

        dead = [r for r in caplog.records
                if r.levelno == logging.WARNING and "DEAD" in r.getMessage()]
        assert len(dead) == 1
        msg = dead[0].getMessage()
        assert "2" in msg and "b@x.com" in msg
        assert "cswap --add-account" in msg

    def test_recovery_logs_info_once(self, temp_home, monkeypatch, caplog):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            h.round({"2": USAGE_TOKEN_EXPIRED}, force=True)            # DEAD
            h.round({"2": {"five_hour": {"pct": 3.0}}}, force=True)    # DEAD -> HEALTHY
            h.round({"2": {"five_hour": {"pct": 3.0}}}, force=True)    # stays healthy

        recov = [r for r in caplog.records
                 if r.levelno == logging.INFO and "recover" in r.getMessage().lower()]
        assert len(recov) == 1

    def test_rate_limited_logs_once_not_per_attempt(self, temp_home, monkeypatch, caplog):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            h.round({"2": USAGE_RATE_LIMITED}, force=True)
            h.now += 100
            h.round({"2": USAGE_RATE_LIMITED}, force=True)

        rl = [r for r in caplog.records
              if r.levelno == logging.INFO and "rate" in r.getMessage().lower()
              and "2" in r.getMessage()]
        assert len(rl) == 1


class TestDeadReprobeBackoff:
    def test_dead_account_excluded_from_next_round(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": USAGE_TOKEN_EXPIRED})                 # t=1000 -> DEAD, dead_until=+900
        h.now += 61                                         # past the 60s backup TTL
        out = h.round({"2": {"five_hour": {"pct": 1.0}}})   # would recover IF fetched

        # Not re-fetched at t=1061 (dead-backed-off); sentinel retained from cache.
        assert [c for c in h.calls if c[0] == h.now] == []
        assert out[1] == USAGE_TOKEN_EXPIRED

    def test_dead_account_reprobed_after_window(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": USAGE_TOKEN_EXPIRED})                 # t=1000
        h.now += 901                                        # past DEAD_REPROBE (900s)
        out = h.round({"2": {"five_hour": {"pct": 2.0}}})

        assert (h.now, "2") in h.calls                      # re-probed
        assert out[1] == {"five_hour": {"pct": 2.0}}        # recovered

    def test_force_bypasses_dead_backoff(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": USAGE_TOKEN_EXPIRED})
        h.now += 5                                          # well within the backoff
        h.round({"2": {"five_hour": {"pct": 2.0}}}, force=True)

        assert (h.now, "2") in h.calls                      # forced -> fetched anyway

    def test_clear_usage_health_pops_dead_and_health(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": USAGE_TOKEN_EXPIRED})                 # arms dead_until + DEAD
        assert "2" in s._usage_dead_until
        assert s._usage_health.get("2") == "DEAD"

        s.clear_usage_health("2")

        assert "2" not in s._usage_dead_until
        assert "2" not in s._usage_health

    def test_reauth_makes_the_account_refetch_first(self, temp_home, monkeypatch):
        """After a browser sign-in the account must refresh IMMEDIATELY.

        Its cache entry was stamped when it was last attempted (showing "login
        expired"), so it looked FRESH (inside the 60s backup TTL) and sorted LAST
        in the stalest-first fetch order — the menu kept rendering the stale
        sentinel until the user hit "Refresh now". Replacing the credentials must
        drop the cached entry so the next round refetches this account first.
        """
        from claude_swap.cache import write_cache
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        # Slot 2 was just attempted (fresh stamp) and shows "login expired";
        # slot 1 is older, so slot 2 would otherwise sort last.
        write_cache(s.backup_dir / "cache" / "usage.json", {
            "1": {"usage": {"five_hour": {"pct": 1.0}},
                  "fetchedAt": h.now - 500, "validAt": h.now - 500},
            "2": {"usage": USAGE_TOKEN_EXPIRED,
                  "fetchedAt": h.now, "validAt": h.now - 900},
        })

        s.clear_usage_health("2")               # what re-auth does

        h._responses = {"1": {"five_hour": {"pct": 1.0}},
                        "2": {"five_hour": {"pct": 5.0}}}
        out = s._collect_usage(_info(), only={"1", "2"}, max_fetch=1)
        assert (h.now, "2") in h.calls          # fetched FIRST, before the older slot 1
        assert out[1] == {"five_hour": {"pct": 5.0}}
        assert out[1] != USAGE_TOKEN_EXPIRED    # stale sentinel is gone

    def test_cleared_account_refetched_without_force(self, temp_home, monkeypatch):
        # The re-auth bug: after a DEAD classification the account is backed off, so
        # even a full (non-forced) refresh skips it — leaving the stale "login
        # expired" in the menu. Clearing the health (as re-auth does) un-blocks it.
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": USAGE_TOKEN_EXPIRED})                 # dead -> backed off
        h.now += 61                                         # stale, but within backoff
        h.round({"2": {"five_hour": {"pct": 5.0}}})         # non-forced
        assert (h.now, "2") not in h.calls                  # skipped (still backed off)

        s.clear_usage_health("2")                           # what re-auth now does
        h.now += 1
        out = h.round({"2": {"five_hour": {"pct": 5.0}}})   # non-forced
        assert (h.now, "2") in h.calls                      # re-fetched now
        assert out[1] == {"five_hour": {"pct": 5.0}}


class TestRefreshRateLimitBackoff:
    """A token-endpoint (refresh) 429 is TRANSIENT and must never be reported as a
    logout.

    Rendering it as USAGE_TOKEN_EXPIRED ("login expired") produced the 2026-07-26
    bug: with the wrong UA every refresh 429'd, so each account died on its 8h
    access-token clock and the menu told the user to re-auth — which bought exactly
    8 more hours, forever. A 429 surfaces as USAGE_RATE_LIMITED (transient, still
    never auto-selected) with its OWN log line; only a genuine 400/401 is a logout."""

    def test_token_rate_limited_renders_rate_limited_not_logout(
        self, temp_home, monkeypatch
    ):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        out = h.round({"2": _oauth.TOKEN_RATE_LIMITED})
        assert out[1] == USAGE_RATE_LIMITED             # transient, NOT a logout
        assert out[1] != USAGE_TOKEN_EXPIRED
        assert _oauth.account_headroom(out[1]) is None  # still never auto-selected
        assert s._usage_health.get("2") != "DEAD"       # never the dead path

    def test_refresh_429_logs_distinctly_from_a_logout(
        self, temp_home, monkeypatch, caplog
    ):
        # The ambiguity that made this undiagnosable: a 429 and a dead credential
        # logged the identical "credentials DEAD (token expired)" line.
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            h.round({"2": _oauth.TOKEN_RATE_LIMITED})

        msgs = [r.getMessage() for r in caplog.records]
        assert any("refresh rate-limited" in m for m in msgs)
        assert not any("DEAD" in m for m in msgs)       # must NOT read as a logout

    def test_genuine_dead_token_still_reports_logout(self, temp_home, monkeypatch):
        # Contrast: a real 400/401 (invalid_grant) IS a logout — keeps the sentinel,
        # the DEAD health and the re-auth prompt.
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        out = h.round({"2": USAGE_TOKEN_EXPIRED})
        assert out[1] == USAGE_TOKEN_EXPIRED
        assert s._usage_health.get("2") == "DEAD"

    def test_backoff_never_exceeds_five_minutes(self, temp_home, monkeypatch):
        # HARD app-wide ceiling: this app exists to catch usage before it expires,
        # so no backoff may ever hide an account for more than 5 minutes.
        assert _switcher._MAX_BACKOFF == 300
        assert _switcher._DEAD_REPROBE <= _switcher._MAX_BACKOFF
        assert max(_switcher._REFRESH_BACKOFF_STEPS) <= _switcher._MAX_BACKOFF

    def test_refresh_backoff_escalates_then_caps(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        seen = []
        for _ in range(5):
            h.round({"2": _oauth.TOKEN_RATE_LIMITED}, force=True)
            seen.append(s._refresh_backoff_until["2"] - h.now)
            h.now += 1000                                   # past any window
        assert seen[:3] == list(_switcher._REFRESH_BACKOFF_STEPS)   # 60, 120, 300
        assert all(v == _switcher._MAX_BACKOFF for v in seen[3:])   # capped

    def test_backed_off_account_skipped_then_retried(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": _oauth.TOKEN_RATE_LIMITED})           # arms 60s
        h.now += 30                                          # inside the window
        h.round({"2": {"five_hour": {"pct": 5.0}}})
        assert [c for c in h.calls if c[0] == h.now] == []   # skipped
        h.now += 31                                          # past 60s
        out = h.round({"2": {"five_hour": {"pct": 5.0}}})
        assert (h.now, "2") in h.calls                       # retried
        assert out[1] == {"five_hour": {"pct": 5.0}}

    def test_success_resets_the_backoff_escalation(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": _oauth.TOKEN_RATE_LIMITED}, force=True)   # step -> 60
        h.round({"2": {"five_hour": {"pct": 5.0}}}, force=True)  # success resets
        assert "2" not in s._refresh_backoff_until
        h.round({"2": _oauth.TOKEN_RATE_LIMITED}, force=True)
        assert s._refresh_backoff_until["2"] - h.now == _switcher._REFRESH_BACKOFF_STEPS[0]

    def test_refresh_backoff_is_per_account_and_force_bypasses(
        self, temp_home, monkeypatch
    ):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"1": _oauth.TOKEN_RATE_LIMITED}, force=True)    # only slot 1
        h.now += 5
        out = h.round({"1": {"five_hour": {"pct": 1.0}}, "2": {"five_hour": {"pct": 2.0}}})
        assert (h.now, "2") in h.calls          # peer unaffected (per-account)
        assert (h.now, "1") not in h.calls      # slot 1 backed off
        h.now += 1
        h.round({"1": {"five_hour": {"pct": 1.0}}}, force=True)
        assert (h.now, "1") in h.calls          # force bypasses

    def test_transient_keychain_read_failure_is_not_a_dead_credential(
        self, temp_home, monkeypatch
    ):
        """A `security` spawn failure ([Errno 35] Resource temporarily unavailable,
        seen live 41x on 2026-07-26) made the backup read return "" — indistinguishable
        from "no credentials" — so healthy accounts were logged as credentials DEAD.
        A transient read failure must be a blip: retain last-known usage, never DEAD."""
        from claude_swap import macos_keychain
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        s.platform = Platform.MACOS
        # The backup read blows up the way a fork-starved `security` call does.
        monkeypatch.setattr(
            s._store, "_kc_read_backup",
            lambda *a: (_ for _ in ()).throw(OSError(35, "Resource temporarily unavailable")),
        )
        assert s._store._read_account_credentials("2", "b@x.com") is None  # not ""
        # And a genuine miss still reads as "" (absent), not as a failure.
        monkeypatch.setattr(s._store, "_kc_read_backup", lambda *a: "")
        assert s._store._read_account_credentials("2", "b@x.com") == ""
        assert macos_keychain.KEYCHAIN_ERRORS  # sanity: OSError is in the caught set

    def test_unreadable_credentials_do_not_classify_dead(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        # accounts_info carrying None creds = "couldn't read", not "no credentials".
        info = [(2, "b@x.com", "", "", False, None)]
        monkeypatch.setattr(_oauth, "fetch_usage_for_account",
                            lambda *a, **k: pytest.fail("must not hit the network"))
        out = s._collect_usage(info, only={"2"}, force=True)
        assert out[0] != USAGE_TOKEN_EXPIRED
        assert s._usage_health.get("2") != "DEAD"

    def test_refresh_429_stops_the_round(self, temp_home, monkeypatch):
        # A refresh-429 breaks the round, so a full/forced refresh can't burst-hammer
        # the shared limit — one token-429 per round, not one per account.
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        out = h.round(
            {"1": _oauth.TOKEN_RATE_LIMITED, "2": {"five_hour": {"pct": 9.0}}},
            force=True,
        )
        assert len([c for c in h.calls if c[0] == h.now]) == 1  # stopped at slot 1
        assert out[0] == USAGE_RATE_LIMITED                     # transient, not a logout
