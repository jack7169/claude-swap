"""Tests for the unified swap notifier (claude_swap.notify).

These never spawn a real ``osascript``: ``subprocess.run`` is patched so the
darwin path is exercised without touching Notification Center.
"""

from __future__ import annotations

from claude_swap import notify


def test_build_notification_script_basic():
    assert notify._build_notification_script("claude-swap", "Switched to 2") == (
        'display notification "Switched to 2" with title "claude-swap"'
    )


def test_build_notification_script_escapes_quotes_and_backslashes():
    bs = chr(92)  # a single backslash, spelled out to avoid literal confusion
    q = '"'
    title = "a" + q + "b"            # a"b
    message = "c" + bs + q + "e"     # c\"e
    script = notify._build_notification_script(title, message)
    # Escaping: each backslash -> two backslashes, then each quote -> \"
    esc_message = "c" + bs + bs + bs + q + "e"   # c \\ \" e
    esc_title = "a" + bs + q + "b"               # a \" b
    assert script == (
        f'display notification "{esc_message}" with title "{esc_title}"'
    )


def test_build_notification_script_collapses_newlines():
    """A stray newline (e.g. an email/identity parsed with a trailing newline)
    must not render the Notification Center alert as broken multiple lines —
    osascript accepts a raw newline inside the string literal and stacks it onto
    separate lines. Control whitespace collapses to single spaces.
    """
    script = notify._build_notification_script("t", "line1\nline2")
    assert "\n" not in script
    assert script == 'display notification "line1 line2" with title "t"'


def test_build_notification_script_collapses_all_control_whitespace():
    script = notify._build_notification_script("a\tb", "x\r\ny\tz")
    assert "\n" not in script and "\r" not in script and "\t" not in script
    assert script == 'display notification "x y z" with title "a b"'


def test_notify_noop_off_darwin(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: called.append(a))
    notify.notify("t", "m")
    assert called == []  # never spawns osascript off macOS


def test_notify_runs_osascript_on_darwin(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    notify.notify("claude-swap", "hi")
    assert len(calls) == 1
    argv = calls[0][0][0]
    assert argv[0] == "/usr/bin/osascript"  # absolute path, PATH-injection-proof
    assert argv[1] == "-e"
    assert argv[2] == notify._build_notification_script("claude-swap", "hi")
    assert calls[0][1].get("timeout") == 5


def test_notify_never_raises(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "darwin")

    def boom(*a, **k):
        raise OSError("osascript missing")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    notify.notify("t", "m")  # must not raise
