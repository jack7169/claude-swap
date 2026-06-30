"""Adversarial review tests for Phase 5 (usage / rotation correctness).

These complement the implementer's test_oauth_robustness.py and
test_usage_rotation_correctness.py by probing edge cases NOT covered there:

- 5.1 definitive-failure merge: the sentinel must win even when there is no
  prior dict, the happy path (fresh dict over stale dict) must be untouched, and
  non-definitive sentinels (API key) must retain the stale dict.
- 5.2 robustness: the aware-timestamp / happy paths of format_reset,
  build_token_status, build_usage_result and refresh_oauth_credentials must be
  byte-for-byte unaffected; negative out-of-range expiresAt must also be caught.
- 5.3 rotation anchor: when the live==recorded account is itself missing from the
  sequence, the anchor falls back rather than crashing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from claude_swap import oauth as _oauth
from claude_swap import oauth_login
from claude_swap.cache import write_cache
from claude_swap.json_output import (
    USAGE_NO_CREDENTIALS,
    USAGE_RATE_LIMITED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

_OAUTH_CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})
_API_KEY_CREDS = "sk-ant-api03-deadbeef"


def _make_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


# --------------------------------------------------------------------------
# 5.1 — merge edge cases
# --------------------------------------------------------------------------

class TestCollectUsageMergeEdges:
    def _two_oauth(self):
        return [
            (1, "a@x.com", "", "", False, _OAUTH_CREDS),
            (2, "b@x.com", "", "", False, _OAUTH_CREDS),
        ]

    def test_definitive_failure_with_no_prior_dict_still_yields_sentinel(
        self, temp_home, monkeypatch
    ):
        # No prior cache at all. A definitive failure must store the sentinel
        # (not None), matching the pre-fix else-branch behavior for this case.
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])

        def fake(num, email, creds, is_active=False, persist_credentials=None):
            return {"1": {"five_hour": {"pct": 5.0}}, "2": USAGE_TOKEN_EXPIRED}[str(num)]

        monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)

        out = s._collect_usage(self._two_oauth(), only={"1", "2"})
        assert out[1] == USAGE_TOKEN_EXPIRED
        assert _oauth.account_headroom(out[1]) is None

    def test_fresh_dict_overrides_stale_dict_happy_path(self, temp_home, monkeypatch):
        # The COMMON case: a fresh dict must replace an older cached dict. The
        # 5.1 change must not perturb this at all.
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(s.backup_dir / "cache" / "usage.json", {
            "1": {"five_hour": {"pct": 90.0}},   # stale
            "2": {"five_hour": {"pct": 80.0}},   # stale
        })

        def fake(num, email, creds, is_active=False, persist_credentials=None):
            return {
                "1": {"five_hour": {"pct": 10.0}},   # fresh
                "2": {"five_hour": {"pct": 20.0}},   # fresh
            }[str(num)]

        monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)

        out = s._collect_usage(self._two_oauth(), only={"1", "2"})
        assert out[0] == {"five_hour": {"pct": 10.0}}
        assert out[1] == {"five_hour": {"pct": 20.0}}

    def test_api_key_sentinel_retains_stale_dict(self, temp_home, monkeypatch):
        # USAGE_API_KEY is NOT a definitive credential failure — it must keep the
        # stale-dict retention (it is excluded from the definitive list). Here a
        # managed API-key account previously had an OAuth usage dict cached.
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        write_cache(s.backup_dir / "cache" / "usage.json", {
            "1": {"five_hour": {"pct": 10.0}},
            "2": {"five_hour": {"pct": 20.0}},
        })
        info = [
            (1, "a@x.com", "", "", False, _OAUTH_CREDS),
            (2, "b@x.com", "", "", False, _API_KEY_CREDS),  # now an API key
        ]

        out = s._collect_usage(info, only={"1", "2"})
        # Slot 2 returns USAGE_API_KEY this round; since that is non-definitive,
        # the prior dict is retained for display.
        assert out[1] == {"five_hour": {"pct": 20.0}}


# --------------------------------------------------------------------------
# 5.2 — happy / aware paths must be unaffected, plus a negative-range guard
# --------------------------------------------------------------------------

class TestFormatResetAwareUnchanged:
    def test_aware_iso_string_unchanged(self):
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        aware_future = "2026-03-23T14:30:00+00:00"
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = _oauth.format_reset(aware_future)
        assert countdown == "2h 30m"


class TestBuildTokenStatusNegativeRange:
    def test_negative_out_of_range_expires_at_does_not_crash(self):
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": -(10 ** 18),
            }
        })
        status = _oauth.build_token_status(credentials)
        assert status == "oauth: unknown expiry, refresh token yes"

    def test_in_range_expires_at_renders_normally(self):
        fixed_now = datetime(2026, 4, 2, 18, 0, 0, tzinfo=timezone.utc)
        expires_at = int(
            datetime(2026, 4, 2, 19, 30, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "a",
                "refreshToken": "r",
                "expiresAt": expires_at,
            }
        })
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.fromtimestamp = datetime.fromtimestamp
            mock_dt.now.return_value = fixed_now
            status = _oauth.build_token_status(credentials)
        assert status is not None
        assert status.startswith("oauth: fresh")


class TestBuildUsageResultHappyPath:
    def test_both_windows_with_utilization_present_unchanged(self):
        data = {
            "five_hour": {"utilization": 12.0, "resets_at": None},
            "seven_day": {"utilization": 34.0, "resets_at": None},
        }
        result = _oauth.build_usage_result(data)
        assert result["five_hour"]["pct"] == 12.0
        assert result["seven_day"]["pct"] == 34.0

    def test_zero_utilization_is_not_skipped(self):
        # 0.0 utilization is a valid value (`is not None`), not a missing window;
        # a naive truthiness guard would wrongly drop it.
        data = {"five_hour": {"utilization": 0.0, "resets_at": None}}
        result = _oauth.build_usage_result(data)
        assert result is not None
        assert result["five_hour"]["pct"] == 0.0


class TestRefreshOAuthHappyPathUnaffected:
    @staticmethod
    def _creds():
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 1,
            }
        })

    def test_valid_200_mutates_and_returns_updated_dict(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new-access",
            "expires_in": 3600,
            "refresh_token": "new-refresh",
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response
        ):
            out = _oauth.refresh_oauth_credentials(self._creds())
        assert out is not None
        payload = json.loads(out)["claudeAiOauth"]
        assert payload["accessToken"] == "new-access"
        assert payload["refreshToken"] == "new-refresh"
        assert payload["expiresAt"] > 0

    def test_expires_in_zero_is_rejected_as_falsy_only_for_access_token(self):
        # expires_in == 0 is numerically valid (isinstance int) and must be
        # accepted: the validation gate keys off type for expires_in, not
        # truthiness, so a 0-second expiry still updates the token.
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new-access",
            "expires_in": 0,
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response
        ):
            out = _oauth.refresh_oauth_credentials(self._creds())
        assert out is not None
        payload = json.loads(out)["claudeAiOauth"]
        assert payload["accessToken"] == "new-access"


class TestCredentialsFromTokenResponseGoodExpiresIn:
    def test_numeric_expires_in_sets_expiry(self):
        data = {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_in": 3600,
            "account": {"email_address": "u@e.com", "uuid": "a"},
            "organization": {"name": "O", "uuid": "o"},
        }
        creds, _identity = oauth_login.credentials_from_token_response(
            data, now_ms=1_000_000
        )
        payload = json.loads(creds)["claudeAiOauth"]
        assert payload["expiresAt"] == 1_000_000 + 3600 * 1000


# --------------------------------------------------------------------------
# 5.3 — anchor fallback when the live==recorded slot is not in the sequence
# --------------------------------------------------------------------------

class TestRotationAnchorFallback:
    def test_live_account_not_in_sequence_falls_back_without_crash(
        self, temp_home, monkeypatch
    ):
        # Live identity resolves to a managed slot (5), but that slot is NOT in
        # the rotation sequence (data drift). int(anchor) succeeds but
        # sequence.index() raises ValueError -> must fall back to active_account
        # (slot 1 in sequence) -> rotate to slot 2, never crash.
        s = _make_switcher()
        seq = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2, 3],
            "accounts": {
                "1": {"email": "a@x.com", "organizationUuid": ""},
                "2": {"email": "b@x.com", "organizationUuid": ""},
                "3": {"email": "c@x.com", "organizationUuid": ""},
                "5": {"email": "e@x.com", "organizationUuid": ""},
            },
        }
        s._write_json(s.sequence_file, seq)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "e@x.com", "organizationUuid": ""}
        }))

        captured: dict = {}

        def fake_perform(target, emit_output=True):
            captured["target"] = target
            return {
                "from": {"number": 0, "email": "x"},
                "to": {"number": int(target), "email": "x"},
                "warnings": [],
            }

        monkeypatch.setattr(s, "_account_is_switchable", lambda num: True)
        monkeypatch.setattr(s, "_perform_switch", fake_perform)
        s.switch(strategy=None)

        # Anchor int(5) is valid but not in [1,2,3]; falls back to active_account
        # (1) -> next is 2.
        assert captured["target"] == "2"
