#!/usr/bin/env bash
# cli-usage — Linux setup. Installs tray libraries with your package manager
# (apt, dnf, pacman or zypper; asks for sudo), then installs and starts the tray.
#   ./setup.sh               install + autostart
#   ./setup.sh --uninstall   remove it again
set -euo pipefail

cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.9+ first, e.g.:" >&2
  echo "  sudo apt install python3        # Debian/Ubuntu" >&2
  echo "  sudo dnf install python3        # Fedora" >&2
  echo "  sudo pacman -S python           # Arch" >&2
  exit 1
fi
# Use the system python3 even inside an activated venv/conda env: the GTK tray
# needs the distro's PyGObject, which those environments don't see.
PY=python3
for cand in /usr/bin/python3 /bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
exec "$PY" install.py --frontend auto --install-system-deps "$@"
