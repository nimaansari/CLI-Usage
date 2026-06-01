#!/usr/bin/env python3
"""cli-usage — GTK/AppIndicator tray frontend (Linux)."""

import gi
import html
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


def markup_for_text(text):
    """Linux GTK-only colored menu labels using Pango markup."""
    safe = html.escape(text)
    stripped = text.strip()
    if stripped.startswith("🟢"):
        return f'<span foreground="#22c55e" weight="bold">{safe}</span>'
    if stripped.startswith("🟡"):
        return f'<span foreground="#d97706" weight="bold">{safe}</span>'
    if stripped.startswith("🔴"):
        return f'<span foreground="#ef4444" weight="bold">{safe}</span>'
    if stripped.startswith("⚪"):
        return f'<span foreground="#94a3b8">{safe}</span>'
    if "usage unavailable" in stripped or "no auth" in stripped or "not installed" in stripped:
        return f'<span foreground="#94a3b8">{safe}</span>'
    if stripped.startswith("●"):
        return f'<span foreground="#38bdf8" weight="bold">{safe}</span>'
    if stripped.startswith("○"):
        return f'<span foreground="#64748b">{safe}</span>'
    if "Account" in stripped or "Auth" in stripped or "Tier" in stripped or "Credits" in stripped:
        return f'<span foreground="#a78bfa">{safe}</span>'
    if "cli-usage" in stripped:
        return f'<span foreground="#7dd3fc" weight="bold">{safe}</span>'
    return safe


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

        # Refresh + Quit at the TOP so they stay reachable even when the
        # body grows past the screen (GTK popup menus don't scroll cleanly).
        self._action("  ↺  Refresh", self._do_refresh_click)
        self._action("  ✕  Quit",    lambda: Gtk.main_quit())
        self.menu.append(Gtk.SeparatorMenuItem())

        # Each CLI becomes a submenu so the top-level stays short. Hovering the
        # parent opens the detail rows. Works at any screen size.
        for name in ("Claude Code", "Codex CLI", "Gemini CLI"):
            info = data.get(name, {})
            sym  = "●" if info.get("installed") else "○"
            summary = self._summary_for(name, info)
            self._submenu(f"  {sym}  {name}{summary}", name, info)

        self.menu.show_all()

    def _summary_for(self, name, info):
        """One-line summary appended to the submenu parent label so the
        top-level menu shows the headline at a glance without expanding."""
        if not info.get("installed"):
            return "  — not installed"
        for text, *_ in info.get("rows", []):
            # Lowest "N% left" wins as the headline
            if "% left" in text:
                pct = text.split("% left")[0].split()[-1]
                return f"  · {pct}% left"
        # Otherwise grab the Account line if present
        for text, *_ in info.get("rows", []):
            if "Account" in text:
                # strip leading whitespace and the "Account" key
                val = text.split("Account", 1)[1].strip()
                return f"  · {val[:30]}"
        return ""

    def _submenu(self, parent_label, name, info):
        item = Gtk.MenuItem(label=parent_label)
        if item.get_child() and hasattr(item.get_child(), "set_markup"):
            item.get_child().set_markup(markup_for_text(parent_label))
        sub = Gtk.Menu()
        for text, *_ in info.get("rows", []):
            row = Gtk.MenuItem(label=text)
            if row.get_child() and hasattr(row.get_child(), "set_markup"):
                row.get_child().set_markup(markup_for_text(text))
            row.set_sensitive(False)
            sub.append(row)
        if info.get("installed"):
            cmd = TOOL_CMDS[name]
            term_item = Gtk.MenuItem(label="     Open terminal…")
            if term_item.get_child() and hasattr(term_item.get_child(), "set_markup"):
                term_item.get_child().set_markup(
                    f'<span foreground="#7c3aed" weight="bold">     Open terminal…</span>')
            term_item.connect("activate", lambda _: self._open(cmd))
            sub.append(term_item)
        item.set_submenu(sub)
        self.menu.append(item)

    def _s(self, text):
        item = Gtk.MenuItem(label=text)
        label = item.get_child()
        if label and hasattr(label, "set_markup"):
            label.set_markup(markup_for_text(text))
        item.set_sensitive(False)
        self.menu.append(item)

    def _action(self, text, fn):
        item = Gtk.MenuItem(label=text)
        label = item.get_child()
        if label and hasattr(label, "set_markup"):
            label.set_markup(f'<span foreground="#7c3aed" weight="bold">{html.escape(text)}</span>')
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
