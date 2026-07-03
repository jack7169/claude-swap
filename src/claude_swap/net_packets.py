"""Per-process packet-rate sampling for the menu-bar stats graph.

Import-safe: no AppKit/rumps at module import (the render layer lives in
``menubar.py``). This module owns the pure parsing/aggregation of ``nettop``
output plus a small threaded driver (:class:`PacketRateMonitor`) that turns one
long-lived ``nettop`` stream into a rolling window of packets/sec.

``nettop`` reports **per-process, cumulative** packet counters, unprivileged
(no sudo/BPF). We attribute Claude traffic by PID (cswap + the Claude Code
CLI/IDE processes) rather than by remote IP, which is impossible on Claude's
shared Cloudflare addresses.
"""

from __future__ import annotations

import collections
import os
import subprocess
import threading

from claude_swap import spawn

# Verified against real output 2026-07-03. -L 0 streams forever, one sample/sec;
# each sample re-emits the header line, then one row per process.
NETTOP_ARGS = [
    "nettop", "-P", "-x", "-L", "0", "-s", "1", "-J", "time,packets_in,packets_out",
]

# Every sample block starts with this line; data rows start with a timestamp.
_HEADER_PREFIX = "time,"


def parse_nettop_sample(block: str) -> dict[int, tuple[int, int]]:
    """Parse one ``nettop`` sample block into ``{pid: (packets_in, packets_out)}``.

    A data row is ``<time>,<name>.<pid>,<packets_in>,<packets_out>,``. The header
    line, blank lines, and any row without a trailing numeric ``.pid`` or
    non-integer counter columns are skipped. Never raises.
    """
    result: dict[int, tuple[int, int]] = {}
    for line in block.splitlines():
        parts = line.split(",")
        if len(parts) < 4:
            continue
        name_pid = parts[1]
        if "." not in name_pid:
            continue
        pid_str = name_pid.rsplit(".", 1)[1]
        if not pid_str.isdigit():
            continue
        try:
            packets_in = int(parts[2])
            packets_out = int(parts[3])
        except ValueError:
            continue
        result[int(pid_str)] = (packets_in, packets_out)
    return result


def sum_targets(sample: dict[int, tuple[int, int]], pids: set[int]) -> int:
    """Cumulative in+out packets across the target PIDs present in the sample."""
    total = 0
    for pid in pids:
        counters = sample.get(pid)
        if counters is not None:
            total += counters[0] + counters[1]
    return total


def delta_rate(prev_total: int | None, cur_total: int) -> int:
    """Per-second packets = ``max(0, cur - prev)``.

    Returns ``0`` for the first sample (``prev_total is None`` — establishes a
    baseline) or when the cumulative total went backwards (a target PID exited
    or counters reset), so a disappearing process never shows as a negative
    spike.
    """
    if prev_total is None:
        return 0
    return max(0, cur_total - prev_total)


def normalize(values: list[int]) -> list[float]:
    """Scale a window of rates to ``[0.0, 1.0]`` against the window max.

    Empty input -> ``[]``; an all-zero window -> all ``0.0``.
    """
    if not values:
        return []
    peak = max(values)
    if peak <= 0:
        return [0.0] * len(values)
    return [v / peak for v in values]


def moving_average(values: list[int], window: int) -> list[float]:
    """Trailing moving average: element ``i`` is the mean of the last ``window``
    values up to and including ``i`` (fewer near the start).

    Smooths the per-sample noise into a rolling window (e.g. a 2-sample window
    over 1 Hz samples is a 2-second average). Empty input -> ``[]``.
    """
    if not values:
        return []
    window = max(1, window)
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        chunk = values[lo:i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


class _NettopStream:
    """Adapts a live ``nettop`` subprocess to the sampler stream contract.

    ``nettop -L 0`` **full-buffers** its stdout when it is a pipe, so a pipe
    only yields output once ~8 KB accumulates (many seconds of lag, or nothing
    until exit). Attaching a pseudo-terminal makes ``nettop`` line-buffer the
    way it does in a real terminal, so each 1 Hz sample flushes promptly. We
    read the pty master fd and split it into newline-terminated lines.

    The child is also made a **session leader with the pty slave as its
    controlling terminal** (see :func:`_spawn_nettop`), so that when this
    process dies for *any* reason — graceful quit, ``SIGTERM``, ``SIGKILL``, or
    a crash — closing the master fd delivers ``SIGHUP`` to ``nettop`` and it
    exits within one sample. This is the real anti-orphan guarantee; the
    explicit :meth:`close` below is belt-and-suspenders for the normal path.
    """

    def __init__(self, proc: subprocess.Popen, master_fd: int):
        self._proc = proc
        self._master_fd = master_fd
        self.lines = self._iter_lines()

    def _iter_lines(self):
        buf = b""
        try:
            while True:
                chunk = os.read(self._master_fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    # Re-attach the newline so the reader's block join and
                    # ``parse_nettop_sample`` see line-delimited text.
                    yield (line + b"\n").decode("utf-8", "replace")
        except OSError:
            # Reading the pty master raises EIO once the child exits; a clean
            # end of stream, not an error.
            pass

    def close(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass
        try:
            os.close(self._master_fd)
        except Exception:
            pass


def _spawn_nettop() -> _NettopStream:
    """Launch the one long-lived ``nettop`` stream (single fork, under the lock).

    Runs ``nettop`` under a pty (see :class:`_NettopStream`) so it line-buffers,
    and makes it a session leader owning the pty slave as its controlling
    terminal so it receives ``SIGHUP`` (and dies) the moment we close the master
    fd — including implicit close on our process death, orphan-proofing it.

    ``pty``/``fcntl``/``termios`` are imported lazily to keep this module
    import-safe on non-Unix.
    """
    import fcntl
    import pty
    import termios

    def _preexec():
        # Async-signal-safe only: new session, then claim the slave (fd 0) as
        # the controlling tty. No Python-level locks touched between fork+exec.
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    master, slave = pty.openpty()
    with spawn.fork_lock:
        proc = subprocess.Popen(
            NETTOP_ARGS,
            stdin=slave,
            stdout=slave,
            stderr=subprocess.DEVNULL,
            preexec_fn=_preexec,
            close_fds=True,
        )
    os.close(slave)  # our copy; nettop holds its own
    return _NettopStream(proc, master)


class PacketRateMonitor:
    """Turns one long-lived ``nettop`` stream into a rolling packets/sec window.

    Thread model: ``start()`` launches the stream via the injected ``sampler``
    and a daemon reader thread that accumulates stdout into per-sample blocks
    (delimited by the ``time,`` header that begins each sample), computes the
    in+out delta over the current target PIDs, and pushes it onto a bounded
    deque under a lock. The UI reads :meth:`rates` / :meth:`current` from any
    thread. All reader-loop errors are swallowed: if ``nettop`` dies or is
    absent, the deque simply stops updating and the graph shows its empty state.
    """

    def __init__(self, *, window: int = 30, avg_window: int = 2, sampler=None):
        self._sampler = sampler or _spawn_nettop
        # Samples to average for display. nettop samples at 1 Hz (its floor —
        # it rejects sub-second ``-s``), so 2 samples ≈ a 2-second rolling
        # average. Exposed so the graph view can smooth the plotted series.
        self.avg_window = avg_window
        self._rates: collections.deque[int] = collections.deque(maxlen=window)
        self._prev_total: int | None = None
        self._pids: set[int] = set()
        self._lock = threading.Lock()
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def set_target_pids(self, pids: set[int]) -> None:
        with self._lock:
            self._pids = set(pids)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stream = self._sampler()
            self._thread = threading.Thread(target=self._read_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        stream = self._stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def rates(self) -> list[int]:
        with self._lock:
            return list(self._rates)

    def current(self) -> int:
        with self._lock:
            return self._rates[-1] if self._rates else 0

    def _read_loop(self) -> None:
        block: list[str] = []
        try:
            for line in self._stream.lines:
                if self._stopped:
                    break
                if line.startswith(_HEADER_PREFIX):
                    # Header begins a new sample; the previous block is complete.
                    if block:
                        self._consume("".join(block))
                        block = []
                else:
                    block.append(line)
            if block and not self._stopped:
                self._consume("".join(block))
        except Exception:
            pass

    def _consume(self, block_text: str) -> None:
        sample = parse_nettop_sample(block_text)
        with self._lock:
            cur = sum_targets(sample, self._pids)
            rate = delta_rate(self._prev_total, cur)
            self._prev_total = cur
            self._rates.append(rate)
