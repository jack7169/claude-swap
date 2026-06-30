"""macOS Keychain access via the ``security`` CLI.

A small wrapper around the system ``security`` tool for storing generic
passwords, used instead of the third-party ``keyring`` library. Two reasons:

- The macOS hot path no longer needs the ``keyring`` dependency.
- Keychain items are created and read by the same stable ``security`` binary, so
  reads stay silent across upgrades. ``keyring`` (and any in-process
  Security.framework call) anchors the item's access to the *Python interpreter*,
  which ``uv tool upgrade`` rebuilds — at which point macOS can show the "wants to
  use your keychain" prompt. ``security`` never changes, so creator == reader and
  there is no prompt.

The read/write/delete shapes mirror Claude Code's own implementation
(``utils/secureStorage/macOsKeychainStorage.ts``):

- ``set_password`` hex-encodes the value (``-X``) and pipes the command through
  ``security -i`` (stdin) so the secret never appears in process argv (a
  process-monitor / CrowdStrike concern). It falls back to argv only when the
  command would overflow ``security -i``'s 4096-byte stdin line buffer, which
  would otherwise truncate mid-argument and silently corrupt the entry.
- ``get_password`` uses ``find-generic-password ... -w`` and treats exit code 44
  as "not found" (returns ``None``); any *other* non-zero exit raises so callers
  can tell a genuine miss apart from a locked/denied/unavailable Keychain.

Caveat: values must be printable text. ``find-generic-password -w`` prints the
stored data raw only when it is printable; data with non-printable bytes comes
back *hex-encoded*, so a write/read round-trip would not be identity. Fine for
this codebase (credentials are ASCII JSON), but don't reuse this wrapper for
arbitrary binary data. Claude Code's ``-w`` reads share the same constraint.

This module is import-safe on every platform (it only shells out at call time);
its functions are only meaningful on macOS.
"""

from __future__ import annotations

import os
import subprocess

# ``security -i`` reads stdin with a 4096-byte fgets() buffer (BUFSIZ on darwin).
# A command line longer than this is truncated mid-argument: it fails to write
# while leaving any previous entry intact (Claude Code #30337). 64 bytes of
# headroom guards against line-terminator accounting differences.
SECURITY_STDIN_LINE_LIMIT = 4096 - 64

_NOT_FOUND_RC = 44  # errSecItemNotFound surfaced by find/delete-generic-password

# Bound every ``security`` spawn so a wedged Keychain (a locked login keychain
# prompting for an unlock that never comes on a headless/SSH host) can't hang the
# CLI. 5s, deliberately short: a credential op that has to fall back to the file
# may be followed by a best-effort cleanup spawn, so the per-op budget doubles in
# the worst case. A healthy Keychain answers in well under 100ms.
_TIMEOUT = 5.0

# Pin the absolute path to Apple's system binary rather than resolving via PATH:
# this is a credential tool, so an attacker-controlled ``security`` earlier on
# PATH must not be able to intercept secrets. ``/usr/bin/security`` is present on
# every macOS.
_SECURITY = "/usr/bin/security"


class KeychainError(Exception):
    """A ``security`` invocation failed for a reason other than "not found"."""


# The exceptions a Keychain operation may raise that callers should treat as
# "Keychain unusable" (→ fall back to file storage) rather than a programming
# bug: a wrapper failure (KeychainError, incl. a converted timeout), a raw
# subprocess timeout, or a missing ``security`` binary (OSError). Catching this
# tuple — never bare ``Exception`` — keeps a real bug loud instead of silently
# routing to the file backend mid-invocation.
KEYCHAIN_ERRORS = (KeychainError, subprocess.TimeoutExpired, OSError)


def keychain_account_name() -> str:
    """Account name for the active-credential Keychain item, mirroring Claude
    Code's ``getUsername()`` (``utils/secureStorage/macOsKeychainHelpers.ts``).

    ``$USER`` first, then the OS username, then a stable final fallback. Matching
    this exactly matters on headless/launchd/cron hosts where ``$USER`` is unset:
    a divergent default (e.g. ``"user"``) would key a *different* Keychain item
    than Claude Code, so the two could not see each other's active credential.
    """
    user = os.environ.get("USER")
    if user:
        return user
    try:
        import pwd  # POSIX-only; the account-name call sites are macOS-only

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return "claude-code-user"


def _login_keychain_path() -> str | None:
    """Absolute path to the user's login keychain, or ``None`` if not found.

    Naming the target keychain on ``add-generic-password`` avoids
    ``errSecNoDefaultKeychain`` — the GUI dialog "A keychain cannot be found to
    store …" macOS raises on an *add* when the session has no *default* keychain
    set. The menu bar runs in a launchd/GUI-app session that often has no default
    keychain, yet the login keychain file still exists; reads/deletes search the
    keychain list and are unaffected, so only writes need this explicit target.
    Returns ``None`` off macOS (no such path), leaving the command unchanged.
    """
    keychains_dir = os.path.join(os.path.expanduser("~"), "Library", "Keychains")
    for name in ("login.keychain-db", "login.keychain"):
        path = os.path.join(keychains_dir, name)
        if os.path.exists(path):
            return path
    return None


def _validate_name(value: str) -> None:
    """Reject an account/service name containing a control character (< 0x20).

    Why every entry point validates, not just ``set_password``: on a write the
    name rides a ``security -i`` stdin command line that is split on ``\\n``/``\\r``
    *before* tokenising, so a control char there would end the command early and
    run the value's tail as a separate ``security`` subcommand (command
    injection). The read/delete/exists paths build argv lists, so the same name is
    *not* an injection vector there — but accepting on read/delete a name that
    write rejects is a confusing inconsistency (an item could never have been
    written under it). Apply the identical check everywhere so a given name is
    either valid for all operations or none. Valid accounts/services never contain
    a control character. Raises :class:`KeychainError`.
    """
    if any(ord(ch) < 0x20 for ch in value):
        raise KeychainError(
            "keychain account/service name contains an illegal control character"
        )


def _quote(value: str) -> str:
    """Quote a value for a ``security -i`` stdin command line.

    ``security -i`` re-parses each line shell-style, so wrap the value in double
    quotes and backslash-escape any embedded ``"``/``\\`` (e.g. the active-
    credential service name contains a space).

    Quoting cannot contain a newline: ``security -i`` splits stdin on line
    boundaries *before* tokenising, so an embedded ``\\n``/``\\r`` would end the
    command early and run the value's tail as a separate ``security`` subcommand
    (command injection). Reject any control character outright (via
    :func:`_validate_name`) rather than emit a multi-line payload — valid
    accounts/services never contain one.
    """
    _validate_name(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def get_password(service: str, account: str) -> str | None:
    """Return the stored password, or ``None`` if no such item exists (rc 44).

    Raises :class:`KeychainError` on any other non-zero exit (locked / denied /
    unavailable) or a timeout, so a genuine miss is not confused with a transient
    failure.

    Reads search the default keychain *list* rather than naming a keychain (unlike
    ``set_password``, which names login.keychain to dodge ``errSecNoDefaultKeychain``
    on an *add* in a launchd/GUI session — see ``_login_keychain_path``). This is
    safe: the login keychain is normally in the search list, so a value written
    there is found by a list-searching read, and search needs no *default*
    keychain set, so the launchd-context failure mode that motivated the explicit
    write target does not apply to reads.
    """
    _validate_name(account)
    _validate_name(service)
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security find-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode == 0:
        # `-w` prints the value followed by one newline; strip exactly that so
        # values with meaningful leading/trailing whitespace survive intact.
        return result.stdout.removesuffix("\n")
    if result.returncode == _NOT_FOUND_RC:
        return None
    raise KeychainError(
        f"security find-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def item_exists(service: str, account: str) -> bool:
    """Whether a generic-password item exists, without touching its secret.

    Attribute-only lookup (no ``-w``): nothing is decrypted, so this can never
    trigger a Keychain prompt, even for items owned by another app. Returns
    ``True`` only on rc 0; "not found" (rc 44), error exits, a timeout, and a
    missing binary all return ``False``. Deliberately **non-raising**: callers use
    it for cleanup verification, not access decisions, so it must never feed the
    capability cache (a timeout here means "couldn't tell", not "Keychain works").

    A name with a control character is also "not found": ``set_password`` rejects
    such names, so no item can exist under one. Returning ``False`` (rather than
    raising, as ``get_password``/``delete_password`` do) keeps this method
    non-raising while staying consistent with the rest of the wrapper.

    Like ``get_password``, this searches the default keychain list rather than
    naming a keychain — safe for the same reason (login.keychain is normally in
    the list, and search needs no *default* keychain set).
    """
    if any(ord(ch) < 0x20 for ch in account) or any(ord(ch) < 0x20 for ch in service):
        return False
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def set_password(service: str, account: str, password: str) -> None:
    """Create or update a generic-password item (``-U``).

    Prefers ``security -i`` stdin so the secret stays out of argv; falls back to
    argv only for payloads that would overflow the stdin line buffer. Raises
    :class:`KeychainError` on a non-zero exit or a timeout.
    """
    hex_value = password.encode("utf-8").hex()
    # Name the login keychain explicitly (trailing positional arg) so the add
    # never depends on a *default* keychain being set in this session — otherwise
    # a launchd/GUI-app context raises errSecNoDefaultKeychain and macOS pops
    # "A keychain cannot be found to store …". None (e.g. off macOS) → unchanged.
    keychain = _login_keychain_path()
    # `-X` passes the value as hex, avoiding any escaping issues for the secret.
    command = (
        f"add-generic-password -U -a {_quote(account)} -s {_quote(service)} "
        f"-X {hex_value}"
    )
    if keychain:
        command += f" {_quote(keychain)}"
    command += "\n"
    argv = [
        _SECURITY, "add-generic-password", "-U",
        "-a", account, "-s", service, "-X", hex_value,
    ]
    if keychain:
        argv.append(keychain)
    try:
        if len(command.encode("utf-8")) <= SECURITY_STDIN_LINE_LIMIT:
            result = subprocess.run(
                [_SECURITY, "-i"],
                input=command,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
        else:
            # Overflows the stdin line buffer; fall back to argv. Hex in argv is
            # recoverable by a determined observer but defeats naive plaintext-grep
            # rules, and the alternative — silent corruption — is strictly worse.
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security add-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode != 0:
        raise KeychainError(
            f"security add-generic-password failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def delete_password(service: str, account: str) -> None:
    """Delete a generic-password item. rc 44 (already absent) counts as success.

    Raises :class:`KeychainError` on any other non-zero exit or a timeout.

    Searches the default keychain list rather than naming a keychain (matching
    ``get_password``); safe because login.keychain is normally in the list and
    search needs no *default* keychain set, so the launchd-context failure mode
    that makes ``set_password`` name the keychain explicitly does not apply here.
    """
    _validate_name(account)
    _validate_name(service)
    try:
        result = subprocess.run(
            [_SECURITY, "delete-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security delete-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode in (0, _NOT_FOUND_RC):
        return
    raise KeychainError(
        f"security delete-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )
