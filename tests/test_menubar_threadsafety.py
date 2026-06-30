"""Thread-safety tests for the menu bar refresh guard.

These tests never import or run rumps/AppKit. They exercise the import-safe
``_RefreshGuard`` helper that backs the menu bar's in-flight refresh guard,
driving it directly from multiple threads. The full ``MenuBarApp`` (a
``rumps.App`` subclass) is *not* instantiated here — only the pure
synchronization logic extracted out of it, per the module's import-safety
pattern.

Background (Phase 2 fix 2.3):
  (a) The old in-flight guard was a check-then-set across threads
      (``if self._refreshing: return; self._refreshing = True``) with no
      synchronization, so two threads could both pass the check and each spawn
      a worker; the reset also happened from the worker thread while the main
      thread read the flag.
  (b) ``recheck_keychain()`` (called from the worker) mutates the switcher's
      shared keychain-capability cache; the main thread reads it. Mutation must
      be confined/serialized so a main-thread read can't see torn state.
"""

from __future__ import annotations

import threading

from claude_swap import menubar


# ---------------------------------------------------------------------------
# _RefreshGuard: the compare-and-set in-flight guard
# ---------------------------------------------------------------------------


def test_guard_starts_idle():
    guard = menubar._RefreshGuard()
    assert guard.in_flight is False


def test_try_begin_returns_true_once_then_false_until_finished():
    guard = menubar._RefreshGuard()
    # First caller wins the right to start a worker.
    assert guard.try_begin() is True
    assert guard.in_flight is True
    # A second caller while one is in flight is rejected (no duplicate worker).
    assert guard.try_begin() is False
    assert guard.try_begin() is False
    # After the worker finishes the guard frees up again.
    guard.finish()
    assert guard.in_flight is False
    assert guard.try_begin() is True


def test_finish_when_idle_is_safe():
    guard = menubar._RefreshGuard()
    # A spurious finish() with nothing in flight must not raise or go negative.
    guard.finish()
    assert guard.in_flight is False
    assert guard.try_begin() is True


def test_concurrent_try_begin_admits_exactly_one_winner():
    """Under a barrier, N threads race try_begin(); exactly one must win.

    With the old unsynchronized check-then-set, multiple threads could pass the
    `if not in_flight` check before any of them set the flag, so more than one
    would "win" and each spawn a worker. The lock must serialize the
    check-and-flip so at most one start happens.
    """
    n = 64
    guard = menubar._RefreshGuard()
    barrier = threading.Barrier(n)
    winners: list[bool] = []
    winners_lock = threading.Lock()

    def race():
        barrier.wait()  # line all threads up so they hit try_begin together
        won = guard.try_begin()
        with winners_lock:
            winners.append(won)

    threads = [threading.Thread(target=race) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == n
    assert sum(1 for w in winners if w) == 1, "exactly one thread may start a worker"
    assert guard.in_flight is True


def test_repeated_race_rounds_each_admit_one_worker():
    """Across many rounds (begin race -> single finish), counts stay exact.

    Models the steady state of the menu bar: repeated bursts of refresh
    requests, each of which must start at most one worker.
    """
    n = 32
    guard = menubar._RefreshGuard()
    for _ in range(50):
        barrier = threading.Barrier(n)
        started = []
        started_lock = threading.Lock()

        def race():
            barrier.wait()
            if guard.try_begin():
                with started_lock:
                    started.append(1)

        threads = [threading.Thread(target=race) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(started) == 1
        guard.finish()  # the single worker completes before the next round
        assert guard.in_flight is False


# ---------------------------------------------------------------------------
# run_exclusive: serialized critical section (keychain-recheck confinement)
# ---------------------------------------------------------------------------


def test_run_exclusive_serializes_callbacks():
    """The guard's critical-section lock must never run two bodies at once.

    The menu bar uses this to confine the keychain-capability-cache mutation
    (``recheck_keychain`` plus the snapshot's keychain reads) so a concurrent
    reader can't observe a torn state. We assert mutual exclusion by detecting
    any overlap of the protected body.
    """
    guard = menubar._RefreshGuard()
    overlap_detected = []
    active = {"n": 0}
    active_lock = threading.Lock()
    start = threading.Barrier(8)

    def body():
        start.wait()
        for _ in range(200):
            with guard.run_exclusive():
                with active_lock:
                    active["n"] += 1
                    if active["n"] != 1:
                        overlap_detected.append(True)
                # tiny window where an unsynchronized peer would overlap
                with active_lock:
                    active["n"] -= 1

    threads = [threading.Thread(target=body) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap_detected == [], "run_exclusive must serialize its body"


def test_run_exclusive_independent_of_in_flight_flag():
    """run_exclusive() does not consume the in-flight slot.

    The exclusive section guards the capability-cache mutation; it is a separate
    concern from the one-worker-at-a-time admission, so entering it must not
    change ``in_flight``.
    """
    guard = menubar._RefreshGuard()
    assert guard.try_begin() is True
    with guard.run_exclusive():
        assert guard.in_flight is True
    assert guard.in_flight is True
    guard.finish()
    assert guard.in_flight is False
