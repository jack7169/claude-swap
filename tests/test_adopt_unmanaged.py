"""Auto-adoption of an externally signed-in (unmanaged) live account.

A user can run ``claude /login`` and land on an account cswap doesn't manage.
The interactive CLI ``switch()`` has always auto-added that account, but the
menu bar — which is what's actually running long-term — never detected it: the
live login rendered as a bare icon with no active checkmark and was silently
excluded from rotation (the reported bug).

Two layers under test:
- ``ClaudeAccountSwitcher.adopt_unmanaged_active()`` — the non-interactive
  switcher primitive: capture the live login into a new slot iff there is a
  live login, at least one account is already managed, and the live identity
  matches no managed slot.
- ``menubar._maybe_adopt_unmanaged(app)`` — the import-safe (no rumps) refresh
  hook: calls the primitive, notifies on success, never raises, and remembers
  a failed identity so a hopeless login (e.g. API key) isn't retried/re-notified
  every refresh tick.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import menubar
from claude_swap.credentials import Platform
from claude_swap.exceptions import ValidationError
from claude_swap.paths import get_backup_root
from claude_swap.switcher import ClaudeAccountSwitcher


def _file_mode_switcher() -> ClaudeAccountSwitcher:
    """Switcher pinned to the file credential backend (no Keychain paths)."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    return switcher


def _write_live_creds(temp_home: Path, payload: str | None = None) -> None:
    creds = payload if payload is not None else json.dumps(
        {"claudeAiOauth": {"accessToken": "sk-live-token"}}
    )
    cred_path = temp_home / ".claude" / ".credentials.json"
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(creds)


class TestAdoptUnmanagedActive:
    """The switcher primitive."""

    def test_returns_none_without_live_login(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """No oauthAccount in ~/.claude.json -> nothing to adopt."""
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher.adopt_unmanaged_active() is None
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 2

    def test_returns_none_when_no_sequence_file(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """Fresh machine (nothing managed yet): adopt must not bootstrap state."""
        switcher = _file_mode_switcher()
        _write_live_creds(temp_home)

        assert switcher.adopt_unmanaged_active() is None
        assert not switcher.sequence_file.exists()

    def test_returns_none_when_no_accounts_managed(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """Empty accounts dict (e.g. after purge): mirror switch()'s precondition."""
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [],
            "accounts": {},
        })
        _write_live_creds(temp_home)

        assert switcher.adopt_unmanaged_active() is None
        data = switcher._get_sequence_data()
        assert data["accounts"] == {}

    def test_returns_none_when_live_identity_already_managed(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """Managed live login -> no-op, and no add_account refresh side effect."""
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "test@example.com", "uuid": "test-uuid-1234",
                      "added": "2024-01-01T00:00:00Z"},
                "2": {"email": "other@example.com", "uuid": "uuid-2",
                      "added": "2024-01-02T00:00:00Z"},
            },
        })
        _write_live_creds(temp_home)

        with patch.object(switcher, "add_account") as add:
            assert switcher.adopt_unmanaged_active() is None
        add.assert_not_called()

    def test_adopts_unmanaged_live_login(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Unmanaged live login is captured into the next slot and made active."""
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        live_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-new-acct"}})
        _write_live_creds(temp_home, live_creds)

        adopted = switcher.adopt_unmanaged_active()

        assert adopted == ("3", "test@example.com")
        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "test@example.com"
        assert data["activeAccountNumber"] == 3
        assert 3 in data["sequence"]
        # Backups captured so the account can be switched back to later.
        assert switcher._read_account_credentials("3", "test@example.com") == live_creds
        backed_config = json.loads(
            switcher._read_account_config("3", "test@example.com")
        )
        assert backed_config["oauthAccount"]["emailAddress"] == "test@example.com"

    def test_no_duplicate_after_org_migration(
        self, temp_home: Path, sample_sequence_data_pre_v06: dict
    ):
        """Pre-v0.6.0 record matching the live login after migration: no adopt.

        The membership check must run on migrated data (like switch() does),
        otherwise an upgraded account looks unmanaged and gets duplicated.
        """
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(
            json.dumps(sample_sequence_data_pre_v06)
        )
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        }))
        switcher = _file_mode_switcher()
        _write_live_creds(temp_home)

        assert switcher.adopt_unmanaged_active() is None
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 2

    def test_api_key_login_rejection_propagates(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A live API-key login can't be captured as OAuth; the error surfaces."""
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        _write_live_creds(temp_home, "sk-ant-api03-abcdef")

        with pytest.raises(ValidationError):
            switcher.adopt_unmanaged_active()

    def test_locked_recheck_prevents_duplicate_slot(
        self, temp_home: Path, mock_claude_config: Path,
    ):
        """A concurrent add committing the same identity between the unlocked
        membership check and the lock must not produce a duplicate slot.

        Simulated by making the unlocked pre-check report the identity absent
        while sequence.json already records it: add_account must reuse the
        existing slot under the lock instead of allocating a fresh one.
        """
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        live_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-race"}})
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {"email": "test@example.com", "uuid": "test-uuid-1234",
                      "added": "2024-01-01T00:00:00Z"},
            },
        })
        _write_live_creds(temp_home, live_creds)

        with patch.object(switcher, "_account_exists", return_value=False):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1  # reused slot 1, no duplicate
        assert data["sequence"] == [1]
        assert data["activeAccountNumber"] == 1
        assert data["accounts"]["1"]["added"] == "2024-01-01T00:00:00Z"
        assert switcher._read_account_credentials("1", "test@example.com") == live_creds


def _install_switch_patches(switcher, creds_store, configs_store, live_state):
    """Patch credential/config IO onto dict stores (mirrors test_json_output)."""
    patches = [
        patch.object(switcher, "_read_account_credentials",
                     side_effect=lambda n, e: creds_store.get((str(n), e), "")),
        patch.object(switcher, "_write_account_credentials",
                     side_effect=lambda n, e, c: creds_store.__setitem__((str(n), e), c)),
        patch.object(switcher, "_read_account_config",
                     side_effect=lambda n, e: configs_store.get((str(n), e), "")),
        patch.object(switcher, "_write_account_config",
                     side_effect=lambda n, e, c: configs_store.__setitem__((str(n), e), c)),
        patch.object(switcher, "_read_credentials",
                     side_effect=lambda: live_state.get("creds", "")),
        patch.object(switcher, "_write_credentials",
                     side_effect=lambda c: live_state.__setitem__("creds", c)),
        # Don't make network calls from the (suppressed) post-switch usage path.
        patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
    ]
    for p in patches:
        p.start()
    return patches


class TestSwitchToAdoptsUnmanaged:
    """switch_to must never discard an unmanaged live login's credentials.

    Before this fix, switching away from an unmanaged account routed through
    the direct-activation path, which takes no backup of the live credential —
    the login was silently lost. Now the unmanaged account is adopted into a
    new slot first, so the switch takes the normal backup-current path.
    """

    def _setup(self, temp_home, sample_sequence_data):
        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        live_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-unmanaged"}})
        creds = {("2", "account2@example.com"): json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-2"}})}
        configs = {("2", "account2@example.com"): json.dumps(
            {"oauthAccount": {"emailAddress": "account2@example.com",
                              "accountUuid": "uuid-2"}})}
        live = {"creds": live_creds}
        return switcher, creds, configs, live, live_creds

    def test_json_mode_adopts_then_switches(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, capsys,
    ):
        switcher, creds, configs, live, live_creds = self._setup(
            temp_home, sample_sequence_data
        )
        patches = _install_switch_patches(switcher, creds, configs, live)
        try:
            result = switcher.switch_to("2", json_output=True)
        finally:
            for p in patches:
                p.stop()

        # The unmanaged login was adopted first: its credentials survive in
        # the new slot's backup instead of being discarded.
        assert creds[("3", "test@example.com")] == live_creds
        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "test@example.com"

        assert result["switched"] is True
        assert result["from"] == {"number": 3, "email": "test@example.com"}
        assert result["to"]["number"] == 2
        assert any("test@example.com" in w for w in result["warnings"])
        # JSON mode stays print-free (adoption must not leak human output).
        assert capsys.readouterr().out == ""

    def test_human_mode_adopts_then_switches(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, capsys,
    ):
        switcher, creds, configs, live, live_creds = self._setup(
            temp_home, sample_sequence_data
        )
        patches = _install_switch_patches(switcher, creds, configs, live)
        try:
            switcher.switch_to("2", json_output=False)
        finally:
            for p in patches:
                p.stop()

        assert creds[("3", "test@example.com")] == live_creds
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 2  # switch completed
        out = capsys.readouterr().out
        assert "Account-3" in out  # the adoption was announced

    def test_switch_proceeds_when_adoption_fails(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Adoption failure degrades to the old discard behavior, never blocks."""
        switcher, creds, configs, live, _ = self._setup(
            temp_home, sample_sequence_data
        )
        patches = _install_switch_patches(switcher, creds, configs, live)
        try:
            with patch.object(
                switcher, "adopt_unmanaged_active",
                side_effect=ValidationError("API-key login"),
            ):
                result = switcher.switch_to("2", json_output=True)
        finally:
            for p in patches:
                p.stop()

        assert result["switched"] is True
        assert result["from"] == {"number": None, "email": "test@example.com"}
        assert result["to"]["number"] == 2
        assert any("API-key login" in w for w in result["warnings"])


class TestTokenlessLiveCredentialGuard:
    """Never capture a token-less live credential over a good backup.

    ``claude /logout`` leaves Claude Code's keychain item in place holding
    only ``mcpOAuth`` server tokens — the ``claudeAiOauth`` section is
    removed while ``~/.claude.json``'s oauthAccount can survive. Backing that
    blob up (switch Step 1, add, adopt) silently destroys the slot's only
    good backup: it is non-empty, so the empty-credential abort passes.
    Observed destroying a real backup in the wild before this guard.
    """

    MCP_ONLY = json.dumps(
        {"mcpOAuth": {"plugin:x|abc": {"accessToken": "mcp-token"}}}
    )

    def test_switch_refuses_to_overwrite_backup_with_tokenless_creds(
        self, temp_home: Path, mock_claude_config: Path,
    ):
        from claude_swap.exceptions import CredentialReadError

        switcher = _file_mode_switcher()
        switcher._setup_directories()
        # The live identity (test@example.com) is managed as slot 1, but the
        # live credential store holds a token-less post-logout blob.
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "test@example.com", "uuid": "test-uuid-1234",
                      "added": "2024-01-01T00:00:00Z"},
                "2": {"email": "account2@example.com", "uuid": "uuid-2",
                      "added": "2024-01-02T00:00:00Z"},
            },
        })
        good_backup = json.dumps({"claudeAiOauth": {"accessToken": "sk-good"}})
        creds = {
            ("1", "test@example.com"): good_backup,
            ("2", "account2@example.com"): json.dumps(
                {"claudeAiOauth": {"accessToken": "sk-2"}}),
        }
        configs = {("2", "account2@example.com"): json.dumps(
            {"oauthAccount": {"emailAddress": "account2@example.com",
                              "accountUuid": "uuid-2"}})}
        live = {"creds": self.MCP_ONLY}
        patches = _install_switch_patches(switcher, creds, configs, live)
        try:
            with pytest.raises(CredentialReadError):
                switcher.switch_to("2", json_output=True)
        finally:
            for p in patches:
                p.stop()

        # The good backup survived; nothing switched.
        assert creds[("1", "test@example.com")] == good_backup
        assert live["creds"] == self.MCP_ONLY

    def test_adopt_rejects_tokenless_live_creds(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        from claude_swap.exceptions import CredentialReadError

        switcher = _file_mode_switcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        _write_live_creds(temp_home, self.MCP_ONLY)

        with pytest.raises(CredentialReadError):
            switcher.adopt_unmanaged_active()
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 2  # nothing captured


class _AdoptApp:
    """Minimal stand-in for the parts of MenuBarApp the adopt hook touches."""

    def __init__(self, switcher, seen=None):
        self.switcher = switcher
        # Pre-seeding `seen` simulates the identity having been observed on a
        # prior refresh pass long enough ago that the settle window elapsed.
        self._adopt_seen_identity = seen
        self._adopt_seen_at = 0.0
        self._adopt_retry_at = 0.0
        self._adopt_notified_identity = None


class _AdoptSwitcher:
    """Scriptable switcher for the menubar hook tests."""

    _logger = type(
        "L", (),
        {"debug": staticmethod(lambda *a, **k: None),
         "warning": staticmethod(lambda *a, **k: None)},
    )()

    def __init__(self, identity=("new@example.com", ""), result=None, error=None):
        self.identity = identity
        self.result = result
        self.error = error
        self.adopt_calls = 0

    def _get_current_account(self):
        return self.identity

    def adopt_unmanaged_active(self):
        self.adopt_calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class TestMaybeAdoptUnmanaged:
    """The import-safe menubar refresh hook."""

    def test_first_sighting_waits_for_login_to_settle(self, monkeypatch):
        """Adopt only after the same identity has been sighted AND a settle
        window has elapsed.

        `claude /login` writes ~/.claude.json (identity) and the credential
        store as two separate writes; adopting on the very first refresh after
        the config write could capture the NEW identity with the OLD tokens.
        Two worker passes can run back-to-back (a queued forced refresh), so
        counting passes alone is not enough — real time must have elapsed.
        """
        sent = []
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda title, msg: sent.append((title, msg)))
        sw = _AdoptSwitcher(result=("3", "new@example.com"))
        app = _AdoptApp(sw)  # no prior sighting

        assert menubar._maybe_adopt_unmanaged(app) is None
        assert sw.adopt_calls == 0  # first sighting: observe only
        assert sent == []

        # An immediate second pass (queued follow-up refresh): still too soon.
        assert menubar._maybe_adopt_unmanaged(app) is None
        assert sw.adopt_calls == 0

        # Once the settle window has elapsed, adopt.
        app._adopt_seen_at -= 60.0
        assert menubar._maybe_adopt_unmanaged(app) == ("3", "new@example.com")
        assert sw.adopt_calls == 1

    def test_adopts_and_notifies_on_success(self, monkeypatch):
        sent = []
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda title, msg: sent.append((title, msg)))
        sw = _AdoptSwitcher(result=("3", "new@example.com"))
        app = _AdoptApp(sw, seen=("new@example.com", ""))

        assert menubar._maybe_adopt_unmanaged(app) == ("3", "new@example.com")
        assert sw.adopt_calls == 1
        assert len(sent) == 1
        assert "new@example.com" in sent[0][1]
        assert "3" in sent[0][1]

    def test_nothing_to_adopt_is_silent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda title, msg: sent.append((title, msg)))
        sw = _AdoptSwitcher(result=None)
        app = _AdoptApp(sw, seen=("new@example.com", ""))

        assert menubar._maybe_adopt_unmanaged(app) is None
        assert sent == []

    def test_failure_is_not_retried_for_same_identity(self, monkeypatch):
        """A failed login (e.g. API key) must not retry/re-notify every tick."""
        sent = []
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda title, msg: sent.append((title, msg)))
        sw = _AdoptSwitcher(identity=("key@example.com", ""),
                            error=ValidationError("API-key login"))
        app = _AdoptApp(sw, seen=("key@example.com", ""))

        assert menubar._maybe_adopt_unmanaged(app) is None
        assert menubar._maybe_adopt_unmanaged(app) is None
        assert sw.adopt_calls == 1  # second tick skipped the adopt entirely
        assert len(sent) == 1  # notified once, not per tick

    def test_failure_retries_after_cooldown_without_renotifying(self, monkeypatch):
        """A transient failure (keychain hiccup) self-heals: the cooldown
        expires, adoption retries, and the earlier failure isn't re-notified.
        (The old permanent per-identity blacklist meant one hiccup disabled
        adoption for that login for the process lifetime.)"""
        sent = []
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda title, msg: sent.append((title, msg)))
        sw = _AdoptSwitcher(identity=("x@example.com", ""),
                            error=OSError("keychain hiccup"))
        app = _AdoptApp(sw, seen=("x@example.com", ""))

        assert menubar._maybe_adopt_unmanaged(app) is None
        assert sw.adopt_calls == 1
        assert len(sent) == 1

        # Cooldown elapses; the hiccup is gone — the retry succeeds.
        app._adopt_retry_at = 0.0
        sw.error = None
        sw.result = ("3", "x@example.com")
        assert menubar._maybe_adopt_unmanaged(app) == ("3", "x@example.com")
        assert sw.adopt_calls == 2
        # Exactly one failure notification + one success notification.
        assert len(sent) == 2
        assert "Added" in sent[1][1]

    def test_failure_guard_clears_when_identity_changes(self, monkeypatch):
        sent = []
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda title, msg: sent.append((title, msg)))
        sw = _AdoptSwitcher(identity=("key@example.com", ""),
                            error=ValidationError("API-key login"))
        app = _AdoptApp(sw, seen=("key@example.com", ""))

        assert menubar._maybe_adopt_unmanaged(app) is None
        assert sw.adopt_calls == 1

        # The user logs into a different account: the cooldown must not carry
        # over, and the settle debounce applies to the new identity afresh.
        sw.identity = ("fresh@example.com", "")
        sw.error = None
        sw.result = ("4", "fresh@example.com")
        assert menubar._maybe_adopt_unmanaged(app) is None  # first sighting
        app._adopt_seen_at -= 60.0  # settle window elapses
        assert menubar._maybe_adopt_unmanaged(app) == ("4", "fresh@example.com")
        assert sw.adopt_calls == 2

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(menubar.notify, "notify",
                            lambda *a, **k: None)

        class _Broken:
            def _get_current_account(self):
                raise RuntimeError("boom")

        assert menubar._maybe_adopt_unmanaged(_AdoptApp(_Broken())) is None


class TestWorkerAdoptsBeforeSnapshot:
    """The refresh worker adopts first so the new account renders immediately."""

    def test_worker_runs_adopt_before_snapshot(self, monkeypatch):
        calls = []

        def fake_snapshot(switcher, full=True, force=False, max_fetch=None):
            calls.append("snapshot")
            return {"accounts": [], "active_email": None, "active_usage": None,
                    "instances": []}

        monkeypatch.setattr(menubar, "_snapshot", fake_snapshot)
        monkeypatch.setattr(menubar.notify, "notify", lambda *a, **k: None)

        class _Harness:
            _logger = type(
                "L", (), {"debug": staticmethod(lambda *a, **k: None)}
            )()

            def __init__(self):
                self._refresh_guard = menubar._RefreshGuard()
                self._last_full_fetch = 0.0
                self._snapshot_at = 0.0
                self.snapshot = {"accounts": [], "active_email": None,
                                 "active_usage": None, "instances": []}
                self._dirty = False
                # identity sighted long ago -> settle window elapsed -> adopt
                self._adopt_seen_identity = ("new@example.com", "")
                self._adopt_seen_at = 0.0
                self._adopt_retry_at = 0.0
                self._adopt_notified_identity = None
                self.switcher = self

            def recheck_keychain(self):
                pass

            def _get_current_account(self):
                return ("new@example.com", "")

            def adopt_unmanaged_active(self):
                calls.append("adopt")
                return None

        app = _Harness()
        menubar._worker_impl(app, full=True)

        assert calls == ["adopt", "snapshot"]
