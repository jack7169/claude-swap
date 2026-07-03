# Menu-bar Packet-Rate Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live Stats-style drawn graph to the top of the `cswap` menu-bar dropdown showing packets/sec (in+out) for the machine's Claude processes (cswap + Claude Code CLI/IDE), 30-second history, ~1 Hz update.

**Architecture:** A pure, testable **data layer** (`net_packets.py`) parses one long-lived `nettop` stream into per-second packet-rate deltas held in a 30-slot ring buffer. A thin **render layer** in `menubar.py` (a lazily-defined `NSView` subclass drawing the chart, hosted in an `NSMenuItem` pinned to the top of the dropdown) reads that buffer each tick and draws it. The refresh worker keeps the monitor's target-PID set current.

**Tech Stack:** Python 3.12+, `nettop` (built-in macOS, unprivileged), pyobjc/AppKit (via the optional `[menubar]` extra), rumps.

## Global Constraints

- **Python 3.12+**, hatchling, `uv`. No linter/formatter/type-checker — do **not** invent lint commands.
- **`net_packets.py` MUST be import-safe** without the `[menubar]` extra: no top-level `AppKit`/`Foundation`/`rumps` import. It may import `subprocess`, `threading`, `collections`, and `claude_swap.spawn`.
- **All subprocess spawns in the menu-bar process go through `spawn.fork_lock`** (see `src/claude_swap/spawn.py`) — concurrent `fork()` from the multithreaded AppKit process livelocks macOS malloc. The single `nettop` spawn is no exception.
- **The graph's contents MUST stay out of `_snapshot_signature`** so redraws never trigger a full NSMenu rebuild (same discipline as the auto-switch countdown line).
- **macOS-only.** The whole menu bar is macOS-only; no behavior is added off macOS.
- **Tests:** run with `uv run pytest`. The pure data layer is unit-tested in CI. The AppKit drawing + `nettop` launch are thin glue **verified by running the built `.app`**, not in CI (this is the project's documented pattern — see CLAUDE.md "Optional-dependency / import-safety pattern").
- Verified `nettop` invocation (locked against real output on 2026-07-03):
  `nettop -P -x -L 0 -s 1 -J time,packets_in,packets_out`
  - Each sample begins with the header line `time,,packets_in,packets_out,`.
  - Each data row is `<time>,<name>.<pid>,<packets_in>,<packets_out>,` (proc name truncated to ~15 chars but the `.<pid>` suffix is preserved; names can contain dots, so the PID is the integer after the **last** dot). Counters are **cumulative** — take deltas between consecutive samples.

---

### Task 1: Data-layer pure functions (`net_packets.py`)

Pure, import-safe parsing/aggregation helpers. No subprocess, no AppKit. These are the numeric core that CI actually tests.

**Files:**
- Create: `src/claude_swap/net_packets.py`
- Test: `tests/test_net_packets.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces (relied on by Task 2 and Task 4):
  - `NETTOP_ARGS: list[str]` — the exact `nettop` command.
  - `parse_nettop_sample(block: str) -> dict[int, tuple[int, int]]` — `{pid: (packets_in, packets_out)}`; never raises.
  - `sum_targets(sample: dict[int, tuple[int, int]], pids: set[int]) -> int` — cumulative in+out across the target PIDs present.
  - `delta_rate(prev_total: int | None, cur_total: int) -> int` — `max(0, cur-prev)`; `0` on first sample / counter reset.
  - `normalize(values: list[int]) -> list[float]` — scale to `[0.0, 1.0]` vs window max; `[]` for empty input; all-`0.0` for an all-zero window.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_net_packets.py`:

```python
"""Unit tests for the pure packet-rate data layer (no subprocess, no AppKit)."""

from claude_swap import net_packets


# ---- parse_nettop_sample ------------------------------------------------

def test_parse_extracts_pid_and_counters():
    block = (
        "12:46:29.488417,kernel_task.0,1791010,3375865,\n"
        "12:46:29.488420,apsd.565,890,870,\n"
    )
    assert net_packets.parse_nettop_sample(block) == {
        0: (1791010, 3375865),
        565: (890, 870),
    }


def test_parse_skips_header_line():
    block = "time,,packets_in,packets_out,\n12:46:29.4,node.123,10,20,\n"
    assert net_packets.parse_nettop_sample(block) == {123: (10, 20)}


def test_parse_pid_is_integer_after_last_dot():
    # Process names can contain dots; the pid is the trailing .<digits>.
    block = "12:46:29.4,com.apple.WebKit.128,5,6,\n"
    assert net_packets.parse_nettop_sample(block) == {128: (5, 6)}


def test_parse_skips_junk_and_short_rows():
    block = (
        "garbage\n"
        ",,,\n"
        "12:46:29.4,noPidHere,1,2,\n"        # second field has no .pid
        "12:46:29.4,proc.notanumber,1,2,\n"  # pid not numeric
        "12:46:29.4,ok.7,3,4,\n"
    )
    assert net_packets.parse_nettop_sample(block) == {7: (3, 4)}


def test_parse_empty_block_is_empty_dict():
    assert net_packets.parse_nettop_sample("") == {}


# ---- sum_targets --------------------------------------------------------

def test_sum_targets_adds_in_and_out_for_present_pids():
    sample = {1: (10, 5), 2: (100, 50), 3: (1, 1)}
    assert net_packets.sum_targets(sample, {1, 3}) == 10 + 5 + 1 + 1


def test_sum_targets_ignores_absent_pids():
    sample = {1: (10, 5)}
    assert net_packets.sum_targets(sample, {2, 3}) == 0


def test_sum_targets_empty_pidset_is_zero():
    assert net_packets.sum_targets({1: (10, 5)}, set()) == 0


# ---- delta_rate ---------------------------------------------------------

def test_delta_rate_first_sample_is_zero():
    assert net_packets.delta_rate(None, 500) == 0


def test_delta_rate_normal_positive_delta():
    assert net_packets.delta_rate(500, 650) == 150


def test_delta_rate_counter_reset_clamps_to_zero():
    # A target PID exited / counters reset -> cumulative drops -> no negative spike.
    assert net_packets.delta_rate(650, 100) == 0


# ---- normalize ----------------------------------------------------------

def test_normalize_empty_is_empty():
    assert net_packets.normalize([]) == []


def test_normalize_all_zero_is_all_zero():
    assert net_packets.normalize([0, 0, 0]) == [0.0, 0.0, 0.0]


def test_normalize_scales_against_peak():
    assert net_packets.normalize([0, 50, 100]) == [0.0, 0.5, 1.0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_net_packets.py -q`
Expected: FAIL / collection error — `ModuleNotFoundError: No module named 'claude_swap.net_packets'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/claude_swap/net_packets.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_net_packets.py -q`
Expected: PASS (all 12 tests), no warnings.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/net_packets.py tests/test_net_packets.py
git commit -m "feat(net): pure packet-rate parsing/aggregation helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `PacketRateMonitor` + `nettop` stream driver

The stateful driver: one long-lived `nettop` stream on a daemon thread, a 30-slot rate ring, and a mutable target-PID set. The subprocess launch is injectable so the threading/deque contract is tested without spawning `nettop`.

**Files:**
- Modify: `src/claude_swap/net_packets.py`
- Test: `tests/test_net_packets.py`

**Interfaces:**
- Consumes: `parse_nettop_sample`, `sum_targets`, `delta_rate` (Task 1); `spawn.fork_lock`.
- Produces (relied on by Task 3 and Task 4):
  - `class PacketRateMonitor`
    - `__init__(self, *, window: int = 30, sampler=None)` — `sampler` is a zero-arg callable returning a *stream* object with a `.lines` iterable-of-str and a `.close()` method; defaults to a real `nettop` launcher.
    - `start(self) -> None` — launch the stream + daemon reader thread; idempotent.
    - `stop(self) -> None` — close the stream (terminate `nettop`); safe if never started.
    - `set_target_pids(self, pids: set[int]) -> None` — replace the summed-PID set.
    - `rates(self) -> list[int]` — snapshot copy of the ring (oldest→newest).
    - `current(self) -> int` — most recent rate, `0` if empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_net_packets.py`:

```python
# ---- PacketRateMonitor --------------------------------------------------

class _FakeStream:
    """Canned nettop stream: a finite list of lines + a close flag."""

    def __init__(self, lines):
        self.lines = iter(lines)
        self.closed = False

    def close(self):
        self.closed = True


def _sample(pid, pin, pout):
    return f"12:00:00.0,proc.{pid},{pin},{pout},\n"


def _run_monitor(lines, pids):
    """Start a monitor on a finite fake stream and wait for the reader to drain."""
    stream = _FakeStream(lines)
    mon = net_packets.PacketRateMonitor(window=30, sampler=lambda: stream)
    mon.set_target_pids(pids)
    mon.start()
    mon._thread.join(timeout=2.0)  # finite stream -> thread exits on drain
    return mon


def test_monitor_computes_per_second_deltas_for_target_pid():
    # Three samples for pid 42: cumulative 100 -> 250 -> 400.
    # First sample = baseline (rate 0), then deltas 150, 150.
    lines = [
        "time,,packets_in,packets_out,\n", _sample(42, 100, 0),
        "time,,packets_in,packets_out,\n", _sample(42, 250, 0),
        "time,,packets_in,packets_out,\n", _sample(42, 400, 0),
    ]
    mon = _run_monitor(lines, {42})
    assert mon.rates() == [0, 150, 150]
    assert mon.current() == 150


def test_monitor_sums_in_and_out_and_ignores_other_pids():
    lines = [
        "time,,packets_in,packets_out,\n", _sample(1, 10, 5), _sample(2, 999, 999),
        "time,,packets_in,packets_out,\n", _sample(1, 40, 5), _sample(2, 999, 999),
    ]
    mon = _run_monitor(lines, {1})  # pid 2 excluded
    # baseline 15, then cur 45 -> delta 30
    assert mon.rates() == [0, 30]


def test_monitor_empty_before_start():
    mon = net_packets.PacketRateMonitor()
    assert mon.rates() == []
    assert mon.current() == 0


def test_monitor_stop_closes_stream():
    stream = _FakeStream([])
    mon = net_packets.PacketRateMonitor(sampler=lambda: stream)
    mon.start()
    mon._thread.join(timeout=2.0)
    mon.stop()
    assert stream.closed is True


def test_monitor_start_is_idempotent():
    calls = []

    def sampler():
        calls.append(1)
        return _FakeStream([])

    mon = net_packets.PacketRateMonitor(sampler=sampler)
    mon.start()
    mon.start()
    mon._thread.join(timeout=2.0)
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_net_packets.py -k monitor -q`
Expected: FAIL — `AttributeError: module 'claude_swap.net_packets' has no attribute 'PacketRateMonitor'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `src/claude_swap/net_packets.py`:

> **Buffering note (discovered during implementation):** `nettop -L 0`
> **full-buffers** its stdout to a pipe, so a `subprocess.PIPE` yields nothing
> until ~8 KB accumulates or the process exits — the monitor's deque stays
> empty. Attaching a **pseudo-terminal** makes `nettop` line-buffer as it does
> in a real terminal, so each 1 Hz sample flushes promptly. The real stream
> therefore reads a pty master fd. (The injected-fake-sampler tests are
> unaffected — they never touch `_spawn_nettop`.)

> **Orphan-proofing (also discovered during implementation):** killing the app
> with a signal skips the `finally: stop()`, and a plain pty slave is not
> `nettop`'s controlling terminal — so closing the master did **not** kill
> `nettop`, leaving one orphan per launch. Fix: make `nettop` a session leader
> that owns the pty slave as its **controlling terminal** (`setsid()` +
> `TIOCSCTTY` in a `preexec_fn`). Then closing the master fd — which happens on
> *any* parent death (graceful quit, `SIGTERM`, `SIGKILL`, crash) — delivers
> `SIGHUP` and `nettop` exits within one sample. Verified live: orphan gone in
> ~0.25 s. Both syscalls are async-signal-safe and the spawn is already under
> `fork_lock`.

```python
class _NettopStream:
    """Adapts a live ``nettop`` subprocess to the sampler stream contract.

    ``nettop -L 0`` full-buffers stdout to a pipe; a pty makes it line-buffer.
    We read the pty master fd and split it into newline-terminated lines. The
    child owns the pty as its controlling terminal, so closing the master fd
    (on any parent death) delivers SIGHUP and it exits — orphan-proofing it.
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
                    yield (line + b"\n").decode("utf-8", "replace")
        except OSError:
            # pty master read raises EIO once the child exits — clean EOF.
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
    """Launch the one long-lived ``nettop`` stream (single fork, under the lock)."""
    import fcntl
    import pty  # lazy: keeps this module import-safe on non-Unix
    import termios

    def _preexec():  # async-signal-safe only
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # slave (fd 0) -> controlling tty

    master, slave = pty.openpty()
    with spawn.fork_lock:
        proc = subprocess.Popen(
            NETTOP_ARGS, stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
            preexec_fn=_preexec, close_fds=True,
        )
    os.close(slave)
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

    def __init__(self, *, window: int = 30, sampler=None):
        self._sampler = sampler or _spawn_nettop
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_net_packets.py -q`
Expected: PASS (all Task 1 + Task 2 tests), no warnings.

- [ ] **Step 5: Verify live that `nettop` streams and parses (manual, not CI)**

Run this one-off to confirm the real stream produces non-negative rates for this process:

```bash
uv run --extra menubar python - <<'PY'
import os, time
from claude_swap import net_packets
m = net_packets.PacketRateMonitor()
m.set_target_pids({os.getpid()})
m.start()
time.sleep(5)
print("rates:", m.rates())
m.stop()
PY
```
Expected: a list of ~4 non-negative integers (first is `0`). If it prints `[]`, `nettop` is unavailable or the format drifted — investigate before proceeding.

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/net_packets.py tests/test_net_packets.py
git commit -m "feat(net): PacketRateMonitor — one long-lived nettop stream to packets/sec

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Keep the monitor's target PIDs current from the refresh worker

The refresh worker already runs on the user's cadence off the Cocoa main thread. After it stores a snapshot, push the current Claude-process PID set (cswap + Claude Code sessions/IDEs) to the monitor if one is attached. Guarded so existing `_worker_impl` tests (which pass an `app` with no monitor) are unaffected.

**Files:**
- Modify: `src/claude_swap/menubar.py` (`_worker_impl`, around line 1094 — right after `app.snapshot = snap`)
- Test: `tests/test_menubar_refresh.py`

**Interfaces:**
- Consumes: `PacketRateMonitor.set_target_pids` (Task 2); `get_running_instances` and `os` (both already imported in `menubar.py`).
- Produces: reads `getattr(app, "_packet_monitor", None)` — set by Task 4's `run()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_menubar_refresh.py`:

Reuse the existing `_RefreshHarness` fake (top of the file) — it binds the real
`_worker_impl` machinery. Add:

```python
def test_worker_updates_packet_monitor_target_pids(monkeypatch):
    """_worker_impl pushes cswap + Claude Code PIDs to an attached monitor."""
    import os
    from claude_swap.process_detection import ClaudeSession, IdeInstance

    captured = {}

    class FakeMonitor:
        def set_target_pids(self, pids):
            captured["pids"] = set(pids)

    # Two running Claude processes: one CLI session (pid 4242), one IDE (pid 5353).
    monkeypatch.setattr(
        menubar, "get_running_instances",
        lambda *a, **k: (
            [ClaudeSession(pid=4242, session_id="s", cwd="/", started_at=0,
                           kind="interactive", entrypoint="cli")],
            [IdeInstance(port=1, pid=5353, ide_name="VS Code")],
        ),
    )
    # Keep the snapshot itself trivial and side-effect free.
    monkeypatch.setattr(
        menubar, "_snapshot",
        lambda switcher, full=True, force=False, max_fetch=None: {
            "accounts": [], "active_email": None, "active_usage": None,
            "instances": [],
        },
    )

    app = _RefreshHarness()
    app._packet_monitor = FakeMonitor()

    menubar._worker_impl(app, full=True)

    assert captured["pids"] == {os.getpid(), 4242, 5353}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_menubar_refresh.py::test_worker_updates_packet_monitor_target_pids -q`
Expected: FAIL — `KeyError: 'pids'` (the worker doesn't call `set_target_pids` yet).

- [ ] **Step 3: Write the minimal implementation**

In `src/claude_swap/menubar.py`, inside `_worker_impl`, immediately after `app._snapshot_at = time.time()` (the line following `app.snapshot = snap`), insert:

```python
        # Keep the packet-rate graph summing the right processes. One extra
        # local read of the session/IDE lockfiles per refresh cycle (cheap,
        # and independent of the usage snapshot). Guarded: no-op when no
        # monitor is attached (e.g. in unit tests / off macOS).
        monitor = getattr(app, "_packet_monitor", None)
        if monitor is not None:
            try:
                sessions, ides = get_running_instances()
                monitor.set_target_pids(
                    {os.getpid()}
                    | {s.pid for s in sessions}
                    | {i.pid for i in ides}
                )
            except Exception:
                app.switcher._logger.debug(
                    "packet monitor pid update failed", exc_info=True
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_menubar_refresh.py -q`
Expected: PASS (the new test plus all existing `_worker_impl` tests — the guard leaves monitor-less fakes untouched).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar_refresh.py
git commit -m "feat(menubar): feed Claude-process PIDs to the packet monitor each refresh

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Render layer — drawn graph in the dropdown + lifecycle wiring

The AppKit glue: a lazily-defined `NSView` subclass that draws the Stats-style chart, hosted in an `NSMenuItem` pinned to the top of the dropdown, plus monitor construction/start/stop in `run()` and a per-tick redraw. **This is thin AppKit/subprocess glue verified by running the built `.app`, not in CI** (AppKit isn't importable in the CI environment; the pure logic it depends on is already covered by Tasks 1–3). There is no pytest step here — the verification is the running graph.

**Files:**
- Modify: `src/claude_swap/menubar.py`:
  - top-of-file import (near line 25): add `net_packets`
  - `run()` (line ~1490): import AppKit symbols; define `_PacketGraphView`; construct/start the monitor; wrap `MenuBarApp().run()` in `try/finally: stop()`
  - `MenuBarApp.__init__` (line ~1588, **before** `self.rebuild_menu()`): create the view + host item, attach the monitor
  - `rebuild_menu` (end, line ~1917): insert the graph item + separator at the top
  - `_MenuObserver.tick_` (line ~1536): request a redraw each tick

**Interfaces:**
- Consumes: `net_packets.PacketRateMonitor`, `net_packets.normalize` (Tasks 1–2); `os`.
- Produces: sets `app._packet_monitor` (read by Task 3's `_worker_impl`) and `app._graph_view` / `app._graph_item`.

- [ ] **Step 1: Add the module import**

In `src/claude_swap/menubar.py`, change the existing line 25:

```python
from claude_swap import login_item, notify, oauth
```
to:
```python
from claude_swap import login_item, net_packets, notify, oauth
```

- [ ] **Step 2: Import AppKit symbols and define `_PacketGraphView` inside `run()`**

In `run()`, extend the existing lazy pyobjc import block (currently importing `NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer` from `Foundation`) by adding, right after it:

```python
    from AppKit import (  # pyobjc; only present with the [menubar] extra
        NSView,
        NSMenuItem,
        NSColor,
        NSBezierPath,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
    )
    from Foundation import NSMakeRect, NSMakePoint, NSString
    import objc
```

Then, alongside the other lazily-defined pyobjc classes in `run()` (e.g. next to `class _MenuObserver(NSObject)`), define:

```python
    _GRAPH_W, _GRAPH_H = 260.0, 64.0
    _GRAPH_PAD = 8.0

    class _PacketGraphView(NSView):
        """Stats-style drawn area/line chart of packets/sec (reads the monitor)."""

        def initWithMonitor_(self, monitor):
            self = objc.super(_PacketGraphView, self).initWithFrame_(
                NSMakeRect(0, 0, _GRAPH_W, _GRAPH_H)
            )
            if self is None:
                return None
            self._monitor = monitor
            return self

        def drawRect_(self, _rect):
            bounds = self.bounds()
            w = bounds.size.width
            h = bounds.size.height
            rates = self._monitor.rates()
            cur = self._monitor.current()

            # Label, top-left.
            label = f"Claude · {cur} pkt/s" if rates else "Claude · —"
            attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(11.0),
                NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
            }
            NSString.stringWithString_(label).drawAtPoint_withAttributes_(
                NSMakePoint(_GRAPH_PAD, h - 18.0), attrs
            )

            norm = net_packets.normalize(rates)
            if not norm:
                return

            # Plot area below the label.
            plot_bottom = _GRAPH_PAD
            plot_top = h - 22.0
            plot_h = max(1.0, plot_top - plot_bottom)
            left = _GRAPH_PAD
            right = w - _GRAPH_PAD
            span = max(1.0, right - left)
            n = len(norm)
            step = span / max(1, n - 1) if n > 1 else span

            def _pt(i, v):
                x = left + (i * step if n > 1 else span)
                y = plot_bottom + v * plot_h
                return NSMakePoint(x, y)

            accent = NSColor.systemBlueColor()

            # Filled area (accent at low alpha) under the line.
            area = NSBezierPath.bezierPath()
            area.moveToPoint_(NSMakePoint(left, plot_bottom))
            for i, v in enumerate(norm):
                area.lineToPoint_(_pt(i, v))
            area.lineToPoint_(NSMakePoint(left + (n - 1) * step if n > 1 else right,
                                          plot_bottom))
            area.closePath()
            accent.colorWithAlphaComponent_(0.18).setFill()
            area.fill()

            # Stroked line on top.
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(1.5)
            for i, v in enumerate(norm):
                pt = _pt(i, v)
                if i == 0:
                    line.moveToPoint_(pt)
                else:
                    line.lineToPoint_(pt)
            accent.setStroke()
            line.stroke()
```

> Drawing specifics (colors, padding, exact geometry) are cosmetic — adjust freely while verifying live in Step 6. The contract that matters: read `monitor.rates()`/`current()`, `normalize`, draw a label + area/line, and draw the "—" empty state when `rates()` is empty.

- [ ] **Step 3: Construct and start the monitor in `run()`**

In `run()`, after the single-instance lock is acquired and before `class MenuBarApp` is defined (or at least before `MenuBarApp()` is instantiated), add:

```python
    packet_monitor = net_packets.PacketRateMonitor()
    packet_monitor.set_target_pids({os.getpid()})  # cswap now; worker adds Claude PIDs
    packet_monitor.start()
```

Then change the final line of `run()` from:

```python
    MenuBarApp().run()
    return 0
```
to:
```python
    try:
        MenuBarApp().run()
    finally:
        # Belt-and-suspenders for the graceful path. The real guarantee is the
        # controlling-tty SIGHUP in net_packets: nettop dies whenever this
        # process closes the master fd, including on signals/crash where this
        # finally never runs.
        packet_monitor.stop()
    return 0
```

- [ ] **Step 4: Attach the monitor + build the graph item in `MenuBarApp.__init__`**

In `MenuBarApp.__init__`, **before** the existing `self.rebuild_menu()` call (line ~1588), add:

```python
            # Packet-rate graph: attach the monitor (started in run()) so the
            # refresh worker can retarget it, and build the host view + menu
            # item once. rebuild_menu re-inserts the item at the top each pass.
            self._packet_monitor = packet_monitor
            self._graph_view = _PacketGraphView.alloc().initWithMonitor_(packet_monitor)
            self._graph_item = NSMenuItem.alloc().init()
            self._graph_item.setView_(self._graph_view)
```

- [ ] **Step 5: Insert the graph at the top of the menu in `rebuild_menu`, and redraw each tick**

At the **very end** of `rebuild_menu` (after the `Quit` item is added, line ~1916), add:

```python
            # Pin the packet-rate graph to the very top of the dropdown. The
            # menu.clear() at the start of this method detached it, so re-insert
            # the persistent view item (its 30 s history survives rebuilds) plus
            # a separator. Inserted directly on the NSMenu (a view item has no
            # useful title key for rumps' dict). Kept OUT of _snapshot_signature
            # so redraws never force a rebuild.
            graph_item = getattr(self, "_graph_item", None)
            if graph_item is not None:
                nsmenu = self.menu._menu
                nsmenu.insertItem_atIndex_(graph_item, 0)
                nsmenu.insertItem_atIndex_(NSMenuItem.separatorItem(), 1)
```

In `_MenuObserver.tick_` (the common-modes ~0.25 s tick), add a redraw request at the end of the method (after `app._update_live_rows()`):

```python
            graph_view = getattr(app, "_graph_view", None)
            if graph_view is not None:
                graph_view.setNeedsDisplay_(True)
```

- [ ] **Step 6: Build the `.app` and verify the graph live**

The graph and `nettop` stream can't run under `pytest` (no AppKit in CI). Verify by building and running the bundle:

```bash
uv run pytest -q          # whole suite still green (data layer + worker test)
./packaging/make-app.sh   # rebuild the self-contained, ad-hoc-signed .app
```
Then swap it into place and launch (per the project's install flow):
```bash
rm -rf /Applications/claude-swap.app && mv dist/claude-swap.app /Applications/
open /Applications/claude-swap.app
```
Manually confirm:
- The dropdown shows a graph at the very top with a `Claude · <n> pkt/s` label and a separator beneath it.
- With the menu open, the value updates ~1×/sec and the line animates (trigger traffic: run a Claude Code command or let cswap's own refresh fire).
- With no Claude traffic the value trends toward `0`; before any sample it shows `Claude · —`.
- No freeze, no console errors; quitting the app leaves no orphaned `nettop`:
  ```bash
  pgrep -fl nettop   # expect: no cswap-spawned nettop after quit
  ```

- [ ] **Step 7: Commit**

```bash
git add src/claude_swap/menubar.py
git commit -m "feat(menubar): Stats-style packets/sec graph at top of the dropdown

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **Why one extra `get_running_instances()` call (Task 3) instead of threading PIDs through `_snapshot`?** Adding a key to the snapshot dict would ripple into `_snapshot`'s equality-based tests and `_snapshot_signature`. The extra call is a handful of local JSON reads once per refresh interval (15–60 s) — negligible, and zero blast radius on existing tests.
- **Why insert on the raw `NSMenu` (Task 4) instead of `self.menu[...] = item`?** rumps keys items by title; a view-only item has no meaningful title and rumps would mismanage/drop it. `insertItem_atIndex_` on `self.menu._menu` is the same escape hatch the code already uses (`self.menu._menu.setDelegate_`, `.highlightedItem()`).
- **Lifecycle safety:** `nettop` runs under a pty it owns as its controlling terminal, so closing the master fd (which happens on any cswap-process death — graceful quit, SIGTERM, SIGKILL, crash) delivers SIGHUP and `nettop` exits within one sample. The `try/finally: stop()` is belt-and-suspenders for the graceful path. Verified live: no orphaned `nettop` after killing the app.
- **Do not** put the graph behind `login_item.is_bundled()` — it should work in both the `.app` and the terminal/LaunchAgent install.
```
