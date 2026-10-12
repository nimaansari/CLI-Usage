#!/usr/bin/env python3
"""cli-usage — cross-platform tray (macOS, Windows, Linux).

Backend: pystray + Pillow. Same data layer as the GTK frontend (cli_usage_core).

Install:
    pip install pystray Pillow
Run:
    python cli_usage_xplat.py
"""

import sys
import threading
from datetime import datetime

import pystray
from pystray import MenuItem as Item, Menu

from cli_usage_core import (acquire_single_instance, fetch_all, open_terminal,
                            provider_summary, worst_remaining_pct)
from cli_usage_icon import render_icon

REFRESH_SECONDS = 60
PROVIDERS = ("Claude Code", "Codex CLI", "Gemini CLI")
TOOL_CMDS = {"Claude Code": "claude", "Codex CLI": "codex", "Gemini CLI": "gemini"}

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


def _icon_image(pct=None):
    # macOS: monochrome template image, tinted by the menu bar for light/dark mode.
    return render_icon(pct, template=IS_MAC)


# ── tray ────────────────────────────────────────────────────────────────────

def _terminal_action(cmd):
    # pystray rejects callbacks with more than two parameters (even defaulted ones).
    return lambda _icon, _item: open_terminal(cmd)


def _disabled(text):
    return Item(text, None, enabled=False)


class XPlatTray:
    def __init__(self):
        self.data    = {}
        self.updated = None
        self.icon    = pystray.Icon(
            "cli-usage",
            _icon_image(),
            "CLI Usage",
            menu=Menu(self._menu_items),
        )
        self._stop   = threading.Event()
        if IS_MAC:
            self._use_template_image()

    def _use_template_image(self):
        # The macOS icon is black-on-transparent, invisible on a dark menu bar.
        # Marking the NSImage as a template lets AppKit tint it for light/dark.
        icon = self.icon
        orig = icon._assert_image

        def assert_image():
            orig()
            if icon._icon_image is not None and not icon._icon_image.isTemplate():
                icon._icon_image.setTemplate_(True)
                icon._status_item.button().setImage_(icon._icon_image)

        icon._assert_image = assert_image

    # pystray calls this lazily each time the menu opens.
    def _menu_items(self):
        if self.updated:
            yield _disabled(f"CLI Usage  ·  updated {self.updated}")
        else:
            yield _disabled("CLI Usage  ·  loading…")
        yield Menu.SEPARATOR

        for name in PROVIDERS:
            info = self.data.get(name, {})
            installed = info.get("installed")
            sym  = "●" if installed else "○"
            yield _disabled(f"{sym}  {name}{provider_summary(info)}")
            if installed:
                for text, *_ in info.get("rows", []):
                    yield _disabled(text)
                yield Item(f"      ›  Open {TOOL_CMDS[name]} in a terminal", _terminal_action(TOOL_CMDS[name]))
            yield Menu.SEPARATOR

        yield Item("↻  Refresh now", lambda _i, _it: self.refresh_now())
        yield Item("Quit CLI Usage", lambda _i, _it: self.icon.stop())

    def refresh_now(self):
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        try:
            self.data = fetch_all()
        except Exception as e:
            print("refresh failed:", repr(e), flush=True)
            self.data = {"_error": str(e)}
        self.updated = datetime.now().strftime("%H:%M")
        worst = worst_remaining_pct(self.data)
        # AppKit aborts (SIGTRAP) if status-item UI is touched off the main thread.
        if IS_MAC:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._apply_ui, worst)
        else:
            self._apply_ui(worst)

    def _apply_ui(self, worst):
        self.icon.title = f"CLI Usage — {worst}% left" if worst is not None else "CLI Usage"
        self.icon.icon = _icon_image(worst)
        # Force menu redraw so the lazy items reflect new data.
        try:
            self.icon.update_menu()
        except Exception as e:
            print("update_menu failed:", repr(e), flush=True)

    def _refresh_loop(self):
        while not self._stop.is_set():
            self._refresh()
            self._stop.wait(REFRESH_SECONDS)

    def run(self):
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self.icon.run()
        self._stop.set()


if __name__ == "__main__":
    if not acquire_single_instance():
        print("cli-usage is already running.", flush=True)
        sys.exit(0)
    XPlatTray().run()
