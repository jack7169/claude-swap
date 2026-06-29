"""Tests for Claude Code process detection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.process_detection import (
    ClaudeSession,
    IdeInstance,
    get_claude_dir,
    get_process_start_time,
    get_running_instances,
    is_pid_alive,
    list_ide_instances,
    list_sessions,
)
from claude_swap.printer import abbreviate_path, entrypoint_label, format_age


# --- get_claude_dir ---


class TestGetClaudeDir:
    def test_default_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            result = get_claude_dir()
            assert result == Path.home() / ".claude"

    def test_respects_env_var(self, tmp_path):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
            assert get_claude_dir() == tmp_path


# --- is_pid_alive ---


class TestIsPidAlive:
    def test_alive_pid(self):
        with patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            assert is_pid_alive(12345) is True
            mock_kill.assert_called_once_with(12345, 0)

    def test_dead_pid(self):
        with patch("os.kill", side_effect=OSError("No such process")):
            assert is_pid_alive(12345) is False

    def test_permission_error_means_alive(self):
        with patch("os.kill", side_effect=PermissionError("Operation not permitted")):
            assert is_pid_alive(12345) is True

    def test_invalid_pid_zero(self):
        assert is_pid_alive(0) is False

    def test_invalid_pid_one(self):
        assert is_pid_alive(1) is False

    def test_negative_pid(self):
        assert is_pid_alive(-1) is False


# --- get_process_start_time ---


class TestGetProcessStartTime:
    def test_returns_none_when_ps_fails(self):
        completed = type("P", (), {"returncode": 1, "stdout": ""})()
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.subprocess.run", return_value=completed
        ):
            assert get_process_start_time(99999) is None

    def test_returns_none_on_windows(self):
        with patch("claude_swap.process_detection.sys.platform", "win32"):
            assert get_process_start_time(1234) is None

    def test_returns_none_on_unparseable_output(self):
        completed = type("P", (), {"returncode": 0, "stdout": "garbage output"})()
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.subprocess.run", return_value=completed
        ):
            assert get_process_start_time(1234) is None

    def test_parses_lstart(self):
        completed = type(
            "P", (), {"returncode": 0, "stdout": "Mon Jun 29 19:35:56 2026   \n"}
        )()
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.subprocess.run", return_value=completed
        ):
            result = get_process_start_time(1234)
        assert result is not None
        assert result == pytest.approx(
            time.mktime(time.strptime("Mon Jun 29 19:35:56 2026", "%a %b %d %H:%M:%S %Y"))
        )


# --- list_sessions ---


def _write_session(sessions_dir: Path, pid: int, **overrides) -> Path:
    """Write a session PID file with sensible defaults."""
    data = {
        "pid": pid,
        "sessionId": f"session-{pid}",
        "cwd": "/home/user/project",
        "startedAt": int(time.time() * 1000),
        "kind": "interactive",
        "entrypoint": "cli",
    }
    data.update(overrides)
    path = sessions_dir / f"{pid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestListSessions:
    def test_reads_valid_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, entrypoint="cli", cwd="/home/user/app")
        _write_session(sessions_dir, 1002, entrypoint="claude-vscode", cwd="/home/user/web")

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert len(result) == 2
        pids = {s.pid for s in result}
        assert pids == {1001, 1002}

    def test_filters_dead_pids(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)
        _write_session(sessions_dir, 1002)

        def alive(pid):
            return pid == 1001

        with patch("claude_swap.process_detection.is_pid_alive", side_effect=alive):
            result = list_sessions(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 1001

    def test_missing_sessions_dir(self, tmp_path):
        assert list_sessions(tmp_path) == []

    def test_corrupt_json(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "9999.json").write_text("not json{{{", encoding="utf-8")

        result = list_sessions(tmp_path)
        assert result == []

    def test_missing_pid_field(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "9999.json").write_text(
            json.dumps({"sessionId": "abc", "cwd": "/tmp"}), encoding="utf-8"
        )

        result = list_sessions(tmp_path)
        assert result == []

    def test_optional_status_field(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, status="busy")

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert result[0].status == "busy"

    def test_status_absent(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert result[0].status is None

    def test_session_fields(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(
            sessions_dir, 5000,
            sessionId="sess-abc",
            cwd="/projects/foo",
            startedAt=1700000000000,
            kind="bg",
            entrypoint="claude-desktop",
        )

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        s = result[0]
        assert s.pid == 5000
        assert s.session_id == "sess-abc"
        assert s.cwd == "/projects/foo"
        assert s.started_at == 1700000000000
        assert s.kind == "bg"
        assert s.entrypoint == "claude-desktop"

    def test_reused_pid_is_not_live(self, tmp_path):
        """A stale session file whose PID was recycled by a newer process must
        not be reported as live (PID reuse: the running process started well
        after the recorded startedAt)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        started_ms = 1_700_000_000_000  # the original (dead) session's start
        _write_session(sessions_dir, 4242, startedAt=started_ms)

        # PID is alive, but the live process started long after startedAt,
        # i.e. the PID was reused by an unrelated process.
        process_start = started_ms / 1000 + 3600  # one hour later
        with patch("claude_swap.process_detection.is_pid_alive", return_value=True), patch(
            "claude_swap.process_detection.get_process_start_time",
            return_value=process_start,
        ):
            result = list_sessions(tmp_path)

        assert result == []

    def test_original_pid_within_tolerance_is_live(self, tmp_path):
        """A genuine session whose process start time matches startedAt (within
        tolerance) must still be reported as live."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        started_ms = 1_700_000_000_000
        _write_session(sessions_dir, 4243, startedAt=started_ms)

        # Process started just after the recorded startedAt, within tolerance.
        process_start = started_ms / 1000 + 1
        with patch("claude_swap.process_detection.is_pid_alive", return_value=True), patch(
            "claude_swap.process_detection.get_process_start_time",
            return_value=process_start,
        ):
            result = list_sessions(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 4243

    def test_unknown_process_start_time_keeps_session(self, tmp_path):
        """When the process start time cannot be determined, fall back to the
        plain liveness check and keep the session (never drop a real one)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 4244, startedAt=1_700_000_000_000)

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True), patch(
            "claude_swap.process_detection.get_process_start_time",
            return_value=None,
        ):
            result = list_sessions(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 4244


# --- list_ide_instances ---


def _write_ide_lock(ide_dir: Path, port: int, **overrides) -> Path:
    """Write an IDE lockfile with sensible defaults."""
    data = {
        "pid": port + 1000,
        "workspaceFolders": ["/home/user/project"],
        "ideName": "Visual Studio Code",
        "transport": "ws",
    }
    data.update(overrides)
    path = ide_dir / f"{port}.lock"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestListIdeInstances:
    def test_reads_valid_lockfiles(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, ideName="Visual Studio Code")
        _write_ide_lock(ide_dir, 45001, ideName="Cursor")

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert len(result) == 2
        names = {i.ide_name for i in result}
        assert names == {"Visual Studio Code", "Cursor"}

    def test_filters_dead_pids(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, pid=2001)
        _write_ide_lock(ide_dir, 45001, pid=2002)

        with patch("claude_swap.process_detection.is_pid_alive", side_effect=lambda p: p == 2001):
            result = list_ide_instances(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 2001

    def test_missing_ide_dir(self, tmp_path):
        assert list_ide_instances(tmp_path) == []

    def test_corrupt_json(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        (ide_dir / "9999.lock").write_text("broken", encoding="utf-8")

        assert list_ide_instances(tmp_path) == []

    def test_missing_pid_field(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        (ide_dir / "9999.lock").write_text(
            json.dumps({"ideName": "VS Code"}), encoding="utf-8"
        )

        assert list_ide_instances(tmp_path) == []

    def test_port_from_filename(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 12345)

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert result[0].port == 12345

    def test_workspace_folders(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, workspaceFolders=["/a", "/b"])

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert result[0].workspace_folders == ["/a", "/b"]


# --- get_running_instances ---


class TestGetRunningInstances:
    def test_returns_both(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)

        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000)

        with patch("claude_swap.process_detection.is_pid_alive", return_value=True):
            sessions, ides = get_running_instances(tmp_path)

        assert len(sessions) == 1
        assert len(ides) == 1

    def test_empty_when_no_dirs(self, tmp_path):
        sessions, ides = get_running_instances(tmp_path)
        assert sessions == []
        assert ides == []


# --- Display helpers (in switcher.py) ---


class TestEntrypointLabel:
    @pytest.mark.parametrize(
        "entrypoint,expected",
        [
            ("cli", "CLI"),
            ("claude-vscode", "VS Code"),
            ("claude-desktop", "Desktop"),
            ("sdk-cli", "SDK"),
            ("mcp", "MCP"),
            ("unknown-thing", "unknown-thing"),
        ],
    )
    def test_known_and_unknown(self, entrypoint, expected):
        assert entrypoint_label(entrypoint) == expected


class TestAbbreviatePath:
    def test_replaces_home(self):
        home = str(Path.home())
        assert abbreviate_path(f"{home}/projects/foo") == "~/projects/foo"

    def test_non_home_path_unchanged(self):
        assert abbreviate_path("/opt/data/bar") == "/opt/data/bar"

    def test_home_root(self):
        home = str(Path.home())
        assert abbreviate_path(home) == "~"


class TestFormatAge:
    def test_just_now(self):
        now_ms = int(time.time() * 1000)
        assert format_age(now_ms) == "just now"

    def test_minutes(self):
        ms = int((time.time() - 300) * 1000)  # 5 minutes ago
        assert format_age(ms) == "5m ago"

    def test_hours(self):
        ms = int((time.time() - 7200) * 1000)  # 2 hours ago
        assert format_age(ms) == "2h ago"

    def test_days(self):
        ms = int((time.time() - 172800) * 1000)  # 2 days ago
        assert format_age(ms) == "2d ago"
