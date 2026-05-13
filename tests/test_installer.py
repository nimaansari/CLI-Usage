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
            with patch.dict(os.environ, {"HOME": tmp}):
                entry = install.install_linux_autostart(Path("/app/cli_usage_xplat.py"), Path("/venv/bin/python"), dry_run=False)
                self.assertTrue(entry.exists())
                text = entry.read_text()
                self.assertIn("Name=cli-usage", text)
                self.assertIn("cli_usage_xplat.py", text)

    def test_dry_run_does_not_write_linux_autostart(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}):
                entry = install.install_linux_autostart(Path("/app/cli_usage_xplat.py"), Path("/venv/bin/python"), dry_run=True)
                self.assertFalse(entry.exists())

    def test_macos_autostart_writes_plist_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}):
                entry = install.install_macos_autostart(Path("/app/cli_usage_xplat.py"), Path("/venv/bin/python"), dry_run=False)
                self.assertTrue(entry.exists())
                text = entry.read_text()
                self.assertIn("com.user.cli-usage", text)
                self.assertIn("cli_usage_xplat.py", text)

    def test_windows_autostart_writes_cmd_to_appdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"APPDATA": tmp}):
                entry = install.install_windows_autostart(Path("C:/app/cli_usage_xplat.py"), Path("C:/app/.venv/Scripts/pythonw.exe"), dry_run=False)
                self.assertTrue(entry.exists())
                text = entry.read_text()
                self.assertIn("cli_usage_xplat.py", text)

    def test_main_dry_run_no_launch_no_autostart(self):
        rc = install.main(["--dry-run", "--skip-deps", "--no-launch", "--no-autostart", "--frontend", "xplat"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
