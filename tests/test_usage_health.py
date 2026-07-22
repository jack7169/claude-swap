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
