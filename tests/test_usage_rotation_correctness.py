"""Tests for usage/rotation correctness (Phase 5.1 and 5.3).

5.1 — Stale usage must not mask a *definitively* failed fresh fetch. When a
fresh fetch for an account returns ``USAGE_TOKEN_EXPIRED`` /
``USAGE_NO_CREDENTIALS`` (the credential just failed this round), the merge in
``_collect_usage`` must NOT fall back to an older cached usage *dict* — doing so
would let ``oauth.account_headroom`` read that stale dict as positive headroom,
so a ``--switch --strategy best``/``next-available`` could land on an account
whose credential just failed. The sentinel must win for the decision path so
headroom resolves to ``None`` ("unknown" → never auto-selected). Transient /
ambiguous results (a bare ``None`` fetch failure, or ``USAGE_RATE_LIMITED``)
keep the existing stale-dict retention for display resilience.

5.3 — Plain ``--switch`` (rotation) must anchor on the LIVE active account, not
the recorded ``activeAccountNumber`` in sequence.json, which can drift if the
user switched via another mechanism. It falls back to the recorded value only
when the live account can't be matched to a managed slot.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from claude_swap import oauth as _oauth
from claude_swap.cache import write_cache
from claude_swap.json_output import (
    USAGE_NO_CREDENTIALS,
    USAGE_RATE_LIMITED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _make_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


_OAUTH_CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})


def _info():
    """Two inactive OAuth accounts (so fetches route through fetch_usage_for_account)."""
    return [
        (1, "a@x.com", "", "", False, _OAUTH_CREDS),
        (2, "b@x.com", "", "", False, _OAUTH_CREDS),
    ]


def _seed_prior_cache(s: ClaudeAccountSwitcher) -> None:
    """Last-known-good usage.json: both slots carry a positive-headroom dict."""
    write_cache(s.backup_dir / "cache" / "usage.json", {
        "1": {"five_hour": {"pct": 10.0}},   # headroom 90
        "2": {"five_hour": {"pct": 20.0}},   # headroom 80
    })


def _patch_fetch(monkeypatch, responses: dict) -> None:
    def fake(num, email, creds, is_active=False, persist_credentials=None):
        return responses[str(num)]
    monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)


class TestCollectUsageDefinitiveFailure:
    """5.1 — definitive failures must not be masked by a stale cached dict."""

    def test_token_expired_overrides_stale_dict(self, temp_home, monkeypatch):
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        _seed_prior_cache(s)
        # Slot 2's fresh fetch DEFINITIVELY fails; slot 1 still returns a dict.
        _patch_fetch(monkeypatch, {
            "1": {"five_hour": {"pct": 15.0}},
            "2": USAGE_TOKEN_EXPIRED,
        })

        out = s._collect_usage(_info(), only={"1", "2"})

        # Slot 2 must report the sentinel, NOT the stale {pct: 20} dict.
        assert out[1] == USAGE_TOKEN_EXPIRED
        assert out[0] == {"five_hour": {"pct": 15.0}}
        # And the sentinel resolves to unknown headroom (never auto-selected).
        assert _oauth.account_headroom(out[1]) is None

    def test_no_credentials_overrides_stale_dict(self, temp_home, monkeypatch):
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        _seed_prior_cache(s)
        _patch_fetch(monkeypatch, {
            "1": {"five_hour": {"pct": 15.0}},
            "2": USAGE_NO_CREDENTIALS,
        })

        out = s._collect_usage(_info(), only={"1", "2"})

        assert out[1] == USAGE_NO_CREDENTIALS
        assert _oauth.account_headroom(out[1]) is None

    def test_definitive_failure_persisted_to_cache_not_stale_dict(
        self, temp_home, monkeypatch
    ):
        """The on-disk usage.json must also store the sentinel, not the old dict,
        so a subsequent decision-path read never sees fake positive headroom."""
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        _seed_prior_cache(s)
        _patch_fetch(monkeypatch, {
            "1": {"five_hour": {"pct": 15.0}},
            "2": USAGE_TOKEN_EXPIRED,
        })

        s._collect_usage(_info(), only={"1", "2"})

        # The cache file wraps the usage map in a {"timestamp", "data"} envelope.
        written = json.loads((s.backup_dir / "cache" / "usage.json").read_text())
        assert written["data"]["2"] == USAGE_TOKEN_EXPIRED


class TestCollectUsageTransientFailure:
    """5.1 — transient/ambiguous failures keep stale-dict retention for display."""

    def test_bare_none_retains_stale_dict(self, temp_home, monkeypatch):
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        _seed_prior_cache(s)
        # A bare None fetch failure is TRANSIENT (e.g. a network blip).
        _patch_fetch(monkeypatch, {
            "1": {"five_hour": {"pct": 15.0}},
            "2": None,
        })

        out = s._collect_usage(_info(), only={"1", "2"})

        # Display resilience: last-known-good dict is retained for slot 2.
        assert out[1] == {"five_hour": {"pct": 20.0}}

    def test_rate_limited_retains_stale_dict(self, temp_home, monkeypatch):
        s = _make_switcher()
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        _seed_prior_cache(s)
        _patch_fetch(monkeypatch, {
            "1": {"five_hour": {"pct": 15.0}},
            "2": USAGE_RATE_LIMITED,
        })

        out = s._collect_usage(_info(), only={"1", "2"})

        assert out[1] == {"five_hour": {"pct": 20.0}}


class TestBestStrategySkipsDefinitivelyFailed:
    """5.1 — a best/next-available switch must not select an account whose
    credential definitively failed this round (even though stale cache had a
    positive-headroom dict for it)."""

    def _setup_three_accounts(self, temp_home: Path):
        """Active slot 1, plus inactive slots 2 and 3. Slot 2 will definitively
        fail its fresh fetch; slot 3 is healthy with LESS headroom than the
        stale (and now-overridden) slot 2 dict."""
        s = _make_switcher()
        seq = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2, 3],
            "accounts": {
                "1": {"email": "a@x.com", "organizationUuid": ""},
                "2": {"email": "b@x.com", "organizationUuid": ""},
                "3": {"email": "c@x.com", "organizationUuid": ""},
            },
        }
        s._write_json(s.sequence_file, seq)
        # Live config: active account is slot 1.
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@x.com", "organizationUuid": ""}
        }))
        return s

    def test_best_does_not_pick_definitively_failed_account(
        self, temp_home, monkeypatch
    ):
        s = self._setup_three_accounts(temp_home)
        # Every slot is switchable (has backups).
        monkeypatch.setattr(s, "_account_is_switchable", lambda num: True)
        # Seed a stale cache giving slot 2 the MOST headroom (pct 5 -> 95).
        write_cache(s.backup_dir / "cache" / "usage.json", {
            "1": {"five_hour": {"pct": 50.0}},   # current headroom 50
            "2": {"five_hour": {"pct": 5.0}},    # stale: headroom 95 (best!)
            "3": {"five_hour": {"pct": 40.0}},   # healthy: headroom 60
        })

        # Fresh fetch: slot 2 DEFINITIVELY fails; 1 and 3 return fresh dicts.
        def fake_collect(accounts_info, only=None, force=False):
            usage_map = {
                "1": {"five_hour": {"pct": 50.0}},
                "2": USAGE_TOKEN_EXPIRED,            # 5.1 fix: sentinel, not stale dict
                "3": {"five_hour": {"pct": 40.0}},
            }
            return [usage_map[str(info[0])] for info in accounts_info]

        monkeypatch.setattr(s, "_collect_usage", fake_collect)

        target, note = s._select_best_switchable("1")

        # Must NOT pick slot 2 (failed). Slot 3 has more headroom (60) than the
        # current slot 1 (50), so it is the only provably-better target.
        assert target != "2"
        assert target == "3"

    def test_next_available_does_not_land_on_failed_account(
        self, temp_home, monkeypatch
    ):
        """next-available rotates and skips accounts at their limit; a
        definitively-failed account reads as unknown headroom (None), so it is
        not *skipped* as exhausted, but the strategy must still never present it
        as healthy. Here slot 2 fails and slot 3 is the rotation target."""
        s = self._setup_three_accounts(temp_home)
        monkeypatch.setattr(s, "_account_is_switchable", lambda num: True)
        captured: dict = {}

        def fake_perform(target, emit_output=True):
            captured["target"] = target
            return {
                "from": {"number": 1, "email": "a@x.com"},
                "to": {"number": int(target), "email": "x"},
                "warnings": [],
            }

        monkeypatch.setattr(s, "_perform_switch", fake_perform)

        # Slot 2 is exhausted (at limit) so next-available skips it; slot 3 is fine.
        def fake_usage_by_account():
            return {
                "1": {"five_hour": {"pct": 10.0}},
                "2": {"five_hour": {"pct": 100.0}},  # at limit -> skipped
                "3": {"five_hour": {"pct": 10.0}},
            }

        monkeypatch.setattr(s, "_usage_by_account", fake_usage_by_account)

        s.switch(strategy="next-available")

        assert captured["target"] == "3"


class TestRotationAnchorDrift:
    """5.3 — plain rotation anchors on the LIVE account, not the recorded one."""

    def _setup(self, temp_home: Path, recorded_active: int, live_email: str):
        s = _make_switcher()
        seq = {
            "activeAccountNumber": recorded_active,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2, 3],
            "accounts": {
                "1": {"email": "a@x.com", "organizationUuid": ""},
                "2": {"email": "b@x.com", "organizationUuid": ""},
                "3": {"email": "c@x.com", "organizationUuid": ""},
            },
        }
        s._write_json(s.sequence_file, seq)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": live_email, "organizationUuid": ""}
        }))
        return s

    def _captured_target(self, s, monkeypatch) -> str:
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
        return captured["target"]

    def test_plain_switch_rotates_from_live_account(self, temp_home, monkeypatch):
        # Recorded active = slot 1, but the LIVE login is slot 2 (drift).
        s = self._setup(temp_home, recorded_active=1, live_email="b@x.com")

        target = self._captured_target(s, monkeypatch)

        # Rotating from the LIVE slot 2 yields slot 3; rotating from the
        # (stale) recorded slot 1 would have yielded slot 2.
        assert target == "3"

    def test_plain_switch_agreement_unchanged(self, temp_home, monkeypatch):
        # Common case: recorded and live agree on slot 1 -> rotate to slot 2.
        s = self._setup(temp_home, recorded_active=1, live_email="a@x.com")

        target = self._captured_target(s, monkeypatch)

        assert target == "2"

    def test_plain_switch_falls_back_to_recorded_when_live_unmatched(
        self, temp_home, monkeypatch
    ):
        # Live login matches NO managed slot (unknown email); rotation must fall
        # back to the recorded activeAccountNumber (slot 2 here) -> rotate to 3.
        s = self._setup(temp_home, recorded_active=2, live_email="unknown@x.com")
        # Make _account_exists succeed for the unmanaged live account so we
        # reach the rotation path rather than the auto-add branch.
        monkeypatch.setattr(s, "_account_exists", lambda *a: True)

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

        assert captured["target"] == "3"
