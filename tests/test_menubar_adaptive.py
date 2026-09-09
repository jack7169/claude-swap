"""Adaptive refresh + smarter auto-switch for the menu bar.

Covers the 2026-09 menu-bar behavior changes, all via import-safe helpers (no
rumps / AppKit):

  1. The ACTIVE account polls 4x faster than the backups (``refresh_interval /
     4``, floored at the usage endpoint's 15s tolerance), and drops to the floor
     whenever the active account is within the near-limit band of the auto-switch
     threshold. Backups roll once per ``refresh_interval`` each, in their own
     driver, so the two cadences never compete for the same slot.
  2. Refresh-interval choices are 1 / 2 / 5 minutes; a persisted legacy value
     (15s / 30s) is coerced to the nearest valid choice on load.
  3. The auto-switch threshold applies to the WEEKLY window on a 5x-compressed
     scale: weekly headroom is worth five sessions, so a 90% session threshold
     becomes a 98% weekly threshold (80% -> 96%, 95% -> 99%).
  4. Reactive mode prefers the candidate whose WEEKLY window resets soonest
     (use-it-or-lose-it), then falls back to the headroom ranking.
  5. Real-time usage estimation: per-account samples are kept, the active
     account's burn rate is derived from the last two, and the auto-switcher
     decides on the PROJECTED usage — so a limit burned through between polls is
     caught before it hits 100%. A new snapshot or a projected crossing triggers
     an immediate evaluation instead of waiting for the periodic cadence.
  6. Live in-place menu updates cover the checkmark, the menu-bar title and the
     sign-in row, so a switch is visible without closing and reopening the menu.
     A non-forced refresh that loses the worker slot is queued, never dropped.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from claude_swap import menubar
from claude_swap.json_output import USAGE_TOKEN_EXPIRED


def _usage(five, seven, *, valid_at=None, reset5=None, reset7=None, fable=None):
    u = {"five_hour": {"pct": five}, "seven_day": {"pct": seven}}
    if reset5 is not None:
        u["five_hour"]["resets_at"] = reset5
    if reset7 is not None:
        u["seven_day"]["resets_at"] = reset7
    if valid_at is not None:
        u["validAt"] = valid_at
    if fable is not None:
        u["model_weekly"] = {"Fable": {"pct": fable}}
    return u


def _acct(num, five, seven, active=False, **kw):
    return (num, f"a{num}@x.com", active, _usage(five, seven, **kw))


# ---------------------------------------------------------------------------
# 2. Refresh-interval choices
# ---------------------------------------------------------------------------


class TestRefreshChoices:
    def test_choices_are_one_two_five_minutes(self):
        assert menubar.REFRESH_CHOICES == (60, 120, 300)

    def test_normalize_keeps_valid_choice(self):
        for secs in menubar.REFRESH_CHOICES:
            assert menubar.normalize_refresh_interval(secs) == secs

    def test_normalize_coerces_legacy_fast_values_up_to_one_minute(self):
        assert menubar.normalize_refresh_interval(15) == 60
        assert menubar.normalize_refresh_interval(30) == 60

    def test_normalize_rounds_up_to_next_choice_and_caps(self):
        assert menubar.normalize_refresh_interval(61) == 120
        assert menubar.normalize_refresh_interval(200) == 300
        assert menubar.normalize_refresh_interval(9999) == 300

    def test_normalize_garbage_is_default(self):
        assert menubar.normalize_refresh_interval(0) == 60
        assert menubar.normalize_refresh_interval(-5) == 60

    def test_settings_load_coerces_legacy_interval(self, tmp_path: Path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"refresh_interval": 15}))
        assert menubar.MenuBarSettings.load(p).refresh_interval == 60
        p.write_text(json.dumps({"refresh_interval": 30}))
        assert menubar.MenuBarSettings.load(p).refresh_interval == 60
        p.write_text(json.dumps({"refresh_interval": 300}))
        assert menubar.MenuBarSettings.load(p).refresh_interval == 300


# ---------------------------------------------------------------------------
# 1. Active account polls 4x faster; near the limit it polls at the floor
# ---------------------------------------------------------------------------


class TestActiveRollInterval:
    def test_factor_is_four(self):
        assert menubar.ACTIVE_REFRESH_FACTOR == 4

    def test_one_minute_gives_fifteen_seconds(self):
        assert menubar._active_roll_interval(60, near_limit=False) == 15.0

    def test_two_and_five_minutes_scale(self):
        assert menubar._active_roll_interval(120, near_limit=False) == 30.0
        assert menubar._active_roll_interval(300, near_limit=False) == 75.0

    def test_floor_is_the_endpoint_tolerance(self):
        assert menubar._active_roll_interval(20, near_limit=False) == menubar._ROLL_MIN_INTERVAL

    def test_near_limit_drops_to_floor(self):
        assert menubar._active_roll_interval(300, near_limit=True) == menubar._ROLL_MIN_INTERVAL
        assert menubar._active_roll_interval(120, near_limit=True) == menubar._ROLL_MIN_INTERVAL


class _RollApp:
    def __init__(self, accounts, *, refresh_interval=60, threshold=90,
                 last_roll=0.0, last_active_roll=0.0, in_flight=False, samples=None):
        self.snapshot = {
            "accounts": accounts, "active_email": None, "active_usage": None,
            "instances": [],
        }
        self.settings = menubar.MenuBarSettings(
            refresh_interval=refresh_interval, auto_switch_threshold=threshold,
        )
        self._refresh_guard = type("G", (), {"in_flight": in_flight})()
        self._last_roll = last_roll
        self._last_active_roll = last_active_roll
        self._last_fetch_dispatch = 0.0
        self._usage_samples = samples or {}
        self.calls = []

    def refresh_async(self, full=False, force=False, max_fetch=None, scope=None,
                      max_age=None):
        self.calls.append(
            {"full": full, "force": force, "max_fetch": max_fetch, "scope": scope,
             "max_age": max_age}
        )


class TestActiveRoll:
    def test_due_active_roll_fetches_active_scope_only(self):
        app = _RollApp([_acct(1, 10, 10, active=True), _acct(2, 5, 5)])
        menubar._maybe_roll_active(app)
        assert len(app.calls) == 1
        call = app.calls[0]
        assert call["scope"] == "active"
        assert call["max_fetch"] == 1
        assert call["force"] is False
        # The driver owns the cadence, so the cache TTL must not be able to skip a
        # due poll on timer jitter: the max-age it passes is well under its period.
        assert call["max_age"] is not None and call["max_age"] < 15.0
        assert app._last_active_roll > 0.0

    def test_active_roll_waits_within_interval(self):
        app = _RollApp([_acct(1, 10, 10, active=True)], last_active_roll=time.time())
        menubar._maybe_roll_active(app)
        assert app.calls == []

    def test_active_roll_skips_while_in_flight_without_stamping(self):
        app = _RollApp([_acct(1, 10, 10, active=True)], in_flight=True)
        menubar._maybe_roll_active(app)
        assert app.calls == []
        assert app._last_active_roll == 0.0  # retries next tick

    def test_active_roll_uses_fast_cadence_near_limit(self):
        # 5-minute interval -> 75s active cadence normally; at 85% with a 90%
        # threshold (inside the 10-point band) the active polls every 15s.
        slow = _RollApp([_acct(1, 10, 10, active=True)], refresh_interval=300,
                        last_active_roll=time.time() - 20)
        menubar._maybe_roll_active(slow)
        assert slow.calls == []  # 20s < 75s: not due at the relaxed cadence

        near = _RollApp([_acct(1, 85, 10, active=True)], refresh_interval=300,
                        last_active_roll=time.time() - 20)
        menubar._maybe_roll_active(near)
        assert len(near.calls) == 1  # 20s >= 15s floor: due at the fast cadence

    def test_active_roll_no_accounts_is_noop(self):
        app = _RollApp([])
        menubar._maybe_roll_active(app)
        assert app.calls == []


class TestBackupRoll:
    def test_backup_roll_fetches_backups_scope(self):
        app = _RollApp([_acct(1, 10, 10, active=True), _acct(2, 5, 5), _acct(3, 5, 5)])
        menubar._maybe_roll(app)
        assert len(app.calls) == 1
        call = app.calls[0]
        assert call["scope"] == "backups"
        assert call["max_fetch"] == 1
        assert call["full"] is True
        assert call["max_age"] is not None and call["max_age"] < 30.0

    def test_backup_roll_interval_counts_backups_only(self):
        # 3 accounts, 1 active -> 2 backups -> roll every 60/2 = 30s.
        app = _RollApp([_acct(1, 10, 10, active=True), _acct(2, 5, 5), _acct(3, 5, 5)],
                       last_roll=time.time() - 20)
        menubar._maybe_roll(app)
        assert app.calls == []  # 20s < 30s
        app._last_roll = time.time() - 31
        menubar._maybe_roll(app)
        assert len(app.calls) == 1

    def test_backup_roll_noop_when_no_backups(self):
        app = _RollApp([_acct(1, 10, 10, active=True)])
        menubar._maybe_roll(app)
        assert app.calls == []

    def test_backup_roll_skips_while_in_flight(self):
        app = _RollApp([_acct(1, 10, 10, active=True), _acct(2, 5, 5)], in_flight=True)
        menubar._maybe_roll(app)
        assert app.calls == []
        assert app._last_roll == 0.0


class TestCrossDriverPacing:
    """The two drivers exclude each other via the guard, but exclusion alone lets
    them fire back-to-back (one RTT apart) whenever both are due — the burst
    pattern that 429s the usage endpoint. A shared minimum spacing between ANY
    two dispatches interleaves them instead."""

    def _both_due(self):
        # 1-min interval, 4 backups: both drivers sit on the 15s floor.
        accts = [_acct(1, 10, 10, active=True)] + [_acct(i, 5, 5) for i in range(2, 6)]
        return _RollApp(accts, refresh_interval=60)

    def test_dispatch_gap_is_half_the_floor(self):
        assert menubar._DISPATCH_GAP == menubar._ROLL_MIN_INTERVAL / 2

    def test_only_one_dispatch_per_tick_then_gap(self):
        app = self._both_due()
        menubar._maybe_roll_all(app)
        assert len(app.calls) == 1
        menubar._maybe_roll_all(app)  # same instant: the other driver must wait
        assert len(app.calls) == 1
        app._last_fetch_dispatch -= menubar._DISPATCH_GAP  # gap elapsed
        menubar._maybe_roll_all(app)
        assert len(app.calls) == 2
        assert {c["scope"] for c in app.calls} == {"active", "backups"}

    def test_each_driver_respects_the_shared_gap_alone(self):
        app = self._both_due()
        app._last_fetch_dispatch = time.time() - 1.0  # a fetch went out 1s ago
        menubar._maybe_roll_active(app)
        menubar._maybe_roll(app)
        assert app.calls == []
        assert app._last_active_roll == 0.0 and app._last_roll == 0.0  # not stamped

    def test_more_overdue_driver_goes_first(self):
        # active: 16s since its 15s period (ratio ~1.07); the single backup
        # (60s period) last rolled 150s ago (ratio 2.5) -> backups first.
        app = _RollApp([_acct(1, 10, 10, active=True), _acct(2, 5, 5)],
                       refresh_interval=60,
                       last_active_roll=time.time() - 16, last_roll=time.time() - 150)
        menubar._maybe_roll_all(app)
        assert [c["scope"] for c in app.calls] == ["backups"]
        # ...and the reverse ordering when the active is the more overdue one.
        app = _RollApp([_acct(1, 10, 10, active=True), _acct(2, 5, 5)],
                       refresh_interval=60,
                       last_active_roll=time.time() - 40, last_roll=time.time() - 61)
        menubar._maybe_roll_all(app)
        assert [c["scope"] for c in app.calls] == ["active"]

    def test_interleaving_keeps_both_cadences_and_spacing(self, monkeypatch):
        """Simulated 90s of 0.25s ticks: active every ~15s, one backup every
        ~15s (4 backups / 60s), never two dispatches closer than the gap."""
        app = self._both_due()
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(menubar.time, "time", lambda: clock["t"])
        stamps = []
        orig = app.refresh_async

        def spy(**kw):
            stamps.append((clock["t"], kw["scope"]))
            orig(**kw)

        app.refresh_async = spy
        for _ in range(360):  # 90s
            menubar._maybe_roll_all(app)
            clock["t"] += 0.25
        actives = [t for t, s in stamps if s == "active"]
        backups = [t for t, s in stamps if s == "backups"]
        assert 5 <= len(actives) <= 7
        assert 5 <= len(backups) <= 7
        times = sorted(t for t, _ in stamps)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert min(gaps) >= menubar._DISPATCH_GAP
        # each driver holds its own 15s period (not slowed to 30s by the other)
        assert all(14.5 <= g <= 16.0 for g in
                   [b - a for a, b in zip(actives, actives[1:])])


class TestSnapshotScope:
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

        def __init__(self):
            self.seen = {}

        def _build_accounts_info(self):
            return [(1, "a@x", "", "", False, ""), (2, "b@x", "", "", True, ""),
                    (3, "c@x", "", "", False, "")]

        def _collect_usage(self, info, only=None, force=False, max_fetch=None,
                           max_age=None):
            self.seen.update(only=only, force=force, max_fetch=max_fetch, max_age=max_age)
            return [None, None, None]

    def test_backups_scope_excludes_active(self):
        sw = self._SW()
        menubar._snapshot(sw, scope="backups", max_fetch=1, max_age=7.5)
        assert sw.seen["only"] == {"1", "3"}
        assert sw.seen["max_fetch"] == 1
        assert sw.seen["max_age"] == 7.5

    def test_active_scope_is_active_only(self):
        sw = self._SW()
        menubar._snapshot(sw, scope="active")
        assert sw.seen["only"] == {"2"}

    def test_all_scope_is_none(self):
        sw = self._SW()
        menubar._snapshot(sw, scope="all")
        assert sw.seen["only"] is None

    def test_full_flag_still_maps_to_scope(self):
        sw = self._SW()
        menubar._snapshot(sw, full=False)
        assert sw.seen["only"] == {"2"}
        sw = self._SW()
        menubar._snapshot(sw, full=True)
        assert sw.seen["only"] is None

    def test_active_scope_with_no_active_fetches_nothing(self):
        class _NoActive(self._SW):
            def _build_accounts_info(self):
                return [(1, "a@x", "", "", False, ""), (3, "c@x", "", "", False, "")]

            def _collect_usage(self, info, only=None, **kw):
                self.seen.update(only=only)
                return [None, None]

        sw = _NoActive()
        menubar._snapshot(sw, scope="active")
        assert sw.seen["only"] == set()  # the backups roll covers them instead


# ---------------------------------------------------------------------------
# 6b. Non-forced refresh is queued (coalesced) instead of dropped
# ---------------------------------------------------------------------------


class TestGuardQueuesNonForced:
    def test_nonforced_while_in_flight_is_queued(self):
        g = menubar._RefreshGuard()
        assert g.try_begin() is True
        assert g.try_begin() is False
        assert g.finish_and_take_pending() == (True, False)
        assert g.finish_and_take_pending() == (False, False)

    def test_forced_while_in_flight_marks_force(self):
        g = menubar._RefreshGuard()
        g.try_begin()
        g.try_begin(force=False)
        g.try_begin(force=True)
        assert g.finish_and_take_pending() == (True, True)

    def test_nothing_pending(self):
        g = menubar._RefreshGuard()
        g.try_begin()
        assert g.finish_and_take_pending() == (False, False)

    def test_worker_runs_queued_nonforced_followup(self, monkeypatch):
        """A refresh requested while a worker runs (e.g. the post-switch
        refresh) starts a follow-up worker instead of being lost."""
        first_in_snapshot = threading.Event()
        release_first = threading.Event()
        snaps = []

        def fake_snapshot(switcher, full=True, force=False, max_fetch=None,
                          scope=None, max_age=None):
            snaps.append((full, force, scope))
            if len(snaps) == 1:
                first_in_snapshot.set()
                release_first.wait(5.0)
            return {"accounts": [], "active_email": None, "active_usage": None,
                    "instances": []}

        monkeypatch.setattr(menubar, "_snapshot", fake_snapshot)

        app = _Harness()
        assert menubar._refresh_async_impl(app, full=True) is True
        assert first_in_snapshot.wait(5.0)
        assert menubar._refresh_async_impl(app, full=True) is False  # queued
        release_first.set()
        app.join_all()
        deadline = time.time() + 5
        while len(snaps) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(snaps) == 2
        assert snaps[1][1] is False  # follow-up is NOT forced (TTL-gated)


class _Harness:
    """Minimal MenuBarApp stand-in for the worker path (mirrors test_menubar_refresh)."""

    def __init__(self):
        self._refresh_guard = menubar._RefreshGuard()
        self._last_full_fetch = 0.0
        self._snapshot_at = 0.0
        self._snapshot_taken_at = 0.0
        self._usage_samples = {}
        self.snapshot = {"accounts": [], "active_email": None, "active_usage": None,
                         "instances": []}
        self.settings = menubar.MenuBarSettings()
        self._dirty = False
        self.switcher = self
        self._threads = []

    _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None),
                             "warning": staticmethod(lambda *a, **k: None)})()

    def recheck_keychain(self):
        pass

    def _build_accounts_info(self):
        return [(1, "a@x", "", "", True, "")]

    def _collect_usage(self, info, only=None, force=False, max_fetch=None, max_age=None):
        return [None]

    def _spawn(self, target, args):
        t = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(t)
        t.start()

    def join_all(self, timeout=5.0):
        deadline = time.time() + timeout
        while self._threads and time.time() < deadline:
            t = self._threads.pop(0)
            t.join(timeout=max(0.0, deadline - time.time()))


class TestWorkerBookkeeping:
    def test_worker_records_samples_and_taken_at(self, monkeypatch):
        monkeypatch.setattr(
            menubar, "_snapshot",
            lambda switcher, **kw: {
                "accounts": [(1, "a@x", True, _usage(40, 10, valid_at=1000.0))],
                "active_email": "a@x", "active_usage": _usage(40, 10, valid_at=1000.0),
                "instances": [],
            },
        )
        app = _Harness()
        before = time.time()
        menubar._worker_impl(app, full=False, scope="active", max_fetch=1)
        assert app._usage_samples == {"1": ((1000.0, 40, 10),)}
        assert app._snapshot_taken_at >= before
        assert app._snapshot_at >= app._snapshot_taken_at

    def test_active_only_snapshot_counts_as_fresh_data(self, monkeypatch):
        """Every completed snapshot has every account's row (backups from cache),
        so an active-scope poll keeps the auto-switch freshness gate satisfied."""
        monkeypatch.setattr(
            menubar, "_snapshot",
            lambda switcher, **kw: {"accounts": [], "active_email": None,
                                    "active_usage": None, "instances": []},
        )
        app = _Harness()
        menubar._worker_impl(app, full=False, scope="active")
        assert app._last_full_fetch > 0.0


# ---------------------------------------------------------------------------
# 3. Weekly threshold on the 5x-compressed scale
# ---------------------------------------------------------------------------


class TestWeeklyScale:
    def test_weekly_equivalent_pct(self):
        assert menubar.weekly_equivalent_pct(100) == 100
        assert menubar.weekly_equivalent_pct(98) == pytest.approx(90)
        assert menubar.weekly_equivalent_pct(96) == pytest.approx(80)
        assert menubar.weekly_equivalent_pct(99) == pytest.approx(95)
        assert menubar.weekly_equivalent_pct(90) == pytest.approx(50)

    def test_weekly_threshold_for_display(self):
        assert menubar.weekly_threshold(90) == pytest.approx(98)
        assert menubar.weekly_threshold(80) == pytest.approx(96)
        assert menubar.weekly_threshold(95) == pytest.approx(99)

    def test_limiting_pct_is_max_of_session_and_weekly_equivalent(self):
        assert menubar._limiting_pct(_usage(10, 98)) == pytest.approx(90)
        assert menubar._limiting_pct(_usage(50, 50)) == 50
        assert menubar._limiting_pct({"five_hour": {"pct": 50}}) is None
        assert menubar._limiting_pct("rate limited") is None

    def test_reactive_active_weekly_below_scaled_threshold_stays(self):
        # 90% threshold -> weekly must reach 98%. 97% is not enough.
        accts = [_acct(1, 10, 97, active=True), _acct(2, 5, 5)]
        assert menubar.decide_auto_switch(accts, 90) == ("none", None)

    def test_reactive_active_weekly_at_scaled_threshold_switches(self):
        accts = [_acct(1, 10, 98, active=True), _acct(2, 5, 5)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 2)

    def test_reactive_eighty_maps_to_ninety_six(self):
        assert menubar.decide_auto_switch(
            [_acct(1, 10, 95.9, active=True), _acct(2, 5, 5)], 80) == ("none", None)
        assert menubar.decide_auto_switch(
            [_acct(1, 10, 96, active=True), _acct(2, 5, 5)], 80) == ("switch", 2)

    def test_reactive_candidate_weekly_gate_is_scaled(self):
        # Peer at weekly 97% used to be excluded at a 90% threshold; now eligible.
        accts = [_acct(1, 99, 10, active=True), _acct(2, 5, 97)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 2)
        accts = [_acct(1, 99, 10, active=True), _acct(2, 5, 98)]
        assert menubar.decide_auto_switch(accts, 90) == ("no_candidate", None)

    def test_session_threshold_unchanged(self):
        accts = [_acct(1, 90, 0, active=True), _acct(2, 5, 5)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 2)
        accts = [_acct(1, 89.9, 0, active=True), _acct(2, 5, 5)]
        assert menubar.decide_auto_switch(accts, 90) == ("none", None)

    def test_consume_first_weekly_gate_is_scaled(self):
        early, late = "2026-06-24T07:00:00+00:00", "2026-06-26T07:00:00+00:00"
        peer_ok = (2, "b@x", False, _usage(10, 97, reset5=early))
        peer_out = (2, "b@x", False, _usage(10, 98, reset5=early))
        active = (1, "a@x", True, _usage(10, 20, reset5=late))
        assert menubar.decide_consume_first([active, peer_ok], 90, frozenset()) == ("switch", 2)
        assert menubar.decide_consume_first([active, peer_out], 90, frozenset()) == ("none", None)

    def test_hysteresis_feed_uses_scaled_weekly(self):
        accts = [_acct(1, 10, 98, active=True), _acct(2, 30, 40)]
        out = menubar.limiting_pct_by_account(accts, "reactive")
        assert out["1"] == pytest.approx(90)
        assert out["2"] == 30  # weekly 40 -> -200 on the session scale; 5h wins

    def test_usage_alerts_stay_on_raw_percentages(self):
        # Notifications are informational and keep reporting the real number.
        alerts, _ = menubar.detect_usage_alerts({}, [_acct(1, 10, 91, active=True)])
        assert alerts and "usage at 90% (now 91%)" in alerts[0][1]


# ---------------------------------------------------------------------------
# 4. Reactive candidates: soonest weekly reset first
# ---------------------------------------------------------------------------


_W_SOON = "2026-06-24T07:00:00+00:00"
_W_LATER = "2026-06-27T07:00:00+00:00"


class TestReactiveWeeklyResetPriority:
    def test_prefers_soonest_weekly_reset_over_headroom(self):
        # #3 has LESS headroom but its weekly window resets sooner -> chosen.
        accts = [_acct(1, 99, 10, active=True),
                 _acct(2, 10, 10, reset7=_W_LATER),
                 _acct(3, 60, 60, reset7=_W_SOON)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 3)

    def test_unknown_weekly_reset_ranks_last(self):
        accts = [_acct(1, 99, 10, active=True),
                 _acct(2, 10, 10),  # no resets_at -> inf
                 _acct(3, 60, 60, reset7=_W_LATER)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 3)

    def test_equal_weekly_reset_falls_back_to_headroom(self):
        accts = [_acct(1, 99, 10, active=True),
                 _acct(2, 60, 60, reset7=_W_SOON),
                 _acct(3, 10, 10, reset7=_W_SOON)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 3)

    def test_jittered_same_boundary_weekly_resets_tie(self):
        a = "2026-06-24T06:59:59.818000+00:00"
        b = "2026-06-24T07:00:00.447000+00:00"
        accts = [_acct(1, 99, 10, active=True),
                 _acct(2, 60, 60, reset7=a),
                 _acct(3, 10, 10, reset7=b)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 3)  # headroom decides

    def test_saturated_soon_reset_candidate_still_excluded(self):
        accts = [_acct(1, 99, 10, active=True),
                 _acct(2, 95, 10, reset7=_W_SOON),   # over the 90% threshold
                 _acct(3, 10, 10, reset7=_W_LATER)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 3)

    def test_fable_exhausted_soon_reset_is_last_resort(self):
        accts = [_acct(1, 99, 10, active=True),
                 _acct(2, 10, 10, reset7=_W_SOON, fable=100),
                 _acct(3, 10, 10, reset7=_W_LATER)]
        assert menubar.decide_auto_switch(accts, 90) == ("switch", 3)
        only_exhausted = [_acct(1, 99, 10, active=True),
                          _acct(2, 10, 10, reset7=_W_SOON, fable=100)]
        assert menubar.decide_auto_switch(only_exhausted, 90) == ("switch", 2)


# ---------------------------------------------------------------------------
# 5. Samples, burn rate, projection, near-limit
# ---------------------------------------------------------------------------


class TestUsageSamples:
    def test_records_one_sample_per_valid_at(self):
        acc = [(1, "a@x", True, _usage(40, 10, valid_at=100.0))]
        s1 = menubar.record_usage_samples({}, acc)
        assert s1 == {"1": ((100.0, 40, 10),)}
        # same validAt again (retained dict under a 429) -> no duplicate
        s2 = menubar.record_usage_samples(s1, acc)
        assert s2 == s1
        # new fetch -> appended
        acc2 = [(1, "a@x", True, _usage(46, 10, valid_at=115.0))]
        s3 = menubar.record_usage_samples(s2, acc2)
        assert s3 == {"1": ((100.0, 40, 10), (115.0, 46, 10))}

    def test_returns_new_dict_and_caps_history(self):
        s = {}
        for i in range(10):
            acc = [(1, "a@x", True, _usage(i, 0, valid_at=100.0 + i))]
            s_next = menubar.record_usage_samples(s, acc)
            assert s_next is not s
            s = s_next
        assert len(s["1"]) == menubar._MAX_SAMPLES
        assert s["1"][-1][0] == 109.0

    def test_drops_accounts_no_longer_present(self):
        s = {"9": ((1.0, 1, 1),)}
        out = menubar.record_usage_samples(s, [(1, "a@x", True, _usage(1, 1, valid_at=5.0))])
        assert "9" not in out and "1" in out

    def test_ignores_sentinels_and_missing_valid_at(self):
        acc = [(1, "a@x", True, "rate limited"), (2, "b@x", False, _usage(1, 1)),
               (3, "c@x", False, None)]
        assert menubar.record_usage_samples({}, acc) == {}

    def test_partial_window_recorded_with_none(self):
        acc = [(1, "a@x", True, {"five_hour": {"pct": 40}, "validAt": 100.0})]
        assert menubar.record_usage_samples({}, acc) == {"1": ((100.0, 40, None),)}


class TestUsageRates:
    def test_rate_from_last_two_samples(self):
        hist = ((100.0, 80, 10), (115.0, 86, 10.5))
        r5, r7 = menubar.usage_rates(hist)
        assert r5 == pytest.approx(0.4)
        assert r7 == pytest.approx(0.5 / 15)

    def test_single_sample_is_zero(self):
        assert menubar.usage_rates(((100.0, 80, 10),)) == (0.0, 0.0)
        assert menubar.usage_rates(()) == (0.0, 0.0)

    def test_decreasing_usage_is_zero_rate(self):
        # the window reset between samples: never project a negative slope
        assert menubar.usage_rates(((100.0, 80, 10), (115.0, 3, 10))) == (0.0, 0.0)

    def test_zero_or_negative_dt_is_zero(self):
        assert menubar.usage_rates(((100.0, 80, 10), (100.0, 90, 10))) == (0.0, 0.0)

    def test_none_window_is_zero(self):
        assert menubar.usage_rates(((100.0, 80, None), (115.0, 86, None)))[1] == 0.0


class TestProjectUsage:
    def test_projects_forward_from_valid_at(self):
        hist = ((100.0, 80, 10), (115.0, 86, 10))
        u = _usage(86, 10, valid_at=115.0)
        p = menubar.project_usage(u, hist, now=125.0)
        assert p["five_hour"]["pct"] == pytest.approx(90)  # 86 + 0.4 * 10
        assert p["seven_day"]["pct"] == 10  # flat window untouched
        assert u["five_hour"]["pct"] == 86  # input not mutated

    def test_caps_at_one_hundred(self):
        hist = ((100.0, 80, 10), (115.0, 95, 10))
        p = menubar.project_usage(_usage(95, 10, valid_at=115.0), hist, now=200.0)
        assert p["five_hour"]["pct"] == 100

    def test_horizon_bounds_extrapolation(self):
        # 0.2%/s; a poll stalled for hours must not extrapolate past the horizon.
        hist = ((100.0, 80, 10), (115.0, 83, 10))
        p = menubar.project_usage(_usage(83, 10, valid_at=115.0), hist, now=10_000.0)
        assert p["five_hour"]["pct"] == pytest.approx(83 + 0.2 * menubar._PROJECTION_HORIZON)
        assert p["five_hour"]["pct"] < 100

    def test_no_history_or_flat_rate_returns_input(self):
        u = _usage(86, 10, valid_at=115.0)
        assert menubar.project_usage(u, (), now=200.0) is u
        assert menubar.project_usage(u, ((100.0, 86, 10), (115.0, 86, 10)), now=200.0) is u
        assert menubar.project_usage("rate limited", ((1.0, 1, 1), (2.0, 5, 5)), now=3.0) == "rate limited"

    def test_project_active_only_touches_active_row(self):
        samples = {"1": ((100.0, 80, 10), (115.0, 86, 10)),
                   "2": ((100.0, 80, 10), (115.0, 86, 10))}
        accts = [(1, "a@x", True, _usage(86, 10, valid_at=115.0)),
                 (2, "b@x", False, _usage(86, 10, valid_at=115.0))]
        out = menubar.project_active(accts, samples, now=125.0)
        assert out[0][3]["five_hour"]["pct"] == pytest.approx(90)
        assert out[1][3]["five_hour"]["pct"] == 86
        assert out[0][:3] == accts[0][:3] and out[1][:3] == accts[1][:3]


class TestActiveLimitingAndNearLimit:
    def test_active_limiting_pct_lenient_on_partial_windows(self):
        assert menubar.active_limiting_pct([(1, "a", True, {"five_hour": {"pct": 42}})]) == 42
        assert menubar.active_limiting_pct([(1, "a", True, _usage(10, 98))]) == pytest.approx(90)
        assert menubar.active_limiting_pct([(1, "a", False, _usage(99, 99))]) is None
        assert menubar.active_limiting_pct([(1, "a", True, "no credentials")]) is None

    def test_near_limit_band(self):
        assert menubar.NEAR_LIMIT_BAND == 10.0
        accts = [_acct(1, 79.9, 10, active=True)]
        assert menubar.active_near_limit(accts, {}, 90, now=0.0) is False
        accts = [_acct(1, 80, 10, active=True)]
        assert menubar.active_near_limit(accts, {}, 90, now=0.0) is True

    def test_near_limit_uses_projection(self):
        samples = {"1": ((100.0, 60, 10), (115.0, 75, 10))}  # 1%/s burn
        accts = [(1, "a@x", True, _usage(75, 10, valid_at=115.0))]
        assert menubar.active_near_limit(accts, samples, 90, now=115.0) is False
        assert menubar.active_near_limit(accts, samples, 90, now=121.0) is True  # ~81%

    def test_near_limit_weekly_axis_scaled(self):
        accts = [_acct(1, 10, 96, active=True)]  # weekly 96 -> 80 session-equivalent
        assert menubar.active_near_limit(accts, {}, 90, now=0.0) is True


class TestPlanAutoEval:
    def test_holds_until_post_switch_snapshot(self):
        assert menubar.plan_auto_eval(
            snapshot_taken_at=100.0, last_eval_snapshot_at=50.0,
            switch_done_at=120.0, active_over=True, now=130.0, fail_until=0.0,
        ) == "hold"

    def test_new_snapshot_evaluates(self):
        assert menubar.plan_auto_eval(
            snapshot_taken_at=100.0, last_eval_snapshot_at=50.0,
            switch_done_at=0.0, active_over=False, now=130.0, fail_until=0.0,
        ) == "evaluate"

    def test_projected_crossing_evaluates_without_new_data(self):
        assert menubar.plan_auto_eval(
            snapshot_taken_at=100.0, last_eval_snapshot_at=100.0,
            switch_done_at=0.0, active_over=True, now=130.0, fail_until=0.0,
        ) == "evaluate"

    def test_otherwise_periodic(self):
        assert menubar.plan_auto_eval(
            snapshot_taken_at=100.0, last_eval_snapshot_at=100.0,
            switch_done_at=0.0, active_over=False, now=130.0, fail_until=0.0,
        ) == "periodic"

    def test_failure_cooldown_holds(self):
        assert menubar.plan_auto_eval(
            snapshot_taken_at=100.0, last_eval_snapshot_at=50.0,
            switch_done_at=0.0, active_over=True, now=130.0, fail_until=200.0,
        ) == "hold"

    def test_no_snapshot_yet_holds(self):
        assert menubar.plan_auto_eval(
            snapshot_taken_at=0.0, last_eval_snapshot_at=0.0,
            switch_done_at=0.0, active_over=False, now=1.0, fail_until=0.0,
        ) == "hold"


class TestProjectedDecision:
    def test_projected_crossing_switches_before_real_sample_crosses(self):
        """80 -> 88 in one 15s poll (0.53%/s). 5s later the projection is ~90.7,
        so the decision switches although the last REAL sample was 88."""
        samples = {"1": ((100.0, 80, 10), (115.0, 88, 10))}
        accts = [(1, "a@x", True, _usage(88, 10, valid_at=115.0)), _acct(2, 5, 5)]
        assert menubar.decide_auto_switch(accts, 90) == ("none", None)  # raw: no
        projected = menubar.project_active(accts, samples, now=120.0)
        assert menubar.decide_auto_switch(projected, 90) == ("switch", 2)

    def test_projected_active_feeds_hysteresis(self):
        samples = {"1": ((100.0, 80, 10), (115.0, 88, 10))}
        accts = [(1, "a@x", True, _usage(88, 10, valid_at=115.0)), _acct(2, 5, 5)]
        projected = menubar.project_active(accts, samples, now=120.0)
        limiting = menubar.limiting_pct_by_account(projected, "reactive")
        blocked = menubar.next_blocked(limiting, 90, menubar.AUTO_HYSTERESIS, frozenset())
        assert blocked == frozenset({"1"})


# ---------------------------------------------------------------------------
# 6. Live in-place updates: checkmark, title, sign-in row
# ---------------------------------------------------------------------------


class _Item:
    def __init__(self, title="", state=0, hidden=False):
        self.title = title
        self.state = state
        self.hidden = hidden


class _LiveApp:
    def __init__(self, accounts, active_email, active_usage):
        self.snapshot = {"accounts": accounts, "active_email": active_email,
                         "active_usage": active_usage, "instances": []}
        self.settings = menubar.MenuBarSettings()
        self.title = "stale"
        self._account_rows = []


class TestLiveRows:
    def _rows(self, accounts):
        rows = []
        for num, email, is_active, usage in accounts:
            label = _Item(title="old", state=1 if is_active else 0)
            details = [_Item(title="old") for _ in menubar.account_detail_lines(usage)]
            reauth = _Item(title="old", hidden=is_active)
            rows.append((num, label, details, reauth))
        return rows

    def test_switch_moves_checkmark_and_title_in_place(self):
        before = [_acct(1, 10, 10, active=True), _acct(2, 5, 5)]
        app = _LiveApp(before, "a1@x.com", before[0][3])
        app._account_rows = self._rows(before)
        r1, r2 = app._account_rows[0], app._account_rows[1]
        assert (r1[1].state, r2[1].state) == (1, 0)

        # A switch lands in the snapshot while the menu is open.
        after = [_acct(1, 10, 10), _acct(2, 5, 5, active=True)]
        app.snapshot = {"accounts": after, "active_email": "a2@x.com",
                        "active_usage": after[1][3], "instances": []}
        menubar._apply_live_rows(app, now=0.0)

        assert (r1[1].state, r2[1].state) == (0, 1)
        assert app.title == menubar.format_title("a2@x.com", after[1][3], app.settings)
        assert r1[1].title == menubar.format_account_label(1, "a1@x.com", after[0][3], 0.0)
        # sign-in row: shown for the now-backup #1, hidden for the now-active #2
        assert r1[3].hidden is False
        assert r2[3].hidden is True
        assert r1[3].title == menubar.reauth_menu_title(after[0][3], False)

    def test_detail_lines_refresh_from_snapshot(self):
        accts = [_acct(1, 10, 10, active=True)]
        app = _LiveApp(accts, "a1@x.com", accts[0][3])
        app._account_rows = self._rows(accts)
        newer = [_acct(1, 55, 10, active=True)]
        app.snapshot = {"accounts": newer, "active_email": "a1@x.com",
                        "active_usage": newer[0][3], "instances": []}
        menubar._apply_live_rows(app, now=0.0)
        lines = menubar.account_detail_lines(newer[0][3])
        assert [d.title for d in app._account_rows[0][2]] == [f"    {l}" for l in lines]

    def test_expired_login_row_title_updates_in_place(self):
        accts = [_acct(1, 10, 10, active=True), _acct(2, 5, 5)]
        app = _LiveApp(accts, "a1@x.com", accts[0][3])
        app._account_rows = self._rows(accts)
        dead = [_acct(1, 10, 10, active=True), (2, "a2@x.com", False, USAGE_TOKEN_EXPIRED)]
        app.snapshot = {"accounts": dead, "active_email": "a1@x.com",
                        "active_usage": dead[0][3], "instances": []}
        menubar._apply_live_rows(app, now=0.0)
        assert app._account_rows[1][3].title == menubar.reauth_menu_title(USAGE_TOKEN_EXPIRED, False)

    def test_rows_for_unknown_accounts_are_left_alone(self):
        accts = [_acct(1, 10, 10, active=True)]
        app = _LiveApp(accts, "a1@x.com", accts[0][3])
        app._account_rows = self._rows(accts) + [(7, _Item("keep"), [], _Item("keep"))]
        menubar._apply_live_rows(app, now=0.0)
        assert app._account_rows[1][1].title == "keep"

    def test_unchanged_values_are_not_rewritten(self):
        """Only differing titles/states are assigned (avoids needless redraws)."""
        writes = []

        class _Spy(_Item):
            def __setattr__(self, k, v):
                writes.append(k)
                super().__setattr__(k, v)

        accts = [_acct(1, 10, 10, active=True)]
        app = _LiveApp(accts, "a1@x.com", accts[0][3])
        label = _Spy(menubar.format_account_label(1, "a1@x.com", accts[0][3], 0.0), state=1)
        details = [_Spy(f"    {l}") for l in menubar.account_detail_lines(accts[0][3])]
        reauth = _Spy(menubar.reauth_menu_title(accts[0][3], False), hidden=True)
        app._account_rows = [(1, label, details, reauth)]
        app.title = menubar.format_title("a1@x.com", accts[0][3], app.settings)
        writes.clear()
        menubar._apply_live_rows(app, now=0.0)
        assert writes == []


class TestThresholdLabel:
    def test_label_shows_weekly_equivalent(self):
        assert menubar.threshold_label(90) == "90%  (weekly 98%)"
        assert menubar.threshold_label(80) == "80%  (weekly 96%)"
        assert menubar.threshold_label(95) == "95%  (weekly 99%)"
