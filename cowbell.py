#!/usr/bin/env python3
"""cowbell — call up a sitting here. Herdr plugin herdr-cowbell, and a standalone TUI.

  cowbell                       on a terminal: the picker (running here · resume · start new)
                                piped: the long registry, same as `cowbell status`
  cowbell --version             one line, `cowbell X.Y.Z` (the plugin shim compares it)
  cowbell status                what is raised here (live · cold · absent), with trust
  cowbell <harness>             attach to that harness here, or raise it with defaults
  cowbell new --harness ID [--model M] [--lane L] [--allow T] [--deny T] [--name N]
  cowbell enter <name>          focus if live, else resume
  cowbell adopt [name]          put the focused agent pane on the roster (herdr)
  cowbell stop <name>           ctrl-c the harness; roster keeps it (herdr)

Inside a herdr pane, starting or resuming opens a new herdr tab. Anywhere else it
runs the agent in this terminal, and Running here also lists agent processes found
by working directory.

  cowbell list [--all]          roster rows (default: this directory)
  cowbell import-seats          pull herdr-seats' seats.json into the roster
  cowbell keys install|uninstall
  cowbell open | picker         plugin entry points (keybind / popup)

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

__version__ = "0.3.0"  # keep equal to herdr-plugin.toml `version` (tested)
PLUGIN_ID = "herdr-cowbell"
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


# --- colour ------------------------------------------------------------------
# House Kettle Dark (~/.config/herdr/config.toml [theme.custom]). Mostly grey
# hierarchy; gold is spent on the one thing to press (the numbers and the prompt).
# State words borrow the sidebar's chroma; red only for a trust warning.

INK, SECONDARY, MUTED, FAINT = "#d8d8e0", "#a9a9b2", "#8a8a92", "#6c6d74"
GOLD, RED, TEAL = "#c8a24a", "#d4756b", "#389bb4"
# working stays ink, not the sidebar's running gold: here gold means "press this".
STATUS_FG = {"working": INK, "blocked": RED, "done": TEAL, "idle": MUTED, "cold": FAINT}


def color_on(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("COWBELL_COLOR") in ("1", "always"):
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


def paint(text: str, fg: str = "", bold: bool = False) -> str:
    """Style already-padded text; plain when colour is off (pipes, tests, NO_COLOR)."""
    if not text or not color_on():
        return text
    codes = ["1"] if bold else []
    if fg:
        codes.append("38;2;{};{};{}".format(*(int(fg[i:i + 2], 16) for i in (1, 3, 5))))
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes else text


def key(n, width: int = 3) -> str:
    return paint(f"{n:>{width}}", GOLD, bold=True)


def status_cell(status: str, width: int = 8) -> str:
    return paint(f"{status:<{width}}", STATUS_FG.get(status, MUTED))


def verdict_cell(v: dict) -> str:
    mark = verdict_mark(v)
    return paint(mark, RED if mark == "!" else FAINT if mark == "ok" else MUTED, bold=mark == "!")


def header(reg: dict, git_top: str | None, wider: int | None, widen_hint: bool = True) -> list[str]:
    out = [f"  {paint('cowbell', INK, bold=True)}  {paint(tilde(reg['root']), SECONDARY)}   "
           f"{paint('subject:', FAINT)} {paint(reg['subject'] or '?', MUTED)}"]
    if git_top and os.path.realpath(git_top) != os.path.realpath(reg["root"]):
        hint = f"   ({wider} live under it)" if wider is not None else ""
        out.append(paint(f"  git root: {tilde(git_top)}{hint}", FAINT)
                   + (f"   {paint('[g]', MUTED)} {paint('widen', FAINT)}" if widen_hint else ""))
    return out


def section_title(title: str) -> str:
    return f"  {paint(title, MUTED, bold=True)}"


def section(title: str, note: str = "") -> str:
    return f"  {paint(title, MUTED, bold=True)}" + (f"  {paint(note, FAINT)}" if note else "")


# --- paths -----------------------------------------------------------------

def plugin_root() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent)


SCHEMA = 1  # catalog and roster format this cowbell reads and writes


class SchemaError(SystemExit):
    pass


def config_dir() -> Path:
    """~/.config/cowbell (COWBELL_CONFIG_DIR, then XDG_CONFIG_HOME). Shared by the plugin and the CLI."""
    base = os.environ.get("COWBELL_CONFIG_DIR") or \
        Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "cowbell"
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    """~/.local/state/cowbell (COWBELL_STATE_DIR, then XDG_STATE_HOME)."""
    base = os.environ.get("COWBELL_STATE_DIR") or \
        Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "cowbell"
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_config_dir() -> Path:
    """Where 0.1–0.3 kept the catalog (herdr's plugin dir). Read only, as a migration source."""
    return Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
                or f"~/.config/herdr/plugins/config/{PLUGIN_ID}").expanduser()


def legacy_state_dir() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_STATE_DIR")
                or f"~/.local/state/herdr/plugins/{PLUGIN_ID}").expanduser()


def migrate_file(new: Path, old: Path) -> bool:
    """Copy (never move) old -> new once, byte-faithful; keep a dated backup and a marker. True if copied."""
    if new.exists() or not old.is_file() or old.resolve() == new.resolve():
        return False
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = new.parent / "migration-backup" / f"{old.name}.{stamp}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old, backup)
    tmp = new.with_name(new.name + ".tmp")
    shutil.copy2(old, tmp)
    os.replace(tmp, new)
    marker = new.parent / ".migrated_from"
    with open(marker, "a") as fh:
        fh.write(json.dumps({"file": new.name, "from": str(old), "backup": str(backup), "at": now()}) + "\n")
    return True


def check_writable(schema, what: str) -> None:
    """A file written by a newer cowbell is read, never overwritten."""
    if isinstance(schema, int) and schema > SCHEMA:
        raise SchemaError(f"cowbell: {what} is schema {schema}; cowbell {__version__} writes schema {SCHEMA}. "
                          f"Upgrade cowbell before changing it.")


def roster_path() -> Path:
    p = state_dir() / "roster.json"
    migrate_file(p, legacy_state_dir() / "roster.json")
    return p


def catalog_path() -> Path:
    p = config_dir() / "harnesses.toml"
    if not migrate_file(p, legacy_config_dir() / "harnesses.toml") and not p.exists():
        seed = plugin_root() / "harnesses.toml"
        if seed.exists():
            shutil.copy2(seed, p)
    return p


# --- herdr client ------------------------------------------------------------

def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")


def herdr(*args: str, expect_json: bool = True) -> dict:
    try:
        proc = subprocess.run([herdr_bin(), *args], capture_output=True, text=True, check=False)
    except OSError as e:  # herdr not installed: standalone cowbell still works
        raise HerdrError(f"herdr unavailable: {e}") from e
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
    """Where cowbell was invoked from: the caller's pane, tab, workspace, cwd."""

    def __init__(self, workspace_id=None, tab_id=None, pane_id=None, cwd=None, agent=None, mode="tab"):
        self.workspace_id, self.tab_id, self.pane_id = workspace_id, tab_id, pane_id
        self.cwd: str = os.path.realpath(cwd or os.getcwd())
        self.agent, self.mode = agent, mode

    @classmethod
    def from_env(cls, env=None) -> "Ctx":
        env = os.environ if env is None else env
        raw = env.get("COWBELL_CTX") or env.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}"
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
            mode=env.get("COWBELL_MODE") or "tab",
        )


def invocation_ctx(env=None) -> dict:
    """The caller's directory and workspace — not the picker popup's own pane."""
    env = os.environ if env is None else env
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON") or env.get("COWBELL_CTX") or "{}"
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
        "--env", "COWBELL_CTX=" + json.dumps(ctx),
        "--env", "COWBELL_MODE=tab",
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
        raise SystemExit("cowbell: python >= 3.11 required (tomllib)")
    p = catalog_path()
    if not p.exists():
        raise SystemExit(f"cowbell: no catalog at {p}")
    with open(p, "rb") as fh:
        cat = tomllib.load(fh)
    cat.setdefault("schema", 1)  # a catalog without the key predates it and is schema 1
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

def roster_schema(p: Path | None = None) -> int:
    p = p or roster_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return SCHEMA
    return data.get("schema", 1) if isinstance(data, dict) else 1


def load_roster() -> list[dict]:
    p = roster_path()
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("harnesses", []) if isinstance(data, dict) else data


def save_roster(rows: list[dict]) -> None:
    p = roster_path()
    check_writable(roster_schema(p), str(p))
    rows.sort(key=lambda r: (r.get("cwd", ""), r.get("name", "")))
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "harnesses": rows}, indent=2) + "\n")
    os.replace(tmp, p)


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
            # turn clock, only where a sidebar reporter publishes one ($since while working, $total when done)
            "since": ((a.get("tokens") or {}).get("since") or (a.get("tokens") or {}).get("total") or "").strip(),
            "pid": a.get("pid"), "tty": a.get("tty", ""),
        })
        seen_pane.add(a["pane_id"])

    cold_rows = []
    for r in rows_here:
        if r.get("pane_id") in seen_pane:
            continue
        pane_alive = r.get("pane_id") in pane_ids and r.get("pane_id") not in live_by_pane
        cold_rows.append({**r, "status": "unknown" if pane_alive else "cold", "pane_alive": pane_alive})

    present = {x["harness"] for x in live_rows} | {x.get("harness") for x in cold_rows}
    absent, startable = [], []
    tools = sorted({str(h["trust_tool"]) for h in cat.get("harness", [])})
    verdicts: dict[str, dict] = {}
    if tools:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for tool, v in zip(tools, pool.map(matcher, tools)):
                verdicts[tool] = v
    for h in cat.get("harness", []):
        row = {"harness": h["id"], "kind": h["kind"], "family": h["family"], "egress": h["egress"],
               "title": h.get("title", ""), "installed": installed(h),
               "trust": verdicts.get(h["trust_tool"], {"verdict": "unknown"})}
        startable.append(row)
        if h["id"] not in present:
            absent.append(row)
    return {"root": root, "subject": subject, "live": live_rows, "cold": cold_rows, "absent": absent,
            "startable": startable}


def render_registry(reg: dict, git_top: str | None = None, wider: int | None = None) -> str:
    out = header(reg, git_top, wider)
    idx = 0
    if reg["live"]:
        out.append("")
        out.append(section("LIVE HERE"))
        for r in reg["live"]:
            idx += 1
            r["idx"] = idx
            tag = "" if r["on_roster"] else "  (not on roster)"
            name, harness = f"{r['name']:<24}", f"{r['harness']:<14}"
            rest = f"{r['pane_id']:<8} {(r.get('session_id') or '')[:8]:<8} {tilde(r['cwd'])}{tag}"
            out.append(f"  {key(idx)}  {paint(name, INK, bold=True)} {paint(harness, MUTED)} "
                       f"{status_cell(r['status'], 9)} {paint(rest, FAINT)}")
    if reg["cold"]:
        out.append("")
        out.append(section("COLD HERE", "resumable"))
        for r in reg["cold"]:
            idx += 1
            r["idx"] = idx
            name, harness = f"{r['name']:<24}", f"{r.get('harness') or r.get('kind'):<14}"
            rest = f"{r.get('pane_id') or DASH:<8} {(r.get('session_id') or '')[:8]:<8} {tilde(r['cwd'])}"
            out.append(f"  {key(idx)}  {paint(name, SECONDARY)} {paint(harness, MUTED)} "
                       f"{status_cell(r['status'], 9)} {paint(rest, FAINT)}")
    if not reg["live"] and not reg["cold"]:
        out.append("")
        out.append(paint("  nothing raised here yet", FAINT))
    if reg["absent"]:
        out.append("")
        out.append(section("ABSENT HERE", "⌂ local  ↑ vendor · ok  ! overprivilege  ⊘ blocked  ? unknown"))
        bits = []  # (plain width, painted)
        for a in reg["absent"]:
            mark = verdict_mark(a["trust"])
            miss = "" if a["installed"] else " (not installed)"
            glyph = GLYPH.get(a["egress"], "?")
            plain = f"{a['harness']} {glyph}{mark}{miss}"
            name = paint(a["harness"], FAINT if not a["installed"] or mark == "⊘" else SECONDARY)
            m = paint(mark, RED) if mark == "⊘" else verdict_cell(a["trust"])
            bits.append((len(plain), f"{name} {paint(glyph, FAINT)}{m}{paint(miss, FAINT)}"))
        sep = paint(" · ", FAINT)
        width, row, lines = 2, [], []
        for w, b in bits:
            if row and width + w + 3 > 100:
                lines.append("  " + sep.join(row))
                width, row = 2, []
            row.append(b)
            width += w + 3
        lines.append("  " + sep.join(row))
        out.extend(lines)
    return "\n".join(out)


FOLD = 5  # a section longer than this shows FOLD-1 rows and a "+ N more" row


def rank_harnesses(ids: list[str], roster: list[dict], root: str) -> list[str]:
    """Most used here first (a start at or beneath root counts 3), then anywhere, then catalog order."""
    order = {hid: i for i, hid in enumerate(ids)}
    score: dict[str, int] = {}
    latest: dict[str, str] = {}
    for r in roster:
        hid = r.get("harness") or r.get("kind") or ""
        if hid not in order:
            continue
        score[hid] = score.get(hid, 0) + (3 if r.get("cwd") and at_or_beneath(r["cwd"], root) else 1)
        latest[hid] = max(latest.get(hid, ""), r.get("last_seen") or r.get("created") or "")
    out = sorted(ids, key=lambda h: latest.get(h, ""), reverse=True)  # stable: ties keep catalog order
    return sorted(out, key=lambda h: -score.get(h, 0))


def ago(stamp: str, today: _dt.date | None = None) -> str:
    """Roster dates are days (last_seen) or ISO seconds (created); say it the way a person would."""
    if not stamp:
        return ""
    try:
        day = _dt.date.fromisoformat(stamp[:10])
    except ValueError:
        return ""
    days = ((today or _dt.date.today()) - day).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 30 \
        else day.strftime("%b %-d")


def fold(rows: list[dict], section: str, expanded: set[str]) -> list[dict]:
    if section in expanded or len(rows) <= FOLD:
        return rows
    return rows[:FOLD - 1] + [{"action": "more", "section": section, "key": f"more:{section}",
                               "hidden": len(rows) - (FOLD - 1),
                               "line": paint(f"+ {len(rows) - (FOLD - 1)} more", FAINT)}]


def picker_choices(reg: dict, roster: list[dict] | None = None, here_ws: str | None = None,
                   ws_labels: dict[str, str] | None = None, expanded: set[str] | None = None) -> list[dict]:
    """Popup rows: running here, resume, start new. Only what changes a choice is on the row."""
    expanded = expanded or set()
    ws_labels = ws_labels or {}
    live: list[dict] = []
    for r in sorted(reg["live"], key=lambda r: bool(here_ws) and r.get("workspace_id") != here_ws):
        name, kind = f"{r['name']:<22}", f"{r.get('kind') or r['harness'] or '':<11}"
        where = ws_labels.get(r.get("workspace_id") or "", "") if r.get("workspace_id") != here_ws else ""
        where = f"in {where}" if where else f"on {r['tty']}" if r.get("tty") else ""
        clock = f"{r.get('since', ''):<6}" if any(x.get("since") for x in reg["live"]) else ""
        extra = f"{clock}{where}".rstrip()
        live.append({"action": "focus", "target": r, "key": f"focus:{r['pane_id']}",
                     "line": f"{paint(name, INK, bold=True)} {paint(kind, MUTED)} {status_cell(r['status'])}"
                             + (f" {paint(extra, FAINT)}" if extra else "")})
    # Resume only what can resume: a row without a session id would just start fresh.
    cold = sorted((r for r in reg["cold"] if r.get("session_id")),
                  key=lambda r: r.get("last_seen") or r.get("created") or "", reverse=True)
    resume = []
    for r in cold:
        name, kind = f"{r['name']:<22}", f"{r.get('harness') or r.get('kind') or '':<11}"
        resume.append({"action": "enter", "target": r, "key": f"enter:{r['name']}",
                       "line": f"{paint(name, SECONDARY)} {paint(kind, MUTED)} "
                               f"{paint(ago(r.get('last_seen') or r.get('created') or ''), FAINT)}"})
    pool = {a["harness"]: a for a in reg.get("startable", reg["absent"])
            if a.get("installed") and a.get("trust", {}).get("verdict") != "blocked"}
    start = []
    ranked = rank_harnesses(list(pool), roster or [], reg["root"])
    shown = ranked if "start" in expanded or len(ranked) <= FOLD else ranked[:FOLD - 1]
    title_w = min(34, max((len(pool[h].get("title") or "") for h in shown), default=0))
    for hid in ranked:
        a = pool[hid]
        warn = f"   {paint('!', RED, bold=True)} {paint('asks first', FAINT)}" \
            if a.get("trust", {}).get("verdict") == "overprivilege" else ""
        start.append({"action": "raise", "target": a, "key": f"raise:{hid}",
                      "line": f"{paint(f'{hid:<22}', SECONDARY)} "
                              + paint(f"{a.get('title') or '':<{title_w}}" if warn else a.get("title") or "", MUTED) + warn})
    out = []
    for section, rows in (("running", live), ("resume", resume), ("start", start)):
        out += [dict(c, section=section) for c in fold(rows, section, expanded)]
    return out


def render_picker(reg: dict, choices: list[dict], git_top: str | None = None, wider: int | None = None,
                  width: int = 84) -> str:
    """New-tab surface: running here, resume, start new. Numbers are for rows you can open."""
    path = tilde(reg["root"])
    right = ""
    if git_top and wider is not None and wider > len(reg["live"]):
        right = f"g widen to {os.path.basename(git_top.rstrip('/'))} · {wider} running"
    gap = width - 2 - len(path) - len(right)
    out = [f"  {paint(path, SECONDARY, bold=True)}" + (f"{' ' * max(3, gap)}{paint(right, FAINT)}" if right else "")]
    n = 0
    for section, title in (("running", "Running here"), ("resume", "Resume"), ("start", "Start new")):
        rows = [c for c in choices if c.get("section") == section]
        if not rows:
            continue
        out.append("")
        out.append(section_title(title))
        for c in rows:
            c["row"] = len(out)
            if c["action"] == "more":
                c["idx"] = None
                out.append(f"       {c['line']}")
                continue
            n += 1
            c["idx"] = n
            out.append(f"  {key(n)}  {c['line']}")
    if not choices:
        out += ["", paint("  nothing to start here: no agents from harnesses.toml are installed", FAINT)]
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


# --- standalone (outside herdr) -----------------------------------------------------
# Same picker in any terminal. Running here = herdr's agents (if a server is up) plus agent
# processes found by cwd; starting or resuming hands this terminal to the agent (exec).

AGENT_EXES = {"claude", "codex", "grok", "gemini", "agy", "opencode", "pi", "cursor-agent", "kimi", "amp",
              "copilot", "qwen", "droid", "omp", "kilo", "cline", "devin", "hermes", "kiro", "muse", "maki",
              "mastracode", "qodercli"}
INTERPRETERS = {"node", "bun", "deno", "python", "python3"}


def in_herdr_pane(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("HERDR_PANE_ID") or env.get("HERDR_PLUGIN_CONTEXT_JSON") or env.get("COWBELL_CTX"))


def ps_table(text: str | None = None) -> dict[int, dict]:
    """pid -> {ppid, tty, argv}. `text` is `ps -axo pid=,ppid=,tty=,args=` output (for tests)."""
    if text is None:
        text = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,args="], capture_output=True, text=True).stdout
    out = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        out[int(parts[0])] = {"ppid": int(parts[1]), "tty": parts[2], "argv": parts[3].split()}
    return out


def exe_kind(argv: list[str]) -> str:
    """Which agent CLI a command line is, or "". App bundles are desktop apps, not terminal agents."""
    if not argv or ".app/" in argv[0]:
        return ""
    head = os.path.basename(argv[0])
    if head in INTERPRETERS or re.match(r"python3\.\d+$", head):
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        head = os.path.basename(script)
    return head if head in AGENT_EXES else ""


def process_cwds(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    if Path("/proc").is_dir():
        out = {}
        for pid in pids:
            try:
                out[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
        return out
    proc = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                          capture_output=True, text=True)
    out, pid = {}, None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def agent_processes(table: dict[int, dict] | None = None, cwds=process_cwds) -> list[dict]:
    """Agent CLIs on a terminal, not under herdr (herdr lists its own) and not nested in another agent."""
    table = ps_table() if table is None else table
    kinds = {pid: exe_kind(p["argv"]) for pid, p in table.items()}

    def outside(pid: int) -> bool:
        seen, cur = set(), table[pid]["ppid"]
        while cur in table and cur not in seen:
            seen.add(cur)
            if kinds.get(cur) or os.path.basename(table[cur]["argv"][0]) == "herdr":
                return False
            cur = table[cur]["ppid"]
        return True

    found = [pid for pid, k in kinds.items()
             if k and table[pid]["tty"] not in ("??", "?", "-") and outside(pid)]
    where = cwds(found)
    return [{"pane_id": f"pid:{pid}", "pid": pid, "tty": table[pid]["tty"], "agent": kinds[pid],
             "cwd": where[pid], "agent_status": "running", "tab_id": None, "workspace_id": None,
             "terminal_title_stripped": kinds[pid], "agent_session": {}}
            for pid in found if pid in where]


def here_command(cat: dict, h: dict, name: str, model="", lane="", allow="", deny="", seat: Path | None = None,
                 resume_id: str = "", extra: list[str] | None = None) -> tuple[list[str], str]:
    """Full command line for this terminal, and the session id cowbell can record for a later resume."""
    import uuid
    argv = build_argv(cat, h, model, lane, allow, deny, seat, resume_id, sitting=name, extra=extra)
    session = resume_id
    if not resume_id and h.get("session_id_flag") and not is_wrapper(h):
        session = str(uuid.uuid4())
        argv = [h["session_id_flag"], session] + argv
    if name and h.get("name_flag") and not is_wrapper(h):
        argv += [h["name_flag"], name]
    return ([h["command"]] if is_wrapper(h) else [h["kind"]]) + argv, session


def exec_here(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", model="", lane="", allow="", deny="",
              seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None, focus: bool = True):
    """Hand this terminal to the agent. Does not return."""
    cmd, session = here_command(cat, h, name, model, lane, allow, deny, seat, resume_id, extra)
    exe = cmd[0] if is_wrapper(h) else shutil.which(cmd[0])
    if not exe:
        raise SystemExit(f"cowbell: {cmd[0]} is not on PATH")
    rows = [r for r in load_roster() if r.get("name") != name]
    rows.append({"name": name, "cwd": root, "harness": h["id"], "kind": h["kind"], "model": model, "lane": lane,
                 "allow": allow, "deny": deny, "seat": str(seat) if seat else "", "session_id": session,
                 "pane_id": None, "created": now(), "last_seen": now()[:10], "argv": cmd[1:], "source": "here"})
    save_roster(rows)
    sys.stdout.write("\x1b[H\x1b[2J" if sys.stdout.isatty() else "")
    print(f"  {paint(name, INK, bold=True)}  {paint(shell_join(cmd), MUTED)}  {paint(tilde(root), FAINT)}")
    sys.stdout.flush()
    os.chdir(root)
    os.execv(exe, cmd)


def launch(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", *args, **kw):
    """Start or resume: a new herdr tab from inside herdr, this terminal from outside."""
    if ctx.mode == "here":
        return exec_here(cat, h, name, root, ctx, *args, **kw)
    return raise_harness(cat, h, name, root, ctx, *args, **kw)


# --- commands ------------------------------------------------------------------

def cmd_registry(ctx: Ctx, cat: dict, root: str | None = None) -> dict:
    root = root or ctx.cwd
    rows = load_roster()
    refresh_sessions(rows)
    agents = live_agents() + (agent_processes() if ctx.mode == "here" else [])
    panes = live_panes()
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
        raise SystemExit(f"cowbell: {'no' if not hits else 'ambiguous'} roster entry {name!r}")
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
        raise SystemExit(f"cowbell: {r['name']} uses harness {r.get('harness')!r} which is not in the catalog")
    seat = Path(r["seat"]) if r.get("seat") and Path(r["seat"]).is_file() else None
    pane = launch(cat, h, r["name"], r.get("cwd") or root or ctx.cwd, ctx, r.get("model", ""), r.get("lane", ""),
                         r.get("allow", ""), r.get("deny", ""), seat, resume_id=r.get("session_id", ""))
    print(f"resumed {r['name']} ({h['id']}) in {pane['pane_id']}")


def cmd_adopt(ctx: Ctx, cat: dict, name: str | None = None) -> None:
    pane_id = ctx.pane_id
    if not pane_id:
        focused = [a for a in live_agents() if a.get("focused")]
        pane_id = focused[0]["pane_id"] if focused else None
    if not pane_id:
        raise SystemExit("cowbell: no focused pane to adopt")
    info = herdr("agent", "get", pane_id)
    a = info.get("agent") or info
    kind = a.get("agent") or ""
    if not kind:
        raise SystemExit(f"cowbell: pane {pane_id} hosts no recognized agent")
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
        raise SystemExit(f"cowbell: no roster entry {name!r}")
    live = name_is_live(name) or {}
    pid = live.get("pane_id") or hits[0].get("pane_id")
    if not pid:
        raise SystemExit(f"cowbell: {name} is not live")
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
        raise SystemExit(f"cowbell: refused — {cat_h['id']} on {tilde(root)}: blocked {v['flag']} (T4 under-clearance)")
    if v["verdict"] == "overprivilege":
        msg = f"{cat_h['id']} on {tilde(root)}: overprivilege {v['flag']} (surface exceeds subject; visible, not silent)"
        if interactive and not assume_yes:
            if input(f"  ! {msg}\n  proceed? [y/N] ").strip().lower() != "y":
                raise SystemExit("cowbell: not raised")
        else:
            print(f"cowbell: ! {msg}", file=sys.stderr)
    return v


def cmd_new(ctx: Ctx, cat: dict, hid: str, model="", lane="", allow="", deny="", name="", no_seat=False,
            assume_yes=False, root: str | None = None, extra: list[str] | None = None, focus: bool = True) -> None:
    root = root or ctx.cwd
    h = harness_by_id(cat, hid)
    if not h:
        raise SystemExit(f"cowbell: unknown harness {hid!r} (see: cowbell catalog)")
    if not installed(h):
        raise SystemExit(f"cowbell: {hid} is not installed here ({h.get('command') or h['kind']})")
    lane = lane or ("" if is_wrapper(h) else cat["defaults"].get("lane", "yellow"))
    if lane and lane not in LANES:
        raise SystemExit(f"cowbell: lane must be one of {', '.join(LANES)}")
    model = model or (h["models"][0] if h["models"] else "")
    seat = None if (no_seat or not h.get("seat_flag")) else seat_file(root)
    gate(h, root, interactive=False, assume_yes=assume_yes)
    base = slug(name or h["id"])
    nm = unique_name(base, live_names() | {r["name"] for r in load_roster()})
    pane = launch(cat, h, nm, root, ctx, model, lane, allow, deny, seat, extra=extra, focus=focus)
    print(f"raised {nm}  {h['id']}{(' ' + model) if model else ''}{(' ' + lane) if lane else ''}  in {pane['pane_id']}  {tilde(root)}")


def cmd_dispatch(ctx: Ctx, cat: dict, hid: str, assume_yes: bool) -> None:
    """`cowbell claude`: attach to that harness here if one exists, else raise it with defaults."""
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

MARK = "# cowbell"
BLOCK_BEGIN, BLOCK_END = "# >>> herdr-cowbell keybindings >>>", "# <<< herdr-cowbell keybindings <<<"
SAVED = "# cowbell saved: "
BLOCK = [
    BLOCK_BEGIN,
    "[[keys.command]]",
    'key = "prefix+c"',
    'type = "plugin_action"',
    'command = "herdr-cowbell.open"',
    'description = "cowbell: new tab from the herd registry"',
    BLOCK_END,
]


def _strip_block(lines: list[str]) -> list[str]:
    out, inside = [], False
    begins = {BLOCK_BEGIN, "# >>> herdmark keybindings >>>"}
    ends = {BLOCK_END, "# <<< herdmark keybindings <<<"}
    for ln in lines:
        s = ln.strip()
        if s in begins:
            inside = True; continue
        if s in ends:
            inside = False; continue
        if not inside:
            out.append(ln)
    return out


def keys_install_text(text: str) -> str:
    """Blank the built-in new_tab (saving the original) and bind prefix+c to herdr-cowbell.open."""
    lines = _strip_block(text.splitlines())
    old_mark, old_saved = "# herdmark", "# herdmark saved: "
    lines = [ln for ln in lines if not ln.rstrip().endswith(MARK) and not ln.rstrip().endswith(old_mark)]
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(SAVED):
            lines[i] = s[len(SAVED):]
        elif s.startswith(old_saved):
            lines[i] = s[len(old_saved):]
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
        if s.startswith("# herdmark saved: "):
            out.append(s[len("# herdmark saved: "):]); continue
        if s.endswith(MARK) or s.endswith("# herdmark"):
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


# --- tui -------------------------------------------------------------------------
# Herdr is mouse-first. The popup is a plain terminal, so the menu speaks the
# terminal's own dialect: raw keys, SGR mouse (1006), alternate screen. Stdlib only.
# Free-text prompts (sitting name, y/N) still run cooked, outside the menu.

SELECT_BG = "#233954"  # theme.custom selection_bg
ANSI_RE = re.compile(r"\x1b\[[0-9;?<]*[A-Za-z]")
KEYS = {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left", b"[H": "home", b"[F": "end",
        b"OA": "up", b"OB": "down", b"OC": "right", b"OD": "left", b"OH": "home", b"OF": "end",
        b"[5~": "pgup", b"[6~": "pgdn", b"[1~": "home", b"[4~": "end", b"[Z": "backtab"}
MOUSE_RE = re.compile(rb"\[<(\d+);(\d+);(\d+)([Mm])")


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def tui_ok() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb" \
        and not os.environ.get("COWBELL_PLAIN")


def parse_input(buf: bytes) -> tuple[list[tuple], bytes]:
    """Bytes -> events. Returns (events, unconsumed tail). A lone trailing ESC stays in the tail."""
    events, i = [], 0
    while i < len(buf):
        b = buf[i:i + 1]
        if b == b"\x1b":
            rest = buf[i + 1:]
            if not rest:
                break  # maybe the start of a sequence; caller decides after a short wait
            m = MOUSE_RE.match(rest)
            if m:
                code, x, y = int(m[1]), int(m[2]), int(m[3])
                events.append(("mouse", code, x, y, m[4] == b"M"))
                i += 1 + m.end()
                continue
            for seq, name in KEYS.items():
                if rest.startswith(seq):
                    events.append(("key", name))
                    i += 1 + len(seq)
                    break
            else:
                if rest[:1] in (b"[", b"O"):  # unknown CSI/SS3: swallow through its final byte
                    j = 1
                    while j < len(rest) and not (0x40 <= rest[j] <= 0x7e):
                        j += 1
                    if j >= len(rest):
                        break  # incomplete
                    i += 2 + j
                else:
                    events.append(("key", "esc"))
                    i += 1
            continue
        if b in (b"\r", b"\n"):
            events.append(("key", "enter"))
        elif b == b"\t":
            events.append(("key", "tab"))
        elif b in (b"\x7f", b"\x08"):
            events.append(("key", "backspace"))
        elif b == b"\x03":
            events.append(("key", "esc"))
        else:
            events.append(("char", b.decode("latin-1")))
        i += 1
    return events, buf[i:]


class Screen:
    """Raw mode + alternate screen + mouse, restored on exit whatever happens."""

    active = False

    def open(self) -> "Screen":
        import termios, tty
        if self.active:
            return self
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.tail = b""
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()
        self.active = True
        return self

    def close(self) -> None:
        import termios
        if not self.active:
            return
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        self.active = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    def events(self, timeout: float | None = None) -> list[tuple]:
        import select
        if not self.tail and not select.select([self.fd], [], [], timeout)[0]:
            return []
        data = self.tail + (os.read(self.fd, 4096) if select.select([self.fd], [], [], 0)[0] else b"")
        evs, self.tail = parse_input(data)
        if self.tail == b"\x1b" and not select.select([self.fd], [], [], 0.03)[0]:
            evs.append(("key", "esc"))
            self.tail = b""
        return evs

    def draw(self, lines: list[str]) -> None:
        sys.stdout.write("\x1b[H" + "".join(ln + "\x1b[K\r\n" for ln in lines[:-1])
                         + (lines[-1] + "\x1b[K" if lines else "") + "\x1b[J")
        sys.stdout.flush()


def highlight(line: str, width: int) -> str:
    """Selection wash across the whole row; survives the resets inside painted cells."""
    pad = " " * max(0, width - visible_len(line))
    reset = "\x1b[0m"
    wash = "\x1b[48;2;{};{};{}m".format(*(int(SELECT_BG[i:i + 2], 16) for i in (1, 3, 5))) if color_on() else "\x1b[7m"
    return wash + line.replace(reset, reset + wash) + pad + reset


def hint_bar(hints: list[tuple[str, str, str]]) -> tuple[str, list[tuple[int, int, str]]]:
    """[(key label, words, event)] -> (painted line, clickable [x0, x1) spans, 1-based columns)."""
    parts, spans, col = [], [], 3
    for label, words, ev in hints:
        plain = label + (" " + words if words else "")
        parts.append(paint(label, MUTED) + (" " + paint(words, FAINT) if words else ""))
        spans.append((col, col + len(plain), ev))
        col += len(plain) + 3
    return "  " + "   ".join(parts), spans


def menu(body: list[str], rows: list[int], hints: list[tuple[str, str, str]], selected: int = 0,
         hotkeys: str = "", flash: str = "", line_keys: dict[int, str] | None = None,
         numbered: list[int] | None = None, scr: Screen | None = None, tick: float | None = None) -> tuple:
    """Pick one of `rows` (line indexes into body).

    Returns ("pick", i), ("key", ch) for a hotkey, clicked hint or clicked `line_keys` line, ("cancel",),
    or ("tick", i) after `tick` idle seconds so the caller can refresh (pass `scr` to keep the screen up).
    `numbered[n-1]` is the row a typed digit n selects (default: rows in order).
    Click or arrows / j k / wheel select; enter (or the ⏎ button) confirms; digits jump (and pick when
    unambiguous); esc or q cancels. Buttons and rows answer the press, so a popup host that forwards
    only presses still works; releases are ignored.
    """
    selected = max(0, min(selected, len(rows) - 1))
    by_line = {ln: i for i, ln in enumerate(rows)}
    numbered = list(range(len(rows))) if numbered is None else numbered
    digits, top = "", 0
    bar, spans = hint_bar(hints)
    own = scr is None
    scr = (scr or Screen()).open()
    try:
        while True:
            cols, height = shutil.get_terminal_size((100, 24))
            foot = ["", bar] + ([paint("  " + flash, RED)] if flash else [])
            view = max(3, height - len(foot))
            sel_line = rows[selected] if rows else -1
            if not rows:
                top = 0
            elif sel_line < top:
                top = 0 if selected == 0 else sel_line
            elif sel_line >= top + view:
                top = sel_line - view + 1
            shown = [highlight(ln, cols) if n == sel_line else ln
                     for n, ln in enumerate(body[top:top + view], start=top)]
            shown += [""] * (view - len(shown))
            bar_y = view + 2  # 1-based screen row of the hint bar
            scr.draw(shown + foot)
            evs = scr.events(tick)
            if tick is not None and not evs:
                return ("tick", selected)
            for ev in evs:
                if ev[0] == "mouse":
                    _, code, x, y, down = ev
                    line = top + y - 1
                    if code in (64, 65):  # wheel
                        selected = max(0, min(len(rows) - 1, selected + (1 if code == 65 else -1)))
                    elif code == 0 and down:  # left press
                        if y == bar_y:
                            hit = next((e for x0, x1, e in spans if x0 <= x < x1), None)
                            if hit:
                                if hit == "esc":
                                    return ("cancel",)
                                if hit == "enter":
                                    if rows:
                                        return ("pick", selected)
                                    continue
                                return ("key", hit)
                        elif line in (line_keys or {}) and y <= view:
                            return ("key", line_keys[line])
                        elif line in by_line and y <= view:
                            selected = by_line[line]
                    continue
                kind, val = ev
                if kind == "key":
                    if val == "esc":
                        return ("cancel",)
                    if val == "enter":
                        if rows:
                            return ("pick", selected)
                        continue
                    step = {"up": -1, "down": 1, "backtab": -1, "tab": 1, "pgup": -view, "pgdn": view}.get(val)
                    if step:
                        selected = max(0, min(len(rows) - 1, selected + step))
                    elif val == "home":
                        selected = 0
                    elif val == "end":
                        selected = len(rows) - 1
                    digits = ""
                elif val.isdigit():
                    digits = (digits + val).lstrip("0")
                    if not digits:
                        continue
                    n = int(digits)
                    if n > len(numbered):
                        digits = val if 0 < int(val) <= len(numbered) else ""
                        n = int(digits or 0)
                    if n:
                        selected = numbered[n - 1]
                        if n * 10 > len(numbered):
                            return ("pick", selected)
                elif val in ("j", "k"):
                    selected = max(0, min(len(rows) - 1, selected + (1 if val == "j" else -1)))
                elif val == "q":
                    return ("cancel",)
                elif val.lower() in hotkeys:
                    return ("key", val.lower())
    finally:
        if own:
            scr.close()


# --- ui --------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    """One line of text. On a terminal esc cancels (KeyboardInterrupt), like ctrl-c."""
    if tui_ok():
        return line_input(prompt, default)
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def line_input(prompt: str, default: str = "") -> str:
    import select, termios, tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    buf, tail = "", b""
    tty.setraw(fd)
    try:
        while True:
            sys.stdout.write(f"\r{prompt}{buf}\x1b[K")
            sys.stdout.flush()
            select.select([fd], [], [])
            evs, tail = parse_input(tail + os.read(fd, 4096))
            if tail == b"\x1b" and not select.select([fd], [], [], 0.03)[0]:
                evs.append(("key", "esc"))
                tail = b""
            for ev in evs:
                if ev == ("key", "esc"):
                    raise KeyboardInterrupt
                if ev == ("key", "enter"):
                    return buf.strip() or default
                if ev == ("key", "backspace"):
                    buf = buf[:-1]
                elif ev[0] == "char" and ev[1] == "\x15":  # ctrl-u
                    buf = ""
                elif ev[0] == "char" and ev[1].isprintable():
                    buf += ev[1]
    finally:
        sys.stdout.write("\r\n")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def choose(title: str, items: list[str], default: int = 0, intro: str = "") -> int | None:
    """Numbered choice: the menu on a terminal (click, arrows, enter, esc), typed numbers otherwise."""
    if not items:
        return None
    if tui_ok():
        body = ([f"  {intro}", ""] if intro else []) + [section(title.upper())]
        rows = []
        for i, s_ in enumerate(items):
            rows.append(len(body))
            body.append(f"  {key(i + 1)}  {s_}{paint('   default', FAINT) if i == default else ''}")
        res = menu([""] + body, [r + 1 for r in rows],
                   [("↑↓", "move", ""), ("⏎", "pick", "enter"), ("esc", "cancel", "esc")], selected=default)
        return res[1] if res[0] == "pick" else None
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
    """New with options: agent, name, model, permissions."""
    hs = [h for h in cat.get("harness", [])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = dict(zip([h["id"] for h in hs], pool.map(lambda h: trust_match(h["trust_tool"], root), hs)))
    note = {"blocked": paint("  ⊘ blocked here", RED), "overprivilege": paint("  ! over-privileged here", RED)}
    rows = []
    for h in hs:
        ok = installed(h)
        hid, desc = f"{h['id']:<22}", h.get("title", "") if ok else "not installed"
        rows.append(f"{paint(hid, SECONDARY if ok else FAINT)} {paint(desc, MUTED if ok else FAINT)}"
                    f"{note.get(verdicts[h['id']]['verdict'], '')}")
    intro = f"{paint('new tab in' if ctx.mode != 'here' else 'in this terminal ·', FAINT)} {paint(tilde(root), SECONDARY)}"
    if not tui_ok():
        print(f"\n  {intro}")
    i = choose("agent", rows, intro=intro)
    if i is None:
        return False
    h = hs[i]
    if not installed(h):
        print(f"  {h['id']} is not installed here"); return False
    v = verdicts[h["id"]]
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {h['id']} on {tilde(root)} is blocked {v['flag']}"); return False
    taken = live_names() | {r["name"] for r in load_roster()}
    base = unique_name(slug(h["id"]), taken)
    print(f"  {paint(h['id'], INK, bold=True)}  {intro}")
    name = unique_name(slug(ask(f"  name [{base}]: ", base)), taken)
    head = f"{paint(name, INK, bold=True)}  {paint(h['id'], MUTED)}"
    model = ""
    if h["models"]:
        j = choose("model", h["models"] + ["(type another)"], intro=head)
        if j is None:
            return False
        model = h["models"][j] if j < len(h["models"]) else ask("  model id: ")
    lane = ""
    if not is_wrapper(h):
        default_lane = cat["defaults"].get("lane", "yellow")
        lane_rows = [f"{paint(f'{ln:<8}', SECONDARY)} "
                     f"{paint(' '.join(cat['lanes'].get(h['kind'], {}).get(ln, [])) or 'agent default', MUTED)}"
                     for ln in LANES]
        j = choose("permissions", lane_rows, default=LANES.index(default_lane) if default_lane in LANES else 1,
                   intro=f"{head}  {paint(model, MUTED)}")
        if j is None:
            return False
        lane = LANES[j]
    seat = seat_file(root) if h.get("seat_flag") else None
    if v["verdict"] == "overprivilege":
        if ask(f"  {paint('!', RED, bold=True)} {h['id']} is over-privileged for {trust_subject(root)} work "
               f"({v['flag']}). start anyway? [y/N] ").lower() != "y":
            return False
    argv = build_argv(cat, h, model, lane, "", "", seat, sitting=name)
    shown = shell_join([h["command"], *argv]) if is_wrapper(h) else shell_join([h["kind"], *argv])
    if ctx.mode != "here":
        print(f"\n  {paint(name, INK, bold=True)}  {paint(shown, MUTED)}")
    pane = launch(cat, h, name, root, ctx, model, lane, "", "", seat)
    print(f"  started {name} in {pane['pane_id']}")
    return True


def raise_quick(ctx: Ctx, cat: dict, hid: str, root: str) -> bool:
    """Start new with defaults: automatic name (unless [defaults] ask_name), default permissions, open now."""
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
    if ctx.mode != "here":  # outside herdr, exec_here prints the command it hands the terminal to
        print(f"  {paint(h['id'], INK, bold=True)}  {paint('new tab in', FAINT)} {paint(tilde(root), SECONDARY)}")
    name = base
    if cat["defaults"].get("ask_name"):
        raw = ask(f"  name [{base}]  (esc = cancel): " if tui_ok() else f"  name [{base}]  (q = cancel): ", base)
        if raw.lower() == "q":
            return False
        name = unique_name(slug(raw), taken)
    if v["verdict"] == "overprivilege":
        if ask(f"  {paint('!', RED, bold=True)} {h['id']} is over-privileged for {trust_subject(root)} work "
               f"({v['flag']}). start anyway? [y/N] ").lower() != "y":
            return False
    model = h["models"][0] if h["models"] else ""
    seat = seat_file(root) if h.get("seat_flag") else None
    pane = launch(cat, h, name, root, ctx, model, lane, "", "", seat)
    print(f"  started {name} in {pane['pane_id']}")
    return True


PICKER_HINTS = [("⏎", "open", "enter"), ("n", "new with options", "n"), ("g", "widen", "g"),
                ("esc", "close", "esc")]
REFRESH_S = 2.0


def footer(can_widen: bool = False) -> str:
    hints = [("number", "= open"), ("[n]", "new with options")] + ([("[g]", "widen")] if can_widen else []) + [("[q]", "")]
    return "  " + "   ".join(f"{paint(k, GOLD if k == 'number' else MUTED)}{(' ' + paint(v, FAINT)) if v else ''}"
                            for k, v in hints)


def remember_live(reg: dict, rows: list[dict]) -> int:
    """Running agents go on the roster as they are seen, so they can be resumed later. No adopt step."""
    taken = {r["name"] for r in rows}
    added = 0
    for r in reg["live"]:
        if r.get("on_roster") or not r.get("session_id"):
            continue
        name = unique_name(slug(r["name"]) or slug(r.get("kind") or "agent"), taken)
        rows.append({"name": name, "cwd": r["cwd"], "harness": r["harness"], "kind": r["kind"], "model": "",
                     "lane": "", "session_id": r["session_id"], "pane_id": r["pane_id"],
                     "workspace_id": r.get("workspace_id"), "label": r["name"], "created": now(),
                     "last_seen": now()[:10], "source": "seen"})
        r["on_roster"] = True
        taken.add(name)
        added += 1
    if added:
        save_roster(rows)
    return added


def picker(ctx: Ctx, cat: dict) -> int:
    root = ctx.cwd
    tui = tui_ok()
    scr = Screen() if tui else None
    here = ctx.mode == "here"
    herdr_up = not here or server_up()
    sel_key, sel_at, flash, expanded = "", 0, "", set()
    verdicts: dict[tuple[str, str], dict] = {}
    git_tops: dict[str, str | None] = {}

    def matcher_for(r):
        def m(tool):
            if (tool, r) not in verdicts:
                verdicts[(tool, r)] = trust_match(tool, r)
            return verdicts[(tool, r)]
        return m

    try:
        while True:
            rows = load_roster()
            if herdr_up:
                refresh_sessions(rows)
            snap = snapshot() if herdr_up else {}
            agents = (live_agents() if herdr_up else []) + (agent_processes() if here else [])
            panes = live_panes() if herdr_up else []
            if root not in git_tops:
                git_tops[root] = git_root(root)
            top = git_tops[root]
            wider = None
            if top and os.path.realpath(top) != os.path.realpath(root):
                wider = sum(1 for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], top))
            reg = build_registry(cat, root, agents, panes, rows, matcher=matcher_for(root),
                                 tab_label=tab_labels(snap))
            remember_live(reg, rows)
            ws_labels = {w.get("workspace_id"): w.get("label") or "" for w in snap.get("workspaces") or []}
            choices = picker_choices(reg, rows, ctx.workspace_id, ws_labels, expanded)
            width = min(shutil.get_terminal_size((84, 24)).columns, 100)
            text = render_picker(reg, choices, top, wider, width=width)
            can_widen = bool(top and wider is not None and wider > len(reg["live"]))
            if tui:
                keys = [c.get("key") for c in choices]
                selected = keys.index(sel_key) if sel_key in keys else min(sel_at, max(0, len(keys) - 1))
                hints = [hh for hh in PICKER_HINTS if hh[2] != "g" or can_widen]
                res = menu([""] + text.split("\n"), [c["row"] + 1 for c in choices], hints,
                           selected=selected, hotkeys="nwag", flash=flash,
                           line_keys={1: "g"} if can_widen else None,
                           numbered=[i for i, c in enumerate(choices) if c.get("idx")], scr=scr, tick=REFRESH_S)
                if res[0] in ("pick", "tick") and choices:
                    sel_key, sel_at = choices[res[1]].get("key") or "", res[1]
                if res[0] == "tick":
                    continue
                flash = ""
                if res[0] == "cancel":
                    return 0
                if res[0] == "pick":
                    hit = choices[res[1]]
                    raw = ""
                else:
                    hit, raw = None, res[1]
                if hit and hit["action"] == "more":
                    expanded.add(hit["section"])
                    continue
            else:
                print()
                print(text)
                print()
                print(footer(can_widen))
                raw = ask(f"  {paint('>', GOLD, bold=True)} ").strip().lower()
                if raw in ("", "q"):
                    return 0
                hit = next((c for c in choices if raw.isdigit() and c.get("idx") == int(raw)), None)
            if raw == "r":
                continue
            if raw == "g":
                root = top or root
                sel_key = ""
                continue
            if raw not in ("w", "n", "a") and not hit:
                continue
            if raw == "a" and not herdr_up:
                flash = "adopt needs herdr"
                continue
            if hit and hit["action"] == "focus" and str(hit["target"]["pane_id"]).startswith("pid:"):
                t = hit["target"]
                flash = f"{t['name']} runs on {t['tty']} (pid {t['pid']}); cowbell can't switch to another terminal"
                continue
            if scr:
                scr.close()
                sys.stdout.write("\x1b[H\x1b[2J")  # prompts that follow start on a clean popup
            try:
                if raw in ("w", "n"):
                    done = wizard(ctx, cat, root)
                elif raw == "a":
                    cmd_adopt(ctx, cat)
                    done = True
                elif hit["action"] == "focus":
                    herdr("agent", "focus", hit["target"]["pane_id"])
                    if here:
                        where = ws_labels.get(hit["target"].get("workspace_id") or "", "")
                        print(f"  focused {hit['target']['name']} in herdr{(' · ' + where) if where else ''}")
                    done = True
                elif hit["action"] == "enter":
                    cmd_enter(ctx, cat, hit["target"]["name"], root)
                    done = True
                else:
                    done = raise_quick(ctx, cat, hit["target"]["harness"], root)
            except KeyboardInterrupt:  # esc / ctrl-c inside a prompt: back to the list
                continue
            except (HerdrError, SystemExit) as e:
                flash = str(e)
                print(f"  {e}")
                continue
            if done:
                return 0
    finally:
        if scr:
            scr.close()


def cmd_open(env=None) -> int:
    """Action entry: open the popup, forwarding the caller's context (the popup's own is not the caller's)."""
    try:
        herdr(*picker_open_args(invocation_ctx(env)))
    except HerdrError as e:
        print(f"cowbell: {e}", file=sys.stderr)
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


NEEDS_HERDR = {"adopt", "stop"}


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    if argv and argv[0] in ("-V", "--version"):
        print(f"cowbell {__version__}"); return 0
    if not argv:
        argv = ["picker"] if tui_ok() else ["status"]
    cmd, *rest = argv
    if cmd == "keys":
        cmd_keys(rest[0] if rest else "install"); return 0
    if cmd == "open":
        return cmd_open()
    herdr_up = server_up()
    if cmd in NEEDS_HERDR and not herdr_up:
        print(f"cowbell: {cmd} needs a running Herdr server", file=sys.stderr); return 2
    # Inside a herdr pane (or its popup) new agents get a herdr tab; anywhere else, this terminal.
    ctx = Ctx.from_env() if herdr_up and in_herdr_pane() else Ctx(cwd=os.getcwd(), mode="here")
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
    raise SystemExit(f"cowbell: unknown command or harness {cmd!r} (see: cowbell catalog)")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except HerdrError as e:
        print(f"cowbell: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
