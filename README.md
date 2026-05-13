# cli-usage

A lightweight tray/menu-bar indicator for monitoring AI CLI usage and rate limits across:

- **Claude Code**
- **Codex CLI**
- **Gemini CLI**

It shows installed tools, account/auth status, remaining rate-limit windows, reset times, and quick shortcuts to open each CLI in a terminal.

> **Note:** This project was vibe coded — built quickly with AI-assisted flow, practical first, polished enough to ship.

## Why this exists

If you use multiple AI CLIs, it is easy to lose track of which account is close to its limit. `cli-usage` keeps a small always-visible `CLI` indicator in your system tray/menu bar and refreshes usage data automatically.

## Features

- Cross-platform tray frontend for **macOS**, **Windows**, and **Linux** using `pystray`
- Native GTK/AppIndicator frontend for Linux desktops that support AppIndicator
- Shows Claude Code 5-hour and weekly usage windows when authenticated
- Shows Codex CLI 5-hour and weekly usage windows when authenticated
- Detects Gemini CLI credentials and reports auth status
- Auto-refreshes every 60 seconds
- One-click terminal launcher for installed CLIs
- Login/startup installer scripts for Linux, macOS, and Windows

## Screens / menu contents

The tray menu includes sections like:

```text
cli-usage · 12:34

● Claude Code
  Account    user@example.com (Max)
  5h limit      [██████░░░░░░] 50% left  (resets 14:00)
  Weekly limit  [████████░░░░] 70% left  (resets 09:00 on 20 May)

● Codex CLI
  Account    user@example.com (Plus)
  5h limit      [██████████░░] 85% left

○ Gemini CLI
  not installed
```

## Requirements

### Common

- Python 3.9+
- The CLI tools you want to monitor installed and authenticated:
  - `claude`
  - `codex`
  - `gemini`

### macOS / Windows / generic Linux frontend

```bash
python3 -m pip install --user pystray Pillow
```

### Linux GTK/AppIndicator frontend

Debian/Ubuntu-style systems:

```bash
sudo apt-get install -y \
  gir1.2-ayatanaappindicator3-0.1 \
  gnome-shell-extension-appindicator \
  python3-gi \
  python3-gi-cairo
```

## Install and run

Clone the repository:

```bash
git clone https://github.com/nimaansari/CLI-Usage.git
cd CLI-Usage
```

### Linux

For GNOME/AppIndicator integration:

```bash
chmod +x setup.sh
./setup.sh
```

Or run manually:

```bash
python3 cli_usage_gtk.py
```

For the cross-platform pystray frontend:

```bash
python3 -m pip install --user pystray Pillow
python3 cli_usage_xplat.py
```

### macOS

```bash
chmod +x setup_macos.sh
./setup_macos.sh
```

Manual run:

```bash
python3 -m pip install --user pystray Pillow
python3 cli_usage_xplat.py
```

### Windows

From PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

Manual run:

```powershell
python -m pip install --user pystray Pillow
python .\cli_usage_xplat.py
```

## Uninstall

### Linux

Remove the autostart file and stop the process:

```bash
rm -f ~/.config/autostart/cli-usage.desktop
pkill -f cli_usage_gtk.py || true
```

### macOS

```bash
launchctl unload ~/Library/LaunchAgents/com.user.cli-usage.plist
rm -f ~/Library/LaunchAgents/com.user.cli-usage.plist
pkill -f cli_usage_xplat.py || true
```

### Windows

Remove the startup shortcut:

```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\cli-usage.lnk"
```

Then quit the tray app from the menu or stop the Python process.

## How it works

`cli_usage_core.py` contains the shared data layer. It checks whether each CLI executable exists, reads local auth/account metadata, and calls official usage endpoints when available:

- Claude Code: Anthropic OAuth usage endpoint
- Codex CLI: ChatGPT Codex usage endpoint
- Gemini CLI: local credential detection only, because there is no public usage endpoint exposed here

Frontends:

- `cli_usage_gtk.py` — Linux GTK/AppIndicator tray frontend
- `cli_usage_xplat.py` — pystray frontend for macOS, Windows, and Linux

## Privacy and security note

This app reads local CLI credential files only to discover the current account and request usage data from the relevant first-party service. It does **not** store tokens, print tokens, or send them anywhere other than the official usage endpoints used by the corresponding CLI provider.

Still, treat this like any local tool that can read CLI auth files: review the code before running it on a machine with sensitive credentials.

## Troubleshooting

### The tray icon does not appear on Linux

- Make sure AppIndicator support is installed and enabled.
- On GNOME/Wayland, log out and back in after installing the extension.
- Check logs:

```bash
tail -f /tmp/cli-usage.log
```

### Usage says unavailable

Common causes:

- The CLI is not authenticated.
- The provider changed an internal usage endpoint.
- Network access is blocked.
- The auth file format changed in a new CLI release.

### Gemini usage is unavailable

This is expected. The app currently detects Gemini auth status, but does not show live Gemini usage because there is no stable public endpoint wired into this project.

## Project status

Small, practical utility. The core architecture is intentionally simple:

```text
cli_usage_core.py      # shared usage/auth detection
cli_usage_gtk.py       # Linux AppIndicator UI
cli_usage_xplat.py     # pystray cross-platform UI
setup*.sh/ps1          # platform startup installers
```

## License

MIT License. See [LICENSE](LICENSE).
