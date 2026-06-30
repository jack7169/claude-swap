"""Permission-hardening tests for Phase 4.4 (dirs/log) and 4.5 (notify).

Every test stays inside the conftest fixtures (redirected ``$HOME``, in-memory
Keychain/keyring fakes) and never spawns a real ``osascript`` / ``security`` /
log-shipper: ``subprocess.run`` is patched where the darwin notify path is
exercised, and the credential / logging paths only touch the temp filesystem.

The hardening guarantees, restated:
  * the per-account credentials directory (``.enc`` filenames embed account
    emails) is created owner-only (0700) — not world-readable;
  * the log directory is owner-only (0700) and ``claude-swap.log`` is 0600 —
    the log can carry debug detail, so it stays owner-only;
  * ``notify()`` pins the absolute ``/usr/bin/osascript`` so a PATH-injected
    ``osascript`` can't intercept the call.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from claude_swap import notify
from claude_swap.credentials import CredentialStore
from claude_swap.logging_config import setup_logging
from claude_swap.models import Platform


class _Host:
    """Minimal data-only ``_StoreHost`` view for the credential store.

    The store reads ``platform`` / ``credentials_dir`` / ``_logger`` at call
    time; nothing else is needed for ``_write_backup_enc``.
    """

    def __init__(self, credentials_dir: Path):
        self.platform = Platform.MACOS
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("claude-swap-test-perms")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- 4.4 credentials directory -------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_backup_enc_creates_credentials_dir_0700(tmp_path: Path):
    """``_write_backup_enc`` must create ``credentials_dir`` owner-only (0700).

    The directory holds ``.creds-<n>-<email>.enc`` files whose names embed
    account emails; a world-readable dir would leak that.
    """
    creds_dir = tmp_path / "backup" / "credentials"
    assert not creds_dir.exists()
    store = CredentialStore(_Host(creds_dir))

    store._write_backup_enc("2", "user@example.com", "secret-token")

    assert creds_dir.is_dir()
    assert _mode(creds_dir) == 0o700
    # The .enc itself stays 0600 (existing behavior, asserted as a guard).
    enc = store._backup_enc_path("2", "user@example.com")
    assert enc.exists()
    assert _mode(enc) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_backup_enc_tightens_preexisting_world_readable_dir(tmp_path: Path):
    """An explicit chmod is required: mkdir's mode is masked by umask, so a dir
    that already exists (or was created under a permissive umask) must still end
    up 0700 after the write."""
    creds_dir = tmp_path / "backup" / "credentials"
    creds_dir.mkdir(parents=True)
    os.chmod(creds_dir, 0o755)  # simulate a world-readable pre-existing dir
    store = CredentialStore(_Host(creds_dir))

    store._write_backup_enc("1", "a@b.co", "tok")

    assert _mode(creds_dir) == 0o700


# -- 4.4 log dir + log file ----------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_log_dir_0700_and_log_file_0600_after_emit(tmp_path: Path):
    """After a record is actually written, the lazily-created log dir is 0700
    and ``claude-swap.log`` is 0600."""
    log_dir = tmp_path / "logs"
    logger = setup_logging(log_dir)
    try:
        assert not log_dir.exists()  # lazy: nothing yet
        logger.warning("trigger an emit so the handler opens the file")
        for handler in logger.handlers:
            handler.flush()

        assert log_dir.is_dir()
        assert _mode(log_dir) == 0o700

        log_file = log_dir / "claude-swap.log"
        assert log_file.exists()
        assert _mode(log_file) == 0o600
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_log_dir_tightened_when_preexisting_world_readable(tmp_path: Path):
    """If the log dir already exists world-readable, the first emit still
    tightens it to 0700 (explicit chmod, not umask-masked mkdir)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    os.chmod(log_dir, 0o755)
    logger = setup_logging(log_dir)
    try:
        logger.warning("trigger")
        for handler in logger.handlers:
            handler.flush()
        assert _mode(log_dir) == 0o700
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


# -- 4.5 notify pins absolute osascript ---------------------------------------

def test_notify_uses_absolute_osascript_path(monkeypatch):
    """``notify()`` must invoke the absolute ``/usr/bin/osascript`` so a
    PATH-injected ``osascript`` can't intercept the call."""
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    captured = []
    monkeypatch.setattr(
        notify.subprocess, "run", lambda *a, **k: captured.append((a, k))
    )

    notify.notify("claude-swap", "hi")

    assert len(captured) == 1
    argv = captured[0][0][0]
    assert argv[0] == "/usr/bin/osascript"
    assert os.path.isabs(argv[0])
    # The remaining argv shape is unchanged.
    assert argv[1] == "-e"
    assert argv[2] == notify._build_notification_script("claude-swap", "hi")


def test_notify_osascript_module_constant_is_absolute():
    """The pinned path lives in a module constant (mirrors
    ``macos_keychain._SECURITY``)."""
    osascript = getattr(notify, "_OSASCRIPT", None)
    assert osascript == "/usr/bin/osascript"
    assert os.path.isabs(osascript)


def test_notify_still_noops_off_darwin(monkeypatch):
    """The PATH-pinning change must not disturb the off-macOS no-op."""
    monkeypatch.setattr(notify.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: called.append(a))
    notify.notify("t", "m")
    assert called == []


def test_notify_still_never_raises(monkeypatch):
    """The broad-except no-op-on-failure behavior is preserved."""
    monkeypatch.setattr(notify.sys, "platform", "darwin")

    def boom(*a, **k):
        raise OSError("osascript missing")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    notify.notify("t", "m")  # must not raise
