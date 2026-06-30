"""Check PyPI for newer versions of claude-swap."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    from packaging.version import InvalidVersion, Version
    _HAVE_PACKAGING = True
except ImportError:  # pragma: no cover - packaging is a declared dependency
    # Defensive: the passive update check must "never fail" even in a stripped
    # environment that somehow lacks the declared `packaging` dependency. Degrade
    # to "no update" instead of letting an ImportError surface at command tail.
    _HAVE_PACKAGING = False

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
# A *failed* PyPI fetch is cached for only this long, so a single transient
# network error can't suppress every update check for the full CACHE_TTL.
NEGATIVE_CACHE_TTL = 15 * 60  # 15 minutes
PYPI_URL = "https://pypi.org/pypi/claude-swap/json"


def _parse_version(v: str) -> Version | None:
    """Parse a PEP 440 version string, or None if it's unparseable.

    Uses ``packaging.version.Version`` so pre-release/post/dev/epoch forms
    (e.g. ``0.15.0b1`` — which this project ships — ``1.3.0.post1``, ``1.3rc1``)
    compare correctly instead of crashing a naive int-split. A garbage version
    yields ``None`` so callers can treat it as "no update" rather than raising.
    """
    if not _HAVE_PACKAGING:
        return None
    try:
        return Version(v)
    except (InvalidVersion, TypeError):
        return None


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        latest_version = None

        # Try reading cache. A cached *failure* (None) is honoured only for the
        # short NEGATIVE_CACHE_TTL so a transient error doesn't suppress checks
        # for the full CACHE_TTL; a cached *success* lasts the full CACHE_TTL.
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is MISSING or cached_data is None:
            # Either there's no fresh entry, or the fresh entry is a cached
            # *failure* (None). A failure is only trustworthy for the short
            # negative TTL, so re-read at that TTL: a recent failure stays
            # honoured (skips the network), an older one falls back to MISSING
            # and triggers a retry.
            cached_data = read_cache(CACHE_PATH, NEGATIVE_CACHE_TTL)

        if cached_data is not MISSING:
            latest_version = cached_data
        else:
            # Fetch from PyPI
            try:
                req = urllib.request.Request(PYPI_URL)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode())
                latest_version = data["info"]["version"]
            except Exception:
                latest_version = None

            # Cache the result. A successful fetch lives for the full CACHE_TTL;
            # a failure (None) is still written (so a flapping network doesn't
            # hammer PyPI every run) but is only trusted for NEGATIVE_CACHE_TTL
            # at read time above, so the next run within 24h retries.
            write_cache(CACHE_PATH, latest_version)

        latest_parsed = _parse_version(latest_version) if latest_version else None
        current_parsed = _parse_version(current_version)
        if (
            latest_parsed is not None
            and current_parsed is not None
            and latest_parsed > current_parsed
        ):
            method = _detect_install_method()
            direct = {
                "uv": "uv tool upgrade claude-swap",
                "pipx": "pipx upgrade claude-swap",
            }.get(method or "")
            if direct and sys.platform != "win32":
                # cswap --upgrade actually performs the upgrade here.
                hint = "Run `cswap --upgrade` to update."
            elif direct:
                # Windows: cswap --upgrade only prints, so point at the real command.
                hint = f"Run `{direct}` to update."
            else:
                # Unknown install method: cswap --upgrade shows manual instructions.
                hint = "Run `cswap --upgrade` for upgrade instructions."
            return (
                f"A newer version of claude-swap is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None


def run_self_upgrade() -> int:
    """Run the appropriate upgrade command for the current install method.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH.
    """
    from claude_swap.printer import accent, error

    method = _detect_install_method()
    commands = {
        "uv": ["uv", "tool", "upgrade", "claude-swap"],
        "pipx": ["pipx", "upgrade", "claude-swap"],
    }
    cmd = commands.get(method or "")
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            "  uv tool upgrade claude-swap\n"
            "  pipx upgrade claude-swap\n"
            f"  {sys.executable} -m pip install --upgrade claude-swap\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
        return 1

    # Windows: the running cswap.exe launcher is locked, so an in-process
    # uv/pipx upgrade fails when it tries to replace the executable even
    # though the package itself updates. cswap exits right after this, which
    # releases the lock, so the user can just run the command themselves.
    if sys.platform == "win32":
        print(f"To upgrade claude-swap on Windows, run:\n  {accent(' '.join(cmd))}")
        return 1

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1
