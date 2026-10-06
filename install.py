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
LOG_PATH = Path("/tmp/cli-usage.log") if os.name != "nt" else Path(os.environ.get("TEMP", ".")) / "cli-usage.log"


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
        raise SystemExit("Python 3.9+ is required.")


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
        try:
            run([sys.executable, "-m", "venv", str(venv)], dry_run=dry_run)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                "Could not create a virtual environment. On Debian/Ubuntu, install python3-venv first: "
                "sudo apt-get install -y python3-venv"
            ) from exc

    pip_cmd = [str(py), "-m", "pip", "install", "--upgrade", "pip"]
    run(pip_cmd, dry_run=dry_run)
    run([str(py), "-m", "pip", "install", "-r", str(REQS)], dry_run=dry_run)
    return py


def install_linux_system_deps(*, dry_run: bool, install_system_deps: bool) -> None:
    if not sys.platform.startswith("linux"):
        return
    if gtk_available():
        return
    if not install_system_deps:
        log("Linux GTK/AppIndicator deps are not installed.")
        log("Either run: ./setup.sh --install-system-deps")
        log("Or use the cross-platform frontend: python3 install.py --frontend xplat")
        return
    if not command_exists("apt-get"):
        log("Automatic GTK system dependency install currently supports apt-get only.")
        return
    run([
        "sudo", "apt-get", "install", "-y",
        "gir1.2-ayatanaappindicator3-0.1",
        "gnome-shell-extension-appindicator",
        "python3-gi",
        "python3-gi-cairo",
    ], dry_run=dry_run)


def enable_gnome_extension(*, dry_run: bool) -> None:
    if not sys.platform.startswith("linux") or not command_exists("gnome-extensions"):
        return
    run(["gnome-extensions", "enable", "appindicatorsupport@rgcjonas.gmail.com"], dry_run=dry_run, check=False)


def install_linux_autostart(script: Path, python_bin: Path, *, dry_run: bool) -> Path:
    autostart = Path.home() / ".config" / "autostart"
    desktop = autostart / "cli-usage.desktop"
    content = f"""[Desktop Entry]
Type=Application
Name=cli-usage
Comment=Tray indicator showing rate-limit usage for Claude Code, Codex CLI, and Gemini CLI
Exec={python_bin} {script}
Icon=network-transmit-receive
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
"""
    log(f"Writing Linux autostart entry: {desktop}")
    if not dry_run:
        autostart.mkdir(parents=True, exist_ok=True)
        old = autostart / "ai-cli-tray.desktop"
        old.unlink(missing_ok=True)
        desktop.write_text(content)
    return desktop


MACOS_LABEL = "com.user.cli-usage"
MACOS_APP = Path("~/Applications/CLI Usage.app")
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

    app = MACOS_APP.expanduser()
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
    """PATH for the LaunchAgent: launchd's default lacks ~/.local/bin and Homebrew,
    so claude/codex/gemini would show as "not installed"."""
    dirs = []
    for tool in ("claude", "codex", "gemini", "ccusage-codex"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
             "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


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
    <key>KeepAlive</key><true/>
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
        old = plist_dir / "com.user.ai-cli-tray.plist"
        if old.exists():
            run(["launchctl", "unload", str(old)], check=False)
            old.unlink()
        plist.write_text(content)
        # Only (re)load the real user's agent; tests point HOME at a temp dir and
        # must not replace the live job that shares this label.
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir) if hasattr(os, "getuid") else None
        if sys.platform == "darwin" and command_exists("launchctl") and Path.home() == real_home:
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


def install_windows_autostart(script: Path, python_bin: Path, *, dry_run: bool) -> Path:
    startup = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    cmd_file = startup / "cli-usage.cmd"
    content = f'@echo off\nstart "" "{python_bin}" "{script}"\n'
    log(f"Writing Windows startup command: {cmd_file}")
    if not dry_run:
        startup.mkdir(parents=True, exist_ok=True)
        old = startup / "AI CLI Tray.lnk"
        old.unlink(missing_ok=True)
        cmd_file.write_text(content)
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


def launch_app(script: Path, python_bin: Path, *, dry_run: bool) -> None:
    log(f"Launching {APP_NAME} with {script.name}...")
    if dry_run:
        return
    subprocess.Popen([str(python_bin), str(script)], stdout=open(LOG_PATH, "a"), stderr=subprocess.STDOUT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install cli-usage for the current user.")
    parser.add_argument("--frontend", choices=["auto", "gtk", "xplat"], default="auto", help="Tray frontend to install/run. Default: auto")
    parser.add_argument("--install-system-deps", action="store_true", help="On Linux, install GTK/AppIndicator packages with apt-get when needed.")
    parser.add_argument("--skip-deps", action="store_true", help="Do not install Python dependencies.")
    parser.add_argument("--venv", default=str(ROOT / ".venv"), help="Virtualenv path for the cross-platform frontend. Default: ./.venv")
    parser.add_argument("--no-autostart", action="store_true", help="Do not create startup/login entries.")
    parser.add_argument("--no-launch", action="store_true", help="Do not launch the tray app after install.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files or installing packages.")
    args = parser.parse_args(argv)

    check_python()
    if args.frontend != "xplat":
        install_linux_system_deps(dry_run=args.dry_run, install_system_deps=args.install_system_deps)
    frontend = choose_frontend(args.frontend)
    script = script_for_frontend(frontend)
    if not script.exists():
        raise SystemExit(f"Missing frontend script: {script}")

    log(f"=== cli-usage installer ===")
    log(f"Platform: {platform.system()} {platform.release()}")
    log(f"Frontend: {frontend} ({script.name})")

    python_bin = install_python_deps(frontend, dry_run=args.dry_run, skip_deps=args.skip_deps, venv=Path(args.venv).expanduser().resolve())
    if os.name == "nt" and frontend == "xplat":
        candidate = venv_pythonw(Path(args.venv).expanduser().resolve())
        if candidate.exists():
            python_bin = candidate
    if frontend == "gtk":
        enable_gnome_extension(dry_run=args.dry_run)

    entry = None
    if not args.no_autostart:
        if sys.platform == "darwin" and frontend == "xplat":
            program = build_macos_app(script, python_bin, dry_run=args.dry_run)
            entry = install_macos_autostart(script, python_bin, dry_run=args.dry_run, program=program)
        else:
            entry = install_autostart(script, python_bin, dry_run=args.dry_run)

    # On macOS the LaunchAgent (RunAtLoad) already started it; launching again would duplicate it.
    if not args.no_launch and not (sys.platform == "darwin" and entry):
        launch_app(script, python_bin, dry_run=args.dry_run)

    log("")
    log("Done.")
    if entry:
        log(f"Autostart entry: {entry}")
    log(f"Log file: {LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
