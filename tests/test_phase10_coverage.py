"""Phase 10 fix 10.3 — fill two test-coverage gaps (tests only, no source changes).

F83 (oauth_login): the real network helper ``exchange_code``'s SUCCESS path and
``run_login_flow``'s state-replay/validation + happy paths were under-tested. The
error branches of ``exchange_code`` already have coverage in
``tests/test_oauth_login.py``; here we cover the 200-OK success branch (urllib is
mocked at the boundary, never hitting the network) and re-exercise the
orchestrator's state-validation and happy paths through its injected seams.

F84 (migrations): the ``run_migrations`` branch where a migration COMPLETES but
``_mark_applied`` RAISES (recording the success fails) was untested. The contract
is that this never aborts switcher construction and leaves the migration unmarked
so it re-runs next time.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from claude_swap import migrations, oauth_login
from claude_swap.migrations import run_migrations
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


# ===========================================================================
# F83 — oauth_login: exchange_code SUCCESS + run_login_flow validation/happy
# ===========================================================================


class _FakeResponse:
    """Context-manager stand-in for urllib's HTTPResponse: ``.read()`` returns the
    body bytes; usable in ``with urllib.request.urlopen(...) as resp``."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_exchange_code_success_parses_token_json(monkeypatch):
    # F83(a): a 200 JSON token response is parsed and returned verbatim as a dict.
    # urllib is mocked at the boundary — no socket, no network.
    token = {
        "access_token": "sk-at",
        "refresh_token": "sk-rt",
        "expires_in": 3600,
        "scope": "user:profile user:inference",
        "account": {"uuid": "acc-uuid", "email_address": "me@example.com"},
        "organization": {"uuid": "org-uuid", "name": "Acme"},
    }
    captured = {}

    def fake_urlopen(req, timeout=None):
        # The request must be a real POST to the token endpoint carrying our body.
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse(json.dumps(token).encode())

    monkeypatch.setattr(oauth_login.urllib.request, "urlopen", fake_urlopen)

    result = oauth_login.exchange_code(
        code="AUTHCODE",
        verifier="VERIFIER",
        redirect_uri="http://127.0.0.1:5000/callback",
        state="STATE123",
    )

    # Returns the parsed dict including the access token, untouched.
    assert result == token
    assert result["access_token"] == "sk-at"
    # The request hit the configured token endpoint via POST with the built body.
    assert captured["url"] == oauth_login.OAUTH_TOKEN_URL
    assert captured["method"] == "POST"
    assert captured["timeout"] == 15
    assert captured["body"]["grant_type"] == "authorization_code"
    assert captured["body"]["code"] == "AUTHCODE"
    assert captured["body"]["code_verifier"] == "VERIFIER"
    assert captured["body"]["state"] == "STATE123"


def _fake_server(query, port=54321):
    """Injected ``make_server`` stand-in: ``wait()`` returns a fixed callback query
    instead of binding a loopback port. Records whether shutdown ran."""

    class _S:
        def __init__(self):
            self.port = port
            self.shut = False

        def wait(self, timeout):
            return query

        def shutdown(self):
            self.shut = True

    return _S()


def test_run_login_flow_state_mismatch_raises_login_error():
    # F83(b): the callback echoes a DIFFERENT state than the one we generated →
    # a state-replay/cross-request error. The exchange must never be attempted.
    exchange_called = {"count": 0}

    def exchange(**kwargs):
        exchange_called["count"] += 1
        return {"access_token": "should-not-happen"}

    with pytest.raises(oauth_login.LoginError) as exc:
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server("code=CODE&state=ATTACKER"),
            exchange=exchange,
            gen_pkce=lambda: ("VERIFIER", "CHALLENGE"),
            gen_state=lambda: "EXPECTED",
            now=lambda: 0.0,
        )

    assert "state mismatch" in str(exc.value).lower()
    # Validation happens before the network exchange — it was never called.
    assert exchange_called["count"] == 0


def test_run_login_flow_happy_path_returns_login_result_with_credentials_and_identity():
    # F83(c): a matching-state callback + a good token exchange yields a
    # LoginResult carrying the serialized credentials and the parsed identity.
    server = _fake_server("code=AUTHCODE&state=STATE123")

    def exchange(code, verifier, redirect_uri, state):
        return {
            "access_token": "sk-at",
            "refresh_token": "sk-rt",
            "expires_in": 3600,
            "scope": "user:profile user:inference",
            "account": {"uuid": "acc-uuid", "email_address": "me@example.com"},
            "organization": {"uuid": "org-uuid", "name": "Acme"},
        }

    result = oauth_login.run_login_flow(
        open_browser=lambda url: None,
        make_server=lambda: server,
        exchange=exchange,
        gen_pkce=lambda: ("VERIFIER", "CHALLENGE"),
        gen_state=lambda: "STATE123",
        now=lambda: 1000.0,
    )

    assert isinstance(result, oauth_login.LoginResult)
    oauth = json.loads(result.credentials)["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-at"
    assert oauth["refreshToken"] == "sk-rt"
    assert oauth["expiresAt"] == 1000.0 * 1000 + 3600 * 1000
    assert oauth["scopes"] == ["user:profile", "user:inference"]
    assert result.identity == oauth_login.Identity(
        email="me@example.com",
        org_name="Acme",
        org_uuid="org-uuid",
        account_uuid="acc-uuid",
    )
    # The injected loopback server was torn down in the finally block.
    assert server.shut is True


# ===========================================================================
# F84 — migrations: migration COMPLETES but _mark_applied RAISES
# ===========================================================================


def _make_switcher() -> ClaudeAccountSwitcher:
    """A switcher with its backup dir materialized so run_migrations does real work
    (it no-ops when the backup dir is absent)."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._setup_directories()
    return switcher


def test_run_migrations_swallows_mark_applied_failure_and_leaves_unmarked(
    temp_home, monkeypatch
):
    # F84: a migration returns True (completed) but recording it via _mark_applied
    # raises. Contract: run_migrations must NOT raise (never break construction),
    # must log the "ran but recording it failed" warning, and must leave the
    # migration UNMARKED so it re-runs next time.
    switcher = _make_switcher()
    switcher._logger = MagicMock()
    real_mark_applied = migrations._mark_applied  # genuine recorder, restored later

    runs = {"count": 0}

    def fake_migration(_switcher):
        runs["count"] += 1
        return True  # completed

    monkeypatch.setattr(
        migrations, "MIGRATIONS", [("phase10_recording_canary", fake_migration)]
    )

    def boom_mark_applied(_switcher, _migration_id):
        raise OSError("disk full recording migration")

    monkeypatch.setattr(migrations, "_mark_applied", boom_mark_applied)

    # Does NOT raise despite the recording failure.
    run_migrations(switcher)

    assert runs["count"] == 1  # the migration ran and completed
    # The "ran but recording it failed" warning was logged.
    warnings = " ".join(
        str(c.args[0]) for c in switcher._logger.warning.call_args_list
    )
    assert "ran but recording it failed" in warnings
    assert "phase10_recording_canary" in warnings

    # Not recorded: no state file (or at least our migration absent) → it re-runs.
    state_file = switcher.backup_dir / ".migrations.json"
    if state_file.exists():
        applied = json.loads(state_file.read_text()).get("applied", {})
        assert "phase10_recording_canary" not in applied

    # Prove the re-run: a second pass (recording now working again) runs it again
    # and this time records it successfully.
    monkeypatch.setattr(migrations, "_mark_applied", real_mark_applied)
    run_migrations(switcher)
    assert runs["count"] == 2  # ran again because it was never recorded
    state = json.loads((switcher.backup_dir / ".migrations.json").read_text())
    assert "phase10_recording_canary" in state["applied"]
