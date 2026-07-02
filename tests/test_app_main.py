"""Tests for the bundled-app entry point (menubar.run mocked; no rumps/AppKit)."""

from __future__ import annotations

from claude_swap import app_main, menubar, notify


def test_main_runs_menubar_with_a_switcher(monkeypatch):
    captured = {}

    def fake_run(switcher):
        captured["switcher"] = switcher
        return 0

    monkeypatch.setattr(menubar, "run", fake_run)
    monkeypatch.setattr(notify, "wire_switch_notifier", lambda sw: None)
    rc = app_main.main()
    assert rc == 0
    # A real ClaudeAccountSwitcher was constructed and handed to run().
    assert type(captured["switcher"]).__name__ == "ClaudeAccountSwitcher"


def test_main_wires_swap_notifier(monkeypatch):
    # The .app entry must wire the swap notifier (the CLI does it in cli.main;
    # the bundle bypasses the CLI, so without this, switch notifications are dead).
    wired = []
    monkeypatch.setattr(notify, "wire_switch_notifier", lambda sw: wired.append(sw))
    monkeypatch.setattr(menubar, "run", lambda sw: 0)
    app_main.main()
    assert len(wired) == 1
    assert type(wired[0]).__name__ == "ClaudeAccountSwitcher"
