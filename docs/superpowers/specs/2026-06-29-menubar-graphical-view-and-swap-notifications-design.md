# Menu-bar graphical status view + unified swap notifications

Date: 2026-06-29
Status: Approved

## Context

`cswap --menubar` already lists accounts (one line each) and posts a
`rumps.notification` on menu-initiated and auto-switch swaps. Two gaps:

1. The dropdown does not show the full per-account detail the CLI prints (5h/7d
   on separate lines with reset clock-time + countdown) and does not show the
   "Running instances" block at all.
2. Swaps commanded from the **CLI** (`cswap --switch` / `--switch-to`) post no
   notification, because `rumps.notification` only works inside the running app.

## Feature 1 — Flat indented status view

Mirror the CLI's indented tree directly in the dropdown. Each account renders as
a clickable name row (checkmark when active, switches on click — unchanged) plus
two disabled detail rows, followed by a "Running instances" section.

New **pure, unit-tested** helpers in `menubar.py`:

- `account_detail_lines(usage) -> list[str]` — `"5h:  5%   resets 18:59   in
  4h 46m"` style rows for each known window. Reuses `oauth.format_reset()` (the
  exact formatter the CLI uses) so clock-time + countdown are identical. Omits a
  window with unknown pct; omits the reset segment when `resets_at` is
  missing/unparseable.
- `group_running_instances(sessions, ides) -> list[tuple[str, str, int, bool]]`
  — `(label, folder, session_count, has_ide)`, grouping identical to
  `switcher.status` (`switcher.py:1590-1614`), reusing
  `printer.entrypoint_label / ide_short_name / abbreviate_path`.
- `format_instance_row(group) -> str` — `"VS Code   ~/Dev/TL-Starnav  (2
  sessions, IDE)"`.

Wiring:

- `_snapshot()` gains `"instances"` from `get_running_instances()`, wrapped so
  any failure degrades to `[]` (the menu must never break).
- `rebuild_menu()` emits, per account: the clickable name row then its detail
  rows; then a separator + "Running instances" header + instance rows (or
  nothing when there are none). Existing actions (rotate/best/next-available,
  add/remove, settings, refresh, quit) unchanged.

## Feature 2 — Unified swap notifier

One notification path for every swap, regardless of origin.

- New module `notify.py`: `notify(title, message)` posts a macOS notification
  via `osascript`. Works from any process (CLI, the LaunchAgent menu app,
  auto-switch). No-op off macOS; never raises; 5s timeout. Pure helper
  `_build_notification_script(title, message)` handles AppleScript escaping
  (backslash + double-quote) and is unit-tested.
- Single core hook: `_perform_switch()` (`switcher.py:2185`) is the one method
  every real swap funnels through (called from `switch()` and `switch_to()`).
  Add `self._announce_switch(account_num, email)` before both success returns.
  `_announce_switch` invokes `self._on_switch` if set; default `None` → no-op, so
  the test suite (run on macOS) never spawns `osascript`.
- `ClaudeAccountSwitcher` gains `self._on_switch = None` and
  `set_switch_notifier(callback)`.
- `cli.main()` (macOS only) calls `switcher.set_switch_notifier(...)` right after
  constructing the switcher, before dispatch. Because the same instance is handed
  to `--menubar`, this covers CLI swaps, menu clicks, and auto-switch.
- Remove the now-redundant `rumps.notification` swap messages in `menubar.py`
  (`_notify_switched`, `_notify_autoswitch`) to avoid double-notify. Non-swap
  alerts ("no fresh account", "auto-switch failed") stay as `rumps.notification`.

## Testing

TDD throughout. Pure helpers: `account_detail_lines` (with `format_reset`
mocked), instance grouping/formatting, `_build_notification_script`, `notify`
no-op off-darwin. Integration: a successful switch invokes the injected notifier
exactly once with the target account. No rumps/osascript in the test suite.

## Risk

Feature 2 edits the delicate `_perform_switch`, but the change is purely additive
(one call before existing returns) behind a default-`None` callback, so behavior
is unchanged unless a notifier is explicitly set.
