"""Validation-consistency tests for the macOS ``security``-CLI wrapper.

``set_password`` already rejects account/service names containing control
characters (via ``_quote``, which matters for the ``security -i`` stdin command).
The read/delete/exists paths build argv lists directly, so they historically
skipped that check — a name rejected on write would be silently accepted on read
or delete. These tests pin the *consistent* behavior:

- ``get_password`` / ``delete_password`` raise :class:`KeychainError` on a name
  with a control char (matching ``set_password``);
- ``item_exists`` (deliberately non-raising) returns ``False`` on such a name;
- a normal name still works for all of them.

argv lists aren't a shell-injection vector, so this is robustness/consistency,
not a security hole. Like ``test_macos_keychain.py`` these mock ``subprocess.run``
to run the real wrapper bodies; they never invoke the real ``security`` binary.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain

# Drive the real wrapper bodies (mocking subprocess), so opt out of the in-memory
# Keychain guard that replaces these functions for other tests.
pytestmark = pytest.mark.no_keychain_fake


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["security"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# A representative spread of control characters that must be rejected the same
# way ``set_password`` rejects them: newline (would split a ``security -i`` line),
# carriage return, and a generic sub-0x20 byte (NUL).
_CONTROL_NAMES = ["bad\nname", "bad\rname", "bad\x00name", "bad\x01name"]


# ---------------------------------------------------------------------------
# get_password — must reject control chars *before* shelling out, like set_password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evil", _CONTROL_NAMES)
def test_get_password_rejects_control_char_in_account(evil):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="x\n")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.get_password("svc", evil)
        run.assert_not_called()  # rejected before any spawn


@pytest.mark.parametrize("evil", _CONTROL_NAMES)
def test_get_password_rejects_control_char_in_service(evil):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="x\n")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.get_password(evil, "acct")
        run.assert_not_called()


# ---------------------------------------------------------------------------
# delete_password — same rejection as set_password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evil", _CONTROL_NAMES)
def test_delete_password_rejects_control_char_in_account(evil):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.delete_password("svc", evil)
        run.assert_not_called()


@pytest.mark.parametrize("evil", _CONTROL_NAMES)
def test_delete_password_rejects_control_char_in_service(evil):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.delete_password(evil, "acct")
        run.assert_not_called()


# ---------------------------------------------------------------------------
# item_exists — deliberately non-raising: returns False on an invalid name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evil", _CONTROL_NAMES)
def test_item_exists_returns_false_on_control_char_account(evil):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        # Must NOT raise (item_exists feeds cleanup, not access decisions) and
        # must NOT shell out with an invalid name.
        assert macos_keychain.item_exists("svc", evil) is False
        run.assert_not_called()


@pytest.mark.parametrize("evil", _CONTROL_NAMES)
def test_item_exists_returns_false_on_control_char_service(evil):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        assert macos_keychain.item_exists(evil, "acct") is False
        run.assert_not_called()


# ---------------------------------------------------------------------------
# normal names still work for every operation (no regression)
# ---------------------------------------------------------------------------


def test_get_password_normal_name_still_works():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="the-secret\n")
        assert macos_keychain.get_password("svc", "acct") == "the-secret"
        run.assert_called_once()


def test_delete_password_normal_name_still_works():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.delete_password("svc", "acct")  # no raise
        run.assert_called_once()


def test_item_exists_normal_name_still_works():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        assert macos_keychain.item_exists("svc", "acct") is True
        run.assert_called_once()


# Names with a space (the active-credential service name contains one) are
# perfectly valid — only sub-0x20 control chars are rejected.
def test_space_in_name_is_not_a_control_char():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="x\n")
        assert macos_keychain.get_password("Claude Code", "the user") == "x"
        run.assert_called_once()
