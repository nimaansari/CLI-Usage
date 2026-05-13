#!/usr/bin/env python3
"""cli-usage — cross-platform tray (macOS, Windows, Linux).

Backend: pystray + Pillow. Same data layer as the GTK frontend (cli_usage_core).

Install:
    pip install pystray Pillow
Run:
    python cli_usage_xplat.py
"""

import shutil
import subprocess
import sys
import threading
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as Item, Menu

from cli_usage_core import fetch_all, worst_remaining_pct

REFRESH_SECONDS = 60
TOOL_CMDS = {"Claude Code": "claude", "Codex CLI": "codex", "Gemini CLI": "gemini"}

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


# ── icon ─────────────────────────────────────────────────────────────────────

def _usage_state(pct):
    """Return visual state for remaining usage percentage."""
    if pct is None:
        return "unknown"
    if pct < 10:
        return "critical"
    if pct < 30:
        return "warning"
    return "healthy"


def _state_colors(pct):
    state = _usage_state(pct)
    if state == "critical":
        return (239, 68, 68, 255), (255, 255, 255, 255)   # red
    if state == "warning":
        return (250, 204, 21, 255), (20, 24, 35, 255)     # yellow
    if state == "healthy":
        return (34, 197, 94, 255), (255, 255, 255, 255)   # green
    return (40, 90, 200, 255), (255, 255, 255, 255)       # blue

def _icon_image(pct=None):
    """Generate a small icon. On macOS we render a template-style monochrome
    icon (Apple's menu bar tints it automatically when set_image with a name
    containing 'Template' is used — pystray doesn't expose that, so we just
    use solid black which works on light menubars and is usable on dark)."""
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)

    bg_color, fg_color = _state_colors(pct)

    # Filled rounded square as a backdrop so it's visible on any tray bg.
    if IS_MAC:
        # Keep macOS mostly template-like, but add tiny status bars below.
        fg = (0, 0, 0, 255)
        bg = (0, 0, 0, 0)
    else:
        fg = fg_color
        bg = bg_color

    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=12, fill=bg)

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 28)
    except Exception:
        try:
            font = ImageFont.truetype("arialbd.ttf", 28)
        except Exception:
            font = ImageFont.load_default()

    text = "CLI"
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1] - 2), text, fill=fg, font=font)

    if pct is not None:
        # Tiny usage bar at the bottom: full width means 100% left.
        bar_x0, bar_y0, bar_x1, bar_y1 = 12, 52, 52, 58
        d.rounded_rectangle((bar_x0, bar_y0, bar_x1, bar_y1), radius=3, fill=(255, 255, 255, 80) if not IS_MAC else (0, 0, 0, 45))
        fill_w = int((bar_x1 - bar_x0) * max(0, min(100, pct)) / 100)
        if fill_w > 0:
            fill = bg_color if IS_MAC else fg_color
            d.rounded_rectangle((bar_x0, bar_y0, bar_x0 + fill_w, bar_y1), radius=3, fill=fill)
    return img


# ── terminal launch (platform-specific) ─────────────────────────────────────

def open_terminal(cmd):
    if IS_MAC:
        # Tell Terminal.app to run the command. Escape double-quotes.
        escaped = cmd.replace('"', '\\"')
        script  = f'tell application "Terminal" to do script "{escaped}"'
        subprocess.Popen(["osascript", "-e", script,
                          "-e", 'tell application "Terminal" to activate'])
        return
    if IS_WIN:
        # Use Windows Terminal if available; fall back to cmd.exe.
        if shutil.which("wt.exe"):
            subprocess.Popen(["wt.exe", "-w", "0", "nt", "cmd", "/k", cmd])
        else:
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", cmd], shell=False)
        return
    # Linux fallback.
    for term in ("gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
        if shutil.which(term):
            if term == "gnome-terminal":
                subprocess.Popen([term, "--", "bash", "-c", f"{cmd}; exec bash"])
            else:
                subprocess.Popen([term, "-e", f"bash -c '{cmd}; exec bash'"])
            return


# ── tray ────────────────────────────────────────────────────────────────────

class XPlatTray:
    def __init__(self):
        self.data    = {}
        self.icon    = pystray.Icon(
            "cli-usage",
            _icon_image(),
            "cli-usage",
            menu=Menu(self._menu_items),
        )
        self._stop   = threading.Event()

    # pystray calls this lazily each time the menu opens.
    def _menu_items(self):
        ts = datetime.now().strftime("%H:%M")
        yield Item(f"cli-usage · {ts}", None, enabled=False)
        yield Menu.SEPARATOR

        for name in ("Claude Code", "Codex CLI", "Gemini CLI"):
            info = self.data.get(name, {})
            sym  = "●" if info.get("installed") else "○"
            yield Item(f"{sym}  {name}", None, enabled=False)
            for text, *_ in info.get("rows", []):
                yield Item(text, None, enabled=False)
            if info.get("installed"):
                cmd = TOOL_CMDS[name]
                yield Item("    Open terminal…", lambda _i, _it, c=cmd: open_terminal(c))
            yield Menu.SEPARATOR

        yield Item("Refresh", lambda _i, _it: self.refresh_now())
        yield Item("Quit",    lambda _i, _it: self.icon.stop())

    def refresh_now(self):
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        try:
            self.data = fetch_all()
        except Exception as e:
            self.data = {"_error": str(e)}
        worst = worst_remaining_pct(self.data)
        self.icon.title = f"cli-usage — {worst}% left" if worst is not None else "cli-usage"
        self.icon.icon = _icon_image(worst)
        # Force menu redraw so the lazy items reflect new data.
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _refresh_loop(self):
        while not self._stop.is_set():
            self._refresh()
            self._stop.wait(REFRESH_SECONDS)

    def run(self):
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self.icon.run()
        self._stop.set()


if __name__ == "__main__":
    XPlatTray().run()
