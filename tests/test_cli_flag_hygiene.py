"""Flag-hygiene tests for the CLI (Phases 8.2 and 8.3).

8.2 (R2F17): the ``--json`` guard must gate on *presence* of ``--switch-to``
(``is not None``), not truthiness, so an empty-string ``--switch-to ""`` is a
valid combo and is not wrongly rejected.

8.3 (R2F16): an inline ``--add-token <secret>`` exposes the secret in argv to
other local processes; the CLI warns on stderr (never stdout) when a token is
passed inline, but stays silent for the stdin (``-``) and interactive-prompt
(no value) forms.

These invoke ``cli.main()`` with a patched ``sys.argv`` and a mocked
``ClaudeAccountSwitcher`` / switcher methods, mirroring the existing CLI tests,
and capture stdout/stderr.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from claude_swap import cli


class TestJsonGuardPresenceNotTruthiness:
    """8.2 (R2F17): the --json guard keys on presence, not truthiness."""

    def test_switch_to_empty_string_with_json_passes_guard_and_dispatches(self):
        """``--switch-to "" --json`` must NOT trip the --json guard; it proceeds
        to dispatch switch_to("") with json_output=True (regression: the guard
        used truthiness on the string flag, wrongly rejecting an empty value)."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch-to", "", "--json"]), \
             patch("os.geteuid", return_value=1000):
            switcher_cls.return_value.switch_to.return_value = {"schemaVersion": 1}
            # If the guard fired it would raise SystemExit(2) before dispatch.
            cli.main()

        switcher_cls.return_value.switch_to.assert_called_once_with(
            "", json_output=True
        )

    def test_list_with_json_is_a_valid_combo(self):
        """A non-empty valid combo (--list --json) still works."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--list", "--json"]), \
             patch("os.geteuid", return_value=1000):
            switcher_cls.return_value.list_accounts.return_value = {"schemaVersion": 1}
            cli.main()

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=False, json_output=True
        )

    def test_status_with_json_is_a_valid_combo(self):
        """--status --json still works."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--status", "--json"]), \
             patch("os.geteuid", return_value=1000):
            switcher_cls.return_value.status.return_value = {"schemaVersion": 1}
            cli.main()

        switcher_cls.return_value.status.assert_called_once_with(json_output=True)

    def test_switch_with_json_is_a_valid_combo(self):
        """--switch --json still works."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch", "--json"]), \
             patch("os.geteuid", return_value=1000):
            switcher_cls.return_value.switch.return_value = {"schemaVersion": 1}
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=True
        )

    def test_add_account_with_json_is_still_rejected(self, capsys):
        """--json with a non-JSON action (--add-account) is still rejected by the
        guard (the fix must not loosen the allowed set)."""
        with patch.object(sys, "argv", ["claude-swap", "--add-account", "--json"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--json can only be used with" in capsys.readouterr().err


class TestAddTokenArgvExposureWarning:
    """8.3 (R2F16): warn when a token is passed inline on the command line."""

    def test_inline_token_warns_on_stderr_not_stdout(self, capsys):
        """``--add-token sk-ant-oat01-XXXX`` emits the argv-exposure warning to
        STDERR (and the warning text is not on stdout). Token handling itself is
        unchanged: the switcher still receives the raw token."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(
                 sys, "argv",
                 ["claude-swap", "--add-token", "sk-ant-oat01-XXXX"],
             ), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        out, err = capsys.readouterr()
        # The token handling path is untouched: the switcher still gets the raw value.
        switcher_cls.return_value.add_account_from_token.assert_called_once_with(
            token="sk-ant-oat01-XXXX", email=None, slot=None
        )
        # Warning lands on stderr and mentions the argv/process exposure + the
        # safer alternatives.
        assert "warning" in err.lower()
        assert "argv" in err.lower() or "command line" in err.lower() or "process" in err.lower()
        assert "--add-token -" in err
        # The secret itself must not be echoed, and the warning must not pollute stdout.
        assert "sk-ant-oat01-XXXX" not in err
        assert "warning" not in out.lower()

    def test_stdin_token_does_not_warn(self, capsys):
        """``--add-token -`` (stdin) does NOT emit the argv-exposure warning."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--add-token", "-"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        out, err = capsys.readouterr()
        switcher_cls.return_value.add_account_from_token.assert_called_once_with(
            token="-", email=None, slot=None
        )
        assert "warning" not in err.lower()
        assert "warning" not in out.lower()

    def test_prompt_token_does_not_warn(self, capsys):
        """``--add-token`` with no value (interactive prompt, const="") does NOT
        emit the argv-exposure warning."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--add-token"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        out, err = capsys.readouterr()
        switcher_cls.return_value.add_account_from_token.assert_called_once_with(
            token="", email=None, slot=None
        )
        assert "warning" not in err.lower()
        assert "warning" not in out.lower()
