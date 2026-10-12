#!/usr/bin/env python3
"""Unified installer for cli-usage.

Safe defaults:
- installs Python deps only when needed
- creates per-user startup entries
- can run in --dry-run / --no-autostart / --no-launch modes for testing
"""

from __future__ import annotations

import argparse
import os
try:
    import pwd
except ImportError:  # Windows
    pwd = None
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "cli-usage"
ROOT = Path(__file__).resolve().parent
REQS = ROOT / "requirements.txt"

def _log_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "cli-usage"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "cli-usage"
    return base / "cli-usage.log"


LOG_PATH = _log_path()
TOOLS = ("claude", "codex", "gemini", "ccusage-codex")


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], *, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess | None:
    pretty = " ".join(str(c) for c in cmd)
    log(f"$ {pretty}")
    if dry_run:
        return None
    return subprocess.run(cmd, check=check)


def check_python() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit(f"Python 3.9+ is required (this is {platform.python_version()}).")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def module_exists(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def gtk_available() -> bool:
    try:
        import gi  # type: ignore

        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
        except ValueError:
            gi.require_version("AppIndicator3", "0.1")
        return True
    except Exception:
        return False


def choose_frontend(requested: str) -> str:
    if requested != "auto":
        return requested
    if sys.platform.startswith("linux") and gtk_available():
        return "gtk"
    return "xplat"


def script_for_frontend(frontend: str) -> Path:
    return ROOT / ("cli_usage_gtk.py" if frontend == "gtk" else "cli_usage_xplat.py")


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_pythonw(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "pythonw.exe"
    return venv_python(venv)


def install_python_deps(frontend: str, *, dry_run: bool, skip_deps: bool, venv: Path) -> Path:
    """Install deps and return the Python executable that should run the app."""
    if frontend == "gtk":
        log("GTK frontend selected; Python GUI deps are provided by system packages on Linux.")
        return Path(sys.executable)

    py = venv_python(venv)
    if skip_deps:
        log("Skipping Python dependency installation.")
        return py if py.exists() else Path(sys.executable)

    if not py.exists():
        log(f"Creating virtual environment: {venv}")
        cmd = [sys.executable, "-m", "venv", str(venv)]
        if sys.platform.startswith("linux"):
            # Lets pystray reach the system's PyGObject/AppIndicator, so the icon
            # shows on GNOME/KDE instead of falling back to the bare X11 backend.
            cmd.insert(3, "--system-site-packages")
        try:
            run(cmd, dry_run=dry_run)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(venv, ignore_errors=True)   # half-made venv breaks the next run
            ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            raise SystemExit(
                "Could not create a virtual environment. On Debian/Ubuntu install the venv module first:\n"
                f"  sudo apt-get install -y python3-venv   (or python{ver}-venv)"
            ) from exc

    try:
        run([str(py), "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "pip"],
            dry_run=dry_run, check=False)
        run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQS)], dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "Installing Python packages failed (see the pip output above).\n"
            "Check your internet connection, then re-run the installer."
        ) from exc
    return py


def install_linux_system_deps(*, dry_run: bool, install_system_deps: bool) -> None:
    if not sys.platform.startswith("linux"):
        return
    if gtk_available():
        return
    if not install_system_deps:
        log("Linux GTK/AppIndicator deps are not installed.")
        log("Either run: ./setup.sh   (installs them with your package manager)")
        log("Or use the cross-platform frontend: python3 install.py --frontend xplat")
        return
    gnome = "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    pm = next((m for m in LINUX_PACKAGES if command_exists(m)), None)
    if not pm:
        log("Could not find apt-get, dnf, pacman or zypper; install PyGObject + AyatanaAppIndicator3 manually.")
        return
    pkgs = list(LINUX_PACKAGES[pm]["base"]) + (list(LINUX_PACKAGES[pm]["gnome"]) if gnome else [])
    sudo = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
    log(f"Installing tray libraries with {pm} (sudo may ask for your password)...")
    try:
        if pm == "apt-get":
            run(sudo + ["apt-get", "update"], dry_run=dry_run, check=False)
        run(sudo + LINUX_PACKAGES[pm]["install"] + pkgs, dry_run=dry_run)
    except (subprocess.CalledProcessError, FileNotFoundError):
        log("System package install failed; falling back to the cross-platform frontend.")


LINUX_PACKAGES = {
    "apt-get": {
        "install": ["apt-get", "install", "-y"],
        "base": ["python3-gi", "python3-gi-cairo", "gir1.2-ayatanaappindicator3-0.1", "python3-pil"],
        "gnome": ["gnome-shell-extension-appindicator"],
    },
    "dnf": {
        "install": ["dnf", "install", "-y"],
        "base": ["python3-gobject", "libayatana-appindicator-gtk3", "python3-pillow"],
        "gnome": ["gnome-shell-extension-appindicator"],
    },
    "pacman": {
        "install": ["pacman", "-S", "--needed", "--noconfirm"],
        "base": ["python-gobject", "libayatana-appindicator", "python-pillow"],
        "gnome": ["gnome-shell-extension-appindicator"],
    },
    "zypper": {
        "install": ["zypper", "--non-interactive", "install"],
        "base": ["python3-gobject", "python3-gobject-Gdk", "typelib-1_0-AyatanaAppIndicator3-0_1", "python3-Pillow"],
        "gnome": ["gnome-shell-extension-appindicator"],
    },
}


def enable_gnome_extension(*, dry_run: bool) -> None:
    if not sys.platform.startswith("linux") or not command_exists("gnome-extensions"):
        return
    # Ubuntu ships its own build under a different UUID than upstream.
    uuids = ("ubuntu-appindicators@ubuntu.com", "appindicatorsupport@rgcjonas.gmail.com")
    listed = subprocess.run(["gnome-extensions", "list"], capture_output=True, text=True).stdout.split()
    present = [u for u in uuids if u in listed]
    if not present:
        log("GNOME AppIndicator extension not found; the tray icon needs it on GNOME.")
        log("Install it with your package manager (gnome-shell-extension-appindicator) and log out/in.")
        return
    run(["gnome-extensions", "enable", present[0]], dry_run=dry_run, check=False)


def session_path_env() -> str:
    """PATH for login-started copies. Desktop sessions and launchd often lack
    ~/.local/bin, nvm and Homebrew, so claude/codex/gemini looked "not installed"."""
    dirs = []
    for tool in TOOLS:
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    dirs += [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and "/.venv" not in d]
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(d for d in dict.fromkeys(dirs) if Path(d).is_dir())


def _desktop_quote(arg) -> str:
    """Quote one argument for a .desktop Exec= line (freedesktop spec)."""
    arg = str(arg)
    for ch in ("\\", '"', "`", "$"):
        arg = arg.replace(ch, "\\" + ch)
    return '"' + arg.replace("%", "%%") + '"'


def _linux_icon(*, dry_run: bool) -> str:
    """Install the gauge icon into the user icon theme; fall back to a stock icon."""
    dest = Path.home() / ".local" / "share" / "icons" / "hicolor" / "128x128" / "apps" / "cli-usage.png"
    try:
        sys.path.insert(0, str(ROOT))
        from cli_usage_icon import render_icon
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            render_icon(None, size=128).save(dest)
        return "cli-usage"
    except Exception:
        return "utilities-system-monitor"


def install_linux_autostart(script: Path, python_bin: Path, *, dry_run: bool) -> Path:
    autostart = Path.home() / ".config" / "autostart"
    desktop = autostart / "cli-usage.desktop"
    exec_line = " ".join(_desktop_quote(a) for a in ("env", f"PATH={session_path_env()}", python_bin, script))
    content = f"""[Desktop Entry]
Type=Application
Name=CLI Usage
Comment=Tray indicator showing rate-limit usage for Claude Code, Codex CLI, and Gemini CLI
Exec={exec_line}
Icon={_linux_icon(dry_run=dry_run)}
Terminal=false
Categories=Utility;Monitor;
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
"""
    log(f"Writing Linux autostart entry: {desktop}")
    # Same entry in the app menu, so it can be started again after Quit.
    launcher = Path.home() / ".local" / "share" / "applications" / "cli-usage.desktop"
    if not dry_run:
        autostart.mkdir(parents=True, exist_ok=True)
        old = autostart / "ai-cli-tray.desktop"
        old.unlink(missing_ok=True)
        desktop.write_text(content)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(content.replace("X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\n", ""))
    return desktop


MACOS_LABEL = "com.user.cli-usage"


def macos_app_path() -> Path:
    return Path.home() / "Applications" / "CLI Usage.app"


def is_real_home() -> bool:
    """False when tests point Path.home() at a temp dir; never touch live jobs then."""
    if pwd is None or not hasattr(os, "getuid"):
        return True
    return Path.home() == Path(pwd.getpwuid(os.getuid()).pw_dir)
MACOS_BUNDLE_ID = "ca.agiam.cli-usage"

LAUNCHER_C = r"""#include <Python.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    setenv("PYTHONPATH", "%(pythonpath)s", 1);
    char *args[] = { argv[0], "%(script)s", NULL };
    return Py_BytesMain(2, args);
}
"""

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>CLI Usage</string>
  <key>CFBundleDisplayName</key><string>CLI Usage</string>
  <key>CFBundleIdentifier</key><string>%s</string>
  <key>CFBundleExecutable</key><string>cli-usage</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
""" % MACOS_BUNDLE_ID


def build_macos_app(script: Path, python_bin: Path, *, dry_run: bool) -> Path | None:
    """Wrap the tray in "CLI Usage.app" so macOS gives it its own menu-bar identity.

    Run as a bare script, the process is just "Python" (com.apple.python3), which
    macOS may hide from the menu bar. The bundle's executable is a tiny C launcher
    that embeds the venv's framework Python and runs the tray script.
    Returns the launcher path, or None to fall back to python + script.
    """
    if not command_exists("clang") or not command_exists("codesign"):
        log("clang/codesign not found (install Xcode Command Line Tools); skipping CLI Usage.app")
        return None
    probe = subprocess.run(
        [str(python_bin), "-c",
         "import sys, sysconfig, site; print(sys.base_prefix); "
         "print(sysconfig.get_paths()['include']); print(site.getsitepackages()[0])"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        log(f"Could not inspect {python_bin}; skipping CLI Usage.app")
        return None
    base_prefix, include, site_packages = probe.stdout.split("\n")[:3]
    # Framework builds live at .../<Name>.framework/Versions/<X.Y>
    fw_dir = Path(base_prefix).parent.parent
    if fw_dir.suffix != ".framework":
        log(f"{python_bin} is not a framework Python; skipping CLI Usage.app")
        return None

    app = macos_app_path()
    exe = app / "Contents" / "MacOS" / "cli-usage"
    log(f"Building macOS app bundle: {app}")
    if dry_run:
        return exe
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(INFO_PLIST)
    src = app / "Contents" / "Resources" / "launcher.c"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(LAUNCHER_C % {"pythonpath": f"{site_packages}:{script.parent}", "script": str(script)})
    run(["clang", "-O2", "-o", str(exe), str(src), f"-I{include}",
         f"-F{fw_dir.parent}", "-framework", fw_dir.stem, f"-Wl,-rpath,{fw_dir.parent}"])
    run(["codesign", "--force", "-s", "-", str(app)])
    return exe


def macos_path_env() -> str:
    return session_path_env()


def install_macos_autostart(script: Path, python_bin: Path, *, dry_run: bool,
                            program: Path | None = None) -> Path:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist = plist_dir / f"{MACOS_LABEL}.plist"
    args = [program] if program else [python_bin, script]
    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)
    content = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key><string>{MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{macos_path_env()}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <!-- restart after a crash, but stay quit when the user picks Quit -->
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>ProcessType</key><string>Interactive</string>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
    <key>StandardOutPath</key><string>{LOG_PATH}</string>
    <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict>
</plist>
"""
    log(f"Writing macOS LaunchAgent: {plist}")
    if not dry_run:
        plist_dir.mkdir(parents=True, exist_ok=True)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        old = plist_dir / "com.user.ai-cli-tray.plist"
        if old.exists():
            run(["launchctl", "unload", str(old)], check=False)
            old.unlink()
        plist.write_text(content)
        # Only (re)load the real user's agent; tests point HOME at a temp dir and
        # must not replace the live job that shares this label.
        if sys.platform == "darwin" and command_exists("launchctl") and is_real_home():
            domain = f"gui/{os.getuid()}"
            run(["launchctl", "bootout", f"{domain}/{MACOS_LABEL}"], check=False)
            # bootout is asynchronous; bootstrapping before the old job is gone fails with EIO.
            for _ in range(20):
                gone = subprocess.run(["launchctl", "print", f"{domain}/{MACOS_LABEL}"],
                                      capture_output=True).returncode != 0
                if gone:
                    break
                time.sleep(0.5)
            for attempt in range(3):
                result = run(["launchctl", "bootstrap", domain, str(plist)], check=False)
                if result is None or result.returncode == 0:
                    break
                time.sleep(2)
    return plist


def _windows_programs_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _windows_shortcut(lnk: Path, target: Path, args: str, workdir: Path) -> bool:
    """Create a .lnk with PowerShell's WScript.Shell; returns False if that fails."""
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return False
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + "); "
        "$s.TargetPath = " + _ps_quote(target) + "; "
        "$s.Arguments = " + _ps_quote(args) + "; "
        "$s.WorkingDirectory = " + _ps_quote(workdir) + "; "
        "$s.Description = 'CLI Usage tray'; $s.Save()"
    )
    result = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True)
    return result.returncode == 0 and lnk.exists()


def install_windows_autostart(script: Path, python_bin: Path, *, dry_run: bool) -> Path:
    programs = _windows_programs_dir()
    startup = programs / "Startup"
    lnk = startup / "CLI Usage.lnk"
    cmd_file = startup / "cli-usage.cmd"
    log(f"Writing Windows startup shortcut: {lnk}")
    if dry_run:
        return lnk
    startup.mkdir(parents=True, exist_ok=True)
    (startup / "AI CLI Tray.lnk").unlink(missing_ok=True)
    args = f'"{script}"'
    # A shortcut to pythonw.exe starts silently; a .cmd flashes a console window at login.
    if _windows_shortcut(lnk, python_bin, args, script.parent):
        cmd_file.unlink(missing_ok=True)
        # Also add it to the Start menu so it can be started again after Quit.
        _windows_shortcut(programs / "CLI Usage.lnk", python_bin, args, script.parent)
        return lnk
    log("Could not create a shortcut; using a startup .cmd file instead.")
    cmd_file.write_text(f'@echo off\r\nstart "" "{python_bin}" "{script}"\r\n')
    return cmd_file


def install_autostart(script: Path, python_bin: Path, *, dry_run: bool) -> Path | None:
    if sys.platform == "darwin":
        return install_macos_autostart(script, python_bin, dry_run=dry_run)
    if os.name == "nt":
        return install_windows_autostart(script, python_bin, dry_run=dry_run)
    if sys.platform.startswith("linux"):
        return install_linux_autostart(script, python_bin, dry_run=dry_run)
    log(f"Autostart is not implemented for this platform: {platform.platform()}")
    return None


SYSTEMD_UNIT = "cli-usage.service"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def systemd_user_available() -> bool:
    """True when a systemd user manager runs the graphical session (GNOME, KDE, ...)."""
    if not sys.platform.startswith("linux") or not command_exists("systemctl") or not is_real_home():
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "graphical-session.target"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _systemd_quote(arg) -> str:
    return '"' + str(arg).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install_linux_service(script: Path, python_bin: Path, *, dry_run: bool) -> Path:
    """systemd user service: starts with the desktop session and restarts the tray
    if it ever stops (crash, killed, session cleanup). The tray's Quit item stops
    the service, so Quit still means quit until the next login."""
    unit = systemd_unit_path()
    content = f"""[Unit]
Description=CLI Usage tray (Claude Code / Codex / Gemini limits)
PartOf=graphical-session.target
After=graphical-session.target
# Never give up restarting.
StartLimitIntervalSec=0

[Service]
ExecStart={_systemd_quote(python_bin)} {_systemd_quote(script)}
Environment={_systemd_quote("PATH=" + session_path_env())}
Environment=CLI_USAGE_SERVICE=1
WorkingDirectory={script.parent}
Restart=always
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""
    log(f"Writing systemd user service: {unit}")
    if dry_run:
        return unit
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    # The service replaces the one-shot autostart entry; keep only the app-menu launcher.
    (Path.home() / ".config" / "autostart" / "cli-usage.desktop").unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"], check=False)
    run(["systemctl", "--user", "enable", SYSTEMD_UNIT], check=False)
    return unit


def _write_linux_menu_launcher(*, dry_run: bool) -> None:
    """App-menu entry that (re)starts the service after Quit."""
    launcher = Path.home() / ".local" / "share" / "applications" / "cli-usage.desktop"
    content = f"""[Desktop Entry]
Type=Application
Name=CLI Usage
Comment=Tray indicator showing rate-limit usage for Claude Code, Codex CLI, and Gemini CLI
Exec=systemctl --user restart {SYSTEMD_UNIT}
Icon={_linux_icon(dry_run=dry_run)}
Terminal=false
Categories=Utility;Monitor;
StartupNotify=false
"""
    if not dry_run:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(content)


def stop_running(*, dry_run: bool) -> None:
    """Stop an already-running tray so the freshly installed code is what runs."""
    if dry_run:
        return
    if systemd_unit_path().exists() and command_exists("systemctl") and is_real_home():
        # Otherwise systemd would restart the old copy as soon as it is killed.
        subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT], check=False, capture_output=True)
    try:
        sys.path.insert(0, str(ROOT))
        from cli_usage_core import stop_running_instance
        if stop_running_instance():
            log("Stopped the running copy of cli-usage.")
        elif os.name != "nt" and command_exists("pkill"):
            # Copies started by older versions hold no lock file; match this checkout's scripts.
            for name in ("cli_usage_gtk.py", "cli_usage_xplat.py"):
                subprocess.run(["pkill", "-f", str(ROOT / name)], check=False)
    except Exception as exc:
        log(f"Could not stop a running copy ({exc}); you may need to quit it from the tray.")


def launch_app(script: Path, python_bin: Path, *, dry_run: bool) -> None:
    log(f"Launching {APP_NAME} with {script.name}...")
    if dry_run:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # Own session: closing the terminal that ran the installer must not kill the tray.
        kwargs["start_new_session"] = True
    with open(LOG_PATH, "a") as logf:
        subprocess.Popen([str(python_bin), str(script)], stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT, cwd=str(ROOT), **kwargs)


def uninstall(*, dry_run: bool) -> int:
    """Remove startup entries, launchers and the macOS app; keeps the repo and .venv."""
    stop_running(dry_run=dry_run)
    home = Path.home()
    paths = [
        home / ".config" / "autostart" / "cli-usage.desktop",
        home / ".local" / "share" / "applications" / "cli-usage.desktop",
        home / ".local" / "share" / "icons" / "hicolor" / "128x128" / "apps" / "cli-usage.png",
        home / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist",
    ]
    if os.name == "nt":
        programs = _windows_programs_dir()
        paths += [programs / "Startup" / "CLI Usage.lnk", programs / "Startup" / "cli-usage.cmd",
                  programs / "CLI Usage.lnk"]
    unit = systemd_unit_path()
    if unit.exists():
        if command_exists("systemctl") and is_real_home():
            run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], dry_run=dry_run, check=False)
        log(f"Removing {unit}")
        if not dry_run:
            unit.unlink()
            if command_exists("systemctl") and is_real_home():
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if sys.platform == "darwin" and command_exists("launchctl") and is_real_home():
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{MACOS_LABEL}"], dry_run=dry_run, check=False)
    for path in paths:
        if path.exists():
            log(f"Removing {path}")
            if not dry_run:
                path.unlink()
    app = macos_app_path()
    if app.exists():
        log(f"Removing {app}")
        if not dry_run:
            shutil.rmtree(app)
    if not dry_run:
        log("cli-usage was uninstalled. Delete this folder to remove the code and its .venv too.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install cli-usage for the current user.")
    parser.add_argument("--frontend", choices=["auto", "gtk", "xplat"], default="auto", help="Tray frontend to install/run. Default: auto")
    parser.add_argument("--install-system-deps", action="store_true", help="On Linux, install GTK/AppIndicator packages with apt-get when needed.")
    parser.add_argument("--skip-deps", action="store_true", help="Do not install Python dependencies.")
    parser.add_argument("--venv", default=str(ROOT / ".venv"), help="Virtualenv path for the cross-platform frontend. Default: ./.venv")
    parser.add_argument("--no-autostart", action="store_true", help="Do not create startup/login entries.")
    parser.add_argument("--no-launch", action="store_true", help="Do not launch the tray app after install.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files or installing packages.")
    parser.add_argument("--uninstall", action="store_true", help="Stop the tray and remove its startup entries.")
    args = parser.parse_args(argv)

    if args.uninstall:
        return uninstall(dry_run=args.dry_run)
    check_python()
    if args.frontend != "xplat":
        install_linux_system_deps(dry_run=args.dry_run, install_system_deps=args.install_system_deps)
    frontend = choose_frontend(args.frontend)
    script = script_for_frontend(frontend)
    if not script.exists():
        raise SystemExit(f"Missing frontend script: {script}")

    log("=== cli-usage installer ===")
    log(f"Platform: {platform.system()} {platform.release()}")
    log(f"Frontend: {frontend} ({script.name})")

    python_bin = install_python_deps(frontend, dry_run=args.dry_run, skip_deps=args.skip_deps, venv=Path(args.venv).expanduser().resolve())
    if os.name == "nt" and frontend == "xplat":
        candidate = venv_pythonw(Path(args.venv).expanduser().resolve())
        if candidate.exists():
            python_bin = candidate
    if frontend == "gtk":
        enable_gnome_extension(dry_run=args.dry_run)
    elif sys.platform.startswith("linux") and not args.dry_run and not gtk_available():
        log("Note: without AppIndicator support the icon may not show on GNOME. Run ./setup.sh to install it.")

    if not args.no_launch:
        # Before touching autostart, so the new LaunchAgent/startup entry isn't racing the old copy.
        stop_running(dry_run=args.dry_run)

    entry = None
    if not args.no_autostart:
        if sys.platform == "darwin" and frontend == "xplat":
            program = build_macos_app(script, python_bin, dry_run=args.dry_run)
            entry = install_macos_autostart(script, python_bin, dry_run=args.dry_run, program=program)
        elif systemd_user_available():
            entry = install_linux_service(script, python_bin, dry_run=args.dry_run)
            _write_linux_menu_launcher(dry_run=args.dry_run)
        else:
            entry = install_autostart(script, python_bin, dry_run=args.dry_run)

    if not args.no_launch:
        if entry is not None and entry.name == SYSTEMD_UNIT:
            log("Starting the cli-usage service...")
            result = run(["systemctl", "--user", "restart", SYSTEMD_UNIT], dry_run=args.dry_run, check=False)
            if result is not None and result.returncode != 0:
                log("The service did not start; using a login autostart entry instead.")
                subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT], check=False)
                systemd_unit_path().unlink(missing_ok=True)
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
                entry = install_linux_autostart(script, python_bin, dry_run=args.dry_run)
                launch_app(script, python_bin, dry_run=args.dry_run)
        elif not (sys.platform == "darwin" and entry):
            # On macOS the LaunchAgent (RunAtLoad) already started it.
            launch_app(script, python_bin, dry_run=args.dry_run)

    log("")
    log("Done. Look for the ring icon in your tray / menu bar.")
    if entry:
        log(f"Autostart entry: {entry}")
    log(f"Log file: {LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
