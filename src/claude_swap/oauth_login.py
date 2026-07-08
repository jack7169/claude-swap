"""Interactive OAuth browser login (loopback auto-capture) to add an account.

Pure helpers (PKCE, authorize URL, callback parsing, token-exchange request,
response->credentials) are unit-tested; the impure orchestration (browser,
loopback server, network exchange) is dependency-injected so the test suite
never opens a browser, binds a port, or hits the network. No-op concerns: this
module is import-safe without rumps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.oauth import OAUTH_CLIENT_ID, OAUTH_TOKEN_URL

AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
LOGIN_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers"
CALLBACK_PATH = "/callback"
LOGIN_TIMEOUT = 180.0


def _b64url(data: bytes) -> str:
    """URL-safe base64 without padding (PKCE/state encoding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce(rand: Callable[[int], bytes] = os.urandom) -> tuple[str, str]:
    """Return an (code_verifier, code_challenge) pair (RFC 7636, S256).

    ``rand`` is injectable so tests are deterministic.
    """
    verifier = _b64url(rand(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    client_id: str = OAUTH_CLIENT_ID,
    scopes: str = LOGIN_SCOPES,
) -> str:
    """Build the Claude OAuth authorize URL (authorization-code + PKCE S256)."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


@dataclass
class CallbackResult:
    """Parsed OAuth redirect query: a code+state on success, else an error."""

    code: str | None
    state: str | None
    error: str | None


def parse_callback_query(query: str) -> CallbackResult:
    """Parse the loopback redirect query string into a CallbackResult.

    Accepts a raw query string or a full ``/callback?...`` path. Recognises an
    explicit ``error`` param (e.g. ``access_denied``); anything missing a
    ``code``/``state`` pair becomes a generic ``invalid_request`` error.
    """
    if "?" in query:
        query = query.split("?", 1)[1]
    params = parse_qs(query)
    error = params.get("error", [None])[0]
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    if error:
        return CallbackResult(code=None, state=state, error=error)
    if not code or not state:
        return CallbackResult(code=None, state=None, error="invalid_request")
    return CallbackResult(code=code, state=state, error=None)


def build_token_exchange(
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    state: str,
    client_id: str = OAUTH_CLIENT_ID,
) -> tuple[str, dict, dict]:
    """Build the (url, body, headers) for the authorization_code token exchange.

    Defaults to a JSON body (the same Content-Type oauth.refresh_oauth_credentials
    already uses against this endpoint). If the endpoint rejects it with
    invalid_grant during live testing, switch the encoding to
    application/x-www-form-urlencoded here — this is the single place to change.

    ``state`` is intentionally included in the body: RFC 6749 §4.1.3 does not list
    it for the token request, but Claude's authorization_code grant has historically
    expected it, and an ignored extra param is harmless whereas omitting a required
    one is fatal. ``state`` is still validated client-side (in run_login_flow)
    before we get here.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "state": state,
    }
    headers = {"Content-Type": "application/json", "User-Agent": "claude-swap/1.0"}
    return OAUTH_TOKEN_URL, body, headers


@dataclass
class Identity:
    """Account identity extracted from the OAuth token response."""

    email: str | None
    org_name: str | None
    org_uuid: str | None
    account_uuid: str | None


def credentials_from_token_response(data: dict, *, now_ms: int) -> tuple[str, "Identity"]:
    """Build Claude Code's credential JSON and the account Identity from a token
    response. Field names for account/organization are best-effort and tolerate
    absence (identity falls back to None; the access/refresh tokens are the part
    that must be present)."""
    account = data.get("account") or {}
    organization = data.get("organization") or {}
    expires_in = data.get("expires_in")
    # A malformed (non-numeric) expires_in must not fail an otherwise-valid login:
    # degrade to expires_at=None (token still usable; expiry unknown).
    try:
        expires_at = now_ms + int(expires_in) * 1000 if expires_in is not None else None
    except (TypeError, ValueError):
        expires_at = None
    oauth = {
        "accessToken": data.get("access_token"),
        "refreshToken": data.get("refresh_token"),
        "expiresAt": expires_at,
        "scopes": (data.get("scope") or "").split(),
    }
    credentials = json.dumps({"claudeAiOauth": oauth})
    identity = Identity(
        email=account.get("email_address"),
        org_name=organization.get("name"),
        org_uuid=organization.get("uuid"),
        account_uuid=account.get("uuid"),
    )
    return credentials, identity


class CallbackCollector:
    """Thread-safe slot for the loopback callback's raw query string."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._query: str | None = None

    def submit(self, query: str) -> None:
        if not self._event.is_set():
            self._query = query
            self._event.set()

    def wait(self, timeout: float) -> str | None:
        return self._query if self._event.wait(timeout) else None


class LoginError(ClaudeSwitchError):
    """A browser OAuth login failed (denied, timed out, or exchange failed)."""


@dataclass
class LoginResult:
    credentials: str
    identity: "Identity"


def _gen_state() -> str:
    return _b64url(os.urandom(32))


def run_login_flow(
    *,
    open_browser: Callable[[str], object],
    make_server: Callable[[], object],
    exchange: Callable[..., dict],
    gen_pkce: Callable[[], tuple[str, str]] = generate_pkce,
    gen_state: Callable[[], str] = _gen_state,
    now: Callable[[], float] = time.time,
    timeout: float = LOGIN_TIMEOUT,
) -> "LoginResult":
    """Drive the loopback OAuth login. Side-effecting collaborators are injected.

    Raises LoginError on denial, state mismatch, timeout, or a failed exchange.
    """
    verifier, challenge = gen_pkce()
    state = gen_state()
    server = make_server()
    try:
        # Host MUST be "localhost" (not "127.0.0.1"): Anthropic's OAuth client
        # allowlists loopback redirects by that literal host (any port), and
        # rejects "127.0.0.1" with "Redirect URI ... is not supported by
        # client". LoopbackServer binds BOTH 127.0.0.1 and ::1, so "localhost"
        # is reachable however it resolves (the earlier IPv6-timeout worry).
        redirect_uri = f"http://localhost:{server.port}{CALLBACK_PATH}"
        url = build_authorize_url(
            redirect_uri=redirect_uri, state=state, code_challenge=challenge
        )
        open_browser(url)
        query = server.wait(timeout)
        if query is None:
            raise LoginError("Timed out waiting for the browser sign-in. Try again.")
        result = parse_callback_query(query)
        if result.error == "access_denied":
            raise LoginError("Sign-in was cancelled in the browser.")
        if result.error:
            raise LoginError(f"The browser sign-in failed ({result.error}).")
        if not result.code:
            raise LoginError("The browser sign-in returned an invalid response.")
        if result.state != state:
            raise LoginError("Sign-in state mismatch — possible cross-request error.")
        data = exchange(
            code=result.code, verifier=verifier, redirect_uri=redirect_uri, state=state
        )
        if not isinstance(data, dict) or not data.get("access_token"):
            raise LoginError("The token exchange did not return an access token.")
        credentials, identity = credentials_from_token_response(
            data, now_ms=int(now() * 1000)
        )
        return LoginResult(credentials=credentials, identity=identity)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass


def exchange_code(code: str, verifier: str, redirect_uri: str, state: str) -> dict:
    """Real network exchange of an authorization code for tokens.

    Network/HTTP failures are wrapped in LoginError so the orchestrator surfaces a
    friendly message instead of leaking a raw urllib traceback.
    """
    url, body, headers = build_token_exchange(
        code=code, verifier=verifier, redirect_uri=redirect_uri, state=state
    )
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        raise LoginError(
            f"Token exchange failed (HTTP {e.code}). {detail}".strip()
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise LoginError(
            "Could not reach the Claude token endpoint — check your connection "
            "and try again."
        ) from e


def _is_callback_path(path: str) -> bool:
    """True only for the OAuth callback endpoint (``/callback``).

    The collector is one-shot: the FIRST GET to reach the ephemeral loopback port
    latches it. Without this guard a browser favicon/preconnect or any stray probe
    that races ahead of the real redirect would win the slot and abort login. Query
    string is ignored — only the path component must match.
    """
    return urlsplit(path).path == CALLBACK_PATH


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        # Ignore anything that isn't the real callback so a stray request can't
        # latch the one-shot collector and drop the genuine redirect.
        if not _is_callback_path(self.path):
            self.send_response(404)
            self.end_headers()
            return
        self.server.collector.submit(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>claude-swap</h2>"
            b"<p>Sign-in complete. You can close this tab.</p></body></html>"
        )

    def log_message(self, *args):  # silence stderr access logs
        pass


class _V6HTTPServer(HTTPServer):
    address_family = socket.AF_INET6


class LoopbackServer:
    """Loopback HTTP server (ephemeral port) capturing one OAuth callback.

    Binds the SAME port on BOTH 127.0.0.1 and ::1 so the ``http://localhost:…``
    redirect is reachable however the browser resolves ``localhost`` (IPv4 or
    IPv6). The redirect host must be ``localhost`` for Anthropic's OAuth client
    to accept it; dual-binding removes the IPv6-vs-IPv4 reachability gap that
    would otherwise tempt a switch to a literal ``127.0.0.1`` (which the client
    rejects). Thin glue around http.server; not unit-tested (the suite never
    binds a port).
    """

    def __init__(self) -> None:
        self.collector = CallbackCollector()
        self._servers: list[HTTPServer] = []
        # IPv4 first, on an ephemeral port we then reuse for IPv6.
        v4 = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        v4.collector = self.collector  # type: ignore[attr-defined]
        self._port = v4.server_address[1]
        self._servers.append(v4)
        # Best-effort IPv6 loopback on the same port; skip if unavailable.
        try:
            v6 = _V6HTTPServer(("::1", self._port), _CallbackHandler)
            v6.collector = self.collector  # type: ignore[attr-defined]
            self._servers.append(v6)
        except OSError:
            pass
        for srv in self._servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self._port

    def wait(self, timeout: float) -> str | None:
        return self.collector.wait(timeout)

    def shutdown(self) -> None:
        for srv in self._servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
