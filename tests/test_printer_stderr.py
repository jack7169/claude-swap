"""Tests for the printer module's stdout/stderr discipline.

Diagnostics (warnings, errors) must go to stderr so they never pollute the
machine-readable --json contract on stdout. Informational output stays on
stdout.
"""

from __future__ import annotations

import pytest

from claude_swap import printer


@pytest.fixture(autouse=True)
def _reset_color_cache():
    """Reset the color detection cache before each test."""
    printer._colors_enabled = None
    yield
    printer._colors_enabled = None


class TestDiagnosticsOnStderr:
    """Diagnostic emitters must write to stderr, not stdout."""

    def test_warning_writes_to_stderr_not_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.warning("be careful")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "be careful" in captured.err

    def test_warning_with_color_on_stderr(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer.warning("be careful")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "\033[33m" in captured.err

    def test_error_writes_to_stderr_not_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.error("something failed")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "something failed" in captured.err

    def test_error_with_color_on_stderr(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer.error("something failed")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "\033[31m" in captured.err


class TestInformationalStaysOnStdout:
    """Inline stylers return strings; printing them keeps data on stdout."""

    def test_accent_print_path_stays_on_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        # Inline stylers return a plain string; emitting it via print() (the
        # normal informational path) must land on stdout, not stderr.
        print(printer.accent("account info"))
        captured = capsys.readouterr()
        assert "account info" in captured.out
        assert captured.err == ""
