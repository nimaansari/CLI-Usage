#!/usr/bin/env bash
# cli-usage one-line installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/nimaansari/CLI-Usage/main/install.sh | bash
#
# Downloads (or updates) cli-usage into ~/.local/share/cli-usage, then runs the
# platform setup. Run the same command again to update. Extra arguments go to
# install.py, e.g.  ... | bash -s -- --uninstall
#
# Overrides: CLI_USAGE_DIR (install folder), CLI_USAGE_REPO (git URL), CLI_USAGE_REF (branch).
set -euo pipefail

main() {
  local repo="${CLI_USAGE_REPO:-https://github.com/nimaansari/CLI-Usage.git}"
  local ref="${CLI_USAGE_REF:-main}"
  local dir="${CLI_USAGE_DIR:-$HOME/.local/share/cli-usage}"
  local os
  os="$(uname -s)"

  say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
  die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

  case "$os" in
    Linux|Darwin) ;;
    *) die "This installer is for Linux and macOS. On Windows run in PowerShell:
  irm https://raw.githubusercontent.com/nimaansari/CLI-Usage/main/install.ps1 | iex" ;;
  esac

  # macOS: Python, clang and codesign come with the Xcode Command Line Tools.
  if [ "$os" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
    say "Installing the Xcode Command Line Tools (a macOS dialog will open)..."
    xcode-select --install >/dev/null 2>&1 || true
    say "Waiting for the install to finish (click Install in the dialog)..."
    local waited=0
    until xcode-select -p >/dev/null 2>&1; do
      sleep 10; waited=$((waited + 10))
      [ "$waited" -ge 3600 ] && die "Command Line Tools still missing; finish that install and re-run this command."
    done
  fi

  # Linux: make sure python3 exists.
  if [ "$os" = Linux ] && ! command -v python3 >/dev/null 2>&1; then
    say "Installing python3 (sudo may ask for your password)..."
    local sudo=""
    [ "$(id -u)" -ne 0 ] && sudo="sudo"
    if   command -v apt-get >/dev/null; then $sudo apt-get update && $sudo apt-get install -y python3 python3-venv
    elif command -v dnf     >/dev/null; then $sudo dnf install -y python3
    elif command -v pacman  >/dev/null; then $sudo pacman -S --needed --noconfirm python
    elif command -v zypper  >/dev/null; then $sudo zypper --non-interactive install python3
    else die "Please install Python 3.9+ and re-run this command."
    fi
  fi

  say "Getting cli-usage into $dir"
  mkdir -p "$(dirname "$dir")"
  if [ -d "$dir/.git" ] && command -v git >/dev/null 2>&1; then
    git -C "$dir" fetch --quiet origin "$ref"
    git -C "$dir" checkout --quiet "$ref" 2>/dev/null || true
    git -C "$dir" reset --quiet --hard "origin/$ref"
  elif command -v git >/dev/null 2>&1 && [ ! -e "$dir" ]; then
    git clone --quiet --depth 1 --branch "$ref" "$repo" "$dir"
  else
    # No git (or an existing non-git copy): download the source tarball.
    local base="${repo%.git}"
    local tmp
    tmp="$(mktemp -d)"
    curl -fsSL "$base/archive/refs/heads/$ref.tar.gz" -o "$tmp/src.tar.gz" \
      || die "Download failed. Check your internet connection."
    mkdir -p "$dir"
    tar -xzf "$tmp/src.tar.gz" -C "$dir" --strip-components 1
    rm -rf "$tmp"
  fi

  cd "$dir"
  # When piped from curl, stdin is this script; give the setup the real terminal
  # so sudo and package managers can prompt.
  local tty=/dev/null
  if [ -r /dev/tty ] && (: < /dev/tty) 2>/dev/null; then tty=/dev/tty; fi
  if [ "$os" = Darwin ]; then
    bash ./setup_macos.sh "$@" < "$tty"
  else
    bash ./setup.sh "$@" < "$tty"
  fi

  echo
  say "Installed in $dir"
  say "Update any time by running the same command again."
}

main "$@"
