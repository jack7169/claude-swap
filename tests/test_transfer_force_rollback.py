"""Tests for the --force overwrite rollback in transfer.import_accounts.

The overwrite branch writes a slot's credentials and config in sequence. If the
config write (or the sequence write) fails *after* the credential write
succeeded, the slot would otherwise be left holding the NEW credentials beside
the OLD config — a mismatched, half-applied state that breaks the next switch.

These pin down the snapshot/restore: a failed overwrite must leave the slot in
its original, consistent (creds, config) pair, and the underlying error must
still propagate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_transfer.py)
# ---------------------------------------------------------------------------


def _linux_switcher() -> ClaudeAccountSwitcher:
    """A switcher with file-based (Linux) credential storage."""
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _seed_account(
    switcher: ClaudeAccountSwitcher,
    num: int,
    email: str,
    org_uuid: str = "",
    org_name: str = "",
    creds: dict | None = None,
    config: dict | None = None,
) -> None:
    """Write an account to backup + sequence.json."""
    creds_obj = creds if creds is not None else {
        "accessToken": "tok-old",
        "refreshToken": "rtok-old",
        "expiresAt": 9999,
        "_marker": "OLD",
    }
    config_obj = config if config is not None else {
        "oauthAccount": {
            "emailAddress": email,
            "accountUuid": f"acct-{num}",
            "organizationUuid": org_uuid,
            "organizationName": org_name,
            "_marker": "OLD",
        }
    }
    switcher._write_account_credentials(str(num), email, json.dumps(creds_obj))
    switcher._write_account_config(str(num), email, json.dumps(config_obj))

    data = switcher._get_sequence_data() or {
        "activeAccountNumber": None,
        "lastUpdated": "",
        "sequence": [],
        "accounts": {},
    }
    data["accounts"][str(num)] = {
        "email": email,
        "uuid": f"acct-{num}",
        "organizationUuid": org_uuid,
        "organizationName": org_name,
        "added": "2024-01-01T00:00:00Z",
    }
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    if data["activeAccountNumber"] is None:
        data["activeAccountNumber"] = num
    switcher._write_json(switcher.sequence_file, data)


def _make_updated_envelope(out_path: Path) -> None:
    """In-place bump the single account's creds + config to NEW markers.

    Simulates re-exporting the same account from another machine with fresher
    tokens and config — the thing a --force import is supposed to apply.
    """
    env = json.loads(out_path.read_text())
    acct = env["accounts"][0]
    acct["credentials"]["accessToken"] = "tok-NEW"
    acct["credentials"]["_marker"] = "NEW"
    acct["config"]["oauthAccount"]["_marker"] = "NEW"
    out_path.write_text(json.dumps(env))


# ---------------------------------------------------------------------------
# Rollback on a failed config write during --force overwrite
# ---------------------------------------------------------------------------


class TestForceOverwriteRollback:
    def test_config_write_failure_restores_creds_and_config(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config write blows up after creds were overwritten → slot restored.

        The credential write succeeds (writing tok-NEW), then the config write
        raises. Without rollback the slot would hold tok-NEW + OLD config — a
        mismatch. With rollback the slot must be back to OLD creds + OLD config,
        and the error must propagate.
        """
        s = _linux_switcher()
        _seed_account(s, 1, "alice@example.com", "org-a", "Org A")

        out = temp_home / "alice.cswap"
        export_accounts(s, str(out), account="1")
        _make_updated_envelope(out)

        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")
        # Sanity: pre-import state is the OLD markers.
        assert json.loads(creds_before)["_marker"] == "OLD"
        assert json.loads(config_before)["oauthAccount"]["_marker"] == "OLD"

        real_write_config = s._write_account_config

        def boom_config(account_num, email, config):
            # Let the seeding / restore writes through; only sabotage the
            # NEW config that the overwrite branch tries to apply.
            if "NEW" in config:
                raise RuntimeError("simulated config write failure")
            return real_write_config(account_num, email, config)

        monkeypatch.setattr(s, "_write_account_config", boom_config)

        with pytest.raises(RuntimeError, match="simulated config write failure"):
            import_accounts(s, str(out), force=True)

        # Slot must be back to its pre-import, internally-consistent state:
        # OLD creds beside OLD config — never tok-NEW + OLD config.
        creds_after = s._read_account_credentials("1", "alice@example.com")
        config_after = s._read_account_config("1", "alice@example.com")
        assert creds_after == creds_before
        assert config_after == config_before
        assert json.loads(creds_after)["_marker"] == "OLD"
        assert json.loads(config_after)["oauthAccount"]["_marker"] == "OLD"

    def test_sequence_write_failure_restores_slot(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sequence write fails after both creds + config were overwritten.

        Both file writes land (tok-NEW + NEW config), then the sequence.json
        write raises. Rollback must restore the slot's original creds AND config
        so the on-disk backup matches the (unchanged) sequence record, and the
        error must propagate.
        """
        s = _linux_switcher()
        _seed_account(s, 1, "alice@example.com", "org-a", "Org A")

        out = temp_home / "alice.cswap"
        export_accounts(s, str(out), account="1")
        _make_updated_envelope(out)

        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")

        real_write_json = s._write_json

        def boom_json(path, data):
            if path == s.sequence_file and "alice@example.com" in json.dumps(data):
                raise RuntimeError("simulated sequence write failure")
            return real_write_json(path, data)

        monkeypatch.setattr(s, "_write_json", boom_json)

        with pytest.raises(RuntimeError, match="simulated sequence write failure"):
            import_accounts(s, str(out), force=True)

        creds_after = s._read_account_credentials("1", "alice@example.com")
        config_after = s._read_account_config("1", "alice@example.com")
        assert creds_after == creds_before
        assert config_after == config_before
        assert json.loads(creds_after)["_marker"] == "OLD"
        assert json.loads(config_after)["oauthAccount"]["_marker"] == "OLD"
