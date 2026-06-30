# claude-swap

Multi-account switcher for Claude Code — switch between accounts without logging out. Works with the Claude Code CLI and the VS Code extension.

## Installation

```bash
uv tool install claude-swap        # or: pipx install claude-swap
```

This installs the CLI. For the macOS menu-bar app, see [Menu bar (macOS)](#menu-bar-macos) — it needs an optional extra that isn't on PyPI yet, so it installs from a clone of the repo.

Update with `cswap --upgrade` (or `uv tool upgrade claude-swap` / `pipx upgrade claude-swap`).

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap --add-account
```

### Add more accounts

Log in with another account, then:

```bash
cswap --add-account
```

### Switch accounts

Rotate to the next account:

```bash
cswap --switch
```

Or switch to a specific account:

```bash
cswap --switch-to 2
cswap --switch-to user@example.com
```

Or let claude-swap auto-pick by remaining quota — `cswap --switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --no-share          # don't share your ~/.claude customizations
```

Your `~/.claude` customizations (settings, keybindings, CLAUDE.md, skills, commands, agents) are shared into the session by default — use `--no-share` for a bare profile. Conversation history stays per-account.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap --add-account
```

This will update the stored credentials without creating a duplicate.

### Menu bar (macOS)

Run a menu bar app that shows each account's usage and lets you switch with a click:

The menu bar app needs the optional `menubar` extra (`rumps` + `pyobjc`). The
published PyPI release does not include this extra yet, so install it from a
clone of the repo:

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv tool install '.[menubar]'   # quote the spec so the shell doesn't glob [menubar]

cswap --menubar
```

Once a release that bundles the extra is published, the one-liner
`uv tool install 'claude-swap[menubar]'` (or `pipx install 'claude-swap[menubar]'`)
will work directly.

The dropdown lists every managed account with a one-line 5h / 7d / spend summary
and, indented beneath each, its 5h and 7d detail — percentage, reset clock-time,
and live countdown, matching `cswap --list`. Below the accounts, a **Running
instances** section shows where Claude Code is currently active (CLI / IDE, by
folder). Click an account to switch to it; the rotate / best / next-available
actions and the TUI's add / remove / refresh actions are all here too. A Settings
submenu controls what the menu-bar title shows and the refresh interval.

Every switch — from the menu, the CLI (`cswap --switch`), or auto-switch — posts
a single macOS notification reminding you to restart Claude Code (the swap takes
effect within ~30s).

**Add an account three ways** (under *Add account*): **From current login**
(capture the account Claude Code is signed into now), **From setup-token…**
(paste an `sk-ant-oat01-…` token), or **Sign in with browser…**, which opens
Claude's OAuth login in your browser and adds the account automatically once you
approve — no CLI or pre-obtained token needed. Browser sign-in is add-only: it
never changes your active account.

**Auto-switch.** Enable *Settings → Auto-switch accounts* to have the app
switch automatically when the active account crosses a usage threshold. When the
active account hits the threshold on its 5h or 7d window, it switches to the
account with the most headroom (skipping any that are themselves at the
threshold), then notifies you to restart Claude Code. Configure:

- **Threshold** (80% / 90% / 95%) — the usage level that triggers a switch.
- **Cooldown** (5m / 10m / 30m) — minimum time between automatic switches.
- **Check** — evaluate *with each display refresh*, or on an independent
  1m / 3m / 5m timer.

Defaults are 95% / 10m / with-display-refresh, and auto-switch is off until you
enable it.

**Strategies.** *Settings → Auto-switch strategy*:

- **Reactive** (default) — stays put until the active account crosses the
  threshold, then switches to the account with the most headroom.
- **Consume-first** — proactively keeps you on the account whose **weekly window
  resets soonest** (use-it-or-lose-it), switching as resets re-order the queue.
  It polls all accounts each tick (needed to rank them).

A small hysteresis dead band prevents switching back and forth when an account
hovers at the threshold.

**Start at login.** Install the menu-bar app as a per-user login item so it
launches automatically and keeps running (it loads into your GUI session so it
can reach the menu bar and your login Keychain):

```bash
cswap --install-startup     # install the login item and start it now
cswap --uninstall-startup   # remove it
```

The login item is the recommended way to keep the menu bar running. Prefer it
over leaving `cswap --menubar` running in a terminal — a terminal you later close
or suspend shouldn't own a GUI app.

### Menu bar troubleshooting (macOS)

- **`ModuleNotFoundError: No module named 'rumps'`** (or *"Menu bar mode requires
  the 'menubar' extra"*) — the menu bar ships as an optional extra that a plain
  install doesn't pull in, and it isn't on the published PyPI release yet.
  Reinstall it from a clone of the repo: `uv tool install '.[menubar]'` (see
  [Menu bar (macOS)](#menu-bar-macos) above). A bare
  `uv tool install 'claude-swap[menubar]'` against PyPI will warn that there is
  no such extra and silently install without the menu bar.
- **The icon is gone after you quit it (or it stopped).** A clean *Quit* stays
  quit by design. Start it again with `cswap --install-startup` (idempotent — it
  reloads and launches the app) or
  `launchctl kickstart -k gui/$(id -u)/com.claude-swap.menubar`.
- **A frozen, unresponsive icon.** Suspending `cswap --menubar` (Ctrl+Z in its
  terminal) used to leave a frozen "phantom" icon; that's now prevented (the app
  ignores Ctrl+Z), and the login item avoids the situation entirely.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap --list                    # Show all accounts with 5h/7d usage and reset times
cswap --status                  # Show current account
cswap --add-account --slot 3    # Add account to a specific slot (prompts before overwrite)
cswap --remove-account 2        # Remove an account
cswap --tui                     # Launch the interactive arrow-key menu
cswap --upgrade                 # Upgrade claude-swap to the latest version
cswap --purge                   # Remove all claude-swap data
```

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap --switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps credentials when you switch accounts
- Account credentials stored securely using platform-appropriate methods

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location. Data from older installs under `~/.claude-swap-backup/` is migrated automatically on first run.

## Advanced

### Backup and migration

Move account data between machines or back it up:

```bash
cswap --export backup.cswap                  # All accounts to a file
cswap --export backup.cswap --account 2      # One account
cswap --export backup.cswap --full           # Include full local ~/.claude.json (same-PC backup)
cswap --import backup.cswap                  # Skips accounts that already exist
cswap --import backup.cswap --force          # Overwrite existing
```

The export file is plaintext JSON. If you need encryption, pipe through your tool of choice (e.g. `cswap --export - | gpg -c > backup.gpg`).

### JSON output for scripting

Add `--json` to `--list`, `--status`, `--switch`, or `--switch-to` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap --list --json                 # all accounts with usage/quota
cswap --status --json               # current active account
cswap --switch --strategy best --json   # switch, then report the result
cswap --switch-to 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

</details>

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap --add-token sk-ant-oat01-...           # OAuth setup-token
cswap --add-token sk-ant-api03-...           # managed API key
cswap --add-token sk-ant-oat01-... --slot 3
cswap --add-token - --slot 3                 # read token from stdin
cswap --add-token --email user@example.com   # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `--switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
cswap --purge
```

Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
