"""Tests for the ``force`` parameter on ``_collect_usage`` (Phase 3.1).

The menu-bar "Refresh now" command does a full refresh with ``only=None``. The
fresh-cache shortcut (``_USAGE_CACHE_TTL``) would otherwise return the cached
``usage.json`` without hitting the network when it is <15s old, so an explicit
user refresh showed stale data. ``force=True`` skips that shortcut and always
re-fetches. There is no cross-round 429 backoff: a rate-limited fetch is
surfaced ("rate limited"), and the next round still fetches on the user's cadence.
"""

from __future__ import annotations

import json
import time as _time

from claude_swap import oauth as _oauth
from claude_swap.cache import write_cache
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


class TestCollectUsageForceRefresh:
    def _setup(self, temp_home):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _info(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})
        return [
            (1, "a@x.com", "", "", True, creds),
            (2, "b@x.com", "", "", False, creds),
        ]

    def _patch_fetch(self, monkeypatch, responses, counter):
        def fake(num, email, creds, is_active, persist_credentials=None):
            counter.append(num)
            return responses[num]
        monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)

    def _seed_fresh_cache(self, s):
        """Write a usage.json where every account's entry is freshly stamped."""
        cache_path = s.backup_dir / "cache" / "usage.json"
        write_cache(cache_path, {
            "1": {"usage": {"five_hour": {"pct": 11.0}}, "fetchedAt": _time.time()},
            "2": {"usage": {"five_hour": {"pct": 22.0}}, "fetchedAt": _time.time()},
        })

    def test_force_false_uses_fresh_cache_without_network(self, temp_home, monkeypatch):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        self._seed_fresh_cache(s)
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 99.0}},
                                        "2": {"five_hour": {"pct": 99.0}}}, calls)

        out = s._collect_usage(self._info(), force=False)

        assert calls == []                              # network skipped
        assert out[0] == {"five_hour": {"pct": 11.0}}   # cached value returned
        assert out[1] == {"five_hour": {"pct": 22.0}}

    def test_force_true_skips_fresh_cache_and_refetches(self, temp_home, monkeypatch):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        self._seed_fresh_cache(s)
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": {"five_hour": {"pct": 44.0}}}, calls)

        out = s._collect_usage(self._info(), force=True)

        assert sorted(calls) == ["1", "2"]              # network hit despite fresh cache
        assert out[0] == {"five_hour": {"pct": 33.0}}   # fresh values returned
        assert out[1] == {"five_hour": {"pct": 44.0}}

    def test_force_true_fetches_but_retains_usage_on_429(self, temp_home, monkeypatch):
        # There is no 429 backoff to block force: the round still fetches, and a
        # rate-limited account KEEPS its last-known % (retained, not wiped) rather
        # than being replaced by a sentinel.
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        self._seed_fresh_cache(s)
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": _oauth.RATE_LIMITED}, calls)

        out = s._collect_usage(self._info(), force=True)

        assert "2" in calls                             # backup account was fetched
        assert out[1] == {"five_hour": {"pct": 22.0}}   # retained, not wiped
