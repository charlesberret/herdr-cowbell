import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herdmark as hm  # noqa: E402


CAT = {
    "defaults": {"lane": "yellow"},
    "lanes": {"claude": {"green": ["--permission-mode", "plan"], "yellow": ["--permission-mode", "acceptEdits"]},
              "grok": {"green": ["--permission-mode", "plan"], "yellow": ["--permission-mode", "acceptEdits"],
                       "red": ["--always-approve"]},
              "pi": {"green": ["--tools", "read"]}},
    "harness": [
        {"id": "claude", "kind": "claude", "family": "claude", "egress": "vendor", "trust_tool": "claude",
         "models": ["claude-sonnet-5", "claude-opus-5"], "model_flag": ["--model"], "resume": ["--resume"],
         "tools_allow_flag": "--allowedTools", "tools_deny_flag": "--disallowedTools",
         "seat_flag": "--append-system-prompt-file"},
        {"id": "grok", "kind": "grok", "family": "grok", "egress": "vendor", "trust_tool": "grok",
         "models": [], "model_flag": ["-m"], "resume": ["--resume"], "claim_flag": "--agent",
         "tools_allow_flag": "--allowedTools", "tools_deny_flag": "--disallowed-tools"},
        {"id": "codex", "kind": "codex", "family": "codex", "egress": "vendor", "trust_tool": "codex",
         "models": [], "resume": ["resume"]},
        {"id": "omp", "kind": "omp", "family": "omp", "egress": "vendor", "trust_tool": "omp",
         "models": [], "resume": ["--resume="]},
        {"id": "oll-mid", "kind": "pi", "family": "oll", "egress": "vendor", "trust_tool": "oll-mid",
         "models": [], "command": "/nonexistent/oll-mid", "resume": ["--continue"]},
        {"id": "local-ornith", "kind": "pi", "family": "local", "egress": "local", "trust_tool": "local-ornith",
         "models": [], "command": "/nonexistent/local-ornith", "resume": ["--continue"]},
    ],
}
ROOT = "/Users/x/code/notes/nomogenesis"


def agent(pane, kind, cwd, status="idle", title="", sid="s1"):
    return {"pane_id": pane, "agent": kind, "cwd": cwd, "agent_status": status, "tab_id": "w1:t1",
            "workspace_id": "w1", "terminal_title_stripped": title, "agent_session": {"value": sid}}


class Scope(unittest.TestCase):
    def test_at_or_beneath(self):
        self.assertTrue(hm.at_or_beneath(ROOT, ROOT))
        self.assertTrue(hm.at_or_beneath(ROOT + "/ballots", ROOT))
        self.assertFalse(hm.at_or_beneath("/Users/x", ROOT))            # above never counts
        self.assertFalse(hm.at_or_beneath(ROOT + "-core", ROOT))        # sibling with prefix
        self.assertFalse(hm.at_or_beneath("/Users/x/code/notes", ROOT))

    def test_seat_file_walks_up_and_stops_at_home(self):
        # Same semantics as code/bin/lib/pi-project-seat.sh: check each dir up to and
        # including $HOME, never above it.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            proj = home / "cloud" / "proj" / "deep"
            proj.mkdir(parents=True)
            (Path(tmp) / "SEAT.md").write_text("above home: never")
            self.assertIsNone(hm.seat_file(str(proj), home=str(home)))
            (home / "SEAT.md").write_text("home seat")
            self.assertTrue(str(hm.seat_file(str(proj), home=str(home))).endswith("home/SEAT.md"))
            (home / "cloud" / "proj" / "SEAT.md").write_text("nearer wins")
            self.assertTrue(str(hm.seat_file(str(proj), home=str(home))).endswith("proj/SEAT.md"))

    def test_slug_and_unique(self):
        self.assertEqual(hm.slug("Claude Code"), "claude-code")
        self.assertEqual(hm.slug("9lives"), "h-9lives")
        self.assertTrue(hm.NAME_RE.match(hm.slug("x" * 60)))
        self.assertEqual(hm.unique_name("a", {"a", "a-2"}), "a-3")


class Argv(unittest.TestCase):
    def test_claude_full(self):
        h = hm.harness_by_id(CAT, "claude")
        with tempfile.TemporaryDirectory() as tmp:
            seat = Path(tmp) / "SEAT.md"; seat.write_text("s")
            argv = hm.build_argv(CAT, h, "claude-opus-5", "green", "Read,Grep", "Bash", seat)
        self.assertEqual(argv, ["--model", "claude-opus-5", "--permission-mode", "plan",
                                "--allowedTools", "Read,Grep", "--disallowedTools", "Bash",
                                "--append-system-prompt-file", str(seat)])

    def test_resume_shapes(self):
        self.assertEqual(hm.build_argv(CAT, hm.harness_by_id(CAT, "claude"), resume_id="abc"), ["--resume", "abc"])
        self.assertEqual(hm.build_argv(CAT, hm.harness_by_id(CAT, "codex"), resume_id="abc"), ["resume", "abc"])
        self.assertEqual(hm.build_argv(CAT, hm.harness_by_id(CAT, "omp"), resume_id="abc"), ["--resume=abc"])

    def test_wrapper_ignores_lane_and_model(self):
        h = hm.harness_by_id(CAT, "oll-mid")
        self.assertEqual(hm.build_argv(CAT, h, "whatever", "red", "x", "y"), [])
        self.assertEqual(hm.build_argv(CAT, h, resume_id="any"), ["--continue"])
        self.assertEqual(hm.build_argv(CAT, h, extra=["--memo"]), ["--memo"])

    def test_unknown_lane_adds_nothing(self):
        self.assertEqual(hm.build_argv(CAT, hm.harness_by_id(CAT, "claude"), lane="red"), [])


class Registry(unittest.TestCase):
    def setUp(self):
        self.agents = [
            agent("w1:p1", "claude", ROOT, "working", "majordomo"),
            agent("w1:p2", "grok", "/Users/x", "idle", "sysadmin"),          # above root: excluded
            agent("w1:p3", "claude", ROOT + "/ballots", "idle", "ballots"),
        ]
        self.panes = [{"pane_id": p} for p in ("w1:p1", "w1:p2", "w1:p3", "w1:p9")]
        self.roster = [
            {"name": "majordomo", "cwd": ROOT, "harness": "claude", "kind": "claude", "pane_id": "w1:p1", "model": "claude-opus-5"},
            {"name": "kimi-notes", "cwd": ROOT, "harness": "oll-mid", "kind": "pi", "pane_id": "w1:p9", "session_id": "k1"},
            {"name": "old-codex", "cwd": ROOT + "/sub", "harness": "codex", "kind": "codex", "pane_id": "gone", "session_id": "c1"},
            {"name": "elsewhere", "cwd": "/Users/x/other", "harness": "claude", "kind": "claude", "pane_id": "w1:p5"},
        ]
        self.matcher = lambda tool: {"verdict": "blocked" if tool == "local-ornith" else "ok", "flag": ""}

    def test_partition(self):
        reg = hm.build_registry(CAT, ROOT, self.agents, self.panes, self.roster, matcher=self.matcher, subject="internal")
        self.assertEqual([r["name"] for r in reg["live"]], ["majordomo", "ballots"])
        self.assertEqual(reg["live"][0]["model"], "claude-opus-5")
        self.assertTrue(reg["live"][0]["on_roster"]); self.assertFalse(reg["live"][1]["on_roster"])
        cold = {r["name"]: r for r in reg["cold"]}
        self.assertEqual(set(cold), {"kimi-notes", "old-codex"})
        self.assertEqual(cold["kimi-notes"]["status"], "unknown")   # pane exists, no detected agent
        self.assertEqual(cold["old-codex"]["status"], "cold")
        absent = {a["harness"]: a for a in reg["absent"]}
        self.assertEqual(set(absent), {"omp", "local-ornith", "grok"})
        self.assertEqual(absent["local-ornith"]["trust"]["verdict"], "blocked")
        self.assertFalse(absent["local-ornith"]["installed"])

    def test_render_mentions_sections(self):
        reg = hm.build_registry(CAT, ROOT, self.agents, self.panes, self.roster, matcher=self.matcher, subject="internal")
        text = hm.render_registry(reg, git_top="/Users/x/code/notes", wider=3)
        for needle in ("LIVE HERE", "COLD HERE", "ABSENT HERE", "majordomo", "kimi-notes", "local-ornith ⌂⊘",
                       "omp ↑ok", "[g] widen", "subject: internal"):
            self.assertIn(needle, text)
        self.assertNotIn("sysadmin", text)

    def test_live_name_prefers_tab_label(self):
        agents = [agent("w1:p1", "grok", ROOT, "idle", "- Inspect flags - Grok 4.6 - grok")]
        agents[0]["tab_id"] = "w1:t9"
        reg = hm.build_registry(CAT, ROOT, agents, [{"pane_id": "w1:p1"}], [],
                                matcher=self.matcher, tab_label={"w1:t9": "coord-herdmark"})
        self.assertEqual(reg["live"][0]["name"], "coord-herdmark")

    def test_empty_root(self):
        reg = hm.build_registry(CAT, "/nowhere", [], [], [], matcher=self.matcher)
        self.assertIn("nothing raised here yet", hm.render_registry(reg))


class Trust(unittest.TestCase):
    def test_parse_match(self):
        text = "tool:      claude\nverdict:   blocked  (H2 subject personal exceeds ceiling project-ip)\nflag:      ⊘H2\n"
        v = hm.parse_match(text, 4)
        self.assertEqual(v["verdict"], "blocked"); self.assertEqual(v["flag"], "⊘H2")
        v = hm.parse_match("verdict:   ok\nflag:      (none)\n", 0)
        self.assertEqual((v["verdict"], v["flag"]), ("ok", ""))
        self.assertEqual(hm.parse_match("", 3)["verdict"], "overprivilege")


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
        self.assertIn('new_tab = ""  # herdmark', once)
        self.assertIn('command = "herdmark.open"', once)
        self.assertIn("herdr-seats.picker", once)                        # existing binding untouched
        import tomllib
        parsed = tomllib.loads(once)
        self.assertEqual(parsed["keys"]["new_tab"], "")
        self.assertEqual([c["command"] for c in parsed["keys"]["command"]], ["herdr-seats.picker", "herdmark.open"])

    def test_uninstall_restores_original(self):
        self.assertEqual(hm.keys_uninstall_text(hm.keys_install_text(CHARLES_CONFIG)).strip(), CHARLES_CONFIG.strip())

    def test_existing_new_tab_is_saved_and_restored(self):
        cfg = "[keys]\nnew_tab = \"prefix+t\"\nsplit = \"prefix+v\"\n"
        inst = hm.keys_install_text(cfg)
        self.assertIn('# herdmark saved: new_tab = "prefix+t"', inst)
        self.assertIn('new_tab = ""  # herdmark', inst)
        self.assertEqual(hm.keys_uninstall_text(inst).strip(), cfg.strip())

    def test_empty_config(self):
        inst = hm.keys_install_text("")
        import tomllib
        parsed = tomllib.loads(inst)
        self.assertEqual(parsed["keys"]["new_tab"], "")
        self.assertEqual(parsed["keys"]["command"][0]["key"], "prefix+c")


class PickerList(unittest.TestCase):
    def test_raiseable_skips_blocked_and_missing(self):
        reg = {
            "root": ROOT, "subject": "internal",
            "live": [{"name": "majordomo", "harness": "claude", "status": "idle",
                      "pane_id": "w1:p1", "on_roster": True}],
            "cold": [{"name": "kimi-notes", "harness": "oll-mid", "status": "cold",
                      "pane_id": None, "kind": "pi"}],
            "absent": [
                {"harness": "grok", "kind": "grok", "egress": "vendor", "installed": True,
                 "trust": {"verdict": "ok"}},
                {"harness": "omp", "kind": "omp", "egress": "vendor", "installed": False,
                 "trust": {"verdict": "ok"}},
                {"harness": "local-ornith", "kind": "pi", "egress": "local", "installed": False,
                 "trust": {"verdict": "blocked"}},
            ],
        }
        choices = hm.picker_choices(reg)
        self.assertEqual([c["action"] for c in choices], ["focus", "enter", "raise"])
        self.assertEqual(choices[2]["target"]["harness"], "grok")
        text = hm.render_picker(reg, choices)
        self.assertIn("LIVE", text)
        self.assertIn("COLD", text)
        self.assertIn("RAISE", text)
        self.assertIn("  3  grok", text)
        self.assertIn("⊘  local-ornith", text)
        self.assertIn("not installed: omp", text)
        self.assertNotIn("local-ornith", [c["target"].get("harness") for c in choices if c["action"] == "raise"])


class Flags(unittest.TestCase):
    def test_parse_flags(self):
        opts, pos = hm.parse_flags(["--harness", "claude", "--lane=green", "--yes", "extra", "--", "--raw"])
        self.assertEqual(opts, {"harness": "claude", "lane": "green", "yes": True})
        self.assertEqual(pos, ["extra", "--raw"])


class Sitting(unittest.TestCase):
    def test_head(self):
        self.assertEqual(hm.sitting_head("builder-minaret"), "builder")
        self.assertEqual(hm.sitting_head("Majordomo"), "majordomo")
        self.assertEqual(hm.sitting_head("coord-herdmark"), "coord")

    def test_grok_claim_only_when_defined(self):
        h = hm.harness_by_id(CAT, "grok")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "builder.md").write_text("x")
            self.assertEqual(hm.claim_argv(h, "builder-minaret", agents_dir=d), ["--agent", "builder"])
            self.assertEqual(hm.claim_argv(h, "career", agents_dir=d), [])  # no career.md
        self.assertEqual(hm.claim_argv(hm.harness_by_id(CAT, "claude"), "builder-minaret"), [])

    def test_grok_argv_lane_and_claim(self):
        h = hm.harness_by_id(CAT, "grok")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "builder.md").write_text("x")
            argv = hm.build_argv(CAT, h, lane="yellow", sitting="builder-minaret", agents_dir=d)
        self.assertEqual(argv, ["--permission-mode", "acceptEdits", "--agent", "builder"])

    def test_shell_ready_single_zsh(self):
        two = {"process_info": {"foreground_processes": [
            {"name": "zsh"}, {"name": "zsh"}]}}
        one = {"process_info": {"foreground_processes": [{"name": "zsh"}]}}
        grok = {"process_info": {"foreground_processes": [{"name": "grok"}]}}
        self.assertFalse(hm.shell_ready_from_process(two))
        self.assertTrue(hm.shell_ready_from_process(one))
        self.assertFalse(hm.shell_ready_from_process(grok))

    def test_live_names_are_tab_labels_not_titles(self):
        snap = {
            "tabs": [{"tab_id": "w1:t1", "label": "builder-minaret"},
                     {"tab_id": "w1:t2", "label": "herdmark"}],
            "agents": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1",
                 "terminal_title_stripped": "- Inspect grok flags - Grok 4.6 - herdmark-core - grok"},
                {"pane_id": "w1:p2", "tab_id": "w1:t2",
                 "terminal_title_stripped": "herdmark-core"},
            ],
        }
        self.assertEqual(hm.live_names(snap), {"builder-minaret", "herdmark"})


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
        self.assertIn("HERDMARK_CTX=" + json.dumps(ctx), args)

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


class Seed(unittest.TestCase):
    def test_grok_lanes_and_claim_flag(self):
        import tomllib
        seed = Path(__file__).resolve().parent.parent / "harnesses.toml"
        cat = tomllib.loads(seed.read_text())
        grok = next(h for h in cat["harness"] if h["id"] == "grok")
        self.assertEqual(grok.get("claim_flag"), "--agent")
        self.assertEqual(cat["lanes"]["grok"]["green"], ["--permission-mode", "plan"])
        self.assertEqual(cat["lanes"]["grok"]["yellow"], ["--permission-mode", "acceptEdits"])


if __name__ == "__main__":
    unittest.main()
