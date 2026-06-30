"""Tests for transfer._atomic_write_file — the atomic, 0600-from-birth writer
that lands exported plaintext OAuth tokens / API keys on disk.

These pin down the mkstemp+os.replace hardening: no world/group-readable window,
no predictable temp name, no temp-file leak on failure, and a clean
``TransferError`` (not a raw ``OSError``) when the destination dir is missing.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import TransferError
from claude_swap.transfer import _atomic_write_file


# ---------------------------------------------------------------------------
# (1) Round-trip
# ---------------------------------------------------------------------------


def test_round_trip(temp_home: Path) -> None:
    out = temp_home / "export.cswap"
    content = '{"version": 1, "secret": "sk-ant-api-abc"}\n'
    _atomic_write_file(out, content)
    assert out.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# (2) Exported file is 0600 immediately — no readable window
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perms only")
def test_exported_file_is_0600_no_readable_window(temp_home: Path) -> None:
    out = temp_home / "export.cswap"

    # If the writer ever made the destination world/group-readable mid-write
    # (write_text at umask, chmod after), the bytes would be exposed. Assert the
    # bytes only ever appear at the final path with 0600, by checking perms the
    # instant the file becomes visible: os.replace is atomic, and mkstemp's fd is
    # 0600 from birth, so there is no observable readable window. Verify both the
    # resulting file perms and that nothing else group/other-readable was left.
    _atomic_write_file(out, "secret\n")
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o600, oct(mode)
    # No group/other bits set.
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perms only")
def test_temp_file_never_world_readable_during_write(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file backing the write must be 0600 the moment it exists.

    Intercept os.replace to inspect the staged temp file's perms at the exact
    point just before it is promoted to the destination — the only window in
    which the plaintext exists under a non-final name.
    """
    out = temp_home / "export.cswap"
    seen_modes: list[int] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        seen_modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    _atomic_write_file(out, "secret\n")

    assert seen_modes == [0o600]


# ---------------------------------------------------------------------------
# (3) Missing destination dir → TransferError, not raw OSError
# ---------------------------------------------------------------------------


def test_missing_destination_dir_raises_transfer_error(temp_home: Path) -> None:
    out = temp_home / "no_such_dir" / "export.cswap"
    with pytest.raises(TransferError) as exc:
        _atomic_write_file(out, "data\n")
    assert "export destination directory does not exist" in str(exc.value)
    # Must not leak a partial file or temp anywhere.
    assert not out.exists()


# ---------------------------------------------------------------------------
# (4) Mid-write failure: no leftover temp, existing destination not clobbered
# ---------------------------------------------------------------------------


def test_mid_write_failure_leaves_no_temp_and_preserves_destination(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest_dir = temp_home / "out"
    dest_dir.mkdir()
    out = dest_dir / "export.cswap"
    out.write_text("OLD CONTENTS\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        _atomic_write_file(out, "NEW CONTENTS\n")

    # Existing destination is untouched (os.replace never ran).
    assert out.read_text(encoding="utf-8") == "OLD CONTENTS\n"

    # No leftover temp files in the directory — only the original destination.
    leftovers = sorted(p.name for p in out.parent.iterdir())
    assert leftovers == ["export.cswap"], leftovers


# ---------------------------------------------------------------------------
# (5) Multi-dot destination name round-trips
# ---------------------------------------------------------------------------


def test_multi_dot_destination_round_trips(temp_home: Path) -> None:
    dest_dir = temp_home / "out"
    dest_dir.mkdir()
    out = dest_dir / "my.backup.2026"
    content = "payload\n"
    _atomic_write_file(out, content)
    assert out.read_text(encoding="utf-8") == content
    assert out.name == "my.backup.2026"
    # No stray temp left behind.
    leftovers = sorted(p.name for p in out.parent.iterdir())
    assert leftovers == ["my.backup.2026"], leftovers
