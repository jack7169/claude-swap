# Auto timer start — design

**Status:** approved (design), pending implementation plan
**Date:** 2026-07-01

## Overview

A new menu-bar toggle, **Auto timer start**, that keeps every managed account's
5-hour session window *warm*. While enabled, cswap automatically detects managed
accounts whose session-limit reset countdown is **not currently running** and
sends each one a single, minimal Haiku message (`"can you hear me?"`). This does
two things at once:

1. **Confirms the account still authenticates** (a failed send surfaces a broken
   / logged-out / expired account — a health check).
2. **Starts the account's 5-hour timer early**, so the window is already counting
   down before the user needs to switch to it.

It is the natural complement to auto-switch: auto-switch moves *away* from
exhausted accounts; auto timer start makes sure the *other* accounts' windows are
already ticking so a later switch lands on an account with a known, in-progress
reset schedule.

## Goals

- A persistent ON/OFF toggle, styled and placed **directly below the Auto-switch
  ON/OFF item** in the menu.
- Hands-free: while ON, idle accounts are warmed automatically with no user action.
- Non-disruptive: warming an account must **not** switch the live account.
- Self-limiting: each idle account is pinged at most once per 5-hour window.
- Failures are surfaced (health-check value).

## Non-goals

- Not a scheduler with its own configurable interval/threshold UI (v1 piggybacks
  on the existing refresh cadence; a dedicated interval can come later if needed).
- Not a general "send a message" feature — the payload is fixed and minimal.
- Does not touch API-key accounts (no subscription window; a send would bill).

## Detection — "no active countdown until session-limit reset"

The refresh cycle already fetches every account's usage. An account is a **warm
candidate** when, in its usage snapshot, the **5-hour window has no `resets_at`**
(the window has not started). This is exactly the field populated in
`oauth.build_usage_result` (`h5_entry["resets_at"]` is only set when the API
returns one).

Excluded from candidacy:
- **API-key accounts** (`looks_like_api_key` on the stored credential) — no
  subscription window to start, and a send would incur billed usage.
- Accounts warmed within the **per-account cooldown** (see below).
- Accounts with no usable stored token (nothing to send with) — though attempting
  and failing is acceptable and produces the health-check signal.

## Warm action (no switching)

Reuse the existing authenticated-request pattern in `oauth.py`
(`Authorization: Bearer <access_token>` + the `anthropic-beta` OAuth header, as
in `request_usage_data`) against the **Messages API**
(`https://api.anthropic.com/v1/messages`), using the candidate account's
**stored backup token directly** — no account switch, no disruption to the live
account.

Request shape (minimal):
- `model`: `claude-haiku-4-5` (concrete id to confirm at implementation via the
  `claude-api` skill; current is `claude-haiku-4-5-20251001`).
- `max_tokens`: small (e.g. 1–16) — the response is discarded; the point is that
  the request registers against the 5-hour window.
- `messages`: `[{"role": "user", "content": "can you hear me?"}]`.
- Short network timeout (align with the existing usage-fetch timeout).

The warm path is **send-only**: it uses the stored token read-only and never
refreshes or writes any credential. (Revised from the original "proactively
refresh first" design after review found refreshing here to be both unsafe and
unnecessary.) Refreshing rotates the server-side refresh token, and the warm
path can only persist to the *backup* store — so refreshing the **active**
account here would leave the live store (what Claude Code reads) holding the
now-invalidated token, forcing a re-login; refreshing a backup could likewise
race a concurrent switch's read-modify-write of that backup. It is also
redundant: an account only becomes a candidate because its usage fetch just
succeeded, so the stored token is valid for the immediately-following send, and
genuinely-expired **backup** tokens are already refreshed under the sequence
lock by `oauth.fetch_usage_for_account` earlier in the same worker cycle. If a
token nonetheless expires in the seconds before the send, the 401 is reported
and the account is retried next cycle (by which point the usage path has
refreshed it) — self-healing, with no credential writes on the warm path.

## Dedup / cooldown

After a **successful** send, record `warmed_at[account]` and skip re-warming that
account for **~10 minutes**. Rationale: once warmed, the window's `resets_at`
appears and the account drops out of the candidate set naturally — but the usage
endpoint (and cswap's usage cache) take a moment to reflect it, so the cooldown
prevents a double-send in that propagation gap. After the cooldown, the
"has `resets_at`" check is the durable self-limiter: each idle account is warmed
exactly once per 5-hour window.

## Health-check reporting

- **Failure** (HTTP 401 / expired token / network error): `notify.notify`
  (`"Account-N timer start failed: <reason>"`) — surfaces broken accounts.
- **Success**: a single brief summary notification per run
  (`"Started timers for N account(s)"`), not one-per-account.

## Settings + menu

- `MenuBarSettings.auto_timer_start_enabled: bool = False` (persisted).
- A checkable `rumps.MenuItem` toggle inserted **immediately below** the existing
  Auto-switch ON/OFF item, following that item's construction pattern and its
  callback style (flip setting → save → no restart needed).

## Threading & safety

- The detection + sends run on the existing **refresh worker thread** (off the
  Cocoa main thread), where the usage snapshot is already available.
- Sends are HTTP via `urllib` — **no `fork()`** — so there is zero interaction
  with the fork-serialization freeze fix (`spawn.fork_lock`).
- No account switching **and no credential writes** (send-only) → independent of
  auto-switch and of the credential store; the two toggles can be on together
  without conflict, with no sequence-lock interaction.

## Risks / validation

1. **Feasibility (primary):** confirm the Messages API accepts a Claude Code
   OAuth token. The usage endpoint already works with this token, and Claude Code
   itself sends via it, so it is expected to work — but the very first
   implementation step is a single real send against one account to validate the
   exact request shape (headers, beta flag, model id) before wiring the loop.
2. **Quota / ToS:** each ping consumes a sliver of that account's 5-hour budget
   (intended — that is what starts the timer) and is an automated multi-account
   send. Implemented as specified; documented so the behavior is explicit.
3. **Token freshness:** the warm path does **not** refresh (send-only, see
   above); it relies on the usage-fetch path having refreshed genuinely-expired
   backup tokens earlier in the same cycle. A token that expires in the brief gap
   before the send 401s and is retried next cycle — never misreported durably.

## Testing

Follows the existing import-safe menu-bar test patterns (no rumps/AppKit):
- Pure-function **candidate selection**: idle detection (`resets_at` absent),
  API-key exclusion, cooldown suppression, active-account inclusion.
- The **send** is exercised through a mocked HTTP layer (like the usage tests);
  assert one request per candidate, correct auth header/model/payload, and that a
  non-2xx / exception is caught and reported without breaking the refresh cycle.
- **Toggle persistence** round-trips through `MenuBarSettings`.
- **Cooldown**: a second run within the window sends nothing.

## Chosen defaults

- Cooldown: 10 minutes.
- Scope: all idle **OAuth** accounts, **including** the currently-active one.
- Notifications: notify on failures + a one-line success summary per run.
