"""Concurrency / locking tests for the account mutators (Phase 2).

These cover the lost-update races fixed in Phase 2:

* 2.1 — ``add_account`` / ``add_account_from_token`` / ``add_account_from_oauth``
  / ``remove_account`` must take the cross-process :class:`FileLock` around their
  ``sequence.json`` read-modify-write, re-reading inside the lock, while keeping
  every ``input()`` / ``getpass`` prompt *outside* the lock.
* 2.2 — ``run_migrations`` must run its backend-mutating pass under the lock.
* 2.6 — ``_active_account_usage``'s cache read-modify-write must be serialized,
  and ``_print_switch_followup`` must use the credential backend captured *inside*
  the switch lock (not re-read after release).

The conftest autouse fixtures redirect ``$HOME`` to a temp dir and replace the
macOS Keychain with an in-memory fake, so nothing here spawns ``security`` /
``osascript`` / ``claude`` / real subprocesses or hits the network. The real
``FileLock`` (plain ``fcntl``/``msvcrt`` on a temp file) is used directly.
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


class _RecordingLock:
    """FileLock stand-in that records acquire/release ordering on a shared log.

    Delegates to a real :class:`FileLock` so cross-process semantics still hold,
    but appends ``("acquire", path)`` / ``("release", path)`` events to a shared
    list and tracks a live held-count so tests can assert ordering (e.g. that no
    ``input()`` ran while the lock was held).
    """

    events: list[tuple[str, str]] = []
    held: dict[str, int] = {}

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        self._inner = FileLock(lock_path, timeout=timeout)
        self._key = str(lock_path)

    @classmethod
    def reset(cls) -> None:
        cls.events = []
        cls.held = {}

    @classmethod
    def is_held(cls, key: str | None = None) -> bool:
        if key is None:
            return any(v > 0 for v in cls.held.values())
        return cls.held.get(key, 0) > 0

    @classmethod
    def acquisitions(cls) -> int:
        return sum(1 for kind, _ in cls.events if kind == "acquire")

    def acquire(self, timeout: float | None = None) -> bool:
        ok = self._inner.acquire(timeout=timeout)
        if ok:
            type(self).events.append(("acquire", self._key))
            type(self).held[self._key] = type(self).held.get(self._key, 0) + 1
        return ok

    def release(self) -> None:
        if self._inner._locked:
            type(self).events.append(("release", self._key))
            type(self).held[self._key] = type(self).held.get(self._key, 0) - 1
        self._inner.release()

    def __enter__(self) -> "_RecordingLock":
        if not self.acquire():
            from claude_swap.exceptions import LockError

            raise LockError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, *args) -> None:
        self.release()


def _seed_two_accounts(switcher: ClaudeAccountSwitcher) -> None:
    """Write a sequence.json with two managed slots plus their backup files."""
    switcher._setup_directories()
    data = {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1, 2],
        "accounts": {
            "1": {
                "email": "one@example.com",
                "uuid": "uuid-1",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2024-01-01T00:00:00Z",
            },
            "2": {
                "email": "two@example.com",
                "uuid": "uuid-2",
                "organizationUuid": "",
                "organizationName": "",
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
# 2.1 — mutators acquire the lock
# ---------------------------------------------------------------------------


class TestMutatorsAcquireLock:
    """Each account mutator must take ``self.lock_file`` around its RMW region."""

    def test_add_account_from_token_acquires_lock(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        _RecordingLock.reset()
        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("setup-token-abc", email=None, slot=None)

        assert _RecordingLock.acquisitions() >= 1
        # The lock taken is the switcher's own lock_file (not some other path).
        assert any(
            key == str(switcher.lock_file)
            for _, key in _RecordingLock.events
        )

    def test_add_account_from_oauth_acquires_lock(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        _RecordingLock.reset()
        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_oauth(
                credentials=creds, email="oauth@example.com", slot=None
            )

        assert _RecordingLock.acquisitions() >= 1

    def test_add_account_acquires_lock(self, temp_home: Path):
        config_path = temp_home / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "live@example.com"}})
        )
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        _RecordingLock.reset()
        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch.object(switcher, "_read_credentials", return_value=creds), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account()

        assert _RecordingLock.acquisitions() >= 1

    def test_remove_account_acquires_lock(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        _seed_two_accounts(switcher)

        _RecordingLock.reset()
        with patch.object(switcher_mod, "FileLock", _RecordingLock):
            switcher.remove_account("2", force=True)

        assert _RecordingLock.acquisitions() >= 1
        data = switcher._get_sequence_data()
        assert "2" not in data["accounts"]


class TestPromptsOutsideLock:
    """Interactive prompts must NOT run while the FileLock is held."""

    def test_remove_account_prompt_outside_lock(self, temp_home: Path):
        """The confirmation ``input()`` must happen before the lock is taken."""
        switcher = ClaudeAccountSwitcher()
        _seed_two_accounts(switcher)

        _RecordingLock.reset()
        captured_held: list[bool] = []

        def fake_input(prompt: str = "") -> str:
            captured_held.append(_RecordingLock.is_held(str(switcher.lock_file)))
            return "y"

        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch("builtins.input", fake_input):
            switcher.remove_account("2", force=False)

        assert captured_held, "input() was never called"
        assert not any(captured_held), "input() ran while the FileLock was held"

    def test_add_token_overwrite_prompt_outside_lock(self, temp_home: Path):
        """``--add-token --slot N`` occupied-slot prompt must precede the lock."""
        switcher = ClaudeAccountSwitcher()
        _seed_two_accounts(switcher)

        _RecordingLock.reset()
        captured_held: list[bool] = []

        def fake_input(prompt: str = "") -> str:
            captured_held.append(_RecordingLock.is_held(str(switcher.lock_file)))
            return "y"

        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch("builtins.input", fake_input), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"), \
             patch.object(switcher, "_delete_account_files"):
            # Slot 1 is occupied by one@example.com; adding a token with a
            # different email at slot 1 must prompt to overwrite.
            switcher.add_account_from_token(
                "setup-token-xyz", email="brand-new@example.com", slot=1
            )

        assert captured_held, "input() was never called"
        assert not any(captured_held), "input() ran while the FileLock was held"

    def test_add_token_getpass_outside_lock(self, temp_home: Path):
        """``getpass`` (empty-token prompt) must run before the lock is taken."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        _RecordingLock.reset()
        captured_held: list[bool] = []

        def fake_getpass(prompt: str = "") -> str:
            captured_held.append(_RecordingLock.is_held(str(switcher.lock_file)))
            return "setup-token-secret"

        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch("getpass.getpass", fake_getpass), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("", email=None, slot=None)

        assert captured_held, "getpass was never called"
        assert not any(captured_held), "getpass ran while the FileLock was held"


# ---------------------------------------------------------------------------
# 2.1 — lost-update race is serialized by the real lock
# ---------------------------------------------------------------------------


class TestNoLostUpdate:
    """Two concurrent mutators must not clobber each other's sequence.json write."""

    def test_concurrent_add_and_remove_keep_both_mutations(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        _seed_two_accounts(switcher)

        # Force an interleaving window: between the read and write of
        # sequence.json, sleep briefly so two unlocked mutators would both
        # read the *original* state and one would clobber the other. With the
        # FileLock around the read-modify-write, they serialize and both land.
        real_write_json = switcher._write_json

        def slow_write_json(path: Path, data: dict) -> None:
            if path == switcher.sequence_file:
                time.sleep(0.15)
            real_write_json(path, data)

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        # Add at an explicit high slot so it never reuses the slot the removal
        # frees — that keeps "both mutations landed" unambiguous regardless of
        # which thread wins the lock first.
        def do_add() -> None:
            try:
                barrier.wait()
                switcher.add_account_from_token(
                    "setup-token-new", email="added@example.com", slot=5
                )
            except BaseException as e:  # noqa: BLE001 - surface in assertion
                errors.append(e)

        def do_remove() -> None:
            try:
                barrier.wait()
                switcher.remove_account("2", force=True)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        with patch.object(switcher, "_write_json", side_effect=slow_write_json), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"), \
             patch.object(switcher, "_delete_account_files"):
            t_add = threading.Thread(target=do_add)
            t_remove = threading.Thread(target=do_remove)
            t_add.start()
            t_remove.start()
            t_add.join(timeout=30)
            t_remove.join(timeout=30)

        assert not errors, f"mutator raised: {errors}"

        data = switcher._get_sequence_data()
        accounts = data["accounts"]
        # The removal must have stuck (slot 2 gone)...
        assert "2" not in accounts, "remove was lost (clobbered by concurrent add)"
        # ...and the new account must have stuck (slot 5 present)...
        assert "5" in accounts and accounts["5"]["email"] == "added@example.com", (
            "add was lost (clobbered by concurrent remove)"
        )
        # ...and the untouched account 1 must survive both.
        assert "1" in accounts, "unrelated account 1 was clobbered"
        # sequence list stays consistent with accounts.
        assert set(data["sequence"]) == {int(n) for n in accounts}


# ---------------------------------------------------------------------------
# 2.2 — migrations run under the lock
# ---------------------------------------------------------------------------


class TestMigrationsUnderLock:
    """``run_migrations`` must hold the lock while a migration mutates backends."""

    def test_run_migrations_holds_lock_during_migration(self, temp_home: Path):
        from claude_swap import migrations as migrations_mod

        switcher = ClaudeAccountSwitcher()
        switcher.backup_dir.mkdir(parents=True, exist_ok=True)

        _RecordingLock.reset()
        held_during_migration: list[bool] = []

        def fake_migration(sw) -> bool:
            held_during_migration.append(_RecordingLock.is_held(str(sw.lock_file)))
            return False  # not applicable → nothing recorded

        # run_migrations now takes the lock via switcher._sequence_lock(), which
        # constructs the real lock through switcher_mod.FileLock — patch there.
        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch.object(
                 migrations_mod, "MIGRATIONS", [("fake_mig", fake_migration)]
             ):
            migrations_mod.run_migrations(switcher)

        assert held_during_migration, "migration never ran"
        assert all(held_during_migration), "migration ran without holding the lock"

    def test_run_migrations_lock_failure_does_not_raise(self, temp_home: Path):
        """A lock-acquire failure must be swallowed (construction must not break)."""
        from claude_swap import migrations as migrations_mod
        from claude_swap.exceptions import LockError

        switcher = ClaudeAccountSwitcher()
        switcher.backup_dir.mkdir(parents=True, exist_ok=True)

        class _FailingLock:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise LockError("busy")

            def __exit__(self, *a):
                return False

        def fake_migration(sw) -> bool:
            return False

        with patch.object(switcher_mod, "FileLock", _FailingLock), \
             patch.object(
                 migrations_mod, "MIGRATIONS", [("fake_mig", fake_migration)]
             ):
            # Must not raise — run_migrations is contractually "never raises".
            migrations_mod.run_migrations(switcher)


# ---------------------------------------------------------------------------
# 2.6 — read-after-unlock races
# ---------------------------------------------------------------------------


class TestActiveAccountUsageLockedCacheWrite:
    """``_active_account_usage`` cache RMW must be serialized by the lock."""

    def test_cache_write_holds_lock(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        _RecordingLock.reset()
        held_during_write: list[bool] = []

        from claude_swap import cache as cache_mod

        real_write_cache = cache_mod.write_cache

        def spy_write_cache(path, data):
            held_during_write.append(_RecordingLock.is_held(str(switcher.lock_file)))
            return real_write_cache(path, data)

        creds = json.dumps({"claudeAiOauth": {"accessToken": "live-tok"}})
        with patch.object(switcher_mod, "FileLock", _RecordingLock), \
             patch.object(switcher_mod, "write_cache", spy_write_cache), \
             patch.object(switcher, "_read_credentials", return_value=creds), \
             patch.object(
                 switcher, "_fetch_active_usage", return_value={"some": "usage"}
             ):
            switcher._active_account_usage("1", "live@example.com")

        assert held_during_write, "cache was never written"
        assert all(held_during_write), "cache write raced outside the lock"

    def test_concurrent_active_usage_writes_do_not_corrupt_cache(
        self, temp_home: Path
    ):
        """Two threads writing different slots' usage must both survive."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        usage_cache_path = switcher.backup_dir / "cache" / "usage.json"

        from claude_swap import cache as cache_mod

        real_write_cache = cache_mod.write_cache

        def slow_write_cache(path, data):
            time.sleep(0.1)
            real_write_cache(path, data)

        creds = json.dumps({"claudeAiOauth": {"accessToken": "live-tok"}})
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def fetch_for(slot):
            def _fetch(num, email, c):
                return {"slot": slot}

            return _fetch

        def run(slot, email):
            try:
                barrier.wait()
                with patch.object(
                    switcher, "_read_credentials", return_value=creds
                ), patch.object(
                    switcher, "_fetch_active_usage", side_effect=fetch_for(slot)
                ):
                    switcher._active_account_usage(slot, email)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        with patch.object(switcher_mod, "write_cache", slow_write_cache):
            t1 = threading.Thread(target=run, args=("1", "one@example.com"))
            t2 = threading.Thread(target=run, args=("2", "two@example.com"))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

        assert not errors, f"raised: {errors}"
        raw = json.loads(usage_cache_path.read_text(encoding="utf-8"))
        cached = raw["data"]
        # Both slots' usage must be present — neither write clobbered the other.
        assert "1" in cached and "2" in cached, cached


class TestSwitchFollowupBackendCapturedInLock:
    """``_print_switch_followup`` must use the backend captured inside the lock."""

    def test_followup_uses_backend_captured_inside_lock(self, temp_home: Path):
        config_path = temp_home / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "one@example.com"}})
        )
        switcher = ClaudeAccountSwitcher()
        _seed_two_accounts(switcher)

        # The active write lands on the file backend during the switch. A
        # concurrent switch then flips _last_active_credentials_backend to
        # "keychain" *after* the lock is released but before the followup runs.
        # The followup must reflect the value captured inside the lock ("file"),
        # not the clobbered shared field.
        switcher._last_active_credentials_backend = "file"

        def flip_backend_after_lock(*args, **kwargs):
            # Simulate a concurrent switch clobbering the shared field after the
            # locked region but before the followup prints.
            switcher._last_active_credentials_backend = "keychain"

        target_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok-2"}})
        target_config = json.dumps({"oauthAccount": {"emailAddress": "two@example.com"}})

        with patch.object(switcher, "_read_credentials", return_value=json.dumps(
                {"claudeAiOauth": {"accessToken": "tok-1"}})), \
             patch.object(switcher, "_write_credentials"), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=target_creds
             ), \
             patch.object(
                 switcher, "_read_account_config", return_value=target_config
             ), \
             patch.object(
                 switcher, "list_accounts", side_effect=flip_backend_after_lock
             ), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                switcher._perform_switch_locked("2", emit_output=True)
            out = buf.getvalue()

        # File-backend followup wording; must NOT show the keychain ~30s wording
        # even though the shared field was flipped to "keychain" after the lock.
        assert "next message" in out, out
        assert "~30 seconds" not in out, out
