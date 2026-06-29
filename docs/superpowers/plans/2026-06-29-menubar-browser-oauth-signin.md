# Menu-bar Browser OAuth Sign-in — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Sign in with browser…" menu-bar item that runs Claude Code's real OAuth browser login (loopback auto-capture) to add a brand-new account, add-only.

**Architecture:** A new `oauth_login.py` holds pure helpers (PKCE, authorize-URL, callback parsing, token-exchange request building, token-response→credentials) plus a thin impure orchestrator (`run_login_flow`) whose side-effecting collaborators (browser opener, loopback server, code exchanger) are dependency-injected so tests never open a browser, bind a port, or hit the network. The switcher gains a focused `add_account_from_oauth` that stores a full-OAuth account (real email/org, refresh token) using existing low-level primitives. `menubar.py` adds the menu item and a background-thread callback that wires the real collaborators.

**Tech Stack:** Python 3.12, stdlib only (`hashlib`, `base64`, `secrets`/`os.urandom`, `urllib.parse`, `urllib.request`, `http.server`, `threading`, `webbrowser`), `rumps` (menu glue only), `pytest`.

## Global Constraints

- Reuse existing constants from `oauth.py`: `OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"`, `OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"`.
- New constants in `oauth_login.py`: `AUTHORIZE_URL = "https://claude.ai/oauth/authorize"`, `LOGIN_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers"`, `CALLBACK_PATH = "/callback"`, `LOGIN_TIMEOUT = 180.0`.
- PKCE: `code_challenge_method=S256`. `state`: base64url of 32 random bytes.
- Redirect URI: `http://localhost:<port>/callback` (loopback).
- Add-only: never touch the live login / active account.
- TDD throughout. Run tests with `uv run --extra menubar pytest -q`. The suite must also pass WITHOUT rumps (CI runs `pip install -e .` then `pytest`); verify with the isolated venv at the end. No real network, browser, or bound port anywhere in the suite.
- Commit after each task.

---

## File Structure

- Create `src/claude_swap/oauth_login.py` — PKCE, authorize URL, callback parsing, token-exchange request, response→credentials, `CallbackCollector`, `LoopbackServer`, `run_login_flow`, `exchange_code`.
- Create `tests/test_oauth_login.py` — unit tests for all pure helpers, `CallbackCollector`, and `run_login_flow` (with fakes).
- Modify `src/claude_swap/switcher.py` — add `add_account_from_oauth`.
- Modify `tests/test_switcher.py` — tests for `add_account_from_oauth`.
- Modify `src/claude_swap/menubar.py` — `_add_menu` item + `on_add_browser_login` (rumps glue, untested).

---

## Task 1: oauth_login scaffolding + PKCE

**Files:**
- Create: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Produces: `_b64url(data: bytes) -> str`; `generate_pkce(rand: Callable[[int], bytes] = os.urandom) -> tuple[str, str]` returning `(verifier, challenge)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_login.py
from __future__ import annotations

import base64
import hashlib

from claude_swap import oauth_login


def test_b64url_strips_padding_and_is_urlsafe():
    # 0xFB,0xFF -> standard b64 has '+'/'/'; urlsafe uses '-'/'_'; no '='
    assert oauth_login._b64url(b"\xfb\xff\xfe") == "-__-"  # '+/+' -> '-_-'? verify below


def test_generate_pkce_verifier_charset_and_challenge_is_s256_of_verifier():
    verifier, challenge = oauth_login.generate_pkce(rand=lambda n: b"\x00" * n)
    # 32 zero bytes -> base64url no padding -> 43 chars, all 'A' then ''
    assert verifier == base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=").decode()
    )
    assert challenge == expected_challenge
    assert "=" not in verifier and "=" not in challenge
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k pkce_or_b64url`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_swap.oauth_login'`.

(Fix the `_b64url` literal in step 1 if the exact expected string is uncertain: compute it as `base64.urlsafe_b64encode(b"\xfb\xff\xfe").rstrip(b"=").decode()` and assert equality against that instead of a hardcoded string.)

- [ ] **Step 3: Write minimal implementation**

```python
# src/claude_swap/oauth_login.py
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
import os
from collections.abc import Callable

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): PKCE generator + b64url helper for OAuth login"
```

---

## Task 2: build_authorize_url

**Files:**
- Modify: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Produces: `build_authorize_url(*, redirect_uri: str, state: str, code_challenge: str, client_id: str = OAUTH_CLIENT_ID, scopes: str = LOGIN_SCOPES) -> str`.

- [ ] **Step 1: Write the failing test**

```python
from urllib.parse import urlparse, parse_qs

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k authorize_url`
Expected: FAIL — `AttributeError: module ... has no attribute 'build_authorize_url'`.

- [ ] **Step 3: Write minimal implementation**

Add to `oauth_login.py` (and `from urllib.parse import urlencode` at top):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k authorize_url`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): build_authorize_url"
```

---

## Task 3: parse_callback_query + CallbackResult

**Files:**
- Modify: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Produces: `@dataclass CallbackResult` with fields `code: str | None`, `state: str | None`, `error: str | None`; `parse_callback_query(query: str) -> CallbackResult`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k callback`
Expected: FAIL — `AttributeError: ... 'parse_callback_query'`.

- [ ] **Step 3: Write minimal implementation**

Add to `oauth_login.py` (and `from dataclasses import dataclass`, `from urllib.parse import parse_qs` at top):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k callback`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): parse_callback_query + CallbackResult"
```

---

## Task 4: build_token_exchange

**Files:**
- Modify: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Produces: `build_token_exchange(*, code: str, verifier: str, redirect_uri: str, state: str, client_id: str = OAUTH_CLIENT_ID) -> tuple[str, dict, dict]` returning `(url, body, headers)`. `url == OAUTH_TOKEN_URL`; `body` is the `authorization_code` grant dict; `headers` carries `Content-Type: application/json` and a `User-Agent`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_token_exchange_body_and_headers():
    url, body, headers = oauth_login.build_token_exchange(
        code="c1", verifier="v1", redirect_uri="http://localhost:7/callback", state="s1",
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k token_exchange`
Expected: FAIL — missing attribute.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k token_exchange`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): build_token_exchange request"
```

---

## Task 5: credentials_from_token_response + Identity

**Files:**
- Modify: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Produces: `@dataclass Identity(email: str | None, org_name: str | None, org_uuid: str | None, account_uuid: str | None)`; `credentials_from_token_response(data: dict, *, now_ms: int) -> tuple[str, Identity]`. The credentials string is `json.dumps({"claudeAiOauth": {...}})` with `accessToken`, `refreshToken`, `expiresAt`, `scopes`.

- [ ] **Step 1: Write the failing test**

```python
import json

def test_credentials_from_token_response_full():
    data = {
        "access_token": "sk-at", "refresh_token": "sk-rt", "expires_in": 3600,
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
        email="me@example.com", org_name="Acme", org_uuid="org-uuid", account_uuid="acc-uuid",
    )

def test_credentials_from_token_response_missing_identity_fields():
    data = {"access_token": "sk-at", "expires_in": 60}
    creds, ident = oauth_login.credentials_from_token_response(data, now_ms=0)
    oauth = json.loads(creds)["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-at"
    assert oauth["refreshToken"] is None
    assert oauth["expiresAt"] == 60_000
    assert ident == oauth_login.Identity(email=None, org_name=None, org_uuid=None, account_uuid=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k token_response`
Expected: FAIL — missing attribute.

- [ ] **Step 3: Write minimal implementation**

Add to `oauth_login.py` (and `import json` at top):

```python
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
    expires_at = now_ms + int(expires_in) * 1000 if expires_in is not None else None
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k token_response`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): credentials_from_token_response + Identity"
```

---

## Task 6: CallbackCollector

**Files:**
- Modify: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Produces: `CallbackCollector` with `submit(query: str) -> None`, `wait(timeout: float) -> str | None` (returns the first submitted query, or `None` on timeout). Thread-safe via `threading.Event`; no socket.

- [ ] **Step 1: Write the failing test**

```python
import threading

def test_callback_collector_returns_submitted_query():
    c = oauth_login.CallbackCollector()
    threading.Timer(0.01, lambda: c.submit("code=x&state=y")).start()
    assert c.wait(timeout=1.0) == "code=x&state=y"

def test_callback_collector_times_out_to_none():
    c = oauth_login.CallbackCollector()
    assert c.wait(timeout=0.05) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k collector`
Expected: FAIL — missing attribute.

- [ ] **Step 3: Write minimal implementation**

Add to `oauth_login.py` (and `import threading` at top):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k collector`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): CallbackCollector"
```

---

## Task 7: run_login_flow orchestrator + LoopbackServer glue

**Files:**
- Modify: `src/claude_swap/oauth_login.py`
- Test: `tests/test_oauth_login.py`

**Interfaces:**
- Consumes: `generate_pkce`, `build_authorize_url`, `parse_callback_query`, `credentials_from_token_response`, `CallbackResult`, `Identity`.
- Produces:
  - `@dataclass LoginResult(credentials: str, identity: Identity)`.
  - Exceptions: `class LoginError(ClaudeSwitchError)`.
  - `run_login_flow(*, open_browser, make_server, exchange, gen_pkce=generate_pkce, gen_state=..., now=..., timeout=LOGIN_TIMEOUT) -> LoginResult`. Server protocol: `make_server()` returns an object with `.port: int`, `.wait(timeout: float) -> str | None`, `.shutdown() -> None`. `exchange(code, verifier, redirect_uri, state) -> dict`.
  - `exchange_code(code, verifier, redirect_uri, state) -> dict` (default real network exchanger).
  - `LoopbackServer` (real http.server glue; **not** unit-tested).

- [ ] **Step 1: Write the failing test**

```python
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
        captured.update(code=code, verifier=verifier, redirect_uri=redirect_uri, state=state)
        return {"access_token": "sk-at", "refresh_token": "sk-rt", "expires_in": 10,
                "scope": "user:inference", "account": {"email_address": "a@b.com"}}

    result = oauth_login.run_login_flow(
        open_browser=open_browser, make_server=make_server, exchange=exchange,
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
    import json as _json
    assert _json.loads(result.credentials)["claudeAiOauth"]["accessToken"] == "sk-at"
    assert result.identity.email == "a@b.com"
    assert server.shut is True

def test_run_login_flow_state_mismatch_raises():
    import pytest
    with pytest.raises(oauth_login.LoginError):
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server("code=C&state=WRONG"),
            exchange=lambda **k: {},
            gen_pkce=lambda: ("V", "C"), gen_state=lambda: "RIGHT", now=lambda: 0.0,
        )

def test_run_login_flow_user_denied_raises():
    import pytest
    with pytest.raises(oauth_login.LoginError):
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server("error=access_denied"),
            exchange=lambda **k: {},
            gen_pkce=lambda: ("V", "C"), gen_state=lambda: "S", now=lambda: 0.0,
        )

def test_run_login_flow_timeout_raises():
    import pytest
    with pytest.raises(oauth_login.LoginError):
        oauth_login.run_login_flow(
            open_browser=lambda url: None,
            make_server=lambda: _fake_server(None),  # wait() returns None -> timeout
            exchange=lambda **k: {},
            gen_pkce=lambda: ("V", "C"), gen_state=lambda: "S", now=lambda: 0.0,
        )
```

Note: `exchange` is called positionally in the happy-path test (`exchange(code, verifier, redirect_uri, state)`) but with `**k` in the error tests; implement the call as keyword (`exchange(code=..., verifier=..., redirect_uri=..., state=...)`) so both forms work.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py -k run_login_flow`
Expected: FAIL — missing `run_login_flow`/`LoginError`.

- [ ] **Step 3: Write minimal implementation**

Add to `oauth_login.py` (top imports: `import time`, `import urllib.request`, `import urllib.error`, `from http.server import BaseHTTPRequestHandler, HTTPServer`, `from claude_swap.exceptions import ClaudeSwitchError`):

```python
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
        redirect_uri = f"http://localhost:{server.port}{CALLBACK_PATH}"
        url = build_authorize_url(
            redirect_uri=redirect_uri, state=state, code_challenge=challenge
        )
        open_browser(url)
        query = server.wait(timeout)
        if query is None:
            raise LoginError(
                "Timed out waiting for the browser sign-in. Try again."
            )
        result = parse_callback_query(query)
        if result.error == "access_denied":
            raise LoginError("Sign-in was cancelled in the browser.")
        if result.error or not result.code:
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
    """Real network exchange of an authorization code for tokens."""
    url, body, headers = build_token_exchange(
        code=code, verifier=verifier, redirect_uri=redirect_uri, state=state
    )
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
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


class LoopbackServer:
    """Loopback HTTP server (127.0.0.1, ephemeral port) capturing one callback.

    Thin glue around http.server; not unit-tested (the suite never binds a port).
    """

    def __init__(self) -> None:
        self.collector = CallbackCollector()
        self._httpd = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._httpd.collector = self.collector  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def wait(self, timeout: float) -> str | None:
        return self.collector.wait(timeout)

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_oauth_login.py`
Expected: PASS (all oauth_login tests).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth_login.py tests/test_oauth_login.py
git commit -m "feat(login): run_login_flow orchestrator + LoopbackServer + exchange_code"
```

---

## Task 8: switcher.add_account_from_oauth

**Files:**
- Modify: `src/claude_swap/switcher.py`
- Test: `tests/test_switcher.py`

**Interfaces:**
- Consumes existing switcher primitives: `_setup_directories`, `_init_sequence_file`, `_migrate_org_fields`, `_validate_email`, `_reject_cross_kind_collision`, `_account_exists(email, org_uuid)`, `_find_account_slot(data, email, org_uuid)`, `_get_next_account_number`, `_write_account_credentials`, `_write_account_config`, `_get_sequence_data`, `_write_json`, `get_timestamp`.
- Produces: `add_account_from_oauth(self, *, credentials: str, email: str | None, org_name: str | None = None, org_uuid: str | None = None, account_uuid: str | None = None, slot: int | None = None) -> str` returning the account number (slot) as a string. Add-only/auto-slot; updates in place when the (email, org) account already exists.

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_switcher.py` (reuse `TestPerformSwitchPostDisplay._install_store_patches`-style in-memory stores via direct patching, but here the switcher's own `_write_account_credentials/_write_account_config` write to the real temp-home stores — simplest is to capture them):

```python
class TestAddAccountFromOAuth:
    def _switcher(self, temp_home):
        from claude_swap.switcher import ClaudeAccountSwitcher
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sw._init_sequence_file()
        return sw

    def test_adds_new_account_with_real_org_and_credentials(self, temp_home: Path):
        sw = self._switcher(temp_home)
        creds_store: dict = {}
        cfg_store: dict = {}
        with patch.object(sw, "_write_account_credentials",
                          side_effect=lambda n, e, c: creds_store.__setitem__((str(n), e), c)), \
             patch.object(sw, "_write_account_config",
                          side_effect=lambda n, e, c: cfg_store.__setitem__((str(n), e), c)):
            num = sw.add_account_from_oauth(
                credentials='{"claudeAiOauth": {"accessToken": "sk-at"}}',
                email="me@example.com", org_name="Acme", org_uuid="org-1",
                account_uuid="acc-1",
            )
        assert num == "1"
        assert creds_store[("1", "me@example.com")] == '{"claudeAiOauth": {"accessToken": "sk-at"}}'
        cfg = json.loads(cfg_store[("1", "me@example.com")])["oauthAccount"]
        assert cfg["emailAddress"] == "me@example.com"
        assert cfg["organizationUuid"] == "org-1"
        assert cfg["organizationName"] == "Acme"
        assert cfg["accountUuid"] == "acc-1"
        data = sw._get_sequence_data()
        rec = data["accounts"]["1"]
        assert rec["email"] == "me@example.com"
        assert rec["organizationUuid"] == "org-1"
        assert rec["organizationName"] == "Acme"
        assert 1 in data["sequence"]

    def test_synthesizes_email_when_missing(self, temp_home: Path):
        sw = self._switcher(temp_home)
        with patch.object(sw, "_write_account_credentials"), \
             patch.object(sw, "_write_account_config"):
            num = sw.add_account_from_oauth(
                credentials='{"claudeAiOauth": {"accessToken": "x"}}', email=None,
            )
        data = sw._get_sequence_data()
        assert data["accounts"][num]["email"].endswith("@token.local")

    def test_updates_existing_account_in_place(self, temp_home: Path):
        sw = self._switcher(temp_home)
        with patch.object(sw, "_write_account_credentials"), \
             patch.object(sw, "_write_account_config"):
            first = sw.add_account_from_oauth(
                credentials='{"claudeAiOauth": {"accessToken": "v1"}}',
                email="me@example.com", org_uuid="org-1",
            )
            again = sw.add_account_from_oauth(
                credentials='{"claudeAiOauth": {"accessToken": "v2"}}',
                email="me@example.com", org_uuid="org-1",
            )
        assert first == again  # same slot, updated in place
        assert len(sw._get_sequence_data()["sequence"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra menubar pytest -q tests/test_switcher.py -k AddAccountFromOAuth`
Expected: FAIL — `AttributeError: ... 'add_account_from_oauth'`.

- [ ] **Step 3: Write minimal implementation**

Add to `ClaudeAccountSwitcher` (place after `add_account_from_token`). Use the existing helpers; `org_uuid`/`org_name`/`account_uuid` default to `""`/`None` consistently with the codebase (records use `""` for absent org):

```python
def add_account_from_oauth(
    self,
    *,
    credentials: str,
    email: str | None,
    org_name: str | None = None,
    org_uuid: str | None = None,
    account_uuid: str | None = None,
    slot: int | None = None,
) -> str:
    """Store a full-OAuth account from a completed browser login (add-only).

    Unlike add_account_from_token (which stores a scope-limited setup-token with
    no org), this persists the real refresh token and organization identity. When
    an account with the same (email, org) already exists, its credentials/config
    are refreshed in place. Returns the account number.
    """
    self._setup_directories()
    self._init_sequence_file()
    self._migrate_org_fields()

    org_uuid = org_uuid or ""
    org_name = org_name or ""
    account_uuid = account_uuid or ""

    if email and not self._validate_email(email):
        raise ValidationError(f"Invalid email format: {email}")
    if not email:
        if slot is None:
            slot = self._get_next_account_number()
        email = f"signed-in-{slot}@token.local"

    self._reject_cross_kind_collision(email, is_api_key=False)

    config = json.dumps({
        "oauthAccount": {
            "emailAddress": email,
            "accountUuid": account_uuid,
            "organizationUuid": org_uuid or None,
            "organizationName": org_name or None,
        }
    })

    # Update in place when this (email, org) account already exists.
    if slot is None and self._account_exists(email, org_uuid):
        seq = self._get_sequence_data()
        account_num = self._find_account_slot(seq, email, org_uuid)
        if account_num is None:
            raise ConfigError(f"Existing account metadata for {email} is inconsistent")
        self._write_account_credentials(account_num, email, credentials)
        self._write_account_config(account_num, email, config)
        seq["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, seq)
        self._logger.info(f"Updated signed-in credentials for account {account_num}: {email}")
        return account_num

    account_num = str(slot) if slot is not None else str(self._get_next_account_number())
    self._write_account_credentials(account_num, email, credentials)
    self._write_account_config(account_num, email, config)

    data = self._get_sequence_data()
    data["accounts"][account_num] = {
        "email": email,
        "uuid": account_uuid,
        "organizationUuid": org_uuid,
        "organizationName": org_name,
        "added": get_timestamp(),
    }
    if int(account_num) not in data["sequence"]:
        data["sequence"].append(int(account_num))
        data["sequence"].sort()
    data["lastUpdated"] = get_timestamp()
    self._write_json(self.sequence_file, data)
    self._logger.info(f"Added account {account_num} from browser sign-in: {email}")
    return account_num
```

Confirm `ValidationError` and `ConfigError` are already imported in `switcher.py` (they are used elsewhere in the file).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra menubar pytest -q tests/test_switcher.py -k AddAccountFromOAuth`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/switcher.py tests/test_switcher.py
git commit -m "feat(login): switcher.add_account_from_oauth (full-OAuth, real org)"
```

---

## Task 9: menubar "Sign in with browser…" item + callback (rumps glue)

**Files:**
- Modify: `src/claude_swap/menubar.py`

**Interfaces:**
- Consumes: `oauth_login.run_login_flow`, `oauth_login.LoopbackServer`, `oauth_login.exchange_code`, `switcher.add_account_from_oauth`.
- Produces: a new "Sign in with browser…" entry in `_add_menu` and an `on_add_browser_login` callback that runs the flow on a background thread.

This task is rumps GUI glue and is not unit-tested (the suite never imports rumps), consistent with the existing add-account callbacks. Verify by import + a manual smoke run.

- [ ] **Step 1: Add the menu item** in `_add_menu` (after the setup-token item):

```python
def _add_menu(self, rumps):
    menu = rumps.MenuItem("Add account")
    menu.add(rumps.MenuItem("From current login", callback=self.on_add_login))
    if hasattr(self.switcher, "add_account_from_token"):
        menu.add(rumps.MenuItem("From setup-token…", callback=self.on_add_token))
    if hasattr(self.switcher, "add_account_from_oauth"):
        menu.add(rumps.MenuItem("Sign in with browser…", callback=self.on_add_browser_login))
    return menu
```

- [ ] **Step 2: Add the callback** (place near `on_add_token`). It runs the login on a daemon thread so the rumps run-loop is never blocked while the user authorizes, then marshals the add + refresh:

```python
def on_add_browser_login(self, _sender):
    # Bring the accessory app forward so any future dialogs render, then run the
    # OAuth login off the main thread (it blocks until the browser callback).
    import webbrowser

    import AppKit
    from claude_swap import oauth_login

    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def worker():
        try:
            result = oauth_login.run_login_flow(
                open_browser=webbrowser.open,
                make_server=oauth_login.LoopbackServer,
                exchange=oauth_login.exchange_code,
            )
            num = self.switcher.add_account_from_oauth(
                credentials=result.credentials,
                email=result.identity.email,
                org_name=result.identity.org_name,
                org_uuid=result.identity.org_uuid,
                account_uuid=result.identity.account_uuid,
            )
            self.switcher._logger.info("browser sign-in added account %s", num)
            self.refresh_async(full=True)
        except ClaudeSwitchError as e:
            self.switcher._logger.warning("browser sign-in failed: %s", e)
            rumps.notification("claude-swap", "Sign-in failed", str(e))
        except Exception:
            self.switcher._logger.debug("browser sign-in error", exc_info=True)
            rumps.notification("claude-swap", "Sign-in failed",
                               "An unexpected error occurred during sign-in.")

    threading.Thread(target=worker, daemon=True).start()
```

(`threading` and `ClaudeSwitchError` are already imported at the top of `menubar.py`.)

- [ ] **Step 3: Verify import + menubar tests still pass**

Run: `uv run --extra menubar python -c "import claude_swap.menubar, claude_swap.oauth_login; print('ok')"`
Expected: `ok`.
Run: `uv run --extra menubar pytest -q tests/test_menubar.py`
Expected: PASS (unchanged count — this is untested glue).

- [ ] **Step 4: Real-rumps menu smoke check** (out-of-band, not in the suite): build a `rumps.rumps.Menu` and confirm the new item is present and the app imports. Reuse the pattern from the earlier scratchpad smoke test if desired. Expected: the "Sign in with browser…" item exists with a callback.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py
git commit -m "feat(login): menu-bar 'Sign in with browser…' item + background login worker"
```

---

## Task 10: Full-suite verification (with and without rumps)

**Files:** none (verification only).

- [ ] **Step 1: Full suite with the menubar extra**

Run: `uv run --extra menubar pytest -q`
Expected: all pass (prior 720 + new oauth_login/switcher tests), 3 skipped.

- [ ] **Step 2: CI-faithful run WITHOUT rumps**

Run (isolated venv, like CI's `pip install -e .` + `pytest`):
```bash
"$CIVENV/bin/pip" install -q -e .
"$CIVENV/bin/python" -m pytest -q
```
Expected: same pass count; `claude_swap.oauth_login` imports without rumps (it must not import rumps).

- [ ] **Step 3: Confirm no real network/port/browser in the suite**

Run: `grep -rnE "urlopen|webbrowser|HTTPServer|socket|LoopbackServer\(\)" tests/`
Expected: no matches that execute real I/O (the orchestrator tests use injected fakes; `LoopbackServer` and `exchange_code` are never instantiated/called by tests).

- [ ] **Step 4: Commit (if any verification fixups were needed)**

```bash
git add -A
git commit -m "test(login): full-suite green with and without rumps"
```

---

## Self-Review

- **Spec coverage:** loopback auto-capture (Task 7), add-only via add_account_from_oauth (Task 8), real email/org identity (Tasks 5, 8), PKCE S256 + state (Tasks 1, 7), authorize/token endpoints + scopes (Tasks 2, 4), error handling for denied/timeout/state-mismatch/bad-exchange (Task 7), menu item + background worker (Task 9), no real I/O in tests + CI-without-rumps (Tasks 7, 10). The two "verify empirically" spec items (token-exchange Content-Type, exact scopes) are centralized in Tasks 2/4 with comments. Non-goals (manual-paste, console login, CLI --login) are intentionally absent.
- **Placeholder scan:** none — every code/test step contains complete code.
- **Type consistency:** `run_login_flow` returns `LoginResult(credentials, identity)`; `identity` is `Identity(email, org_name, org_uuid, account_uuid)` consumed verbatim by `add_account_from_oauth(credentials=, email=, org_name=, org_uuid=, account_uuid=)` in Task 9. `make_server()` → object with `.port/.wait/.shutdown` matches `LoopbackServer` (Task 7). `exchange(code=, verifier=, redirect_uri=, state=)` matches `exchange_code` signature.
