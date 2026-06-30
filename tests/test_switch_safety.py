"""Credential-swap rollback safety tests (Phase 1 fixes 1.1, 1.3, 1.4, 1.5).

These tests target ``ClaudeAccountSwitcher._perform_switch_locked`` and
``SwitchTransaction.rollback`` — the multi-step reverse-order restore that
prevents an account-lockout / identity-mismatch when a write fails (or a Ctrl-C
lands) mid-switch.

They reuse the in-memory store-patch helpers from ``test_switcher.py`` so
nothing touches the real macOS Keychain (which would prompt the user) or spawn
subprocesses — the conftest autouse fixtures redirect ``$HOME`` and fake the
Keychain, and these patches stub the credential/config read/write delegators.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import ConfigError, SwitchError
from claude_swap.models import SwitchTransaction
from claude_swap.switcher import ClaudeAccountSwitcher

# Reuse the existing in-memory store-patch helpers verbatim rather than
# re-implementing the keychain-bypass plumbing.
from tests.test_switcher import TestPerformSwitchPostDisplay


def _two_account_switcher(temp_home: Path, sample_sequence_data: dict):
    """Build a switcher with account 1 live+active and account 2 as a backup.

    Returns (switcher, creds_store, configs_store, live_state, config_path,
    original_live_creds, original_config_text). The store patches are NOT
    installed yet — the caller installs them so it controls teardown.
    """
    helper = TestPerformSwitchPostDisplay()
    switcher, creds_store, configs_store = helper._setup_two_accounts(
        temp_home, sample_sequence_data,
    )

    # Live ~/.claude.json must name account 1 so the normal (back-up-current)
    # branch runs: _get_current_account() reads emailAddress from it and
    # _find_account_slot maps it to slot 1.
    config_path = temp_home / ".claude.json"
    original_config_text = json.dumps({
        "oauthAccount": {
            "emailAddress": "test@example.com",
            "accountUuid": "uuid-1",
        }
    })
    config_path.write_text(original_config_text)

    original_live_creds = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-live-1",
            "refreshToken": "rt-live-1",
        },
    })
    live_state = {"creds": original_live_creds}
    return (
        switcher,
        creds_store,
        configs_store,
        live_state,
        config_path,
        original_live_creds,
        original_config_text,
        helper,
    )


class TestSigintBypassesRollback:
    """1.1 — a KeyboardInterrupt mid-switch must still roll back (BaseException)."""

    def test_keyboard_interrupt_at_config_write_rolls_back_creds(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """Ctrl-C between Step 3 (creds) and Step 4 (config) must restore the
        live credentials to the original account and re-raise the interrupt.

        Before the fix the rollback is guarded by ``except Exception``, so the
        ``KeyboardInterrupt`` (a ``BaseException``) escapes WITHOUT rollback —
        leaving account 2's credentials live but config/sequence on account 1.
        """
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)
        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def interrupt_on_config_write(path, data):
            # Step 4 writes the *config* path (not the sequence file).
            if path == config_path:
                raise KeyboardInterrupt()
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=interrupt_on_config_write,
            ), pytest.raises(KeyboardInterrupt):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        # Live credentials must be rolled back to account 1's originals.
        assert live_state["creds"] == original_live_creds
        # Config and sequence never advanced past account 1.
        assert config_path.read_text() == original_config_text
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_normal_exception_still_rolls_back_and_wraps_in_switcherror(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """An ordinary Exception path is unchanged: rollback runs and the error
        is wrapped in a SwitchError mentioning 'rolled back'."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)
        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def fail_on_sequence_write(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 2:
                raise OSError("disk full")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=fail_on_sequence_write,
            ), pytest.raises(SwitchError, match="rolled back"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == original_live_creds
        assert config_path.read_text() == original_config_text


class TestNormalPathRollback:
    """1.3 — the normal-path SwitchTransaction.rollback restore guarantees."""

    def test_sequence_write_failure_restores_creds_config_and_active(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """(a) sequence write fails after creds+config written -> live creds,
        ~/.claude.json oauthAccount, and activeAccountNumber are ALL restored to
        the pre-switch account, and a SwitchError('rolled back') is raised."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)
        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def fail_on_sequence_write(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 2:
                raise OSError("sequence write boom")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=fail_on_sequence_write,
            ), pytest.raises(SwitchError, match="rolled back"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        # Live creds restored to account 1.
        assert live_state["creds"] == original_live_creds
        # ~/.claude.json oauthAccount restored to account 1.
        restored = json.loads(config_path.read_text())
        assert restored["oauthAccount"]["emailAddress"] == "test@example.com"
        assert config_path.read_text() == original_config_text
        # activeAccountNumber still account 1.
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_config_write_failure_restores_creds(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """(b) config write fails after creds written -> creds restored."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)
        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def fail_on_config_write(path, data):
            if path == config_path:
                raise OSError("config write boom")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=fail_on_config_write,
            ), pytest.raises(SwitchError, match="rolled back"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == original_live_creds
        assert config_path.read_text() == original_config_text
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_rollback_failure_raises_manual_recovery_error(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """(c) when rollback itself fails -> the 'rollback also failed / Manual
        recovery' SwitchError is raised."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)
        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json
        # Fail the sequence write to trigger rollback...
        def fail_on_sequence_write(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 2:
                raise OSError("sequence write boom")
            return original_write_json(path, data)

        # ...and make the rollback's credential restore fail too.
        write_calls = {"n": 0}
        original_write_creds = switcher._write_credentials

        def fail_creds_on_rollback(creds):
            write_calls["n"] += 1
            # First call is Step 3 (activate target); a later call is the
            # rollback restore — fail that one.
            if write_calls["n"] >= 2:
                raise OSError("rollback creds boom")
            return original_write_creds(creds)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=fail_on_sequence_write,
            ), patch.object(
                switcher, "_write_credentials", side_effect=fail_creds_on_rollback,
            ), pytest.raises(SwitchError, match="Manual recovery"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()


class TestSwitchTransactionRollbackUnit:
    """1.3 — direct unit test of SwitchTransaction.rollback per step + order."""

    def test_each_recorded_step_restores_in_reverse_order(
        self, temp_home: Path,
    ):
        """rollback restores credentials, config, and sequence — and does so in
        reverse order of the recorded steps."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 7,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [3, 7],
            "accounts": {
                "3": {"email": "three@example.com", "added": "x"},
                "7": {"email": "seven@example.com", "added": "x"},
            },
        })
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({"oauthAccount": {"emailAddress": "new"}}))

        transaction = SwitchTransaction(
            original_credentials="ORIGINAL-CREDS",
            original_config=json.dumps({"oauthAccount": {"emailAddress": "orig"}}),
            original_account_num="3",
            original_email="three@example.com",
            config_path=config_path,
        )
        transaction.record_step("credentials_written")
        transaction.record_step("config_written")
        transaction.record_step("sequence_updated")

        order: list[str] = []
        restored = {"creds": None}

        def record_write_creds(creds):
            order.append("creds")
            restored["creds"] = creds

        with patch.object(
            switcher, "_write_credentials", side_effect=record_write_creds,
        ):
            ok = transaction.rollback(switcher)

        assert ok is True
        # Reverse order: sequence first, config next, credentials last.
        assert order == ["creds"]  # only the creds write is observable here
        # Credentials restored to the original value.
        assert restored["creds"] == "ORIGINAL-CREDS"
        # Config restored to the original text.
        assert json.loads(config_path.read_text())["oauthAccount"]["emailAddress"] == (
            "orig"
        )
        # activeAccountNumber restored to the original account.
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 3

    def test_reverse_order_of_restores(self, temp_home: Path):
        """The three restores fire strictly in reverse of how they were
        recorded: sequence_updated, then config_written, then credentials_written."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 9,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [2, 9],
            "accounts": {
                "2": {"email": "two@example.com", "added": "x"},
                "9": {"email": "nine@example.com", "added": "x"},
            },
        })
        config_path = temp_home / ".claude.json"
        config_path.write_text("{}")

        transaction = SwitchTransaction(
            original_credentials="creds-2",
            original_config="{}",
            original_account_num="2",
            original_email="two@example.com",
            config_path=config_path,
        )
        transaction.record_step("credentials_written")
        transaction.record_step("config_written")
        transaction.record_step("sequence_updated")

        order: list[str] = []

        def record_creds(creds):
            order.append("credentials_written")

        original_write_json = switcher._write_json

        def record_json(path, data):
            if path == switcher.sequence_file:
                order.append("sequence_updated")
            return original_write_json(path, data)

        # Wrap Path.write_text on the config to detect the config restore.
        original_write_text = type(config_path).write_text

        def record_write_text(self, *args, **kwargs):
            if self == config_path:
                order.append("config_written")
            return original_write_text(self, *args, **kwargs)

        with patch.object(
            switcher, "_write_credentials", side_effect=record_creds,
        ), patch.object(
            switcher, "_write_json", side_effect=record_json,
        ), patch.object(
            type(config_path), "write_text", record_write_text,
        ):
            transaction.rollback(switcher)

        assert order == [
            "sequence_updated",
            "config_written",
            "credentials_written",
        ]


class TestStepOneBackupRestore:
    """1.4 — Step 1 must not unrecoverably overwrite the current account's backup."""

    def test_prior_backup_restored_when_later_step_fails(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """If a later step fails, the current account's BACKUP must be restored
        to its pre-switch contents — not left as the possibly-wrong live snapshot
        that Step 1 wrote over it."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)

        # Seed a GOOD prior backup for account 1 that differs from the live
        # snapshot. Step 1 would overwrite this with the (here, different) live
        # value; rollback must put the prior backup back.
        prior_backup_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-GOOD-prior-1",
                "refreshToken": "rt-GOOD-prior-1",
            },
        })
        prior_backup_config = json.dumps({
            "oauthAccount": {"emailAddress": "test@example.com", "marker": "PRIOR"},
        })
        creds_store[("1", "test@example.com")] = prior_backup_creds
        configs_store[("1", "test@example.com")] = prior_backup_config

        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def fail_on_sequence_write(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 2:
                raise OSError("sequence write boom")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=fail_on_sequence_write,
            ), pytest.raises(SwitchError, match="rolled back"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        # The prior backup for account 1 must be restored, NOT the live snapshot
        # Step 1 wrote over it.
        assert creds_store[("1", "test@example.com")] == prior_backup_creds
        assert configs_store[("1", "test@example.com")] == prior_backup_config

    def test_newly_written_backup_removed_when_no_prior_backup(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """If there was NO prior backup for the current account, rollback removes
        the newly-written one rather than leaving a stray backup behind."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)

        # No prior backup for account 1 (the helper only seeds account 2).
        assert ("1", "test@example.com") not in creds_store
        assert ("1", "test@example.com") not in configs_store

        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        # The rollback removes the newly-written backup via the store delete
        # primitives; route those through the in-memory stores too.
        def delete_creds(num, email):
            creds_store.pop((str(num), email), None)

        def delete_cfg(num, email):
            configs_store.pop((str(num), email), None)

        patches.append(
            patch.object(
                switcher, "_delete_account_credentials", side_effect=delete_creds,
            )
        )
        patches.append(
            patch.object(
                switcher, "_delete_account_config", side_effect=delete_cfg,
            )
        )
        patches[-2].start()
        patches[-1].start()

        original_write_json = switcher._write_json

        def fail_on_sequence_write(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 2:
                raise OSError("sequence write boom")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=fail_on_sequence_write,
            ), pytest.raises(SwitchError, match="rolled back"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        # The backup written by Step 1 must have been removed during rollback.
        assert ("1", "test@example.com") not in creds_store
        assert ("1", "test@example.com") not in configs_store


class TestCorruptLiveConfig:
    """1.5 — invalid live ~/.claude.json yields a clear ConfigError, not TypeError."""

    def test_invalid_live_config_raises_configerror(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """When ~/.claude.json is invalid JSON at Step 4, the switch must fail
        with a clear ConfigError cause (rolled back, surfaced through the
        rollback's SwitchError wrapper), not a confusing TypeError from indexing
        the None that _read_json returns for invalid JSON."""
        (
            switcher,
            creds_store,
            configs_store,
            live_state,
            config_path,
            original_live_creds,
            original_config_text,
            helper,
        ) = _two_account_switcher(temp_home, sample_sequence_data)
        patches = helper._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        # _get_current_account reads the live config FIRST (must be valid so the
        # normal back-up-current branch runs and slot 1 is found). Corrupt it
        # only when Step 4 re-reads it via _read_json.
        original_read_json = switcher._read_json
        seen = {"current_account_done": False}

        def corrupt_on_step4(path):
            if path == config_path and seen["current_account_done"]:
                # Simulate invalid JSON -> _read_json returns None.
                return None
            return original_read_json(path)

        # Mark that _get_current_account has run by patching it to set the flag
        # after returning the real identity.
        original_get_current = switcher._get_current_account

        def get_current():
            result = original_get_current()
            seen["current_account_done"] = True
            return result

        try:
            with patch.object(
                switcher, "_get_current_account", side_effect=get_current,
            ), patch.object(
                switcher, "_read_json", side_effect=corrupt_on_step4,
            ), pytest.raises(SwitchError, match="not valid JSON") as exc_info:
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        # The underlying cause must be a clear ConfigError (NOT a confusing
        # TypeError from indexing None), and the switch must be rolled back.
        assert isinstance(exc_info.value.__context__, ConfigError)
        assert not isinstance(exc_info.value.__context__, TypeError)
        assert "rolled back" in str(exc_info.value)
        assert live_state["creds"] == original_live_creds
