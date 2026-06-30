"""Tests for robust WSL detection in Platform.detect (Phase 6.2).

Platform.detect previously relied solely on the WSL_DISTRO_NAME environment
variable, which is frequently unset (non-login shells, services, some
terminals), causing WSL to be misdetected as plain Linux. These tests pin down
the more robust kernel-identity probe (/proc/sys/kernel/osrelease and
/proc/version contain a "microsoft"/"wsl" marker under WSL) while keeping the
env var as a fast path and never crashing when /proc is unreadable.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from claude_swap import models
from claude_swap.models import Platform


def _force_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin sys.platform (as models reads it) to a Linux value."""
    monkeypatch.setattr(models.sys, "platform", "linux")


def _patch_proc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    osrelease: str | None = None,
    version: str | None = None,
) -> None:
    """Fake the /proc kernel-identity reads.

    A value of None for a given path simulates that file being missing or
    unreadable (the read raises OSError).
    """
    contents = {
        "/proc/sys/kernel/osrelease": osrelease,
        "/proc/version": version,
    }
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):  # noqa: ANN001
        key = str(file)
        if key in contents:
            value = contents[key]
            if value is None:
                raise OSError("simulated unreadable /proc file")
            import io

            return io.StringIO(value)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


class TestWslDetection:
    def test_env_var_set_detects_wsl(self, monkeypatch: pytest.MonkeyPatch):
        """WSL_DISTRO_NAME set -> WSL (fast path, no /proc needed)."""
        _force_linux(monkeypatch)
        monkeypatch.setitem(models.os.environ, "WSL_DISTRO_NAME", "Ubuntu")
        # /proc deliberately unreadable to prove the env fast path wins.
        _patch_proc(monkeypatch, osrelease=None, version=None)

        assert Platform.detect() is Platform.WSL

    def test_proc_version_microsoft_marker_detects_wsl(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Env unset, but /proc/version contains 'microsoft' -> WSL."""
        _force_linux(monkeypatch)
        monkeypatch.delitem(models.os.environ, "WSL_DISTRO_NAME", raising=False)
        _patch_proc(
            monkeypatch,
            osrelease="5.15.90.1-microsoft-standard-WSL2\n",
            version=(
                "Linux version 5.15.90.1-microsoft-standard-WSL2 "
                "(root@build) ... Microsoft ...\n"
            ),
        )

        assert Platform.detect() is Platform.WSL

    def test_osrelease_marker_only_detects_wsl(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """osrelease carries the marker even when /proc/version is missing."""
        _force_linux(monkeypatch)
        monkeypatch.delitem(models.os.environ, "WSL_DISTRO_NAME", raising=False)
        _patch_proc(
            monkeypatch,
            osrelease="5.15.90.1-microsoft-standard-WSL2\n",
            version=None,
        )

        assert Platform.detect() is Platform.WSL

    def test_wsl_marker_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The 'wsl' marker is matched case-insensitively."""
        _force_linux(monkeypatch)
        monkeypatch.delitem(models.os.environ, "WSL_DISTRO_NAME", raising=False)
        _patch_proc(
            monkeypatch,
            osrelease="6.6.0-WSL2-generic\n",
            version=None,
        )

        assert Platform.detect() is Platform.WSL

    def test_plain_linux_no_env_no_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Plain Linux (no env, no kernel marker) -> LINUX."""
        _force_linux(monkeypatch)
        monkeypatch.delitem(models.os.environ, "WSL_DISTRO_NAME", raising=False)
        _patch_proc(
            monkeypatch,
            osrelease="6.8.0-45-generic\n",
            version="Linux version 6.8.0-45-generic (buildd@lcy02) ...\n",
        )

        assert Platform.detect() is Platform.LINUX

    def test_unreadable_proc_falls_back_to_linux(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Missing/unreadable /proc with no env -> LINUX, never crashes."""
        _force_linux(monkeypatch)
        monkeypatch.delitem(models.os.environ, "WSL_DISTRO_NAME", raising=False)
        _patch_proc(monkeypatch, osrelease=None, version=None)

        assert Platform.detect() is Platform.LINUX

    def test_non_linux_platforms_unaffected(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The /proc probe only runs on the linux branch."""
        monkeypatch.setattr(models.sys, "platform", "darwin")
        assert Platform.detect() is Platform.MACOS

        monkeypatch.setattr(models.sys, "platform", "win32")
        assert Platform.detect() is Platform.WINDOWS
