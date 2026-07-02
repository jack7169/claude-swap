"""macOS 'Start at login' via SMAppService (the modern Login Item API).

Used by the bundled ``.app`` (see packaging/) to register itself as a per-user
Login Item — the app-bundle equivalent of the LaunchAgent that ``cswap
--install-startup`` writes for the pip/terminal install. SMAppService requires a
real, signed app in a stable location, so these helpers are only meaningful when
running from a bundle (see :func:`is_bundled`); a ``python -m claude_swap`` run
uses the LaunchAgent path instead.

Import-safe: the ``ServiceManagement`` pyobjc framework is imported lazily inside
:func:`_main_service` (mirroring the rumps/keyring optional-import pattern), so
this module imports — and its pure helpers unit-test — without the framework.
"""

from __future__ import annotations

import sys

# SMAppServiceStatus raw values (ServiceManagement/SMAppService.h).
_STATUS_NAMES = {
    0: "not-registered",     # SMAppServiceStatusNotRegistered
    1: "enabled",            # SMAppServiceStatusEnabled
    2: "requires-approval",  # SMAppServiceStatusRequiresApproval
    3: "not-found",          # SMAppServiceStatusNotFound
}


def is_bundled() -> bool:
    """True when running from inside a py2app ``.app`` bundle.

    SMAppService can register only a real bundled app, so the "Start at login"
    control is shown only when this is True. py2app sets ``sys.frozen``; the
    ``.app/Contents/`` executable path is a fallback signal.
    """
    if getattr(sys, "frozen", False):
        return True
    return ".app/Contents/" in (sys.executable or "")


def _status_name(raw: int) -> str:
    """Map an SMAppServiceStatus raw value to a stable lowercase string (pure)."""
    return _STATUS_NAMES.get(raw, "unknown")


def _main_service():
    """Return ``SMAppService.mainAppService()`` or ``None`` if unavailable.

    Lazy import keeps this module import-safe without pyobjc-ServiceManagement
    (absent in the pip/terminal install; bundled into the .app by py2app).
    """
    try:
        from ServiceManagement import SMAppService
    except Exception:
        return None
    try:
        return SMAppService.mainAppService()
    except Exception:
        return None


def status() -> str:
    """Current Login Item status as a stable string; 'unavailable' off-bundle."""
    svc = _main_service()
    if svc is None:
        return "unavailable"
    try:
        return _status_name(int(svc.status()))
    except Exception:
        return "unknown"


def enable() -> tuple[bool, str | None]:
    """Register the app as a Login Item. Returns (ok, error). Never raises."""
    svc = _main_service()
    if svc is None:
        return (False, "ServiceManagement unavailable")
    try:
        ok, err = svc.registerAndReturnError_(None)
        return (bool(ok), None if ok else str(err))
    except Exception as e:
        return (False, repr(e))


def disable() -> tuple[bool, str | None]:
    """Unregister the Login Item. Returns (ok, error). Never raises."""
    svc = _main_service()
    if svc is None:
        return (False, "ServiceManagement unavailable")
    try:
        ok, err = svc.unregisterAndReturnError_(None)
        return (bool(ok), None if ok else str(err))
    except Exception as e:
        return (False, repr(e))


def toggle() -> tuple[bool, str | None]:
    """Enable if not currently enabled, else disable. Returns (ok, error)."""
    return disable() if status() == "enabled" else enable()
