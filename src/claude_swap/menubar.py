"""macOS menu bar app for claude-swap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` — it never re-implements
account logic. Built on ``rumps`` (an optional extra, macOS only). The pure
helpers below (settings, formatting, plist rendering) are import-safe without
rumps so they can be unit-tested in CI; ``rumps`` is imported lazily inside
the app glue.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import notify, oauth
from claude_swap.exceptions import ClaudeSwitchError, CredentialReadError
from claude_swap.printer import abbreviate_path, entrypoint_label, ide_short_name
from claude_swap.process_detection import get_running_instances

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95)
AUTO_COOLDOWN_CHOICES: tuple[int, ...] = (300, 600, 1800)
AUTO_CHECK_CHOICES: tuple[int, ...] = (0, 60, 180, 300)  # 0 == with display refresh
AUTO_STRATEGY_CHOICES: tuple[str, ...] = ("reactive", "consume-first")
AUTO_HYSTERESIS = 5.0  # dead band (percent) that prevents auto-switch thrash
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
_FULL_REFRESH_EVERY = 300  # seconds between full (all-account) usage refreshes


@dataclass
class MenuBarSettings:
    """User-configurable menu bar behavior, persisted as JSON."""

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    refresh_interval: int = 60
    auto_switch_enabled: bool = False
    auto_switch_threshold: int = 95
    auto_switch_cooldown: int = 600
    auto_switch_interval: int = 0  # 0 == evaluate with each display refresh
    auto_switch_strategy: str = "reactive"  # one of AUTO_STRATEGY_CHOICES

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                kwargs[f.name] = raw[f.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


@dataclass
class MenuBarState:
    """Cooldown/notification timestamps for the auto-switcher, persisted as JSON.

    Separate from MenuBarSettings: settings are user choices, state is runtime
    bookkeeping. Persisting across restarts means a relaunch respects the
    cooldown instead of swapping immediately.
    """

    last_switch_at: float = 0.0
    last_noswap_notify_at: float = 0.0
    blocked: list[str] = field(default_factory=list)  # 5h/limit-blocked account nums

    @classmethod
    def load(cls, path: Path) -> "MenuBarState":
        """Load state; defaults on missing/corrupt. Int timestamps coerce to float."""
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            default = getattr(defaults, f.name)
            val = raw.get(f.name)
            if isinstance(default, float):
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    kwargs[f.name] = float(val)
            elif isinstance(default, list):
                if isinstance(val, list) and all(isinstance(x, str) for x in val):
                    kwargs[f.name] = list(val)
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write state as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Mirrors ``oauth.account_headroom`` (which returns ``100 - max(pct)``) but
    surfaces the utilization itself for display. Spend is excluded — it isn't
    a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time. Deriving
    it from the absolute ``resets_at`` keeps it correct between/without refetches.
    Returns ``None`` when there's no ``resets_at`` or it has already passed (the
    cached value is stale — omit rather than show a wrong/negative countdown).
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def usage_summary(usage: dict | str | None, now: float | None = None) -> str:
    """One-line usage summary for an account row (reset countdown computed live)."""
    if isinstance(usage, str):
        return usage
    if usage is None:
        return "usage unavailable"
    if now is None:
        now = time.time()
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            seg = f"{label} {window['pct']:.0f}%"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"  # time until this window resets
            parts.append(seg)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def auto_switch_menu_label(enabled: bool) -> str:
    """Label for the auto-switch toggle, with explicit ON/OFF text.

    macOS menu checkmarks are easy to miss (an unmarked item reads as a plain
    line), so the on/off state is spelled out in the label rather than shown
    only as a check.
    """
    return f"Auto-switch accounts: {'ON' if enabled else 'OFF'}"


_STRATEGY_SHORT_LABELS = {"reactive": "Reactive", "consume-first": "Consume-first"}


def auto_switch_strategy_label(strategy: str) -> str:
    """Short, human-facing name for an auto-switch strategy (unknown -> as-is)."""
    return _STRATEGY_SHORT_LABELS.get(strategy, strategy)


def _fmt_mmss(seconds: float) -> str:
    """Whole seconds as ``M:SS`` (negative clamps to ``0:00``)."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def auto_switch_countdown_text(seconds_to_next: float, cadence: int) -> str:
    """The live 'next check' line: countdown to the next eval plus its cadence."""
    return f"Next check: {_fmt_mmss(seconds_to_next)} (every {cadence}s)"


def auto_switch_header_lines(
    enabled: bool, strategy: str, cadence: int, seconds_to_next: float
) -> list[str]:
    """Status-header rows for the top of the menu.

    Enabled -> on/off, method, and a live countdown line (the only row the sync
    tick re-titles each second). Disabled -> a single ``OFF`` line, since there
    is no check to count down to.
    """
    if not enabled:
        return ["Auto-swap: OFF"]
    return [
        "Auto-swap: ON",
        f"Method: {auto_switch_strategy_label(strategy)}",
        auto_switch_countdown_text(seconds_to_next, cadence),
    ]


def format_account_label(
    num: int, email: str, usage: dict | str | None, now: float | None = None
) -> str:
    """Build one account row's menu label."""
    return f"{num}  {email}  {usage_summary(usage, now)}"


def account_detail_lines(usage: dict | str | None) -> list[str]:
    """Per-window detail rows for the dropdown, mirroring the CLI's usage tree.

    Each known rate-limit window (5h, 7d) becomes a row like
    ``"5h:  5%   resets 18:59   in 4h 46m"``. The clock-time + countdown are
    derived live from ``resets_at`` via :func:`oauth.format_reset` — the exact
    formatter the CLI uses — so a stale (cached) ``clock``/``countdown`` can't
    show a wrong time. A window with unknown ``pct`` is omitted entirely; the
    reset segment is dropped when ``resets_at`` is missing or unparseable.
    """
    if not isinstance(usage, dict):
        return []
    lines: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("pct")
        if not isinstance(pct, (int, float)):
            continue
        row = f"{label}: {pct:>2.0f}%"
        resets_at = window.get("resets_at")
        if isinstance(resets_at, str):
            try:
                countdown, clock = oauth.format_reset(resets_at)
            except ValueError:
                pass  # unparseable -> show the percent without a reset segment
            else:
                row += f"   resets {clock}   in {countdown}"
        lines.append(row)
    return lines


def group_running_instances(sessions, ides) -> list[tuple[str, str, int, bool]]:
    """Group running Claude Code sessions/IDEs for the dropdown.

    Mirrors ``switcher.status`` exactly: sessions group by
    ``(entrypoint_label, abbreviated cwd)``; each IDE lockfile contributes its
    workspace folders under ``(ide_short_name, abbreviated folder)``. A session
    and an IDE pointing at the same folder collapse into one group. Returns
    ``(label, folder, session_count, has_ide)`` tuples in first-seen order
    (sessions before IDE-only folders).
    """
    groups: dict[tuple[str, str], dict[str, int]] = {}
    for session in sessions:
        key = (entrypoint_label(session.entrypoint), abbreviate_path(session.cwd))
        groups.setdefault(key, {"sessions": 0, "ide": 0})["sessions"] += 1
    for ide in ides:
        name = ide_short_name(ide.ide_name)
        for folder in ide.workspace_folders:
            key = (name, abbreviate_path(folder))
            groups.setdefault(key, {"sessions": 0, "ide": 0})["ide"] += 1
    return [
        (label, folder, counts["sessions"], counts["ide"] > 0)
        for (label, folder), counts in groups.items()
    ]


def format_instance_row(group: tuple[str, str, int, bool]) -> str:
    """Render one running-instance group row, e.g.
    ``"VS Code   ~/Dev/TL-Starnav  (2 sessions, IDE)"``.

    The ``(... sessions, IDE)`` suffix mirrors ``switcher.status``: the session
    count is singular for 1, and ``IDE`` is appended when an IDE lockfile points
    at the same folder.
    """
    label, folder, session_count, has_ide = group
    parts: list[str] = []
    if session_count:
        parts.append(f"{session_count} session{'s' if session_count > 1 else ''}")
    if has_ide:
        parts.append("IDE")
    return f"{label}   {folder}  ({', '.join(parts)})"


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
) -> str:
    """Build the menu-bar title from the active account and settings."""
    if active_email is None:
        return ICON
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(_local_part(active_email))
    # In "both" mode the two percentages are otherwise indistinguishable, so
    # prefix each with a short window label ("5h"/"7d"). In single-window modes
    # the menu item already names the window, so the bare percentage is clear.
    both = settings.title_pct == "both"
    if settings.title_pct in ("5h", "both"):
        p = _window_pct(active_usage, "five_hour")
        if p is not None:
            segments.append(f"5h {p:.0f}%" if both else f"{p:.0f}%")
    if settings.title_pct in ("7d", "both"):
        p = _window_pct(active_usage, "seven_day")
        if p is not None:
            segments.append(f"7d {p:.0f}%" if both else f"{p:.0f}%")
    if not segments:
        return ICON
    return f"{ICON} " + " · ".join(segments)


NOSWAP_NOTIFY_EVERY = 3600  # seconds between repeat "no fresh account" notifications


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _worst_pct(usage: dict | str | None) -> float | None:
    """Higher of the 5h/7d utilization, or None if either window is unknown."""
    five = _window_pct(usage, "five_hour")
    seven = _window_pct(usage, "seven_day")
    if five is None or seven is None:
        return None
    return max(five, seven)


def next_blocked(
    limiting_by_account: dict[str, float | None],
    threshold: float,
    hysteresis: float,
    prev_blocked,
) -> frozenset[str]:
    """Sticky 'at-limit' set with a dead band, to stop auto-switch thrash.

    An account enters the set when its limiting % is ``>= threshold`` and leaves
    only when it drops below ``threshold - hysteresis``. Unknown (``None``) usage
    carries the prior membership — a network blip never unblocks an account.
    """
    nxt: set[str] = set()
    for num, pct in limiting_by_account.items():
        if pct is None:
            if num in prev_blocked:
                nxt.add(num)
            continue
        if num in prev_blocked:
            if pct >= threshold - hysteresis:
                nxt.add(num)
        elif pct >= threshold:
            nxt.add(num)
    return frozenset(nxt)


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad.

    Total — never raises. A far-future/past ``resets_at`` can make
    ``.timestamp()`` raise ``OverflowError``/``OSError`` on some platforms, and
    an unparseable string raises ``ValueError``; all fall through to ``inf`` so
    a bad value ranks last (never auto-selected). A timezone-naive timestamp is
    normalized to UTC before conversion so it orders consistently against
    UTC-aware peers (treating it as *local* time would reorder the consume-first
    ranking depending on the host's offset).
    """
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                dt = datetime.fromisoformat(ra)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except (ValueError, OverflowError, OSError):
                pass
    return float("inf")


def decide_auto_switch(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
    blocked=frozenset(),
) -> tuple[str, int | None]:
    """Reactive auto-switch: switch when the active account hits the threshold.

    ``blocked`` is the hysteresis set (account-number strings at/over limit); a
    blocked candidate must clear ``threshold - AUTO_HYSTERESIS`` to be eligible
    again. Returns ``("switch", num)``, ``("none", None)``,
    ``("unknown_active", None)``, ``("no_candidate", None)`` (all peers exhausted),
    or ``("no_candidate_unverifiable", None)`` (a peer's usage was unreadable).
    Total — never raises.
    """
    active = next((a for a in accounts if a[2]), None)
    if active is None:
        return ("none", None)
    active_worst = _worst_pct(active[3])
    if active_worst is None:
        return ("unknown_active", None)
    if active_worst < threshold:
        return ("none", None)

    candidates: list[tuple[float, float, float, int]] = []
    any_unverifiable = False
    for num, _email, is_active, usage in accounts:
        if is_active:
            continue
        worst = _worst_pct(usage)
        if worst is None:
            any_unverifiable = True
            continue
        limit = threshold - AUTO_HYSTERESIS if str(num) in blocked else threshold
        if worst >= limit:
            continue
        seven = _window_pct(usage, "seven_day")
        five = _window_pct(usage, "five_hour")
        candidates.append((worst, seven, five, num))
    if not candidates:
        return ("no_candidate_unverifiable", None) if any_unverifiable else ("no_candidate", None)
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return ("switch", candidates[0][3])


def decide_consume_first(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
    blocked=frozenset(),
) -> tuple[str, int | None]:
    """Proactive 'consume the soonest-resetting account first' strategy.

    Eligible accounts have 5h not blocked (hysteresis) AND 7d below the threshold;
    the eligible account whose 7d window resets soonest (then most headroom, then
    rotation order) is optimal. The active account wins exact ties (it is already
    optimal — never switch to an equally-good peer), and a missing/unparseable 7d
    reset time on the active account never demotes it below peers (an unknown reset
    means "no information", not "resets last"). Returns ``("switch", num)``,
    ``("none", None)`` (already optimal), ``("unknown_active", None)``,
    ``("no_candidate", None)`` (all weekly-exhausted -> notify),
    ``("no_candidate_unverifiable", None)``, or ``("all_session_limited", None)``
    (weekly room but all 5h-blocked -> silent). Total — never raises.
    """
    active = next((a for a in accounts if a[2]), None)
    if active is None:
        return ("none", None)
    if _window_pct(active[3], "five_hour") is None or _window_pct(active[3], "seven_day") is None:
        return ("unknown_active", None)

    # The active account's known 7d reset; if it's missing/unparseable we treat it
    # as "no information" rather than the worst case, so a peer never displaces a
    # healthy active account just because the API omitted its resets_at.
    active_reset = _resets_at_ts(active[3].get("seven_day"))

    eligible: list[tuple[float, float, int, int, bool]] = []
    any_unverifiable = False
    any_weekly_room = False
    for idx, (num, _email, is_active, usage) in enumerate(accounts):
        five = _window_pct(usage, "five_hour")
        seven = _window_pct(usage, "seven_day")
        if five is None or seven is None:
            if not is_active:
                any_unverifiable = True
            continue
        if seven < threshold:
            any_weekly_room = True
        limit5 = threshold - AUTO_HYSTERESIS if str(num) in blocked else threshold
        if five < limit5 and seven < threshold:
            reset = _resets_at_ts(usage.get("seven_day"))
            # If the active account's reset is unknown, don't let a peer's known
            # (finite) reset rank ahead of it: raise the peer's reset to the
            # active's (inf) so only headroom/rotation can distinguish them.
            if not is_active and active_reset == float("inf"):
                reset = active_reset
            eligible.append((reset, _worst_pct(usage), not is_active, idx, num))
    if not eligible:
        if any_unverifiable:
            return ("no_candidate_unverifiable", None)
        if any_weekly_room:
            return ("all_session_limited", None)
        return ("no_candidate", None)
    # Tie-break order: soonest 7d reset, most headroom, then the active account
    # (not is_active == False sorts first), then rotation index. The is_active
    # term makes an equally-optimal active account always win.
    eligible.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    best_num = eligible[0][4]
    if best_num == active[0]:
        return ("none", None)
    return ("switch", best_num)


def limiting_pct_by_account(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    strategy: str,
) -> dict[str, float | None]:
    """Per-account 'limiting %' feeding the hysteresis FSM, per strategy.

    reactive -> worst-of(5h, 7d); consume-first -> the 5h axis. None when unknown.
    """
    out: dict[str, float | None] = {}
    for num, _email, _is_active, usage in accounts:
        if strategy == "consume-first":
            out[str(num)] = _window_pct(usage, "five_hour")
        else:
            out[str(num)] = _worst_pct(usage)
    return out


def evaluate_strategy(
    strategy: str,
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
    blocked,
) -> tuple[str, int | None]:
    """Dispatch to the active strategy's decision function (unknown -> reactive)."""
    if strategy == "consume-first":
        return decide_consume_first(accounts, threshold, blocked)
    return decide_auto_switch(accounts, threshold, blocked)


def plan_auto_switch(
    decision: tuple[str, int | None],
    state: "MenuBarState",
    settings: "MenuBarSettings",
    now: float,
) -> tuple[str, int | None]:
    """Apply cooldown + notification rate-limiting to a decision.

    Returns ``("switch", num)``, ``("cooldown", None)``,
    ``("notify_noswap", None)``, or ``("noop", None)``. Total — never raises.
    """
    kind, num = decision
    if kind == "switch":
        if now - state.last_switch_at >= settings.auto_switch_cooldown:
            return ("switch", num)
        return ("cooldown", None)
    if kind == "no_candidate":
        if now - state.last_noswap_notify_at >= NOSWAP_NOTIFY_EVERY:
            return ("notify_noswap", None)
        return ("noop", None)
    return ("noop", None)


def _snapshot(switcher, full: bool = True, force: bool = False) -> dict:
    """Fetch accounts + usage off the main thread. Returns a render snapshot.

    Shape: ``{"accounts": [(num, email, is_active, usage), ...],
    "active_email": str | None, "active_usage": dict | str | None,
    "instances": [(label, folder, session_count, has_ide), ...]}``.
    ``full=False`` fetches only the active account over the network (backups come
    from cache) to stay under the usage endpoint's per-IP rate limit; ``full=True``
    fetches all. ``force=True`` bypasses ``_collect_usage``'s 15s fresh-cache
    shortcut so an explicit user refresh always re-fetches (the per-IP 429
    backoff still applies). The running-instance list is computed exactly once
    per refresh and reused. Never raises — failures degrade to empty/unknown.
    """
    instances = _snapshot_instances(switcher)
    try:
        accounts_info = switcher._build_accounts_info()
        only = None
        if not full:
            active = next((str(info[0]) for info in accounts_info if info[4]), None)
            only = {active} if active else None
        usages = switcher._collect_usage(accounts_info, only=only, force=force)
    except Exception:
        switcher._logger.debug("menubar snapshot failed", exc_info=True)
        return {
            "accounts": [], "active_email": None, "active_usage": None,
            "instances": instances,
        }

    accounts = []
    active_email = None
    active_usage = None
    for (num, email, _org, _uuid, is_active, _creds), usage in zip(accounts_info, usages):
        accounts.append((num, email, is_active, usage))
        if is_active:
            active_email, active_usage = email, usage
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
        "instances": instances,
    }


def _snapshot_instances(switcher) -> list[tuple[str, str, int, bool]]:
    """Grouped running Claude instances for the dropdown; ``[]`` on any failure.

    Detecting instances is local file I/O independent of the usage fetch, so it
    runs even when the account snapshot degrades. The menu must never break, so
    every failure mode (missing dirs, unreadable lockfiles) collapses to ``[]``.
    """
    try:
        sessions, ides = get_running_instances()
        return group_running_instances(sessions, ides)
    except Exception:
        switcher._logger.debug("menubar instance detection failed", exc_info=True)
        return []


def _snapshot_signature(snapshot: dict, settings: "MenuBarSettings") -> tuple:
    """A cheap, hashable signature of everything ``rebuild_menu`` renders.

    Comparing this between refreshes lets the sync tick skip rebuilding the
    whole NSMenu when nothing the user would see changed. It covers the title
    inputs (active email/usage + the settings that drive the title and menu
    rows), the per-account rows, and the running-instance rows — the same data
    ``rebuild_menu`` reads. Import-safe (no rumps) so it can be unit-tested.
    """
    accounts = tuple(
        (num, email, is_active, _usage_signature(usage))
        for (num, email, is_active, usage) in snapshot.get("accounts", [])
    )
    instances = tuple(tuple(group) for group in (snapshot.get("instances") or []))
    settings_sig = (
        settings.show_account_name,
        settings.title_pct,
        settings.auto_switch_enabled,
        settings.auto_switch_strategy,
        settings.auto_switch_threshold,
        settings.auto_switch_cooldown,
        settings.auto_switch_interval,
        settings.refresh_interval,
    )
    return (
        snapshot.get("active_email"),
        _usage_signature(snapshot.get("active_usage")),
        accounts,
        instances,
        settings_sig,
    )


def _usage_signature(usage: dict | str | None):
    """Hashable projection of a usage value for the rebuild diff.

    A usage dict is rendered by ``format_account_label`` / ``account_detail_lines``
    via its per-window ``pct`` and ``resets_at``; project just those (plus any
    string sentinel / None) so equal renders compare equal without depending on
    dict ordering or unhashable nested values.
    """
    if isinstance(usage, dict):
        return tuple(
            (
                key,
                window.get("pct") if isinstance(window, dict) else None,
                window.get("resets_at") if isinstance(window, dict) else None,
            )
            for key, window in sorted(usage.items())
        )
    return usage  # str sentinel ("no credentials" / "rate limited") or None


def _maybe_rebuild_on_dirty(app) -> bool:
    """Rebuild the menu only when the rendered-state signature actually changed.

    Consumes ``app._dirty`` (set by a worker after it stores a new snapshot) and
    rebuilds via ``app.rebuild_menu()`` only if the signature differs from the
    last rendered one, avoiding a full NSMenu teardown/rebuild every refresh when
    nothing visible changed. Returns True if a rebuild happened. Import-safe so
    the diff logic is unit-testable without rumps.
    """
    if not app._dirty:
        return False
    app._dirty = False
    sig = _snapshot_signature(app.snapshot, app.settings)
    if sig == getattr(app, "_menu_sig", None):
        return False
    app._menu_sig = sig
    app.rebuild_menu()
    return True


def _refresh_async_impl(app, full: bool = False, force: bool = False) -> bool:
    """Start a background refresh worker, honoring the in-flight guard.

    Compare-and-set under the guard's lock so at most one worker runs at a time.
    A burst of callers (refresh timer, sync tick, manual "Refresh now") can't
    each pass the check and spawn a duplicate worker. When ``force=True`` loses
    the slot to an in-flight worker, the guard records a pending forced refresh
    so the running worker launches a follow-up — the click is queued, never
    dropped. Returns True if this call started a worker. Import-safe: spawning
    goes through ``app._spawn`` so tests can drive it without rumps.
    """
    if not app._refresh_guard.try_begin(force=force):
        return False
    app._spawn(_worker_impl, (app, full, force))
    return True


def _worker_impl(app, full: bool, force: bool = False) -> None:
    """Background-refresh worker body (runs off the Cocoa main thread).

    Rebinds plain attributes (atomic in CPython) that the main-thread sync tick
    reads. At most one worker runs at a time (see ``_refresh_async_impl``). On
    completion, if a forced refresh was queued while this worker ran, it starts
    the follow-up forced refresh so a "Refresh now" click never gets dropped.
    Import-safe so the force-threading and follow-up logic are unit-testable.
    """
    try:
        now = time.time()
        if now - app._last_full_fetch >= _FULL_REFRESH_EVERY:
            full = True
        # Re-arm Keychain probing each cycle (a one-off `security` timeout flips
        # the store to file mode and sticks for the process); confine the
        # capability-cache mutation under the guard's exclusive lock so a
        # concurrent main-thread read can't observe a torn state.
        with app._refresh_guard.run_exclusive():
            app.switcher.recheck_keychain()
            snap = _snapshot(app.switcher, full=full, force=force)
        app.snapshot = snap
        app._snapshot_at = time.time()
        if full:
            app._last_full_fetch = app._snapshot_at
        app._dirty = True  # picked up by on_sync_tick on the main thread
    finally:
        # Release the slot and atomically learn whether a forced refresh was
        # queued meanwhile; if so, run it now (exactly one worker still active
        # at a time, since the follow-up re-claims the freed slot).
        if app._refresh_guard.finish_and_take_pending():
            _refresh_async_impl(app, full=True, force=True)


def _offload_action(app_guard, work, on_done=None) -> bool:
    """Run a blocking switcher action off the Cocoa main thread.

    Auto-switch and the menu switch/add/remove callbacks do keychain-subprocess
    and ``FileLock`` work that would freeze the UI if run on the main thread.
    This mirrors the refresh worker offload: claim the in-flight slot (so a
    second click can't spawn an overlapping worker), run ``work`` on a daemon
    thread, then release the slot and invoke ``on_done`` (used to mark the app
    dirty so the main-thread sync tick re-renders). Returns True if a worker was
    started, False if one was already in flight. Import-safe and unit-testable.
    """
    if not app_guard.try_begin():
        return False

    def runner():
        try:
            work()
        finally:
            app_guard.finish()
            if on_done is not None:
                on_done()

    threading.Thread(target=runner, daemon=True).start()
    return True


LAUNCH_AGENT_LABEL = "com.claude-swap.menubar"


def render_launch_agent_plist(
    *,
    label: str,
    program_args: list[str],
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> str:
    """Render a per-user LaunchAgent plist for the menu bar app.

    The agent loads into the user's GUI (``Aqua``) session so the menu-bar icon
    can reach WindowServer *and* ``security`` can read the unlocked login
    Keychain — a background/daemon session can do neither. ``KeepAlive`` restarts
    the app only when it exits non-zero, so an explicit Quit from the menu stays
    quit while a crash is recovered automatically.
    """
    plist: dict = {
        "Label": label,
        "ProgramArguments": list(program_args),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }
    if stdout_path is not None:
        plist["StandardOutPath"] = stdout_path
    if stderr_path is not None:
        plist["StandardErrorPath"] = stderr_path
    return plistlib.dumps(plist).decode("utf-8")


def launch_agent_plist_path() -> Path:
    """Path of the menu-bar LaunchAgent plist in the user's ``LaunchAgents`` dir."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _menubar_program_args() -> list[str]:
    """Argv launchd should run: the current interpreter + ``-m claude_swap``.

    Pinning to ``sys.executable -m claude_swap`` (rather than the ``cswap``
    console script) ties the agent to the exact interpreter cswap is installed
    in, with no dependence on ``PATH`` or a shebang being resolvable at login.
    """
    return [sys.executable, "-m", "claude_swap", "--menubar"]


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


_BOOTSTRAP_ATTEMPTS = 3
_BOOTSTRAP_RETRY_DELAY = 0.1  # seconds; lets an async bootout settle before retry


def install_startup() -> Path:
    """Write the LaunchAgent plist and (re)load it into the GUI session.

    Idempotent: re-running rewrites the plist and reloads the agent so a changed
    interpreter path or config takes effect. Returns the plist path.
    """
    log_dir = Path.home() / "Library" / "Logs" / "claude-swap"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agent_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render_launch_agent_plist(
            label=LAUNCH_AGENT_LABEL,
            program_args=_menubar_program_args(),
            stdout_path=str(log_dir / "menubar.out.log"),
            stderr_path=str(log_dir / "menubar.err.log"),
        ),
        encoding="utf-8",
    )
    domain = _gui_domain()
    target = f"{domain}/{LAUNCH_AGENT_LABEL}"
    # bootout is best-effort AND asynchronous: re-installing over a running agent,
    # it can return before launchd has finished tearing the old job down, so an
    # immediate bootstrap of the same label transiently fails ("service already
    # bootstrapped" / EIO). Retry a few times — re-booting out and pausing briefly
    # between attempts — before treating a failure as real (e.g. no Aqua GUI domain
    # over SSH, or an MDM/SIP policy blocking it).
    bootstrap = None
    for attempt in range(_BOOTSTRAP_ATTEMPTS):
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True)
        bootstrap = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True
        )
        if bootstrap.returncode == 0:
            break
        if attempt + 1 < _BOOTSTRAP_ATTEMPTS:
            time.sleep(_BOOTSTRAP_RETRY_DELAY)
    # If bootstrap still failed, the agent was NOT loaded. Surface that instead of
    # letting the caller report a misleading "installed and running" success.
    if bootstrap.returncode != 0:
        detail = (bootstrap.stderr or b"").decode("utf-8", "replace").strip()
        raise ClaudeSwitchError(
            "Failed to load the menu bar login item via "
            f"'launchctl bootstrap {domain}'"
            + (f": {detail}" if detail else ".")
        )
    # Start it now too, so installing also launches the app immediately.
    subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True)
    return plist_path


def uninstall_startup() -> bool:
    """Unload the agent and delete its plist. Returns True if a plist existed."""
    plist_path = launch_agent_plist_path()
    subprocess.run(
        ["launchctl", "bootout", _gui_domain(), str(plist_path)], capture_output=True
    )
    existed = plist_path.exists()
    if existed:
        # missing_ok guards a TOCTOU race: a concurrent uninstall or an external
        # deletion between exists() and unlink() must not raise FileNotFoundError.
        plist_path.unlink(missing_ok=True)
    return existed


class _RefreshGuard:
    """Thread-safe in-flight guard for the menu bar's background refresh.

    Import-safe (no rumps) so the cross-thread synchronization can be unit
    tested in isolation. Two responsibilities, both backed by one lock:

    * ``try_begin`` / ``finish`` — a compare-and-set admission control so at
      most one worker runs at a time. The check ("is a worker already in
      flight?") and the flip (mark one in flight) happen atomically under the
      lock, so a burst of concurrent ``refresh_async`` calls can't each pass
      the check and spawn a duplicate worker. ``finish`` clears the flag (idle
      when there's nothing in flight is a safe no-op).
    * ``run_exclusive`` — a serialized critical section used to confine the
      keychain-capability-cache mutation. ``recheck_keychain`` rebinds the
      switcher's shared cache and the snapshot's keychain reads re-learn it;
      running that under this lock keeps the mutation single-threaded so a
      concurrent reader can't observe a torn state. It uses a *separate* lock
      from the admission flag (so it is independent of the in-flight slot and
      can never deadlock against ``finish``/``try_begin``).
    """

    def __init__(self) -> None:
        self._flag_lock = threading.Lock()
        self._cap_lock = threading.Lock()
        self._in_flight = False
        self._pending_force = False

    @property
    def in_flight(self) -> bool:
        with self._flag_lock:
            return self._in_flight

    def try_begin(self, force: bool = False) -> bool:
        """Atomically claim the single worker slot.

        Returns True if the caller won the right to start a worker (no worker
        was in flight), False if one is already running. The check-and-flip is
        done under the lock so concurrent callers serialize and exactly one
        wins.

        When a worker is already in flight and ``force=True``, the request is
        rejected (False) but a pending-forced-refresh flag is recorded so the
        running worker can launch a follow-up forced refresh on completion —
        i.e. a user "Refresh now" click is queued, never silently dropped. A
        non-forced request that loses the slot is dropped as before (the next
        timer tick will refresh anyway).
        """
        with self._flag_lock:
            if self._in_flight:
                if force:
                    self._pending_force = True
                return False
            self._in_flight = True
            return True

    def finish(self) -> None:
        """Release the worker slot. Safe to call when already idle."""
        with self._flag_lock:
            self._in_flight = False

    def finish_and_take_pending(self) -> bool:
        """Release the slot and atomically consume any queued forced refresh.

        Returns True if a forced refresh was queued while this worker ran (the
        caller should then start a follow-up forced refresh), False otherwise.
        Clearing the slot and reading-and-clearing the pending flag happen under
        the same lock so a forced request arriving in this instant is never lost
        nor double-counted.
        """
        with self._flag_lock:
            self._in_flight = False
            pending = self._pending_force
            self._pending_force = False
            return pending

    def run_exclusive(self):
        """Context manager serializing its body against other callers."""
        return self._cap_lock


def _guard_against_terminal_suspend() -> None:
    """Ignore SIGTSTP so Ctrl+Z in a controlling terminal can't suspend the app.

    Run as ``cswap --menubar`` in a foreground terminal, the menu bar app owns an
    NSStatusItem but its Cocoa runloop is an ordinary foreground job. Ctrl+Z
    sends SIGTSTP and *stops* the process: the icon stays drawn but is frozen and
    unresponsive, and a stopped process can't act on the SIGHUP sent when the
    terminal later closes — so the icon lingers as a phantom that's hard to kill.

    Ignoring SIGTSTP keeps the runloop alive (Ctrl+Z becomes a no-op); a normal
    terminal close then delivers SIGHUP, the process exits, and the system clears
    the icon. No-op under launchd (no controlling tty) and anywhere SIGTSTP is
    absent or can't be set (e.g. not the main thread).
    """
    import signal

    try:
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    except (ValueError, AttributeError, OSError):
        # ValueError: not on the main thread; AttributeError: no SIGTSTP (Windows).
        pass


def run(switcher) -> int:
    """Entry point for ``cswap --menubar``. Blocks until the user quits."""
    import rumps  # lazy: optional dependency, imported only when launching

    _guard_against_terminal_suspend()

    settings_path = switcher.backup_dir / "menubar_settings.json"
    state_path = switcher.backup_dir / "menubar_state.json"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self.switcher = switcher
            self.settings = MenuBarSettings.load(settings_path)
            self.snapshot = {
                "accounts": [], "active_email": None, "active_usage": None,
                "instances": [],
            }
            self._dirty = False
            self._menu_sig = None  # signature of the last rendered menu (3.3)
            self._countdown_item = None  # live "Next check:" header item, if any
            self.state = MenuBarState.load(state_path)
            self._snapshot_at = 0.0
            self._last_auto_eval = 0.0
            self._last_full_fetch = 0.0
            # Thread-safe in-flight guard: the compare-and-set that admits at
            # most one worker, plus the lock that confines the keychain-
            # capability-cache mutation to one thread at a time.
            self._refresh_guard = _RefreshGuard()
            # Separate guard for blocking switch/add/remove actions so a click
            # can't spawn an overlapping action worker (3.2).
            self._action_guard = _RefreshGuard()
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
            self.rebuild_menu()
            # Background refresh on the user's interval, plus a fast UI-sync tick
            # that applies snapshots produced by worker threads on the main thread.
            self.refresh_timer = rumps.Timer(self.on_refresh_tick, self.settings.refresh_interval)
            self.refresh_timer.start()
            self.sync_timer = rumps.Timer(self.on_sync_tick, 1)
            self.sync_timer.start()
            self.refresh_async(full=True)  # first fetch is a full one

        # ---- refresh plumbing -------------------------------------------------
        def _spawn(self, target, args):
            """Start a daemon worker thread (indirection so the import-safe
            ``_refresh_async_impl`` can spawn without referencing rumps)."""
            threading.Thread(target=target, args=args, daemon=True).start()

        def refresh_async(self, full=False, force=False):
            # Compare-and-set under a lock: at most one worker runs at a time.
            # A forced refresh (manual "Refresh now") that loses the slot is
            # queued (not dropped); a non-forced tick is dropped as before. See
            # _refresh_async_impl / _RefreshGuard.
            return _refresh_async_impl(self, full=full, force=force)

        def _worker(self, full, force=False):
            # Handoff: the worker rebinds plain attributes (atomic in CPython);
            # the main-thread sync tick reads them. recheck_keychain() re-arms
            # Keychain probing each cycle so a transient `security` timeout
            # self-heals; the capability-cache mutation is confined under the
            # guard's exclusive lock. force threads through to bypass the 15s
            # cache TTL; a queued forced refresh runs as a follow-up on finish.
            _worker_impl(self, full, force=force)

        def _offload(self, work):
            """Run a blocking switcher action off the Cocoa main thread (3.2).

            Auto-switch and the menu switch/add/remove callbacks do keychain-
            subprocess + FileLock work that would freeze the UI on the main
            thread. Offload it onto a daemon worker, guarded so a click can't
            spawn an overlapping action; on completion mark the app dirty so the
            sync tick re-renders. Returns True if a worker was started.
            """
            return _offload_action(
                self._action_guard, work,
                on_done=lambda: setattr(self, "_dirty", True),
            )

        def on_refresh_tick(self, _timer):
            self.refresh_async()

        def on_sync_tick(self, _timer):
            # Rebuild the menu only when the rendered-state signature changed
            # (3.3) — avoids a full NSMenu teardown every refresh when nothing
            # the user sees has changed.
            _maybe_rebuild_on_dirty(self)
            self._update_countdown()
            self._detect_active_change()
            if self.settings.auto_switch_enabled:
                self._auto_tick()

        def _update_countdown(self):
            """Re-title the live 'next check' header line each tick (no rebuild).

            Touches only the single countdown leaf item, so it stays cheap and
            never tears down the NSMenu while the user has it open.
            """
            item = self._countdown_item
            if item is None or not self.settings.auto_switch_enabled:
                return
            cadence = self.settings.auto_switch_interval or self.settings.refresh_interval
            seconds_to_next = max(0.0, (self._last_auto_eval + cadence) - time.time())
            item.title = auto_switch_countdown_text(seconds_to_next, cadence)

        def _detect_active_change(self):
            # Reflect account switches from any source (menu, CLI, auto-switcher)
            # within ~1s. Detecting *which* account is active is a cheap local
            # read of ~/.claude.json -- no Keychain or usage API -- so we can do
            # it on every tick. We gate the read on the file's mtime (a cheap
            # stat) so a large config isn't parsed each second, and only kick a
            # refresh when the active email actually changed (Claude Code rewrites
            # this file often for unrelated reasons).
            if self._refresh_guard.in_flight:
                return  # a worker is already in-flight; it refreshes the marker
            try:
                mtime = self._config_path.stat().st_mtime
            except OSError:
                return
            if mtime == self._config_mtime:
                return
            self._config_mtime = mtime
            current = self.switcher._get_current_account()
            email = current[0] if current else None
            if email and email != self.snapshot.get("active_email"):
                self.refresh_async(full=True)

        def _auto_tick(self):
            now = time.time()
            cadence = self.settings.auto_switch_interval or self.settings.refresh_interval
            if now - self._last_auto_eval < cadence:
                return
            # If the snapshot is staler than the cadence (always true in mode B
            # with a sub-refresh interval; possible in either mode), fetch fresh
            # and evaluate on a later tick so we never act on stale usage.
            if now - self._snapshot_at > cadence and not self._refresh_guard.in_flight:
                # consume-first forces a full fetch here; between these it may rank
                # backups up to _FULL_REFRESH_EVERY old — fine, since it ranks by
                # weekly reset time (days-scale), not by minute-to-minute usage.
                self.refresh_async(full=(self.settings.auto_switch_strategy == "consume-first"))
                return
            self._last_auto_eval = now
            self._maybe_auto_switch(now)

        def _maybe_auto_switch(self, now):
            accounts = self.snapshot["accounts"]
            strategy = self.settings.auto_switch_strategy
            threshold = self.settings.auto_switch_threshold
            limiting = limiting_pct_by_account(accounts, strategy)
            self.state.blocked = sorted(
                next_blocked(limiting, threshold, AUTO_HYSTERESIS, frozenset(self.state.blocked))
            )
            self.state.save(state_path)
            decision = evaluate_strategy(strategy, accounts, threshold, frozenset(self.state.blocked))
            action, num = plan_auto_switch(decision, self.state, self.settings, now)
            if action == "switch":
                # Record the switch timestamp up front so the cooldown holds even
                # if the (offloaded) switch is slow; the keychain + FileLock work
                # runs off the Cocoa main thread so the UI never freezes (3.2).
                self.state.last_switch_at = now
                self.state.save(state_path)

                def do_switch(num=num):
                    try:
                        self.switcher.switch_to(str(num))
                    except ClaudeSwitchError as e:
                        self.switcher._logger.warning("auto-switch failed: %s", e)
                        # notify.notify (osascript) works from this non-bundled
                        # LaunchAgent process; rumps.notification would raise here.
                        notify.notify("claude-swap", f"Auto-switch failed: {e}")
                        return
                    # No rumps.notification here: the swap notification is posted
                    # by the unified notifier (switch_to -> _perform_switch ->
                    # _announce_switch -> notify.notify), wired in cli.main.
                    # Posting one here too would double-notify.
                    self.refresh_async(full=True)

                self._offload(do_switch)
            elif action == "notify_noswap":
                # Post first, then record the rate-limit timestamp only after the
                # alert is dispatched — otherwise a failed notification would burn
                # the NOSWAP_NOTIFY_EVERY budget and suppress retries for an hour.
                # notify.notify (osascript) works from this non-bundled process and
                # never raises; rumps.notification would raise here.
                notify.notify(
                    "claude-swap",
                    f"Claude limit — no fresh account. Active account is at its "
                    f"limit (≥{self.settings.auto_switch_threshold}%) but no other "
                    "account has headroom.",
                )
                self.state.last_noswap_notify_at = now
                self.state.save(state_path)

        # ---- menu construction ------------------------------------------------
        def rebuild_menu(self):
            self.title = format_title(
                self.snapshot["active_email"], self.snapshot["active_usage"], self.settings
            )
            # Built imperatively (not via `self.menu = [list]`) because the
            # disabled detail/instance rows can share identical text across
            # accounts (e.g. two unused accounts both "5h:  0%"). rumps keys
            # menu items by title and silently drops a duplicate-titled item, so
            # those rows are added with explicit unique keys via `self.menu[k]=`.
            self.menu.clear()

            # Auto-swap status header at the very top. The status/method lines
            # change only with settings (covered by the rebuild signature); the
            # countdown line is re-titled in place each second by the sync tick,
            # so it stays OUT of the signature (else it'd force a full NSMenu
            # rebuild every tick — defeating the 3.3 optimization).
            cadence = self.settings.auto_switch_interval or self.settings.refresh_interval
            seconds_to_next = max(0.0, (self._last_auto_eval + cadence) - time.time())
            header = auto_switch_header_lines(
                self.settings.auto_switch_enabled,
                self.settings.auto_switch_strategy,
                cadence,
                seconds_to_next,
            )
            self._countdown_item = None
            for idx, line in enumerate(header):
                item = rumps.MenuItem(line, callback=None)
                self.menu.add(item)
                if self.settings.auto_switch_enabled and idx == len(header) - 1:
                    self._countdown_item = item  # the live "Next check:" line
            self.menu.add(None)

            accounts = self.snapshot["accounts"]
            for num, email, is_active, usage in accounts:
                item = rumps.MenuItem(
                    format_account_label(num, email, usage),
                    callback=self._make_switch_to(num),
                )
                item.state = 1 if is_active else 0
                self.menu.add(item)  # title carries the slot number -> unique
                for i, line in enumerate(account_detail_lines(usage)):
                    self.menu[f"detail-{num}-{i}"] = rumps.MenuItem(
                        f"    {line}", callback=None
                    )
            if not accounts:
                self.menu.add(rumps.MenuItem("No managed accounts", callback=None))

            instances = self.snapshot.get("instances") or []
            if instances:
                self.menu.add(None)
                self.menu.add(rumps.MenuItem("Running instances", callback=None))
                for i, group in enumerate(instances):
                    self.menu[f"instance-{i}"] = rumps.MenuItem(
                        f"    {format_instance_row(group)}", callback=None
                    )

            self.menu.add(None)
            self.menu.add(rumps.MenuItem("Rotate to next", callback=self._switch(None)))
            self.menu.add(rumps.MenuItem("Switch to best", callback=self._switch("best")))
            self.menu.add(
                rumps.MenuItem("Next available", callback=self._switch("next-available"))
            )
            self.menu.add(None)
            self.menu.add(self._add_menu(rumps))
            self.menu.add(self._remove_menu(rumps))
            self.menu.add(
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds)
            )
            self.menu.add(None)
            self.menu.add(self._settings_menu(rumps))
            self.menu.add(rumps.MenuItem("Refresh now", callback=self.on_refresh_now))
            self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

        def _add_menu(self, rumps):
            menu = rumps.MenuItem("Add account")
            menu.add(rumps.MenuItem("From current login", callback=self.on_add_login))
            if hasattr(self.switcher, "add_account_from_token"):
                menu.add(rumps.MenuItem("From setup-token…", callback=self.on_add_token))
            if hasattr(self.switcher, "add_account_from_oauth"):
                menu.add(rumps.MenuItem("Sign in with browser…", callback=self.on_add_browser_login))
            return menu

        def _remove_menu(self, rumps):
            menu = rumps.MenuItem("Remove account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _usage in accounts:
                menu.add(rumps.MenuItem(f"{num}  {email}", callback=self._make_remove(num)))
            return menu

        def _settings_menu(self, rumps):
            menu = rumps.MenuItem("Settings")
            name_item = rumps.MenuItem("Show account name in menu bar", callback=self.on_toggle_name)
            name_item.state = 1 if self.settings.show_account_name else 0
            menu.add(name_item)
            title_pct = rumps.MenuItem("Title percentage")
            tp_labels = {"off": "None", "5h": "Session (5h)",
                         "7d": "Weekly (7d)", "both": "Both (5h · 7d)"}
            for mode in TITLE_PCT_CHOICES:
                ch = rumps.MenuItem(tp_labels[mode], callback=self._make_title_pct(mode))
                ch.state = 1 if self.settings.title_pct == mode else 0
                title_pct.add(ch)
            menu.add(title_pct)
            interval = rumps.MenuItem("Refresh interval")
            labels = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
            for secs in REFRESH_CHOICES:
                choice = rumps.MenuItem(labels[secs], callback=self._make_interval(secs))
                choice.state = 1 if self.settings.refresh_interval == secs else 0
                interval.add(choice)
            menu.add(interval)

            auto_item = rumps.MenuItem(
                auto_switch_menu_label(self.settings.auto_switch_enabled),
                callback=self.on_toggle_autoswitch,
            )
            menu.add(auto_item)

            strategy_menu = rumps.MenuItem("Auto-switch strategy")
            st_labels = {"reactive": "Reactive (threshold)",
                         "consume-first": "Consume-first (soonest reset)"}
            for name in AUTO_STRATEGY_CHOICES:
                ch = rumps.MenuItem(st_labels[name], callback=self._make_strategy(name))
                ch.state = 1 if self.settings.auto_switch_strategy == name else 0
                strategy_menu.add(ch)
            menu.add(strategy_menu)

            threshold_menu = rumps.MenuItem("Auto-switch threshold")
            for pct in AUTO_THRESHOLD_CHOICES:
                ch = rumps.MenuItem(f"{pct}%", callback=self._make_threshold(pct))
                ch.state = 1 if self.settings.auto_switch_threshold == pct else 0
                threshold_menu.add(ch)
            menu.add(threshold_menu)

            cooldown_menu = rumps.MenuItem("Auto-switch cooldown")
            cd_labels = {300: "5 minutes", 600: "10 minutes", 1800: "30 minutes"}
            for secs in AUTO_COOLDOWN_CHOICES:
                ch = rumps.MenuItem(cd_labels[secs], callback=self._make_cooldown(secs))
                ch.state = 1 if self.settings.auto_switch_cooldown == secs else 0
                cooldown_menu.add(ch)
            menu.add(cooldown_menu)

            check_menu = rumps.MenuItem("Auto-switch check")
            ck_labels = {0: "With display refresh", 60: "Every 1 minute",
                         180: "Every 3 minutes", 300: "Every 5 minutes"}
            for secs in AUTO_CHECK_CHOICES:
                ch = rumps.MenuItem(ck_labels[secs], callback=self._make_check(secs))
                ch.state = 1 if self.settings.auto_switch_interval == secs else 0
                check_menu.add(ch)
            menu.add(check_menu)

            return menu

        # ---- callbacks --------------------------------------------------------
        def _save_and_rebuild(self):
            self.settings.save(settings_path)
            self.rebuild_menu()

        def _offload_switch(self, fn, *, record_switch=False):
            """Offload a blocking switcher mutation (switch/add/remove) (3.2).

            The keychain-subprocess + FileLock work runs on a daemon worker so
            the Cocoa main thread (and the menu) never freezes. Errors are
            surfaced via notify.notify — rumps.alert can't be shown from a
            worker thread. On success a full refresh is queued (the swap
            notification itself is posted by the unified notifier wired in
            cli.main). ``record_switch`` stamps the cooldown timestamp.
            """
            def work():
                try:
                    fn()
                except ClaudeSwitchError as e:
                    # notify.notify (osascript) works from this non-bundled
                    # process and never raises; rumps.alert would need the main
                    # thread and isn't safe here.
                    notify.notify("claude-swap", str(e))
                    return
                if record_switch:
                    self.state.last_switch_at = time.time()
                    self.state.save(state_path)
                self.refresh_async(full=True)
            self._offload(work)

        # Swap notifications are posted by the unified notifier wired in
        # cli.main (switch/switch_to -> _perform_switch -> _announce_switch ->
        # notify.notify), so the menu callbacks below no longer post their own.

        def _make_switch_to(self, num):
            def cb(_sender):
                self._offload_switch(
                    lambda: self.switcher.switch_to(str(num)), record_switch=True
                )
            return cb

        def _switch(self, strategy):
            def cb(_sender):
                self._offload_switch(
                    lambda: self.switcher.switch(strategy=strategy), record_switch=True
                )
            return cb

        def _make_remove(self, num):
            def cb(_sender):
                # The confirmation dialog stays on the main thread (it's UI);
                # only the blocking removal is offloaded.
                if rumps.alert(
                    title="Remove account",
                    message=f"Remove account {num}?",
                    ok="Remove",
                    cancel="Cancel",
                ) == 1:  # 1 == OK
                    self._offload_switch(
                        lambda: self.switcher.remove_account(str(num), force=True)
                    )
            return cb

        def on_add_login(self, _sender):
            self._offload_switch(self.switcher.add_account)

        def on_add_token(self, _sender):
            # A menu-bar (accessory) app isn't the active app, so a modal
            # rumps.Window can render black/blank until we bring the app
            # forward. Activate before showing the input dialogs.
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            email_win = rumps.Window(
                title="Add account from setup-token",
                message="Email for this token:",
                ok="Next", cancel="Cancel", dimensions=(320, 24),
            )
            email_resp = email_win.run()
            if email_resp.clicked != 1 or not email_resp.text.strip():
                return
            token_win = rumps.Window(
                title="Add account from setup-token",
                message="Setup token (sk-ant-oat01-…):",
                ok="Add", cancel="Cancel", dimensions=(320, 24),
            )
            token_resp = token_win.run()
            if token_resp.clicked != 1 or not token_resp.text.strip():
                return
            # Dialogs ran on the main thread; offload the blocking add (keychain
            # + FileLock) so the UI doesn't freeze (3.2).
            token = token_resp.text.strip()
            email = email_resp.text.strip()
            self._offload_switch(lambda: self.switcher.add_account_from_token(
                token=token, email=email, slot=None,
            ))

        def on_add_browser_login(self, _sender):
            # Bring the accessory app forward so any future dialogs render, then run the
            # OAuth login off the main thread (it blocks until the browser callback).
            import webbrowser

            import AppKit
            from claude_swap import oauth_login

            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

            def worker():
                try:
                    result = oauth_login.run_login_flow(
                        open_browser=webbrowser.open,
                        make_server=oauth_login.LoopbackServer,
                        exchange=oauth_login.exchange_code,
                    )
                    num = self.switcher.add_account_from_oauth(
                        credentials=result.credentials,
                        email=result.identity.email,
                        org_name=result.identity.org_name,
                        org_uuid=result.identity.org_uuid,
                        account_uuid=result.identity.account_uuid,
                    )
                    self.switcher._logger.info("browser sign-in added account %s", num)
                    # Refresh first so a notification failure can never skip the
                    # UI refresh and leave the just-added account missing.
                    self.refresh_async(full=True)
                    # Confirm the add (this is not a swap, so it doesn't go through
                    # the unified swap notifier — post a distinct "added" alert).
                    # notify.notify (osascript) works from this non-bundled process
                    # and never raises; rumps.notification would raise here.
                    notify.notify(
                        "claude-swap",
                        f"Account added — signed in and added "
                        f"{result.identity.email or f'Account-{num}'}. "
                        "Switch to it from the menu when ready.",
                    )
                except ClaudeSwitchError as e:
                    self.switcher._logger.warning("browser sign-in failed: %s", e)
                    notify.notify("claude-swap", f"Sign-in failed: {e}")
                except Exception:
                    self.switcher._logger.debug("browser sign-in error", exc_info=True)
                    notify.notify("claude-swap",
                                  "Sign-in failed: an unexpected error occurred "
                                  "during sign-in.")

            threading.Thread(target=worker, daemon=True).start()

        def on_refresh_creds(self, _sender):
            if self.switcher._get_current_account() is None:
                rumps.alert(title="claude-swap",
                            message="No active Claude Code login detected. Log in first.")
                return

            # Offload the blocking add (keychain read + FileLock) so the menu
            # doesn't freeze (3.2). Errors are surfaced via notify.notify — a
            # worker thread can't safely show a rumps.alert.
            def work():
                try:
                    self.switcher.add_account(slot=None)
                except CredentialReadError:
                    # Almost always a launchd/login-agent Keychain block: the
                    # active credential lives in the macOS Keychain, which a
                    # background agent can't read (the security call times out).
                    notify.notify(
                        "claude-swap",
                        "Couldn't read the active credential. If the menu bar is "
                        "running as a background/login agent, macOS blocks its "
                        "Keychain access — quit and relaunch it from a Terminal "
                        "with: cswap --menubar",
                    )
                    return
                except ClaudeSwitchError as e:
                    notify.notify("claude-swap", str(e))
                    return
                self.refresh_async(full=True)

            self._offload(work)

        def on_refresh_now(self, _sender):
            # Force past the 15s usage-cache TTL so the click fetches fresh data
            # immediately; if a worker is mid-fetch the force is queued (3.1).
            self.refresh_async(full=True, force=True)

        def on_toggle_name(self, _sender):
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def _make_title_pct(self, mode):
            def cb(_sender):
                self.settings.title_pct = mode
                self._save_and_rebuild()
            return cb

        def _make_interval(self, secs):
            def cb(_sender):
                self.settings.refresh_interval = secs
                # rumps 0.4.0's Timer.interval setter is a no-op while running
                # unless a full interval has elapsed; stop/start forces the new
                # cadence to take effect immediately.
                self.refresh_timer.stop()
                self.refresh_timer.interval = secs
                self.refresh_timer.start()
                self._save_and_rebuild()
            return cb

        def on_toggle_autoswitch(self, _sender):
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self._last_auto_eval = 0.0  # let it evaluate on the next tick when enabling
            self._save_and_rebuild()

        def _make_strategy(self, name):
            def cb(_sender):
                self.settings.auto_switch_strategy = name
                self._last_auto_eval = 0.0  # re-evaluate promptly on change
                self._save_and_rebuild()
            return cb

        def _make_threshold(self, pct):
            def cb(_sender):
                self.settings.auto_switch_threshold = pct
                self._save_and_rebuild()
            return cb

        def _make_cooldown(self, secs):
            def cb(_sender):
                self.settings.auto_switch_cooldown = secs
                self._save_and_rebuild()
            return cb

        def _make_check(self, secs):
            def cb(_sender):
                self.settings.auto_switch_interval = secs
                self._last_auto_eval = 0.0
                self._save_and_rebuild()
            return cb

    MenuBarApp().run()
    return 0
