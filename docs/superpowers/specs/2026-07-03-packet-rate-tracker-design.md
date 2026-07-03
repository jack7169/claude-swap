# Menu-bar packet-rate tracker — design

_2026-07-03. A real-time stats graph at the top of the menu-bar dropdown showing
packets/sec from the machine's Claude processes to Claude servers._

## Goal

Add a live stats tracker to the top of the `cswap` menu-bar dropdown: a
Stats-style (exelban/Stats) **drawn area/line chart** of **packets per second**
with a 30-second history and a 1-second real-time update. The metric is the
combined packet rate of the local machine's Claude processes — the Claude Code
CLI/IDE instances plus `cswap` itself.

## Decisions (from the 2026-07-03 brainstorm)

| Question | Decision | Why |
|----------|----------|-----|
| What metric? | **Packets/sec** (in + out), summed across cswap + detected Claude Code PIDs. | User's choice. A real-time "how hard are we hitting Claude" pulse (relevant after the 429 storm). |
| Machine-wide vs cswap-only? | **Per-process, machine-wide**: cswap's PID + all Claude Code CLI/IDE PIDs. | User wants all Claude processes. Attributing by PID (not remote IP) sidesteps the shared-Cloudflare-IP problem — Claude's traffic can't be isolated by IP, but Claude Code processes talk ~only to Claude. |
| Data source? | **`nettop`** with the `packets_in`/`packets_out` columns. | Verified: `nettop -J packets_in,packets_out` reports per-process packet counts, **unprivileged** (no sudo/BPF, unlike tcpdump/Wireshark). It uses the same NetworkStatistics data a capture tool would, but per-process. |
| Avoid the fork-storm? | **One long-lived `nettop` stream**, read on a daemon thread. | A per-second `nettop` spawn is exactly the frequent-`fork()` pattern behind the freeze we fixed; a single streamed process avoids it. |
| Graph rendering? | **Custom `NSView` (`drawRect:`)** hosted as an `NSMenuItem` view — a drawn chart, not a Unicode sparkline. | User wants a real Stats-like graph/UI. `NSMenuItem.setView_` supports a custom view; pyobjc can subclass `NSView`. |
| Where? | **Dropdown**, pinned at the top. Status-bar item unchanged. | Stats' "widget" popover equivalent. An inline status-bar mini-graph is a separate, bigger effort (out of scope). |

## Architecture

Two cleanly separated layers: a **data layer** (pure, testable, no AppKit) and a
**render layer** (AppKit-bound, thin, verified live).

### Data layer — `src/claude_swap/net_packets.py` (new, import-safe)

Pure functions (unit-tested by feeding synthetic `nettop` output — no subprocess):

- `parse_nettop_sample(block: str) -> dict[int, tuple[int, int]]`
  Parse one CSV sample block from `nettop -P -x -L 0 -s 1 -J time,packets_in,packets_out`
  into `{pid: (packets_in, packets_out)}`. Each data row is
  `time,<procname>.<pid>,<packets_in>,<packets_out>,`; extract the trailing
  `.<pid>` and the two integer columns. Rows without a parseable `name.pid` or
  integer columns are skipped. Never raises.

- `sum_targets(sample: dict[int, tuple[int, int]], pids: set[int]) -> int`
  Cumulative in+out packets across the target PIDs present in the sample.

- `delta_rate(prev_total: int | None, cur_total: int) -> int`
  Per-second packets = `max(0, cur_total - prev_total)`; returns `0` when
  `prev_total is None` (first sample = baseline) or when the counter went
  backwards (a target PID exited / counters reset).

- `normalize(values: list[int]) -> list[float]`
  Scale a window of rates to `[0.0, 1.0]` against the window max for plotting
  (all-zero window → all `0.0`; single spike → that point `1.0`, rest scaled).

Stateful driver (thin glue; the numeric parts above are what's tested):

- `class PacketRateMonitor`
  - `__init__(self, *, window: int = 30, sampler=<default nettop launcher>)`
    Holds a `collections.deque(maxlen=window)` of per-second rates, the previous
    cumulative total, the current target-PID set, and a `threading.Lock`.
  - `start(self)` — launch the one long-lived `nettop` subprocess (via
    `spawn.fork_lock` for the single spawn) and a daemon reader thread. Idempotent.
  - `stop(self)` — terminate the subprocess (called on app quit).
  - `set_target_pids(self, pids: set[int])` — update which PIDs to sum (cswap +
    Claude Code). Called by the app when the detected set changes.
  - reader loop: accumulate stdout into per-sample blocks (delimited by the
    `time,,packets_in,packets_out,` header line that begins each sample), call
    `parse_nettop_sample` → `sum_targets` → `delta_rate`, push the rate under the
    lock. Swallows all errors; if `nettop` dies/absent the deque simply stops
    updating and the UI shows the empty state.
  - `rates(self) -> list[int]` — snapshot copy of the deque (for the view/tests).
  - `current(self) -> int` — most recent rate (0 if empty).

### Render layer — in `src/claude_swap/menubar.py`

- `class _PacketGraphView(NSView)` (defined lazily inside `run()`, alongside the
  other pyobjc classes, so the module stays import-safe without AppKit):
  - Holds a reference to the `PacketRateMonitor`.
  - `drawRect_(self, rect)`: read `monitor.rates()`, `normalize(...)`, and draw:
    a gradient-filled area under a stroked line across the width, a faint
    baseline, and the current value as text top-left
    (`"Claude · <n> pkt/s"`). Uses `NSColor.labelColor()` / a system accent so it
    adapts to light/dark like Stats. Fixed size ~260×64 pt.
  - Empty/zero state: draw the label with "—" and a flat baseline.
- Menu integration in `rebuild_menu`: create the view + its host `NSMenuItem`
  **once** (cached on the app), and insert that item at the **top** of the menu
  on every rebuild (the view object persists across rebuilds, so its data/history
  survive). A separator follows it, above the existing auto-swap header.
- Live redraw: the existing common-modes tick (`_MenuObserver.tick_`, already
  ~1 Hz-relevant) calls `graph_view.setNeedsDisplay_(True)` each tick so the
  chart animates every second. The graph reads live data in `drawRect:`; its
  contents are **not** part of `_snapshot_signature`, so it never triggers a
  full menu rebuild (same discipline as the countdown lines).
- Target-PID upkeep: the refresh worker already computes running Claude Code
  instances (`process_detection`); after it builds them, call
  `monitor.set_target_pids({os.getpid()} | {pid of each running instance})`.

### Lifecycle

- `run()` constructs the `PacketRateMonitor`, calls `start()`, wires it to the
  graph view, and registers `stop()` on quit.
- macOS-only (the whole menu bar is). No behavior off macOS.

## Data flow

```
nettop (1 long-lived proc, 1 sample/sec, all processes, packet cols)
  → daemon reader thread: parse_nettop_sample → sum_targets(target_pids) → delta_rate
  → deque(maxlen=30) of packets/sec         [under lock]
refresh worker → set_target_pids({cswap pid} | {claude code pids})
common-modes tick (1s) → graph_view.setNeedsDisplay_(True)
_PacketGraphView.drawRect_ → monitor.rates() → normalize → draw area/line + current value
```

## Error handling

- `nettop` missing, non-zero exit, or unparseable output → reader swallows it;
  deque stops updating; the graph shows the empty ("—") state. Never crashes the
  menu bar.
- A target PID exiting mid-window makes the cumulative total drop → `delta_rate`
  clamps to `0` (no negative spike).
- Single spawn only, through `spawn.fork_lock`; the reader is a daemon thread, so
  a stuck `nettop` can't wedge the UI (the app quitting terminates it).

## Testing

- Unit tests (no subprocess, no AppKit) for `parse_nettop_sample` (well-formed
  rows, junk rows, the header line, missing columns), `sum_targets` (subset of
  PIDs, absent PIDs), `delta_rate` (first sample, normal, counter-reset/negative),
  and `normalize` (all-zero, single spike, flat non-zero).
- `PacketRateMonitor` driven with an **injected fake sampler** (yields canned
  sample blocks) to test the deque/target-pid/threading contract without spawning
  `nettop`.
- The `nettop` launch and `_PacketGraphView.drawRect_` are thin AppKit/subprocess
  glue — verified by running the built `.app`, not in CI.

## Out of scope

- An inline mini-graph in the status-bar item itself (separate, larger effort).
- Per-remote-host attribution / isolating Claude from other HTTPS by IP
  (impossible with shared Cloudflare IPs; we attribute by process instead).
- Windows/Linux (no menu bar there).
- Historical persistence across restarts (the 30 s window is in-memory only).
