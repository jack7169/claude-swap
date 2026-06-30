"""Robustness tests for FileLock (Phase 2 fix 2.5).

These cover edge cases beyond the basic happy path in test_locking.py:
- release() must reset state and close the fd even if the unlock syscall raises.
- A second acquire() on an already-held instance must not silently leak the fd
  or drop the prior lock.
- The lock file must not be truncated when it pre-exists with content.

POSIX-testable; Windows-specific assertions are gated on sys.platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claude_swap.locking import FileLock

if sys.platform != "win32":
    import fcntl


class TestFileLockRelease:
    """release() cleanup must be exception-safe."""

    def test_acquire_release_happy_path(self, tmp_path: Path):
        """Sanity: acquire then release leaves a clean state."""
        lock = FileLock(tmp_path / ".lock")

        assert lock.acquire(timeout=1.0) is True
        assert lock._locked is True
        assert lock._lock_file is not None

        lock.release()

        assert lock._locked is False
        assert lock._lock_file is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX unlock path")
    def test_release_resets_state_even_if_unlock_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If the unlock syscall raises OSError, state must still be reset and
        the fd closed (no leak)."""
        lock = FileLock(tmp_path / ".lock")
        assert lock.acquire(timeout=1.0) is True
        held_file = lock._lock_file
        assert held_file is not None

        real_flock = fcntl.flock

        def fake_flock(fd, operation):
            if operation == fcntl.LOCK_UN:
                raise OSError("boom on unlock")
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", fake_flock)

        # Must not propagate the OSError.
        lock.release()

        assert lock._lock_file is None
        assert lock._locked is False
        # The fd must have been closed despite the unlock failure.
        assert held_file.closed is True

    def test_double_release_after_unlock_failure_is_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A second release() after a failed unlock must not raise."""
        if sys.platform == "win32":
            pytest.skip("POSIX unlock path")
        lock = FileLock(tmp_path / ".lock")
        assert lock.acquire(timeout=1.0) is True

        def fake_flock(fd, operation):
            raise OSError("boom")

        monkeypatch.setattr(fcntl, "flock", fake_flock)
        lock.release()
        lock.release()  # Should not raise.


class TestFileLockReacquire:
    """A second acquire() on the same instance must not leak/drop."""

    def test_reacquire_while_held_does_not_replace_fd(self, tmp_path: Path):
        """Re-acquiring while already holding the lock must not silently swap
        the underlying fd (which would leak the old handle and drop the lock)."""
        lock = FileLock(tmp_path / ".lock")
        assert lock.acquire(timeout=1.0) is True
        first_file = lock._lock_file
        first_fileno = first_file.fileno()

        result = lock.acquire(timeout=1.0)

        # Whatever the policy (return True / no-op), the prior handle must not
        # be silently replaced by a brand-new open while still held.
        assert lock._locked is True
        assert result is True
        assert first_file.closed is False
        assert lock._lock_file is first_file
        assert lock._lock_file.fileno() == first_fileno

        lock.release()

    def test_reacquire_still_holds_lock_against_other_process_view(
        self, tmp_path: Path
    ):
        """After a re-acquire, another FileLock instance still can't grab it."""
        lock = FileLock(tmp_path / ".lock")
        assert lock.acquire(timeout=1.0) is True
        assert lock.acquire(timeout=1.0) is True

        other = FileLock(tmp_path / ".lock")
        assert other.acquire(timeout=0.3) is False

        lock.release()


class TestFileLockNoTruncate:
    """The lock file must not be truncated when it pre-exists with content."""

    def test_acquire_does_not_truncate_existing_content(self, tmp_path: Path):
        """A pre-existing lock file with content must survive acquire()."""
        lock_path = tmp_path / ".lock"
        lock_path.write_bytes(b"pre-existing content\n")
        original = lock_path.read_bytes()

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=1.0) is True
        try:
            # The on-disk content must not have been truncated to zero.
            assert lock_path.read_bytes() == original
        finally:
            lock.release()

        # Still intact after release.
        assert lock_path.read_bytes() == original

    def test_acquire_creates_file_when_missing(self, tmp_path: Path):
        """When the lock file doesn't exist yet, acquire() still creates it."""
        lock_path = tmp_path / "nested" / ".lock"
        assert not lock_path.exists()

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=1.0) is True
        try:
            assert lock_path.exists()
        finally:
            lock.release()

    @pytest.mark.skipif(
        sys.platform != "win32", reason="Windows locks a 1-byte region"
    )
    def test_lock_file_has_at_least_one_byte_on_windows(self, tmp_path: Path):
        """On Windows msvcrt.locking locks a 1-byte region, so the file must
        contain at least one byte after acquire() even if it was empty."""
        lock_path = tmp_path / ".lock"
        lock_path.write_bytes(b"")  # empty

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=1.0) is True
        try:
            assert lock_path.stat().st_size >= 1
        finally:
            lock.release()
