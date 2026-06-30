"""Robustness tests for purge() and add/remove edge cases (Phase 7.1 + 7.3).

Covers:
- purge() under non-interactive/EOF stdin cancels cleanly rather than crashing
  with an uncaught EOFError, and a mid-delete ``shutil.rmtree`` failure surfaces
  a clean ``ClaudeSwitchError`` instead of a raw ``OSError``.
- add_account_from_oauth's explicit-slot branch now displaces a different
  occupant (no orphaned .enc / lost record) instead of silently overwriting.
- add_account_from_token to an occupied slot from a NON-INTERACTIVE stdin
  (token '-' or stdin not a TTY) is a hard error, not a silent exit-0 cancel.
- _reconcile_orphaned_backups() removes crash-orphan backups whose (N,email)
  has no matching sequence record, while leaving valid backups intact.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from claude_swap import switcher as switcher_mod
from claude_swap.exceptions import ClaudeSwitchError, ConfigError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4  # 53 chars
OAUTH_CREDS = json.dumps(
    {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rtok", "expiresAt": 9}}
)


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


# ---------------------------------------------------------------------------
# 7.1 purge(): EOF stdin + rmtree failure
# ---------------------------------------------------------------------------


class TestPurgeEofStdin:
    """An EOF / non-interactive stdin at the confirmation prompt must
    safe-cancel rather than raise an uncaught EOFError."""

    def test_eof_cancels_without_raising_or_rmtree(
        self, temp_home: Path, monkeypatch, capsys
    ):
        s = _linux_switcher()
        s.add_account_from_token(OAUTH_CREDS, email="user@example.com", slot=1)
        assert s.backup_dir.exists()

        def _raise_eof(prompt=""):
            raise EOFError

        monkeypatch.setattr(builtins, "input", _raise_eof)

        # rmtree must never be reached on a cancel. Record calls rather than
        # raising from the stub (a raising stub would also blow up pytest's own
        # tmp cleanup at session teardown).
        calls: list = []
        real_rmtree = switcher_mod.shutil.rmtree
        monkeypatch.setattr(
            switcher_mod.shutil,
            "rmtree",
            lambda *a, **k: calls.append((a, k)) or real_rmtree(*a, **k),
        )

        # Must not raise.
        s.purge()

        out = capsys.readouterr().out
        assert "Cancelled" in out
        # rmtree was never invoked, and the backup dir is untouched.
        assert calls == []
        assert s.backup_dir.exists()

    def test_keyboard_interrupt_cancels(self, temp_home: Path, monkeypatch, capsys):
        s = _linux_switcher()
        s.add_account_from_token(OAUTH_CREDS, email="user@example.com", slot=1)

        def _raise_kbd(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr(builtins, "input", _raise_kbd)
        calls: list = []
        real_rmtree = switcher_mod.shutil.rmtree
        monkeypatch.setattr(
            switcher_mod.shutil,
            "rmtree",
            lambda *a, **k: calls.append((a, k)) or real_rmtree(*a, **k),
        )

        s.purge()

        assert "Cancelled" in capsys.readouterr().out
        assert calls == []
        assert s.backup_dir.exists()


class TestPurgeRmtreeFailure:
    """A failure deleting the backup directory must surface as a clean
    ClaudeSwitchError summarizing the partial state, not a raw OSError."""

    def test_rmtree_oserror_becomes_claudeswitcherror(
        self, temp_home: Path, monkeypatch
    ):
        s = _linux_switcher()
        s.add_account_from_token(OAUTH_CREDS, email="user@example.com", slot=1)

        monkeypatch.setattr(builtins, "input", lambda prompt="": "y")

        real_rmtree = switcher_mod.shutil.rmtree

        def _fail_rmtree(path, *a, **k):
            # Simulate a failed delete of OUR backup dir: like
            # rmtree(ignore_errors=True) hitting an undeletable file, the call
            # returns but the directory survives. Any other rmtree (e.g.
            # pytest's own tmp cleanup) works normally.
            if Path(path) == s.backup_dir:
                return None  # left the directory in place
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr(switcher_mod.shutil, "rmtree", _fail_rmtree)

        with pytest.raises(ClaudeSwitchError):
            s.purge()


# ---------------------------------------------------------------------------
# 7.3 (R2F1) add_account_from_oauth explicit-slot displaces a different occupant
# ---------------------------------------------------------------------------


class TestOauthExplicitSlotDisplace:
    def test_displaces_different_identity_no_orphan(self, temp_home: Path):
        s = _linux_switcher()
        # Seed slot 1 with one OAuth account.
        old = s.add_account_from_oauth(
            credentials=OAUTH_CREDS,
            email="old@example.com",
            slot=1,
        )
        assert old == "1"
        old_enc = s.credentials_dir / ".creds-1-old@example.com.enc"
        old_cfg = s.configs_dir / ".claude-config-1-old@example.com.json"
        assert old_enc.exists()
        assert old_cfg.exists()

        # Now add a DIFFERENT identity to the same explicit slot.
        new = s.add_account_from_oauth(
            credentials=OAUTH_CREDS,
            email="new@example.com",
            slot=1,
        )
        assert new == "1"

        data = s._get_sequence_data()
        # Slot 1 record replaced by the new identity.
        assert data["accounts"]["1"]["email"] == "new@example.com"
        # The old identity's record is gone (not orphaned in a second slot).
        emails = {acct["email"] for acct in data["accounts"].values()}
        assert "old@example.com" not in emails
        # And the old occupant's backup files are removed (no orphan).
        assert not old_enc.exists()
        assert not old_cfg.exists()
        # The new occupant's backups exist.
        assert (s.credentials_dir / ".creds-1-new@example.com.enc").exists()
        assert (s.configs_dir / ".claude-config-1-new@example.com.json").exists()

    def test_cross_kind_collision_rejected(self, temp_home: Path):
        from claude_swap.exceptions import ValidationError

        s = _linux_switcher()
        # An API-key account on this email already exists.
        s.add_account_from_token(API_KEY, email="dup@example.com")
        with pytest.raises(ValidationError, match="already exists as an API-key"):
            s.add_account_from_oauth(
                credentials=OAUTH_CREDS,
                email="dup@example.com",
                slot=2,
            )


# ---------------------------------------------------------------------------
# 7.3 (F33) add_account_from_token occupied slot, non-interactive -> hard error
# ---------------------------------------------------------------------------


class TestTokenOccupiedSlotNonInteractive:
    def test_stdin_dash_occupied_slot_raises(self, temp_home: Path, monkeypatch):
        s = _linux_switcher()
        s.add_account_from_token(OAUTH_CREDS, email="occupant@example.com", slot=1)

        # Read the new token from stdin ('-' is inherently non-interactive).
        monkeypatch.setattr(
            switcher_mod.sys.stdin, "readline", lambda: API_KEY + "\n"
        )
        with pytest.raises(ConfigError):
            s.add_account_from_token("-", email="newcomer@example.com", slot=1)

        # Slot 1 is unchanged.
        data = s._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "occupant@example.com"

    def test_non_tty_stdin_occupied_slot_raises(self, temp_home: Path, monkeypatch):
        s = _linux_switcher()
        s.add_account_from_token(OAUTH_CREDS, email="occupant@example.com", slot=1)

        # stdin is not a TTY -> no human to answer the overwrite prompt.
        monkeypatch.setattr(switcher_mod.sys.stdin, "isatty", lambda: False)
        with pytest.raises(ConfigError):
            s.add_account_from_token(API_KEY, email="newcomer@example.com", slot=1)

        data = s._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "occupant@example.com"

    def test_interactive_same_identity_refresh_still_works(
        self, temp_home: Path, monkeypatch
    ):
        # A non-interactive add to an occupied slot holding the SAME identity is
        # an in-place refresh (no overwrite of a different occupant), so it must
        # still succeed even when stdin is not a TTY. (Same kind too — switching
        # an email from OAuth to API-key is a separate cross-kind guard.)
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-orig", email="same@example.com", slot=1)
        monkeypatch.setattr(switcher_mod.sys.stdin, "isatty", lambda: False)
        # Re-add the same identity (same OAuth kind) to the same slot — no raise.
        s.add_account_from_token("sk-ant-oat01-fresh", email="same@example.com", slot=1)
        blob = json.loads(s._read_account_credentials("1", "same@example.com"))
        assert blob["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-fresh"


# ---------------------------------------------------------------------------
# 7.3 (R2F3) _reconcile_orphaned_backups
# ---------------------------------------------------------------------------


class TestReconcileOrphanedBackups:
    def test_removes_orphan_enc_keeps_valid(self, temp_home: Path):
        s = _linux_switcher()
        # A real, recorded account.
        s.add_account_from_token(OAUTH_CREDS, email="real@example.com", slot=1)
        valid_enc = s.credentials_dir / ".creds-1-real@example.com.enc"
        valid_cfg = s.configs_dir / ".claude-config-1-real@example.com.json"
        assert valid_enc.exists()

        # A crash-orphan: backup files with NO matching sequence record.
        orphan_enc = s.credentials_dir / ".creds-9-ghost@example.com.enc"
        orphan_cfg = s.configs_dir / ".claude-config-9-ghost@example.com.json"
        orphan_enc.write_text("orphan-creds", encoding="utf-8")
        orphan_cfg.write_text("{}", encoding="utf-8")

        s._reconcile_orphaned_backups()

        # Orphan removed.
        assert not orphan_enc.exists()
        assert not orphan_cfg.exists()
        # Valid backup untouched.
        assert valid_enc.exists()
        assert valid_cfg.exists()
        # The recorded account is still present.
        assert s._get_sequence_data()["accounts"]["1"]["email"] == "real@example.com"

    def test_missing_backup_record_is_kept(self, temp_home: Path):
        # A sequence record whose backups are missing is logged, NOT deleted.
        s = _linux_switcher()
        s.add_account_from_token(OAUTH_CREDS, email="real@example.com", slot=1)
        # Remove its backup files behind reconcile's back.
        (s.credentials_dir / ".creds-1-real@example.com.enc").unlink()
        cfg = s.configs_dir / ".claude-config-1-real@example.com.json"
        if cfg.exists():
            cfg.unlink()

        s._reconcile_orphaned_backups()

        # The record survives — reconcile must not delete sequence entries.
        assert s._get_sequence_data()["accounts"]["1"]["email"] == "real@example.com"

    def test_reconcile_runs_under_add_and_clears_prior_orphan(self, temp_home: Path):
        # An orphan from a crashed prior add is swept when the next add reuses
        # the slot, so no stale backup shadows the reused slot.
        s = _linux_switcher()
        orphan_enc = s.credentials_dir / ".creds-1-ghost@example.com.enc"
        orphan_cfg = s.configs_dir / ".claude-config-1-ghost@example.com.json"
        orphan_enc.write_text("orphan-creds", encoding="utf-8")
        orphan_cfg.write_text("{}", encoding="utf-8")

        s.add_account_from_token(OAUTH_CREDS, email="fresh@example.com", slot=1)

        assert not orphan_enc.exists()
        assert not orphan_cfg.exists()
        assert s._read_account_credentials("1", "fresh@example.com")
