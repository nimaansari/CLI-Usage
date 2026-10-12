#!/usr/bin/env bash
# cli-usage — macOS setup. Builds ~/Applications/CLI Usage.app and starts it at login.
#   ./setup_macos.sh               install + autostart
#   ./setup_macos.sh --uninstall   remove it again
set -euo pipefail

cd "$(dirname "$0")"
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Xcode Command Line Tools are needed (they provide python3, clang and codesign)." >&2
  echo "Run:  xcode-select --install   then re-run ./setup_macos.sh" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found. Install it with: xcode-select --install  (or brew install python)" >&2
  exit 1
fi
exec python3 install.py --frontend xplat "$@"
