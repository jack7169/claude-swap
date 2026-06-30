"""Locale/timeout robustness tests for get_process_start_time.

``ps -o lstart`` emits month/weekday names in the active LC_TIME locale, so a
non-English locale breaks the English ``strptime`` parse and silently disables
PID-reuse detection. We force a stable C locale for the ``ps`` invocation and
add a timeout so a wedged ``ps`` cannot hang the hot path.
"""

from __future__ import annotations

import subprocess
import time
from unittest.mock import patch

import pytest

from claude_swap.process_detection import get_process_start_time


class TestPsInvocationEnvAndTimeout:
    def test_ps_called_with_c_locale_and_finite_timeout(self):
        """The ps call must force LC_ALL=C/LANG=C and pass a finite timeout so
        lstart is always English-parseable and a wedged ps cannot hang."""
        completed = type(
            "P", (), {"returncode": 0, "stdout": "Mon Jun 29 19:35:56 2026"}
        )()
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.subprocess.run", return_value=completed
        ) as mock_run:
            get_process_start_time(1234)

        assert mock_run.call_count == 1
        kwargs = mock_run.call_args.kwargs

        env = kwargs.get("env")
        assert env is not None, "ps must be invoked with an explicit env"
        assert env.get("LC_ALL") == "C"
        assert env.get("LANG") == "C"

        timeout = kwargs.get("timeout")
        assert timeout is not None, "ps must be invoked with a timeout"
        assert timeout > 0

    def test_env_preserves_existing_environment(self):
        """Forcing the locale must not drop the rest of the environment (PATH
        etc. still needed to locate ps)."""
        completed = type(
            "P", (), {"returncode": 0, "stdout": "Mon Jun 29 19:35:56 2026"}
        )()
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.os.environ",
            {"PATH": "/usr/bin:/bin", "LC_ALL": "fr_FR.UTF-8"},
        ), patch(
            "claude_swap.process_detection.subprocess.run", return_value=completed
        ) as mock_run:
            get_process_start_time(1234)

        env = mock_run.call_args.kwargs["env"]
        assert env["PATH"] == "/usr/bin:/bin"
        # The inherited LC_ALL must be overridden, not merely merged.
        assert env["LC_ALL"] == "C"


class TestTimeoutExpired:
    def test_timeout_expired_returns_none(self):
        """A wedged ps that raises TimeoutExpired must return None (callers fall
        back to a plain liveness check) instead of propagating the exception."""
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=2),
        ):
            assert get_process_start_time(1234) is None


class TestCLocaleParsing:
    def test_parses_c_locale_lstart(self):
        """The English (C-locale) lstart string still parses to epoch seconds."""
        completed = type(
            "P", (), {"returncode": 0, "stdout": "Mon Jun 29 19:35:56 2026\n"}
        )()
        with patch("claude_swap.process_detection.sys.platform", "darwin"), patch(
            "claude_swap.process_detection.subprocess.run", return_value=completed
        ):
            result = get_process_start_time(1234)

        assert result is not None
        assert result == pytest.approx(
            time.mktime(
                time.strptime("Mon Jun 29 19:35:56 2026", "%a %b %d %H:%M:%S %Y")
            )
        )
