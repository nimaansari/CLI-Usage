import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install


class InstallerTests(unittest.TestCase):
    def test_script_for_frontend(self):
        self.assertEqual(install.script_for_frontend("gtk").name, "cli_usage_gtk.py")
        self.assertEqual(install.script_for_frontend("xplat").name, "cli_usage_xplat.py")

    def test_linux_autostart_writes_desktop_file_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("install.Path.home", return_value=Path(tmp)):
                entry = install.install_linux_autostart(Path("/app/cli_usage_xplat.py"), Path("/venv/bin/python"), dry_run=False)
                self.assertTrue(entry.exists())
                text = entry.read_text()
                self.assertIn("Name=CLI Usage", text)
                self.assertIn('"/venv/bin/python" "/app/cli_usage_xplat.py"', text)
                self.assertIn("cli_usage_xplat.py", text)

    def test_dry_run_does_not_write_linux_autostart(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("install.Path.home", return_value=Path(tmp)):
                entry = install.install_linux_autostart(Path("/app/cli_usage_xplat.py"), Path("/venv/bin/python"), dry_run=True)
                self.assertFalse(entry.exists())

    def test_macos_autostart_writes_plist_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("install.Path.home", return_value=Path(tmp)):
                entry = install.install_macos_autostart(Path("/app/cli_usage_xplat.py"), Path("/venv/bin/python"), dry_run=False)
                self.assertTrue(entry.exists())
                text = entry.read_text()
                self.assertIn("com.user.cli-usage", text)
                self.assertIn("cli_usage_xplat.py", text)

    def test_windows_autostart_writes_cmd_to_appdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Force the .cmd fallback so the test reads text, not a binary .lnk.
            with patch.dict(os.environ, {"APPDATA": tmp}), patch("install._windows_shortcut", return_value=False):
                entry = install.install_windows_autostart(Path("C:/app/cli_usage_xplat.py"), Path("C:/app/.venv/Scripts/pythonw.exe"), dry_run=False)
                self.assertTrue(entry.exists())
                text = entry.read_text()
                self.assertIn("cli_usage_xplat.py", text)

    def test_desktop_quote_escapes_specials(self):
        self.assertEqual(install._desktop_quote("/my apps/x.py"), '"/my apps/x.py"')
        self.assertEqual(install._desktop_quote('a"b$c%d'), '"a\\"b\\$c%%d"')

    def test_uninstall_dry_run_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("install.Path.home", return_value=Path(tmp)), \
                    patch("install._windows_programs_dir", return_value=Path(tmp) / "Programs"):
                entry = install.install_linux_autostart(Path("/app/cli_usage_gtk.py"), Path("/usr/bin/python3"), dry_run=False)
                with patch("install.stop_running"):
                    self.assertEqual(install.uninstall(dry_run=True), 0)
                self.assertTrue(entry.exists())
                with patch("install.stop_running"):
                    install.uninstall(dry_run=False)
                self.assertFalse(entry.exists())

    def test_linux_service_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("install.Path.home", return_value=Path(tmp)), patch("install.run"):
                unit = install.install_linux_service(Path("/my app/cli_usage_gtk.py"), Path("/usr/bin/python3"), dry_run=False)
                text = unit.read_text()
                self.assertIn('ExecStart="/usr/bin/python3" "/my app/cli_usage_gtk.py"', text)
                self.assertIn("WorkingDirectory=/my app\n", text)
                self.assertIn("Restart=always", text)
                self.assertIn("Environment=CLI_USAGE_SERVICE=1", text)
                self.assertIn("WantedBy=graphical-session.target", text)

    def test_main_dry_run_no_launch_no_autostart(self):
        rc = install.main(["--dry-run", "--skip-deps", "--no-launch", "--no-autostart", "--frontend", "xplat"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
