#!/usr/bin/env python3
"""cli-usage — GTK/AppIndicator tray frontend (Linux)."""

import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3

from gi.repository import Gtk, GLib

import shutil
import subprocess
import threading
from datetime import datetime

from cli_usage_core import fetch_all, worst_remaining_pct

REFRESH_SECONDS = 60
TOOL_CMDS = {"Claude Code": "claude", "Codex CLI": "codex", "Gemini CLI": "gemini"}


def usage_state(pct):
    if pct is None:
        return "unknown"
    if pct < 10:
        return "critical"
    if pct < 30:
        return "warning"
    return "healthy"


def usage_icon_name(pct):
    state = usage_state(pct)
    if state == "critical":
        return "dialog-error"
    if state == "warning":
        return "dialog-warning"
    return "dialog-information"


def usage_prefix(pct):
    state = usage_state(pct)
    if state == "critical":
        return "🔴"
    if state == "warning":
        return "🟡"
    if state == "healthy":
        return "🟢"
    return "CLI"


class AITray:
    def __init__(self):
        self.indicator = AppIndicator3.Indicator.new(
            "cli-usage",
            "dialog-information",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label("CLI", "CLI")

        self.menu = Gtk.Menu()
        self._s("cli-usage")
        self.menu.append(Gtk.SeparatorMenuItem())
        self._action("Refresh", self._do_refresh_click)
        self._action("Quit",    lambda: Gtk.main_quit())
        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        self.do_refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.do_refresh)

    def do_refresh(self):
        threading.Thread(target=self._bg_fetch, daemon=True).start()
        return True

    def _bg_fetch(self):
        try:
            data = fetch_all()
        except Exception as e:
            data = {"_error": str(e)}
        GLib.idle_add(self._rebuild, data)

    def _rebuild(self, data):
        worst = worst_remaining_pct(data)
        self.indicator.set_icon_full(usage_icon_name(worst), "cli-usage")
        self.indicator.set_label(f"{usage_prefix(worst)} {worst}%" if worst is not None else "CLI", "CLI 100%")

        for c in self.menu.get_children():
            self.menu.remove(c)

        ts = datetime.now().strftime("%H:%M")
        self._s(f"  cli-usage · {ts}")

        for name in ("Claude Code", "Codex CLI", "Gemini CLI"):
            info = data.get(name, {})
            sym  = "●" if info.get("installed") else "○"
            self.menu.append(Gtk.SeparatorMenuItem())
            self._s(f"  {sym}  {name}")
            for text, *_ in info.get("rows", []):
                self._s(text)
            if info.get("installed"):
                cmd = TOOL_CMDS[name]
                self._action("     Open terminal…", lambda c=cmd: self._open(c))

        self.menu.append(Gtk.SeparatorMenuItem())
        self._action("  ↺  Refresh", self._do_refresh_click)
        self._action("  ✕  Quit",    lambda: Gtk.main_quit())
        self.menu.show_all()

    def _s(self, text):
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        self.menu.append(item)

    def _action(self, text, fn):
        item = Gtk.MenuItem(label=text)
        item.connect("activate", lambda _: fn())
        self.menu.append(item)

    def _do_refresh_click(self):
        self.do_refresh()

    def _open(self, cmd):
        for term in ["gnome-terminal", "xterm", "xfce4-terminal", "konsole"]:
            if shutil.which(term):
                subprocess.Popen([term, "--", "bash", "-c", f"{cmd}; exec bash"])
                return


if __name__ == "__main__":
    AITray()
    Gtk.main()
