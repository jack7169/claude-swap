"""Rendering-safety tests for the TUI (Phases 9.1 & 9.2).

These tests focus on the curses *drawing* layer rather than dispatch logic:

  * ``_prompt_text`` must clamp the cursor/echo to the window width so that a
    long secret (setup-token / API key, 100+ chars) typed on a narrow terminal
    never writes outside the window (which would raise ``curses.error`` and
    crash ``cswap`` mid-entry). The captured buffer must still be the full
    typed string.
  * ``_select_from`` must scroll its menu so the selected item is always
    visible even when there are more items than rows, and show a truncation
    indicator.
  * ``_show_message`` must width-clamp its footer like the other footers.

Unlike ``test_tui.py``, the stub window here *enforces* curses-style bounds
checking (raising ``curses.error`` on out-of-window writes) so the tests
actually exercise the clamping rather than letting a bare MagicMock swallow
out-of-bounds coordinates.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_swap import tui

curses = tui.curses


# --------------------------------------------------------------------------- #
# A recording stub window that behaves like real curses w.r.t. bounds.         #
# --------------------------------------------------------------------------- #


class FakeScreen:
    """Minimal curses-window stand-in that records draws and enforces bounds.

    Real curses raises ``curses.error`` when ``addstr``/``move`` reference a
    coordinate outside the window. A plain ``MagicMock`` silently accepts any
    coordinate, which would hide exactly the bug Phase 9.1 fixes — so this stub
    raises the same way real curses does.
    """

    def __init__(self, rows: int, cols: int, keys=None):
        self._rows = rows
        self._cols = cols
        # Calls recorded as (method, y, x, text-or-None).
        self.calls: list[tuple] = []
        self.addstr_calls: list[tuple[int, int, str]] = []
        self.move_calls: list[tuple[int, int]] = []
        self._keys = list(keys or [])

    # --- geometry --- #
    def getmaxyx(self):
        return (self._rows, self._cols)

    # --- no-op lifecycle --- #
    def erase(self):
        self.calls.append(("erase",))

    def refresh(self):
        self.calls.append(("refresh",))

    # --- input --- #
    def getch(self):
        if not self._keys:
            raise AssertionError("FakeScreen ran out of scripted keys")
        return self._keys.pop(0)

    # --- drawing (bounds-checked) --- #
    def _check(self, y, x):
        if y < 0 or y >= self._rows or x < 0 or x >= self._cols:
            raise curses.error(f"addwin/move out of bounds: y={y} x={x}")

    def move(self, y, x):
        self._check(y, x)
        self.move_calls.append((y, x))
        self.calls.append(("move", y, x))

    def addstr(self, y, x, text, attr=0):
        # A string written starting at column x occupies columns
        # [x, x + len(text)); the last cell touched is x + len(text) - 1.
        self._check(y, x)
        if text:
            self._check(y, x + len(text) - 1)
        self.addstr_calls.append((y, x, text))
        self.calls.append(("addstr", y, x, text))

    # everything else the code might call is a harmless no-op
    def __getattr__(self, name):  # pragma: no cover - defensive
        def _noop(*args, **kwargs):
            return None

        return _noop


def _max_col_touched(screen: FakeScreen) -> int:
    """Highest column index referenced by any addstr/move (-1 if none)."""
    cols = [-1]
    for y, x, text in screen.addstr_calls:
        cols.append(x)
        if text:
            cols.append(x + len(text) - 1)
    for y, x in screen.move_calls:
        cols.append(x)
    return max(cols)


# --------------------------------------------------------------------------- #
# 9.1 — _prompt_text width clamping                                            #
# --------------------------------------------------------------------------- #


class TestPromptTextClamping:
    def test_long_input_does_not_raise_and_captures_full_buffer(self):
        # A 200-char token typed on a 60-col terminal must not crash, and the
        # returned value must be the full string (no truncation from scrolling).
        secret = "sk-ant-" + "A" * 193  # 200 chars total
        assert len(secret) == 200
        keys = [ord(c) for c in secret] + [10]  # type secret, then Enter
        screen = FakeScreen(rows=24, cols=60, keys=keys)

        with patch("claude_swap.tui.curses.curs_set"):
            result = tui._prompt_text(screen, "Setup token: ")

        assert result == secret

    def test_long_password_input_does_not_raise(self):
        # password=True must also stay in bounds (and still capture fully).
        secret = "x" * 200
        keys = [ord(c) for c in secret] + [10]
        screen = FakeScreen(rows=24, cols=60, keys=keys)

        with patch("claude_swap.tui.curses.curs_set"):
            result = tui._prompt_text(screen, "Setup token: ", password=True)

        assert result == secret

    def test_every_column_stays_within_window_bounds(self):
        secret = "B" * 150
        keys = [ord(c) for c in secret] + [10]
        cols = 60
        screen = FakeScreen(rows=24, cols=cols, keys=keys)

        with patch("claude_swap.tui.curses.curs_set"):
            tui._prompt_text(screen, "Email for this token: ")

        assert _max_col_touched(screen) < cols

    def test_backspace_then_continue_stays_in_bounds_and_captures(self):
        # Type past the edge, backspace a few, type more — must stay in bounds
        # and the captured buffer must reflect the real edits.
        keys = [ord(c) for c in "C" * 120]
        keys += [curses.KEY_BACKSPACE, curses.KEY_BACKSPACE]  # drop 2
        keys += [ord("Z")]
        keys += [10]
        cols = 60
        screen = FakeScreen(rows=24, cols=cols, keys=keys)

        with patch("claude_swap.tui.curses.curs_set"):
            result = tui._prompt_text(screen, "x: ")

        assert _max_col_touched(screen) < cols
        assert result == "C" * 118 + "Z"

    def test_short_input_still_works(self):
        keys = [ord(c) for c in "hi@x.com"] + [10]
        screen = FakeScreen(rows=24, cols=60, keys=keys)
        with patch("claude_swap.tui.curses.curs_set"):
            result = tui._prompt_text(screen, "Email: ")
        assert result == "hi@x.com"

    def test_esc_returns_none(self):
        screen = FakeScreen(rows=24, cols=60, keys=[27])
        with patch("claude_swap.tui.curses.curs_set"):
            result = tui._prompt_text(screen, "Email: ")
        assert result is None


# --------------------------------------------------------------------------- #
# 9.2 — _select_from scrolling                                                 #
# --------------------------------------------------------------------------- #


class TestSelectFromScrolling:
    def _items(self, n: int) -> list[tuple[str, str]]:
        return [(f"item-{i}", str(i)) for i in range(n)]

    def test_selection_below_fold_is_rendered(self):
        # rows=10 only fits a few items; the selected item near the bottom of a
        # long list must be among the rendered labels.
        items = self._items(40)
        # Press DOWN 30 times then ENTER -> selects index 30.
        keys = [curses.KEY_DOWN] * 30 + [10]
        screen = FakeScreen(rows=10, cols=40, keys=keys)

        result = tui._select_from(screen, "title", items=items)
        assert result == "30"

        rendered = {text for _, _, text in screen.addstr_calls}
        assert any("item-30" in t for t in rendered), rendered

    def test_truncation_indicator_shown_when_more_items_than_fit(self):
        items = self._items(40)
        screen = FakeScreen(rows=10, cols=40, keys=[10])  # immediate ENTER
        tui._select_from(screen, "title", items=items)

        rendered = " ".join(text for _, _, text in screen.addstr_calls)
        assert "more" in rendered.lower()

    def test_no_indicator_when_all_items_fit(self):
        items = self._items(3)
        screen = FakeScreen(rows=24, cols=40, keys=[10])
        tui._select_from(screen, "title", items=items)
        rendered = " ".join(text for _, _, text in screen.addstr_calls)
        assert "more" not in rendered.lower()

    def test_all_menu_draws_stay_within_window_bounds(self):
        items = self._items(50)
        cols = 40
        # navigate to the very bottom then select
        keys = [curses.KEY_DOWN] * 49 + [10]
        screen = FakeScreen(rows=10, cols=cols, keys=keys)
        tui._select_from(screen, "title", items=items)
        assert _max_col_touched(screen) < cols

    def test_first_item_visible_at_top(self):
        # With selection at index 0, the first item must be rendered.
        items = self._items(40)
        screen = FakeScreen(rows=10, cols=40, keys=[10])
        result = tui._select_from(screen, "title", items=items)
        assert result == "0"
        rendered = {text for _, _, text in screen.addstr_calls}
        assert any("item-0" in t for t in rendered)


# --------------------------------------------------------------------------- #
# 9.2 — _show_message footer clamping                                          #
# --------------------------------------------------------------------------- #


class TestShowMessageFooterClamp:
    def test_footer_clamped_on_narrow_terminal(self):
        # The footer string is longer than a very narrow window; it must be
        # clamped so it never writes outside the window.
        cols = 24
        screen = FakeScreen(rows=10, cols=cols, keys=[ord("q")])
        # Should not raise (bounds-checked stub) and stay within width.
        tui._show_message(screen, "hello")
        assert _max_col_touched(screen) < cols

    def test_footer_present_but_within_bounds(self):
        cols = 30
        screen = FakeScreen(rows=12, cols=cols, keys=[ord("q")])
        tui._show_message(screen, "done")
        # The footer is the last addstr at row rows-1; verify it was clamped.
        footer_calls = [c for c in screen.addstr_calls if c[0] == 12 - 1]
        assert footer_calls, "expected a footer addstr on the last row"
        for _y, x, text in footer_calls:
            assert x + len(text) <= cols
