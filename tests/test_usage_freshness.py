"""Per-account usage freshness in ``_collect_usage`` (the frozen-usage bug).

The old design had a single file-level cache timestamp and fetched every
account in a parallel burst. In practice the usage endpoint's per-IP limit
tolerates roughly one request per short window, so every burst tripped a 429
and the retained-prior merge silently carried stale percentages forward with a
fresh-looking timestamp — non-active accounts' usage effectively never updated,
and auto-switch decided on frozen data.

Contract under test:
- cache entries are ``{"usage": <dict|str|None>, "fetchedAt": <ts>}``;
  legacy bare entries read as stale (fetchedAt=0) and upgrade on next write;
  a stamp in the FUTURE is also stale (a frozen account must self-heal)
- per-account TTL decides what to fetch: active 15s, backups 60s
- fetching is sequential, active-first then stalest-first; the round stops
  at the first 429 (which is SURFACED, not hidden as stale; untouched accounts
  keep aging) and when the wall-clock budget is exhausted (a dead-slow network
  must not stall callers). There is no cross-round backoff: the next round
  fetches on the user's cadence.
- ``force=True`` bypasses the TTLs
- transient failures retain the prior good dict but stamp the attempt;
  definitive failures (token expired / no creds) replace it
- ``_active_account_usage`` (status path, second reader/writer of the cache)
  speaks the same entry format: unwraps on read, judges freshness per entry,
  and merge-writes without wiping other accounts' entries

The clock is frozen per-test (``time.time`` monkeypatched) so TTL boundaries
are deterministic — no real-clock races on slow CI runners.
"""

from __future__ import annotations

import json
import time as _time
from unittest.mock import patch

import pytest

from claude_swap import oauth as _oauth
from claude_swap.cache import write_cache
from claude_swap.json_output import USAGE_RATE_LIMITED, USAGE_TOKEN_EXPIRED
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

FROZEN = 1_800_000_000.0


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze ``time.time`` (shared by switcher.py and cache.py) at FROZEN."""
    monkeypatch.setattr(_time, "time", lambda: FROZEN)
    return FROZEN


def _entry(usage, age: float):
    """A new-format cache entry stamped ``age`` seconds before FROZEN."""
    return {"usage": usage, "fetchedAt": FROZEN - age}


class _Base:
    def _setup(self, temp_home):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _info(self, n=2):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})
        rows = [(1, "a@x.com", "", "", True, creds)]
        for i in range(2, n + 1):
            rows.append((i, f"acct{i}@x.com", "", "", False, creds))
        return rows

    def _patch_fetch(self, monkeypatch, responses, counter):
        def fake(num, email, creds, is_active, persist_credentials=None):
            counter.append(num)
            return responses[num]
        monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)

    def _cache_path(self, s):
        return s.backup_dir / "cache" / "usage.json"

    def _cache_entries(self, s) -> dict:
        return json.loads(self._cache_path(s).read_text())["data"]


class TestPerAccountFreshness(_Base):
    # -- format ---------------------------------------------------------

    def test_legacy_bare_entries_are_stale_and_upgraded(
        self, temp_home, frozen_now, monkeypatch
    ):
        """Old-format bare dicts have no per-account stamp: treat as stale."""
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        # Legacy format: bare usage dicts, only the file-level timestamp (fresh).
        write_cache(self._cache_path(s), {
            "1": {"five_hour": {"pct": 11.0}},
            "2": {"five_hour": {"pct": 22.0}},
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": {"five_hour": {"pct": 44.0}}}, calls)

        out = s._collect_usage(self._info())

        assert sorted(calls) == ["1", "2"]  # legacy entries never count as fresh
        assert out == [{"five_hour": {"pct": 33.0}}, {"five_hour": {"pct": 44.0}}]
        entries = self._cache_entries(s)
        for k in ("1", "2"):
            assert set(entries[k]) == {"usage", "fetchedAt"}  # upgraded format
            assert entries[k]["fetchedAt"] == FROZEN

    def test_future_fetched_at_is_stale(self, temp_home, frozen_now, monkeypatch):
        """A stamp in the future (clock rollback, restored backup) must not
        freeze the account until wall-clock time catches up."""
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=5),      # fresh
            "2": _entry({"five_hour": {"pct": 22.0}}, age=-3600),  # future stamp
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": {"five_hour": {"pct": 44.0}}}, calls)

        out = s._collect_usage(self._info())

        assert calls == ["2"]  # future-stamped entry re-fetched
        assert out[1] == {"five_hour": {"pct": 44.0}}

    # -- TTL selection ---------------------------------------------------

    def test_fresh_entries_skip_network_stale_ones_fetch(
        self, temp_home, frozen_now, monkeypatch
    ):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=5),     # active, fresh (<15s)
            "2": _entry({"five_hour": {"pct": 22.0}}, age=120),   # backup, stale (>60s)
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": {"five_hour": {"pct": 44.0}}}, calls)

        out = s._collect_usage(self._info())

        assert calls == ["2"]
        assert out[0] == {"five_hour": {"pct": 11.0}}  # served from fresh cache
        assert out[1] == {"five_hour": {"pct": 44.0}}  # re-fetched

    def test_backup_ttl_longer_than_active(self, temp_home, frozen_now, monkeypatch):
        """At 30s of age the active account is stale (15s TTL), a backup isn't (60s)."""
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=30),
            "2": _entry({"five_hour": {"pct": 22.0}}, age=30),
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": {"five_hour": {"pct": 44.0}}}, calls)

        out = s._collect_usage(self._info())

        assert calls == ["1"]
        assert out == [{"five_hour": {"pct": 33.0}}, {"five_hour": {"pct": 22.0}}]

    # -- sequential stalest-first + stop on 429 / budget -------------------

    def test_round_is_stalest_first_and_stops_on_429(
        self, temp_home, frozen_now, monkeypatch
    ):
        """Strictly stalest-first — the active account gets NO ordering
        priority. With stop-on-first-429, prioritizing the active account
        would let a chronically rate-limited environment starve every backup
        forever (the active account is stale nearly every round)."""
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=100),  # active, stale
            "2": _entry({"five_hour": {"pct": 22.0}}, age=300),  # stalest
            "3": _entry({"five_hour": {"pct": 33.0}}, age=200),  # staler
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 55.0}},
                                        "2": USAGE_RATE_LIMITED,
                                        "3": {"five_hour": {"pct": 66.0}}}, calls)

        out = s._collect_usage(self._info(n=3))

        # Stalest goes first; its 429 ends the round immediately (no backoff armed).
        assert calls == ["2"]
        assert out == [{"five_hour": {"pct": 11.0}},
                       "rate limited",   # slot 2's 429 is SURFACED, not hidden as stale
                       {"five_hour": {"pct": 33.0}}]
        entries = self._cache_entries(s)
        # Untouched accounts keep their old stamps (keep aging → first next
        # round); the attempted-but-429 account is stamped so ordering rotates.
        assert entries["1"]["fetchedAt"] == FROZEN - 100
        assert entries["3"]["fetchedAt"] == FROZEN - 200
        assert entries["2"]["fetchedAt"] == FROZEN

    def test_chronic_429_rotates_attempts_instead_of_starving(
        self, temp_home, frozen_now, monkeypatch
    ):
        """Even when EVERY request 429s, successive rounds attempt different
        accounts — no account is permanently starved behind another."""
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=100),
            "2": _entry({"five_hour": {"pct": 22.0}}, age=300),
            "3": _entry({"five_hour": {"pct": 33.0}}, age=200),
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": USAGE_RATE_LIMITED,
                                        "2": USAGE_RATE_LIMITED,
                                        "3": USAGE_RATE_LIMITED}, calls)

        for _ in range(3):
            s._collect_usage(self._info(n=3))

        assert calls == ["2", "3", "1"]  # every account got its turn

    def test_next_round_prioritizes_missed_accounts(
        self, temp_home, frozen_now, monkeypatch
    ):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=5),    # active, fresh
            "2": _entry({"five_hour": {"pct": 22.0}}, age=70),   # stale
            "3": _entry({"five_hour": {"pct": 33.0}}, age=90),   # stalest
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 55.0}},
                                        "2": {"five_hour": {"pct": 66.0}},
                                        "3": {"five_hour": {"pct": 77.0}}}, calls)

        s._collect_usage(self._info(n=3))

        assert calls == ["3", "2"]  # stalest first, fresh active skipped

    def test_round_stops_when_wall_clock_budget_exhausted(
        self, temp_home, monkeypatch
    ):
        """A dead-slow network (per-request timeouts) must not stall the caller
        linearly with the account count; unattempted accounts keep aging."""
        clock = {"t": FROZEN}
        monkeypatch.setattr(_time, "time", lambda: clock["t"])
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=100),
            "2": _entry({"five_hour": {"pct": 22.0}}, age=300),
            "3": _entry({"five_hour": {"pct": 33.0}}, age=200),
        })
        calls = []

        def slow_fetch(num, email, creds, is_active, persist_credentials=None):
            calls.append(num)
            clock["t"] += 15.0  # each request crawls for 15s
            return {"five_hour": {"pct": 1.0}}

        monkeypatch.setattr(_oauth, "fetch_usage_for_account", slow_fetch)

        out = s._collect_usage(self._info(n=3))

        # Stalest-first order is [2, 3, 1]; 30s elapsed > 20s budget → 1 skipped.
        assert calls == ["2", "3"]
        assert out[0] == {"five_hour": {"pct": 11.0}}  # prior retained
        assert self._cache_entries(s)["1"]["fetchedAt"] == FROZEN - 100

    # -- force (bypasses TTL; no backoff to honor) ------------------------

    def test_force_bypasses_ttl(
        self, temp_home, frozen_now, monkeypatch
    ):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=1),
            "2": _entry({"five_hour": {"pct": 22.0}}, age=1),
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 33.0}},
                                        "2": {"five_hour": {"pct": 44.0}}}, calls)

        out = s._collect_usage(self._info(), force=True)
        assert calls == ["1", "2"]  # TTLs bypassed (fresh entries refetched)
        assert out == [{"five_hour": {"pct": 33.0}}, {"five_hour": {"pct": 44.0}}]

    # -- merge semantics ---------------------------------------------------

    def test_transient_failure_retains_prior_dict_and_stamps_attempt(
        self, temp_home, frozen_now, monkeypatch
    ):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=5),
            "2": _entry({"five_hour": {"pct": 22.0}}, age=120),
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": None, "2": None}, calls)

        out = s._collect_usage(self._info())

        assert calls == ["2"]
        assert out[1] == {"five_hour": {"pct": 22.0}}  # prior dict retained
        entries = self._cache_entries(s)
        # The attempt is stamped so a broken account isn't hammered every tick.
        assert entries["2"]["fetchedAt"] == FROZEN

    def test_definitive_failure_replaces_prior_dict(
        self, temp_home, frozen_now, monkeypatch
    ):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=5),
            "2": _entry({"five_hour": {"pct": 22.0}}, age=120),
        })
        calls = []
        self._patch_fetch(monkeypatch, {"1": None, "2": USAGE_TOKEN_EXPIRED}, calls)

        out = s._collect_usage(self._info())

        assert out[1] == USAGE_TOKEN_EXPIRED  # never mask a dead credential
        entries = self._cache_entries(s)
        assert entries["2"]["usage"] == USAGE_TOKEN_EXPIRED


class TestActiveAccountUsageFormat(_Base):
    """The status path is the cache's second reader/writer — same contract.

    ``_active_account_usage`` predates the per-account stamps; left unmigrated
    it returned the ``{"usage", "fetchedAt"}`` wrapper as if it were usage
    (breaking ``cswap --status``) and its merge-write re-read with the 15s
    file-level TTL, wiping every other account's entry whenever the file
    envelope was old — destroying the rotation state the freshness fix
    depends on.
    """

    def _live_creds(self):
        return json.dumps({"claudeAiOauth": {"accessToken": "sk-live"}})

    def test_cache_hit_unwraps_new_format_entry(
        self, temp_home, frozen_now, monkeypatch
    ):
        s = self._setup(temp_home)
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=5),
        })
        with patch.object(s, "_read_credentials", return_value=self._live_creds()), \
             patch.object(s, "_fetch_active_usage") as fetch:
            out = s._active_account_usage("1", "a@x.com")

        fetch.assert_not_called()
        assert out == {"five_hour": {"pct": 11.0}}  # unwrapped, not the wrapper

    def test_stale_entry_refetches_despite_fresh_file_envelope(
        self, temp_home, frozen_now, monkeypatch
    ):
        """Freshness is judged per entry, not by the file-level timestamp."""
        s = self._setup(temp_home)
        write_cache(self._cache_path(s), {
            "1": _entry({"five_hour": {"pct": 11.0}}, age=120),  # stale entry
        })
        with patch.object(s, "_read_credentials", return_value=self._live_creds()), \
             patch.object(s, "_fetch_active_usage",
                          return_value={"five_hour": {"pct": 50.0}}):
            out = s._active_account_usage("1", "a@x.com")

        assert out == {"five_hour": {"pct": 50.0}}

    def test_miss_write_preserves_other_entries_and_stamps(
        self, temp_home, frozen_now, monkeypatch
    ):
        """The merge-write must not wipe other accounts when the file is old."""
        s = self._setup(temp_home)
        path = self._cache_path(s)
        path.parent.mkdir(parents=True, exist_ok=True)
        # File envelope aged past the old 15s file TTL; account 2's entry is
        # still individually fresh-ish and must survive the merge.
        path.write_text(json.dumps({
            "timestamp": FROZEN - 300,
            "data": {"2": {"usage": {"five_hour": {"pct": 22.0}},
                           "fetchedAt": FROZEN - 30}},
        }))
        with patch.object(s, "_read_credentials", return_value=self._live_creds()), \
             patch.object(s, "_fetch_active_usage",
                          return_value={"five_hour": {"pct": 50.0}}):
            out = s._active_account_usage("1", "a@x.com")

        assert out == {"five_hour": {"pct": 50.0}}
        entries = self._cache_entries(s)
        assert entries["2"] == {"usage": {"five_hour": {"pct": 22.0}},
                                "fetchedAt": FROZEN - 30}  # survived the write
        assert entries["1"] == {"usage": {"five_hour": {"pct": 50.0}},
                                "fetchedAt": FROZEN}  # new format, stamped
