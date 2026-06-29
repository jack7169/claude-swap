# TODO / Backlog

## Auto-switch settings: finer granularity + show config in the menu

Captured 2026-06-29. Feature request, not yet implemented.

### 1. Add a 30-second auto-switch cooldown option
- `AUTO_COOLDOWN_CHOICES` ([menubar.py:31](../src/claude_swap/menubar.py#L31)) is currently `(300, 600, 1800)` → add `30`.
- `cd_labels` in `_settings_menu` ([menubar.py:911](../src/claude_swap/menubar.py#L911)) → add `30: "30 seconds"`.
- Note: 30s is very aggressive vs. the current 5-min floor. Intended for fast testing / power users; the cooldown gate lives in the decision logic around [menubar.py:486-495](../src/claude_swap/menubar.py#L486-L495).

### 2. Add a 15-second auto-switch check-interval option
- `AUTO_CHECK_CHOICES` ([menubar.py:32](../src/claude_swap/menubar.py#L32)) is currently `(0, 60, 180, 300)` → add `15`.
- `ck_labels` in `_settings_menu` ([menubar.py:919](../src/claude_swap/menubar.py#L919)) → add `15: "Every 15 seconds"`.
- Caveat: the check cadence is `auto_switch_interval or refresh_interval` ([menubar.py:750](../src/claude_swap/menubar.py#L750)). A 15s check against a 60s refresh evaluates on possibly-stale usage until the next fetch. Decide whether a sub-refresh check should force a fresh usage fetch (it already conditionally does at [menubar.py:756-760](../src/claude_swap/menubar.py#L756-L760) — confirm 15s interacts correctly with `_FULL_REFRESH_EVERY` and the `_refreshing` guard).

### 3. Show the current auto-switch config in the main menu
The main menu currently shows per-account usage rows (built around [menubar.py:810-823](../src/claude_swap/menubar.py#L810-L823)); the auto-switch options live in the nested `_settings_menu` ([menubar.py:869](../src/claude_swap/menubar.py#L869)) and aren't visible at a glance.

Add a non-interactive summary section to the top-level menu displaying the active auto-switch options, reading from `self.settings`:
- enabled on/off (`auto_switch_enabled`)
- type / strategy (`auto_switch_strategy`)
- threshold / "timeout" (`auto_switch_threshold`)
- cooldown (`auto_switch_cooldown`)
- check interval (`auto_switch_interval`)

Keep it consistent with the existing per-account usage rows (disabled `callback=None` items, indented).
