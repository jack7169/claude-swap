"""Per-thread reentrant ``_sequence_lock`` tests (Phase: residual A & B).

``FileLock`` (locking.py) is a NON-reentrant cross-process advisory lock, but
several read-modify-write helpers on :class:`ClaudeAccountSwitcher` are reachable
from *inside* an already-held locked region — most importantly
``_migrate_org_fields`` (via ``_get_sequence_data_migrated``), which
``transfer.import_accounts`` triggers while it holds the lock. A naive
"``_migrate_org_fields`` acquires ``FileLock`` itself" would deadlock there.

The switcher therefore exposes a per-thread *reentrant* context manager,
``_sequence_lock()``: the first entry on a thread acquires the real ``FileLock``;
nested entries on the same thread just reuse it (depth counter). Cross-thread
acquisitions still serialize through the underlying ``FileLock``.

These tests cover:

* **Reentrancy** — nesting ``_sequence_lock`` doesn't hang and acquires the
  underlying ``FileLock`` exactly once.
* **Deadlock regression (fix A)** — migration triggered from inside a held lock
  completes (guarded by a worker thread + ``join(timeout=…)`` so a regression is
  a test failure, not a hung suite).
* **Cross-thread serialization** — two threads each mutating via the public API
  don't lose a record.
* **Fix B** — two concurrent no-email ``add_account_from_token`` calls land on
  two *distinct* slots, not a clobber.

The conftest autouse fixtures redirect ``$HOME`` and fake the macOS Keychain, so
nothing here spawns ``security`` / ``osascript`` / ``claude`` / real subprocesses
or hits the network. The real ``FileLock`` (plain ``fcntl``/``msvcrt`` on a temp
file) is used directly except where a counting stub is explicitly installed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import switcher as switcher_mod
from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CountingLock:
    """FileLock stand-in that counts how many times the REAL lock is acquired.

    Delegates to a real :class:`FileLock` (so cross-process semantics still
    hold) but bumps a class-level counter on every successful ``acquire``. With
    a reentrant ``_sequence_lock``, nesting on one thread must construct/acquire
    this at most once.
    """

    acquire_count = 0
    construct_count = 0

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        type(self).construct_count += 1
        self._inner = FileLock(lock_path, timeout=timeout)

    @classmethod
    def reset(cls) -> None:
        cls.acquire_count = 0
        cls.construct_count = 0

    def acquire(self, timeout: float | None = None) -> bool:
        ok = self._inner.acquire(timeout=timeout)
        if ok:
            type(self).acquire_count += 1
        return ok

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> "_CountingLock":
        if not self.acquire():
            from claude_swap.exceptions import LockError

            raise LockError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, *args) -> None:
        self.release()


def _seed_pre_org_accounts(switcher: ClaudeAccountSwitcher) -> None:
    """Write a sequence.json whose account records LACK organizationUuid.

    Mirrors a pre-v0.6.0 backup that would trigger ``_migrate_org_fields`` on
    the next ``_get_sequence_data_migrated`` call. Backup config/credential
    files are written too so the migration's per-account fallback can read them.
    """
    switcher._setup_directories()
    data = {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1, 2],
        "accounts": {
            "1": {
                "email": "one@example.com",
                "uuid": "uuid-1",
                "added": "2024-01-01T00:00:00Z",
            },
            "2": {
                "email": "two@example.com",
                "uuid": "uuid-2",
                "added": "2024-01-02T00:00:00Z",
            },
        },
    }
    switcher._write_json(switcher.sequence_file, data)
    for num, email in (("1", "one@example.com"), ("2", "two@example.com")):
        creds = json.dumps({"claudeAiOauth": {"accessToken": f"tok-{num}"}})
        config = json.dumps({"oauthAccount": {"emailAddress": email}})
        switcher._write_account_credentials(num, email, creds)
        switcher._write_account_config(num, email, config)


# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


class TestReentrancy:
    """Nested ``_sequence_lock`` reuses the held lock (no deadlock, one acquire)."""

    def test_nested_sequence_lock_does_not_hang(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        entered_inner = []

        def work() -> None:
            with switcher._sequence_lock():
                with switcher._sequence_lock():
                    entered_inner.append(True)

        t = threading.Thread(target=work)
        t.start()
        t.join(timeout=15)
        assert not t.is_alive(), "nested _sequence_lock hung (non-reentrant)"
        assert entered_inner == [True]

    def test_underlying_filelock_acquired_once_when_nested(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        _CountingLock.reset()
        with patch.object(switcher_mod, "FileLock", _CountingLock):
            with switcher._sequence_lock():
                with switcher._sequence_lock():
                    with switcher._sequence_lock():
                        pass

        assert _CountingLock.acquire_count == 1, (
            f"underlying FileLock acquired {_CountingLock.acquire_count} times "
            "for a nested _sequence_lock (expected exactly 1)"
        )
        assert _CountingLock.construct_count == 1

    def test_sequential_locks_each_acquire(self, temp_home: Path):
        """Two NON-nested entries on one thread each take the real lock."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        _CountingLock.reset()
        with patch.object(switcher_mod, "FileLock", _CountingLock):
            with switcher._sequence_lock():
                pass
            with switcher._sequence_lock():
                pass

        assert _CountingLock.acquire_count == 2

    def test_depth_reset_after_exit(self, temp_home: Path):
        """Depth must return to 0 so a later region re-acquires the real lock."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        with switcher._sequence_lock():
            with switcher._sequence_lock():
                pass
        # After fully exiting, depth must be 0 again.
        assert getattr(switcher._lock_state, "depth", 0) == 0


# ---------------------------------------------------------------------------
# Deadlock regression — fix A
# ---------------------------------------------------------------------------


class TestMigrationInsideHeldLock:
    """Migration reached from inside a held lock must COMPLETE (no deadlock)."""

    def test_migrate_inside_held_lock_completes(self, temp_home: Path):
        config_path = temp_home / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "one@example.com"}})
        )
        switcher = ClaudeAccountSwitcher()
        _seed_pre_org_accounts(switcher)

        done: list[bool] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                # Hold the lock, then trigger the org-field migration from
                # inside it (as transfer.import_accounts does). With a
                # non-reentrant lock this would deadlock.
                with switcher._sequence_lock():
                    switcher._get_sequence_data_migrated()
                done.append(True)
            except BaseException as e:  # noqa: BLE001 - surface in assertion
                errors.append(e)

        t = threading.Thread(target=work)
        t.start()
        t.join(timeout=15)

        assert not t.is_alive(), "migration inside a held lock DEADLOCKED"
        assert not errors, f"migration raised: {errors}"
        assert done == [True]

        # The migration actually backfilled organizationUuid.
        data = switcher._get_sequence_data()
        for acc in data["accounts"].values():
            assert "organizationUuid" in acc

    def test_import_accounts_with_pre_org_export_completes(self, temp_home: Path):
        """transfer.import_accounts (holds the lock, migrates inside) completes."""
        from claude_swap import transfer

        config_path = temp_home / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "one@example.com"}})
        )
        switcher = ClaudeAccountSwitcher()
        _seed_pre_org_accounts(switcher)

        # Craft a minimal export envelope adding a brand-new account. The
        # ``number`` must be an int and credentials/config are JSON objects
        # (transfer.FORMAT_VERSION == 1).
        envelope = {
            "version": 1,
            "type": "claude-swap-export",
            "activeAccountNumber": None,
            "accounts": [
                {
                    "number": 3,
                    "email": "three@example.com",
                    "uuid": "uuid-3",
                    "organizationUuid": "",
                    "organizationName": "",
                    "credentials": {"claudeAiOauth": {"accessToken": "tok-3"}},
                    "config": {"oauthAccount": {"emailAddress": "three@example.com"}},
                }
            ],
        }
        envelope_path = temp_home / "import.cswap"
        envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

        done: list[bool] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                transfer.import_accounts(switcher, str(envelope_path), force=False)
                done.append(True)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=work)
        t.start()
        t.join(timeout=20)

        assert not t.is_alive(), "import_accounts DEADLOCKED (migration inside lock)"
        assert not errors, f"import raised: {errors}"
        assert done == [True]

        data = switcher._get_sequence_data()
        # Pre-existing accounts migrated, and the new account landed.
        assert "3" in data["accounts"]
        assert data["accounts"]["3"]["email"] == "three@example.com"
        for acc in data["accounts"].values():
            assert "organizationUuid" in acc


# ---------------------------------------------------------------------------
# Cross-thread serialization still holds
# ---------------------------------------------------------------------------


class TestCrossThreadSerialization:
    """Two threads mutating via the public API don't lose a record."""

    def test_concurrent_adds_both_land(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        # Widen the read-modify-write window so two UNSERIALIZED adds would
        # clobber each other; the lock must serialize them.
        real_write_json = switcher._write_json

        def slow_write_json(path: Path, data: dict) -> None:
            if path == switcher.sequence_file:
                time.sleep(0.15)
            real_write_json(path, data)

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def add(slot: int, email: str) -> None:
            try:
                barrier.wait()
                switcher.add_account_from_token(
                    "setup-token-x", email=email, slot=slot
                )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        with patch.object(switcher, "_write_json", side_effect=slow_write_json), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            t1 = threading.Thread(target=add, args=(1, "a@example.com"))
            t2 = threading.Thread(target=add, args=(2, "b@example.com"))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

        assert not errors, f"add raised: {errors}"
        data = switcher._get_sequence_data()
        accounts = data["accounts"]
        assert "1" in accounts and accounts["1"]["email"] == "a@example.com"
        assert "2" in accounts and accounts["2"]["email"] == "b@example.com"
        assert set(data["sequence"]) == {int(n) for n in accounts}


# ---------------------------------------------------------------------------
# Fix B — concurrent no-email adds pick DISTINCT slots
# ---------------------------------------------------------------------------


class TestConcurrentNoEmailAddsDistinctSlots:
    """Two no-email ``add_account_from_token`` calls must not collide on a slot."""

    def test_two_no_email_token_adds_get_distinct_slots(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        # Widen the window between picking the next slot and writing it back so
        # an UNLOCKED slot pick (residual B) would hand both threads slot 1.
        real_write_json = switcher._write_json

        def slow_write_json(path: Path, data: dict) -> None:
            if path == switcher.sequence_file:
                time.sleep(0.15)
            real_write_json(path, data)

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def add() -> None:
            try:
                barrier.wait()
                # No email AND no slot -> the slot is synthesized inside the
                # lock; two concurrent calls must not pick the same number.
                switcher.add_account_from_token("setup-token-y", email=None, slot=None)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        with patch.object(switcher, "_write_json", side_effect=slow_write_json), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            t1 = threading.Thread(target=add)
            t2 = threading.Thread(target=add)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

        assert not errors, f"add raised: {errors}"
        data = switcher._get_sequence_data()
        accounts = data["accounts"]
        # Both adds must have landed on distinct slots (no clobber).
        assert len(accounts) == 2, (
            f"expected 2 distinct slots, got {sorted(accounts)} "
            "(concurrent no-email adds collided on one slot)"
        )
        emails = {acc["email"] for acc in accounts.values()}
        assert len(emails) == 2, (
            f"slots share a synthesized email {emails} — slot pick raced"
        )
        assert set(data["sequence"]) == {int(n) for n in accounts}

    def test_two_no_email_oauth_adds_get_distinct_slots(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        real_write_json = switcher._write_json

        def slow_write_json(path: Path, data: dict) -> None:
            if path == switcher.sequence_file:
                time.sleep(0.15)
            real_write_json(path, data)

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        def add() -> None:
            try:
                barrier.wait()
                switcher.add_account_from_oauth(
                    credentials=creds, email=None, slot=None
                )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        with patch.object(switcher, "_write_json", side_effect=slow_write_json), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            t1 = threading.Thread(target=add)
            t2 = threading.Thread(target=add)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

        assert not errors, f"add raised: {errors}"
        data = switcher._get_sequence_data()
        accounts = data["accounts"]
        assert len(accounts) == 2, (
            f"expected 2 distinct slots, got {sorted(accounts)} "
            "(concurrent no-email oauth adds collided on one slot)"
        )
        emails = {acc["email"] for acc in accounts.values()}
        assert len(emails) == 2, f"slots share a synthesized email {emails}"
