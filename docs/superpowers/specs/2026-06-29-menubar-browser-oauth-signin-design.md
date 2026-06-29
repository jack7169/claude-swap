# Menu-bar "Sign in to new account" — browser OAuth login

Date: 2026-06-29
Status: Approved

## Context

`cswap --menubar` can add accounts two ways today (`menubar.py` `_add_menu`):
"From current login" (`add_account` — snapshots the live Claude Code login) and
"From setup-token…" (`add_account_from_token` — wraps a pre-obtained
`sk-ant-oat01-…` token). Both require the user to already have a credential in
hand. There is **no** interactive browser login: the repo has no OAuth
*authorize* URL builder, no PKCE generator, and no authorization-code → token
exchange (`oauth.py` only refreshes existing tokens and reads usage).

This feature adds a third path: a menu item that runs Claude Code's real OAuth
browser login so a user can add a brand-new account without the CLI and without
pre-obtaining a token.

## Goal & decisions

- **Add-only.** A successful sign-in adds the account to the managed list but
  does **not** switch to it (consistent with the other two Add-account items;
  the live login is never touched, so any failure is harmless).
- **Loopback auto-capture.** A temporary `127.0.0.1` callback server captures the
  authorization code automatically — no copy/paste. This is Claude Code's own
  primary browser-login mechanism (ephemeral-port loopback redirect).
- **Account type:** Claude subscription (Pro/Max) — the account kind claude-swap
  manages (5h/7d usage windows). Console/API login is out of scope for v1.

## Verified OAuth parameters

The `client_id` and token endpoint below already exist in `oauth.py`
(`OAUTH_CLIENT_ID`, `OAUTH_TOKEN_URL`) and are known-good for token refresh.

| Parameter | Value |
| --- | --- |
| Authorize endpoint | `https://claude.ai/oauth/authorize` |
| Token endpoint | `https://platform.claude.com/v1/oauth/token` |
| `client_id` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| `response_type` | `code` |
| Scopes | `user:profile user:inference user:sessions:claude_code user:mcp_servers` |
| Redirect URI | `http://localhost:<port>/callback` (loopback, port-agnostic) |
| PKCE | `code_challenge_method=S256` |
| `state` | base64url-encoded 32 random bytes (CSRF protection) |

**Two values verified empirically during implementation** (centralized as
constants so a correction is a one-line change):

1. **Token-exchange `Content-Type`.** `oauth.refresh_oauth_credentials` already
   POSTs JSON to this endpoint successfully, but one reference says the
   `authorization_code` grant requires `application/x-www-form-urlencoded` (else
   `invalid_grant`). Start with JSON (repo-consistent); fall back to form
   encoding if the endpoint rejects it.
2. **Exact scope string.** Use the table value; confirm it is accepted (an
   "Unknown scope" 400 means it needs adjusting).

## User flow

1. Menu ▸ **Add account** ▸ **"Sign in with browser…"**.
2. claude-swap binds a free port on `127.0.0.1`, generates PKCE
   (`verifier`/`challenge`) and a random `state`, and opens the default browser
   to the authorize URL (`redirect_uri=http://localhost:<port>/callback`).
3. User approves in the browser. Claude redirects to
   `http://localhost:<port>/callback?code=…&state=…`.
4. The callback server validates `state`, captures `code`, and serves a minimal
   "You can close this tab" page.
5. claude-swap exchanges `code` + `verifier` for tokens at the token endpoint.
6. It extracts the access/refresh tokens, `expiresAt`, scopes, and the real
   email / organization / account-uuid from the response.
7. It stores the account (add-only). The menu refreshes; a notification confirms.

## Architecture

### New module `oauth_login.py`

Pure helpers (fully unit-tested) with the impure orchestration kept thin and
dependency-injected so the suite never opens a browser, binds a port, or hits the
network:

- `generate_pkce() -> (verifier, challenge)` — RFC 7636 S256. Randomness source
  injectable for deterministic tests.
- `build_authorize_url(*, redirect_uri, state, code_challenge) -> str` — pure.
- `parse_callback_query(query: str) -> CallbackResult` — pure. Returns the
  `(code, state)` on success; recognises `error=access_denied` (user declined)
  and malformed/missing params as distinct, friendly failures.
- `build_token_exchange(*, code, verifier, redirect_uri, state) -> (url, body, headers)`
  — pure; encodes the `authorization_code` grant.
- `credentials_from_token_response(data: dict) -> (credentials_json, Identity)` —
  pure. Builds the Claude Code credential JSON
  (`{"claudeAiOauth": {accessToken, refreshToken, expiresAt, scopes, …}}`) and
  extracts `Identity(email, org_name, org_uuid, account_uuid)`. Tolerates missing
  identity fields (falls back to a synthesized email, like the token path).
- `run_login_flow(*, open_browser, make_server, exchange, timeout=180) -> LoginResult`
  — orchestrator. Picks the port, builds the URL, opens the browser, waits for
  the callback (with timeout), validates `state`, and runs the exchange. All
  side-effecting collaborators are injected.

### Switcher

- New `add_account_from_oauth(self, *, credentials, email, org_name, org_uuid,
  account_uuid, slot=None)` — stores a full-OAuth account (real identity, refresh
  token).
- Refactor: extract the shared store body of `add_account_from_token` into a
  private `_store_new_account(...)` that both paths call (collision rejection,
  credential/config write, sequence update). No behaviour change to the existing
  token path; covered by its current tests.

### Menu (`menubar.py`)

- `_add_menu` gains **"Sign in with browser…"** → `on_add_browser_login`.
- The callback activates the app (like `on_add_token`), then runs the flow on a
  **background thread** (mirroring the existing `refresh_async` worker) so the
  rumps run-loop is never blocked while the user authorizes. On success it calls
  `add_account_from_oauth` and `refresh_async(full=True)`; on failure it shows a
  `rumps.alert`. The non-swap nature means no double-notify concerns.

## Error handling & security

- Loopback socket bound to `127.0.0.1` only.
- `state` is validated on the callback (CSRF / cross-flow protection).
- Distinct, friendly failures for: port-bind failure, 180 s timeout (no callback),
  user-denied (`error=access_denied`), `state` mismatch, and a non-200 / malformed
  token-exchange response. Every path aborts cleanly.
- Add-only: the live login and active account are never modified, so any failure
  leaves the system exactly as it was.

## Testing (TDD)

- **Pure helpers**, fully unit-tested: PKCE output shape/charset and
  verifier→challenge S256 relationship; authorize-URL parameter set and encoding;
  callback parsing for success, `access_denied`, missing/garbage params, and
  `state` mismatch; token-exchange request construction; token-response →
  credentials/identity including missing-identity fallback.
- **Orchestrator** `run_login_flow` tested with injected fakes (fake
  `open_browser`, a fake server that returns a canned callback, a fake
  `exchange`): asserts URL/`state`/PKCE wiring and the returned payload — no real
  browser, port, or network.
- **Switcher** `add_account_from_oauth` / `_store_new_account` tested with the
  in-memory credential/config stores used by the existing add tests.
- The rumps menu glue (`on_add_browser_login`) is not unit-tested, consistent
  with the current menu-bar test strategy (the suite never imports rumps).
- No real network, browser, or bound port anywhere in the suite; it stays green
  with and without the `menubar` extra (CI runs without rumps).

## Risks

- Replicates Claude Code's OAuth client parameters (authorize host, scopes,
  redirect form). These are not published by Anthropic as a stable contract and
  could change — the same fragility class as the existing usage/refresh calls.
  Mitigated by centralizing the constants, reusing the repo's known-good
  `client_id`/token endpoint, and verifying the two empirical values above during
  implementation.

## Non-goals (v1)

- Manual code-paste fallback when loopback can't be used.
- Console / API-account login.
- A CLI `--login` equivalent (the orchestration will live in `oauth_login.py`, so
  adding one later is cheap).
