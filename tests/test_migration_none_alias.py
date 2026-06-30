"""Phase 7.4 — the ambiguous ``account-None-{email}`` alias must not be deleted
as cleanup when its email maps to MULTIPLE slots.

The READ side already gates the ``account-None`` *fallback* on
``email_counts[email] == 1`` (an ambiguous alias is never attributed to a slot).
The cleanup-delete of the ``account-None`` alias must use the same guard:
otherwise, when two slots share an email but each has its own canonical entry, a
successful migration of a canonical entry would delete the ambiguous
``account-None`` alias that was never migrated anywhere — risking loss of an
entry that may be the only copy of real data.

These tests reuse the in-memory ``FakeKeyring`` / Keychain fakes and the
switcher fixtures from ``tests/test_migrations.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap import migrations
from claude_swap.switcher import KEYRING_SERVICE, ClaudeAccountSwitcher

from tests.test_migrations import (
    FakeKeyring,
    _make_macos_switcher,
    _make_windows_switcher,
    _patch_keyring,
    _seed_sequence,
)


# ---------------------------------------------------------------------------
# Windows (migrate_windows_keyring_to_files)
# ---------------------------------------------------------------------------


class TestWindowsNoneAliasCleanup:
    def test_ambiguous_none_alias_preserved_when_email_maps_to_many_slots(
        self, temp_home: Path
    ):
        """Two slots share an email, each with its own canonical entry, PLUS an
        ambiguous ``account-None-{email}`` alias. Migrating the canonical
        entries must NOT delete the ambiguous alias (it was never attributed)."""
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(
            switcher,
            {
                "1": {"email": "dup@example.com", "organizationUuid": "org-1"},
                "2": {"email": "dup@example.com", "organizationUuid": "org-2"},
            },
        )
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-dup@example.com"): "creds-1",
                (KEYRING_SERVICE, "account-2-dup@example.com"): "creds-2",
                (KEYRING_SERVICE, "account-None-dup@example.com"): "ambiguous",
            }
        )

        with _patch_keyring(fake):
            migrations.migrate_windows_keyring_to_files(switcher)

        # Both canonical entries migrated into their slots…
        assert switcher._read_account_credentials("1", "dup@example.com") == "creds-1"
        assert switcher._read_account_credentials("2", "dup@example.com") == "creds-2"
        # …their canonical keyring entries cleaned up…
        assert (KEYRING_SERVICE, "account-1-dup@example.com") in fake.deleted
        assert (KEYRING_SERVICE, "account-2-dup@example.com") in fake.deleted
        # …but the ambiguous account-None alias is PRESERVED (never deleted),
        # because its email maps to more than one slot.
        assert (KEYRING_SERVICE, "account-None-dup@example.com") not in fake.deleted
        assert (KEYRING_SERVICE, "account-None-dup@example.com") in fake.store

    def test_none_alias_deleted_when_email_maps_to_single_slot(
        self, temp_home: Path
    ):
        """A single slot with both a canonical entry and a redundant
        ``account-None`` alias (unambiguous email) → the alias IS cleaned up."""
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "solo@example.com"}})
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-solo@example.com"): "canonical",
                (KEYRING_SERVICE, "account-None-solo@example.com"): "stale-none",
            }
        )

        with _patch_keyring(fake):
            migrations.migrate_windows_keyring_to_files(switcher)

        assert switcher._read_account_credentials("1", "solo@example.com") == "canonical"
        # Unambiguous email → the redundant None alias is removed as cleanup.
        assert (KEYRING_SERVICE, "account-None-solo@example.com") in fake.deleted
        assert fake.store == {}


# ---------------------------------------------------------------------------
# macOS (migrate_macos_keyring_to_security)
# ---------------------------------------------------------------------------


class TestMacosNoneAliasCleanup:
    def test_ambiguous_none_alias_preserved_when_email_maps_to_many_slots(
        self, temp_home: Path
    ):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(
            switcher,
            {
                "1": {"email": "dup@example.com", "organizationUuid": "org-1"},
                "2": {"email": "dup@example.com", "organizationUuid": "org-2"},
            },
        )
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-dup@example.com"): "creds-1",
                (KEYRING_SERVICE, "account-2-dup@example.com"): "creds-2",
                (KEYRING_SERVICE, "account-None-dup@example.com"): "ambiguous",
            }
        )

        with _patch_keyring(fake):
            assert migrations.migrate_macos_keyring_to_security(switcher) is True

        # Both canonical entries relocated to the security service…
        assert switcher._read_account_credentials("1", "dup@example.com") == "creds-1"
        assert switcher._read_account_credentials("2", "dup@example.com") == "creds-2"
        # …their canonical keyring entries deleted…
        assert (KEYRING_SERVICE, "account-1-dup@example.com") in fake.deleted
        assert (KEYRING_SERVICE, "account-2-dup@example.com") in fake.deleted
        # …but the ambiguous account-None alias is PRESERVED (never deleted).
        assert (KEYRING_SERVICE, "account-None-dup@example.com") not in fake.deleted
        assert (KEYRING_SERVICE, "account-None-dup@example.com") in fake.store

    def test_none_alias_deleted_when_email_maps_to_single_slot(
        self, temp_home: Path
    ):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "solo@example.com"}})
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-solo@example.com"): "canonical",
                (KEYRING_SERVICE, "account-None-solo@example.com"): "stale-none",
            }
        )

        with _patch_keyring(fake):
            assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert switcher._read_account_credentials("1", "solo@example.com") == "canonical"
        # Unambiguous email → the redundant None alias is removed as cleanup.
        assert (KEYRING_SERVICE, "account-None-solo@example.com") in fake.deleted
        assert fake.store == {}
