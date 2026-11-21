"""The Herdr desktop app mod in app/cowbell.

Runs the node tests of its plain logic (app_logic.test.mjs) and, where the
Herdr app is installed, `herdr-app check` on the mod. Each skips when its
program is missing, so CI without the app still runs the rest.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "app" / "cowbell"


class AppMod(unittest.TestCase):
    def test_mod_json_opens_as_a_dialog_and_docks_nowhere(self):
        spec = json.loads((MOD / "mod.json").read_text())
        self.assertEqual(spec["place"], "none")
        self.assertEqual(spec["overlay"]["kind"], "dialog")

    def test_contract_names_are_in_one_constant(self):
        logic = (MOD / "logic.js").read_text()
        main = (MOD / "main.js").read_text()
        for name in ("COWBELL_HC_SPACE", "COWBELL_HC_CWD"):
            self.assertEqual(logic.count(f'"{name}"'), 1, name)
            self.assertNotIn(name, main)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_logic_under_node(self):
        r = subprocess.run(["node", "--test", str(ROOT / "tests" / "app_logic.test.mjs")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-4000:] + r.stderr[-2000:])

    @unittest.skipUnless(shutil.which("herdr-app"), "the Herdr app is not installed")
    def test_herdr_app_check(self):
        r = subprocess.run(["herdr-app", "check", str(MOD)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("check passed", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main()
