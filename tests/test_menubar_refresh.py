"""Tests for the menu bar refresh path (Phase 3 fixes).

These tests never import or run rumps/AppKit. They exercise the import-safe
helpers/methods that back the menu bar's refresh plumbing, driving them
directly, per the module's import-safety pattern. The full ``MenuBarApp`` (a
``rumps.App`` subclass) is *not* instantiated here.

Coverage:
  3.1 — "Refresh now" threads ``force=True`` end to end (``refresh_async`` ->
        ``_worker`` -> ``_snapshot`` -> ``_collect_usage``), and a forced
        refresh requested while a worker is in flight schedules a follow-up
        forced refresh rather than dropping it.
  3.3 — the menu is rebuilt only when the rendered-state signature changes; the
        snapshot computes the running-instance list once and reuses it.
  3.4 — ``_resets_at_ts`` never raises (overflowing timestamp -> inf) and
        normalizes a naive timestamp as UTC; the title "both" mode labels the
        two percentages so they are distinguishable.
  3.2 — switch/add/remove callbacks offload their blocking keychain/lock work
        to a worker thread (the blocking call does not run inline on the
        caller's thread) and the offload guard prevents overlapping workers.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from claude_swap import menubar


# ---------------------------------------------------------------------------
# 3.1 — force flag threaded end to end + queued follow-up
# ---------------------------------------------------------------------------


class _RefreshHarness:
    """Minimal stand-in for the parts of MenuBarApp the refresh path touches.

    Reuses the real, import-safe ``refresh_async``/``_worker`` logic by binding
    the unbound module-level implementations to this object (the same functions
    the rumps ``MenuBarApp`` uses), so we exercise the exact production code
    without instantiating rumps.
    """

    def __init__(self):
        self._refresh_guard = menubar._RefreshGuard()
        self._last_full_fetch = 0.0
        self._snapshot_at = 0.0
        self.snapshot = {
            "accounts": [], "active_email": None, "active_usage": None,
            "instances": [],
        }
        self._dirty = False
        self.switcher = self
        # spawned worker threads, so tests can join them deterministically
        self._threads: list[threading.Thread] = []

    # the switcher methods _worker -> _snapshot relies on
    _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

    def recheck_keychain(self):
        pass

    def _build_accounts_info(self):
        return [(1, "a@x", "", "", True, "")]

    def _collect_usage(self, info, only=None, force=False):
        return [None]

    # spawn worker threads synchronously-joinable for tests
    def _spawn(self, target, args):
        t = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(t)
        t.start()

    def join_all(self, timeout=5.0):
        deadline = time.time() + timeout
        while self._threads and time.time() < deadline:
            t = self._threads.pop(0)
            t.join(timeout=max(0.0, deadline - time.time()))


def test_worker_skips_full_stamp_during_429_backoff(monkeypatch):
    """A full refresh served under the 429 backoff must not claim freshness.

    During the backoff ``_collect_usage`` returns cached data without any
    network fetch; stamping ``_last_full_fetch`` then would let the auto-switch
    evaluate frozen usage believing it just fetched it (the frozen-usage bug).
    """
    monkeypatch.setattr(
        menubar, "_snapshot",
        lambda switcher, full=True, force=False: {
            "accounts": [], "active_email": None, "active_usage": None,
            "instances": [],
        },
    )

    app = _RefreshHarness()
    app._rate_limited_until = lambda: time.time() + 60  # backoff active
    menubar._refresh_async_impl(app, full=True, force=False)
    app.join_all()
    assert app._last_full_fetch == 0.0  # not stamped: nothing was fetched

    app._rate_limited_until = lambda: 0.0  # backoff over
    menubar._refresh_async_impl(app, full=True, force=False)
    app.join_all()
    assert app._last_full_fetch > 0.0  # honest full fetch → stamped


def test_refresh_async_force_threads_force_to_snapshot(monkeypatch):
    """on_refresh_now -> refresh_async(force=True) -> _snapshot(force=True)."""
    seen = {}

    def fake_snapshot(switcher, full=True, force=False):
        seen["full"] = full
        seen["force"] = force
        return {"accounts": [], "active_email": None, "active_usage": None,
                "instances": []}

    monkeypatch.setattr(menubar, "_snapshot", fake_snapshot)

    app = _RefreshHarness()
    menubar._refresh_async_impl(app, full=True, force=True)
    app.join_all()

    assert seen["force"] is True
    assert seen["full"] is True


def test_snapshot_threads_force_to_collect_usage(monkeypatch):
    """_snapshot(force=True) passes force through to _collect_usage."""
    seen = {}

    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

        def _build_accounts_info(self):
            return [(1, "a@x", "", "", True, "")]

        def _collect_usage(self, info, only=None, force=False):
            seen["force"] = force
            seen["only"] = only
            return [None]

    monkeypatch.setattr(menubar, "_snapshot_instances", lambda sw: [])
    menubar._snapshot(_SW(), full=True, force=True)
    assert seen["force"] is True
    assert seen["only"] is None


def test_forced_refresh_while_in_flight_schedules_followup(monkeypatch):
    """A forced refresh while a worker is in flight queues a follow-up.

    The first worker is held mid-fetch; a second forced refresh arrives and
    must NOT be dropped — it records a pending forced refresh that runs as a
    follow-up worker once the first finishes.
    """
    started = []
    release = threading.Event()
    first_in_snapshot = threading.Event()

    def fake_snapshot(switcher, full=True, force=False):
        started.append(force)
        if len(started) == 1:
            first_in_snapshot.set()
            release.wait(timeout=5)  # hold the first worker mid-fetch
        return {"accounts": [], "active_email": None, "active_usage": None,
                "instances": []}

    monkeypatch.setattr(menubar, "_snapshot", fake_snapshot)

    app = _RefreshHarness()
    # First forced refresh wins the slot and blocks in _snapshot.
    menubar._refresh_async_impl(app, full=True, force=True)
    assert first_in_snapshot.wait(timeout=5)
    # Second forced refresh while in flight: must be queued, not dropped.
    menubar._refresh_async_impl(app, full=True, force=True)
    # Let the first worker complete; it should kick off the queued follow-up.
    release.set()
    app.join_all()

    # Two forced fetches ran in total (the in-flight one plus the queued one).
    assert started == [True, True]


def test_worker_census_logs_anomaly_when_concurrency_exceeds_one(monkeypatch):
    """The census logs a warning (with a stack) only when >1 worker is live.

    Instrumentation to capture the production worker-multiplication runaway: the
    guard's invariant is one live refresh worker; a second concurrent admit is an
    anomaly and must be recorded with the admitting call stack.
    """
    warnings: list[tuple] = []

    class _App:
        switcher = type(
            "S", (),
            {"_logger": type("L", (), {"warning": staticmethod(
                lambda *a, **k: warnings.append(a))})()},
        )()

    monkeypatch.setattr(menubar, "_live_workers", 0, raising=False)

    app = _App()
    assert menubar._census_admit(app) == 1  # first worker: within invariant
    assert warnings == []                    # no anomaly logged

    assert menubar._census_admit(app) == 2   # second concurrent worker: anomaly
    assert len(warnings) == 1
    logged = " ".join(str(x) for x in warnings[0])
    assert "concurrency" in logged.lower()

    menubar._census_release()
    menubar._census_release()
    # released back to zero; a fresh single admit is again clean
    warnings.clear()
    assert menubar._census_admit(app) == 1
    assert warnings == []
    menubar._census_release()


def test_nonforced_refresh_while_in_flight_is_dropped(monkeypatch):
    """A plain (non-forced) tick while in flight is still dropped (no follow-up)."""
    started = []
    release = threading.Event()
    first_in_snapshot = threading.Event()

    def fake_snapshot(switcher, full=True, force=False):
        started.append(force)
        if len(started) == 1:
            first_in_snapshot.set()
            release.wait(timeout=5)
        return {"accounts": [], "active_email": None, "active_usage": None,
                "instances": []}

    monkeypatch.setattr(menubar, "_snapshot", fake_snapshot)

    app = _RefreshHarness()
    menubar._refresh_async_impl(app, full=True, force=True)
    assert first_in_snapshot.wait(timeout=5)
    # Non-forced tick while in flight: dropped, no queued follow-up.
    menubar._refresh_async_impl(app, full=False, force=False)
    release.set()
    app.join_all()

    assert started == [True]  # only the first worker ran


# ---------------------------------------------------------------------------
# 3.1 — _RefreshGuard pending-force mechanics (unit, no threads)
# ---------------------------------------------------------------------------


def test_guard_try_begin_force_records_pending_when_in_flight():
    guard = menubar._RefreshGuard()
    assert guard.try_begin(force=True) is True  # wins the slot
    # in flight; a forced request is rejected but remembered
    assert guard.try_begin(force=True) is False
    # finishing reports there is a queued forced refresh
    assert guard.finish_and_take_pending() is True
    # the pending flag is consumed (one follow-up only)
    assert guard.finish_and_take_pending() is False


def test_guard_nonforced_while_in_flight_sets_no_pending():
    guard = menubar._RefreshGuard()
    assert guard.try_begin(force=False) is True
    assert guard.try_begin(force=False) is False  # dropped
    assert guard.finish_and_take_pending() is False  # nothing queued


# ---------------------------------------------------------------------------
# 3.3 — rebuild only on signature change; instances computed once
# ---------------------------------------------------------------------------


def test_snapshot_signature_stable_for_equal_state():
    snap = {
        "accounts": [(1, "a@x", True, {"five_hour": {"pct": 10.0}})],
        "active_email": "a@x",
        "active_usage": {"five_hour": {"pct": 10.0}},
        "instances": [("ide", "~/p", 1, True)],
    }
    settings = menubar.MenuBarSettings()
    sig1 = menubar._snapshot_signature(snap, settings)
    sig2 = menubar._snapshot_signature(dict(snap), settings)
    assert sig1 == sig2


def test_snapshot_signature_changes_when_state_changes():
    settings = menubar.MenuBarSettings()
    base = {
        "accounts": [(1, "a@x", True, {"five_hour": {"pct": 10.0}})],
        "active_email": "a@x",
        "active_usage": {"five_hour": {"pct": 10.0}},
        "instances": [],
    }
    sig_base = menubar._snapshot_signature(base, settings)

    changed_usage = {**base, "accounts": [(1, "a@x", True, {"five_hour": {"pct": 99.0}})]}
    assert menubar._snapshot_signature(changed_usage, settings) != sig_base

    changed_instances = {**base, "instances": [("ide", "~/p", 1, True)]}
    assert menubar._snapshot_signature(changed_instances, settings) != sig_base

    changed_settings = menubar.MenuBarSettings(title_pct="off")
    assert menubar._snapshot_signature(base, changed_settings) != sig_base


def test_sync_tick_skips_rebuild_when_signature_unchanged():
    """on_sync_tick rebuilds only when the rendered-state signature changed."""
    rebuilds = []

    class _App:
        def __init__(self):
            self.snapshot = {
                "accounts": [], "active_email": None, "active_usage": None,
                "instances": [],
            }
            self.settings = menubar.MenuBarSettings()
            self._dirty = False
            self._menu_sig = None

        def rebuild_menu(self):
            rebuilds.append(self._snapshot_sig_now())

        def _snapshot_sig_now(self):
            return menubar._snapshot_signature(self.snapshot, self.settings)

    app = _App()

    # First dirty pass with an empty snapshot -> rebuild once, sig recorded.
    app._dirty = True
    menubar._maybe_rebuild_on_dirty(app)
    assert len(rebuilds) == 1

    # Dirty again but nothing changed -> skip rebuild.
    app._dirty = True
    menubar._maybe_rebuild_on_dirty(app)
    assert len(rebuilds) == 1

    # State changes -> rebuild fires again.
    app.snapshot = {
        "accounts": [(1, "a@x", True, None)], "active_email": "a@x",
        "active_usage": None, "instances": [],
    }
    app._dirty = True
    menubar._maybe_rebuild_on_dirty(app)
    assert len(rebuilds) == 2


def test_snapshot_computes_instances_once(monkeypatch):
    """_snapshot scans running instances exactly once per refresh."""
    calls = {"n": 0}

    def counting_instances(switcher):
        calls["n"] += 1
        return []

    monkeypatch.setattr(menubar, "_snapshot_instances", counting_instances)

    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

        def _build_accounts_info(self):
            return [(1, "a@x", "", "", True, "")]

        def _collect_usage(self, info, only=None, force=False):
            return [None]

    menubar._snapshot(_SW(), full=True)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 3.4 — _resets_at_ts robustness + title labels
# ---------------------------------------------------------------------------


def test_resets_at_ts_returns_inf_on_overflowing_timestamp():
    """A far-future year overflows .timestamp() on some platforms; must not raise."""
    # datetime.max year; .timestamp() raises OverflowError/OSError on many libcs.
    far = {"resets_at": "9999-12-31T23:59:59+00:00"}
    val = menubar._resets_at_ts(far)
    assert val == float("inf") or isinstance(val, float)
    # An out-of-range / unparseable epoch must never raise.
    assert menubar._resets_at_ts({"resets_at": "0001-01-01T00:00:00+00:00"}) is not None


def test_resets_at_ts_normalizes_naive_as_utc():
    """A timezone-naive resets_at is interpreted as UTC, not local time."""
    naive = {"resets_at": "2026-06-24T07:00:00"}
    aware = {"resets_at": "2026-06-24T07:00:00+00:00"}
    assert menubar._resets_at_ts(naive) == menubar._resets_at_ts(aware)
    # And it equals the explicit UTC epoch.
    expected = datetime(2026, 6, 24, 7, 0, 0, tzinfo=timezone.utc).timestamp()
    assert menubar._resets_at_ts(naive) == expected


def test_resets_at_ts_never_raises_on_overflow_branch(monkeypatch):
    """Even when .timestamp() raises OverflowError/OSError, fall through to inf."""

    class _BadInstance:
        tzinfo = timezone.utc  # already tz-aware so the naive-normalize branch is skipped

        def timestamp(self):
            raise OverflowError("boom")

    class _FakeDatetime:
        @staticmethod
        def fromisoformat(s):
            return _BadInstance()

    # Replace the module's datetime reference so .timestamp() raises OverflowError.
    monkeypatch.setattr(menubar, "datetime", _FakeDatetime)
    assert menubar._resets_at_ts({"resets_at": "whatever"}) == float("inf")


def test_title_both_mode_labels_windows():
    """The title 'both' mode labels the two percentages so they're distinguishable."""
    usage = {"five_hour": {"pct": 12.0}, "seven_day": {"pct": 34.0}}
    settings = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    title = menubar.format_title("a@x.com", usage, settings)
    # Both percentages present and each carries a short window label.
    assert "12%" in title
    assert "34%" in title
    assert "5h" in title
    assert "7d" in title


# ---------------------------------------------------------------------------
# 3.2 — switch/add/remove callbacks offload off the main (caller's) thread
# ---------------------------------------------------------------------------


def test_offload_runs_blocking_work_off_caller_thread():
    """The blocking action runs on a worker thread, not inline on the caller."""
    guard = menubar._RefreshGuard()
    caller_thread = threading.current_thread()
    ran_on = {}
    done = threading.Event()

    def blocking():
        ran_on["thread"] = threading.current_thread()
        done.set()

    started = menubar._offload_action(guard, blocking)
    assert started is True
    assert done.wait(timeout=5)
    assert ran_on["thread"] is not caller_thread


def test_offload_drops_overlapping_action_while_in_flight():
    """A second offload while one is in flight is rejected (no overlap)."""
    guard = menubar._RefreshGuard()
    release = threading.Event()
    first_started = threading.Event()
    runs = []

    def blocking():
        runs.append(1)
        first_started.set()
        release.wait(timeout=5)

    assert menubar._offload_action(guard, blocking) is True
    assert first_started.wait(timeout=5)
    # While the first worker holds the slot, a second is rejected.
    assert menubar._offload_action(guard, lambda: runs.append(2)) is False
    release.set()
    # give the first worker a moment to finish and free the slot
    deadline = time.time() + 5
    while guard.in_flight and time.time() < deadline:
        time.sleep(0.01)
    assert runs == [1]
    # After it finishes the slot is free again.
    assert guard.in_flight is False


def test_offload_marshals_dirty_back_for_sync_tick():
    """After the worker completes, the app is marked dirty for the sync tick."""
    guard = menubar._RefreshGuard()

    class _App:
        def __init__(self):
            self._dirty = False

    app = _App()
    done = threading.Event()

    def action():
        done.set()

    menubar._offload_action(guard, action, on_done=lambda: setattr(app, "_dirty", True))
    assert done.wait(timeout=5)
    deadline = time.time() + 5
    while not app._dirty and time.time() < deadline:
        time.sleep(0.01)
    assert app._dirty is True


# --- auto-tick cadence: evaluate on a fresh FULL fetch, like "Refresh now" ----
# The auto-switcher must not decide on the routine active-only snapshot; a recent
# partial display refresh kept _snapshot_at fresh and masked stale backups, so a
# switch only fired after a manual full+forced "Refresh now". plan_auto_tick gates
# the pre-eval refresh on the last *full* fetch instead.

def test_plan_auto_tick_waits_within_cadence():
    assert menubar.plan_auto_tick(
        now=10.0, last_eval=5.0, last_full_fetch=5.0, cadence=30, in_flight=False
    ) == "wait"


def test_plan_auto_tick_refreshes_when_full_fetch_is_stale():
    # Eval is due (last_eval old) and the last FULL fetch is older than cadence,
    # even though a partial refresh may have updated the snapshot recently.
    assert menubar.plan_auto_tick(
        now=100.0, last_eval=0.0, last_full_fetch=0.0, cadence=30, in_flight=False
    ) == "refresh"


def test_plan_auto_tick_waits_instead_of_evaluating_on_stale_while_in_flight():
    # A full fetch is needed but a worker holds the slot: wait for it rather than
    # evaluate on stale/partial data.
    assert menubar.plan_auto_tick(
        now=100.0, last_eval=0.0, last_full_fetch=0.0, cadence=30, in_flight=True
    ) == "wait"


def test_plan_auto_tick_evaluates_when_full_fetch_is_fresh():
    assert menubar.plan_auto_tick(
        now=100.0, last_eval=0.0, last_full_fetch=90.0, cadence=30, in_flight=False
    ) == "evaluate"


def test_rebuild_deferred_while_menu_open_then_runs_on_close():
    """A rebuild requested while the menu is open is deferred (tearing down the
    NSMenu under an open menu collapses it -> flicker) and runs on close."""
    rebuilds = []

    class _App:
        def __init__(self):
            self.snapshot = {
                "accounts": [], "active_email": None, "active_usage": None,
                "instances": [],
            }
            self.settings = menubar.MenuBarSettings()
            self._dirty = False
            self._menu_sig = None
            self._menu_open = False

        def rebuild_menu(self):
            rebuilds.append(menubar._snapshot_signature(self.snapshot, self.settings))

    app = _App()
    app._dirty = True
    menubar._maybe_rebuild_on_dirty(app)
    assert len(rebuilds) == 1  # baseline render

    # Visible state changes while the menu is open -> defer; keep _dirty pending.
    app._menu_open = True
    app.snapshot = {
        "accounts": [(1, "a@x", True, None)], "active_email": "a@x",
        "active_usage": None, "instances": [],
    }
    app._dirty = True
    assert menubar._maybe_rebuild_on_dirty(app) is False
    assert len(rebuilds) == 1
    assert app._dirty is True  # still pending for the close handler

    # Menu closes -> the deferred rebuild runs exactly once.
    app._menu_open = False
    assert menubar._maybe_rebuild_on_dirty(app) is True
    assert len(rebuilds) == 2


def test_unchanged_signature_consumes_dirty_even_while_open():
    """If nothing visible changed, _dirty is consumed (no pending rebuild) even
    while open, so closing the menu doesn't trigger a needless rebuild."""
    rebuilds = []

    class _App:
        def __init__(self):
            self.snapshot = {
                "accounts": [], "active_email": None, "active_usage": None,
                "instances": [],
            }
            self.settings = menubar.MenuBarSettings()
            self._dirty = False
            self._menu_sig = None
            self._menu_open = False

        def rebuild_menu(self):
            rebuilds.append(1)

    app = _App()
    app._dirty = True
    menubar._maybe_rebuild_on_dirty(app)  # baseline
    assert len(rebuilds) == 1

    app._menu_open = True
    app._dirty = True  # dirty but snapshot/settings unchanged
    assert menubar._maybe_rebuild_on_dirty(app) is False
    assert app._dirty is False  # consumed; nothing pending
