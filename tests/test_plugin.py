"""herdr-cowbell packaging: manifest, vendored core, which core runs, a fresh checkout, the plugin commands.

Core behaviour is tested in the cowbell tree; these tests only cover what this plugin adds.
The vendored herdr raise is pinned here: a place with unix_user must pane-run agent-run.
"""
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
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
        commands = (
            [a["command"] for a in m["actions"]]
            + [p["command"] for p in m["panes"]]
            + [s["command"] for s in m.get("startup", [])]
        )
        self.assertTrue(commands)
        self.assertTrue(all(c[0] == "./bin/cowbell" for c in commands))
        self.assertEqual({c[1] for c in commands}, {"open", "adopt", "keys", "picker"})
        self.assertEqual(m["startup"], [{"command": ["./bin/cowbell", "keys", "ensure"]}])
        picker = next(p for p in m["panes"] if p["id"] == "picker")
        self.assertEqual(picker["title"], "\u2800\u2800cowbell")

    def test_vendored_core_is_pinned_and_clean(self):
        text = (ROOT / "vendor" / "cowbell.py").read_text()
        for name in ("keys_install_text", "def cmd_open", "picker_open_args", "def import_seats"):
            self.assertNotIn(name, text)
        self.assertIn("def unix_confine(", text)
        self.assertIn("unix_confine(cat, root, base)", text)
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

    def test_live_like_keys_table_without_new_tab(self):
        cfg = (
            "[keys]\n"
            "\n"
            "[[keys.command]]\n"
            'key = "prefix+shift+r"\n'
            'type = "plugin_action"\n'
            'command = "herdr-agent-quota.refresh"\n'
        )
        inst = hm.keys_install_text(cfg)
        parsed = tomllib.loads(inst)
        self.assertEqual(parsed["keys"]["new_tab"], "")
        self.assertEqual(
            [c["command"] for c in parsed["keys"]["command"]],
            ["herdr-agent-quota.refresh", "herdr-cowbell.open"],
        )

    def test_ensure_without_claim_leaves_config_alone(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        cfg = base / "config.toml"
        cfg.write_text("[keys]\n", encoding="utf-8")
        claim = base / "prefix-c.claimed"
        hm.cmd_keys("ensure", cfg, claim=claim)
        self.assertEqual(cfg.read_text(encoding="utf-8"), "[keys]\n")
        self.assertFalse(claim.exists())

    def test_install_claims_and_ensure_reasserts_after_wipe(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        cfg = base / "config.toml"
        cfg.write_text("[theme]\nname = \"tokyo-night\"\n\n[keys]\n", encoding="utf-8")
        claim = base / "prefix-c.claimed"
        hm.cmd_keys("install", cfg, claim=claim)
        self.assertTrue(claim.is_file())
        self.assertIn("herdr-cowbell.open", cfg.read_text(encoding="utf-8"))
        # herdr upgrade rewrote config.toml and dropped [keys] entirely
        cfg.write_text("[theme]\nname = \"tokyo-night\"\n", encoding="utf-8")
        hm.cmd_keys("ensure", cfg, claim=claim)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("herdr-cowbell.open", text)
        self.assertIn('new_tab = ""  # cowbell', text)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["keys"]["new_tab"], "")
        self.assertEqual(parsed["keys"]["command"][0]["command"], "herdr-cowbell.open")

    def test_uninstall_releases_the_claim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        cfg = base / "config.toml"
        cfg.write_text(CHARLES_CONFIG, encoding="utf-8")
        claim = base / "prefix-c.claimed"
        hm.cmd_keys("install", cfg, claim=claim)
        hm.cmd_keys("uninstall", cfg, claim=claim)
        self.assertFalse(claim.exists())
        self.assertNotIn("herdr-cowbell.open", cfg.read_text(encoding="utf-8"))
        hm.cmd_keys("ensure", cfg, claim=claim)
        self.assertNotIn("herdr-cowbell.open", cfg.read_text(encoding="utf-8"))

    def test_unknown_keys_action_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            hm.cmd_keys("clobber")
        self.assertIn("unknown action", str(ctx.exception))


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


class VendoredUnixConfine(unittest.TestCase):
    """A herdr raise in a place with unix_user runs agent-run.

    `herdr agent start` launches as the server uid, so the vendored core
    sends `pane run` of `agent-run <uid> -- <cmd>` (or `sudo -u` when
    agent-run is not on PATH). The tab command is not the caller's.

    `cowbell adopt` only records a pane that is already running. It cannot
    change that pane's uid, and it does not re-exec.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "proj"
        self.root.mkdir()
        self._saved = {
            "PATH": os.environ.get("PATH"),
            "COWBELL_CONFIG_DIR": os.environ.get("COWBELL_CONFIG_DIR"),
            "COWBELL_STATE_DIR": os.environ.get("COWBELL_STATE_DIR"),
        }
        os.environ["COWBELL_CONFIG_DIR"] = str(self.base / "cfg")
        os.environ["COWBELL_STATE_DIR"] = str(self.base / "state")
        self.addCleanup(self._restore_env)
        self.me = pwd.getpwuid(os.geteuid()).pw_name
        self.other = self._other_user()

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _other_user(self) -> str:
        for name in ("nobody", "daemon"):
            try:
                if pwd.getpwnam(name).pw_uid != os.geteuid():
                    return name
            except KeyError:
                continue
        for entry in pwd.getpwall():
            if entry.pw_uid != os.geteuid() and entry.pw_name:
                return entry.pw_name
        self.skipTest("no other unix account on this host")

    def _agent_run(self) -> Path:
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "agent-run"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self._saved['PATH'] or ''}"
        return exe

    def _raise(self, unix_user: str) -> list[tuple]:
        cb = hm.core()
        calls: list[tuple] = []

        def fake_herdr(*args, expect_json=True):
            calls.append(args)
            if args[:2] == ("tab", "create"):
                return {
                    "root_pane": {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"},
                    "tab": {"tab_id": "w1:t1", "workspace_id": "w1"},
                }
            if args[:2] == ("pane", "process-info"):
                return {"process_info": {"foreground_processes": [{"name": "bash"}]}}
            if args[:2] == ("agent", "start"):
                return {"agent": {"agent_session": {"value": "sess-1"}}}
            return {}

        harness = {"id": "claude", "kind": "claude", "models": [], "resume": []}
        cat = {
            "places": {"lab": {"cwd": str(self.root), "unix_user": unix_user}},
            "harness": [harness],
            "defaults": {},
            "lanes": {},
        }
        ctx = cb.Ctx(workspace_id="w1", cwd=str(self.root), mode="tab",
                     live=cb.LiveSource(), launch=cb.HerdrLaunch())
        with mock.patch.object(cb, "herdr", fake_herdr):
            pane = ctx.launch.start(cat, harness, "lab-claude", str(self.root), ctx)
        self.assertEqual(pane["pane_id"], "w1:p1")
        return calls

    def _pane_run(self, calls: list[tuple]) -> list[str]:
        runs = [c for c in calls if c[:3] == ("pane", "run", "w1:p1")]
        self.assertEqual(len(runs), 1, calls)
        return shlex.split(runs[0][3])

    def test_herdr_raise_with_unix_user_runs_agent_run(self):
        exe = self._agent_run()
        calls = self._raise(self.other)
        self.assertFalse([c for c in calls if c[:2] == ("agent", "start")])
        parts = self._pane_run(calls)
        self.assertEqual(parts[0], str(exe))
        self.assertEqual(parts[1:4], [self.other, "--", "claude"])
        creates = [c for c in calls if c[:2] == ("tab", "create")]
        self.assertEqual(creates[0][creates[0].index("--cwd") + 1], str(self.root))

    def test_herdr_raise_without_agent_run_uses_sudo(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        sudo = shutil.which("sudo")
        if not sudo:
            self.skipTest("sudo is not on PATH")
        calls = self._raise(self.other)
        parts = self._pane_run(calls)
        self.assertEqual(parts[:5], [sudo, "-u", self.other, "--", "claude"])

    def test_same_uid_stays_on_agent_start(self):
        self._agent_run()
        calls = self._raise(self.me)
        self.assertFalse([c for c in calls if c[:2] == ("pane", "run")])
        starts = [c for c in calls if c[:2] == ("agent", "start")]
        self.assertEqual(starts[0][2], "lab-claude")
        self.assertIn("claude", starts[0])

    def test_adopt_does_not_reexec(self):
        import inspect
        src = inspect.getsource(hm.core().cmd_adopt)
        self.assertNotIn("unix_confine", src)
        self.assertNotIn("agent-run", src)


if __name__ == "__main__":
    unittest.main()
