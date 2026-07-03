"""Tests for the menu bar's auto-timer-start (account warming) feature.

These tests never import or run rumps/AppKit. They exercise the import-safe,
module-level pure functions that back the warm cycle — candidate selection, the
per-account warm/send action, and the warm-cycle orchestration — plus the pure
menu label/flip helpers. The full ``MenuBarApp`` (a ``rumps.App`` subclass) is
not instantiated here, matching the module's import-safety pattern and the
conventions in tests/test_menubar_refresh.py.

HTTP is always mocked at ``claude_swap.oauth.urllib.request.urlopen`` — no real
network calls are made (the real feasibility send is out of scope).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
from unittest.mock import MagicMock, patch

from claude_swap import menubar


def _oauth_creds(*, access="tok", refresh="ref", expires_at=None) -> str:
    """Build an OAuth-JSON credentials string (the shape looks_like_api_key rejects)."""
    oauth: dict = {"accessToken": access}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    return json.dumps({"claudeAiOauth": oauth})


# ---------------------------------------------------------------------------
# Step 2 + 3 — pure candidate selection
# ---------------------------------------------------------------------------


class TestSelectWarmCandidates:
    """select_warm_candidates: idle detection, exclusions, cooldown, dedup."""

    def test_idle_window_no_resets_at_is_candidate(self):
        accounts = [(1, "a@x", _oauth_creds(), {"five_hour": {"pct": 0}})]
        assert menubar.select_warm_candidates(accounts, now=1000.0, cooldowns={}) == [1]

    def test_window_with_resets_at_excluded(self):
        usage = {"five_hour": {"pct": 10, "resets_at": "2026-07-01T20:00:00+00:00"}}
        accounts = [(1, "a@x", _oauth_creds(), usage)]
        assert menubar.select_warm_candidates(accounts, now=1000.0, cooldowns={}) == []

    def test_api_key_account_excluded(self):
        accounts = [(1, "a@x", "sk-ant-api-xyz", {"five_hour": {"pct": 0}})]
        assert menubar.select_warm_candidates(accounts, now=1000.0, cooldowns={}) == []

    def test_active_account_included(self):
        # The (num, email, creds, usage) input shape carries no is_active flag —
        # candidacy does not depend on active status, so an active idle account
        # is a candidate exactly like any other idle account.
        accounts = [(2, "b@x", _oauth_creds(), {"five_hour": {"pct": 0}})]
        assert menubar.select_warm_candidates(accounts, now=1000.0, cooldowns={}) == [2]

    def test_missing_five_hour_excluded(self):
        for usage in ({}, "rate limited", None, {"seven_day": {"pct": 0}}):
            accounts = [(1, "a@x", _oauth_creds(), usage)]
            assert menubar.select_warm_candidates(accounts, now=1000.0, cooldowns={}) == [], usage

    def test_no_oauth_token_still_candidate(self):
        # OAuth creds present but no accessToken -> still a candidate (attempting
        # and failing is an acceptable health signal).
        creds = json.dumps({"claudeAiOauth": {"refreshToken": "r"}})
        accounts = [(1, "a@x", creds, {"five_hour": {"pct": 0}})]
        assert menubar.select_warm_candidates(accounts, now=1000.0, cooldowns={}) == [1]

    # ---- Step 3: cooldown suppression + dedup ----

    def test_cooldown_suppresses(self):
        accounts = [(1, "a@x", _oauth_creds(), {"five_hour": {"pct": 0}})]
        cooldowns = {"1": 1000.0 - 100}
        assert menubar.select_warm_candidates(
            accounts, now=1000.0, cooldowns=cooldowns, cooldown_seconds=600
        ) == []

    def test_cooldown_expired_recandidate(self):
        accounts = [(1, "a@x", _oauth_creds(), {"five_hour": {"pct": 0}})]
        cooldowns = {"1": 1000.0 - 700}
        assert menubar.select_warm_candidates(
            accounts, now=1000.0, cooldowns=cooldowns, cooldown_seconds=600
        ) == [1]

    def test_cooldown_keyed_by_str_num(self):
        # Cooldowns are keyed by str(num); a matching str key suppresses.
        accounts = [(3, "c@x", _oauth_creds(), {"five_hour": {"pct": 0}})]
        assert menubar.select_warm_candidates(
            accounts, now=1000.0, cooldowns={"3": 1000.0}, cooldown_seconds=600
        ) == []


# ---------------------------------------------------------------------------
# Step 4 — per-account warm/send action
# ---------------------------------------------------------------------------


class TestWarmAccount:
    """warm_account: send-only Bearer+beta minimal Haiku request; never refreshes.

    The warm path deliberately does NOT refresh or rotate any OAuth token: doing
    so would rotate the server-side refresh token while persisting only to the
    backup store, corrupting the live credential Claude Code reads for the active
    account (and racing a concurrent switch for backups). A candidate only exists
    because its usage fetch just succeeded, so its stored token is API-valid for
    the immediately-following send; genuine expiry is refreshed under the lock by
    the usage-fetch path (oauth.fetch_usage_for_account) that runs first.
    """

    def _ok_response(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"content": []}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_sends_bearer_and_beta_and_model(self):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["req"] = req
            return self._ok_response()

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
            status, reason = menubar.warm_account(_oauth_creds(access="tok123"))

        assert status == "ok"
        assert reason is None
        req = captured["req"]
        assert req.full_url == "https://api.anthropic.com/v1/messages"
        assert req.get_method() == "POST"
        # urllib lowercases header keys internally
        assert req.get_header("Authorization") == "Bearer tok123"
        assert req.get_header("Anthropic-beta") == menubar.oauth.OAUTH_BETA_HEADER
        body = json.loads(req.data.decode())
        assert body["model"] == "claude-haiku-4-5"
        assert isinstance(body["max_tokens"], int) and body["max_tokens"] <= 16
        assert body["messages"] == [{"role": "user", "content": "can you hear me?"}]

    def test_401_is_failure(self):
        err = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=401, msg="Unauthorized", hdrs=None, fp=None,
        )
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            status, reason = menubar.warm_account(_oauth_creds())
        assert status == "error"
        assert reason is not None

    def test_network_error_is_failure(self):
        err = urllib.error.URLError("no route to host")
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            status, reason = menubar.warm_account(_oauth_creds())
        assert status == "error"
        assert reason is not None

    def test_expired_token_is_not_refreshed(self):
        # An expired token is NOT refreshed/rotated: the send uses the stored
        # access token as-is (the usage-fetch path already refreshed it under the
        # lock before this account became a candidate).
        expired = _oauth_creds(access="stored", refresh="r", expires_at=0)
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["auth"] = req.get_header("Authorization")
            return self._ok_response()

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("claude_swap.oauth.refresh_oauth_credentials") as rf:
            status, reason = menubar.warm_account(expired)

        rf.assert_not_called()
        assert status == "ok"
        assert captured["auth"] == "Bearer stored"

    def test_no_token_returns_error(self):
        creds = json.dumps({"claudeAiOauth": {"refreshToken": "r"}})
        with patch("claude_swap.oauth.urllib.request.urlopen") as urlopen:
            status, reason = menubar.warm_account(creds)
        urlopen.assert_not_called()
        assert status == "error"
        assert reason is not None


# ---------------------------------------------------------------------------
# Step 5 — warm-cycle orchestration
# ---------------------------------------------------------------------------


class TestRunWarmCycle:
    """run_warm_cycle: run candidates, record cooldown, notify."""

    def _accounts(self):
        return [
            (1, "a@x", _oauth_creds(), {"five_hour": {"pct": 0}}),
            (2, "b@x", _oauth_creds(), {"five_hour": {"pct": 0}}),
        ]

    def test_one_send_per_candidate(self):
        seen = []

        def warm(creds, **kw):
            seen.append(creds)
            return ("ok", None)

        menubar.run_warm_cycle(
            accounts=self._accounts(), now=1000.0, warmed_at={},
            notify_fn=lambda *a: None, warm=warm,
        )
        assert len(seen) == 2

    def test_success_records_cooldown(self):
        warmed_at = {}
        menubar.run_warm_cycle(
            accounts=[self._accounts()[0]], now=1000.0, warmed_at=warmed_at,
            notify_fn=lambda *a: None,
            warm=lambda creds, **kw: ("ok", None),
        )
        assert warmed_at["1"] == 1000.0

    def test_failure_notifies_per_account(self):
        warmed_at = {}
        notes = []
        menubar.run_warm_cycle(
            accounts=[self._accounts()[0]], now=1000.0, warmed_at=warmed_at,
            notify_fn=lambda title, msg: notes.append(msg),
            warm=lambda creds, **kw: ("error", "401"),
        )
        assert "1" not in warmed_at  # cooldown NOT recorded on failure
        assert any("Account-1" in m and "timer start failed" in m for m in notes)

    def test_success_summary_once(self):
        notes = []
        menubar.run_warm_cycle(
            accounts=self._accounts(), now=1000.0, warmed_at={},
            notify_fn=lambda title, msg: notes.append(msg),
            warm=lambda creds, **kw: ("ok", None),
        )
        summaries = [m for m in notes if "Started timers for" in m]
        assert summaries == ["Started timers for 2 account(s)"]

    def test_no_success_summary_when_zero(self):
        notes = []
        menubar.run_warm_cycle(
            accounts=self._accounts(), now=1000.0, warmed_at={},
            notify_fn=lambda title, msg: notes.append(msg),
            warm=lambda creds, **kw: ("error", "boom"),
        )
        assert not any("Started timers for" in m for m in notes)

    def test_one_failure_never_breaks_cycle(self):
        # A raising warm for one account must not prevent the others running.
        seen = []

        def warm(creds, **kw):
            seen.append(creds)
            if len(seen) == 1:
                raise RuntimeError("boom")
            return ("ok", None)

        menubar.run_warm_cycle(
            accounts=self._accounts(), now=1000.0, warmed_at={},
            notify_fn=lambda *a: None, warm=warm,
        )
        assert len(seen) == 2


# ---------------------------------------------------------------------------
# Step 6 — worker wiring
# ---------------------------------------------------------------------------


class _RunWarmHarness:
    """Minimal stand-in for MenuBarApp's worker path, mirroring _RefreshHarness."""

    def __init__(self, *, enabled: bool, full: bool = True):
        self._refresh_guard = menubar._RefreshGuard()
        self._last_full_fetch = 0.0
        self._snapshot_at = 0.0
        self.snapshot = {
            "accounts": [], "active_email": None, "active_usage": None,
            "instances": [],
        }
        self._dirty = False
        self._warmed_at = {}
        self.switcher = self
        self.settings = menubar.MenuBarSettings(auto_timer_start_enabled=enabled)
        self._threads: list[threading.Thread] = []
        self._full = full

    _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

    def recheck_keychain(self):
        pass

    def _build_accounts_info(self):
        # (num, email, org, uuid, is_active, creds)
        return [
            (1, "a@x", "", "", True, _oauth_creds()),
            (2, "b@x", "", "", False, _oauth_creds()),
        ]

    def _collect_usage(self, info, only=None, force=False, max_fetch=None):
        return [{"five_hour": {"pct": 0}} for _ in info]

    def _write_account_credentials(self, num, email, creds):
        pass

    def _spawn(self, target, args):
        t = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(t)
        t.start()

    def join_all(self, timeout=5.0):
        deadline = time.time() + timeout
        while self._threads and time.time() < deadline:
            t = self._threads.pop(0)
            t.join(timeout=max(0.0, deadline - time.time()))


class TestWorkerWiring:
    """_worker_impl runs the warm cycle on the worker thread, behind the toggle."""

    def test_worker_runs_warm_when_enabled(self, monkeypatch):
        calls = []
        monkeypatch.setattr(menubar, "run_warm_cycle",
                            lambda **kw: calls.append(kw) or 0)
        app = _RunWarmHarness(enabled=True)
        menubar._refresh_async_impl(app, full=True, force=False)
        app.join_all()
        assert app.snapshot["accounts"]  # snapshot was stored
        assert len(calls) == 1

    def test_worker_skips_warm_when_disabled(self, monkeypatch):
        calls = []
        monkeypatch.setattr(menubar, "run_warm_cycle",
                            lambda **kw: calls.append(kw) or 0)
        app = _RunWarmHarness(enabled=False)
        menubar._refresh_async_impl(app, full=True, force=False)
        app.join_all()
        assert calls == []

    def test_warm_passes_all_accounts_creds(self, monkeypatch):
        calls = []
        monkeypatch.setattr(menubar, "run_warm_cycle",
                            lambda **kw: calls.append(kw) or 0)
        app = _RunWarmHarness(enabled=True, full=False)
        menubar._refresh_async_impl(app, full=False, force=False)
        app.join_all()
        assert len(calls) == 1
        accounts = calls[0]["accounts"]
        # Uses _build_accounts_info creds for ALL accounts, not just active.
        nums = sorted(a[0] for a in accounts)
        assert nums == [1, 2]
        assert all(a[2] for a in accounts)  # every entry carries creds

    def test_warm_never_breaks_worker(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("warm blew up")
        monkeypatch.setattr(menubar, "run_warm_cycle", boom)
        app = _RunWarmHarness(enabled=True)
        menubar._refresh_async_impl(app, full=True, force=False)
        app.join_all()
        assert app.snapshot["accounts"]  # snapshot still stored
        assert app._dirty is True


class TestWarmNeverWritesCredentials:
    """The warm path is send-only: it never rotates or persists any credential.

    Regression guard for two confirmed defects the first cut had:
      * warming the ACTIVE account refreshed+rotated its OAuth token but wrote
        only to the backup store, invalidating the live creds Claude Code reads
        (invalid_grant / forced re-login);
      * the (backup) persist ran off the refresh worker WITHOUT the sequence
        lock, racing a concurrent switch's read-modify-write of the backup.
    Both vanish if the warm path never writes credentials at all — which is safe
    because a candidate's token was just validated by the usage fetch, and the
    usage-fetch path already refreshes genuinely-expired backups under the lock.
    """

    def _ok_response(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"content": []}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_run_warm_from_snapshot_never_refreshes_or_writes(self):
        writes = []

        class _App:
            _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

            def __init__(self):
                self._warmed_at = {}
                self.switcher = self

            def _build_accounts_info(self):
                # (num, email, org, uuid, is_active, creds); tokens marked expired
                # (expiresAt=0) to prove even an "expired" token is NOT refreshed.
                return [
                    (1, "a@x", "", "", True, _oauth_creds(access="live-active", expires_at=0)),
                    (2, "b@x", "", "", False, _oauth_creds(access="backup", expires_at=0)),
                ]

            def _write_account_credentials(self, num, email, creds):
                writes.append((num, email, creds))

        snap = {
            "accounts": [
                (1, "a@x", True, {"five_hour": {"pct": 0}}),
                (2, "b@x", False, {"five_hour": {"pct": 0}}),
            ],
        }
        sends = []

        def fake_urlopen(req, timeout=0):
            sends.append(req.get_header("Authorization"))
            return self._ok_response()

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("claude_swap.oauth.refresh_oauth_credentials") as rf, \
             patch("claude_swap.notify.notify"):
            menubar._run_warm_from_snapshot(_App(), snap)

        assert writes == []          # warm path NEVER persists credentials
        rf.assert_not_called()       # and NEVER refreshes/rotates a token
        # Both accounts warmed with their stored token as-is (incl. the active one).
        assert sorted(sends) == ["Bearer backup", "Bearer live-active"]


# ---------------------------------------------------------------------------
# Step 7 — menu toggle pure helpers
# ---------------------------------------------------------------------------


class TestToggleHelpers:
    def test_auto_timer_header_line(self):
        assert menubar.auto_timer_start_label(True) == "Auto timer start: ON"
        assert menubar.auto_timer_start_label(False) == "Auto timer start: OFF"

    def test_toggle_callback_flips_and_saves(self, tmp_path):
        s = menubar.MenuBarSettings(auto_timer_start_enabled=False)
        s = menubar.toggle_auto_timer_start(s)
        assert s.auto_timer_start_enabled is True
        s = menubar.toggle_auto_timer_start(s)
        assert s.auto_timer_start_enabled is False
        # round-trips through save/load
        path = tmp_path / "settings.json"
        flipped = menubar.toggle_auto_timer_start(s)
        flipped.save(path)
        assert menubar.MenuBarSettings.load(path).auto_timer_start_enabled is True
