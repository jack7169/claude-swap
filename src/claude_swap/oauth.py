"""OAuth token management and usage API for Claude Code accounts."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

from claude_swap.printer import warning as print_warning

OAUTH_BETA_HEADER = "oauth-2025-04-20"
OAUTH_EXPIRY_BUFFER_MS = 5 * 60 * 1000
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
RATE_LIMITED = "rate limited"
# Definitive "this credential is dead" sentinel for the usage path. The string
# value MUST equal json_output.USAGE_TOKEN_EXPIRED so the switcher merge's
# ``val in (USAGE_TOKEN_EXPIRED, …)`` comparison matches without importing it
# (mirrors how RATE_LIMITED / USAGE_RATE_LIMITED already interoperate).
TOKEN_EXPIRED = "token expired"
# fetch_usage_for_account signal: the TOKEN endpoint (refresh) is rate-limited
# (429), distinct from RATE_LIMITED (the USAGE endpoint). The token limit is
# per-IP and shared across every account, so continuing to refresh only keeps it
# pinned; the switcher intercepts this to arm a global refresh back-off, then
# renders it like RATE_LIMITED. Kept a DISTINCT string so a usage-429 (which must
# NOT back off — the roll spreads it) is never confused with a refresh-429.
TOKEN_RATE_LIMITED = "token rate limited"

# Why a refresh failed. A 400/401 from the token endpoint (invalid_grant) proves
# the refresh token is dead; a 429 is a shared per-IP rate limit (back off, don't
# hammer); everything else (other HTTP status, network blip, timeout, malformed
# 200) is transient and must NOT wipe good usage.
REFRESH_AUTH_FAILED = "auth"
REFRESH_TRANSIENT = "transient"
REFRESH_RATE_LIMITED = "rate_limited"

# The usage endpoint buckets rate limits on User-Agent, PER ACCESS TOKEN.
# ``claude-code/<version>`` is the safe bucket (fine at ~180s intervals); any
# other UA (e.g. the old ``claude-swap/1.0``) lands in an aggressively-throttled
# bucket that returns persistent 429s. Confirmed live: every account returned
# HTTP 200 with this UA while the fleet was 429-storming under claude-swap/1.0.
# The bucket keys on the ``claude-code/`` prefix, so the exact version is not
# load-bearing — just bump it occasionally.
CLAUDE_CODE_VERSION = "2.1.204"
CLAUDE_CODE_UA = f"claude-code/{CLAUDE_CODE_VERSION}"

_logger = logging.getLogger("claude-swap")


def extract_access_token(credentials: str) -> str | None:
    """Extract the OAuth access token from a credentials JSON string."""
    try:
        data = json.loads(credentials)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, AttributeError):
        return None


def extract_oauth_data(credentials: str) -> dict | None:
    """Extract the Claude AI OAuth payload from a credentials JSON string."""
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def is_oauth_token_expired(expires_at: object) -> bool:
    """Return whether an OAuth token is expired or about to expire."""
    if not isinstance(expires_at, (int, float)):
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + OAUTH_EXPIRY_BUFFER_MS >= int(expires_at)


def _refresh_with_reason(credentials: str) -> tuple[str | None, str]:
    """Refresh an OAuth access token, and report WHY it failed.

    Returns ``(new_credentials, "ok")`` on success, ``(None, REFRESH_AUTH_FAILED)``
    when the token endpoint returns 400/401 (a dead/revoked refresh token —
    ``invalid_grant``), and ``(None, REFRESH_TRANSIENT)`` for every other failure
    (other HTTP status, network/timeout, malformed 200, or a missing/unparseable
    refresh token). The usage path uses the auth/transient split to surface a
    definitively-dead credential (TOKEN_EXPIRED) without wiping good usage data
    on a mere network blip.
    """
    try:
        data = json.loads(credentials)
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None, REFRESH_TRANSIENT

        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            return None, REFRESH_TRANSIENT

        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }).encode()

        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": CLAUDE_CODE_UA,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_data = json.loads(resp.read().decode())

        # A 200 response missing access_token/expires_in must not half-update the
        # local oauth dict — validate BEFORE mutating, then bail (transient).
        new_access = resp_data.get("access_token")
        new_expires_in = resp_data.get("expires_in")
        if not new_access or not isinstance(new_expires_in, (int, float)):
            _logger.debug(
                "OAuth refresh got a 200 with missing access_token/expires_in"
            )
            return None, REFRESH_TRANSIENT

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        oauth["accessToken"] = new_access
        oauth["expiresAt"] = now_ms + int(new_expires_in) * 1000
        if resp_data.get("refresh_token"):
            oauth["refreshToken"] = resp_data["refresh_token"]
        if resp_data.get("scope"):
            oauth["scopes"] = resp_data["scope"].split()

        data["claudeAiOauth"] = oauth
        return json.dumps(data), "ok"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        _logger.debug("OAuth refresh failed: %r, body: %s", e, body[:500])
        reason = (
            REFRESH_AUTH_FAILED if e.code in (400, 401)
            else REFRESH_RATE_LIMITED if e.code == 429
            else REFRESH_TRANSIENT
        )
        return None, reason
    except Exception as e:
        _logger.debug("OAuth refresh failed: %r", e)
        return None, REFRESH_TRANSIENT


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh an OAuth access token via direct token endpoint POST.

    Thin wrapper over :func:`_refresh_with_reason` preserving the ``str | None``
    contract that ``session.py`` and other callers depend on.
    """
    return _refresh_with_reason(credentials)[0]



def build_token_status(credentials: str) -> str | None:
    """Return a short debug summary of stored OAuth token state."""
    oauth = extract_oauth_data(credentials)
    if not oauth:
        return None

    has_refresh_token = bool(oauth.get("refreshToken"))
    expires_at = oauth.get("expiresAt")
    refresh_str = "yes" if has_refresh_token else "no"

    if not isinstance(expires_at, (int, float)):
        return f"oauth: unknown expiry, refresh token {refresh_str}"

    try:
        expires_utc = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
        state = "expired" if is_oauth_token_expired(expires_at) else "fresh"
        countdown, clock = format_reset(expires_utc.isoformat())
    except (OverflowError, ValueError, OSError):
        # An out-of-range expiresAt (fromtimestamp can raise OverflowError/OSError/
        # ValueError) must not crash the token-status listing.
        return f"oauth: unknown expiry, refresh token {refresh_str}"
    return f"oauth: {state}, refresh token {refresh_str}, expires {clock} in {countdown}"


def format_reset(resets_at: str) -> tuple[str, str]:
    """Return (countdown, clock) for a reset time in local time."""
    reset_utc = datetime.fromisoformat(resets_at)
    # A naive ISO string (no tzinfo) would raise TypeError when subtracted from
    # the tz-aware ``now`` below, propagating out and voiding the whole account's
    # usage. Treat a naive timestamp as UTC.
    if reset_utc.tzinfo is None:
        reset_utc = reset_utc.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    remaining = reset_utc - now
    total_seconds = max(0, int(remaining.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days > 0:
        countdown = f"{days}d {hours}h"
    elif hours > 0:
        countdown = f"{hours}h {minutes}m"
    else:
        countdown = f"{minutes}m"

    reset_local = reset_utc.astimezone()
    now_local = now.astimezone()
    if reset_local.date() == now_local.date():
        time_str = reset_local.strftime("%H:%M")
    else:
        day = str(reset_local.day)
        time_str = reset_local.strftime(f"%b {day} %H:%M")

    return countdown, time_str


def request_usage_data(access_token: str) -> dict:
    """Request raw utilization data from the Anthropic usage API."""
    url = "https://api.anthropic.com/api/oauth/usage"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
        "User-Agent": CLAUDE_CODE_UA,
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())



def send_warm_message(
    access_token: str, model: str, max_tokens: int, timeout: float
) -> dict:
    """POST a minimal Messages API request to warm an account's 5-hour window.

    Mirrors :func:`request_usage_data`'s authenticated-request pattern
    (``Authorization: Bearer <token>`` + the ``anthropic-beta`` OAuth header)
    but against the Messages endpoint. Sends a fixed one-line prompt; the reply
    is discarded — the point is that the request registers against the account's
    5-hour window (starting its reset countdown) and confirms the token still
    authenticates. Returns the parsed 2xx body; propagates ``urllib`` errors on
    a non-2xx status or network failure (the caller in ``menubar.warm_account``
    classifies them into a health-check result). ``model`` is the bare Haiku
    alias (``claude-haiku-4-5``) — no date suffix.
    """
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
        # The Messages API is stricter than the OAuth usage endpoint: it returns
        # HTTP 400 ("anthropic-version: header is required") without this header.
        # Confirmed against the live endpoint; the stable version suffices.
        "anthropic-version": "2023-06-01",
        "User-Agent": CLAUDE_CODE_UA,
        "Content-Type": "application/json",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "can you hear me?"}],
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def build_usage_result(data: dict) -> dict | None:
    """Normalize raw usage API data into the structure used by the CLI."""
    _logger.debug("Usage API response: %s", json.dumps(data, indent=2))

    result = {}

    h5 = data.get("five_hour")
    # Skip a window whose utilization is missing rather than KeyErroring and
    # voiding the whole account's usage (mirrors the nullable-spend handling).
    if h5 and h5.get("utilization") is not None:
        h5_entry = {"pct": h5["utilization"]}
        if h5.get("resets_at"):
            h5_entry["resets_at"] = h5["resets_at"]
            h5_entry["countdown"], h5_entry["clock"] = format_reset(h5["resets_at"])
        result["five_hour"] = h5_entry

    d7 = data.get("seven_day")
    if d7 and d7.get("utilization") is not None:
        d7_entry = {"pct": d7["utilization"]}
        if d7.get("resets_at"):
            d7_entry["resets_at"] = d7["resets_at"]
            d7_entry["countdown"], d7_entry["clock"] = format_reset(d7["resets_at"])
        result["seven_day"] = d7_entry

    eu = data.get("extra_usage")
    if eu and eu.get("is_enabled"):
        # Claude Code returns nullable used_credits, monthly_limit, and utilization
        # (monthly_limit=None = unlimited). All three are needed to render the spend
        # line, so when any is null skip just the spend entry; five_hour/seven_day
        # go through unchanged.
        used_credits = eu.get("used_credits")
        monthly_limit = eu.get("monthly_limit")
        utilization = eu.get("utilization")
        if used_credits is not None and monthly_limit is not None and utilization is not None:
            try:
                spend_entry: dict = {
                    "used": float(used_credits) / 100,
                    "limit": float(monthly_limit) / 100,
                    "pct": float(utilization),
                    "currency": eu.get("currency", "USD"),
                }
                if eu.get("resets_at"):
                    spend_entry["resets_at"] = eu["resets_at"]
                    spend_entry["countdown"], spend_entry["clock"] = format_reset(eu["resets_at"])
                result["spend"] = spend_entry
            except (TypeError, ValueError) as e:
                _logger.debug("extra_usage parse failed: %r", e)

    # Model-scoped weekly limits (e.g. the Fable-5 weekly cap) live in a
    # ``limits`` array as ``weekly_scoped`` entries, NOT top-level seven_day_*
    # keys (which the API returns null). Parse every model-scoped weekly entry
    # generically into ``model_weekly`` keyed by the model's display name, so the
    # menubar/auto-swap pick up Fable — and any future model limit — automatically.
    # Defensive throughout: a missing/non-list ``limits`` or malformed entry is
    # skipped rather than voiding the whole account's usage.
    limits = data.get("limits")
    if isinstance(limits, list):
        model_weekly: dict = {}
        for entry in limits:
            if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
                continue
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            pct = entry.get("percent")
            if not name or pct is None:
                continue
            m_entry = {
                "pct": pct,
                "is_active": bool(entry.get("is_active")),
                "severity": entry.get("severity"),
            }
            if entry.get("resets_at"):
                m_entry["resets_at"] = entry["resets_at"]
                m_entry["countdown"], m_entry["clock"] = format_reset(
                    entry["resets_at"]
                )
            model_weekly[name] = m_entry
        if model_weekly:
            result["model_weekly"] = model_weekly

    return result if result else None


def account_headroom(usage: dict | None) -> float | None:
    """Remaining percentage before this account hits a rate-limit window.

    Considers only the 5-hour and 7-day utilization windows — the two that
    actually gate requests. ``spend`` (pay-as-you-go extra-usage credits) is a
    separate axis and is deliberately ignored. Returns the headroom of the
    *binding* window (``100 - max(pct)``), so ``<= 0`` means the account is at
    or over a limit. Returns ``None`` when usage is unavailable or carries no
    window data, which callers treat as "unknown" (never auto-skipped).
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    if not pcts:
        return None
    return 100.0 - max(pcts)


def fetch_usage(access_token: str) -> dict | None:
    """Fetch 5-hour and 7-day utilization from the Anthropic usage API."""
    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except Exception as e:
        _logger.debug("Usage fetch failed: %r", e)
        return None


def fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
) -> dict | str | None:
    """Fetch usage for an account, refreshing expired tokens for inactive accounts only.

    Active accounts are never refreshed — Claude Code owns those credentials.
    """
    oauth = extract_oauth_data(credentials)
    access_token = oauth.get("accessToken") if oauth else None
    if not access_token:
        return None

    working_credentials = credentials

    if (
        not is_active
        and oauth.get("refreshToken")
        and is_oauth_token_expired(oauth.get("expiresAt"))
    ):
        refreshed, reason = _refresh_with_reason(working_credentials)
        if refreshed:
            working_credentials = refreshed
            _persist(persist_credentials, account_num, email, working_credentials)
            oauth = extract_oauth_data(working_credentials) or oauth
            access_token = oauth.get("accessToken") or access_token
        elif reason == REFRESH_AUTH_FAILED:
            # Dead/revoked refresh token — definitive, not a transient blip.
            # Skip the usage call entirely: it would only 401 and (under a bad
            # UA bucket) add to the 429 storm. Surfaces as TOKEN_EXPIRED so the
            # switcher merge overrides any stale retained usage.
            return TOKEN_EXPIRED
        elif reason == REFRESH_RATE_LIMITED:
            # Token endpoint is rate-limited (shared per-IP). Falling through to a
            # doomed usage 401 would only add load; signal the switcher to back off
            # refreshes so the shared limit can reset.
            return TOKEN_RATE_LIMITED
        # transient -> fall through with the stale token (may 401 -> retry below)

    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _logger.debug("Usage fetch rate limited (429)")
            return RATE_LIMITED
        _logger.debug("Usage fetch failed: %r", e)
        if (
            e.code != 401
            or is_active
            or not oauth
            or not oauth.get("refreshToken")
        ):
            return None

        # Retry once after refreshing on 401 (inactive accounts only).
        refreshed, reason = _refresh_with_reason(working_credentials)
        if not refreshed:
            # A dead refresh token here is definitive -> TOKEN_EXPIRED; a rate-
            # limited token endpoint -> TOKEN_RATE_LIMITED (back off); a transient
            # blip stays None so the merge retains last-known-good.
            if reason == REFRESH_AUTH_FAILED:
                return TOKEN_EXPIRED
            if reason == REFRESH_RATE_LIMITED:
                return TOKEN_RATE_LIMITED
            return None

        working_credentials = refreshed
        _persist(persist_credentials, account_num, email, working_credentials)
        refreshed_oauth = extract_oauth_data(working_credentials)
        new_token = refreshed_oauth.get("accessToken") if refreshed_oauth else None
        if not new_token:
            return None

        try:
            data = request_usage_data(new_token)
            return build_usage_result(data)
        except urllib.error.HTTPError as retry_error:
            if retry_error.code == 429:
                return RATE_LIMITED
            _logger.debug("Usage fetch failed after refresh: %r", retry_error)
            return None
        except Exception as retry_error:
            _logger.debug("Usage fetch failed after refresh: %r", retry_error)
            return None
    except Exception as e:
        _logger.debug("Usage fetch failed: %r", e)
        return None


def _persist(
    callback: Callable[[str, str, str], None] | None,
    account_num: str,
    email: str,
    credentials: str,
) -> None:
    """Call the persist callback, warning loudly on failure."""
    if not callback:
        return
    try:
        callback(account_num, email, credentials)
    except Exception as e:
        _logger.warning(
            "Refreshed OAuth token for account %s (%s) but failed to persist it: %r. "
            "The refresh token on disk may now be stale; if the next refresh fails "
            "with invalid_grant, re-run `cswap --add-account` after logging in.",
            account_num,
            email,
            e,
        )
        print_warning(
            f"Warning: failed to save refreshed token for account {account_num} ({email}). "
            f"If the next refresh fails, re-run `cswap --add-account` after logging in."
        )
