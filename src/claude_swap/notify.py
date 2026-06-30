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


def notify(title: str, message: str) -> None:
    """Post a macOS notification. No-op off macOS; never raises."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [_OSASCRIPT, "-e", _build_notification_script(title, message)],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        _logger.debug("notification failed", exc_info=True)
