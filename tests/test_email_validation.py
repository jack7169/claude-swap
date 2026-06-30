"""Tests for trailing/embedded-newline rejection in email validation.

The regex used by ``_validate_email`` was anchored with ``$``, which in Python
matches *before* a trailing newline. An email like ``"user@example.com\n"``
therefore passed validation, and the newline then flowed into f-string backup
filenames (``.creds-N-EMAIL.enc`` etc.). The anchor must be ``\\Z`` (true end of
string) so a trailing or embedded newline is rejected.
"""

from __future__ import annotations

from pathlib import Path

from claude_swap.switcher import ClaudeAccountSwitcher


class TestTrailingNewlineRejected:
    """A trailing newline must be rejected (regression for the ``$`` anchor)."""

    def test_trailing_newline_rejected(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert not switcher._validate_email("user@example.com\n")

    def test_trailing_carriage_return_rejected(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert not switcher._validate_email("user@example.com\r")

    def test_trailing_crlf_rejected(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert not switcher._validate_email("user@example.com\r\n")


class TestEmbeddedNewlineRejected:
    """A newline embedded in the middle must also be rejected."""

    def test_embedded_newline_rejected(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert not switcher._validate_email("a@b\nc@d")

    def test_embedded_newline_with_valid_halves_rejected(self, temp_home: Path):
        # Each half on its own would be plausible; the whole string must fail.
        switcher = ClaudeAccountSwitcher()
        assert not switcher._validate_email("first@example.com\nsecond@example.com")


class TestNormalEmailsStillPass:
    """Valid emails without newlines must still validate."""

    def test_valid_emails_still_pass(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        valid_emails = [
            "user@example.com",
            "user.name@example.co.uk",
            "user+tag@example.org",
            "user123@test.io",
        ]
        for email in valid_emails:
            assert switcher._validate_email(email), f"Expected {email} to be valid"
