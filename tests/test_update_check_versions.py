"""Tests for PEP 440-aware version comparison and negative-cache TTL in update_check.

Phase 7.2: the naive ``_parse_version`` int-split crashed on pre-release/post/dev
versions (claude-swap itself ships betas like ``0.15.0b1``), defeating the update
check; and a failed PyPI fetch was cached as ``None`` for the full 24h, suppressing
all checks for a day after one transient error.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.update_check import (
    CACHE_TTL,
    _parse_version,
    check_for_update,
)


def _make_pypi_response(version: str) -> MagicMock:
    data = json.dumps({"info": {"version": version}}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _write_cache(path, version, timestamp=None):
    """Write a cache file in the shared cache format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": timestamp if timestamp is not None else time.time(),
                "data": version,
            }
        )
    )


class TestParseVersionPep440:
    """_parse_version must understand PEP 440, not just numeric dot-splits."""

    @pytest.mark.parametrize(
        "version",
        [
            "0.15.0b1",
            "1.3.0.post1",
            "1.3rc1",
            "1.3.0.dev1",
            "1!1.0.0",  # epoch
            "1.2.0a3",
            "2.0",
        ],
    )
    def test_does_not_crash_on_pep440(self, version):
        # The old int-split raised ValueError on any of these.
        result = _parse_version(version)
        assert result is not None

    def test_beta_orders_before_final(self):
        # The very beta this project ships must sort below the final release.
        assert _parse_version("0.15.0b1") < _parse_version("0.15.0")

    def test_patch_ordering(self):
        assert _parse_version("1.2.0") < _parse_version("1.2.1")

    def test_post_release_orders_after_final(self):
        assert _parse_version("1.3.0") < _parse_version("1.3.0.post1")

    def test_dev_orders_before_final(self):
        assert _parse_version("1.3.0.dev1") < _parse_version("1.3.0")

    def test_rc_orders_before_final(self):
        assert _parse_version("1.3rc1") < _parse_version("1.3")


class TestCheckForUpdatePep440:
    """The full check_for_update path must compare PEP 440 versions correctly."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_beta_current_sees_final_as_newer(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json"
        )
        mock_urlopen.return_value = _make_pypi_response("0.15.0")

        result = check_for_update("0.15.0b1")

        assert result is not None
        assert "0.15.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_final_current_ignores_older_beta(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json"
        )
        # PyPI reports a beta that is older than the installed final release.
        mock_urlopen.return_value = _make_pypi_response("0.15.0b1")

        result = check_for_update("0.15.0")

        assert result is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_garbage_latest_yields_no_update(self, mock_urlopen, tmp_path, monkeypatch):
        # A garbage version from PyPI must never produce a traceback in the
        # passive update notice — it just means "no update".
        monkeypatch.setattr(
            "claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json"
        )
        mock_urlopen.return_value = _make_pypi_response("not-a-version")

        result = check_for_update("0.3.2")

        assert result is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_garbage_current_yields_no_update(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json"
        )
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("garbage-local-build")

        assert result is None


class TestFailedFetchDoesNotPoisonCache:
    """A single transient PyPI failure must not suppress checks for the full 24h."""

    @patch(
        "claude_swap.update_check.urllib.request.urlopen",
        side_effect=OSError("network error"),
    )
    def test_failed_fetch_not_cached_for_long_ttl(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        assert result is None
        # If a failure was cached at all, it must NOT be cached at the long TTL:
        # the next run, hours later but within 24h, must still retry the network.
        if cache_path.exists():
            cache = json.loads(cache_path.read_text())
            assert cache["data"] is None
            # Backdate just past the short negative TTL but well within 24h.
            cache["timestamp"] = time.time() - (CACHE_TTL / 2)
            cache_path.write_text(json.dumps(cache))

        # A subsequent call must hit the network again rather than trust the
        # poisoned negative entry.
        with patch(
            "claude_swap.update_check.urllib.request.urlopen"
        ) as mock_retry:
            mock_retry.return_value = _make_pypi_response("0.4.0")
            retry_result = check_for_update("0.3.2")
            mock_retry.assert_called()

        assert retry_result is not None
        assert "0.4.0" in retry_result

    @patch(
        "claude_swap.update_check.urllib.request.urlopen",
        side_effect=OSError("network error"),
    )
    def test_successful_fetch_is_cached_for_long_ttl(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # A successful fetch should still be honoured for the full 24h window
        # (regression guard: the negative-TTL fix must not shorten success caching).
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        with patch(
            "claude_swap.update_check.urllib.request.urlopen"
        ) as mock_success:
            mock_success.return_value = _make_pypi_response("0.5.0")
            first = check_for_update("0.3.2")
            mock_success.assert_called_once()

        assert first is not None and "0.5.0" in first

        # Same data, aged most of a day but inside CACHE_TTL: must NOT refetch.
        cache = json.loads(cache_path.read_text())
        cache["timestamp"] = time.time() - (CACHE_TTL - 3600)
        cache_path.write_text(json.dumps(cache))

        with patch(
            "claude_swap.update_check.urllib.request.urlopen"
        ) as mock_again:
            second = check_for_update("0.3.2")
            mock_again.assert_not_called()

        assert second is not None and "0.5.0" in second
