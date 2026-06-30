"""Curses-based interactive TUI for claude-swap.

Activated via ``cswap --tui``. Provides a single-level arrow-key menu over
the existing CLI commands, so users don't have to memorize flags.

The TUI never re-implements account logic — every action shells out to the
existing ``ClaudeAccountSwitcher`` methods. It exists purely as a navigation
layer.

Design constraints:
    * Zero new runtime dependencies (uses stdlib ``curses``).
    * Falls back gracefully when terminal is too small or curses is missing.
    * After each action, returns to the main menu (does not auto-exit).
"""

from __future__ import annotations

import curses
import sys
from typing import Callable

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import ClaudeAccountSwitcher


# Minimum terminal size we render in. Below this, we bail to plain CLI advice.
_MIN_ROWS = 12
_MIN_COLS = 60


def run(switcher: ClaudeAccountSwitcher) -> int:
    """Entry point for ``cswap --tui``. Returns process exit code."""
    try:
        return curses.wrapper(_main_loop, switcher)
    except _ExitRequested:
        return 0


# ---------------------------------------------------------------------------
# Main menu loop
# ---------------------------------------------------------------------------


class _ExitRequested(Exception):
    """Internal signal to break out of the curses loop."""


def _main_loop(stdscr: "curses._CursesWindow", switcher: ClaudeAccountSwitcher) -> int:
    rows, cols = stdscr.getmaxyx()
    if rows < _MIN_ROWS or cols < _MIN_COLS:
        curses.endwin()
        sys.stderr.write(
            f"Terminal too small for TUI ({rows}x{cols}, need at least "
            f"{_MIN_ROWS}x{_MIN_COLS}). Use the regular CLI flags instead.\n"
        )
        return 2

    curses.curs_set(0)  # hide cursor
    has_token_flow = hasattr(switcher, "add_account_from_token")

    while True:
        items: list[tuple[str, str]] = [
            ("Switch account", "switch"),
            ("Add account", "add"),
            ("Remove account", "remove"),
            ("Refresh credentials (current login, in-place)", "refresh"),
            ("List accounts (with usage)", "list"),
            ("Status", "status"),
            ("Quit", "quit"),
        ]
        choice = _select_from(
            stdscr,
            title="claude-swap",
            subtitle=_status_line(switcher),
            items=items,
        )
        if choice in (None, "quit"):
            return 0

        try:
            if choice == "switch":
                _do_switch(stdscr, switcher)
            elif choice == "add":
                _do_add(stdscr, switcher, has_token_flow)
            elif choice == "remove":
                _do_remove(stdscr, switcher)
            elif choice == "refresh":
                _do_refresh(stdscr, switcher)
            elif choice == "list":
                _shell_out(stdscr, lambda: switcher.list_accounts())
            elif choice == "status":
                _shell_out(stdscr, switcher.status)
        except ClaudeSwitchError as e:
            _show_message(stdscr, f"Error: {e}", is_error=True)
        except KeyboardInterrupt:
            _show_message(stdscr, "Operation cancelled.")


# ---------------------------------------------------------------------------
# Sub-flows
# ---------------------------------------------------------------------------


def _do_switch(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    items = _account_items(switcher)
    if not items:
        _show_message(stdscr, "No managed accounts. Add one first.")
        return
    items.append(("-- Cancel --", None))
    choice = _select_from(stdscr, "switch to", items=items)
    if choice is None:
        return
    _shell_out(stdscr, lambda: switcher.switch_to(choice))


def _do_add(stdscr, switcher: ClaudeAccountSwitcher, has_token_flow: bool) -> None:
    items: list[tuple[str, str]] = [
        ("From current Claude Code login   (cswap --add-account)", "login"),
    ]
    if has_token_flow:
        items.append(
            ("From a setup-token              (cswap --add-token)", "token")
        )
    items.append(("-- Cancel --", None))

    choice = _select_from(stdscr, "add account", items=items)
    if choice is None:
        return

    if choice == "login":
        _shell_out(stdscr, switcher.add_account)
        return

    # choice == "token"
    email = _prompt_text(stdscr, "Email for this token: ")
    if not email:
        return
    token = _prompt_text(stdscr, "Setup token: ", password=True)
    if not token:
        return
    _shell_out(
        stdscr,
        lambda: switcher.add_account_from_token(token=token, email=email, slot=None),
    )


def _do_remove(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    items = _account_items(switcher)
    if not items:
        _show_message(stdscr, "No managed accounts.")
        return
    items.append(("-- Cancel --", None))
    choice = _select_from(stdscr, "remove which account?", items=items)
    if choice is None:
        return
    if not _confirm(stdscr, f"Remove account {choice}? Type 'y' to confirm: "):
        return
    _shell_out(stdscr, lambda: switcher.remove_account(choice))


def _do_refresh(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    identity = switcher._get_current_account()
    if identity is None:
        _show_message(
            stdscr,
            "No active Claude Code login detected. Log in first, then retry.",
            is_error=True,
        )
        return
    email, _org = identity
    _shell_out(stdscr, lambda: switcher.add_account(slot=None))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_line(switcher: ClaudeAccountSwitcher) -> str:
    """Compact one-liner: 'Active: email [org] · N managed'. Pure-local, no network."""
    seq = switcher._get_sequence_data() or {}
    n = len(seq.get("accounts", {}))
    identity = switcher._get_current_account()
    if identity is None:
        active = "(no active login)"
    else:
        email, org = identity
        tag = "personal" if not org else org[:8]
        active = f"{email} [{tag}]"
    return f"Active: {active}  ·  {n} managed"


def _account_items(switcher: ClaudeAccountSwitcher) -> list[tuple[str, str]]:
    """Build (label, account_num) list for switch/remove sub-pages.

    No network — usage % is intentionally omitted to keep the picker snappy.
    """
    seq = switcher._get_sequence_data_migrated() or {}
    accounts = seq.get("accounts", {})
    if not accounts:
        return []
    active = str(seq.get("activeAccountNumber", ""))
    items: list[tuple[str, str]] = []
    for num in sorted(seq.get("sequence", []), key=int):
        acc = accounts.get(str(num), {})
        email = acc.get("email", "?")
        org = acc.get("organizationName", "") or "personal"
        marker = "  ★ active" if str(num) == active else ""
        label = f"{num}  {email:<32}  [{org}]{marker}"
        items.append((label, str(num)))
    return items


# ---------------------------------------------------------------------------
# Curses primitives — kept thin so we can mock them in tests
# ---------------------------------------------------------------------------


def _select_from(
    stdscr,
    title: str,
    items: list[tuple[str, str | None]],
    subtitle: str = "",
) -> str | None:
    """Vertical menu picker. Returns the selected value, or ``None`` on cancel.

    ``items`` is a list of ``(label, value)`` pairs. Items whose value is
    ``None`` are treated as cancel sentinels (selecting them returns ``None``).
    """
    idx = 0
    while True:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, title, subtitle, cols)

        # Menu items occupy rows 4 .. rows-3 (rows-2 is reserved for an
        # optional "... more" indicator, rows-1 for the footer). Scroll a
        # window of ``visible_count`` items so the selection stays in view.
        first_row = 4
        last_row = rows - 3  # inclusive; rows-2 may hold the indicator
        visible_count = max(1, last_row - first_row + 1)
        total = len(items)

        offset = _scroll_offset(idx, total, visible_count)
        window = items[offset : offset + visible_count]
        for j, (label, _val) in enumerate(window):
            i = offset + j
            y = first_row + j
            line = label[: cols - 6]
            if i == idx:
                stdscr.addstr(y, 2, "> ", curses.A_BOLD)
                stdscr.addstr(y, 4, line, curses.A_REVERSE)
            else:
                stdscr.addstr(y, 4, line)

        if total > visible_count:
            below = max(0, total - (offset + len(window)))
            above = offset
            indicator = f"... more ({above} above, {below} below)"
            stdscr.addstr(rows - 2, 2, indicator[: cols - 4], curses.A_DIM)

        footer = "[↑/↓] move  [Enter] select  [Esc/q] cancel"
        stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(items)
        elif key in (curses.KEY_ENTER, 10, 13):
            return items[idx][1]
        elif key in (27, ord("q")):  # Esc / q
            return None


def _scroll_offset(idx: int, total: int, visible_count: int) -> int:
    """Pick the first-visible index so ``idx`` stays inside the window.

    Keeps the selection on screen by sliding a ``visible_count``-tall window:
    the offset is clamped so the window never starts before 0 nor extends past
    the end of the list. When everything fits, the offset is always 0.
    """
    if total <= visible_count:
        return 0
    # Center-ish: keep the selection in view, biased so it isn't pinned to an
    # edge unless we're near the start/end of the list.
    offset = idx - visible_count // 2
    offset = max(0, min(offset, total - visible_count))
    # Defensive guarantee: selection must be within [offset, offset+count).
    if idx < offset:
        offset = idx
    elif idx >= offset + visible_count:
        offset = idx - visible_count + 1
    return offset


def _prompt_text(stdscr, label: str, password: bool = False) -> str | None:
    """Single-line text prompt. Returns string or ``None`` on Esc.

    When ``password`` is True, keystrokes are not echoed.
    """
    curses.curs_set(1)
    try:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, "claude-swap", "", cols)
        # The label itself may overflow a very narrow window; clamp it so the
        # initial draw never writes outside the window.
        stdscr.addstr(4, 2, label[: cols - 2])
        footer = "[Enter] confirm  [Esc] cancel"
        stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)

        # Where the typed text begins, and the last usable column. We keep one
        # cell of slack (``cols - 2``) so the blinking cursor can sit just past
        # the final character without leaving the window.
        cursor_x = min(2 + len(label), cols - 2)
        max_col = cols - 2
        field_width = max(1, max_col - cursor_x)

        buf: list[str] = []
        while True:
            _redraw_field(stdscr, cursor_x, field_width, buf, password)
            stdscr.refresh()
            key = stdscr.getch()
            if key == 27:  # Esc
                return None
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buf).strip()
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                continue
            if 32 <= key < 127:  # printable ASCII
                buf.append(chr(key))
    finally:
        curses.curs_set(0)


def _redraw_field(
    stdscr,
    cursor_x: int,
    field_width: int,
    buf: list[str],
    password: bool,
) -> None:
    """Render the editable text field as a width-clamped, tail-scrolled view.

    ``buf`` always holds the *full* typed string. We only ever draw the last
    ``field_width`` characters (the part that fits) so the echo and the visible
    cursor never exceed the window. Every curses call is wrapped in
    ``try/except curses.error`` so a draw that still slips out of bounds (e.g.
    a terminal resized to nothing) never crashes mid-entry — the keystroke is
    already captured in ``buf`` either way.
    """
    n = len(buf)
    # The visible cursor sits one cell past the last visible character, but
    # never beyond the field. The visible window shows the tail of ``buf``.
    visible_len = min(n, field_width)
    visible = "".join(buf[n - visible_len:])
    try:
        # Clear the field first so shrinking (backspace) leaves no stale chars.
        stdscr.addstr(4, cursor_x, " " * field_width)
    except curses.error:
        pass
    if not password and visible:
        try:
            stdscr.addstr(4, cursor_x, visible)
        except curses.error:
            pass
    try:
        stdscr.move(4, cursor_x + visible_len)
    except curses.error:
        pass


def _confirm(stdscr, prompt: str) -> bool:
    """Y/N prompt. Returns True only on 'y' / 'Y'."""
    answer = _prompt_text(stdscr, prompt)
    return bool(answer) and answer.lower() in ("y", "yes")


def _show_message(stdscr, msg: str, is_error: bool = False) -> None:
    """Display a single-line message and wait for any key."""
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    _draw_header(stdscr, "claude-swap", "", cols)
    attr = curses.A_BOLD if is_error else curses.A_NORMAL
    for i, line in enumerate(msg.split("\n")):
        if 4 + i >= rows - 2:
            break
        stdscr.addstr(4 + i, 2, line[: cols - 4], attr)
    footer = "[Press any key to continue]"
    stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
    stdscr.refresh()
    stdscr.getch()


def _draw_header(stdscr, title: str, subtitle: str, cols: int) -> None:
    stdscr.addstr(1, 2, title[: cols - 4], curses.A_BOLD)
    if subtitle:
        stdscr.addstr(2, 2, subtitle[: cols - 4], curses.A_DIM)


def _shell_out(stdscr, fn: Callable[[], None]) -> None:
    """Temporarily exit curses to run ``fn`` with normal stdout/stdin.

    Pauses afterwards so the user can read output, then restores the curses
    screen.
    """
    curses.def_prog_mode()  # save curses state
    curses.endwin()
    try:
        try:
            fn()
        except ClaudeSwitchError as e:
            print(f"Error: {e}")
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
        print()
        try:
            input("[Press Enter to return to TUI]")
        except (EOFError, KeyboardInterrupt):
            pass
    finally:
        curses.reset_prog_mode()  # restore curses state
        stdscr.refresh()
