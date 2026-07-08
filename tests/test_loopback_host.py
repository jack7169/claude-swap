"""The loopback OAuth redirect_uri must use the literal host ``localhost``.

Anthropic's OAuth client allowlists loopback redirects by the literal host
``localhost`` (with any port) and rejects ``127.0.0.1`` ("Redirect URI ... is
not supported by client"). So the redirect_uri must be ``localhost`` everywhere
it is used — the authorize URL given to the browser and the token exchange —
and must match between the two (OAuth requires them identical).

The earlier IPv6-vs-IPv4 reachability worry (``localhost`` can resolve to ``::1``
where an IPv4-only bind isn't listening) is handled by :class:`LoopbackServer`
binding BOTH ``127.0.0.1`` and ``::1`` on the same port, not by changing the
redirect host. These tests drive run_login_flow through its dependency-injection
seam (no real socket).
"""

from __future__ import annotations

import inspect
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


def test_authorize_redirect_uri_uses_localhost_not_ipv4_literal():
    # The authorize URL's redirect_uri host is "localhost" (what the OAuth
    # client allows), never the literal 127.0.0.1 (which it rejects).
    url, _ = _drive_flow(port=54321)
    redirect_uri = _redirect_uri_from_authorize_url(url)
    assert redirect_uri == "http://localhost:54321/callback"
    assert "127.0.0.1" not in redirect_uri


def test_authorize_url_encoded_redirect_uri_carries_localhost():
    # Guard the raw (percent-encoded) form too, so a future refactor that
    # bypasses redirect_uri parsing can't silently regress to 127.0.0.1.
    url, _ = _drive_flow(port=54321)
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A54321%2Fcallback" in url
    assert "127.0.0.1" not in url


def test_token_exchange_redirect_uri_uses_localhost():
    # The same redirect_uri must reach the token exchange: OAuth requires the
    # token-request redirect_uri to match the authorize-request one exactly.
    _, captured = _drive_flow(port=54321)
    assert captured["redirect_uri"] == "http://localhost:54321/callback"
    assert "127.0.0.1" not in captured["redirect_uri"]


def test_authorize_and_exchange_redirect_uri_are_consistent():
    # Consistency: whatever host/port the authorize URL advertises is exactly
    # what the token exchange replays. Mismatch would fail the real exchange.
    url, captured = _drive_flow(port=12345)
    redirect_uri = _redirect_uri_from_authorize_url(url)
    assert redirect_uri == captured["redirect_uri"]
    assert redirect_uri == "http://localhost:12345/callback"


def test_loopback_server_dual_binds_ipv4_and_ipv6():
    # localhost may resolve to 127.0.0.1 or ::1; the server binds BOTH so the
    # "localhost" redirect is reachable either way (the reason we can keep the
    # required "localhost" host without an IPv6 timeout).
    src = inspect.getsource(oauth_login.LoopbackServer.__init__)
    assert '"127.0.0.1"' in src
    assert '"::1"' in src
