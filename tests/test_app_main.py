"""Tests for the bundled-app entry point (menubar.run mocked; no rumps/AppKit)."""

from __future__ import annotations

from claude_swap import app_main, menubar


def test_main_runs_menubar_with_a_switcher(monkeypatch):
    captured = {}

    def fake_run(switcher):
        captured["switcher"] = switcher
        return 0

    monkeypatch.setattr(menubar, "run", fake_run)
    rc = app_main.main()
    assert rc == 0
    # A real ClaudeAccountSwitcher was constructed and handed to run().
    assert type(captured["switcher"]).__name__ == "ClaudeAccountSwitcher"
