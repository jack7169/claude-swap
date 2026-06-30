"""Phase 7.6 — session re-bootstrap safety (F22, F21).

Two hardening guarantees for session mode:

* **F22** — `setup_session` must never re-seed credentials or `rmtree` a
  session profile out from under a LIVE (but auth-invalid) claude. The
  cheap reuse check fails when the live session's token is revoked/expired,
  which previously dropped straight into `_bootstrap` (re-seed + keychain
  delete) and, on validation failure, `_cleanup_failed_session` (rmtree) —
  pulling credentials/keychain/dir out from under the running process. The
  re-bootstrap path and `_cleanup_failed_session` must refuse with a
  `SessionError` telling the user to exit the running instance first
  (mirroring the switcher's live-session guard).

* **F21** — the "Launching… [session mode]" status line is buffered and is
  silently dropped by ``os.execvpe`` when stdout is not a TTY (exec replaces
  the process without flushing Python's buffers). `run` must flush stdout
  before handing off to `_exec`.

These drive the public `SessionManager` API with monkeypatched
`live_sessions_for` / `_exec` / `subprocess.run` (auth-status), exactly like
the existing session tests — never spawning a real claude.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import session as session_mod
from claude_swap.exceptions import SessionError
from claude_swap.models import Platform
from claude_swap.session import (
    SessionManager,
    session_dir_for,
)
from claude_swap.switcher import ClaudeAccountSwitcher

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


# ---------------------------------------------------------------------------
# fixtures (mirror tests/test_session.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def macos_platform(monkeypatch):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))


@pytest.fixture
def seeded_switcher(temp_home: Path, macos_platform) -> ClaudeAccountSwitcher:
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


def make_live_session(session_dir: Path) -> object:
    """A fake live ClaudeSession (only .pid is read by the guards)."""
    return SimpleNamespace(pid=4242)


# ---------------------------------------------------------------------------
# F22 — never re-seed / rmtree under a live session
# ---------------------------------------------------------------------------


class TestLiveSessionBootstrapGuard:
    def _seed_profile(self, manager) -> Path:
        """Create an on-disk profile (creds + config) as a prior bootstrap would."""
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(CREDS)
        (session_dir / ".claude.json").write_text(CONFIG)
        return session_dir

    @pytest.fixture
    def auth_always_invalid(self, monkeypatch):
        """`claude auth status` never reports logged-in → reuse check fails."""

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)

    def test_refuses_rebootstrap_while_live(
        self, manager, auth_always_invalid, monkeypatch
    ):
        """A live but auth-invalid session must not be re-seeded.

        The cheap reuse check fails (revoked token), but a claude is running
        against the profile — `setup_session` must raise instead of dropping
        into `_bootstrap`.
        """
        session_dir = self._seed_profile(manager)
        monkeypatch.setattr(
            session_mod,
            "live_sessions_for",
            lambda d: [make_live_session(d)] if Path(d) == session_dir else [],
        )
        # If we ever reach the re-seed, this would blow up the test loudly.
        monkeypatch.setattr(
            manager,
            "_bootstrap",
            lambda *a, **k: pytest.fail("_bootstrap must not run under a live session"),
        )

        with pytest.raises(SessionError, match="live session"):
            manager.setup_session("2", share=False)

    def test_profile_preserved_when_refused(
        self, manager, auth_always_invalid, monkeypatch
    ):
        """The live profile's dir, credentials and keychain are left intact."""
        session_dir = self._seed_profile(manager)
        monkeypatch.setattr(
            session_mod,
            "live_sessions_for",
            lambda d: [make_live_session(d)] if Path(d) == session_dir else [],
        )
        # Any keychain delete here would pull the entry out from under claude.
        monkeypatch.setattr(
            session_mod,
            "delete_macos_keychain_entry",
            lambda d: pytest.fail("keychain entry must not be deleted under a live session"),
        )

        with pytest.raises(SessionError):
            manager.setup_session("2", share=False)

        # rmtree / re-seed never happened.
        assert session_dir.exists()
        assert (session_dir / ".credentials.json").read_text() == CREDS

    def test_cleanup_failed_session_refuses_live(self, manager, monkeypatch):
        """`_cleanup_failed_session` itself refuses while a session is live."""
        session_dir = self._seed_profile(manager)
        monkeypatch.setattr(
            session_mod,
            "live_sessions_for",
            lambda d: [make_live_session(d)] if Path(d) == session_dir else [],
        )
        monkeypatch.setattr(
            session_mod.shutil,
            "rmtree",
            lambda *a, **k: pytest.fail("rmtree must not run under a live session"),
        )

        with pytest.raises(SessionError, match="live session"):
            manager._cleanup_failed_session(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)

        assert session_dir.exists()

    def test_cleanup_failed_session_proceeds_when_not_live(self, manager, monkeypatch):
        """No live session → cleanup still rmtree's the failed profile."""
        session_dir = self._seed_profile(manager)
        monkeypatch.setattr(session_mod, "live_sessions_for", lambda d: [])
        monkeypatch.setattr(
            session_mod, "delete_macos_keychain_entry", lambda d: None
        )

        manager._cleanup_failed_session(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)

        assert not session_dir.exists()

    def test_rebootstrap_proceeds_when_not_live(self, manager, monkeypatch):
        """No live session → an invalid profile is re-bootstrapped normally."""
        session_dir = self._seed_profile(manager)
        # Make the on-disk creds obviously stale so a re-bootstrap is visible.
        (session_dir / ".credentials.json").write_text("stale lineage")
        monkeypatch.setattr(session_mod, "live_sessions_for", lambda d: [])
        monkeypatch.setattr(
            session_mod, "refresh_oauth_credentials", lambda c: None
        )

        # Auth status: invalid until creds re-seeded to the canonical backup.
        def auth(cmd, env=None, **kwargs):
            cfg = Path(env["CLAUDE_CONFIG_DIR"])
            seeded = (cfg / ".credentials.json").read_text() == CREDS
            payload = (
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "email": ACCOUNT_EMAIL,
                    "orgId": ORG_UUID,
                }
                if seeded
                else {"loggedIn": False, "authMethod": "none"}
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr(session_mod.subprocess, "run", auth)

        out_dir, num, email = manager.setup_session("2", share=False)

        assert out_dir == session_dir
        assert (session_dir / ".credentials.json").read_text() == CREDS


# ---------------------------------------------------------------------------
# F21 — flush stdout before exec so the launch line is never dropped
# ---------------------------------------------------------------------------


class _ExecCalled(Exception):
    def __init__(self, binary, argv, env):
        self.binary, self.argv, self.env = binary, argv, env


class TestFlushBeforeExec:
    @pytest.fixture
    def capture_exec(self, monkeypatch):
        def fake_execvpe(binary, argv, env):
            raise _ExecCalled(binary, argv, env)

        monkeypatch.setattr(session_mod.os, "execvpe", fake_execvpe)
        monkeypatch.setattr(
            session_mod.shutil, "which", lambda name: f"/fake/bin/{name}"
        )

    @pytest.fixture
    def valid_session(self, monkeypatch):
        """Profile passes validation so run() reaches the launch line + exec."""

        def auth(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "claude.ai",
                        "email": ACCOUNT_EMAIL,
                        "orgId": ORG_UUID,
                    }
                ),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", auth)
        monkeypatch.setattr(
            session_mod, "refresh_oauth_credentials", lambda c: None
        )

    def test_stdout_flushed_before_exec(
        self, manager, capture_exec, valid_session, monkeypatch
    ):
        """sys.stdout.flush() must run before os.execvpe replaces the process.

        execvpe never flushes Python's buffers, so without an explicit flush
        the "Launching…" line is silently dropped on a non-TTY stdout.
        """
        events: list[str] = []
        real_flush = sys.stdout.flush

        def tracking_flush():
            events.append("flush")
            real_flush()

        monkeypatch.setattr(sys.stdout, "flush", tracking_flush)

        # Bootstrap the profile first so run() takes the launch (not fast) path.
        manager.setup_session("2", share=False)

        with pytest.raises(_ExecCalled):
            # Record the exec relative to the flush ordering.
            def fake_execvpe(binary, argv, env):
                events.append("exec")
                raise _ExecCalled(binary, argv, env)

            monkeypatch.setattr(session_mod.os, "execvpe", fake_execvpe)
            manager.run("2", [])

        assert "flush" in events, "stdout was never flushed before exec"
        assert events.index("flush") < events.index("exec"), (
            "stdout must be flushed before exec replaces the process"
        )
