from __future__ import annotations

import base64
import hashlib
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from claude_swap import oauth_login


def test_b64url_strips_padding_and_is_urlsafe():
    # 0xFB,0xFF -> standard b64 has '+'/'/'; urlsafe uses '-'/'_'; no '='
    assert oauth_login._b64url(b"\xfb\xff\xfe") == "-__-"


def test_generate_pkce_verifier_charset_and_challenge_is_s256_of_verifier():
    verifier, challenge = oauth_login.generate_pkce(rand=lambda n: b"\x00" * n)
    # 32 zero bytes -> base64url no padding -> 43 chars, all 'A' then ''
    assert verifier == base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected_challenge
    assert "=" not in verifier and "=" not in challenge


def test_build_authorize_url_has_all_oauth_params():
    url = oauth_login.build_authorize_url(
        redirect_uri="http://localhost:5000/callback",
        state="st-123",
        code_challenge="chal-abc",
    )
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == oauth_login.AUTHORIZE_URL
    q = parse_qs(parsed.query)
    assert q["client_id"] == [oauth_login.OAUTH_CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["http://localhost:5000/callback"]
    assert q["scope"] == [oauth_login.LOGIN_SCOPES]
    assert q["state"] == ["st-123"]
    assert q["code_challenge"] == ["chal-abc"]
    assert q["code_challenge_method"] == ["S256"]


def test_parse_callback_success():
    r = oauth_login.parse_callback_query("code=abc123&state=st-9")
    assert (r.code, r.state, r.error) == ("abc123", "st-9", None)


def test_parse_callback_user_denied():
    r = oauth_login.parse_callback_query("error=access_denied&error_description=nope")
    assert r.error == "access_denied"
    assert r.code is None


def test_parse_callback_missing_params_is_error():
    r = oauth_login.parse_callback_query("foo=bar")
    assert r.error is not None
    assert r.code is None and r.state is None


def test_parse_callback_handles_leading_question_mark_and_path():
    r = oauth_login.parse_callback_query("/callback?code=x&state=y")
    assert (r.code, r.state, r.error) == ("x", "y", None)


def test_build_token_exchange_body_and_headers():
    url, body, headers = oauth_login.build_token_exchange(
        code="c1",
        verifier="v1",
        redirect_uri="http://localhost:7/callback",
        state="s1",
    )
    assert url == oauth_login.OAUTH_TOKEN_URL
    assert body == {
        "grant_type": "authorization_code",
        "code": "c1",
        "redirect_uri": "http://localhost:7/callback",
        "client_id": oauth_login.OAUTH_CLIENT_ID,
        "code_verifier": "v1",
        "state": "s1",
    }
    assert headers["Content-Type"] == "application/json"
    assert "User-Agent" in headers


def test_credentials_from_token_response_full():
    data = {
        "access_token": "sk-at",
        "refresh_token": "sk-rt",
        "expires_in": 3600,
        "scope": "user:profile user:inference",
        "account": {"uuid": "acc-uuid", "email_address": "me@example.com"},
        "organization": {"uuid": "org-uuid", "name": "Acme"},
    }
    creds, ident = oauth_login.credentials_from_token_response(data, now_ms=1_000_000)
    oauth = json.loads(creds)["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-at"
    assert oauth["refreshToken"] == "sk-rt"
    assert oauth["expiresAt"] == 1_000_000 + 3600 * 1000
    assert oauth["scopes"] == ["user:profile", "user:inference"]
    assert ident == oauth_login.Identity(
        email="me@example.com",
        org_name="Acme",
        org_uuid="org-uuid",
        account_uuid="acc-uuid",
    )


def test_credentials_from_token_response_missing_identity_fields():
    data = {"access_token": "sk-at", "expires_in": 60}
    creds, ident = oauth_login.credentials_from_token_response(data, now_ms=0)
    oauth = json.loads(creds)["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-at"
    assert oauth["refreshToken"] is None
    assert oauth["expiresAt"] == 60_000
    assert ident == oauth_login.Identity(
        email=None, org_name=None, org_uuid=None, account_uuid=None
    )


def test_callback_collector_returns_submitted_query():
    c = oauth_login.CallbackCollector()
    threading.Timer(0.01, lambda: c.submit("code=x&state=y")).start()
    assert c.wait(timeout=1.0) == "code=x&state=y"


def test_callback_collector_times_out_to_none():
    c = oauth_login.CallbackCollector()
    assert c.wait(timeout=0.05) is None


def _fake_server(query, port=54321):
    class _S:
        def __init__(self):
            self.port = port
            self.shut = False

        def wait(self, timeout):
            return query

        def shutdown(self):
            self.shut = True

    return _S()


def test_run_login_flow_happy_path_wires_state_and_returns_credentials():
    opened = {}
    captured = {}
    server = _fake_server("code=AUTHCODE&state=STATE123")

    def open_browser(url):
        opened["url"] = url

    def make_server():
        return server

    def exchange(code, verifier, redirect_uri, state):
        captured.update(
            code=code, verifier=verifier, redirect_uri=redirect_uri, state=state
        )
        return {
            "access_token": "sk-at",
            "refresh_token": "sk-rt",
            "expires_in": 10,
            "scope": "user:inference",
            "account": {"email_address": "a@b.com"},
        }

    result = oauth_login.run_login_flow(
        open_browser=open_browser,
        make_server=make_server,
        exchange=exchange,
        gen_pkce=lambda: ("VERIFIER", "CHALLENGE"),
        gen_state=lambda: "STATE123",
        now=lambda: 1000.0,
    )
    # browser opened to an authorize URL carrying our state + challenge + the loopback redirect
    assert "state=STATE123" in opened["url"]
    assert "code_challenge=CHALLENGE" in opened["url"]
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A54321%2Fcallback" in opened["url"]
    # exchange got the captured code + verifier + matching redirect/state
    assert captured["code"] == "AUTHCODE"
    assert captured["verifier"] == "VERIFIER"
    assert captured["redirect_uri"] == "http://localhost:54321/callback"
    assert captured["state"] == "STATE123"
    # tokens flowed into credentials; server torn down
    assert json.loads(result.credentials)["claudeAiOauth"]["accessToken"] == "sk-at"
    assert result.identity.email == "a@b.com"
    assert server.shut is True


def test_run_login_flow_state_mismatch_raises():
    with pytest.raises(oauth_login.LoginError):
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server("code=C&state=WRONG"),
            exchange=lambda **k: {},
            gen_pkce=lambda: ("V", "C"),
            gen_state=lambda: "RIGHT",
            now=lambda: 0.0,
        )


def test_run_login_flow_user_denied_raises():
    with pytest.raises(oauth_login.LoginError):
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server("error=access_denied"),
            exchange=lambda **k: {},
            gen_pkce=lambda: ("V", "C"),
            gen_state=lambda: "S",
            now=lambda: 0.0,
        )


def test_run_login_flow_timeout_raises():
    with pytest.raises(oauth_login.LoginError):
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server(None),  # wait() returns None -> timeout
            exchange=lambda **k: {},
            gen_pkce=lambda: ("V", "C"),
            gen_state=lambda: "S",
            now=lambda: 0.0,
        )


def _run_flow_with_exchange(query, exchange):
    return oauth_login.run_login_flow(
        open_browser=lambda url: None,
        make_server=lambda: _fake_server(query),
        exchange=exchange,
        gen_pkce=lambda: ("V", "C"),
        gen_state=lambda: "STATE123",
        now=lambda: 0.0,
    )


def test_run_login_flow_invalid_callback_query_raises():
    # Garbage callback (no code/state) -> LoginError before any exchange.
    with pytest.raises(oauth_login.LoginError):
        _run_flow_with_exchange("foo=bar", lambda **k: {"access_token": "x"})


@pytest.mark.parametrize("bad", [None, "not-a-dict", {}, {"error": "invalid_grant"}])
def test_run_login_flow_bad_exchange_response_raises(bad):
    # A response missing a usable access_token is rejected for every shape.
    with pytest.raises(oauth_login.LoginError):
        _run_flow_with_exchange("code=AUTHCODE&state=STATE123", lambda **k: bad)


def test_exchange_code_wraps_network_error_in_login_error(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(oauth_login.urllib.request, "urlopen", boom)
    with pytest.raises(oauth_login.LoginError):
        oauth_login.exchange_code(
            code="c", verifier="v", redirect_uri="http://localhost:1/callback", state="s",
        )


def test_exchange_code_wraps_http_error_in_login_error(monkeypatch):
    import io
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError(
            url="u", code=400, msg="Bad Request", hdrs=None,
            fp=io.BytesIO(b'{"error":"invalid_grant"}'),
        )

    monkeypatch.setattr(oauth_login.urllib.request, "urlopen", boom)
    with pytest.raises(oauth_login.LoginError):
        oauth_login.exchange_code(
            code="c", verifier="v", redirect_uri="http://localhost:1/callback", state="s",
        )
