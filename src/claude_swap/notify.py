"""Unified macOS swap notifications via ``osascript``.

Unlike ``rumps.notification`` — which only posts from inside the running menu
bar app — ``osascript`` posts a Notification Center alert from *any* process:
the ``cswap`` CLI, the LaunchAgent menu app, and the auto-switcher all route
through here. No-op off macOS; never raises (a notification failure must never
break a switch).
"""

from __future__ import annotations

import logging
import subprocess
import sys

from claude_swap import spawn

_logger = logging.getLogger("claude-swap")

# Pin the absolute path (mirrors macos_keychain._SECURITY) so a PATH-injected
# ``osascript`` can't intercept the notification call.
_OSASCRIPT = "/usr/bin/osascript"


def _sanitize(value: str) -> str:
    """Collapse all whitespace runs (newlines, tabs, CRs) to single spaces.

    osascript accepts a raw newline inside an AppleScript string literal and
    Notification Center stacks it onto a separate line, so a stray ``\\n`` — e.g.
    an account email/identity parsed with a trailing newline — renders the alert
    as broken multiple lines. These are single-line alerts, so flatten first.
    """
    return " ".join(value.split())


def _escape(value: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal.

    Backslash must be doubled before quotes are escaped, otherwise the
    backslash introduced by quote-escaping would itself be doubled.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_notification_script(title: str, message: str) -> str:
    """Build the AppleScript line that posts a notification (sanitize + escape)."""
    return (
        f'display notification "{_escape(_sanitize(message))}" '
        f'with title "{_escape(_sanitize(title))}"'
    )


def wire_switch_notifier(switcher) -> None:
    """Register the standard swap notifier on ``switcher`` (macOS only).

    Shared by ``cli.main`` and the bundled app's ``app_main.main`` so a switch
    from ANY entry point — CLI command, menu-bar click, or auto-switch — posts
    the same alert. The bundled ``.app`` bypasses the CLI, so it must call this
    too or its switches would fire ``_announce_switch`` against a ``None``
    notifier and stay silent. No-op off macOS (osascript is mac-only).
    """
    if sys.platform != "darwin":
        return
    switcher.set_switch_notifier(
        lambda num, email: notify(
            "claude-swap",
            f"Switched to Account-{num} ({email}) — restart Claude Code "
            "to apply (active within ~30s).",
        )
    )


def notify(title: str, message: str) -> None:
    """Post a macOS notification. No-op off macOS; never raises."""
    if sys.platform != "darwin":
        return
    try:
        # Serialize with the other menu-bar spawns: an osascript notification
        # fired during auto-switch must not fork() concurrently with a refresh
        # worker (malloc atfork livelock). See claude_swap.spawn.
        with spawn.fork_lock:
            subprocess.run(
                [_OSASCRIPT, "-e", _build_notification_script(title, message)],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        _logger.debug("notification failed", exc_info=True)
