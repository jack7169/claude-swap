"""Detect running Claude Code instances.

Reads session PID files (~/.claude/sessions/{pid}.json) and IDE lockfiles
(~/.claude/ide/{port}.lock) to determine which Claude Code instances are
currently running. Uses the same mechanism Claude Code itself uses internally.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.paths import get_claude_config_home

logger = logging.getLogger(__name__)


@dataclass
class ClaudeSession:
    """A running Claude Code session from ~/.claude/sessions/{pid}.json."""

    pid: int
    session_id: str
    cwd: str
    started_at: int  # epoch milliseconds
    kind: str  # "interactive", "bg", "daemon", "daemon-worker"
    entrypoint: str  # "cli", "claude-vscode", "claude-desktop", "sdk-cli", "mcp"
    status: str | None = None  # "busy", "idle", "waiting"


@dataclass
class IdeInstance:
    """A running IDE instance from ~/.claude/ide/{port}.lock."""

    port: int  # from filename
    pid: int
    ide_name: str  # "Visual Studio Code", "Cursor", "Windsurf"
    workspace_folders: list[str] = field(default_factory=list)


def get_claude_dir() -> Path:
    """Return the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    return get_claude_config_home()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Cross-platform:
    - macOS/Linux/WSL: os.kill(pid, 0)
    - Windows: ctypes OpenProcess
    """
    if pid <= 1:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM means the process exists but we lack permission
        return True
    except OSError:
        return False


def _is_pid_alive_windows(pid: int) -> bool:
    """Windows-specific PID liveness check using ctypes."""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


# Allowance (seconds) for the gap between a process spawning and writing its
# session file, plus any clock skew, before we treat a later process start time
# as evidence of PID reuse rather than the original session.
_PID_REUSE_TOLERANCE_S = 120


def get_process_start_time(pid: int) -> float | None:
    """Return the process start time as epoch seconds, or None if unknown.

    Used to detect PID reuse: a recycled PID belongs to a process that started
    after the original one died. Only POSIX is supported via ``ps``; on other
    platforms (or when ``ps`` is unavailable) this returns None and callers fall
    back to a plain liveness check.
    """
    if sys.platform == "win32":
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            # Force a stable C locale so lstart's weekday/month names are always
            # English and the strptime parse below works regardless of the
            # caller's LC_TIME; without this a non-English locale silently
            # disables PID-reuse detection.
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            # Bound a wedged ps so this hot path can't hang.
            timeout=2,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.debug("Could not read start time for pid %s: %s", pid, exc)
        return None
    raw = proc.stdout.strip()
    if proc.returncode != 0 or not raw:
        return None
    try:
        # ps lstart format, e.g. "Mon Jun 29 19:35:56 2026" (local time).
        dt = datetime.datetime.strptime(raw, "%a %b %d %H:%M:%S %Y")
        return dt.timestamp()
    except (ValueError, OverflowError) as exc:
        logger.debug("Could not parse start time %r for pid %s: %s", raw, pid, exc)
        return None


def _pid_matches_started_at(pid: int, started_at_ms: int) -> bool:
    """Whether the live PID plausibly belongs to the session that wrote the file.

    Detects PID reuse: if the running process started well after the session
    file's recorded ``startedAt``, the PID was recycled by an unrelated process
    and the original session is dead. Conservative -- when the process start
    time can't be determined or no ``startedAt`` was recorded, it returns True so
    real sessions are never dropped.
    """
    if not started_at_ms:
        return True
    proc_start = get_process_start_time(pid)
    if proc_start is None:
        return True
    return proc_start <= (started_at_ms / 1000) + _PID_REUSE_TOLERANCE_S


def list_sessions(claude_dir: Path | None = None) -> list[ClaudeSession]:
    """Read session PID files and return only those with alive processes."""
    sessions_dir = (claude_dir or get_claude_dir()) / "sessions"
    if not sessions_dir.is_dir():
        return []

    sessions = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data["pid"]
            if not is_pid_alive(pid):
                continue
            if not _pid_matches_started_at(pid, data.get("startedAt", 0)):
                logger.debug(
                    "Skipping session file %s: pid %s appears reused", path, pid
                )
                continue
            sessions.append(ClaudeSession(
                pid=pid,
                session_id=data.get("sessionId", ""),
                cwd=data.get("cwd", ""),
                started_at=data.get("startedAt", 0),
                kind=data.get("kind", ""),
                entrypoint=data.get("entrypoint", ""),
                status=data.get("status"),
            ))
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.debug("Skipping session file %s: %s", path, exc)
    return sessions


def list_ide_instances(claude_dir: Path | None = None) -> list[IdeInstance]:
    """Read IDE lockfiles and return only those with alive processes."""
    ide_dir = (claude_dir or get_claude_dir()) / "ide"
    if not ide_dir.is_dir():
        return []

    instances = []
    for path in ide_dir.glob("*.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid is None or not is_pid_alive(pid):
                continue
            port = int(path.stem)
            instances.append(IdeInstance(
                port=port,
                pid=pid,
                ide_name=data.get("ideName", "Unknown IDE"),
                workspace_folders=data.get("workspaceFolders", []),
            ))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            logger.debug("Skipping IDE lockfile %s: %s", path, exc)
    return instances


def get_running_instances(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[IdeInstance]]:
    """Return all running Claude Code sessions and IDE instances."""
    resolved = claude_dir or get_claude_dir()
    return list_sessions(resolved), list_ide_instances(resolved)
