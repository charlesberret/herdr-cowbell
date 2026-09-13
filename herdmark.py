#!/usr/bin/env python3
"""herdmark — registry-first harness roster for Herdr, scoped to the directory you are in.

  herdmark                      what is raised here (live · cold · absent), with trust
                                (no args = this registry)
  herdmark <harness>            attach to that harness here, or raise it with defaults
  herdmark new --harness ID [--model M] [--lane L] [--allow T] [--deny T] [--name N]
  herdmark enter <name>         focus if live, else resume in a new tab
  herdmark adopt [name]         put the focused agent pane on the roster
  herdmark stop <name>          ctrl-c the harness; roster keeps it
  herdmark list [--all]         roster rows (default: this directory)
  herdmark import-seats         pull herdr-seats' seats.json into the roster
  herdmark keys install|uninstall
  herdmark open | picker        plugin entry points (keybind / popup)

Stdlib only; python >= 3.11 (tomllib). fzf is optional.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

PLUGIN_ID = "herdmark"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
LANES = ("green", "yellow", "red")
READY = {"idle", "done"}
SHELLS = {"zsh", "bash", "fish", "sh", "dash", "ksh"}
GLYPH = {"local": "⌂", "mesh": "⌂", "vendor": "↑"}  # ⌂ ↑
DASH = "—"
HERDR_CONFIG = Path("~/.config/herdr/config.toml").expanduser()
SEATS_JSON = Path("~/.config/herdr/plugins/config/herdr-seats/seats.json").expanduser()
GROK_AGENTS_DIR = Path("~/.grok/agents").expanduser()
CTX_KEYS = (
    "workspace_id", "workspace_cwd", "tab_id", "tab_label",
    "focused_pane_id", "focused_pane_cwd", "focused_pane_agent",
)


class HerdrError(RuntimeError):
    pass


# --- paths -----------------------------------------------------------------

def plugin_root() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent)


def config_dir() -> Path:
    p = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or f"~/.config/herdr/plugins/config/{PLUGIN_ID}").expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    p = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or f"~/.local/state/herdr/plugins/{PLUGIN_ID}").expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def roster_path() -> Path:
    return state_dir() / "roster.json"


def catalog_path() -> Path:
    p = config_dir() / "harnesses.toml"
    if not p.exists():
        seed = plugin_root() / "harnesses.toml"
        if seed.exists():
            shutil.copy2(seed, p)
    return p


# --- herdr client ------------------------------------------------------------

def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")


def herdr(*args: str, expect_json: bool = True) -> dict:
    proc = subprocess.run([herdr_bin(), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise HerdrError(err or f"herdr {' '.join(args)} exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        if expect_json:
            raise HerdrError(f"herdr {' '.join(args)}: non-JSON output: {out[:120]}")
        return {"raw": out}
    if isinstance(payload, dict) and "error" in payload:
        e = payload["error"]
        raise HerdrError(e.get("message", str(e)) if isinstance(e, dict) else str(e))
    if isinstance(payload, dict) and "result" in payload:
        r = payload["result"]
        return r if isinstance(r, dict) else {"value": r}
    return payload if isinstance(payload, dict) else {"value": payload}


def server_up() -> bool:
    try:
        herdr("workspace", "list")
        return True
    except (HerdrError, OSError):
        return False


# --- context -------------------------------------------------------------------

class Ctx:
    """Where herdmark was invoked from: the caller's pane, tab, workspace, cwd."""

    def __init__(self, workspace_id=None, tab_id=None, pane_id=None, cwd=None, agent=None, mode="tab"):
        self.workspace_id, self.tab_id, self.pane_id = workspace_id, tab_id, pane_id
        self.cwd: str = os.path.realpath(cwd or os.getcwd())
        self.agent, self.mode = agent, mode

    @classmethod
    def from_env(cls, env=None) -> "Ctx":
        env = os.environ if env is None else env
        raw = env.get("HERDMARK_CTX") or env.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        cwd = data.get("focused_pane_cwd") or data.get("workspace_cwd")
        pane_id = data.get("focused_pane_id") or env.get("HERDR_PANE_ID")
        if not cwd and pane_id:
            try:
                cwd = (herdr("pane", "get", pane_id).get("pane") or {}).get("cwd")
            except (HerdrError, OSError):
                cwd = None
        return cls(
            workspace_id=data.get("workspace_id") or env.get("HERDR_WORKSPACE_ID"),
            tab_id=data.get("tab_id") or env.get("HERDR_TAB_ID"),
            pane_id=pane_id,
            cwd=os.path.realpath(cwd or os.getcwd()),
            agent=data.get("focused_pane_agent"),
            mode=env.get("HERDMARK_MODE") or "tab",
        )


def invocation_ctx(env=None) -> dict:
    """The caller's directory and workspace — not the picker popup's own pane."""
    env = os.environ if env is None else env
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON") or env.get("HERDMARK_CTX") or "{}"
    try:
        parsed = json.loads(raw)
        data = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        data = {}
    out = {k: data[k] for k in CTX_KEYS if data.get(k)}
    if not out.get("workspace_id") and env.get("HERDR_WORKSPACE_ID"):
        out["workspace_id"] = env["HERDR_WORKSPACE_ID"]
    if not out.get("tab_id") and env.get("HERDR_TAB_ID"):
        out["tab_id"] = env["HERDR_TAB_ID"]
    pane_id = out.get("focused_pane_id") or env.get("HERDR_PANE_ID")
    if pane_id:
        out["focused_pane_id"] = pane_id
    if not out.get("focused_pane_cwd"):
        cwd = out.get("workspace_cwd")
        if not cwd and pane_id:
            try:
                cwd = (herdr("pane", "get", pane_id).get("pane") or {}).get("cwd")
            except (HerdrError, OSError):
                cwd = None
        if cwd:
            out["focused_pane_cwd"] = cwd
    return out


def picker_open_args(ctx: dict) -> list[str]:
    args = [
        "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "picker", "--focus",
        "--env", "HERDMARK_CTX=" + json.dumps(ctx),
        "--env", "HERDMARK_MODE=tab",
    ]
    if ctx.get("focused_pane_cwd"):
        args += ["--cwd", ctx["focused_pane_cwd"]]
    return args


# --- scope -----------------------------------------------------------------

def at_or_beneath(path: str, root: str) -> bool:
    """A pane above the root never counts — a workspace at ~ would match every project."""
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def git_root(path: str) -> str | None:
    proc = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def seat_file(root: str, home: str | None = None) -> Path | None:
    """Nearest SEAT.md walking up from root, stopping at $HOME (never inject home)."""
    home = os.path.realpath(home or Path.home())
    d = os.path.realpath(root)
    while True:
        cand = Path(d) / "SEAT.md"
        if cand.is_file():
            return cand
        if d in (home, "/"):
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (s or "").strip().lower()).strip("-")[:32]
    if not s or not s[0].isalpha():
        s = ("h-" + s)[:32].rstrip("-")
    return s if NAME_RE.match(s) else "harness"


def sitting_head(name: str) -> str:
    """First word of a sitting (`builder-minaret` → `builder`): the claim, not the note."""
    return slug((name or "").split("-")[0])


def unique_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while True:
        cand = f"{base[:29]}-{n}" if len(base) > 29 else f"{base}-{n}"
        if cand not in taken:
            return cand
        n += 1


# --- catalog -------------------------------------------------------------------

def load_catalog() -> dict:
    if tomllib is None:
        raise SystemExit("herdmark: python >= 3.11 required (tomllib)")
    p = catalog_path()
    if not p.exists():
        raise SystemExit(f"herdmark: no catalog at {p}")
    with open(p, "rb") as fh:
        cat = tomllib.load(fh)
    cat.setdefault("defaults", {})
    cat.setdefault("lanes", {})
    for h in cat.get("harness", []):
        h.setdefault("kind", h["id"])
        h.setdefault("family", h["kind"])
        h.setdefault("egress", "vendor")
        h.setdefault("trust_tool", h["id"])
        h.setdefault("models", [])
        h.setdefault("resume", [])
        if "command" in h:
            h["command"] = os.path.expanduser(h["command"])
    return cat


def harness_by_id(cat: dict, hid: str) -> dict | None:
    for h in cat.get("harness", []):
        if h["id"] == hid:
            return h
    return None


def is_wrapper(h: dict) -> bool:
    return bool(h.get("command"))


def installed(h: dict) -> bool:
    return os.path.exists(h["command"]) if is_wrapper(h) else bool(shutil.which(h["kind"]))


# --- roster --------------------------------------------------------------------

def load_roster() -> list[dict]:
    p = roster_path()
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("harnesses", []) if isinstance(data, dict) else data


def save_roster(rows: list[dict]) -> None:
    rows.sort(key=lambda r: (r.get("cwd", ""), r.get("name", "")))
    tmp = roster_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"harnesses": rows}, indent=2) + "\n")
    os.replace(tmp, roster_path())


def roster_here(rows: list[dict], root: str) -> list[dict]:
    return [r for r in rows if r.get("cwd") and at_or_beneath(r["cwd"], root)]


def import_seats(seats_path: Path = SEATS_JSON) -> int:
    if not seats_path.exists():
        return 0
    data = json.loads(seats_path.read_text())
    seats = data.get("seats", data) if isinstance(data, dict) else data
    rows = load_roster()
    names = {r["name"] for r in rows}
    added = 0
    for s in seats:
        if s.get("name") in names:
            continue
        kind = s.get("kind") or ""
        rows.append({
            "name": s["name"], "cwd": s.get("cwd", ""), "harness": kind, "kind": kind,
            "model": "", "lane": "", "session_id": s.get("session_id", ""),
            "pane_id": s.get("pane_id"), "workspace_id": s.get("workspace_id"),
            "label": s.get("label", s["name"]), "created": now(), "source": "herdr-seats",
        })
        names.add(s["name"]); added += 1
    save_roster(rows)
    return added


def now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# --- live ----------------------------------------------------------------------

def live_agents() -> list[dict]:
    try:
        return herdr("agent", "list").get("agents") or []
    except HerdrError:
        return []


def live_panes() -> list[dict]:
    try:
        return herdr("pane", "list").get("panes") or []
    except HerdrError:
        return []


def workspaces() -> list[dict]:
    try:
        return herdr("workspace", "list").get("workspaces") or []
    except HerdrError:
        return []


def snapshot() -> dict:
    try:
        res = herdr("api", "snapshot")
    except (HerdrError, OSError):
        return {}
    snap = res.get("snapshot")
    return snap if isinstance(snap, dict) else {}


def tab_labels(snap: dict | None = None) -> dict[str, str]:
    snap = snapshot() if snap is None else snap
    return {t["tab_id"]: (t.get("label") or "") for t in snap.get("tabs") or [] if t.get("tab_id")}


# --- trust (delegated to agent-deck-trust; never re-encoded here) --------------------

def trust_available() -> bool:
    return shutil.which("agent-deck-trust") is not None


def trust_subject(root: str) -> str:
    if not trust_available():
        return "?"
    proc = subprocess.run(["agent-deck-trust", "subject", root], capture_output=True, text=True)
    return proc.stdout.strip() or "?"


def parse_match(text: str, code: int) -> dict:
    verdict = flag = ""
    for line in text.splitlines():
        m = re.match(r"^\s*verdict:\s+(\S+)\s*(.*)$", line)
        if m:
            verdict, flag = m.group(1), m.group(2).strip()
        m = re.match(r"^\s*flag:\s+(.*)$", line)
        if m and m.group(1).strip() not in ("(none)", ""):
            flag = m.group(1).strip()
    if not verdict:
        verdict = {0: "ok", 3: "overprivilege", 4: "blocked"}.get(code, "unknown")
    return {"verdict": verdict, "flag": flag, "exit": code}


def trust_match(tool: str, root: str) -> dict:
    if not trust_available():
        return {"verdict": "unknown", "flag": "?", "exit": -1}
    proc = subprocess.run(["agent-deck-trust", "match", tool, root], capture_output=True, text=True)
    if proc.returncode not in (0, 3, 4):
        return {"verdict": "unknown", "flag": "?", "exit": proc.returncode}
    return parse_match(proc.stdout, proc.returncode)


def trust_badge(tool: str) -> str:
    if not trust_available():
        return "?"
    proc = subprocess.run(["agent-deck-trust", "badge", tool], capture_output=True, text=True)
    return proc.stdout.strip() or "?"


def verdict_mark(v: dict) -> str:
    return {"ok": "ok", "overprivilege": "!", "blocked": "⊘", "unknown": "?"}.get(v.get("verdict") or "unknown", "?")


# --- registry ------------------------------------------------------------------

def build_registry(cat: dict, root: str, agents: list[dict], panes: list[dict], roster: list[dict],
                   matcher=None, subject: str = "", tab_label: dict | None = None) -> dict:
    """Partition the herd for one root: live here, cold here, absent here."""
    matcher = matcher or (lambda tool: trust_match(tool, root))
    labels = tab_label or {}
    live = [a for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], root)]
    live_by_pane = {a["pane_id"]: a for a in live}
    pane_ids = {p.get("pane_id") for p in panes}
    rows_here = roster_here(roster, root)
    by_pane: dict[str, dict] = {str(r["pane_id"]): r for r in rows_here if r.get("pane_id")}

    live_rows, seen_pane = [], set()
    for a in live:
        r = by_pane.get(a["pane_id"])
        lab = labels.get(a.get("tab_id"), "")
        live_rows.append({
            "name": ((r or {}).get("name") or (slug(lab) if lab else "")
                     or slug(a.get("terminal_title_stripped") or a.get("agent") or "agent")),
            "harness": (r or {}).get("harness") or a.get("agent"),
            "kind": a.get("agent"), "model": (r or {}).get("model", ""), "lane": (r or {}).get("lane", ""),
            "status": a.get("agent_status") or "live", "pane_id": a["pane_id"], "tab_id": a.get("tab_id"),
            "workspace_id": a.get("workspace_id"), "cwd": a.get("cwd"),
            "session_id": (a.get("agent_session") or {}).get("value") or (r or {}).get("session_id", ""),
            "on_roster": r is not None,
        })
        seen_pane.add(a["pane_id"])

    cold_rows = []
    for r in rows_here:
        if r.get("pane_id") in seen_pane:
            continue
        pane_alive = r.get("pane_id") in pane_ids and r.get("pane_id") not in live_by_pane
        cold_rows.append({**r, "status": "unknown" if pane_alive else "cold", "pane_alive": pane_alive})

    present = {x["harness"] for x in live_rows} | {x.get("harness") for x in cold_rows}
    absent = []
    tools = sorted({str(h["trust_tool"]) for h in cat.get("harness", []) if h["id"] not in present})
    verdicts: dict[str, dict] = {}
    if tools:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for tool, v in zip(tools, pool.map(matcher, tools)):
                verdicts[tool] = v
    for h in cat.get("harness", []):
        if h["id"] in present:
            continue
        absent.append({"harness": h["id"], "kind": h["kind"], "family": h["family"], "egress": h["egress"],
                       "installed": installed(h), "trust": verdicts.get(h["trust_tool"], {"verdict": "unknown"})})
    return {"root": root, "subject": subject, "live": live_rows, "cold": cold_rows, "absent": absent}


def render_registry(reg: dict, git_top: str | None = None, wider: int | None = None) -> str:
    out = [f"herdmark  {tilde(reg['root'])}   subject: {reg['subject'] or '?'}"]
    if git_top and os.path.realpath(git_top) != os.path.realpath(reg["root"]):
        hint = f"   ({wider} live under it)" if wider is not None else ""
        out.append(f"  git root: {tilde(git_top)}{hint}   [g] widen")
    idx = 0
    if reg["live"]:
        out.append("")
        out.append("  LIVE HERE")
        for r in reg["live"]:
            idx += 1
            r["idx"] = idx
            tag = "" if r["on_roster"] else "  (not on roster)"
            out.append(f"  {idx:>3}  {r['name']:<24} {r['harness']:<14} {r['status']:<9} {r['pane_id']:<8} "
                       f"{(r.get('session_id') or '')[:8]:<8} {tilde(r['cwd'])}{tag}")
    if reg["cold"]:
        out.append("")
        out.append("  COLD HERE (resumable)")
        for r in reg["cold"]:
            idx += 1
            r["idx"] = idx
            out.append(f"  {idx:>3}  {r['name']:<24} {r.get('harness') or r.get('kind'):<14} {r['status']:<9} "
                       f"{r.get('pane_id') or DASH:<8} {(r.get('session_id') or '')[:8]:<8} {tilde(r['cwd'])}")
    if not reg["live"] and not reg["cold"]:
        out.append("")
        out.append("  nothing raised here yet")
    if reg["absent"]:
        out.append("")
        out.append("  ABSENT HERE  (glyph: ⌂ local  ↑ vendor · verdict: ok  ! overprivilege  ⊘ blocked  ? unknown)")
        bits = []
        for a in reg["absent"]:
            mark = verdict_mark(a["trust"])
            miss = "" if a["installed"] else " (not installed)"
            bits.append(f"{a['harness']} {GLYPH.get(a['egress'], '?')}{mark}{miss}")
        line, lines = "  ", []
        for b in bits:
            if len(line) + len(b) + 3 > 100:
                lines.append(line.rstrip(" ·"))
                line = "  "
            line += b + " · "
        lines.append(line.rstrip(" ·"))
        out.extend(lines)
    return "\n".join(out)


def picker_choices(reg: dict) -> list[dict]:
    """Popup rows: live, cold, then raiseable absent (installed and not blocked)."""
    out: list[dict] = []
    for r in reg["live"]:
        tag = "" if r.get("on_roster") else "  (not on roster)"
        out.append({"action": "focus", "target": r,
                    "line": f"{r['name']:<22} {r['harness']:<12} {r['status']:<8} {r['pane_id']}{tag}"})
    for r in reg["cold"]:
        out.append({"action": "enter", "target": r,
                    "line": f"{r['name']:<22} {r.get('harness') or r.get('kind') or '':<12} {r['status']:<8} {r.get('pane_id') or DASH}"})
    for a in reg["absent"]:
        if not a.get("installed") or a.get("trust", {}).get("verdict") == "blocked":
            continue
        mark = GLYPH.get(a["egress"], "?") + verdict_mark(a["trust"])
        out.append({"action": "raise", "target": a, "line": f"{a['harness']:<22} {mark}"})
    return out


def render_picker(reg: dict, choices: list[dict], git_top: str | None = None, wider: int | None = None) -> str:
    """New-tab surface: one numbered list, then it should close."""
    out = [f"herdmark  {tilde(reg['root'])}   subject: {reg['subject'] or '?'}"]
    if git_top and os.path.realpath(git_top) != os.path.realpath(reg["root"]):
        hint = f"   ({wider} live under it)" if wider is not None else ""
        out.append(f"  git root: {tilde(git_top)}{hint}   [g] widen")
    n = 0
    sections = (("focus", "LIVE"), ("enter", "COLD"),
                ("raise", "RAISE  (yellow · sitting asked once)"))
    for action, title in sections:
        rows = [c for c in choices if c["action"] == action]
        if not rows:
            continue
        out.append("")
        out.append(f"  {title}")
        for c in rows:
            n += 1
            c["idx"] = n
            out.append(f"  {n:>3}  {c['line']}")
    blocked = [a["harness"] for a in reg["absent"]
               if a.get("trust", {}).get("verdict") == "blocked"]
    missing = [a["harness"] for a in reg["absent"]
               if not a.get("installed") and a.get("trust", {}).get("verdict") != "blocked"]
    if blocked or missing:
        out.append("")
        if blocked:
            out.append("  ⊘  " + " · ".join(blocked))
        if missing:
            out.append("     not installed: " + " · ".join(missing))
    return "\n".join(out)


# --- launch --------------------------------------------------------------------

def claim_argv(h: dict, sitting: str, agents_dir: Path | None = None) -> list[str]:
    """Grok `--agent <member>` is the claim. Pass only when a definition file exists."""
    flag = h.get("claim_flag")
    if not flag or not sitting or is_wrapper(h):
        return []
    word = sitting_head(sitting)
    if not word or not NAME_RE.match(word):
        return []
    if h.get("kind") == "grok":
        d = Path(agents_dir) if agents_dir is not None else GROK_AGENTS_DIR
        if not (d / f"{word}.md").is_file():
            return []
    return [flag, word]


def build_argv(cat: dict, h: dict, model: str = "", lane: str = "", allow: str = "", deny: str = "",
               seat: Path | None = None, resume_id: str = "", sitting: str = "",
               extra: list[str] | None = None, agents_dir: Path | None = None) -> list[str]:
    """Agent-side argv (after `--` for native kinds; after the wrapper for house wrappers)."""
    argv: list[str] = []
    if is_wrapper(h):
        if resume_id:
            argv += list(h.get("resume") or ["--continue"])
        return argv + list(extra or [])
    if model and h.get("model_flag"):
        argv += list(h["model_flag"]) + [model]
    lane_map = cat.get("lanes", {}).get(h["kind"], {})
    if lane and lane in lane_map:
        argv += list(lane_map[lane])
    if allow and h.get("tools_allow_flag"):
        argv += [h["tools_allow_flag"], allow]
    if deny and h.get("tools_deny_flag"):
        argv += [h["tools_deny_flag"], deny]
    if seat and h.get("seat_flag"):
        argv += [h["seat_flag"], str(seat)]  # claude takes -file; pi's --append-system-prompt accepts a path
    argv += claim_argv(h, sitting, agents_dir=agents_dir)
    if resume_id:
        spec = list(h.get("resume") or [])
        if spec == ["resume"]:
            argv += ["resume", resume_id]
        elif spec and spec[0].endswith("="):
            argv += [spec[0] + resume_id]
        elif spec:
            argv += spec + [resume_id]
    return argv + list(extra or [])


def shell_join(parts: list[str]) -> str:
    import shlex
    return " ".join(shlex.quote(p) for p in parts)


def pick_workspace(ctx: Ctx, root: str) -> str | None:
    """The caller's workspace when it has a pane at/under root; else any such workspace."""
    panes = live_panes()
    if ctx.workspace_id and any(p.get("workspace_id") == ctx.workspace_id and p.get("cwd") and at_or_beneath(p["cwd"], root) for p in panes):
        return ctx.workspace_id
    for p in panes:
        if p.get("cwd") and at_or_beneath(p["cwd"], root):
            return p.get("workspace_id")
    return ctx.workspace_id


def create_tab(workspace_id: str | None, root: str, label: str) -> dict:
    """New tab labelled with the sitting. Identity is the tab, never pane --title / rename."""
    if workspace_id:
        res = herdr("tab", "create", "--workspace", workspace_id, "--cwd", root, "--label", label, "--no-focus")
    else:
        res = herdr("workspace", "create", "--cwd", root, "--label", os.path.basename(root) or root, "--no-focus")
        tab = res.get("tab") or {}
        tid = tab.get("tab_id")
        if tid and (tab.get("label") or "") != label:
            try:
                herdr("tab", "rename", tid, label)
            except HerdrError:
                pass
    pane = res.get("root_pane") or {}
    tab = res.get("tab") or {}
    pid = pane.get("pane_id")
    if not pid:
        raise HerdrError(f"no root pane returned: {res}")
    return {
        "pane_id": pid,
        "tab_id": pane.get("tab_id") or tab.get("tab_id"),
        "workspace_id": pane.get("workspace_id") or tab.get("workspace_id") or workspace_id,
    }


def shell_ready_from_process(info: dict) -> bool:
    """True when the pane's foreground is a single shell — the contract for `agent start`."""
    proc = info.get("process_info") or info
    fg = proc.get("foreground_processes") or []
    if len(fg) != 1:
        return False
    name = (fg[0].get("name") or "").lstrip("-")
    return name in SHELLS


def wait_shell(pane_id: str, timeout_ms: int = 10000) -> None:
    """A just-created tab is often still spawning; agent start then returns agent_pane_busy."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if shell_ready_from_process(herdr("pane", "process-info", "--pane", pane_id)):
                return
        except (HerdrError, OSError):
            pass
        time.sleep(0.15)


def start_native(name: str, kind: str, pane_id: str, argv: list[str]) -> dict:
    wait_shell(pane_id)
    args = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "30000"]
    if argv:
        args += ["--", *argv]
    last: HerdrError | None = None
    for _ in range(5):
        try:
            return herdr(*args)
        except HerdrError as e:
            last = e
            msg = str(e)
            if any(k in msg for k in ("not_ready", "blocked", "agent_not_ready")):
                return {}
            if "agent_pane_busy" in msg or "not an available shell" in msg:
                time.sleep(0.4)
                continue
            raise
    raise last or HerdrError(f"agent start {name} failed")


def raise_harness(cat: dict, h: dict, name: str, root: str, ctx: Ctx, model="", lane="", allow="", deny="",
                  seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None,
                  focus: bool = True) -> dict:
    argv = build_argv(cat, h, model, lane, allow, deny, seat, resume_id, sitting=name, extra=extra)
    ws = pick_workspace(ctx, root)
    pane = create_tab(ws, root, name)
    pid = pane["pane_id"]
    launched_session = ""
    try:
        if is_wrapper(h):
            wait_shell(pid)
            herdr("pane", "run", pid, shell_join([h["command"], *argv]), expect_json=False)
        else:
            res = start_native(name, h["kind"], pid, argv)
            launched_session = ((res.get("agent") or {}).get("agent_session") or {}).get("value") or ""
    except HerdrError:
        if pane.get("tab_id"):  # do not leave an empty tab behind a failed launch
            try:
                herdr("tab", "close", pane["tab_id"])
            except HerdrError:
                pass
        raise
    rows = [r for r in load_roster() if r.get("name") != name]
    rows.append({
        "name": name, "cwd": root, "harness": h["id"], "kind": h["kind"], "model": model, "lane": lane,
        "allow": allow, "deny": deny, "seat": str(seat) if seat else "",
        "session_id": launched_session or resume_id, "pane_id": pid, "tab_id": pane.get("tab_id"),
        "workspace_id": pane.get("workspace_id"), "created": now(), "argv": argv,
    })
    save_roster(rows)
    if focus and pane.get("tab_id"):
        try:
            herdr("tab", "focus", pane["tab_id"])
        except HerdrError:
            pass
    return pane


def live_names(snap: dict | None = None) -> set[str]:
    """Taken sittings: tab labels of live agent panes (the sidebar identity token)."""
    snap = snapshot() if snap is None else snap
    labels = tab_labels(snap)
    names: set[str] = set()
    for a in snap.get("agents") or []:
        lab = slug(labels.get(a.get("tab_id"), "") or "")
        if lab:
            names.add(lab)
    return names


def name_is_live(name: str) -> dict | None:
    try:
        info = herdr("agent", "get", name)
    except HerdrError:
        return None
    return info.get("agent") or info


def refresh_sessions(rows: list[dict]) -> None:
    """Fill session ids the agent only reported after launch; match by pane."""
    by_pane = {a["pane_id"]: a for a in live_agents() if a.get("pane_id")}
    today = now()[:10]
    changed = False
    for r in rows:
        a = by_pane.get(r.get("pane_id") or "")
        if a:
            sid = (a.get("agent_session") or {}).get("value")
            if sid and sid != r.get("session_id"):
                r["session_id"] = sid; changed = True
            if r.get("last_seen") != today:
                r["last_seen"] = today; changed = True
    if changed:
        save_roster(rows)


# --- commands ------------------------------------------------------------------

def cmd_registry(ctx: Ctx, cat: dict, root: str | None = None) -> dict:
    root = root or ctx.cwd
    rows = load_roster()
    refresh_sessions(rows)
    agents, panes = live_agents(), live_panes()
    reg = build_registry(cat, root, agents, panes, rows, subject=trust_subject(root),
                         tab_label=tab_labels())
    top = git_root(root)
    wider = None
    if top and os.path.realpath(top) != os.path.realpath(root):
        wider = sum(1 for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], top))
    print(render_registry(reg, top, wider))
    return reg


def cmd_enter(ctx: Ctx, cat: dict, name: str, root: str | None = None) -> None:
    rows = load_roster()
    hits = [r for r in rows if r["name"] == name] or [r for r in rows if name in r["name"]]
    if len(hits) != 1:
        raise SystemExit(f"herdmark: {'no' if not hits else 'ambiguous'} roster entry {name!r}")
    r = hits[0]
    live = name_is_live(r["name"])
    if live and live.get("pane_id"):
        herdr("agent", "focus", live["pane_id"])
        print(f"focused {r['name']} in {live['pane_id']}")
        return
    for a in live_agents():
        if a.get("pane_id") == r.get("pane_id"):
            herdr("agent", "focus", a["pane_id"])
            print(f"focused {r['name']} in {a['pane_id']}")
            return
    h = harness_by_id(cat, r.get("harness") or r.get("kind") or "")
    if not h:
        raise SystemExit(f"herdmark: {r['name']} uses harness {r.get('harness')!r} which is not in the catalog")
    seat = Path(r["seat"]) if r.get("seat") and Path(r["seat"]).is_file() else None
    pane = raise_harness(cat, h, r["name"], r.get("cwd") or root or ctx.cwd, ctx, r.get("model", ""), r.get("lane", ""),
                         r.get("allow", ""), r.get("deny", ""), seat, resume_id=r.get("session_id", ""))
    print(f"resumed {r['name']} ({h['id']}) in {pane['pane_id']}")


def cmd_adopt(ctx: Ctx, cat: dict, name: str | None = None) -> None:
    pane_id = ctx.pane_id
    if not pane_id:
        focused = [a for a in live_agents() if a.get("focused")]
        pane_id = focused[0]["pane_id"] if focused else None
    if not pane_id:
        raise SystemExit("herdmark: no focused pane to adopt")
    info = herdr("agent", "get", pane_id)
    a = info.get("agent") or info
    kind = a.get("agent") or ""
    if not kind:
        raise SystemExit(f"herdmark: pane {pane_id} hosts no recognized agent")
    sitting = tab_labels().get(a.get("tab_id"), "")
    title = a.get("terminal_title_stripped") or ""
    noisy = (not title or title.startswith("-") or " - grok " in title.lower()
             or "waiting" in title.lower() or "running:" in title.lower())
    base = slug(name or sitting or (title if not noisy else kind))
    rows = load_roster()
    nm = unique_name(base, {r["name"] for r in rows if r.get("pane_id") != a.get("pane_id")})
    rows = [r for r in rows if r.get("pane_id") != a.get("pane_id")]
    h = harness_by_id(cat, kind)
    rows.append({
        "name": nm, "cwd": a.get("cwd") or ctx.cwd, "harness": h["id"] if h else kind, "kind": kind,
        "model": "", "lane": "", "session_id": (a.get("agent_session") or {}).get("value") or "",
        "pane_id": a.get("pane_id") or pane_id, "tab_id": a.get("tab_id"), "workspace_id": a.get("workspace_id"),
        "label": title or nm, "created": now(), "source": "adopt",
    })
    save_roster(rows)
    print(f"adopted {nm}  {kind}  {tilde(a.get('cwd') or ctx.cwd)}")


def cmd_stop(name: str) -> None:
    rows = load_roster()
    hits = [r for r in rows if r["name"] == name]
    if not hits:
        raise SystemExit(f"herdmark: no roster entry {name!r}")
    live = name_is_live(name) or {}
    pid = live.get("pane_id") or hits[0].get("pane_id")
    if not pid:
        raise SystemExit(f"herdmark: {name} is not live")
    herdr("agent", "send-keys", pid, "ctrl+c")
    print(f"ctrl+c → {name} ({pid})")


def cmd_list(ctx: Ctx, all_rows: bool) -> None:
    rows = load_roster()
    refresh_sessions(rows)
    by_pane = {a["pane_id"]: a for a in live_agents() if a.get("pane_id")}
    rows = rows if all_rows else roster_here(rows, ctx.cwd)
    if not rows:
        print("no roster entries" + ("" if all_rows else f" under {tilde(ctx.cwd)}  (try --all)"))
        return
    print(f"{'name':<24} {'harness':<14} {'status':<9} {'pane':<8} {'session':<8} cwd")
    for r in rows:
        a = by_pane.get(r.get("pane_id") or "")
        st = (a or {}).get("agent_status") or ("live" if a else "cold")
        print(f"{r['name']:<24} {r.get('harness') or r.get('kind'):<14} {st:<9} {r.get('pane_id') or DASH:<8} "
              f"{(r.get('session_id') or '')[:8]:<8} {tilde(r.get('cwd', ''))}")


def gate(cat_h: dict, root: str, interactive: bool, assume_yes: bool) -> dict:
    v = trust_match(cat_h["trust_tool"], root)
    if v["verdict"] == "blocked":
        raise SystemExit(f"herdmark: refused — {cat_h['id']} on {tilde(root)}: blocked {v['flag']} (T4 under-clearance)")
    if v["verdict"] == "overprivilege":
        msg = f"{cat_h['id']} on {tilde(root)}: overprivilege {v['flag']} (surface exceeds subject; visible, not silent)"
        if interactive and not assume_yes:
            if input(f"  ! {msg}\n  proceed? [y/N] ").strip().lower() != "y":
                raise SystemExit("herdmark: not raised")
        else:
            print(f"herdmark: ! {msg}", file=sys.stderr)
    return v


def cmd_new(ctx: Ctx, cat: dict, hid: str, model="", lane="", allow="", deny="", name="", no_seat=False,
            assume_yes=False, root: str | None = None, extra: list[str] | None = None, focus: bool = True) -> None:
    root = root or ctx.cwd
    h = harness_by_id(cat, hid)
    if not h:
        raise SystemExit(f"herdmark: unknown harness {hid!r} (see: herdmark catalog)")
    if not installed(h):
        raise SystemExit(f"herdmark: {hid} is not installed here ({h.get('command') or h['kind']})")
    lane = lane or ("" if is_wrapper(h) else cat["defaults"].get("lane", "yellow"))
    if lane and lane not in LANES:
        raise SystemExit(f"herdmark: lane must be one of {', '.join(LANES)}")
    model = model or (h["models"][0] if h["models"] else "")
    seat = None if (no_seat or not h.get("seat_flag")) else seat_file(root)
    gate(h, root, interactive=False, assume_yes=assume_yes)
    base = slug(name or h["id"])
    nm = unique_name(base, live_names() | {r["name"] for r in load_roster()})
    pane = raise_harness(cat, h, nm, root, ctx, model, lane, allow, deny, seat, extra=extra, focus=focus)
    print(f"raised {nm}  {h['id']}{(' ' + model) if model else ''}{(' ' + lane) if lane else ''}  in {pane['pane_id']}  {tilde(root)}")


def cmd_dispatch(ctx: Ctx, cat: dict, hid: str, assume_yes: bool) -> None:
    """`herdmark claude`: attach to that harness here if one exists, else raise it with defaults."""
    root = ctx.cwd
    reg = build_registry(cat, root, live_agents(), live_panes(), load_roster(), matcher=lambda _tool: {"verdict": "unknown"})
    for r in reg["live"]:
        if r["harness"] == hid or r["kind"] == hid:
            herdr("agent", "focus", r["pane_id"])
            print(f"focused {r['name']} ({hid}) in {r['pane_id']}")
            return
    for r in reg["cold"]:
        if (r.get("harness") or r.get("kind")) == hid:
            cmd_enter(ctx, cat, r["name"], root)
            return
    cmd_new(ctx, cat, hid, assume_yes=assume_yes, root=root)


def cmd_catalog(cat: dict) -> None:
    print(f"{'harness':<16} {'kind':<8} {'egress':<7} {'installed':<9} {'badge':<10} title")
    for h in cat.get("harness", []):
        print(f"{h['id']:<16} {h['kind']:<8} {GLYPH.get(h['egress'], '?') + ' ' + h['egress']:<7} "
              f"{'yes' if installed(h) else 'no':<9} {trust_badge(h['trust_tool']):<10} {h.get('title', '')}")


# --- keybinding install (text-level, atomic, reversible) ---------------------------------

MARK = "# herdmark"
BLOCK_BEGIN, BLOCK_END = "# >>> herdmark keybindings >>>", "# <<< herdmark keybindings <<<"
SAVED = "# herdmark saved: "
BLOCK = [
    BLOCK_BEGIN,
    "[[keys.command]]",
    'key = "prefix+c"',
    'type = "plugin_action"',
    'command = "herdmark.open"',
    'description = "herdmark: new tab from the herd registry"',
    BLOCK_END,
]


def _strip_block(lines: list[str]) -> list[str]:
    out, inside = [], False
    for ln in lines:
        s = ln.strip()
        if s == BLOCK_BEGIN:
            inside = True; continue
        if s == BLOCK_END:
            inside = False; continue
        if not inside:
            out.append(ln)
    return out


def keys_install_text(text: str) -> str:
    """Blank the built-in new_tab (saving the original) and bind prefix+c to herdmark.open."""
    lines = _strip_block(text.splitlines())
    lines = [ln for ln in lines if not ln.rstrip().endswith(MARK)]
    for i, ln in enumerate(lines):
        if ln.strip().startswith(SAVED):
            lines[i] = ln.strip()[len(SAVED):]
    managed = f'new_tab = ""  {MARK}'
    keys_at = next((i for i, ln in enumerate(lines) if ln.strip() == "[keys]"), None)
    if keys_at is None:
        # TOML: the [keys] table must precede any [[keys.command]] entry. Insert it
        # before that entry's attached comment lines, and add no blank lines of our
        # own, so uninstall (remove the marked lines) restores the file byte-for-byte.
        first_cmd = next((i for i, ln in enumerate(lines) if ln.strip() == "[[keys.command]]"), None)
        at = first_cmd if first_cmd is not None else len(lines)
        while at > 0 and lines[at - 1].strip().startswith("#"):
            at -= 1
        lines[at:at] = [f"[keys]  {MARK}", managed]
    else:
        end = next((i for i in range(keys_at + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        hit = next((i for i in range(keys_at + 1, end) if re.match(r"^\s*new_tab\s*=", lines[i])), None)
        if hit is None:
            lines.insert(keys_at + 1, managed)
        else:
            lines[hit:hit + 1] = [SAVED + lines[hit].strip(), managed]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines + [""] + BLOCK) + "\n"


def keys_uninstall_text(text: str) -> str:
    lines = _strip_block(text.splitlines())
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(SAVED):
            out.append(s[len(SAVED):]); continue
        if s.endswith(MARK):
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def write_atomic(path: Path, text: str) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode if target.exists() else None
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_keys(action: str, path: Path = HERDR_CONFIG) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new = keys_install_text(text) if action == "install" else keys_uninstall_text(text)
    if new == text:
        print(f"{path}: already {action}ed")
        return
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    n = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}.{n}"); n += 1
    if path.exists():
        shutil.copy2(path, backup)
    write_atomic(path, new)
    print(f"{action}ed prefix+c binding in {path} (backup: {backup.name})")
    print("run: herdr server reload-config")


# --- ui --------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def choose(title: str, items: list[str], default: int = 0) -> int | None:
    """Numbered choice. No fzf: --height=100% ate the popup."""
    if not items:
        return None
    print(f"  {title}")
    for i, s in enumerate(items):
        print(f"  {i + 1:>3}  {s}{'   (default)' if i == default else ''}")
    raw = ask("  number (empty = default, q = cancel): ", str(default + 1))
    if raw.lower() == "q":
        return None
    try:
        i = int(raw) - 1
    except ValueError:
        return None
    return i if 0 <= i < len(items) else None


def wizard(ctx: Ctx, cat: dict, root: str) -> bool:
    hs = [h for h in cat.get("harness", [])]
    subject = trust_subject(root)
    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = dict(zip([h["id"] for h in hs], pool.map(lambda h: trust_match(h["trust_tool"], root), hs)))
    rows = []
    for h in hs:
        v = verdicts[h["id"]]
        rows.append(f"{h['id']:<16} {GLYPH.get(h['egress'], '?')} {verdict_mark(v):<2} "
                    f"{'' if installed(h) else '(not installed) '}{h.get('title', '')}")
    print(f"\n  raise a harness in {tilde(root)}   subject: {subject}")
    i = choose("harness", rows)
    if i is None:
        return False
    h = hs[i]
    if not installed(h):
        print(f"  {h['id']} is not installed here"); return False
    v = verdicts[h["id"]]
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {h['id']} on {tilde(root)} is blocked {v['flag']} (T4 under-clearance)"); return False
    taken = live_names() | {r["name"] for r in load_roster()}
    base = unique_name(slug(h["id"]), taken)
    name = slug(ask(f"  sitting (tab name) [{base}]: ", base))
    name = unique_name(name, taken)
    model = ""
    if h["models"]:
        j = choose("model", h["models"] + ["(type another)"])
        if j is None:
            return False
        model = h["models"][j] if j < len(h["models"]) else ask("  model id: ")
    lane = ""
    if not is_wrapper(h):
        default_lane = cat["defaults"].get("lane", "yellow")
        lane_rows = [f"{ln:<7} {' '.join(cat['lanes'].get(h['kind'], {}).get(ln, [])) or '(kind default)'}" for ln in LANES]
        j = choose("lane", lane_rows, default=LANES.index(default_lane) if default_lane in LANES else 1)
        if j is None:
            return False
        lane = LANES[j]
    seat = seat_file(root) if h.get("seat_flag") else None
    if v["verdict"] == "overprivilege":
        if ask(f"  ! overprivilege {v['flag']}: {h['id']} exceeds what {subject} work warrants. proceed? [y/N] ").lower() != "y":
            return False
    argv = build_argv(cat, h, model, lane, "", "", seat, sitting=name)
    shown = shell_join([h["command"], *argv]) if is_wrapper(h) else shell_join([h["kind"], *argv])
    print(f"\n  {name}  →  {shown}\n  new tab named {name} in {tilde(root)}")
    pane = raise_harness(cat, h, name, root, ctx, model, lane, "", "", seat)
    print(f"  raised {name} in {pane['pane_id']}")
    return True


def raise_quick(ctx: Ctx, cat: dict, hid: str, root: str) -> bool:
    """Numbered RAISE: defaults, one sitting prompt, then close."""
    h = harness_by_id(cat, hid)
    if not h:
        print(f"  unknown harness {hid}"); return False
    if not installed(h):
        print(f"  {hid} is not installed here"); return False
    v = trust_match(h["trust_tool"], root)
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {hid} on {tilde(root)} is blocked {v['flag']}"); return False
    taken = live_names() | {r["name"] for r in load_roster()}
    base = unique_name(slug(h["id"]), taken)
    lane = "" if is_wrapper(h) else cat["defaults"].get("lane", "yellow")
    print(f"  {h['id']}{(' ' + lane) if lane else ''}  →  new tab in {tilde(root)}")
    raw = ask(f"  sitting (tab name) [{base}]  (q = cancel): ", base)
    if raw.lower() == "q":
        return False
    name = unique_name(slug(raw), taken)
    if v["verdict"] == "overprivilege":
        if ask(f"  ! overprivilege {v['flag']}: proceed? [y/N] ").lower() != "y":
            return False
    model = h["models"][0] if h["models"] else ""
    seat = seat_file(root) if h.get("seat_flag") else None
    pane = raise_harness(cat, h, name, root, ctx, model, lane, "", "", seat)
    print(f"  raised {name} in {pane['pane_id']}")
    return True


def picker(ctx: Ctx, cat: dict) -> int:
    root = ctx.cwd
    while True:
        rows = load_roster()
        refresh_sessions(rows)
        agents, panes = live_agents(), live_panes()
        top = git_root(root)
        wider = None
        if top and os.path.realpath(top) != os.path.realpath(root):
            wider = sum(1 for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], top))
        reg = build_registry(cat, root, agents, panes, rows, subject=trust_subject(root),
                             tab_label=tab_labels())
        choices = picker_choices(reg)
        print()
        print(render_picker(reg, choices, top, wider))
        print()
        print("  number = attach or raise   [w] wizard   [a] adopt   [g] widen   [q]")
        raw = ask("  > ").strip().lower()
        if raw in ("", "q"):
            return 0
        if raw == "r":
            continue
        if raw == "g":
            if top:
                root = top
            continue
        if raw in ("w", "n"):  # n = old "new harness" key
            try:
                if wizard(ctx, cat, root):
                    return 0
            except (HerdrError, SystemExit) as e:
                print(f"  {e}")
            continue
        if raw == "a":
            try:
                cmd_adopt(ctx, cat)
                return 0
            except (HerdrError, SystemExit) as e:
                print(f"  {e}")
            continue
        if raw.isdigit():
            hit = next((c for c in choices if c.get("idx") == int(raw)), None)
            if not hit:
                continue
            try:
                if hit["action"] == "focus":
                    herdr("agent", "focus", hit["target"]["pane_id"])
                    return 0
                if hit["action"] == "enter":
                    cmd_enter(ctx, cat, hit["target"]["name"], root)
                    return 0
                if hit["action"] == "raise":
                    if raise_quick(ctx, cat, hit["target"]["harness"], root):
                        return 0
            except (HerdrError, SystemExit) as e:
                print(f"  {e}")


def cmd_open(env=None) -> int:
    """Action entry: open the popup, forwarding the caller's context (the popup's own is not the caller's)."""
    try:
        herdr(*picker_open_args(invocation_ctx(env)))
    except HerdrError as e:
        print(f"herdmark: {e}", file=sys.stderr)
        return 1
    return 0


# --- main ------------------------------------------------------------------------

def parse_flags(args: list[str]) -> tuple[dict, list[str]]:
    opts, rest = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i + 1:]; break
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1); opts[k] = v
        elif a in ("--yes", "-y", "--all", "--no-seat", "--no-focus", "-i"):
            opts[a.lstrip("-")] = True
        elif a.startswith("--") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]; i += 1
        else:
            rest.append(a)
        i += 1
    return opts, rest


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    if not argv:
        argv = ["registry"]
    cmd, *rest = argv
    if cmd == "keys":
        cmd_keys(rest[0] if rest else "install"); return 0
    if cmd == "open":
        return cmd_open()
    if not server_up():
        print("herdmark: no Herdr server (is `herdr` running?)", file=sys.stderr); return 2
    ctx = Ctx.from_env()
    cat = load_catalog()
    opts, pos = parse_flags(rest)
    if cmd == "picker":
        return picker(ctx, cat)
    if cmd in ("registry", "here", "status"):
        cmd_registry(ctx, cat, opts.get("root")); return 0
    if cmd == "list":
        cmd_list(ctx, bool(opts.get("all"))); return 0
    if cmd == "catalog":
        cmd_catalog(cat); return 0
    if cmd == "import-seats":
        print(f"imported {import_seats()} seats from herdr-seats"); return 0
    if cmd == "adopt":
        cmd_adopt(ctx, cat, pos[0] if pos else None); return 0
    if cmd == "enter":
        if not pos:
            raise SystemExit("enter <name>")
        cmd_enter(ctx, cat, pos[0]); return 0
    if cmd == "stop":
        if not pos:
            raise SystemExit("stop <name>")
        cmd_stop(pos[0]); return 0
    if cmd == "new":
        hid = opts.get("harness") or (pos[0] if pos else "")
        if not hid:
            raise SystemExit("new --harness <id> [--model M] [--lane L] [--allow T] [--deny T] [--name N] [--no-seat] [--yes]")
        cmd_new(ctx, cat, hid, opts.get("model", ""), opts.get("lane", ""), opts.get("allow", ""), opts.get("deny", ""),
                opts.get("name", ""), bool(opts.get("no-seat")), bool(opts.get("yes")), opts.get("root"),
                extra=pos[1:] if pos and pos[0] == hid else pos, focus=not opts.get("no-focus"))
        return 0
    if harness_by_id(cat, cmd):
        cmd_dispatch(ctx, cat, cmd, bool(opts.get("yes"))); return 0
    raise SystemExit(f"herdmark: unknown command or harness {cmd!r} (see: herdmark catalog)")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except HerdrError as e:
        print(f"herdmark: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
