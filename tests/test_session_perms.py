"""Phase 4.3: session profile credential/config writes must be 0600 from birth.

The session bootstrap seeds a profile's plaintext ``.credentials.json`` and
``.claude.json`` (OAuth refresh token, identity). These previously used
``write_text`` (umask perms, typically 0644) followed by ``os.chmod 0600`` —
a brief world/group-readable window. These tests assert the bytes are *never*
written at umask perms: the files exist at 0600 immediately after ``_bootstrap``
*even if the belt-and-suspenders post-write chmod is neutered*, proving the file
was created restrictively (mkstemp / os.open O_CREAT 0o600), not chmod'd after.

Follows the existing test_session.py patterns: macOS platform forced, refresh
network call and the auth-status subprocess probe stubbed, ``_bootstrap`` driven
via the switcher API — nothing real is spawned (conftest fakes Keychain/$HOME).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from claude_swap import session as session_mod
from claude_swap.models import Platform
from claude_swap.session import SessionManager, session_dir_for

ACCOUNT_EMAIL = "account2@example.com"
ACCOUNT_NUM = "2"
ORG_UUID = "org-uuid-2"

CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "stored-access",
            "refreshToken": "stored-refresh",
            "expiresAt": 1,
        }
    }
)
CONFIG = json.dumps(
    {
        "oauthAccount": {
            "emailAddress": ACCOUNT_EMAIL,
            "accountUuid": "uuid-2",
            "organizationUuid": ORG_UUID,
        },
        "theme": "light",
    }
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file permissions"
)


@pytest.fixture
def macos_platform(monkeypatch):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))


@pytest.fixture
def seeded_switcher(temp_home: Path, macos_platform):
    """A switcher with account 2 fully backed up (creds + config + sequence)."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher(debug=True)
    switcher._setup_directories()
    switcher._write_json(
        switcher.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {
                    "email": "account1@example.com",
                    "uuid": "uuid-1",
                    "organizationUuid": "org-uuid-1",
                    "organizationName": "Org One",
                    "added": "2024-01-01T00:00:00Z",
                },
                ACCOUNT_NUM: {
                    "email": ACCOUNT_EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG_UUID,
                    "organizationName": "Org Two",
                    "added": "2024-01-02T00:00:00Z",
                },
            },
        },
    )
    switcher._write_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL, CREDS)
    switcher._write_account_config(ACCOUNT_NUM, ACCOUNT_EMAIL, CONFIG)
    return switcher


@pytest.fixture
def manager(seeded_switcher) -> SessionManager:
    return SessionManager(seeded_switcher)


@pytest.fixture
def no_refresh(monkeypatch):
    """Skip the token refresh network call: stored creds are used as-is."""
    monkeypatch.setattr(session_mod, "refresh_oauth_credentials", lambda c: None)


@pytest.fixture
def no_auth_probe(monkeypatch):
    """Avoid spawning `claude auth status`; report the profile as logged in.

    Drives ``_is_session_valid`` to True post-bootstrap so setup_session won't
    cleanup the profile, leaving the seeded files in place for perm assertions.
    """

    from types import SimpleNamespace

    def fake_run(cmd, env=None, **kwargs):
        config_dir = Path(env["CLAUDE_CONFIG_DIR"])
        logged_in = (config_dir / ".credentials.json").exists()
        payload = {
            "loggedIn": logged_in,
            "authMethod": "claude.ai" if logged_in else "none",
            "email": ACCOUNT_EMAIL,
            "orgId": ORG_UUID,
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(session_mod.subprocess, "run", fake_run)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestBootstrapPerms:
    def test_seeded_files_are_0600_and_dir_0700(
        self, manager, no_refresh, no_auth_probe
    ):
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        manager._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)

        assert _mode(session_dir) == 0o700
        assert _mode(session_dir / ".credentials.json") == 0o600
        assert _mode(session_dir / ".claude.json") == 0o600

    def test_files_created_restrictively_without_post_chmod(
        self, manager, no_refresh, no_auth_probe, monkeypatch
    ):
        """The credential/config files must be 0600 from birth, not via a
        trailing chmod.

        Neuter ``os.chmod`` for the two seeded files (keep it working for the
        session dir, which is a separate hardening) and assert the plaintext
        files are still 0600 — i.e. they were created with restrictive perms,
        closing the umask-then-chmod readable window. With the old
        ``write_text`` + chmod approach this fails (the file is born at umask).
        """
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        creds_path = session_dir / ".credentials.json"
        config_path = session_dir / ".claude.json"
        neutered = {str(creds_path), str(config_path)}
        real_chmod = os.chmod

        def selective_chmod(path, mode, *a, **k):
            if str(path) in neutered:
                return  # drop the belt-and-suspenders chmod for these files
            return real_chmod(path, mode, *a, **k)

        monkeypatch.setattr(session_mod.os, "chmod", selective_chmod)

        manager._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)

        assert _mode(creds_path) == 0o600
        assert _mode(config_path) == 0o600

    def test_no_temp_files_left_behind(self, manager, no_refresh, no_auth_probe):
        """The atomic writer must not leak its temp file on the success path."""
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        manager._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)

        entries = {p.name for p in session_dir.iterdir()}
        assert entries == {".credentials.json", ".claude.json"}

    def test_content_intact_after_restrictive_write(
        self, manager, no_refresh, no_auth_probe
    ):
        """Closing the perm window must not corrupt the seeded payloads."""
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        manager._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)

        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == CREDS
        config = json.loads((session_dir / ".claude.json").read_text(encoding="utf-8"))
        assert config["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL
        assert config["hasCompletedOnboarding"] is True
        assert config["theme"] == "light"

    def test_rebootstrap_preserves_history_and_perms(
        self, manager, no_refresh, no_auth_probe, monkeypatch
    ):
        """A re-bootstrap over an existing .claude.json must merge the identity
        seed (preserving the profile's own projects) AND still land at 0600
        from birth, even with the post-write chmod neutered for that file."""
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        manager._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)
        config_path = session_dir / ".claude.json"
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        existing["projects"] = {"/p": {"history": ["x"]}}
        config_path.write_text(json.dumps(existing), encoding="utf-8")
        (session_dir / ".credentials.json").unlink()

        real_chmod = os.chmod

        def selective_chmod(path, mode, *a, **k):
            if str(path) == str(config_path):
                return
            return real_chmod(path, mode, *a, **k)

        monkeypatch.setattr(session_mod.os, "chmod", selective_chmod)

        manager._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)

        merged = json.loads(config_path.read_text(encoding="utf-8"))
        assert merged["projects"] == {"/p": {"history": ["x"]}}
        assert merged["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL
        assert _mode(config_path) == 0o600
