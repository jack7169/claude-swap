# macOS `.app` Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained, double-clickable, Spotlight-searchable `claude-swap.app` (menu bar + start-at-login) for the user's own Apple-Silicon machine.

**Architecture:** A new module-level entry (`app_main.py`) launches the existing `menubar.run()` menu bar directly. A new import-safe `login_item.py` wraps `SMAppService` for the native Login Item. `menubar.py` gains a "Start at login" menu item shown only when running from a bundle. A `packaging/` build recipe (`setup_app.py` + `make-app.sh` + `make-icon.py`) produces and ad-hoc-signs the bundle with py2app. All account logic stays in `ClaudeAccountSwitcher`.

**Tech Stack:** Python 3.12 (build only), py2app, rumps/pyobjc-Cocoa, pyobjc-framework-ServiceManagement, macOS `codesign`/`iconutil`/`sips`, `launchctl` (existing).

## Global Constraints

- **Local/personal build only** — ad-hoc signed (`codesign -s -`); no Developer ID, no notarization, no public distribution / Homebrew cask.
- **Build against Python 3.12** (Homebrew `python3.12`); 3.14 (the dev interpreter) is too new for py2app. Runtime users unaffected.
- **Apple-Silicon only** (arm64); no universal2.
- **Bundle identifier:** `com.claude-swap.menubar` (reuse the existing LaunchAgent label so only one identity exists).
- **`LSUIElement=True`** — menu-bar agent, no Dock icon.
- **`SMAppService` requires macOS 13+** and a bundled, signed app in a stable location (`/Applications`).
- **`login_item.py` must be import-safe** — lazy-import `ServiceManagement` inside a function, like the `rumps`/`keyring` pattern. Do NOT add `ServiceManagement` or `py2app` to `pyproject.toml` runtime deps; the build venv installs them and py2app bundles `ServiceManagement`.
- **No credential/account logic** in any new file — delegate to `ClaudeAccountSwitcher`.
- **The existing test suite must stay green** (`uv run pytest`, currently 1191 passed / 4 skipped). Build scripts are verified manually, not in CI.
- Worktree: `.claude/worktrees/auto-timer-start` on branch `worktree-auto-timer-start`. Run `uv run pytest` from the worktree root. Commit per task; `main` fast-forwards from this branch after review.

## File Structure

- Create `src/claude_swap/login_item.py` — `SMAppService` wrapper: `is_bundled()`, `status()`, `enable()`, `disable()`, `toggle()` + pure `_status_name()`. One responsibility: Login Item state.
- Create `src/claude_swap/app_main.py` — bundle entry point (`main()` → `menubar.run(ClaudeAccountSwitcher())`).
- Modify `src/claude_swap/menubar.py` — import `login_item`; add a "Start at login" checkable item (shown only when `login_item.is_bundled()`) + `on_toggle_login_item` callback, mirroring the auto-timer toggle.
- Create `tests/test_login_item.py` — unit tests (framework mocked).
- Create `tests/test_app_main.py` — entry-point test (`menubar.run` mocked).
- Create `packaging/make-icon.py` — render the `⇄` glyph → `packaging/claude-swap.icns`.
- Create `packaging/setup_app.py` — py2app recipe.
- Create `packaging/make-app.sh` — build + ad-hoc-sign driver.
- Modify `.gitignore` — ignore `build/`, `dist/`, `*.app`, `packaging/*.iconset`, `packaging/*.icns`.
- Modify `README.md` — "Build the macOS app" section.

---

### Task 1: `login_item.py` — bundle detection + status mapping (pure helpers)

**Files:**
- Create: `src/claude_swap/login_item.py`
- Test: `tests/test_login_item.py`

**Interfaces:**
- Produces: `is_bundled() -> bool`; `_status_name(raw: int) -> str` (maps 0→"not-registered", 1→"enabled", 2→"requires-approval", 3→"not-found", other→"unknown").

- [ ] **Step 1: Write the failing test**

```python
# tests/test_login_item.py
"""Tests for the SMAppService Login Item wrapper.

Never imports the ServiceManagement framework: the pure helpers are tested
directly and the service-backed functions are tested with _main_service mocked.
"""

from __future__ import annotations

from claude_swap import login_item


def test_status_name_maps_known_values():
    assert login_item._status_name(0) == "not-registered"
    assert login_item._status_name(1) == "enabled"
    assert login_item._status_name(2) == "requires-approval"
    assert login_item._status_name(3) == "not-found"


def test_status_name_unknown_value():
    assert login_item._status_name(99) == "unknown"


def test_is_bundled_true_when_frozen(monkeypatch):
    monkeypatch.setattr(login_item.sys, "frozen", "macosx_app", raising=False)
    assert login_item.is_bundled() is True


def test_is_bundled_true_for_app_bundle_path(monkeypatch):
    monkeypatch.delattr(login_item.sys, "frozen", raising=False)
    monkeypatch.setattr(
        login_item.sys, "executable",
        "/Applications/claude-swap.app/Contents/MacOS/python",
    )
    assert login_item.is_bundled() is True


def test_is_bundled_false_for_plain_interpreter(monkeypatch):
    monkeypatch.delattr(login_item.sys, "frozen", raising=False)
    monkeypatch.setattr(login_item.sys, "executable", "/opt/homebrew/bin/python3.12")
    assert login_item.is_bundled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_login_item.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_swap.login_item'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/claude_swap/login_item.py
"""macOS 'Start at login' via SMAppService (the modern Login Item API).

Used by the bundled ``.app`` (see packaging/) to register itself as a per-user
Login Item — the app-bundle equivalent of the LaunchAgent that ``cswap
--install-startup`` writes for the pip/terminal install. SMAppService requires a
real, signed app in a stable location, so these helpers are only meaningful when
running from a bundle (see :func:`is_bundled`); a ``python -m claude_swap`` run
uses the LaunchAgent path instead.

Import-safe: the ``ServiceManagement`` pyobjc framework is imported lazily inside
:func:`_main_service` (mirroring the rumps/keyring optional-import pattern), so
this module imports — and its pure helpers unit-test — without the framework.
"""

from __future__ import annotations

import sys

# SMAppServiceStatus raw values (ServiceManagement/SMAppService.h).
_STATUS_NAMES = {
    0: "not-registered",     # SMAppServiceStatusNotRegistered
    1: "enabled",            # SMAppServiceStatusEnabled
    2: "requires-approval",  # SMAppServiceStatusRequiresApproval
    3: "not-found",          # SMAppServiceStatusNotFound
}


def is_bundled() -> bool:
    """True when running from inside a py2app ``.app`` bundle.

    SMAppService can register only a real bundled app, so the "Start at login"
    control is shown only when this is True. py2app sets ``sys.frozen``; the
    ``.app/Contents/`` executable path is a fallback signal.
    """
    if getattr(sys, "frozen", False):
        return True
    return ".app/Contents/" in (sys.executable or "")


def _status_name(raw: int) -> str:
    """Map an SMAppServiceStatus raw value to a stable lowercase string (pure)."""
    return _STATUS_NAMES.get(raw, "unknown")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_login_item.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/login_item.py tests/test_login_item.py
git commit -m "feat(login-item): bundle detection + SMAppService status mapping"
```

---

### Task 2: `login_item.py` — status()/enable()/disable()/toggle() over SMAppService

**Files:**
- Modify: `src/claude_swap/login_item.py`
- Test: `tests/test_login_item.py`

**Interfaces:**
- Consumes: `_status_name`, `is_bundled` (Task 1).
- Produces: `_main_service() -> object | None` (lazy `SMAppService.mainAppService()`); `status() -> str` ("unavailable" when no service); `enable() -> tuple[bool, str | None]`; `disable() -> tuple[bool, str | None]`; `toggle() -> tuple[bool, str | None]` (disable if currently "enabled", else enable). None of these raise.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_login_item.py

class _FakeService:
    def __init__(self, status_val=1, register_ok=True, err="boom"):
        self._status = status_val
        self._register_ok = register_ok
        self._err = err
        self.calls = []

    def status(self):
        return self._status

    def registerAndReturnError_(self, _none):
        self.calls.append("register")
        return (self._register_ok, None if self._register_ok else self._err)

    def unregisterAndReturnError_(self, _none):
        self.calls.append("unregister")
        return (self._register_ok, None if self._register_ok else self._err)


def test_status_reads_service(monkeypatch):
    monkeypatch.setattr(login_item, "_main_service", lambda: _FakeService(status_val=1))
    assert login_item.status() == "enabled"


def test_status_unavailable_when_no_service(monkeypatch):
    monkeypatch.setattr(login_item, "_main_service", lambda: None)
    assert login_item.status() == "unavailable"


def test_enable_calls_register(monkeypatch):
    svc = _FakeService(register_ok=True)
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    assert login_item.enable() == (True, None)
    assert svc.calls == ["register"]


def test_enable_reports_error(monkeypatch):
    svc = _FakeService(register_ok=False, err="denied")
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    ok, err = login_item.enable()
    assert ok is False and "denied" in err


def test_disable_calls_unregister(monkeypatch):
    svc = _FakeService(register_ok=True)
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    assert login_item.disable() == (True, None)
    assert svc.calls == ["unregister"]


def test_toggle_enables_when_not_enabled(monkeypatch):
    svc = _FakeService(status_val=0)  # not-registered
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    login_item.toggle()
    assert svc.calls == ["register"]


def test_toggle_disables_when_enabled(monkeypatch):
    svc = _FakeService(status_val=1)  # enabled
    monkeypatch.setattr(login_item, "_main_service", lambda: svc)
    login_item.toggle()
    assert svc.calls == ["unregister"]


def test_functions_unavailable_without_service(monkeypatch):
    monkeypatch.setattr(login_item, "_main_service", lambda: None)
    assert login_item.enable()[0] is False
    assert login_item.disable()[0] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_login_item.py -v`
Expected: FAIL — `AttributeError: module 'claude_swap.login_item' has no attribute '_main_service'` (and `status`/`enable`/etc.).

- [ ] **Step 3: Write minimal implementation**

Append to `src/claude_swap/login_item.py`:

```python
def _main_service():
    """Return ``SMAppService.mainAppService()`` or ``None`` if unavailable.

    Lazy import keeps this module import-safe without pyobjc-ServiceManagement
    (absent in the pip/terminal install; bundled into the .app by py2app).
    """
    try:
        from ServiceManagement import SMAppService
    except Exception:
        return None
    try:
        return SMAppService.mainAppService()
    except Exception:
        return None


def status() -> str:
    """Current Login Item status as a stable string; 'unavailable' off-bundle."""
    svc = _main_service()
    if svc is None:
        return "unavailable"
    try:
        return _status_name(int(svc.status()))
    except Exception:
        return "unknown"


def enable() -> tuple[bool, str | None]:
    """Register the app as a Login Item. Returns (ok, error). Never raises."""
    svc = _main_service()
    if svc is None:
        return (False, "ServiceManagement unavailable")
    try:
        ok, err = svc.registerAndReturnError_(None)
        return (bool(ok), None if ok else str(err))
    except Exception as e:
        return (False, repr(e))


def disable() -> tuple[bool, str | None]:
    """Unregister the Login Item. Returns (ok, error). Never raises."""
    svc = _main_service()
    if svc is None:
        return (False, "ServiceManagement unavailable")
    try:
        ok, err = svc.unregisterAndReturnError_(None)
        return (bool(ok), None if ok else str(err))
    except Exception as e:
        return (False, repr(e))


def toggle() -> tuple[bool, str | None]:
    """Enable if not currently enabled, else disable. Returns (ok, error)."""
    return disable() if status() == "enabled" else enable()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_login_item.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/login_item.py tests/test_login_item.py
git commit -m "feat(login-item): status/enable/disable/toggle over SMAppService"
```

---

### Task 3: `app_main.py` — bundle entry point

**Files:**
- Create: `src/claude_swap/app_main.py`
- Test: `tests/test_app_main.py`

**Interfaces:**
- Consumes: `menubar.run(switcher)`, `ClaudeAccountSwitcher()` (existing).
- Produces: `main() -> int` — constructs a `ClaudeAccountSwitcher` and returns `menubar.run(switcher)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_main.py
"""Tests for the bundled-app entry point (menubar.run mocked; no rumps/AppKit)."""

from __future__ import annotations

from claude_swap import app_main, menubar


def test_main_runs_menubar_with_a_switcher(monkeypatch):
    captured = {}

    def fake_run(switcher):
        captured["switcher"] = switcher
        return 0

    monkeypatch.setattr(menubar, "run", fake_run)
    rc = app_main.main()
    assert rc == 0
    # A real ClaudeAccountSwitcher was constructed and handed to run().
    assert type(captured["switcher"]).__name__ == "ClaudeAccountSwitcher"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_swap.app_main'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/claude_swap/app_main.py
"""Entry point for the bundled ``.app`` — launches the menu bar directly.

py2app runs this module as the app's main script. Unlike ``cswap --menubar`` it
does not go through the CLI argument parser (a bundle receives launchd/Finder
argv, and the CLI's required action group would reject an empty argv). All
account logic still lives in ``ClaudeAccountSwitcher``.
"""

from __future__ import annotations


def main() -> int:
    from claude_swap import menubar
    from claude_swap.switcher import ClaudeAccountSwitcher

    return menubar.run(ClaudeAccountSwitcher())


if __name__ == "__main__":
    import sys

    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app_main.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/app_main.py tests/test_app_main.py
git commit -m "feat(app): bundle entry point launching the menu bar"
```

---

### Task 4: menu integration — "Start at login" item (shown only when bundled)

**Files:**
- Modify: `src/claude_swap/menubar.py` (import `login_item`; add item in `rebuild_menu` after the auto-timer item ~line 1752-1756; add `on_toggle_login_item` callback near `on_toggle_auto_timer_start` ~line 2090)
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `login_item.is_bundled()`, `login_item.status()`, `login_item.toggle()` (Tasks 1-2).
- Produces: `login_item_menu_state(status: str) -> int` (pure: 1 if `status == "enabled"` else 0) in `menubar.py`; a `on_toggle_login_item(self, _sender)` callback; a conditionally-added `rumps.MenuItem("Start at login")`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_menubar.py

def test_login_item_menu_state_checked_only_when_enabled():
    assert menubar.login_item_menu_state("enabled") == 1
    assert menubar.login_item_menu_state("not-registered") == 0
    assert menubar.login_item_menu_state("requires-approval") == 0
    assert menubar.login_item_menu_state("unavailable") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_menubar.py::test_login_item_menu_state_checked_only_when_enabled -v`
Expected: FAIL — `AttributeError: module 'claude_swap.menubar' has no attribute 'login_item_menu_state'`.

- [ ] **Step 3: Write minimal implementation**

Add the import near the other `claude_swap` imports at the top of `menubar.py`:

```python
from claude_swap import login_item
```

Add the pure helper near `toggle_auto_timer_start` (module level):

```python
def login_item_menu_state(status: str) -> int:
    """Checkmark state (1/0) for the 'Start at login' item given a status string."""
    return 1 if status == "enabled" else 0
```

In `rebuild_menu`, immediately after the auto-timer-start item is added (the block around line 1752 that builds `auto_timer_start_label(...)` with `callback=self.on_toggle_auto_timer_start`), add:

```python
            # Native Login Item toggle — only meaningful (and only shown) when
            # running from the .app bundle; the pip/terminal install uses the
            # `cswap --install-startup` LaunchAgent instead.
            if login_item.is_bundled():
                login_status = login_item.status()
                login_toggle = rumps.MenuItem(
                    "Start at login", callback=self.on_toggle_login_item
                )
                login_toggle.state = login_item_menu_state(login_status)
                self.menu["Start at login"] = login_toggle
```

Add the callback near `on_toggle_auto_timer_start` (~line 2090), mirroring its flip→notify→rebuild shape (no settings save — SMAppService is the source of truth):

```python
        def on_toggle_login_item(self, _sender):
            ok, err = login_item.toggle()
            if not ok:
                notify.notify("claude-swap", f"Start-at-login change failed: {err}")
            self.rebuild_menu()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_menubar.py::test_login_item_menu_state_checked_only_when_enabled -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite (integration guard)**

Run: `uv run pytest -q`
Expected: PASS (1192+ passed). Confirms the new import and menu code don't break existing menu-bar tests.

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): 'Start at login' item via SMAppService when bundled"
```

---

### Task 5: `packaging/make-icon.py` — generate the app icon from the glyph

**Files:**
- Create: `packaging/make-icon.py`

**Interfaces:**
- Produces: `packaging/claude-swap.icns` (build artifact, git-ignored). Referenced by `setup_app.py` (Task 6) if present.

Note: this is a build helper verified by running it, not by a unit test (it needs Cocoa + `iconutil`). It renders the `⇄` glyph with pyobjc (available in the build venv) and converts via `iconutil`.

- [ ] **Step 1: Create the script**

```python
# packaging/make-icon.py
"""Render the claude-swap glyph into an .icns (run inside the build venv).

Uses Cocoa (pyobjc, present in the build venv) to draw the app glyph onto a
1024x1024 image, writes the required iconset sizes, then calls `iconutil`.
Output: packaging/claude-swap.icns. Best-effort: if drawing fails, exit non-zero
and let the build proceed without a custom icon.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from AppKit import (
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPNGFileType,
    NSString,
)

GLYPH = "⇄"  # the menu-bar glyph
HERE = Path(__file__).resolve().parent
ICONSET = HERE / "claude-swap.iconset"
ICNS = HERE / "claude-swap.icns"
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _render_png(px: int) -> bytes:
    img = NSImage.alloc().initWithSize_((px, px))
    img.lockFocus()
    NSColor.clearColor().set()
    style = NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(2)  # NSTextAlignmentCenter is 2 on macOS
    attrs = {
        NSFontAttributeName: NSFont.systemFontOfSize_(px * 0.62),
        NSForegroundColorAttributeName: NSColor.labelColor(),
        NSParagraphStyleAttributeName: style,
    }
    s = NSString.stringWithString_(GLYPH)
    size = s.sizeWithAttributes_(attrs)
    rect = NSMakeRect(0, (px - size.height) / 2.0, px, size.height)
    s.drawInRect_withAttributes_(rect, attrs)
    img.unlockFocus()
    tiff = img.TIFFRepresentation()
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    return bytes(rep.representationUsingType_properties_(NSPNGFileType, {}))


def main() -> int:
    ICONSET.mkdir(parents=True, exist_ok=True)
    for px in SIZES:
        (ICONSET / f"icon_{px}x{px}.png").write_bytes(_render_png(px))
        if px <= 512:  # @2x variants
            (ICONSET / f"icon_{px}x{px}@2x.png").write_bytes(_render_png(px * 2))
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True
    )
    print(f"wrote {ICNS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: (manual, during build) Verify it produces an .icns**

This runs inside the build venv created by Task 7's `make-app.sh`. Verification command (after the build venv exists):
Run: `python packaging/make-icon.py && file packaging/claude-swap.icns`
Expected: prints `wrote .../claude-swap.icns` and `file` reports `Mac OS X icon`. If it errors, the build still proceeds (Task 6 makes the icon optional).

- [ ] **Step 3: Commit**

```bash
git add packaging/make-icon.py
git commit -m "build(app): icon generator (glyph -> .icns)"
```

---

### Task 6: `packaging/setup_app.py` — py2app recipe

**Files:**
- Create: `packaging/setup_app.py`

**Interfaces:**
- Consumes: `src/claude_swap/app_main.py` (Task 3) as the app entry; optional `packaging/claude-swap.icns` (Task 5).
- Produces: `dist/claude-swap.app` when run as `python setup_app.py py2app` inside the build venv.

Note: verified by the actual build (Task 7), not a unit test.

- [ ] **Step 1: Create the recipe**

```python
# packaging/setup_app.py
"""py2app build recipe for claude-swap.app (run inside the 3.12 build venv).

Invoked by packaging/make-app.sh:  python setup_app.py py2app
Reads the version from pyproject.toml so it can't drift. Bundles the lazily
imported ServiceManagement framework explicitly (py2app can't see lazy imports).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ICNS = Path(__file__).resolve().parent / "claude-swap.icns"

APP = [str(ROOT / "src" / "claude_swap" / "app_main.py")]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["claude_swap", "rumps"],
    # Lazily imported at runtime -> py2app won't auto-detect it; include explicitly.
    "includes": ["ServiceManagement"],
    "plist": {
        "CFBundleName": "claude-swap",
        "CFBundleDisplayName": "claude-swap",
        "CFBundleIdentifier": "com.claude-swap.menubar",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,  # menu-bar agent, no Dock icon
        "LSMinimumSystemVersion": "13.0",  # SMAppService
    },
}
if ICNS.exists():
    OPTIONS["iconfile"] = str(ICNS)

setup(
    app=APP,
    name="claude-swap",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
```

- [ ] **Step 2: Commit**

```bash
git add packaging/setup_app.py
git commit -m "build(app): py2app recipe (LSUIElement, SMAppService, versioned)"
```

---

### Task 7: `packaging/make-app.sh` — build + ad-hoc sign driver

**Files:**
- Create: `packaging/make-app.sh`

**Interfaces:**
- Consumes: `packaging/setup_app.py` (Task 6), `packaging/make-icon.py` (Task 5).
- Produces: an ad-hoc-signed `dist/claude-swap.app`.

Note: verified by running it on the user's machine (needs Python 3.12 + network + macOS toolchain), not in CI.

- [ ] **Step 1: Create the script**

```bash
# packaging/make-app.sh
#!/usr/bin/env bash
# Build a self-contained, ad-hoc-signed claude-swap.app for personal use.
# Builds against Python 3.12 (py2app is unreliable on 3.14). Not for public
# distribution — ad-hoc signing only (no Developer ID / notarization).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_VENV="$ROOT/build/appvenv"
PY312="${PYTHON312:-$(command -v python3.12 || echo /opt/homebrew/bin/python3.12)}"

echo "==> build venv (Python 3.12) at $BUILD_VENV"
"$PY312" -m venv "$BUILD_VENV"
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"
python -m pip install --upgrade pip wheel
python -m pip install "py2app" "pyobjc-framework-ServiceManagement"
python -m pip install "$ROOT[menubar]"

echo "==> generate icon (best-effort)"
python "$ROOT/packaging/make-icon.py" || echo "icon generation skipped"

echo "==> py2app build"
rm -rf "$ROOT/dist/claude-swap.app"
( cd "$ROOT" && python packaging/setup_app.py py2app )

APP="$ROOT/dist/claude-swap.app"
echo "==> ad-hoc code sign"
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict "$APP" && echo "signature OK"

deactivate
cat <<EOF

Built: $APP

Next steps (manual, one time):
  1. mv "$APP" /Applications/          # SMAppService needs a stable location
  2. Right-click > Open once           # clear the ad-hoc Gatekeeper prompt
  3. Menu bar > Start at login         # register the native Login Item
  4. If migrating from the pip install:  cswap --uninstall-startup
EOF
```

- [ ] **Step 2: Make it executable + build (manual verification)**

Run:
```bash
chmod +x packaging/make-app.sh
./packaging/make-app.sh
```
Expected: ends with "signature OK" and prints the built path; `dist/claude-swap.app` exists and `open dist/claude-swap.app` shows the menu-bar icon. (First run downloads py2app + pyobjc into the build venv — a few minutes.)

- [ ] **Step 3: Commit**

```bash
git add packaging/make-app.sh
git commit -m "build(app): make-app.sh build + ad-hoc-sign driver"
```

---

### Task 8: `.gitignore` + README docs

**Files:**
- Modify: `.gitignore`
- Modify: `README.md`

- [ ] **Step 1: Ignore build artifacts**

Append to `.gitignore`:

```gitignore
# macOS .app packaging (local/personal build)
/build/
/dist/
*.app
packaging/*.iconset/
packaging/claude-swap.icns
```

- [ ] **Step 2: Verify artifacts are ignored**

Run: `git status --porcelain packaging/ dist/ build/`
Expected: no `dist/`, `build/`, `*.icns`, or `*.iconset/` entries appear (only the committed `packaging/*.py` / `*.sh` sources would, and those are already committed).

- [ ] **Step 3: Document the build**

Add a "### Build the macOS app (personal)" subsection under the menu-bar section of `README.md`:

```markdown
### Build the macOS app (personal, local only)

A self-contained `claude-swap.app` (menu bar + start-at-login) can be built for
your own machine. It is **ad-hoc signed** (no Apple Developer account) and not
meant for distribution.

```bash
./packaging/make-app.sh                 # builds dist/claude-swap.app (uses Python 3.12)
mv dist/claude-swap.app /Applications/   # SMAppService needs a stable location
open /Applications/claude-swap.app       # first launch: right-click > Open to clear Gatekeeper
```

Then use **menu bar → Start at login** to register it as a native Login Item.
If you previously used the CLI login item, remove it: `cswap --uninstall-startup`.
Rebuild after pulling changes by re-running `./packaging/make-app.sh`.
```

- [ ] **Step 4: Commit**

```bash
git add .gitignore README.md
git commit -m "docs(app): ignore build artifacts; document the .app build"
```

---

## Self-Review

**1. Spec coverage:**
- py2app self-contained bundle → Tasks 6, 7. ✓
- Build against Python 3.12 → Task 7 (`PY312`). ✓
- LSUIElement / bundle id / version-from-pyproject → Task 6 plist. ✓
- Ad-hoc signing → Task 7 `codesign -s -`. ✓
- SMAppService Login Item + import-safe `login_item.py` (is_bundled/status/enable/disable) → Tasks 1, 2. ✓
- "Start at login" item shown only when bundled → Task 4. ✓
- Bundle entry avoiding CLI arg parsing → Task 3. ✓
- Icon generated from glyph, optional → Tasks 5, 6 (`if ICNS.exists()`). ✓
- `ServiceManagement` bundled despite lazy import → Task 6 `includes`. ✓
- No `ServiceManagement`/`py2app` in pyproject runtime deps → Task 7 installs into build venv only. ✓
- build/dist/*.app git-ignored → Task 8. ✓
- LaunchAgent path preserved for pip install; mutual exclusion documented → Task 4 (bundled-only), Task 7/8 (`--uninstall-startup` note). ✓
- Unit tests for pure helpers, build steps manual → Tasks 1-4 (tests), 5-7 (manual). ✓
- Out of scope (notarization, universal2, Win/Linux) → not present. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has complete content. ✓

**3. Type consistency:** `is_bundled`, `status`, `enable`, `disable`, `toggle`, `_main_service`, `_status_name`, `login_item_menu_state`, `on_toggle_login_item`, `app_main.main` are named consistently across tasks and match their consumers. Return types `tuple[bool, str | None]` consistent for enable/disable/toggle. ✓
