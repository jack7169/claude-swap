"""Tests for the oauth module."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from claude_swap import oauth


class TestExtractAccessToken:
    """Test extract_access_token."""

    def test_valid_credentials(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-test-token"}})
        assert oauth.extract_access_token(creds) == "sk-test-token"

    def test_missing_key(self):
        creds = json.dumps({"claudeAiOauth": {}})
        assert oauth.extract_access_token(creds) is None

    def test_invalid_json(self):
        assert oauth.extract_access_token("not-json") is None

    def test_empty_string(self):
        assert oauth.extract_access_token("") is None


class TestAccountHeadroom:
    """Test account_headroom."""

    def test_binding_window_is_the_higher_utilization(self):
        usage = {"five_hour": {"pct": 80.0}, "seven_day": {"pct": 20.0}}
        assert oauth.account_headroom(usage) == 20.0  # 100 - max(80, 20)

    def test_seven_day_can_be_the_binding_window(self):
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 95.0}}
        assert oauth.account_headroom(usage) == 5.0

    def test_single_window(self):
        assert oauth.account_headroom({"five_hour": {"pct": 40.0}}) == 60.0

    def test_at_limit_is_zero_headroom(self):
        assert oauth.account_headroom({"five_hour": {"pct": 100.0}}) == 0.0

    def test_spend_is_ignored(self):
        # Pay-as-you-go credits must not drive rate-limit headroom.
        usage = {"spend": {"pct": 99.0}, "five_hour": {"pct": 10.0}}
        assert oauth.account_headroom(usage) == 90.0

    def test_no_window_data_is_unknown(self):
        assert oauth.account_headroom({"spend": {"pct": 50.0}}) is None
        assert oauth.account_headroom({}) is None

    def test_none_and_non_dict_are_unknown(self):
        assert oauth.account_headroom(None) is None
        assert oauth.account_headroom("no credentials") is None

    def test_malformed_pct_is_ignored(self):
        assert oauth.account_headroom({"five_hour": {"pct": None}}) is None


class TestFormatReset:
    """Test format_reset."""

    def test_same_day_shows_time_only(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=2, minutes=15)
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(future.isoformat())
        assert countdown == "2h 15m"
        assert clock.count(":") == 1

    def test_different_day_shows_date(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(days=2)
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(future.isoformat())
        import calendar
        months = list(calendar.month_abbr)[1:]
        assert any(m in clock for m in months)

    def test_minutes_only_when_under_one_hour(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(minutes=45)
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(future.isoformat())
        assert countdown == "45m"
        assert "h" not in countdown


class TestFetchUsage:
    """Test fetch_usage."""

    def test_success(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=1)
        response_data = {
            "five_hour": {"utilization": 22.0, "resets_at": future.isoformat()},
            "seven_day": {"utilization": 61.0, "resets_at": future.isoformat()},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response), \
             patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            result = oauth.fetch_usage("sk-test-token")

        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert result["five_hour"]["countdown"] == "1h 0m"

    def test_network_error(self):
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=Exception("timeout")):
            result = oauth.fetch_usage("sk-test-token")
        assert result is None

    def test_http_error_logs_in_debug_mode(self, capsys):
        import logging
        logger = logging.getLogger("claude-swap")
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        try:
            http_error = urllib.error.HTTPError(
                url="https://api.anthropic.com/api/oauth/usage",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=None,
            )

            with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=http_error):
                result = oauth.fetch_usage("sk-test-token")

            assert result is None
            debug_output = capsys.readouterr().err
            assert "Usage fetch failed" in debug_output
            assert "<HTTPError 429: 'Too Many Requests'>" in debug_output
        finally:
            logger.removeHandler(handler)
            logger.setLevel(logging.WARNING)

    def test_bad_response(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b"{}"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            result = oauth.fetch_usage("sk-test-token")
        assert result is None

    def test_null_resets_at(self):
        """When resets_at is null, still return pct without clock/countdown."""
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=22)
        response_data = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 100.0, "resets_at": future.isoformat()},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response), \
             patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            result = oauth.fetch_usage("sk-test-token")

        assert result is not None
        assert result["five_hour"]["pct"] == 0.0
        assert "clock" not in result["five_hour"]
        assert "countdown" not in result["five_hour"]
        assert result["seven_day"]["pct"] == 100.0
        assert "clock" in result["seven_day"]
        assert "countdown" in result["seven_day"]

    @staticmethod
    def _fetch_with_response(response_data):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            return oauth.fetch_usage("sk-test-token")

    def test_extra_usage_complete(self):
        """All extra_usage fields populated — spend, five_hour, and seven_day all present."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 72900,
                "monthly_limit": 500000,
                "utilization": 14.58,
                "currency": "USD",
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert result["spend"]["used"] == 729.0
        assert result["spend"]["limit"] == 5000.0
        assert result["spend"]["pct"] == 14.58
        assert result["spend"]["currency"] == "USD"

    def test_extra_usage_unlimited_keeps_other_rows(self):
        """Unlimited (monthly_limit=None) drops the spend entry without losing five_hour/seven_day."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 72900,
                "monthly_limit": None,
                "utilization": None,
                "currency": "USD",
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert "spend" not in result

    def test_extra_usage_partial_keeps_other_rows(self):
        """A null in used_credits leaves the rest of the response untouched."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": None,
                "monthly_limit": 500000,
                "utilization": 14.58,
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert "spend" not in result

    def test_extra_usage_disabled_keeps_other_rows(self):
        """is_enabled=False suppresses spend even with valid numeric fields."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": False,
                "used_credits": 72900,
                "monthly_limit": 500000,
                "utilization": 14.58,
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert "spend" not in result


class TestBuildUsageModelWeekly:
    """The weekly Fable-5 limit is a ``weekly_scoped`` entry in the ``limits``
    array (scope.model.display_name == 'Fable') — NOT a top-level seven_day_fable
    key (which is null). Confirmed against the live usage API."""

    @staticmethod
    def _fable_limits(percent, *, severity="normal", is_active=True, resets_at=None):
        return [
            {"kind": "session", "group": "session", "percent": 8,
             "severity": "normal", "resets_at": None, "scope": None,
             "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": 27,
             "severity": "normal", "resets_at": None, "scope": None,
             "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": percent,
             "severity": severity, "resets_at": resets_at,
             "scope": {"model": {"id": None, "display_name": "Fable"},
                       "surface": None},
             "is_active": is_active},
        ]

    def test_build_usage_parses_fable_weekly_from_limits(self):
        from datetime import timedelta
        future = datetime.now(timezone.utc) + timedelta(hours=5)
        data = {
            "five_hour": {"utilization": 8.0, "resets_at": None},
            "seven_day": {"utilization": 27.0, "resets_at": None},
            "limits": self._fable_limits(46.0, resets_at=future.isoformat()),
        }
        result = oauth.build_usage_result(data)
        fable = result["model_weekly"]["Fable"]
        assert fable["pct"] == 46.0
        assert fable["is_active"] is True
        assert fable["resets_at"] == future.isoformat()
        assert "countdown" in fable and "clock" in fable
        # Existing windows are unaffected by the new parsing.
        assert result["five_hour"]["pct"] == 8.0
        assert result["seven_day"]["pct"] == 27.0

    def test_build_usage_no_limits_key_is_backcompat(self):
        data = {
            "five_hour": {"utilization": 8.0, "resets_at": None},
            "seven_day": {"utilization": 27.0, "resets_at": None},
        }
        result = oauth.build_usage_result(data)
        assert "model_weekly" not in result
        assert result["five_hour"]["pct"] == 8.0

    def test_fable_exhausted_percent_100(self):
        data = {
            "five_hour": {"utilization": 5.0, "resets_at": None},
            "limits": self._fable_limits(100.0, severity="critical"),
        }
        result = oauth.build_usage_result(data)
        fable = result["model_weekly"]["Fable"]
        assert fable["pct"] == 100.0
        assert fable["severity"] == "critical"
        assert fable["is_active"] is True

    def test_limits_non_list_is_ignored(self):
        data = {"five_hour": {"utilization": 5.0, "resets_at": None}, "limits": "nope"}
        result = oauth.build_usage_result(data)
        assert "model_weekly" not in result

    def test_weekly_scoped_without_model_name_skipped(self):
        data = {
            "five_hour": {"utilization": 5.0, "resets_at": None},
            "limits": [
                {"kind": "weekly_scoped", "group": "weekly", "percent": 50,
                 "severity": "normal", "resets_at": None,
                 "scope": {"model": None, "surface": None}, "is_active": True},
            ],
        }
        result = oauth.build_usage_result(data)
        assert "model_weekly" not in result


class TestRefreshOAuthCredentials:
    """Test direct OAuth refresh requests."""

    @staticmethod
    def _make_credentials(scopes=None):
        if scopes is None:
            scopes = ["user:profile", "user:inference", "user:sessions:claude_code"]
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 0,
                "scopes": scopes,
            }
        })

    def test_refresh_sends_correct_body(self):
        seen_body = {}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        def mock_urlopen(req, timeout=0):
            seen_body.update(json.loads(req.data.decode()))
            return mock_response

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            refreshed = oauth.refresh_oauth_credentials(self._make_credentials())

        assert refreshed is not None
        assert seen_body["grant_type"] == "refresh_token"
        assert seen_body["refresh_token"] == "old-refresh"
        assert seen_body["client_id"] == oauth.OAUTH_CLIENT_ID
        assert "scope" not in seen_body


class TestBuildTokenStatus:
    """Test token status formatting."""

    def test_builds_fresh_token_status(self):
        fixed_now = datetime(2026, 4, 2, 18, 0, 0, tzinfo=timezone.utc)
        expires_at = int(datetime(2026, 4, 2, 19, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": expires_at,
            }
        })

        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.fromtimestamp = datetime.fromtimestamp
            mock_dt.now.return_value = fixed_now
            status = oauth.build_token_status(credentials)

        assert status is not None
        assert "oauth: fresh, refresh token yes" in status
        assert "in 1h 30m" in status

    def test_builds_unknown_expiry_status(self):
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
            }
        })

        status = oauth.build_token_status(credentials)

        assert status == "oauth: unknown expiry, refresh token yes"


class TestFetchUsageForAccount:
    """Test refresh-aware usage fetches for managed accounts."""

    @staticmethod
    def _make_credentials(access="old-access", refresh="old-refresh",
                          expires_at=None, org_uuid="org-1", scopes=None):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if scopes is None:
            scopes = ["user:profile", "user:inference", "user:sessions:claude_code"]
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires_at if expires_at is not None else now_ms + 3_600_000,
                "scopes": scopes,
                "subscriptionType": "pro",
                "rateLimitTier": "default_claude_ai",
            },
            "organizationUuid": org_uuid,
        })

    @staticmethod
    def _make_token_response(access="new-access", refresh="new-refresh",
                             expires_in=3600):
        return json.dumps({
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": expires_in,
            "scope": "user:profile user:inference user:sessions:claude_code",
        }).encode()

    @staticmethod
    def _make_usage_response(h5_pct=12.0, d7_pct=34.0):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "five_hour": {"utilization": h5_pct, "resets_at": None},
            "seven_day": {"utilization": d7_pct, "resets_at": None},
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_refreshes_expired_token_before_usage_fetch(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        token_resp = MagicMock()
        token_resp.read.return_value = self._make_token_response()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        usage_resp = self._make_usage_response()
        persist_mock = MagicMock()

        def mock_urlopen(req, timeout=0):
            if "oauth/token" in req.full_url:
                return token_resp
            if "oauth/usage" in req.full_url:
                assert req.get_header("Authorization") == "Bearer new-access"
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
                persist_credentials=persist_mock,
            )

        assert result is not None
        assert result["five_hour"]["pct"] == 12.0
        persist_mock.assert_called_once()
        persisted_creds = persist_mock.call_args[0][2]
        merged = json.loads(persisted_creds)
        assert merged["organizationUuid"] == "org-1"
        assert merged["claudeAiOauth"]["accessToken"] == "new-access"
        assert merged["claudeAiOauth"]["refreshToken"] == "new-refresh"

    def test_retries_401_with_token_refresh(self):
        """Account gets 401, refreshes, retries successfully."""
        credentials = self._make_credentials()

        token_resp = MagicMock()
        token_resp.read.return_value = self._make_token_response()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        usage_resp = self._make_usage_response(h5_pct=56.0, d7_pct=78.0)
        usage_calls = 0
        persist_mock = MagicMock()

        def mock_urlopen(req, timeout=0):
            nonlocal usage_calls
            if "oauth/token" in req.full_url:
                return token_resp
            if "oauth/usage" in req.full_url:
                usage_calls += 1
                if usage_calls == 1:
                    assert req.get_header("Authorization") == "Bearer old-access"
                    raise urllib.error.HTTPError(
                        req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                    )
                assert req.get_header("Authorization") == "Bearer new-access"
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "2", "test@example.com", credentials,
                is_active=False,
                persist_credentials=persist_mock,
            )

        assert result is not None
        assert result["seven_day"]["pct"] == 78.0
        assert usage_calls == 2
        persist_mock.assert_called_once()
        refreshed_oauth = json.loads(persist_mock.call_args[0][2])["claudeAiOauth"]
        assert refreshed_oauth["accessToken"] == "new-access"

    def test_valid_token_fetches_usage_without_refresh(self):
        """Account with valid token fetches usage without refresh."""
        credentials = self._make_credentials()

        usage_resp = self._make_usage_response(h5_pct=10.0, d7_pct=20.0)

        def mock_urlopen(req, timeout=0):
            if "oauth/usage" in req.full_url:
                assert req.get_header("Authorization") == "Bearer old-access"
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen), \
             patch("claude_swap.oauth.refresh_oauth_credentials") as refresh_mock:
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
            )

        refresh_mock.assert_not_called()
        assert result is not None
        assert result["five_hour"]["pct"] == 10.0

    def test_dead_refresh_token_returns_token_expired(self):
        """A backup whose refresh token is dead/revoked (token endpoint 400 =
        invalid_grant) must surface the TOKEN_EXPIRED sentinel — NOT bare None,
        which the merge would treat as a transient blip and retain a stale usage
        dict forever. A dead token is definitive."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        def mock_urlopen(req, timeout=0):
            if "oauth/token" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
            )

        assert result == oauth.TOKEN_EXPIRED

    def test_dead_token_makes_no_usage_call(self):
        """A proactively-detected dead token short-circuits: one token POST, and
        ZERO usage calls (so a dead backup stops contributing to the 429 storm)."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)
        usage_calls = 0

        def mock_urlopen(req, timeout=0):
            nonlocal usage_calls
            if "oauth/token" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            if "oauth/usage" in req.full_url:
                usage_calls += 1
                raise AssertionError("dead token must not reach the usage endpoint")
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials, is_active=False,
            )

        assert result == oauth.TOKEN_EXPIRED
        assert usage_calls == 0

    def test_transient_refresh_failure_returns_none(self):
        """A network blip on refresh (URLError, not a 400/401) must NOT be
        classified as a dead token — it stays transient (None), so the merge
        retains the last-known-good usage rather than wiping it. This guards the
        offline-laptop case: an expired token during an outage is not 'dead'."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        def mock_urlopen(req, timeout=0):
            if "oauth/token" in req.full_url:
                raise urllib.error.URLError("connection refused")
            if "oauth/usage" in req.full_url:
                raise urllib.error.URLError("connection refused")
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials, is_active=False,
            )

        assert result is None

    def test_401_retry_dead_refresh_returns_token_expired(self):
        """Non-expired token that 401s, then the retry-refresh 400s (dead token):
        surface TOKEN_EXPIRED, not None."""
        credentials = self._make_credentials()  # not expired -> no proactive refresh

        def mock_urlopen(req, timeout=0):
            if "oauth/usage" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            if "oauth/token" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials, is_active=False,
            )

        assert result == oauth.TOKEN_EXPIRED

    def test_refreshes_when_scopes_are_missing(self):
        """Refresh should work even when stored credentials have no scopes."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(
            expires_at=now_ms - 1_000,
            scopes=None,
        )
        parsed = json.loads(credentials)
        del parsed["claudeAiOauth"]["scopes"]
        credentials = json.dumps(parsed)

        token_resp = MagicMock()
        token_resp.read.return_value = self._make_token_response()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        usage_resp = self._make_usage_response()
        persist_mock = MagicMock()

        def mock_urlopen(req, timeout=0):
            if "oauth/token" in req.full_url:
                body = json.loads(req.data.decode())
                assert "scope" not in body
                return token_resp
            if "oauth/usage" in req.full_url:
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
                persist_credentials=persist_mock,
            )

        assert result is not None
        persist_mock.assert_called_once()

    def test_active_account_skips_refresh_even_when_expired(self):
        """Active account with expired token must NOT trigger a refresh POST.

        Claude Code owns the active account's credentials and coordinates its
        own refresh via a lockfile on ~/.claude/ that cswap doesn't honor, so
        cswap must never touch the active account's tokens.
        """
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        persist_mock = MagicMock()
        refresh_calls = 0

        def mock_urlopen(req, timeout=0):
            nonlocal refresh_calls
            if "oauth/token" in req.full_url:
                refresh_calls += 1
                raise AssertionError(
                    "Active account must not trigger a refresh POST"
                )
            if "oauth/usage" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=True,
                persist_credentials=persist_mock,
            )

        assert refresh_calls == 0
        persist_mock.assert_not_called()
        # Usage call 401'd and there's no retry-with-refresh for active, so None.
        assert result is None

    def test_active_account_401_does_not_retry_with_refresh(self):
        """Active account that 401s returns None without attempting a refresh."""
        credentials = self._make_credentials()

        def mock_urlopen(req, timeout=0):
            if "oauth/token" in req.full_url:
                raise AssertionError(
                    "Active account must not trigger a refresh POST on 401"
                )
            if "oauth/usage" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        persist_mock = MagicMock()
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=True,
                persist_credentials=persist_mock,
            )

        assert result is None
        persist_mock.assert_not_called()

    def test_persist_failure_logs_warning_with_recovery_hint(self, caplog, capsys):
        """If the persist callback raises, _persist logs at WARNING level with
        a recovery hint (re-run `cswap --add-account`), not debug, AND prints
        a user-visible warning to stdout.
        """
        import logging

        def boom(acct_num, acct_email, creds):
            raise RuntimeError("disk exploded")

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            oauth._persist(boom, "1", "test@example.com", "{}")

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "claude-swap"
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "failed to persist" in msg
        assert "cswap --add-account" in msg
        assert "1" in msg
        assert "test@example.com" in msg

        # Also verify the user-visible printed warning (warnings go to stderr)
        output = capsys.readouterr().err
        assert "failed to save refreshed token" in output
        assert "cswap --add-account" in output


def _creds():
    import json
    return json.dumps({"claudeAiOauth": {"accessToken": "sk-test", "refreshToken": "rt"}})


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


class TestRefreshWithReason:
    """_refresh_with_reason splits the failure into auth (dead token) vs transient.

    Only a 400/401 from the token endpoint (invalid_grant) is 'auth' (definitively
    dead); every other failure is 'transient' so good usage is never wiped on a blip.
    """

    @staticmethod
    def _creds():
        return json.dumps({"claudeAiOauth": {
            "accessToken": "old", "refreshToken": "rt",
        }})

    def _run(self, urlopen_side_effect):
        with patch("claude_swap.oauth.urllib.request.urlopen",
                   side_effect=urlopen_side_effect):
            return oauth._refresh_with_reason(self._creds())

    def test_success_returns_ok(self):
        def ok(req, timeout=0):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"access_token": "new", "expires_in": 3600}
            ).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        creds, reason = self._run(ok)
        assert reason == "ok"
        assert json.loads(creds)["claudeAiOauth"]["accessToken"] == "new"

    def test_http_400_is_auth(self):
        creds, reason = self._run(
            lambda req, timeout=0: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 400, "Bad", None, None)))
        assert (creds, reason) == (None, oauth.REFRESH_AUTH_FAILED)

    def test_http_401_is_auth(self):
        creds, reason = self._run(
            lambda req, timeout=0: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 401, "Unauth", None, None)))
        assert (creds, reason) == (None, oauth.REFRESH_AUTH_FAILED)

    def test_http_500_is_transient(self):
        creds, reason = self._run(
            lambda req, timeout=0: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 500, "Err", None, None)))
        assert (creds, reason) == (None, oauth.REFRESH_TRANSIENT)

    def test_urlerror_is_transient(self):
        creds, reason = self._run(
            lambda req, timeout=0: (_ for _ in ()).throw(
                urllib.error.URLError("boom")))
        assert (creds, reason) == (None, oauth.REFRESH_TRANSIENT)

    def test_malformed_200_is_transient(self):
        def bad(req, timeout=0):
            resp = MagicMock()
            resp.read.return_value = json.dumps({"expires_in": 3600}).encode()  # no access_token
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        assert self._run(bad) == (None, oauth.REFRESH_TRANSIENT)

    def test_refresh_oauth_credentials_delegates_and_returns_str_or_none(self):
        # Back-compat: the thin wrapper keeps its str|None contract (session.py).
        def ok(req, timeout=0):
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"access_token": "new", "expires_in": 3600}
            ).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=ok):
            out = oauth.refresh_oauth_credentials(self._creds())
        assert isinstance(out, str)
        assert json.loads(out)["claudeAiOauth"]["accessToken"] == "new"


def test_fetch_usage_returns_rate_limited_on_429():
    with patch.object(oauth, "request_usage_data", side_effect=_http_error(429)):
        result = oauth.fetch_usage_for_account("1", "a@x.com", _creds(), is_active=True)
    assert result == oauth.RATE_LIMITED


def test_fetch_usage_returns_none_on_other_http_error():
    with patch.object(oauth, "request_usage_data", side_effect=_http_error(500)):
        result = oauth.fetch_usage_for_account("1", "a@x.com", _creds(), is_active=True)
    assert result is None


def test_fetch_usage_returns_none_on_timeout():
    with patch.object(oauth, "request_usage_data", side_effect=TimeoutError("slow")):
        result = oauth.fetch_usage_for_account("1", "a@x.com", _creds(), is_active=True)
    assert result is None


def test_fetch_usage_returns_rate_limited_on_429_after_refresh():
    # is_active=False + refreshToken + non-expired token -> no proactive refresh.
    # First usage request 401s -> refresh succeeds -> retry request 429s -> sentinel.
    creds = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-old", "refreshToken": "rt",
        "expiresAt": 9999999999000,  # far future ms -> not proactively refreshed
    }})
    refreshed = json.dumps({"claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt"}})
    # fetch_usage_for_account refreshes via the _refresh_with_reason seam (which
    # carries the auth/transient reason); patch that, not the thin wrapper.
    with patch.object(oauth, "request_usage_data",
                      side_effect=[_http_error(401), _http_error(429)]), \
         patch.object(oauth, "_refresh_with_reason", return_value=(refreshed, "ok")):
        result = oauth.fetch_usage_for_account("1", "a@x.com", creds, is_active=False)
    assert result == oauth.RATE_LIMITED


class TestUserAgent:
    """The usage endpoint buckets rate limits on User-Agent, per access token.

    ``claude-code/<version>`` is the safe bucket (fine at ~180s intervals);
    anything else hits an aggressively-throttled bucket -> persistent 429s.
    Confirmed live: all accounts returned HTTP 200 with claude-code/2.1.204
    while the fleet was 429-storming under claude-swap/1.0. So every
    authenticated request cswap makes must send the claude-code UA.
    """

    def test_ua_constant_is_claude_code_prefixed(self):
        assert oauth.CLAUDE_CODE_UA.startswith("claude-code/")

    def test_usage_request_sends_claude_code_user_agent(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["req"] = req
            resp = MagicMock()
            resp.read.return_value = json.dumps({}).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
            oauth.request_usage_data("tok123")

        assert captured["req"].get_header("User-agent") == oauth.CLAUDE_CODE_UA

    def test_refresh_request_sends_claude_code_user_agent(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["req"] = req
            resp = MagicMock()
            resp.read.return_value = json.dumps(
                {"access_token": "a", "expires_in": 3600}
            ).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        creds = json.dumps({"claudeAiOauth": {
            "accessToken": "old", "refreshToken": "rt",
        }})
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
            oauth.refresh_oauth_credentials(creds)

        assert captured["req"].get_header("User-agent") == oauth.CLAUDE_CODE_UA

    def test_warm_request_sends_claude_code_user_agent(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["req"] = req
            resp = MagicMock()
            resp.read.return_value = json.dumps({"content": []}).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
            oauth.send_warm_message("tok", "claude-haiku-4-5", 8, 5.0)

        assert captured["req"].get_header("User-agent") == oauth.CLAUDE_CODE_UA


class TestSendWarmMessage:
    """send_warm_message: minimal Messages API POST, Bearer + beta headers."""

    def _ok_response(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"content": []}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_builds_bearer_beta_post_request(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["req"] = req
            captured["timeout"] = timeout
            return self._ok_response()

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
            body = oauth.send_warm_message("tok123", "claude-haiku-4-5", 8, 5.0)

        req = captured["req"]
        assert req.full_url == "https://api.anthropic.com/v1/messages"
        assert req.get_method() == "POST"
        assert req.get_header("Authorization") == "Bearer tok123"
        assert req.get_header("Anthropic-beta") == oauth.OAUTH_BETA_HEADER
        # The Messages API rejects the request (HTTP 400 "anthropic-version:
        # header is required") without a version header — confirmed against the
        # live endpoint. Unlike the OAuth usage endpoint, this one is mandatory.
        assert req.get_header("Anthropic-version") == "2023-06-01"
        assert req.get_header("Content-type") == "application/json"
        assert captured["timeout"] == 5.0
        sent = json.loads(req.data.decode())
        assert sent == {
            "model": "claude-haiku-4-5",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "can you hear me?"}],
        }
        assert body == {"content": []}

    def test_raises_on_http_error(self):
        err = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=401, msg="Unauthorized", hdrs=None, fp=None,
        )
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            try:
                oauth.send_warm_message("tok", "claude-haiku-4-5", 8, 5.0)
            except urllib.error.HTTPError as e:
                assert e.code == 401
            else:
                raise AssertionError("expected HTTPError to propagate")
