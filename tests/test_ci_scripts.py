"""CI helper scripts: vendor checksum and public-surface scan."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK_VENDOR = ROOT / "scripts" / "check-vendor.sh"
PUBLIC_SCAN = ROOT / "scripts" / "public-surface-scan.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "public-surface"


class CheckVendor(unittest.TestCase):
    def test_passes_on_current_tree(self):
        proc = subprocess.run(["sh", str(CHECK_VENDOR)], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    def test_fails_when_cowbell_byte_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "plugin"
            shutil.copytree(ROOT / "vendor", tree / "vendor")
            dest = tree / "scripts" / "check-vendor.sh"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CHECK_VENDOR, dest)
            cowbell = tree / "vendor" / "cowbell.py"
            data = bytearray(cowbell.read_bytes())
            data[0] ^= 0x01
            cowbell.write_bytes(data)
            proc = subprocess.run(["sh", str(dest)], cwd=tree, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("sha256 mismatch", proc.stderr)


class PublicSurfaceScan(unittest.TestCase):
    def test_current_tree_is_clean(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep not installed")
        proc = subprocess.run(["sh", str(PUBLIC_SCAN)], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    def test_fixture_clean_passes(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep not installed")
        proc = subprocess.run(
            ["sh", str(PUBLIC_SCAN), str(FIXTURES / "clean")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)

    def test_fixture_dirty_fails(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep not installed")
        proc = subprocess.run(
            ["sh", str(PUBLIC_SCAN), str(FIXTURES / "dirty")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stdout + proc.stderr, r"leak\.txt")

