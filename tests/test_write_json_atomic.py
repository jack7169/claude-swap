"""Atomicity / permission tests for ClaudeAccountSwitcher._write_json.

Phase 4.1: _write_json must mirror the mkstemp reference pattern used in
credentials.py (mkstemp -> os.write -> os.close -> os.replace -> chmod 0600,
unlinking the temp on any failure). The file holding ``oauthAccount`` etc. must
never have a world/group-readable window, and a failed write must neither leave
a leftover ``.tmp`` file nor corrupt an existing target.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.switcher import ClaudeAccountSwitcher


def _tmp_leftovers(directory: Path) -> list[Path]:
    """Any leftover temp files mkstemp could have created in ``directory``."""
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


class TestWriteJsonAtomic:
    """_write_json writes atomically with no world-readable window."""

    def test_round_trip(self, temp_home: Path):
        """Data written by _write_json reads back identically."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        path = switcher.backup_dir / "round_trip.json"
        data = {"key": "value", "number": 42, "nested": {"a": [1, 2, 3]}}

        switcher._write_json(path, data)

        assert switcher._read_json(path) == data

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX file permissions only"
    )
    def test_file_is_0600_immediately(self, temp_home: Path):
        """The written file is mode 0600 the instant _write_json returns.

        mkstemp creates the fd at 0600 atomically and os.replace inherits it, so
        there is never a window where ~/.claude.json is world/group-readable.
        """
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        path = switcher.backup_dir / "secure.json"
        switcher._write_json(path, {"oauthAccount": {"emailAddress": "x@y.z"}})

        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_failed_replace_leaves_no_temp_and_no_corruption(
        self, temp_home: Path, monkeypatch
    ):
        """A failing os.replace unlinks the temp and leaves the target intact."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        path = switcher.backup_dir / "target.json"
        original = {"existing": "data", "n": 1}
        switcher._write_json(path, original)

        def boom(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", boom)

        with pytest.raises(OSError):
            switcher._write_json(path, {"new": "payload"})

        # No leftover temp file in the directory.
        assert _tmp_leftovers(switcher.backup_dir) == []
        # The pre-existing target was not corrupted or truncated.
        assert switcher._read_json(path) == original

    def test_no_temp_remaining_after_success(self, temp_home: Path):
        """A successful write leaves no ``.tmp`` files behind."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        path = switcher.backup_dir / "clean.json"
        switcher._write_json(path, {"a": 1})

        assert _tmp_leftovers(switcher.backup_dir) == []
        assert path.exists()

    def test_unserializable_raises_config_error_no_temp(self, temp_home: Path):
        """Serialization failure raises ConfigError and leaves no temp file."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        path = switcher.backup_dir / "bad.json"

        # An object json.dumps cannot serialize must surface as ConfigError
        # (preserving the old "Generated invalid JSON" guard semantics) without
        # creating the target or leaving a stray temp file.
        with pytest.raises(ConfigError):
            switcher._write_json(path, {"bad": object()})

        assert not path.exists()
        assert _tmp_leftovers(switcher.backup_dir) == []
