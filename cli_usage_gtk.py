#!/usr/bin/env python3
"""cli-usage — GTK/AppIndicator tray frontend (Linux).

One tray indicator per provider (Claude Code, Codex CLI), so both usages are
visible at a glance. Each label is `<color> <tag> <5h>/<weekly>` — the two
windows always in the same order, so the number never "switches" on you.
"""

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

import cairo
import os
import shutil
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from cli_usage_core import fetch_all


def sd_notify(state):
    """Send a message to systemd via $NOTIFY_SOCKET (no-op if unset).

    Used for the Type=notify READY signal and the WatchdogSec heartbeat, so
    systemd restarts us if the refresh loop ever stops. Dependency-free.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):  # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass

REFRESH_SECONDS = 60

# (provider name as returned by fetch_all, short tray tag, CLI command)
PROVIDERS = [
    ("Claude Code", "CC", "claude"),
    ("Codex CLI",   "CX", "codex"),
]


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
    return "⚪"


# GNOME Shell renders the tray ICON reliably but ignores the AppIndicator text
# label, so we draw the number INTO the icon instead of setting a label.
ICON_DIR = Path.home() / ".cache" / "cli-usage-icons"
STATUS_RGB = {
    "healthy":  (0.13, 0.77, 0.37),
    "warning":  (0.85, 0.47, 0.02),
    "critical": (0.94, 0.27, 0.27),
    "unknown":  (0.58, 0.64, 0.72),
}


def render_status_icon(tag, text, state):
    """Render `<tag> <n>` (e.g. "CC 34") as a colored PNG tray icon.

    Returns (theme_dir, icon_name) for AppIndicator.set_icon_theme_path +
    set_icon_full. The name encodes the content so GNOME reloads on change.
    """
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    slug = text.replace(" ", "_").replace("/", "-").replace("%", "p").replace("?", "q")
    name = f"cliusage-{state}-{slug}"
    path = ICON_DIR / f"{name}.png"
    if not path.exists():
        height, font = 44, 30
        measure = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8))
        measure.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        measure.set_font_size(font)
        xb, yb, tw, th, _, _ = measure.text_extents(text)
        width = int(tw + 14)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(font)
        cr.set_source_rgb(*STATUS_RGB.get(state, STATUS_RGB["unknown"]))
        cr.move_to(7 - xb, (height - th) / 2 - yb)
        cr.show_text(text)
        surface.write_to_png(str(path))
    return str(ICON_DIR), name


def _pct(remaining):
    """A remaining-percent as a short integer string."""
    return str(int(round(remaining)))


def tray_label(tag, info):
    """Build the compact tray label for one provider.

    Both windows present → "<color> <tag> <5h>/<weekly>" (e.g. "🟢 CC 94/71").
    Only one window      → "<color> <tag> <n>%"          (e.g. "🟢 CX 85%").
    No data / uninstalled → "⚪ <tag>".

    Kept to ASCII digits + one emoji: the GNOME panel label renderer would
    drop the whole CX label when it contained an en dash for the missing 5h
    window, showing only the icon.
    """
    if not info.get("installed"):
        return f"⚪ {tag}"
    summary = info.get("summary") or {}
    five, week = summary.get("5h"), summary.get("weekly")
    present = [v for v in (five, week) if v is not None]
    if not present:
        return f"⚪ {tag}"
    worst = min(present)
    if five is not None and week is not None:
        return f"{usage_prefix(worst)} {tag} {_pct(five)}/{_pct(week)}"
    return f"{usage_prefix(worst)} {tag} {_pct(present[0])}%"


def worst_of(info):
    """Lowest remaining across a provider's 5h/weekly windows (for icon color)."""
    summary = info.get("summary") or {}
    present = [v for v in (summary.get("5h"), summary.get("weekly")) if v is not None]
    return min(present) if present else None


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
    if stripped.startswith("⚠"):
        return f'<span foreground="#f59e0b" weight="bold">{safe}</span>'
    if "usage unavailable" in stripped or "no auth" in stripped or "not installed" in stripped:
        return f'<span foreground="#94a3b8">{safe}</span>'
    if stripped.startswith("●"):
        return f'<span foreground="#38bdf8" weight="bold">{safe}</span>'
    if stripped.startswith("○"):
        return f'<span foreground="#64748b">{safe}</span>'
    if "Account" in stripped or "Auth" in stripped or "Tier" in stripped or "Credits" in stripped:
        return f'<span foreground="#a78bfa">{safe}</span>'
    return safe


class ProviderIndicator:
    """One tray icon for a single provider: its own label, icon, and menu."""

    def __init__(self, name, tag, cmd, on_refresh, on_quit):
        self.name = name
        self.tag = tag
        self.cmd = cmd
        self.on_refresh = on_refresh
        self.on_quit = on_quit

        self.indicator = AppIndicator3.Indicator.new(
            f"cli-usage-{tag.lower()}",
            "dialog-information",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_label(f"{tag} …", f"{tag} 100/100")

        # set_menu exports the menu over D-Bus at THIS point, so it must already
        # be non-empty — handing libayatana an empty menu makes GNOME treat the
        # icon as having no menu, and clicks do nothing. Seed one item first;
        # update() then rebuilds in place (which propagates via dbusmenu).
        self.menu = Gtk.Menu()
        self._s("  …")
        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def update(self, info):
        # Draw the numbers INTO the icon (GNOME renders icons reliably) and
        # clear the text label. GNOME renders the label only intermittently —
        # present now, gone after a few hours — which both duplicated the icon
        # and was the recurring "numbers disappeared" bug.
        summary = info.get("summary") or {}
        five, week = summary.get("5h"), summary.get("weekly")
        present = [v for v in (five, week) if v is not None]
        if not info.get("installed") or not present:
            state, text = "unknown", self.tag
        elif five is not None and week is not None:
            state = usage_state(min(present))
            text = f"{self.tag} {int(round(five))}/{int(round(week))}%"
        else:
            state = usage_state(present[0])
            text = f"{self.tag} {int(round(present[0]))}%"
        theme_dir, name = render_status_icon(self.tag, text, state)
        self.indicator.set_icon_theme_path(theme_dir)
        self.indicator.set_icon_full(name, self.name)
        self.indicator.set_label("", "")

        for c in self.menu.get_children():
            self.menu.remove(c)

        installed = info.get("installed")
        ts = datetime.now().strftime("%H:%M")
        self._s(f"  {'●' if installed else '○'}  {self.name} · {ts}")
        for text, *_ in info.get("rows", []):
            self._s(text)

        self.menu.append(Gtk.SeparatorMenuItem())
        if installed:
            self._action("  ⧉  Open terminal…", lambda: self._open(self.cmd))
        self._action("  ↺  Refresh", self.on_refresh)
        self._action("  ✕  Quit",    self.on_quit)
        self.menu.show_all()

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

    def _open(self, cmd):
        for term in ["gnome-terminal", "xterm", "xfce4-terminal", "konsole"]:
            if shutil.which(term):
                subprocess.Popen([term, "--", "bash", "-c", f"{cmd}; exec bash"])
                return


class AITray:
    def __init__(self):
        self.panels = [
            ProviderIndicator(name, tag, cmd, self._do_refresh_click, Gtk.main_quit)
            for (name, tag, cmd) in PROVIDERS
        ]
        self.do_refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.do_refresh)
        sd_notify("READY=1")

    def do_refresh(self):
        # Never let an exception escape: PyGObject treats a raising timeout
        # callback as "return False", which permanently removes the 60s timer.
        try:
            threading.Thread(target=self._bg_fetch, daemon=True).start()
        except Exception as e:
            print(f"cli-usage: refresh failed: {e}", flush=True)
        return True

    def _bg_fetch(self):
        try:
            data = fetch_all()
        except Exception as e:
            print(f"cli-usage: fetch failed: {e}", flush=True)
            data = {}
        GLib.idle_add(self._rebuild, data)

    def _rebuild(self, data):
        for panel in self.panels:
            panel.update(data.get(panel.name, {}))
        # Heartbeat: reaching here means timer → fetch thread → idle callback
        # all still work. If the loop ever stalls, the pings stop and systemd's
        # WatchdogSec restarts us.
        sd_notify("WATCHDOG=1")

    def _do_refresh_click(self):
        self.do_refresh()


if __name__ == "__main__":
    AITray()
    Gtk.main()
