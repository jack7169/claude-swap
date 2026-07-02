#!/usr/bin/env bash
# Build a self-contained, ad-hoc-signed claude-swap.app for personal use.
# Builds against Python 3.12 (py2app is unreliable on 3.14). Not for public
# distribution — ad-hoc signing only (no Developer ID / notarization).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_VENV="$ROOT/build/appvenv"
PY312="${PYTHON312:-$(command -v python3.12 || echo /opt/homebrew/bin/python3.12)}"

echo "==> build venv (Python 3.12) at $BUILD_VENV"
"$PY312" -m venv "$BUILD_VENV"
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"
python -m pip install --upgrade pip wheel
python -m pip install "py2app" "pyobjc-framework-ServiceManagement"
python -m pip install "$ROOT[menubar]"

echo "==> generate icon (best-effort)"
python "$ROOT/packaging/make-icon.py" || echo "icon generation skipped"

echo "==> py2app build"
rm -rf "$ROOT/dist/claude-swap.app"
( cd "$ROOT" && python packaging/setup_app.py py2app )

APP="$ROOT/dist/claude-swap.app"
echo "==> ad-hoc code sign"
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict "$APP" && echo "signature OK"

deactivate
cat <<EOF

Built: $APP

Next steps (manual, one time):
  1. mv "$APP" /Applications/          # SMAppService needs a stable location
  2. Right-click > Open once           # clear the ad-hoc Gatekeeper prompt
  3. Menu bar > Start at login         # register the native Login Item
  4. If migrating from the pip install:  cswap --uninstall-startup
EOF
