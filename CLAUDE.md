# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`claude-swap` (CLI `cswap`/`claude-swap`) switches Claude Code between multiple accounts without logging out, on macOS, Linux/WSL, and Windows. macOS is the primary target and the only platform with a menu-bar app. Python 3.12+, packaged with hatchling, managed with `uv`.

## Commands

```bash
uv sync                         # create the dev environment
uv sync --extra menubar         # include the optional macOS menu-bar deps (rumps/pyobjc)
uv run cswap --help             # run the CLI from source
uv run pytest                   # full test suite
uv run pytest tests/test_cli.py # one module
uv run pytest tests/test_cli.py::TestCLI::test_version_flag   # one test
```

There is **no linter/formatter/type-checker configured** (no ruff/black/mypy) — don't invent lint commands. CI (`.github/workflows/ci.yml`) only runs `pytest` and `python -m build`; a separate job runs the real-Keychain contract tests (`tests/test_macos_keychain_contract.py tests/test_macos_keychain.py`) on macOS runners.

## Architecture (the parts that span files)

**`ClaudeAccountSwitcher` (switcher.py) is the core orchestrator.** Everything funnels through it; the CLI, TUI, and menu bar are thin shells that call its methods and must never re-implement account logic. It is large — read it by method, not top-to-bottom.

**Platform-conditional credential storage.** `Platform.detect()` picks the backend, and the switcher delegates all credential I/O to `CredentialStore` (credentials.py):
- **macOS** → the login Keychain via the `security` CLI (macos_keychain.py). A runtime capability cache (`_use_keychain`) flips to **file mode** on the first Keychain error and sticks for the process, so a single run never half-writes across backends.
- **Linux/WSL/Windows** → file-based credentials under the backup dir.
The legacy `keyring` backend is imported lazily and only for one-time migration/cleanup.

**The state model.** `sequence.json` holds `accounts`, `sequence`, and `activeAccountNumber`. The *active* account's credentials live in Claude Code's own store (`~/.claude.json` + Keychain/file); every other slot keeps a *backup copy*. A **switch** swaps a backup copy into the live store and updates `sequence.json`. Mutations that touch sequence/switch take a cross-process advisory `FileLock` (locking.py) — hold it around read-modify-write of `sequence.json`.

**Why restarts are usually unnecessary** (shapes a lot of behavior): on Linux/Windows Claude Code re-reads the credential file on change; on macOS the Keychain is cached ~30s. Switch logic and notifications are written around this.

**Surrounding modules:** session.py (`cswap run` — run an account in one terminal only via an isolated profile), process_detection.py (detect running Claude Code CLI/IDE instances), oauth.py (token + usage-window parsing), oauth_login.py (browser OAuth sign-in via a one-shot loopback callback server), transfer.py (`--export`/`--import` `.cswap` files), migrations.py + paths.py (auto-migrate legacy backup dirs, including cross-filesystem moves), notify.py (macOS notifications via `osascript`, works from any process), menubar.py (rumps app).

**Optional-dependency / import-safety pattern.** `rumps` is an optional extra (`[menubar]`); menubar.py keeps its pure helpers (settings, formatting, plist rendering) import-safe and imports `rumps` lazily inside the app glue, so they unit-test in CI without the extra. `keyring` is treated the same way. When touching the `--menubar` launch path, the menu-bar runs as a non-bundled `python -m claude_swap` LaunchAgent — use `notify.notify` (osascript) for banners, not `rumps.notification` (which needs an app bundle and raises otherwise).

**CLI dispatch quirk (cli.py).** Actions are a single `required` mutually-exclusive argparse group. The positional `cswap run …` subcommand is pre-dispatched *before* the main parser is built (it can't coexist with the required group). String-valued flags (`--export`, `--switch-to`, `--remove-account`, `--import`, `--add-token`) must be dispatched/validated with `is not None`, not truthiness — an empty-string value is a valid argparse value and truthiness checks silently mishandle it.

## Tests

Two autouse fixtures in `tests/conftest.py` are load-bearing — understand them before writing tests:
1. **`$HOME` is redirected** to a temp dir for every test, and `CLAUDE_CONFIG_DIR`/`XDG_DATA_HOME` are unset, so nothing reads/writes the developer's real backup dir.
2. **The macOS Keychain is replaced** with an in-memory `(service, account) → secret` fake (and `keyring` likewise), so unit tests never shell out to `security`.

Opt out only for real-Keychain integration tests: the `tmp_keychain` fixture (swaps the default keychain to a throwaway one) and the `no_keychain_fake` marker. These run against a real keychain on macOS CI and need the real `$HOME`. System interactions (`launchctl`, `security`, `osascript`, `subprocess`) are mocked in unit tests — follow the existing per-module patterns rather than spawning real processes.
