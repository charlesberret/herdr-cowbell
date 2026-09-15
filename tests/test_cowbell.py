import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cowbell as hm  # noqa: E402


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
                                matcher=self.matcher, tab_label={"w1:t9": "coord-cowbell"})
        self.assertEqual(reg["live"][0]["name"], "coord-cowbell")

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


class PickerList(unittest.TestCase):
    def reg(self):
        return {
            "root": ROOT, "subject": "internal",
            "live": [{"name": "majordomo", "harness": "claude", "kind": "claude", "status": "idle",
                      "pane_id": "w1:p1", "workspace_id": "w1", "on_roster": True, "since": ""},
                     {"name": "scholar", "harness": "grok", "kind": "grok", "status": "done",
                      "pane_id": "w2:p1", "workspace_id": "w2", "on_roster": True, "since": "4m"}],
            "cold": [{"name": "kimi-notes", "harness": "oll-mid", "status": "cold", "pane_id": None, "kind": "pi",
                      "last_seen": "2026-09-11", "session_id": "k1"},
                     {"name": "no-session", "harness": "grok", "status": "cold", "pane_id": None, "kind": "grok",
                      "last_seen": "2026-09-12"}],
            "startable": [
                {"harness": "claude", "title": "Claude Code", "installed": True, "trust": {"verdict": "ok"}},
                {"harness": "grok", "title": "Grok CLI", "installed": True, "trust": {"verdict": "overprivilege"}},
                {"harness": "omp", "title": "omp", "installed": False, "trust": {"verdict": "ok"}},
                {"harness": "local-ornith", "title": "ornith", "installed": True, "trust": {"verdict": "blocked"}},
                {"harness": "pi", "title": "pi (bare)", "installed": True, "trust": {"verdict": "unknown"}},
            ],
            "absent": [],
        }

    def test_sections_and_what_is_left_off(self):
        reg = self.reg()
        choices = hm.picker_choices(reg, [], here_ws="w1", ws_labels={"w2": "sys:clubhouse"})
        self.assertEqual([c["section"] for c in choices], ["running", "running", "resume", "start", "start", "start"])
        self.assertEqual([c["target"]["harness"] for c in choices if c["action"] == "raise"], ["claude", "grok", "pi"])
        text = hm.render_picker(reg, choices)
        for needle in ("Running here", "Resume", "Start new", "  1  majordomo", "in sys:clubhouse", "4m",
                       "Claude Code", "! asks first"):
            self.assertIn(needle, text)
        for gone in ("LIVE", "RAISE", "subject", "omp", "local-ornith", "⊘", "?", "w1:p1", "cold", "no-session"):
            self.assertNotIn(gone, text)

    def test_widen_only_when_it_shows_more(self):
        reg = self.reg()
        choices = hm.picker_choices(reg, [])
        self.assertNotIn("widen", hm.render_picker(reg, choices, "/Users/x/code/notes", wider=2))
        self.assertIn("g widen to metawork · 5 running",
                      hm.render_picker(reg, choices, "/Users/x/code/notes", wider=5))

    def test_fold_and_numbers_skip_more_row(self):
        reg = self.reg()
        reg["startable"] = [{"harness": f"h{i}", "title": "", "installed": True, "trust": {"verdict": "ok"}}
                            for i in range(8)]
        choices = hm.picker_choices(reg, [])
        start = [c for c in choices if c["section"] == "start"]
        self.assertEqual(len(start), hm.FOLD)
        self.assertEqual(start[-1]["action"], "more")
        self.assertIn("+ 4 more", start[-1]["line"])
        hm.render_picker(reg, choices)
        self.assertIsNone(start[-1]["idx"])
        self.assertEqual([c["idx"] for c in choices if c["action"] != "more"], list(range(1, 8)))
        self.assertEqual(len([c for c in hm.picker_choices(reg, [], expanded={"start"}) if c["section"] == "start"]), 8)

    def test_rank_here_beats_elsewhere_beats_catalog_order(self):
        roster = [{"harness": "grok", "cwd": "/Users/x/other", "last_seen": "2026-09-12"},
                  {"harness": "grok", "cwd": "/Users/x/other2", "last_seen": "2026-09-12"},
                  {"harness": "pi", "cwd": ROOT + "/sub", "last_seen": "2026-09-01"}]
        self.assertEqual(hm.rank_harnesses(["claude", "grok", "pi", "agy"], roster, ROOT),
                         ["pi", "grok", "claude", "agy"])

    def test_ago(self):
        import datetime as dt
        today = dt.date(2026, 9, 13)
        self.assertEqual(hm.ago("2026-09-13T10:00:00", today), "today")
        self.assertEqual(hm.ago("2026-09-12", today), "yesterday")
        self.assertEqual(hm.ago("2026-09-11", today), "2 days ago")
        self.assertEqual(hm.ago("2026-06-01", today), "Jun 1")
        self.assertEqual(hm.ago("", today), "")

    def test_remember_live_records_unseen_agents_once(self):
        reg = {"live": [{"name": "ballots", "harness": "claude", "kind": "claude", "cwd": ROOT, "pane_id": "w1:p3",
                         "session_id": "s9", "on_roster": False},
                        {"name": "nosession", "harness": "grok", "kind": "grok", "cwd": ROOT, "pane_id": "w1:p4",
                         "session_id": "", "on_roster": False}]}
        rows = [{"name": "ballots", "cwd": "/elsewhere"}]
        saved = []
        orig, hm.save_roster = hm.save_roster, saved.append
        try:
            self.assertEqual(hm.remember_live(reg, rows), 1)
            self.assertEqual(hm.remember_live(reg, rows), 0)
        finally:
            hm.save_roster = orig
        self.assertEqual(rows[-1]["name"], "ballots-2")
        self.assertEqual(rows[-1]["session_id"], "s9")
        self.assertEqual(len(saved), 1)


class Input(unittest.TestCase):
    def test_keys_and_mouse(self):
        evs, tail = hm.parse_input(b"\x1b[A\x1b[Bj\r\x1b[<0;8;11M\x1b[<0;8;11m\x1b[<65;1;2M\x1b[5~")
        self.assertEqual(evs, [("key", "up"), ("key", "down"), ("char", "j"), ("key", "enter"),
                               ("mouse", 0, 8, 11, True), ("mouse", 0, 8, 11, False),
                               ("mouse", 65, 1, 2, True), ("key", "pgup")])
        self.assertEqual(tail, b"")

    def test_lone_esc_waits_and_partial_sequence_is_kept(self):
        self.assertEqual(hm.parse_input(b"\x1b"), ([], b"\x1b"))
        self.assertEqual(hm.parse_input(b"a\x1b[<0;8"), ([("char", "a")], b"\x1b[<0;8"))
        self.assertEqual(hm.parse_input(b"\x1bx")[0], [("key", "esc"), ("char", "x")])

    def test_hint_bar_spans_are_plain_columns(self):
        line, spans = hm.hint_bar([("⏎", "open", "enter"), ("w", "wizard", "w"), ("esc", "close", "esc")])
        self.assertEqual(spans, [(3, 9, "enter"), (12, 20, "w"), (23, 32, "esc")])
        self.assertEqual(hm.visible_len(line), 31)


class Paths(unittest.TestCase):
    """XDG config/state shared by both doors; one-time copy off herdr's plugin dirs (LAB-248)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg, self.state = base / "cfg" / "cowbell", base / "state" / "cowbell"
        self.old_cfg, self.old_state = base / "old-cfg", base / "old-state"
        self.seed_root = base / "seed"
        self.seed_root.mkdir()
        (self.seed_root / "harnesses.toml").write_text('schema = 1\n[[harness]]\nid = "claude"\n')
        self.env = {"COWBELL_CONFIG_DIR": str(self.cfg), "COWBELL_STATE_DIR": str(self.state),
                    "HERDR_PLUGIN_CONFIG_DIR": str(self.old_cfg), "HERDR_PLUGIN_STATE_DIR": str(self.old_state),
                    "HERDR_PLUGIN_ROOT": str(self.seed_root)}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def legacy(self):
        self.old_cfg.mkdir()
        self.old_state.mkdir()
        catalog = b'# house catalog\n[defaults]\nlane = "yellow"  # kept\n\n[[harness]]\nid = "grok"\n'
        (self.old_cfg / "harnesses.toml").write_bytes(catalog)
        rows = [{"name": "butler", "cwd": "/x", "harness": "grok", "session_id": "s1"}]
        (self.old_state / "roster.json").write_text(json.dumps({"harnesses": rows}))
        return catalog, rows

    def test_fresh_run_seeds_new_dirs(self):
        p = hm.catalog_path()
        self.assertEqual(p, self.cfg / "harnesses.toml")
        self.assertEqual(p.read_text(), (self.seed_root / "harnesses.toml").read_text())
        self.assertEqual(hm.load_roster(), [])
        self.assertFalse((self.cfg / ".migrated_from").exists())

    def test_migrates_byte_faithful_once(self):
        catalog, rows = self.legacy()
        self.assertEqual(hm.catalog_path().read_bytes(), catalog)
        self.assertEqual(hm.load_roster(), rows)
        backups = sorted(p.name for p in (self.cfg / "migration-backup").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((self.old_cfg / "harnesses.toml").exists())            # copy, not move
        marker = (self.cfg / ".migrated_from").read_text().splitlines()
        self.assertEqual(json.loads(marker[0])["from"], str(self.old_cfg / "harnesses.toml"))
        (self.old_cfg / "harnesses.toml").write_text("# changed later in the old dir\n")
        hm.catalog_path(); hm.load_roster()                                     # second run
        self.assertEqual(hm.catalog_path().read_bytes(), catalog)
        self.assertEqual(sorted(p.name for p in (self.cfg / "migration-backup").iterdir()), backups)
        self.assertEqual(len((self.cfg / ".migrated_from").read_text().splitlines()), 1)

    def test_missing_seed_and_legacy_means_no_catalog(self):
        (self.seed_root / "harnesses.toml").unlink()
        with self.assertRaises(SystemExit):
            hm.load_catalog()

    def test_xdg_homes_when_no_override(self):
        for k in ("COWBELL_CONFIG_DIR", "COWBELL_STATE_DIR"):
            os.environ.pop(k)
        base = Path(self.tmp.name)
        os.environ["XDG_CONFIG_HOME"], os.environ["XDG_STATE_HOME"] = str(base / "xc"), str(base / "xs")
        try:
            self.assertEqual(hm.config_dir(), base / "xc" / "cowbell")
            self.assertEqual(hm.state_dir(), base / "xs" / "cowbell")
        finally:
            os.environ.pop("XDG_CONFIG_HOME"); os.environ.pop("XDG_STATE_HOME")

    def test_roster_without_schema_is_accepted_and_rewritten(self):
        _, rows = self.legacy()
        hm.save_roster(hm.load_roster())
        data = json.loads((self.state / "roster.json").read_text())
        self.assertEqual(data["schema"], hm.SCHEMA)
        self.assertEqual(data["harnesses"], rows)

    def test_newer_roster_is_read_but_never_written(self):
        self.state.mkdir(parents=True)
        body = json.dumps({"schema": hm.SCHEMA + 1, "harnesses": [{"name": "a", "cwd": "/x"}], "future": True})
        (self.state / "roster.json").write_text(body)
        self.assertEqual([r["name"] for r in hm.load_roster()], ["a"])
        with self.assertRaises(hm.SchemaError):
            hm.save_roster([])
        self.assertEqual((self.state / "roster.json").read_text(), body)

    def test_newer_catalog_loads_and_refuses_writes(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "harnesses.toml").write_text(f'schema = {hm.SCHEMA + 1}\n[[harness]]\nid = "claude"\n')
        cat = hm.load_catalog()
        self.assertEqual(cat["harness"][0]["id"], "claude")
        with self.assertRaises(hm.SchemaError):
            hm.check_writable(cat["schema"], "harnesses.toml")


class Standalone(unittest.TestCase):
    PS = """\
    1     0 ??       /sbin/launchd
  500     1 ??       /Applications/Herdr.app/Contents/MacOS/herdr server
  510   500 ttys001  -zsh
  511   510 ttys001  claude --model claude-opus-5
  600     1 ??       tmux -u new-session -d -s agentdeck_x
  601   600 ttys004  pi
  602   600 ttys005  /Users/x/.local/bin/grok --resume 019f
  603   602 ttys005  node /opt/mcp/server.js
  604   602 ttys005  grok helper
  700     1 ttys009  node /opt/homebrew/bin/gemini
  800     1 ??       /Applications/ChatGPT.app/Contents/Resources/codex app-server
  900     1 ??       claude --print hello
"""

    def test_exe_kind(self):
        self.assertEqual(hm.exe_kind(["claude", "--model", "x"]), "claude")
        self.assertEqual(hm.exe_kind(["/Users/x/.local/bin/grok"]), "grok")
        self.assertEqual(hm.exe_kind(["node", "--no-warnings", "/opt/homebrew/bin/gemini"]), "gemini")
        self.assertEqual(hm.exe_kind(["/Applications/ChatGPT.app/Contents/Resources/codex"]), "")
        self.assertEqual(hm.exe_kind(["-zsh"]), "")

    def test_agent_processes_skip_herdr_nested_and_ttyless(self):
        table = hm.ps_table(self.PS)
        rows = hm.agent_processes(table, cwds=lambda pids: {p: f"/work/{p}" for p in pids})
        self.assertEqual(sorted((r["pid"], r["agent"], r["tty"]) for r in rows),
                         [(601, "pi", "ttys004"), (602, "grok", "ttys005"), (700, "gemini", "ttys009")])
        self.assertEqual(rows[0]["pane_id"], f"pid:{rows[0]['pid']}")

    def test_here_command_session_and_name(self):
        claude = dict(CAT["harness"][0], session_id_flag="--session-id", name_flag="--name")
        cmd, sid = hm.here_command(CAT, claude, "butler", model="claude-opus-5", lane="yellow")
        self.assertEqual(cmd[:3], ["claude", "--session-id", sid])
        self.assertEqual(len(sid), 36)
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[-2:], ["--name", "butler"])
        cmd, sid = hm.here_command(CAT, claude, "butler", resume_id="abc")
        self.assertEqual(sid, "abc")
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")

    def test_launch_routes_by_mode(self):
        calls = []
        orig = hm.exec_here, hm.raise_harness
        hm.exec_here = lambda *a, **k: calls.append("here")
        hm.raise_harness = lambda *a, **k: calls.append("tab")
        try:
            hm.launch(CAT, CAT["harness"][0], "n", ROOT, hm.Ctx(cwd="/", mode="here"))
            hm.launch(CAT, CAT["harness"][0], "n", ROOT, hm.Ctx(cwd="/", mode="tab"))
        finally:
            hm.exec_here, hm.raise_harness = orig
        self.assertEqual(calls, ["here", "tab"])

    def test_in_herdr_pane(self):
        self.assertTrue(hm.in_herdr_pane({"HERDR_PANE_ID": "w1:p1"}))
        self.assertFalse(hm.in_herdr_pane({"TERM": "xterm"}))


class Version(unittest.TestCase):
    def test_version_line_is_parseable(self):
        import io, re
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(hm.main(["--version"]), 0)
        self.assertRegex(buf.getvalue(), r"^cowbell \d+\.\d+\.\d+\n$")

    def test_version_needs_no_herdr(self):
        orig = hm.server_up
        hm.server_up = lambda: (_ for _ in ()).throw(AssertionError("server_up called"))
        try:
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                self.assertEqual(hm.main(["-V"]), 0)
        finally:
            hm.server_up = orig

    def test_version_matches_plugin_manifest(self):
        import tomllib
        manifest = Path(__file__).resolve().parent.parent / "herdr-plugin.toml"
        self.assertEqual(tomllib.loads(manifest.read_text())["version"], hm.__version__)


class Flags(unittest.TestCase):
    def test_parse_flags(self):
        opts, pos = hm.parse_flags(["--harness", "claude", "--lane=green", "--yes", "extra", "--", "--raw"])
        self.assertEqual(opts, {"harness": "claude", "lane": "green", "yes": True})
        self.assertEqual(pos, ["extra", "--raw"])


class Sitting(unittest.TestCase):
    def test_head(self):
        self.assertEqual(hm.sitting_head("builder-minaret"), "builder")
        self.assertEqual(hm.sitting_head("Majordomo"), "majordomo")
        self.assertEqual(hm.sitting_head("coord-cowbell"), "coord")

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
                     {"tab_id": "w1:t2", "label": "cowbell"}],
            "agents": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1",
                 "terminal_title_stripped": "- Inspect grok flags - Grok 4.6 - herdr-cowbell-core - grok"},
                {"pane_id": "w1:p2", "tab_id": "w1:t2",
                 "terminal_title_stripped": "herdr-cowbell-core"},
            ],
        }
        self.assertEqual(hm.live_names(snap), {"builder-minaret", "cowbell"})


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


class Seed(unittest.TestCase):
    def test_grok_lanes_and_claim_flag(self):
        import tomllib
        seed = Path(__file__).resolve().parent.parent / "harnesses.toml"
        cat = tomllib.loads(seed.read_text())
        grok = next(h for h in cat["harness"] if h["id"] == "grok")
        self.assertEqual(grok.get("claim_flag"), "--agent")
        self.assertEqual(cat["lanes"]["grok"]["green"], ["--permission-mode", "plan"])
        self.assertEqual(cat["lanes"]["grok"]["yellow"], ["--permission-mode", "acceptEdits"])

    def test_seed_is_standard_agents_only(self):
        import tomllib
        seed = Path(__file__).resolve().parent.parent / "harnesses.toml"
        cat = tomllib.loads(seed.read_text())
        self.assertFalse([h["id"] for h in cat["harness"] if "command" in h])   # no house wrappers
        self.assertFalse(cat["defaults"]["ask_name"])


if __name__ == "__main__":
    unittest.main()
