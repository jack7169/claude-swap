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
