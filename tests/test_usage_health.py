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
    """A token-endpoint (refresh) 429 must arm a GLOBAL back-off so cswap stops
    hammering the shared per-IP token limit. Otherwise every expired-token backup
    retries its refresh every round, keeping the limit pinned so no token ever
    refreshes — the 'usage unavailable' storm. Distinct from a usage-endpoint 429
    (RATE_LIMITED), which the roll spreads and must NOT trigger a back-off."""

    def test_token_rate_limited_arms_backoff_and_renders_rate_limited(
        self, temp_home, monkeypatch
    ):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        out = h.round({"2": _oauth.TOKEN_RATE_LIMITED})
        # The internal signal is rendered as the ordinary rate-limited sentinel.
        assert out[1] == USAGE_RATE_LIMITED
        # Global refresh back-off armed for the window.
        assert s._refresh_rl_until == h.now + _switcher._REFRESH_RL_BACKOFF

    def test_backed_off_non_active_skipped_within_window(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": _oauth.TOKEN_RATE_LIMITED})           # arm at t=1000
        h.now += 30                                         # within the window
        out = h.round({"2": {"five_hour": {"pct": 5.0}}})   # would recover IF fetched
        assert [c for c in h.calls if c[0] == h.now] == []  # not re-fetched
        assert out[1] == USAGE_RATE_LIMITED                 # retains rate-limited

    def test_backed_off_account_refetched_after_window(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": _oauth.TOKEN_RATE_LIMITED})
        h.now += _switcher._REFRESH_RL_BACKOFF + 1          # past the window
        out = h.round({"2": {"five_hour": {"pct": 7.0}}})
        assert (h.now, "2") in h.calls                      # re-probed
        assert out[1] == {"five_hour": {"pct": 7.0}}        # recovered

    def test_force_bypasses_refresh_backoff(self, temp_home, monkeypatch):
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": _oauth.TOKEN_RATE_LIMITED})
        h.now += 5                                          # well within the window
        h.round({"2": {"five_hour": {"pct": 2.0}}}, force=True)
        assert (h.now, "2") in h.calls                      # forced -> fetched anyway

    def test_active_account_not_skipped_during_refresh_backoff(
        self, temp_home, monkeypatch
    ):
        # An ACTIVE account never refreshes via cswap (Claude Code owns its token),
        # so the shared token limit doesn't apply — it must keep fetching during
        # the back-off, routed through _fetch_active_usage (not fetch_usage...).
        s = _make_switcher()
        h = _Harness(s, monkeypatch)
        h.round({"2": _oauth.TOKEN_RATE_LIMITED})           # arm global back-off
        active_calls = []
        monkeypatch.setattr(
            s, "_fetch_active_usage",
            lambda num, email, creds: active_calls.append(str(num))
            or {"five_hour": {"pct": 3.0}},
        )
        h.now += 10
        out = s._collect_usage([(1, "a@x.com", "", "", True, _CREDS)], only={"1"})
        assert active_calls == ["1"]                        # active fetched anyway
        assert out[0] == {"five_hour": {"pct": 3.0}}
