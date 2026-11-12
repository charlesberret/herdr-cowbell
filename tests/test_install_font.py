"""Plugin manifest wiring for install-font."""
import os
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class InstallFontAction(unittest.TestCase):
    def test_manifest_install_font_action(self):
        manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        action = next(a for a in manifest["actions"] if a["id"] == "install-font")
        self.assertEqual(action["command"], ["./bin/cowbell", "font", "install"])

    def test_cowbell_shim_exists_and_is_executable(self):
        shim = ROOT / "bin" / "cowbell"
        self.assertTrue(shim.is_file(), "bin/cowbell must exist")
        mode = shim.stat().st_mode
        self.assertTrue(mode & os.X_OK, "bin/cowbell must be executable")
