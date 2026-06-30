"""Phase 10 fix 10.1 guard tests.

These are light guards confirming the dead/redundant-code removals in
credentials.py (function-local ``import tempfile`` in
``_write_active_credentials_file`` and ``_write_backup_enc``), models.py
(unused ``import json``), and tui.py (unused ``email`` unpack in
``_do_refresh``) did not break the touched code paths. The full suite carries
the real coverage; these just catch a ``NameError`` from an over-eager removal.

All tests stay inside the conftest fixtures (redirected ``$HOME``, in-memory
Keychain/keyring fakes) and only touch the temp filesystem.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path


class _Host:
    """Minimal data-only ``_StoreHost`` view for the credential store."""

    def __init__(self, credentials_dir: Path):
        from claude_swap.models import Platform

        self.platform = Platform.MACOS
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("claude-swap-test-phase10")


def test_modules_import_cleanly():
    """Each touched module imports without NameError from a removed symbol."""
    for name in (
        "claude_swap.credentials",
        "claude_swap.models",
        "claude_swap.tui",
    ):
        mod = importlib.import_module(name)
        assert mod is not None


def test_models_platform_detect_works():
    """models.py: exercise a function from the file (no ``json`` dependency)."""
    from claude_swap.models import Platform

    platform = Platform.detect()
    assert isinstance(platform, Platform)


def test_credentials_backup_enc_uses_module_tempfile(tmp_path: Path):
    """credentials.py: ``_write_backup_enc`` still works via the module-level
    ``tempfile`` import (the function-local import was removed)."""
    from claude_swap.credentials import CredentialStore

    creds_dir = tmp_path / "backup" / "credentials"
    store = CredentialStore(_Host(creds_dir))

    store._write_backup_enc("2", "user@example.com", "secret-token")

    enc = store._backup_enc_path("2", "user@example.com")
    assert enc.exists()


def test_credentials_active_write_uses_module_tempfile(tmp_path: Path, monkeypatch):
    """credentials.py: ``_write_active_credentials_file`` still works via the
    module-level ``tempfile`` import (the function-local import was removed)."""
    import claude_swap.credentials as credentials_mod
    from claude_swap.credentials import CredentialStore

    cred_dir = tmp_path / "claude_config"
    monkeypatch.setattr(credentials_mod, "get_claude_config_home", lambda: cred_dir)

    store = CredentialStore(_Host(tmp_path / "backup"))
    payload = '{"token": "abc"}'
    store._write_active_credentials_file(payload)

    written = (cred_dir / ".credentials.json").read_text(encoding="utf-8")
    assert written == payload


def test_tui_status_line_helper_runs():
    """tui.py: a pure helper still works after the unused-binding removal in
    ``_do_refresh`` (same module)."""
    from claude_swap import tui

    class _StubSwitcher:
        def _get_sequence_data(self):
            return {"accounts": {}}

        def _get_current_account(self):
            return None

    line = tui._status_line(_StubSwitcher())
    assert isinstance(line, str)
