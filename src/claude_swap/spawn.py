"""Process-wide serialization of subprocess spawns (one ``fork()`` at a time).

The macOS menu-bar app (``menubar.py``) is a multithreaded, AppKit-initialized
process. Forking from such a process is only safe **one at a time**: every
``subprocess`` spawn takes the fork+exec path (bare-name execs like ``ps`` and
``close_fds=True`` both disqualify ``posix_spawn`` on CPython), and ``fork()``
runs the malloc atfork handlers (``_malloc_fork_prepare``) which acquire *all*
malloc-zone locks. When several threads fork concurrently those handlers
contend O(N^2) on the global malloc locks and livelock — observed in the wild
as ~14 refresh workers wedged in ``do_fork_exec`` with the menu bar frozen
(the Cocoa main thread stays responsive, but no refresh ever completes).

Holding this one lock around each spawn guarantees at most one ``fork()`` is in
flight at any instant — the known-good behavior the app always had when a single
refresh worker ran at a time. In normal operation (one worker) it is
uncontended; under a pathological worker burst it degrades a *freeze* into
*slower-but-progressing* serial work.

Usage — keep the ``subprocess`` call in its own module (so existing tests that
patch ``<module>.subprocess.run`` still intercept) and wrap it::

    from claude_swap import spawn
    with spawn.fork_lock:
        result = subprocess.run([...], ...)

The lock is a plain ``threading.Lock`` (usable directly as a context manager).
It is re-entrant-unsafe on purpose: no wrapped spawn nests another wrapped
spawn, so a single non-reentrant lock is correct and cheapest.
"""

from __future__ import annotations

import threading

# Module-level so it is a single shared instance for the whole process; every
# fork site in the menu-bar hot path acquires this same object.
fork_lock = threading.Lock()
