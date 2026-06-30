"""Robustness tests for oauth/oauth_login timestamp + response parsing.

These cover the "total parsing" hardening from Phase 5.2: a single bad field
(naive timestamp, out-of-range expiresAt, missing utilization, malformed token
response) must degrade gracefully rather than crash the whole account's usage
or token-status listing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from claude_swap import oauth, oauth_login


class TestFormatResetNaiveTimestamp:
    """format_reset must tolerate a NAIVE ISO string (no tzinfo).

    A naive datetime minus the tz-aware ``now`` raises TypeError, which would
    propagate out of build_usage_result and void the whole account's usage.
    """

    def test_naive_iso_string_does_not_raise(self):
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        # No tzinfo on the reset time — two hours ahead of now (in UTC terms).
        naive_future = "2026-03-23T14:30:00"
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(naive_future)

        # Treated as UTC: 14:30 - 12:00 = 2h 30m.
        assert countdown == "2h 30m"
        assert isinstance(clock, str)
        assert clock  # non-empty clock string


class TestBuildTokenStatusOutOfRange:
    """build_token_status must not crash on an absurd expiresAt."""

    def test_absurd_expires_at_returns_unknown_expiry(self):
        # 10**18 ms is far beyond what datetime.fromtimestamp can represent;
        # the computation must fall back to the 'unknown expiry' string.
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 10 ** 18,
            }
        })

        status = oauth.build_token_status(credentials)

        assert status == "oauth: unknown expiry, refresh token yes"


class TestBuildUsageResultMissingUtilization:
    """build_usage_result must skip a window missing 'utilization', not crash."""

    def test_window_missing_utilization_is_skipped_others_returned(self):
        data = {
            # five_hour has no "utilization" key -> must be skipped.
            "five_hour": {"resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
        }

        result = oauth.build_usage_result(data)

        assert result is not None
        assert "five_hour" not in result
        assert result["seven_day"]["pct"] == 61.0

    def test_all_windows_missing_utilization_returns_none(self):
        data = {
            "five_hour": {"resets_at": None},
            "seven_day": {"resets_at": None},
        }

        result = oauth.build_usage_result(data)

        assert result is None


class TestRefreshOAuthCredentialsMalformed200:
    """refresh_oauth_credentials must not partially mutate on a malformed 200."""

    @staticmethod
    def _make_credentials():
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 12345,
                "scopes": ["user:profile"],
            }
        })

    def test_200_missing_access_token_returns_none_without_mutating(self):
        # A 200 response that omits access_token / expires_in must yield None
        # and must NOT half-update the oauth dict.
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            # No access_token, no expires_in.
            "refresh_token": "new-refresh",
            "scope": "user:profile user:inference",
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        creds = self._make_credentials()
        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            result = oauth.refresh_oauth_credentials(creds)

        assert result is None

    def test_200_missing_expires_in_does_not_set_access_token(self):
        # The response has a valid access_token but no expires_in. With direct
        # indexing the old code would set oauth["accessToken"] = "new-access"
        # and only then KeyError on expires_in. Validation must bail BEFORE any
        # mutation, so probing the validation path can't have applied the token.
        captured = {}

        def mock_urlopen(req, timeout=0):
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                "access_token": "new-access",
                # No expires_in.
            }).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        creds = self._make_credentials()
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.refresh_oauth_credentials(creds)

        assert result is None
        # Sanity: the function returns None, never a dict carrying the new token
        # paired with the stale (un-updated) expiry.
        assert result is None or "new-access" not in result
        assert captured == {}

    def test_200_missing_expires_in_returns_none(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new-access",
            # No expires_in.
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        creds = self._make_credentials()
        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            result = oauth.refresh_oauth_credentials(creds)

        assert result is None


class TestCredentialsFromTokenResponseBadExpiresIn:
    """credentials_from_token_response must tolerate a non-numeric expires_in."""

    def test_non_numeric_expires_in_yields_none_expiry_but_credentials(self):
        data = {
            "access_token": "acc-123",
            "refresh_token": "ref-456",
            "expires_in": "not-a-number",
            "scope": "user:profile user:inference",
            "account": {"email_address": "user@example.com", "uuid": "acc-uuid"},
            "organization": {"name": "Org", "uuid": "org-uuid"},
        }

        credentials, identity = oauth_login.credentials_from_token_response(
            data, now_ms=1_000_000
        )

        parsed = json.loads(credentials)
        oauth_payload = parsed["claudeAiOauth"]
        assert oauth_payload["accessToken"] == "acc-123"
        assert oauth_payload["refreshToken"] == "ref-456"
        # Bad expires_in degrades to expiresAt=None (token usable; expiry unknown).
        assert oauth_payload["expiresAt"] is None
        assert identity.email == "user@example.com"
