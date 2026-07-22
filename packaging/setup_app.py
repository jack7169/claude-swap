"""py2app build recipe for claude-swap.app (run inside the 3.12 build venv).

Invoked by packaging/make-app.sh:  python setup_app.py py2app
Reads the version from pyproject.toml so it can't drift. Bundles the lazily
imported ServiceManagement framework explicitly (py2app can't see lazy imports).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ICNS = Path(__file__).resolve().parent / "claude-swap.icns"

APP = [str(ROOT / "src" / "claude_swap" / "app_main.py")]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["claude_swap", "rumps"],
    # Lazily imported at runtime -> py2app won't auto-detect it; include explicitly.
    "includes": ["ServiceManagement"],
    "plist": {
        # CFBundleName stays "claude-swap" so the built bundle is claude-swap.app
        # (py2app names the bundle after it) — keeps the /Applications path and the
        # make-app.sh mv stable. Only the user-visible DISPLAY name is rebranded.
        "CFBundleName": "claude-swap",
        "CFBundleDisplayName": "claude-swap 2",
        # Bundle id UNCHANGED so the existing SMAppService Login Item registration
        # (keyed on the id) keeps working across the rename.
        "CFBundleIdentifier": "com.claude-swap.menubar",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,  # menu-bar agent, no Dock icon
        "LSMinimumSystemVersion": "13.0",  # SMAppService
    },
}
if ICNS.exists():
    OPTIONS["iconfile"] = str(ICNS)

setup(
    app=APP,
    name="claude-swap",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
