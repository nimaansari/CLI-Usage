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

import sys
import threading
from datetime import datetime

from cli_usage_core import (claim_single_instance, request_quit, sd_notify, app_dir, fetch_all, open_terminal,
                            provider_summary, worst_remaining_pct)

try:
    from cli_usage_icon import render_icon, usage_state
except ImportError:  # Pillow missing (python3-pil): fall back to theme icons
    render_icon = None
    from cli_usage_core import _status_icon

    def usage_state(pct):
        return {"🔴": "critical", "🟡": "warning", "🟢": "healthy"}.get(_status_icon(pct), "unknown")

REFRESH_SECONDS = 60
PROVIDERS = ("Claude Code", "Codex CLI", "Gemini CLI")
TOOL_CMDS = {"Claude Code": "claude", "Codex CLI": "codex", "Gemini CLI": "gemini"}

ACCENT = "#8b5cf6"
MUTED  = "#94a3b8"
STATE_COLORS = {"healthy": "#22c55e", "warning": "#f59e0b", "critical": "#ef4444", "unknown": MUTED}


def usage_icon_name(pct):
    state = usage_state(pct)
    if state == "critical":
        return "dialog-error"
    if state == "warning":
        return "dialog-warning"
    return "dialog-information"


def markup_for_text(text):
    """Colour a menu row with Pango markup based on its status glyph."""
    safe = html.escape(text)
    stripped = text.strip()
    if stripped.startswith("🟢"):
        return f'<span foreground="{STATE_COLORS["healthy"]}">{safe}</span>'
    if stripped.startswith("🟡"):
        return f'<span foreground="{STATE_COLORS["warning"]}" weight="bold">{safe}</span>'
    if stripped.startswith("🔴"):
        return f'<span foreground="{STATE_COLORS["critical"]}" weight="bold">{safe}</span>'
    if stripped.startswith("⚠") or stripped.startswith("⏳"):
        return f'<span foreground="{STATE_COLORS["warning"]}">{safe}</span>'
    if stripped.startswith("⚪") or "unavailable" in stripped or "not installed" in stripped \
            or "no quota" in stripped:
        return f'<span foreground="{MUTED}" style="italic">{safe}</span>'
    return safe


def _header_markup(name, info):
    installed = info.get("installed")
    summary = provider_summary(info)
    pct = None
    if "% left" in summary:
        pct = int(summary.split("% left")[0].split()[-1])
    dot_color = STATE_COLORS[usage_state(pct)] if installed and pct is not None else (
        ACCENT if installed else MUTED)
    dot = "●" if installed else "○"
    weight = "bold" if installed else "normal"
    name_part = f'<span weight="{weight}">{html.escape(name)}</span>'
    summary_part = f'<span foreground="{MUTED}">{html.escape(summary)}</span>' if summary else ""
    return f'<span foreground="{dot_color}">{dot}</span>  {name_part}{summary_part}'


class AITray:
    def __init__(self):
        self.icon_dir = app_dir() / "icons"
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        self.indicator = AppIndicator3.Indicator.new(
            "cli-usage",
            "dialog-information",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("CLI Usage")
        if render_icon:
            self.indicator.set_icon_theme_path(str(self.icon_dir))
        self._set_icon(None)

        self.menu = Gtk.Menu()
        self._label("CLI Usage  ·  loading…", bold=True)
        self.menu.append(Gtk.SeparatorMenuItem())
        self._action("↻  Refresh now", self.do_refresh)
        self._action("Quit CLI Usage", self._quit)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        self.do_refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)
        # systemd watchdog: pinged from the GTK main loop, so a frozen UI gets restarted
        sd_notify("READY=1")
        GLib.timeout_add_seconds(30, self._watchdog)

    def _watchdog(self):
        sd_notify("WATCHDOG=1")
        return True

    def _set_icon(self, pct):
        desc = f"{pct}% left" if pct is not None else "CLI Usage"
        if render_icon:
            # AppIndicator caches by icon name, so each value gets its own file.
            name = f"cli-usage-{'na' if pct is None else int(pct)}"
            path = self.icon_dir / f"{name}.png"
            try:
                if not path.exists():
                    render_icon(pct, size=64).save(path)
                self.indicator.set_icon_full(name, desc)
                self.indicator.set_label("", "")
                return
            except Exception as e:
                print("icon render failed:", repr(e), flush=True)
        self.indicator.set_icon_full(usage_icon_name(pct), desc)
        self.indicator.set_label(f"{pct}%" if pct is not None else "CLI", "100%")

    def _tick(self):
        self.do_refresh()
        return True

    def do_refresh(self):
        threading.Thread(target=self._bg_fetch, daemon=True).start()

    def _bg_fetch(self):
        try:
            data = fetch_all()
        except Exception as e:
            print("refresh failed:", repr(e), flush=True)
            data = {"_error": str(e)}
        GLib.idle_add(self._rebuild, data)

    def _rebuild(self, data):
        worst = worst_remaining_pct(data)
        self._set_icon(worst)

        for c in self.menu.get_children():
            self.menu.remove(c)

        ts = datetime.now().strftime("%H:%M")
        self._label(f"CLI Usage  ·  updated {ts}", bold=True)
        self.menu.append(Gtk.SeparatorMenuItem())

        # Each CLI is a submenu so the top level stays short at any screen size.
        for name in PROVIDERS:
            self._submenu(name, data.get(name, {}))

        self.menu.append(Gtk.SeparatorMenuItem())
        self._action("↻  Refresh now", self.do_refresh)
        self._action("Quit CLI Usage", self._quit)
        self.menu.show_all()
        return False

    @staticmethod
    def _markup_item(markup, plain):
        item = Gtk.MenuItem(label=plain)
        label = item.get_child()
        if label and hasattr(label, "set_markup"):
            label.set_markup(markup)
        return item

    def _submenu(self, name, info):
        item = self._markup_item(_header_markup(name, info), name + provider_summary(info))
        if not info.get("installed"):
            item.set_sensitive(False)
            self.menu.append(item)
            return
        sub = Gtk.Menu()
        for text, *_ in info.get("rows", []):
            row = self._markup_item(markup_for_text(text), text)
            row.set_sensitive(False)
            sub.append(row)
        sub.append(Gtk.SeparatorMenuItem())
        cmd = TOOL_CMDS[name]
        term = self._markup_item(
            f'<span foreground="{ACCENT}" weight="bold">›  Open {html.escape(cmd)} in a terminal</span>',
            f"Open {cmd} in a terminal")
        term.connect("activate", lambda _: open_terminal(cmd))
        sub.append(term)
        item.set_submenu(sub)
        self.menu.append(item)

    def _quit(self):
        request_quit()
        Gtk.main_quit()

    def _label(self, text, bold=False):
        markup = f"<b>{html.escape(text)}</b>" if bold else html.escape(text)
        item = self._markup_item(markup, text)
        item.set_sensitive(False)
        self.menu.append(item)

    def _action(self, text, fn):
        item = Gtk.MenuItem(label=text)
        item.connect("activate", lambda _: fn())
        self.menu.append(item)


if __name__ == "__main__":
    if not claim_single_instance():
        print("cli-usage is already running.", flush=True)
        sys.exit(0)
    AITray()
    Gtk.main()
