# macOS `.app` packaging (local/personal build) — design

_2026-07-02. Scope: a self-contained, double-clickable, Spotlight-searchable
`claude-swap.app` for the user's own Apple-Silicon machine. **Not** for public
distribution._

## Goal

Package the existing menu-bar app into a real macOS application so it can be
launched from Spotlight/Finder and start at login — without needing a terminal
or a `pip`/`uv` install to run it. All account logic stays in
`ClaudeAccountSwitcher`; the bundle is a thin shell exactly like the current
`--menubar` entry.

## Decisions (from the 2026-07-01 brainstorm)

| Question | Decision |
|----------|----------|
| Distribution | **Local/personal only** — ad-hoc signed (`codesign -s -`), no Developer ID, no notarization. Public distribution / Homebrew cask is explicitly out of scope (see the `homebrew-cask-signing-required` memo). |
| Bundler | **py2app** — the canonical bundler for `rumps`/`pyobjc` menu-bar apps; handles `LSUIElement` and the pyobjc frameworks natively. |
| Start-at-login | **`SMAppService`** native Login Item (macOS 13+), managed by the app itself — not the existing LaunchAgent. |
| Icon | Generate an `.icns` from the `⇄` glyph (the app has no icon asset today). |
| Build Python | **3.12** — most stable for py2app; 3.14 (the current dev interpreter) is too new for the bundlers. Runtime users are unaffected; this pins only the build. |
| Architecture | Apple-Silicon-only (arm64); no universal2 for a personal build. |

## Architecture

### 1. Build recipe (kept separate from the hatchling wheel build)

- **`packaging/setup_app.py`** — the py2app setup script. It is independent of
  `pyproject.toml`/hatchling (py2app needs a classic `setup()` call) and is never
  part of the pip/wheel build. It declares:
  - the app entry (see §2);
  - `plist`: `LSUIElement=True` (menu-bar agent, no Dock icon),
    `CFBundleIdentifier="com.claude-swap.menubar"` (reuse the existing label so
    only one identity exists), `CFBundleName="claude-swap"`,
    `CFBundleShortVersionString` read from `pyproject.toml` so it can't drift;
  - `packages=["claude_swap", "rumps"]` plus the pyobjc `includes` py2app needs;
  - `iconfile` = the generated `.icns`.
- **`packaging/make-app.sh`** — one-shot, idempotent build: create a throwaway
  Python 3.12 venv, install `.[menubar]` + `py2app`, generate the icon (§3), run
  `python packaging/setup_app.py py2app`, then ad-hoc sign
  (`codesign -s - --deep --force --timestamp=none dist/claude-swap.app`), verify
  (`codesign --verify --deep --strict`), and print the "move to /Applications"
  next step.
- `build/`, `dist/`, and `*.app` are git-ignored. The committed artifacts are
  `setup_app.py`, `make-app.sh`, and the icon source — not the built bundle.

### 2. App entry point

Add **`src/claude_swap/app_main.py`** with a single `main()` that launches the
menu bar directly (equivalent to `cswap --menubar`, but bypassing the CLI
argument parser — a bundled app receives argv from launchd/Finder, and the
CLI's required mutually-exclusive action group would reject an empty argv). The
py2app entry is this module. `python -m claude_swap --menubar` and the `cswap`
console script are unchanged.

### 3. Icon

`packaging/make-icon.sh`: render the `⇄` glyph to a 1024×1024 PNG, expand into an
`.iconset` (the standard sizes), and `iconutil -c icns`. If glyph rendering
proves fiddly, fall back to committing a static 1024 PNG in `packaging/` and
converting that. The `.icns` is a build product (git-ignored); the PNG/source is
committed.

### 4. Start-at-login — `SMAppService`

New import-safe module **`src/claude_swap/login_item.py`** (the `ServiceManagement`
pyobjc import is lazy, mirroring the `rumps`/`keyring` optional-import pattern so
its pure helpers unit-test without the framework):

- `is_bundled() -> bool` — true only when running from inside a `.app`
  (`SMAppService` can register **only** a real, signed app in a stable location;
  a `python -m claude_swap` run cannot use it). Detected from the executable path
  / bundle presence.
- `status() -> str` — wraps `SMAppService.mainApp.status` → `"enabled"` /
  `"not-registered"` / `"requires-approval"` / `"not-found"`.
- `enable()` / `disable()` — `register()` / `unregister()` on the main-app
  service; never raise (fold errors into a notify banner, like the rest of the
  menu-bar glue).

Menu integration: a **"Start at login"** checkable item (styled like the
auto-switch toggle) shown **only when `is_bundled()`**. In a non-bundled run the
item is hidden and the existing `cswap --install-startup` (LaunchAgent) remains
the login mechanism for terminal/pip installs.

The two mechanisms are mutually exclusive per install type (bundle → login item;
pip → LaunchAgent). To avoid a double-launch if a user has both, `app_main.main()`
checks for an already-running instance (the menu-bar already has a single-instance
guard) and the build docs tell the user to `--uninstall-startup` the old
LaunchAgent when moving to the `.app`.

## Testing

- Unit-test `login_item.py`'s pure helpers (`is_bundled` path logic, `status`
  string mapping) with `SMAppService` mocked — no framework needed in CI, matching
  the module's import-safety pattern.
- The build (`make-app.sh`) and the actual login-item registration are
  **manual/GUI steps** documented in the README, not CI: GitHub runners have no
  Aqua GUI session, no `/Applications`, and cannot verify Spotlight or a Login
  Item. CI keeps running the existing `pytest` + `python -m build` only.
- The full existing suite stays green; no change to `switcher`/`credentials`.

## Risks & mitigations

1. **py2app + pyobjc/rumps bundling quirks** — recipe iteration is expected.
   Mitigate by building against 3.12 and pinning known-good `py2app`.
2. **`SMAppService` location/signing requirement** — registration fails from a
   quarantined/translocated path. The build must ad-hoc sign, and the docs must
   say "move `claude-swap.app` to `/Applications` before enabling Start at login."
3. **Gatekeeper on first open** — an ad-hoc-signed app triggers a warning; the
   user opens it once via right-click → Open. Acceptable for personal use;
   documented.
4. **macOS 13+ only** for `SMAppService`. Acceptable (personal machine); the
   toggle degrades gracefully (hidden / notifies) on older systems.

## Out of scope

- Public distribution, Homebrew cask, notarization, Developer ID signing.
- Windows/Linux packaging; universal2 builds.
- Replacing the pip/uv install path — the wheel + console scripts stay as-is; the
  `.app` is an additional, parallel way to run the menu bar.
