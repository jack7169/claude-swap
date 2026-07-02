"""Fork serialization for the menu-bar hot path.

Forking from the multithreaded, AppKit-initialized menu-bar process is only
safe one at a time: concurrent ``fork()`` calls serialize inside the malloc
atfork handlers (``_malloc_fork_prepare``) and livelock, freezing the app
(observed in the wild as ~14 refresh workers all wedged in ``do_fork_exec``).
Every ``subprocess`` spawn in the credential / process-detection hot paths must
therefore hold a single process-wide lock so at most one ``fork()`` is ever in
flight. These tests pin that invariant.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from claude_swap import macos_keychain, process_detection


class _ConcurrencyProbe:
    """Records the maximum number of callers simultaneously inside the spawn."""

    def __init__(self, hold: float = 0.02):
        self.hold = hold
        self.live = 0
        self.max_live = 0
        self._lock = threading.Lock()

    def run(self, *args, **kwargs):
        with self._lock:
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        time.sleep(self.hold)  # model the fork+exec+wait window
        with self._lock:
            self.live -= 1
        return types.SimpleNamespace(returncode=0, stdout="secret\n", stderr="")


def _run_threads(target, n=8):
    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)


@pytest.mark.no_keychain_fake
def test_concurrent_keychain_reads_never_fork_concurrently(monkeypatch):
    """Eight concurrent ``get_password`` calls must serialize their forks."""
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(macos_keychain.subprocess, "run", probe.run)

    _run_threads(lambda: macos_keychain.get_password("svc", "acct"))

    assert probe.max_live == 1


@pytest.mark.no_keychain_fake
def test_keychain_and_process_detection_share_one_fork_lock(monkeypatch):
    """A keychain read and a ``ps`` probe must serialize against *each other*.

    Proves the lock is process-wide (one object shared across modules), not a
    per-module lock — otherwise cross-module concurrent forks still storm.
    """
    probe = _ConcurrencyProbe()

    def ps_run(*args, **kwargs):
        # process_detection parses stdout as ps lstart output; return a value it
        # can parse (or empty -> None). Empty stdout keeps the probe simple.
        with probe._lock:
            probe.live += 1
            probe.max_live = max(probe.max_live, probe.live)
        time.sleep(probe.hold)
        with probe._lock:
            probe.live -= 1
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(macos_keychain.subprocess, "run", probe.run)
    monkeypatch.setattr(process_detection.subprocess, "run", ps_run)

    def mixed(i=[0]):
        # alternate between the two fork sites
        n = threading.current_thread().name
        if n.endswith(("0", "2", "4", "6")):
            macos_keychain.get_password("svc", "acct")
        else:
            process_detection.get_process_start_time(1234)

    _run_threads(mixed)

    assert probe.max_live == 1
