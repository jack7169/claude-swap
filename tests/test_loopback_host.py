"""Phase 6.3: the loopback OAuth redirect_uri must use the literal IPv4 host.

The loopback server binds IPv4 ``("127.0.0.1", 0)`` but the redirect_uri was
built with ``localhost``. On an IPv6-preferring host ``localhost`` can resolve to
``::1``, so the browser's callback hits a port nothing is listening on and the
sign-in silently times out. The redirect_uri must therefore be the literal
``127.0.0.1`` to match the bind. These tests drive run_login_flow through its
dependency-injection seam (no real socket) and assert the redirect_uri is
``127.0.0.1`` everywhere it is used — the authorize URL given to the browser and
the token exchange — and never ``localhost``.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from claude_swap import oauth_login


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


def _drive_flow(*, port=54321):
    """Run a happy-path flow and return (opened_url, captured_exchange_kwargs)."""
    opened = {}
    captured = {}

    def open_browser(url):
        opened["url"] = url

    def exchange(code, verifier, redirect_uri, state):
        captured.update(
            code=code, verifier=verifier, redirect_uri=redirect_uri, state=state
        )
        return {"access_token": "sk-at", "expires_in": 10}

    oauth_login.run_login_flow(
        open_browser=open_browser,
        make_server=lambda: _fake_server("code=AUTHCODE&state=STATE123", port=port),
        exchange=exchange,
        gen_pkce=lambda: ("VERIFIER", "CHALLENGE"),
        gen_state=lambda: "STATE123",
        now=lambda: 0.0,
    )
    return opened["url"], captured


def _redirect_uri_from_authorize_url(url):
    return parse_qs(urlparse(url).query)["redirect_uri"][0]


def test_authorize_redirect_uri_uses_ipv4_literal_not_localhost():
    # The browser is sent an authorize URL whose redirect_uri host is the literal
    # 127.0.0.1 (matching the IPv4 bind), never "localhost" (which can resolve to
    # ::1 on an IPv6-preferring host and silently time the sign-in out).
    url, _ = _drive_flow(port=54321)
    redirect_uri = _redirect_uri_from_authorize_url(url)
    assert redirect_uri == "http://127.0.0.1:54321/callback"
    assert "127.0.0.1" in redirect_uri
    assert "localhost" not in redirect_uri


def test_authorize_url_encoded_redirect_uri_carries_ipv4_literal():
    # Guard the raw (percent-encoded) form in the authorize URL too, so a future
    # refactor that bypasses redirect_uri parsing can't silently regress.
    url, _ = _drive_flow(port=54321)
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A54321%2Fcallback" in url
    assert "localhost" not in url


def test_token_exchange_redirect_uri_uses_ipv4_literal_not_localhost():
    # The same redirect_uri must reach the token exchange: OAuth requires the
    # token-request redirect_uri to match the authorize-request one exactly.
    _, captured = _drive_flow(port=54321)
    assert captured["redirect_uri"] == "http://127.0.0.1:54321/callback"
    assert "localhost" not in captured["redirect_uri"]


def test_authorize_and_exchange_redirect_uri_are_consistent():
    # Consistency: whatever host/port the authorize URL advertises is exactly what
    # the token exchange replays. Mismatch would fail the real exchange.
    url, captured = _drive_flow(port=12345)
    redirect_uri = _redirect_uri_from_authorize_url(url)
    assert redirect_uri == captured["redirect_uri"]
    assert redirect_uri == "http://127.0.0.1:12345/callback"


def test_loopback_server_binds_ipv4_127_0_0_1():
    # Document the bind the redirect_uri must match: the constant the server uses
    # to construct its address is the IPv4 loopback literal, not a name.
    import inspect

    src = inspect.getsource(oauth_login.LoopbackServer.__init__)
    assert '"127.0.0.1"' in src
