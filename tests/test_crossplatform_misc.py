"""Cross-platform robustness tests for Phase 6.4.

Two independent concerns, both gated on a *simulated* platform indicator so the
real OS (the macOS test runner) never matters:

6.4a — ``credentials.py`` ``os.replace`` swap on Windows.
    Claude Code may hold the target active-credential / config file open while
    cswap swaps in a new one; on Windows ``os.replace`` then raises
    ``PermissionError`` even though the move would otherwise succeed moments
    later. The writers wrap the ``os.replace`` in a small bounded retry **on
    Windows only** (a few attempts with a short ``time.sleep`` backoff) before
    giving up. POSIX keeps the single ``os.replace`` — no behavior change, a
    ``PermissionError`` propagates immediately.

6.4b — ``switcher._is_running_in_container`` on macOS.
    The Docker/cgroup probe is reached on every non-Windows platform, but
    ``/.dockerenv`` and the Linux ``/proc`` cgroup/mountinfo paths don't exist
    on macOS, so it must cleanly return ``False`` there and ``True`` only when a
    real container marker (``/.dockerenv``) is present.

Everything stays inside the conftest fixtures (redirected ``$HOME``, in-memory
Keychain/keyring fakes). No real ``security`` / ``osascript`` / ``subprocess``
/ network is touched: ``os.replace`` and ``time.sleep`` are monkeypatched, and
the container probe only consults monkeypatched path-existence checks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from claude_swap import credentials as credentials_mod
from claude_swap.credentials import CredentialStore
from claude_swap.models import Platform
from claude_swap.paths import get_claude_config_home, get_global_config_path
from claude_swap.switcher import ClaudeAccountSwitcher


class _Host:
    """Minimal data-only ``_StoreHost`` view for the credential store.

    The store reads ``platform`` / ``credentials_dir`` / ``_logger`` at call
    time. File-mode writers (``_write_active_credentials_file`` and
    ``_update_global_config``) don't touch the Keychain, so a non-macOS platform
    keeps the test purely on the file path.
    """

    def __init__(self, credentials_dir: Path):
        self.platform = Platform.LINUX
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("claude-swap-test-crossplatform")


def _make_store(tmp_path: Path) -> CredentialStore:
    creds_dir = tmp_path / "backup" / "credentials"
    creds_dir.mkdir(parents=True)
    return CredentialStore(_Host(creds_dir))


# -- 6.4a Windows os.replace retry --------------------------------------------


class _FlakyReplace:
    """``os.replace`` stand-in that raises ``PermissionError`` N times, then works.

    Delegates to the real ``os.replace`` once the failure budget is spent so the
    file actually lands. ``time.sleep`` is patched to a no-op in these tests so
    the bounded backoff costs nothing.
    """

    def __init__(self, fail_times: int):
        self.remaining_failures = fail_times
        self.calls = 0
        self._real_replace = os.replace

    def __call__(self, src, dst):
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise PermissionError("WinError 5: file held open by Claude Code")
        return self._real_replace(src, dst)


def test_write_active_credentials_file_retries_replace_on_windows(
    tmp_path: Path, temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """On Windows, ``os.replace`` raising PermissionError twice then succeeding
    must still land the active-credentials file (bounded retry)."""
    monkeypatch.setattr(credentials_mod.sys, "platform", "win32")
    monkeypatch.setattr(credentials_mod.time, "sleep", lambda *_a, **_k: None)
    flaky = _FlakyReplace(fail_times=2)
    monkeypatch.setattr(credentials_mod.os, "replace", flaky)

    store = _make_store(tmp_path)
    store._write_active_credentials_file('{"claudeAiOauth": "x"}')

    assert flaky.calls == 3  # 2 failures + 1 success
    cred_file = get_claude_config_home() / ".credentials.json"
    assert cred_file.read_text(encoding="utf-8") == '{"claudeAiOauth": "x"}'
    # No leftover temp file once the swap succeeds.
    leftover = list(get_claude_config_home().glob("*.tmp"))
    assert leftover == []


def test_update_global_config_retries_replace_on_windows(
    tmp_path: Path, temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """``_update_global_config`` benefits from the same Windows retry helper."""
    monkeypatch.setattr(credentials_mod.sys, "platform", "win32")
    monkeypatch.setattr(credentials_mod.time, "sleep", lambda *_a, **_k: None)
    flaky = _FlakyReplace(fail_times=2)
    monkeypatch.setattr(credentials_mod.os, "replace", flaky)

    store = _make_store(tmp_path)
    store._update_global_config(lambda cfg: cfg.__setitem__("primaryApiKey", "k"))

    assert flaky.calls == 3
    import json

    data = json.loads(get_global_config_path().read_text(encoding="utf-8"))
    assert data["primaryApiKey"] == "k"
    leftover = list(get_global_config_path().parent.glob("*.tmp"))
    assert leftover == []


def test_write_active_credentials_retry_is_bounded_on_windows(
    tmp_path: Path, temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """If ``os.replace`` keeps failing past the retry budget, the write re-raises
    and the temp file is unlinked (no orphaned ``.tmp``)."""
    monkeypatch.setattr(credentials_mod.sys, "platform", "win32")
    monkeypatch.setattr(credentials_mod.time, "sleep", lambda *_a, **_k: None)
    # Never succeeds — exhausts every attempt.
    flaky = _FlakyReplace(fail_times=10_000)
    monkeypatch.setattr(credentials_mod.os, "replace", flaky)

    store = _make_store(tmp_path)
    with pytest.raises(PermissionError):
        store._write_active_credentials_file('{"claudeAiOauth": "x"}')

    # Bounded: a small number of attempts, not thousands.
    assert 1 < flaky.calls <= 10
    # Failure still unlinks the temp file (existing behavior preserved).
    leftover = list(get_claude_config_home().glob("*.tmp"))
    assert leftover == []


def test_write_active_credentials_no_retry_on_posix(
    tmp_path: Path, temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """On POSIX the single ``os.replace`` path is unchanged: a PermissionError
    propagates on the first call (no retry) and the temp file is unlinked."""
    monkeypatch.setattr(credentials_mod.sys, "platform", "linux")
    # If the implementation ever slept on POSIX this would loudly fail the test.
    monkeypatch.setattr(
        credentials_mod.time,
        "sleep",
        lambda *_a, **_k: pytest.fail("must not retry/sleep on POSIX"),
    )
    flaky = _FlakyReplace(fail_times=2)
    monkeypatch.setattr(credentials_mod.os, "replace", flaky)

    store = _make_store(tmp_path)
    with pytest.raises(PermissionError):
        store._write_active_credentials_file('{"claudeAiOauth": "x"}')

    assert flaky.calls == 1  # single attempt, propagates immediately
    leftover = list(get_claude_config_home().glob("*.tmp"))
    assert leftover == []


def test_update_global_config_no_retry_on_posix(
    tmp_path: Path, temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """``_update_global_config`` keeps the single-replace POSIX behavior too."""
    monkeypatch.setattr(credentials_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        credentials_mod.time,
        "sleep",
        lambda *_a, **_k: pytest.fail("must not retry/sleep on POSIX"),
    )
    flaky = _FlakyReplace(fail_times=2)
    monkeypatch.setattr(credentials_mod.os, "replace", flaky)

    store = _make_store(tmp_path)
    with pytest.raises(Exception):  # CredentialWriteError wraps the PermissionError
        store._update_global_config(lambda cfg: cfg.__setitem__("primaryApiKey", "k"))

    assert flaky.calls == 1
    leftover = list(get_global_config_path().parent.glob("*.tmp"))
    assert leftover == []


# -- 6.4b _is_running_in_container on macOS / Linux ----------------------------


def _no_container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the CONTAINER env-var shortcut so the path probe is reached."""
    monkeypatch.delenv("CONTAINER", raising=False)
    monkeypatch.delenv("container", raising=False)


def test_is_running_in_container_false_on_macos_like_env(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """On a macOS-like environment none of the Linux/Docker markers exist, so the
    probe must cleanly return ``False`` (never misfire)."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    _no_container_env(monkeypatch)

    # macOS has no /.dockerenv and no /proc/* cgroup/mountinfo files.
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        s = str(self)
        if s == "/.dockerenv" or s.startswith("/proc/"):
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert switcher._is_running_in_container() is False


def test_is_running_in_container_true_when_dockerenv_present(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """When ``/.dockerenv`` exists the probe reports a container."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS  # reached on any non-Windows platform
    _no_container_env(monkeypatch)

    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/.dockerenv":
            return True
        if str(self).startswith("/proc/"):
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert switcher._is_running_in_container() is True


def test_is_running_in_container_survives_proc_read_error_on_macos(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """If a ``/proc`` marker claims to exist but reading it raises (e.g. an OSError
    that isn't PermissionError), the probe must not crash — it degrades to
    ``False`` rather than propagating."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    _no_container_env(monkeypatch)

    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/.dockerenv":
            return False
        if str(self).startswith("/proc/"):
            return True  # pretend the proc files exist
        return real_exists(self)

    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):
        if str(self).startswith("/proc/"):
            raise OSError("simulated unreadable /proc on a non-Linux kernel")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert switcher._is_running_in_container() is False
