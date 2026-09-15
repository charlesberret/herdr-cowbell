"""herdr-cowbell packaging: manifest, vendored core, which core runs, a fresh checkout, the plugin commands.

Core behaviour is tested in the cowbell tree; these tests only cover what this plugin adds.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import herdr_cowbell as hm  # noqa: E402

SHIM = ROOT / "bin" / "cowbell"


def fake_cowbell(directory: Path, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / "cowbell"
    exe.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "cowbell {version}"; else echo "FAKE-CORE {version} $@"; fi\n')
    exe.chmod(0o755)
    return exe


def run_shim(*args, path: str, shim: Path = SHIM, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "COWBELL_"))}
    env.update({"PATH": path, **(env_extra or {})})
    return subprocess.run([str(shim), *args], capture_output=True, text=True, env=env, timeout=60)


class Packaging(unittest.TestCase):
    def test_manifest_commands_go_through_the_shim(self):
        m = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        commands = [a["command"] for a in m["actions"]] + [p["command"] for p in m["panes"]]
        self.assertTrue(commands)
        self.assertTrue(all(c[0] == "./bin/cowbell" for c in commands))
        self.assertEqual({c[1] for c in commands}, {"open", "adopt", "keys", "picker"})

    def test_vendored_core_is_pinned_and_clean(self):
        text = (ROOT / "vendor" / "cowbell.py").read_text()
        for name in ("keys_install_text", "def cmd_open", "picker_open_args", "def import_seats", "def cmd_adopt"):
            self.assertNotIn(name, text)
        pin = json.loads((ROOT / "vendor" / "VENDORED.json").read_text())
        version = ".".join(map(str, hm.vendored_version()))
        self.assertEqual(pin["version"], version)
        self.assertEqual(tomllib.loads((ROOT / "herdr-plugin.toml").read_text())["version"], version)
        self.assertFalse(pin["dirty"])

    def test_seed_is_the_vendored_one(self):
        self.assertEqual((ROOT / "harnesses.toml").resolve(), (ROOT / "vendor" / "harnesses.toml").resolve())
        self.assertEqual(tomllib.loads((ROOT / "harnesses.toml").read_text())["schema"], 1)

    def test_no_core_copy_outside_vendor(self):
        self.assertFalse((ROOT / "cowbell.py").exists())


class WhichCore(unittest.TestCase):
    """A cowbell on PATH at least as new as the vendored core wins; otherwise the vendored core runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vendored = ".".join(map(str, hm.vendored_version()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_newer_path_cowbell_wins(self):
        fake_cowbell(self.base / "new", "99.0.0")
        out = run_shim("--version", path=f"{self.base / 'new'}:/usr/bin:/bin")
        self.assertEqual(out.stdout.strip(), "cowbell 99.0.0")
        self.assertIn("FAKE-CORE 99.0.0 picker", run_shim("picker", path=f"{self.base / 'new'}:/usr/bin:/bin").stdout)

    def test_equal_path_cowbell_wins(self):
        fake_cowbell(self.base / "same", self.vendored)
        self.assertIn("FAKE-CORE", run_shim("status", path=f"{self.base / 'same'}:/usr/bin:/bin").stdout)

    def test_older_path_cowbell_loses(self):
        fake_cowbell(self.base / "old", "0.0.1")
        out = run_shim("--version", path=f"{self.base / 'old'}:/usr/bin:/bin")
        self.assertEqual(out.stdout.strip(), f"cowbell {self.vendored}")

    def test_no_path_cowbell_runs_vendored(self):
        self.assertEqual(run_shim("--version", path="/usr/bin:/bin").stdout.strip(), f"cowbell {self.vendored}")

    def test_the_shim_itself_on_path_is_not_a_core(self):
        out = run_shim("--version", path=f"{SHIM.parent}:/usr/bin:/bin")
        self.assertEqual(out.stdout.strip(), f"cowbell {self.vendored}")

    def test_plugin_commands_never_leave_for_path(self):
        fake_cowbell(self.base / "new", "99.0.0")
        out = run_shim("keys", "install", path=f"{self.base / 'new'}:/usr/bin:/bin",
                       env_extra={"HOME": str(self.base)})
        self.assertNotIn("FAKE-CORE", out.stdout)
        self.assertIn("prefix+c", (self.base / ".config" / "herdr" / "config.toml").read_text())


class FreshCheckout(unittest.TestCase):
    """What `herdr plugin install` clones must run with no pip or venv: stdlib only."""

    def test_copy_of_tracked_files_runs_status(self):
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                               capture_output=True, text=True, check=True).stdout.split()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "herdr-cowbell"
            for f in files:
                src = ROOT / f
                if not src.exists() and not src.is_symlink():
                    continue  # deleted in the working tree
                (dest / f).parent.mkdir(parents=True, exist_ok=True)
                if src.is_symlink():
                    os.symlink(os.readlink(src), dest / f)
                else:
                    shutil.copy2(src, dest / f)
            base = Path(tmp)
            env = {"HERDR_BIN_PATH": str(base / "no-herdr"), "HERDR_SOCKET_PATH": str(base / "no.sock"),
                   "COWBELL_MODE": "here", "COWBELL_CONFIG_DIR": str(base / "cfg"),
                   "COWBELL_STATE_DIR": str(base / "state"), "HOME": str(base)}
            out = run_shim("status", path="/usr/bin:/bin", shim=dest / "bin" / "cowbell", env_extra=env)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("cowbell", out.stdout)
            self.assertTrue((base / "cfg" / "harnesses.toml").exists())   # seeded from the vendored seed


CHARLES_CONFIG = """onboarding = false

[ui]
status_indicators = "symbols"

[theme]
name = "tokyo-night"

# herdr-seats: enter a named sitting
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "herdr-seats.picker"
description = "enter a seat"
"""


class Keys(unittest.TestCase):
    def test_install_places_keys_table_before_array_and_is_idempotent(self):
        once = hm.keys_install_text(CHARLES_CONFIG)
        self.assertEqual(once, hm.keys_install_text(once))
        keys_at = once.index("[keys]")
        self.assertLess(keys_at, once.index("[[keys.command]]"))         # TOML: table before array-of-tables
        self.assertIn('new_tab = ""  # cowbell', once)
        self.assertIn('command = "herdr-cowbell.open"', once)
        self.assertIn("herdr-seats.picker", once)                        # existing binding untouched
        import tomllib
        parsed = tomllib.loads(once)
        self.assertEqual(parsed["keys"]["new_tab"], "")
        self.assertEqual([c["command"] for c in parsed["keys"]["command"]], ["herdr-seats.picker", "herdr-cowbell.open"])

    def test_uninstall_restores_original(self):
        self.assertEqual(hm.keys_uninstall_text(hm.keys_install_text(CHARLES_CONFIG)).strip(), CHARLES_CONFIG.strip())

    def test_existing_new_tab_is_saved_and_restored(self):
        cfg = "[keys]\nnew_tab = \"prefix+t\"\nsplit = \"prefix+v\"\n"
        inst = hm.keys_install_text(cfg)
        self.assertIn('# cowbell saved: new_tab = "prefix+t"', inst)
        self.assertIn('new_tab = ""  # cowbell', inst)
        self.assertEqual(hm.keys_uninstall_text(inst).strip(), cfg.strip())

    def test_empty_config(self):
        inst = hm.keys_install_text("")
        import tomllib
        parsed = tomllib.loads(inst)
        self.assertEqual(parsed["keys"]["new_tab"], "")
        self.assertEqual(parsed["keys"]["command"][0]["key"], "prefix+c")


class OpenCtx(unittest.TestCase):
    def test_strips_to_caller_keys_and_fills_herdr_env(self):
        env = {
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({
                "workspace_id": "wY", "focused_pane_id": "wY:pA",
                "focused_pane_cwd": "/tmp/proj", "selected_text": "noise",
            }),
        }
        ctx = hm.invocation_ctx(env)
        self.assertEqual(ctx["workspace_id"], "wY")
        self.assertEqual(ctx["focused_pane_cwd"], "/tmp/proj")
        self.assertNotIn("selected_text", ctx)
        args = hm.picker_open_args(ctx)
        self.assertIn("--cwd", args)
        self.assertEqual(args[args.index("--cwd") + 1], "/tmp/proj")
        self.assertIn("COWBELL_CTX=" + json.dumps(ctx), args)

    def test_cli_herdr_env_without_plugin_json(self):
        ctx = hm.invocation_ctx({
            "HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1",
            "HERDR_PANE_ID": "w1:p1", "HERDR_PLUGIN_CONTEXT_JSON": "",
        })
        self.assertEqual(ctx["workspace_id"], "w1")
        self.assertEqual(ctx["focused_pane_id"], "w1:p1")
        # no pane get: cwd hole is left empty (picker --cwd omitted)
        self.assertNotIn("focused_pane_cwd", ctx)
        self.assertNotIn("--cwd", hm.picker_open_args(ctx))


if __name__ == "__main__":
    unittest.main()
