"""Tests for the OAuth↔API-key mutual-exclusion guarantee in ``_write_credentials``.

Activating one auth axis must leave *exactly one* axis live. The historical bug
(Phase 1.2) was a dual-active window: the new axis was written first, then the old
axis cleared as a separate best-effort step — a crash in between left both an OAuth
credential and a managed ``primaryApiKey`` active, and Claude Code silently honored
the wrong one. The fix:

- **clear-old-FIRST**, then write the new axis, so the worst residual mid-operation
  is "old account still active" (fail-safe) rather than "new + old both active"; and
- make the config-level clear of the loser axis (``primaryApiKey`` for the OAuth
  arm, ``.credentials.json`` for the API-key arm) **hard-fail** — raise
  ``CredentialWriteError`` so the switch rolls back rather than leaving two axes live.

Uses the in-memory Keychain fake (``block_real_keychain``) and a platform-stubbed
``ClaudeAccountSwitcher`` like the other credential tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
)
from claude_swap.exceptions import CredentialWriteError
from claude_swap.models import Platform
from claude_swap.paths import get_credentials_path, get_global_config_path
from claude_swap.switcher import ClaudeAccountSwitcher

API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4  # 53 chars
OAUTH_JSON = json.dumps(
    {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rtok", "expiresAt": 9}}
)


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _macos_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    return s


def _read_global_config() -> dict:
    path = get_global_config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (1) Success case: exactly one axis active after a switch, both directions.
# ---------------------------------------------------------------------------


class TestExactlyOneAxisLinux:
    def test_oauth_to_apikey_leaves_only_apikey(self, temp_home: Path):
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        s._write_credentials(API_KEY)

        # API-key axis live, OAuth axis gone — exactly one axis.
        cfg = _read_global_config()
        assert cfg["primaryApiKey"] == API_KEY
        assert not cred_file.exists()

    def test_apikey_to_oauth_leaves_only_oauth(self, temp_home: Path):
        s = _linux_switcher()
        # Start with the API key live.
        s._write_credentials(API_KEY)
        assert _read_global_config()["primaryApiKey"] == API_KEY

        s._write_credentials(OAUTH_JSON)

        # OAuth axis live, managed-key axis gone — exactly one axis.
        cred_file = get_credentials_path()
        assert cred_file.read_text(encoding="utf-8") == OAUTH_JSON
        assert "primaryApiKey" not in _read_global_config()


class TestExactlyOneAxisMacOS:
    def test_oauth_to_apikey_leaves_only_apikey(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        acct = macos_keychain.keychain_account_name()
        store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct, OAUTH_JSON)

        s._write_credentials(API_KEY)

        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == API_KEY
        assert store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct) is None
        assert "primaryApiKey" not in _read_global_config()

    def test_apikey_to_oauth_leaves_only_oauth(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        acct = macos_keychain.keychain_account_name()
        s._write_credentials(API_KEY)
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == API_KEY

        s._write_credentials(OAUTH_JSON)

        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) is None
        assert store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct) == OAUTH_JSON


# ---------------------------------------------------------------------------
# (2) Hard-fail: if the old-axis config clear fails, raise CredentialWriteError
#     and do NOT leave both axes active.
# ---------------------------------------------------------------------------


class TestOldAxisClearFailureRaises:
    def test_oauth_arm_clear_managed_key_failure_raises(self, temp_home: Path):
        """OAuth arm: a failure clearing ``primaryApiKey`` must raise and must not
        leave the new OAuth file written alongside the still-live managed key."""
        s = _linux_switcher()
        # Managed key is live (the loser axis we must clear before writing OAuth).
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        cred_file = get_credentials_path()
        assert not cred_file.exists()

        # Make the config-level clear blow up.
        def _boom(_mutator) -> None:
            raise OSError("disk full")

        s._store._update_global_config = _boom  # type: ignore[assignment]

        with pytest.raises(CredentialWriteError):
            s._write_credentials(OAUTH_JSON)

        # Fail-safe residual: old (managed key) axis still live, new OAuth NOT written.
        assert _read_global_config()["primaryApiKey"] == API_KEY
        assert not cred_file.exists()

    def test_apikey_arm_clear_oauth_file_failure_raises(self, temp_home: Path):
        """API-key arm: a failure removing ``.credentials.json`` must raise and must
        not commit the managed key alongside the still-present OAuth file."""
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        # Make the OAuth-file removal fail.
        real_unlink = Path.unlink

        def _boom_unlink(self_path: Path, *a, **k):
            if self_path == cred_file:
                raise OSError("permission denied")
            return real_unlink(self_path, *a, **k)

        import claude_swap.credentials as credmod

        # Patch at the Path level used inside _clear_oauth_credential.
        orig = credmod.Path.unlink
        credmod.Path.unlink = _boom_unlink  # type: ignore[assignment]
        try:
            with pytest.raises(CredentialWriteError):
                s._write_credentials(API_KEY)
        finally:
            credmod.Path.unlink = orig  # type: ignore[assignment]

        # Fail-safe residual: old (OAuth file) axis still present, managed key NOT live.
        assert cred_file.read_text(encoding="utf-8") == OAUTH_JSON
        assert "primaryApiKey" not in _read_global_config()


# ---------------------------------------------------------------------------
# (3) Ordering: the old axis is cleared BEFORE the new axis is written.
# ---------------------------------------------------------------------------


class TestClearBeforeWriteOrdering:
    def test_oauth_arm_clears_managed_key_first(self, temp_home: Path):
        s = _linux_switcher()
        calls: list[str] = []

        orig_clear = s._store._clear_managed_key
        orig_write = s._store._write_oauth_credentials

        def rec_clear() -> None:
            calls.append("clear_managed_key")
            orig_clear()

        def rec_write(creds: str) -> None:
            calls.append("write_oauth")
            orig_write(creds)

        s._store._clear_managed_key = rec_clear  # type: ignore[assignment]
        s._store._write_oauth_credentials = rec_write  # type: ignore[assignment]

        s._write_credentials(OAUTH_JSON)

        assert calls == ["clear_managed_key", "write_oauth"]

    def test_apikey_arm_clears_oauth_before_committing_key(self, temp_home: Path):
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        calls: list[str] = []

        orig_clear = s._store._clear_oauth_credential
        orig_update = s._store._update_global_config

        def rec_clear() -> None:
            calls.append("clear_oauth")
            orig_clear()

        def rec_update(mutator) -> None:
            calls.append("write_key_config")
            orig_update(mutator)

        s._store._clear_oauth_credential = rec_clear  # type: ignore[assignment]
        s._store._update_global_config = rec_update  # type: ignore[assignment]

        s._write_credentials(API_KEY)

        # The OAuth (loser) axis is cleared before the managed key is committed.
        assert calls.index("clear_oauth") < calls.index("write_key_config")
