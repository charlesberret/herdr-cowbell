"""scripts/vendor-core refuses a core older than the vendored pin."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "vendor-core"
PINNED = "0.3.3"


class VendorCoreVersionGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        # vendor-core finds the plugin from its own location, so run a copy
        # inside a throwaway plugin tree; the real vendor/ is never touched.
        self.plugin = base / "plugin"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "vendor").mkdir()
        self.script = self.plugin / "scripts" / "vendor-core"
        shutil.copy2(SCRIPT, self.script)
        self.record = self.plugin / "vendor" / "VENDORED.json"
        self.record.write_text(json.dumps({"version": PINNED, "commit": "abc1234", "dirty": False}) + "\n")
        (self.plugin / "vendor" / "cowbell.py").write_text(f'__version__ = "{PINNED}"\n')
        (self.plugin / "herdr-plugin.toml").write_text(f'id = "herdr-cowbell"\nversion = "{PINNED}"\n')
        self.core = base / "core"
        self.core.mkdir()
        (self.core / "harnesses.toml").write_text("")

    def vendor(self, version):
        (self.core / "cowbell.py").write_text(f'__version__ = "{version}"\n')
        return subprocess.run([sys.executable, str(self.script), str(self.core)],
                              capture_output=True, text=True, timeout=30)

    def vendored_version(self):
        return json.loads(self.record.read_text())["version"]

    def test_same_version_vendors(self):
        result = self.vendor(PINNED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("refusing", result.stderr)
        self.assertEqual(self.vendored_version(), PINNED)

    def test_older_version_refuses_without_writing(self):
        before = {p.name: p.read_text() for p in (self.plugin / "vendor").iterdir()}
        manifest = (self.plugin / "herdr-plugin.toml").read_text()
        result = self.vendor("0.3.2")
        self.assertNotEqual(result.returncode, 0)
        reason = result.stderr.strip()
        self.assertEqual(len(reason.splitlines()), 1, reason)
        self.assertIn("0.3.2", reason)
        self.assertIn(PINNED, reason)
        self.assertEqual({p.name: p.read_text() for p in (self.plugin / "vendor").iterdir()}, before)
        self.assertEqual((self.plugin / "herdr-plugin.toml").read_text(), manifest)

    def test_newer_version_vendors(self):
        result = self.vendor("0.3.10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("refusing", result.stderr)
        self.assertEqual(self.vendored_version(), "0.3.10")
        self.assertIn('version = "0.3.10"', (self.plugin / "herdr-plugin.toml").read_text())


if __name__ == "__main__":
    unittest.main()
