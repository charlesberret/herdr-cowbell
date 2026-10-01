#!/usr/bin/env python3
"""cowbell — pick an agent for this directory: running here, resume, or start new.

  cowbell                       on a terminal: the picker (running here · resume · start new)
                                piped: the long registry, same as `cowbell status`
  cowbell --version             one line, `cowbell X.Y.Z` (the plugin shim compares it)
  cowbell status [--json] [--place P]   what is raised here (running · resume · new)
                                --json: machine rows (name, harness, place, cwd, host, state)
  cowbell <harness>             attach to that harness here, or raise it with defaults
  cowbell new --harness ID [--model M] [--lane L] [--allow T] [--deny T] [--name N] [--place P] [--spawn] [--pen] [--split]
                                --spawn: start and return (assemblies raise N times). default: exec here
                                --pen: a person asks for the place's own herdr pen (a new machine-list line)
                                --split: a pane split instead of a new tab. `cowbell new -h` prints new's usage
  cowbell enter <name>          focus if live, else resume
  cowbell adopt [name] [--pane ID]  put a live sitting cowbell did not start on the roster
  cowbell list [--all]          roster rows (default: this directory)
  cowbell catalog               every harness: kind, egress, installed, trust badge
  cowbell reconcile [--dry-run] rewrite a claimed pane border from the agent Herdr detects

A person running cowbell gets the session in this terminal, including inside herdr
(`COWBELL_CALLER=person` forces that). An agent caller inside a herdr pane opens a new tab
in the project workspace (`COWBELL_MODE=here` forces this terminal; `COWBELL_MODE=tab` forces
a tab, which is how prefix+c still opens a tab). `--split` asks for a pane split instead of a
tab. `--pen` opens the place's own pen (a new machine-list line). Running also lists agent
processes found by working directory.

The herdr plugin (herdr-cowbell) adds prefix+c, stop and import-seats on top of this.

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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

__version__ = "0.3.1"
PLUGIN_ID = "herdr-cowbell"  # only to find config and state that 0.1–0.3 kept in herdr's plugin dirs
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
LANES = ("green", "yellow", "red")
READY = {"idle", "done"}
SHELLS = {"zsh", "bash", "fish", "sh", "dash", "ksh"}
GLYPH = {"local": "⌂", "mesh": "⌂", "vendor": "↑"}  # ⌂ ↑
DASH = "—"
GROK_AGENTS_DIR = Path("~/.grok/agents").expanduser()


class HerdrError(RuntimeError):
    pass


# --- colour ------------------------------------------------------------------
# Palette: mostly grey, with one accent. Reads the terminal theme, not a config file.
# Gold is Resume. Green is New Session. Blue is the numbers on rows you can open.
# Active heading and box are light gray (secondary). Sitting and harness names are ink.

INK, SECONDARY, MUTED, FAINT = "#d8d8e0", "#a9a9b2", "#8a8a92", "#6c6d74"
GOLD, GREEN, RED, TEAL, BLUE = "#c8a24a", "#7dba6f", "#d4756b", "#389bb4", "#7d9fc7"
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
    return paint(f"{n:>{width}}", BLUE, bold=True)


def status_cell(status: str, width: int = 8) -> str:
    return paint(f"{status:<{width}}", STATUS_FG.get(status, MUTED))


def verdict_cell(v: dict) -> str:
    mark = verdict_mark(v)
    return paint(mark, RED if mark == "⊘" else FAINT if mark in ("ok", "!") else MUTED, bold=mark == "⊘")


def header(reg: dict, git_top: str | None, wider: int | None, widen_hint: bool = True) -> list[str]:
    note = reg.get("place_note") or ""
    out = [f"  {paint('cowbell', INK, bold=True)}  {paint(tilde(reg['root']), SECONDARY)}   "
           f"{paint('subject:', FAINT)} {paint(reg['subject'] or '?', MUTED)}"
           + (f"   {paint('place:', FAINT)} {paint(note, MUTED)}" if note else "")]
    if git_top and os.path.realpath(git_top) != os.path.realpath(reg["root"]):
        hint = f"   ({wider} live under it)" if wider is not None else ""
        out.append(paint(f"  git root: {tilde(git_top)}{hint}", FAINT)
                   + (f"   {paint('[g]', MUTED)} {paint('widen', FAINT)}" if widen_hint else ""))
    return out


def section_title(title: str, fg: str = MUTED) -> str:
    return f"  {paint(title, fg, bold=True)}"


def section(title: str, note: str = "") -> str:
    return f"  {paint(title, MUTED, bold=True)}" + (f"  {paint(note, FAINT)}" if note else "")


# --- paths -----------------------------------------------------------------

def plugin_root() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_ROOT") or Path(__file__).resolve().parent)


def seed_path() -> Path | None:
    """The shipped harnesses.toml: beside a checkout or plugin, or share/cowbell/ when pip/uv installed it."""
    for p in (plugin_root() / "harnesses.toml", Path(sys.prefix) / "share" / "cowbell" / "harnesses.toml"):
        if p.is_file():
            return p
    return None


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
        seed = seed_path()
        if seed:
            shutil.copy2(seed, p)
    return p


# --- context -------------------------------------------------------------------

class Ctx:
    """Where cowbell was invoked from: the caller's pane, tab, workspace, cwd."""

    def __init__(self, workspace_id=None, tab_id=None, pane_id=None, cwd=None, agent=None, mode="tab",
                 live: "LiveSource | None" = None, launch: "HostLaunch | None" = None):
        self.workspace_id, self.tab_id, self.pane_id = workspace_id, tab_id, pane_id
        self.cwd: str = os.path.realpath(cwd or os.getcwd())
        self.agent, self.mode = agent, mode
        # The two host seams. main() chooses them once; the defaults follow the mode.
        self.live = live if live is not None else (ProcessScan() if mode == "here" else HerdrLive())
        self.launch = launch if launch is not None else (HereLaunch() if mode == "here" else HerdrLaunch())

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
        # A plugin action runs in the plugin root, so ask herdr where the caller's pane is. A command typed
        # in a pane runs where it was typed: the process cwd is the truth, even when the pane's shell moved.
        if not cwd and pane_id and env.get("HERDR_PLUGIN_ROOT"):
            cwd = herdr_pane_cwd(pane_id)
        return cls(
            workspace_id=data.get("workspace_id") or env.get("HERDR_WORKSPACE_ID"),
            tab_id=data.get("tab_id") or env.get("HERDR_TAB_ID"),
            pane_id=pane_id,
            cwd=os.path.realpath(cwd or os.getcwd()),
            agent=data.get("focused_pane_agent"),
            mode=env.get("COWBELL_MODE") or "tab",
        )


# --- scope -----------------------------------------------------------------

def at_or_beneath(path: str, root: str) -> bool:
    """A pane above the root never counts — a workspace at ~ would match every project."""
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def same_tree(a: str, b: str) -> bool:
    """Either path is the other, or one sits under the other."""
    return bool(a and b and (at_or_beneath(a, b) or at_or_beneath(b, a)))


def occupant_same(row: dict, agent: dict, tab_label: str = "") -> bool:
    """The live pane still holds this sitting: same directory tree, or the tab still bears the sitting name."""
    if same_tree(agent.get("cwd") or "", row.get("cwd") or ""):
        return True
    sitting = row.get("name") or ""
    lab = slug(tab_label) if tab_label else ""
    return bool(sitting and lab and sitting == lab)


def bind_sitting(row: dict, live: dict) -> None:
    """Copy the live occupant's pane, session, kind, and cwd onto a roster row."""
    sid = live.get("session_id") or ((live.get("agent_session") or {}).get("value") or "")
    if sid:
        row["session_id"] = sid
    live_kind = live.get("kind") or live.get("agent") or ""
    old_kind = row.get("kind") or ""
    if live_kind:
        if (row.get("harness") or "") in ("", old_kind):
            row["harness"] = live.get("harness") or live_kind
        row["kind"] = live_kind
    toks = live.get("tokens") if isinstance(live.get("tokens"), dict) else {}
    qmodel = live.get("model") or toks.get("quota_model") or ""
    if qmodel:
        row["model"] = qmodel
    if live.get("cwd"):
        row["cwd"] = live["cwd"]
    if live.get("pane_id"):
        row["pane_id"] = live["pane_id"]
    if live.get("tab_id"):
        row["tab_id"] = live["tab_id"]
    if live.get("workspace_id"):
        row["workspace_id"] = live["workspace_id"]
    row["last_seen"] = now()[:10]


def unbind_sitting(row: dict) -> None:
    """Drop a pane that now holds someone else. Session id was the new occupant's, so drop it too."""
    row["pane_id"] = ""
    row["tab_id"] = ""
    row["workspace_id"] = ""
    row["session_id"] = ""


def git_root(path: str) -> str | None:
    proc = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def seat_file(root: str, home: str | None = None) -> Path | None:
    """Nearest EXT["seat_file"] walking up from root, stopping at $HOME (never inject home)."""
    home = os.path.realpath(home or Path.home())
    d = os.path.realpath(root)
    while True:
        cand = Path(d) / EXT["seat_file"]
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

# Two hooks onto tools cowbell does not own, overridable in [defaults].
# trust_command: asked `subject <path>`, `match <tool> <path>`, `badge <tool>`;
# absent from PATH means no trust verdicts at all. seat_file: a brief found by
# walking up from the directory, handed to a harness via its system_prompt_flag.
EXT = {"trust_command": "harness-trust", "seat_file": "SEAT.md"}


def apply_ext(cat: dict) -> None:
    d = cat.get("defaults", {})
    for key in EXT:
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            EXT[key] = v.strip()


def prompt_flag(h: dict) -> str | None:
    """Flag that hands a harness a system-prompt file (`seat_flag` until 0.3)."""
    return h.get("system_prompt_flag") or h.get("seat_flag")


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
    apply_ext(cat)
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


# A place is a directory with its own rules: the herdr workspace ("pen") its sittings open in,
# which harnesses may start there, and a default lane. Declared as [places.<name>] in the
# config-dir catalog; [defaults] untitled_place names the place a sitting from ~ goes to.

def places(cat: dict) -> dict[str, dict]:
    out = {}
    for name, p in (cat.get("places") or {}).items():
        if isinstance(p, dict) and p.get("cwd"):
            out[name] = {**p, "name": name, "cwd": os.path.realpath(os.path.expanduser(str(p["cwd"])))}
    return out


def place_for(cat: dict, root: str) -> dict | None:
    """The deepest place whose directory holds root (longest cwd wins; equal-length
    ties go to the first place declared in the catalog)."""
    hits = [p for p in places(cat).values() if at_or_beneath(os.path.realpath(root), p["cwd"])]
    return max(hits, key=lambda p: len(p["cwd"]), default=None)


def resolve_root(cat: dict, root: str, home: str | None = None) -> tuple[str, dict | None, bool]:
    """(root, place, remapped). A sitting from ~ never lands in ~ when an untitled place is declared."""
    home = os.path.realpath(os.path.expanduser(home or "~"))
    if os.path.realpath(root) == home:
        pl = places(cat).get(cat.get("defaults", {}).get("untitled_place") or "")
        if pl:
            return pl["cwd"], pl, True
    return root, place_for(cat, root), False


def scoped_catalog(cat: dict, place: dict | None) -> dict:
    """The catalog as seen from a place: only its allowed harnesses, its default lane."""
    if not place:
        return cat
    out = {**cat, "defaults": {**cat.get("defaults", {})}}
    if place.get("lane"):
        out["defaults"]["lane"] = place["lane"]
    if place.get("default_harness"):
        out["defaults"]["default_harness"] = place["default_harness"]
    allow = place.get("allow_harness")
    if allow is not None:
        allowed = set(allow)
        out["harness"] = [h for h in cat.get("harness", []) if h["id"] in allowed]
        # keep allow_harness order so a default guest door can sit first
        order = {hid: i for i, hid in enumerate(allow)}
        out["harness"].sort(key=lambda h: order.get(h["id"], len(order)))
    return out


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


def now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# --- live (pure: reads a herdr snapshot already fetched) -------------------------

def tab_labels(snap: dict) -> dict[str, str]:
    return {t["tab_id"]: (t.get("label") or "") for t in snap.get("tabs") or [] if t.get("tab_id")}


# A pane border may cache `sitting · harness`. The tab label is the sitting.
# Herdr's detected agent is the harness. A border with no claim is not a cache.
HARNESS_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def harness_claim(label: str):
    """The harness a pane border claims, or None when the border makes no claim."""
    if " · " not in (label or ""):
        return None
    sitting, tool = (label or "").split(" · ", 1)
    sitting, tool = sitting.strip(), tool.strip()
    if not sitting or not HARNESS_TOKEN.match(tool):
        return None
    return sitting, tool


def coherent_pane_label(sitting: str, harness: str) -> str:
    """`sitting · harness` when both are present, otherwise the sitting alone."""
    sitting, harness = (sitting or "").strip(), (harness or "").strip()
    if sitting and harness:
        return f"{sitting} · {harness}"
    return sitting


def reconcile_label(label, sitting, detected, idle_shell=False):
    """The border a claimed pane should wear, or None when the claim should stay.

    A detected agent replaces the claimed harness. An idle shell with no agent
    drops the claim back to the sitting. Anything else — including a wrapper
    Herdr cannot see — keeps the claim it has.
    """
    claim = harness_claim(label)
    if not claim:
        return None
    name, _tool = claim
    sitting = (sitting or name).strip() or name
    detected = (detected or "").strip()
    if detected:
        want = coherent_pane_label(sitting, detected)
        return None if want == (label or "") else want
    if idle_shell:
        return None if sitting == (label or "") else sitting
    return None


def pane_is_idle_shell(pane: dict) -> bool:
    """A pane whose foreground is a shell prompt, and which has no detected agent."""
    if pane.get("agent"):
        return False
    title = pane.get("terminal_title_stripped") or pane.get("terminal_title") or ""
    return "@" in title and ":" in title


# --- session history (read-only discovery from each harness's own store) -------
# cowbell's roster only knows what cowbell started or `adopt` was told about. The
# harnesses keep their own per-directory stores; these providers read the metadata
# needed for a Resume row (id, times, title) and nothing else from a transcript.
# Off unless [defaults] session_history = "on" — a session store is private.

def history_mode(cat: dict) -> str:
    """off (default) · on (sittings) · all (including agents dispatched by a sitting)."""
    v = str(cat.get("defaults", {}).get("session_history", "off")).strip().lower()
    return {"true": "on", "yes": "on", "on": "on", "all": "all"}.get(v, "off")


def history_on(cat: dict) -> bool:
    return history_mode(cat) != "off"


# A session whose opening line casts a role was dispatched by another sitting, not
# chosen by a person. Honest history, but noise in a list you pick from.
DISPATCHED = re.compile(r"^(you are|act as|role:)\b", re.I)


def history_cache() -> dict:
    try:
        return json.loads((state_dir() / "history-cache.json").read_text())
    except Exception:
        return {}


def write_history_cache(cache: dict) -> None:
    try:
        p = state_dir() / "history-cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache)[:4_000_000])
    except OSError:
        pass  # a cache that cannot be written is a slow cowbell, not a broken one


def cache_key(f: Path) -> str:
    try:
        st = f.stat()
    except OSError:
        return ""
    return f"{f}:{int(st.st_mtime)}:{st.st_size}"


def message_text(v) -> str:
    """The human text of a harness message: str, {content|text}, or a list of blocks."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return message_text(v.get("content", v.get("text", "")))
    if isinstance(v, list):
        return " ".join(t for t in (message_text(x) for x in v) if t)
    return ""


# First user "messages" are often the harness talking to itself: a git-status
# preamble, a rules digest, a caveat about local commands. Not what the sitting was about.
INJECTED = re.compile(r"^(caveat:|this is the git status|the rules section|codebase and user instructions"
                      r"|analysis:|<)", re.I)


def session_title(v, limit: int = 60) -> str:
    """A title from a user message, with the harness's injected blocks dropped."""
    raw = message_text(v)
    m = re.search(r"<user_query>\s*(.+?)\s*</user_query>", raw, re.S)
    if m:
        raw = m.group(1)
    for tag in ("system-reminder", "user_info", "command-name", "command-message", "local-command-stdout"):
        raw = re.sub(rf"<{tag}>.*?</{tag}>", " ", raw, flags=re.S)
    raw = re.sub(r"<[^>]{1,40}>", " ", raw)
    raw = " ".join(raw.split())
    return raw[:limit].strip()


def claude_store(root: str, home: Path | None = None) -> Path:
    """Claude Code keys a project dir by the cwd with / and . flattened to -."""
    base = (home or Path.home()) / ".claude" / "projects"
    return base / re.sub(r"[/.]", "-", os.path.realpath(root))


def claude_sessions(root: str, cache: dict, home: Path | None = None) -> list[dict]:
    out = []
    store = claude_store(root, home)
    if not store.is_dir():
        return out
    for f in sorted(store.glob("*.jsonl")):
        key = cache_key(f)
        hit = cache.get(key)
        if hit is None:
            hit = {"session_id": f.stem, "title": "", "started": "", "last": ""}
            try:
                with open(f, "r", errors="replace") as fh:
                    for i, line in enumerate(fh):
                        if i > 400:
                            break
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        ts = d.get("timestamp") or ""
                        if ts:
                            hit["started"] = hit["started"] or ts
                            hit["last"] = max(hit["last"], ts)
                        if d.get("type") == "custom-title" and d.get("customTitle"):
                            hit["title"] = str(d["customTitle"])[:60]
                        elif not hit["title"] and d.get("type") == "user":
                            t = session_title(d.get("message"))
                            hit["title"] = "" if INJECTED.match(t) else t
            except OSError:
                continue
            cache[key] = hit
        out.append({**hit, "harness": "claude", "kind": "claude"})
    return out


def grok_store(root: str, home: Path | None = None) -> Path:
    """Grok keys a session dir by the percent-encoded cwd."""
    base = (home or Path.home()) / ".grok" / "sessions"
    return base / urllib.parse.quote(os.path.realpath(root), safe="")


def grok_sessions(root: str, cache: dict, home: Path | None = None) -> list[dict]:
    out = []
    store = grok_store(root, home)
    if not store.is_dir():
        return out
    for d in sorted(store.iterdir()):
        chat = d / "chat_history.jsonl"
        if not chat.is_file():
            continue
        key = cache_key(chat)
        hit = cache.get(key)
        if hit is None:
            hit = {"session_id": d.name, "title": "", "started": "", "last": ""}
            try:
                st = chat.stat()
                hit["last"] = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat()
                hit["started"] = _dt.datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime),
                                                            _dt.timezone.utc).isoformat()
                with open(chat, "r", errors="replace") as fh:
                    for i, line in enumerate(fh):
                        if i > 40:
                            break
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("type") == "user":
                            t = session_title(r.get("content"))
                            if t and INJECTED.match(t):
                                continue
                            if t:
                                hit["title"] = t
                                break
            except OSError:
                continue
            cache[key] = hit
        out.append({**hit, "harness": "grok", "kind": "grok"})
    return out


HISTORY_PROVIDERS = {"claude": claude_sessions, "grok": grok_sessions}


def discover_sessions(root: str, cat: dict, home: Path | None = None) -> list[dict]:
    """Sessions this directory has seen, from the harnesses' own stores. [] when off."""
    if not history_on(cat):
        return []
    ids = {h["id"] for h in cat.get("harness", [])}
    cache, rows = history_cache(), []
    for hid, provider in HISTORY_PROVIDERS.items():
        if ids and hid not in ids:
            continue  # a place's allow_harness governs discovery too
        try:
            rows += provider(root, cache, home)
        except Exception:
            continue  # one unreadable store never costs the others
    write_history_cache(cache)
    if history_mode(cat) != "all":
        rows = [r for r in rows if not DISPATCHED.match(r.get("title") or "")]
    for r in rows:
        title = r.get("title") or ""
        r.setdefault("name", (title[:21] + "…") if len(title) > 22 else (title or r["session_id"][:8]))
        r["cwd"], r["source"] = os.path.realpath(root), "history"
        r["last_seen"] = (r.get("last") or "")[:10]
    _disambiguate_history_names(rows)
    rows.sort(key=lambda r: r.get("last") or "", reverse=True)
    return rows


def _disambiguate_history_names(rows: list[dict]) -> None:
    """Two sittings can share a truncated title. The picker passes the row; the CLI matches the name."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("name") or "", []).append(r)
    for name, group in groups.items():
        if len(group) < 2 or not name:
            continue
        for n in (4, 8, 12):
            tails = [(r.get("session_id") or "")[:n] for r in group]
            if all(tails) and len(set(tails)) == len(group):
                for r, tail in zip(group, tails):
                    r["name"] = f"{name} {tail}"
                break
        else:
            for i, r in enumerate(group, 1):
                r["name"] = f"{name} {i}"


# --- trust (delegated to EXT["trust_command"]; never re-encoded here) ----------------

def trust_available() -> bool:
    return shutil.which(EXT["trust_command"]) is not None


def trust_subject(root: str) -> str:
    if not trust_available():
        return "?"
    proc = subprocess.run([EXT["trust_command"], "subject", root], capture_output=True, text=True)
    return proc.stdout.strip() or "?"


def parse_match(text: str, code: int) -> dict:
    verdict = flag = ""
    for line in text.splitlines():
        m = re.match(r"^\s*verdict:\s+(\S+)\s*(.*)$", line)
        if m:
            verdict = m.group(1)
            flag = re.sub(r"\s*\[.*\]\s*$", "", m.group(2).strip()).strip("()")
        m = re.match(r"^\s*flag:\s+(.*)$", line)
        if m and m.group(1).strip() not in ("(none)", ""):
            flag = m.group(1).strip()
    if not verdict:
        verdict = {0: "ok", 3: "overprivilege", 4: "blocked"}.get(code, "unknown")
    return {"verdict": verdict, "flag": flag, "exit": code}


def trust_note(hid: str, v: dict, root: str) -> str:
    """Over-privilege is advisory (T4: visible, never a gate). Say what the checker saw."""
    why = f" ({v['flag']})" if v.get("flag") else ""
    return f"{hid} has more reach than {tilde(root)} needs{why}"


def trust_match(tool: str, root: str) -> dict:
    if not trust_available():
        return {"verdict": "unknown", "flag": "?", "exit": -1}
    proc = subprocess.run([EXT["trust_command"], "match", tool, root], capture_output=True, text=True)
    if proc.returncode not in (0, 3, 4):
        return {"verdict": "unknown", "flag": "?", "exit": proc.returncode}
    return parse_match(proc.stdout, proc.returncode)


def trust_badge(tool: str) -> str:
    if not trust_available():
        return "?"
    proc = subprocess.run([EXT["trust_command"], "badge", tool], capture_output=True, text=True)
    return proc.stdout.strip() or "?"


def verdict_mark(v: dict) -> str:
    return {"ok": "ok", "overprivilege": "!", "blocked": "⊘", "unknown": "?"}.get(v.get("verdict") or "unknown", "?")


# --- registry ------------------------------------------------------------------

def build_registry(cat: dict, root: str, agents: list[dict], panes: list[dict], roster: list[dict],
                   matcher=None, subject: str = "", tab_label: dict | None = None,
                   history: list[dict] | None = None) -> dict:
    """Partition the herd for one root: live here, cold here, absent here."""
    matcher = matcher or (lambda tool: trust_match(tool, root))
    labels = tab_label or {}
    live = [a for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], root)]
    live_by_pane = {a["pane_id"]: a for a in live}
    pane_ids = {p.get("pane_id") for p in panes}
    rows_here = roster_here(roster, root)
    by_pane: dict[str, dict] = {str(r["pane_id"]): r for r in rows_here if r.get("pane_id")}
    # identify an exe by catalog: a kind only one harness in this catalog has is that harness
    # id; a kind several share (pi wrappers) stays the exe name. LAB-294.
    kind_id: dict[str, str] = {}
    for h in cat.get("harness", []):
        prev = kind_id.setdefault(h["kind"], h["id"])
        if prev != h["id"]:
            kind_id[h["kind"]] = ""

    live_rows, seen_pane = [], set()
    for a in live:
        r = by_pane.get(a["pane_id"])
        lab = labels.get(a.get("tab_id"), "")
        live_rows.append({
            "name": ((r or {}).get("name") or (slug(lab) if lab else "")
                     or slug(a.get("terminal_title_stripped") or a.get("agent") or "agent")),
            "harness": (r or {}).get("harness") or kind_id.get(a.get("agent") or "", "") or a.get("agent"),
            "kind": a.get("agent"), "model": (r or {}).get("model", ""), "lane": (r or {}).get("lane", ""),
            "status": a.get("agent_status") or "live", "pane_id": a["pane_id"], "tab_id": a.get("tab_id"),
            "workspace_id": a.get("workspace_id"), "cwd": a.get("cwd"),
            "place": (place_for(cat, a.get("cwd") or "") or {}).get("name") or "",
            "session_id": (a.get("agent_session") or {}).get("value") or (r or {}).get("session_id", ""),
            "on_roster": r is not None,
            # turn clock, only where a sidebar reporter publishes one ($since while working, $total when done)
            "since": ((a.get("tokens") or {}).get("since") or (a.get("tokens") or {}).get("total") or "").strip(),
            "pid": a.get("pid"), "tty": a.get("tty", ""),
        })
        seen_pane.add(a["pane_id"])

    allowed = {h["id"] for h in cat.get("harness", [])}
    cold_rows = []
    for r in rows_here:
        if r.get("pane_id") in seen_pane:
            continue
        hid = r.get("harness") or r.get("kind") or ""
        if allowed and hid not in allowed:
            continue  # place catalog: do not offer resume of a harness the door does not admit
        pane_alive = r.get("pane_id") in pane_ids and r.get("pane_id") not in live_by_pane
        cold_rows.append({**r, "status": "unknown" if pane_alive else "cold", "pane_alive": pane_alive})

    # sessions the harnesses recorded themselves; a roster row wins, it knows the pane
    seen_ids = {x.get("session_id") for x in live_rows} | {x.get("session_id") for x in cold_rows}
    for r in (history or []):
        if r.get("session_id") in seen_ids or (allowed and r.get("harness") not in allowed):
            continue
        seen_ids.add(r.get("session_id"))
        cold_rows.append({**r, "status": "cold", "pane_alive": False})

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
            "startable": startable, "default_harness": cat.get("defaults", {}).get("default_harness", "")}


def row_host(r: dict) -> str:
    """Where the sitting is attached: a herdr pane, this terminal, or nowhere (cold)."""
    pane = str(r.get("pane_id") or "")
    if pane.startswith("pid:") or r.get("tty"):
        return "tty"
    if pane and pane != "gone":
        return "herdr pane"
    return "none"


def registry_json(reg: dict, place: dict | None = None) -> dict:
    """Running / resume / new as the picker names them, for assemblies that cannot scrape the TUI."""
    place_name = (place or {}).get("name") or ""
    root = reg.get("root") or ""

    def sitting_row(r: dict) -> dict:
        return {
            "name": r.get("name") or "",
            "harness": r.get("harness") or r.get("kind") or "",
            "place": r.get("place") or place_name,
            "cwd": r.get("cwd") or "",
            "host": row_host(r),
            "state": r.get("status") or "",
            "pane_id": r.get("pane_id") or "",
            "session_id": r.get("session_id") or "",
        }

    def new_row(a: dict) -> dict:
        return {
            "name": "",
            "harness": a.get("harness") or "",
            "place": place_name,
            "cwd": root,
            "host": "none",
            "state": "new",
            "installed": bool(a.get("installed")),
            "trust": (a.get("trust") or {}).get("verdict") or "unknown",
        }

    return {
        "root": root,
        "place": place_name,
        "running": [sitting_row(r) for r in reg.get("live") or []],
        # Resume only what can resume: a row without a session id would just start fresh (LAB-298).
        "resume": [sitting_row(r) for r in reg.get("cold") or [] if r.get("session_id")],
        "new": [new_row(a) for a in reg.get("absent") or []],
    }


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
        # Resume only what can resume: a row without a session id would just start fresh (LAB-298).
        cold = [r for r in reg["cold"] if r.get("session_id")]
        if cold:
            out.append("")
            out.append(section("COLD HERE", "resumable"))
        for r in cold:
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
        out.append(section("ABSENT HERE", "⌂ local  ↑ vendor · ok  ! more reach  ⊘ blocked  ? unknown"))
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
# Census first (not a menu), then the two menus chroma marks as openable.
PICKER_SECTIONS = (("running", "Active", SECONDARY), ("resume", "Resume", GOLD), ("start", "New Session", GREEN))


def picker_selectable(c: dict) -> bool:
    """Rows the cursor, digits, and Enter can land on. Active sittings are a census."""
    return c.get("section") != "running" or c.get("action") == "more"


def rank_harnesses(ids: list[str], roster: list[dict], root: str, default: str = "") -> list[str]:
    """Most used here first (a start at or beneath root counts 3), then anywhere, then catalog order.
    A place default_harness stays first so usage of a house grok in the pad does not bury the guest door."""
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
    out = sorted(out, key=lambda h: -score.get(h, 0))
    if default and default in out:
        out = [default] + [h for h in out if h != default]
    return out


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
    """Popup rows: active census, resume, start new. Only Resume and New Session are a menu."""
    expanded = expanded or set()
    ws_labels = ws_labels or {}
    live: list[dict] = []
    for r in sorted(reg["live"], key=lambda r: bool(here_ws) and r.get("workspace_id") != here_ws):
        name, kind = f"{r['name']:<22}", f"{r.get('kind') or r['harness'] or '':<11}"
        where = ws_labels.get(r.get("workspace_id") or "", "") if r.get("workspace_id") != here_ws else ""
        where = f"in {where}" if where else f"on {r['tty']}" if r.get("tty") else ""
        clock_w = max((len(x.get("since") or "") for x in reg["live"]), default=0)
        clock = f"{r.get('since', ''):<{clock_w + 2}}" if clock_w else ""
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
                       "line": f"{paint(name, INK, bold=True)} {paint(kind, MUTED)} "
                               f"{paint(ago(r.get('last_seen') or r.get('created') or ''), FAINT)}"})
    pool = {a["harness"]: a for a in reg.get("startable", reg["absent"])
            if a.get("installed") and a.get("trust", {}).get("verdict") != "blocked"}
    start = []
    ranked = rank_harnesses(list(pool), roster or [], reg["root"], default=reg.get("default_harness") or "")
    shown = ranked if "start" in expanded or len(ranked) <= FOLD else ranked[:FOLD - 1]
    title_w = min(34, max((len(pool[h].get("title") or "") for h in shown), default=0))
    for hid in ranked:
        a = pool[hid]
        warn = f"   {paint('! more reach than needed', FAINT)}" \
            if a.get("trust", {}).get("verdict") == "overprivilege" else ""
        start.append({"action": "raise", "target": a, "key": f"raise:{hid}",
                      "line": f"{paint(f'{hid:<22}', INK, bold=True)} "
                              + paint(f"{a.get('title') or '':<{title_w}}" if warn else a.get("title") or "", MUTED) + warn})
    out = []
    for section, rows in (("running", live), ("resume", resume), ("start", start)):
        out += [dict(c, section=section) for c in fold(rows, section, expanded)]
    return out


def _active_box(title: str, rows: list[dict], fg: str = SECONDARY) -> tuple[str, list[str], str]:
    """Light-gray box around the Active census. Heading sits on the top rule.

    Inner names start at the same column as numbered Resume / New Session rows.
    """
    inners = [f"    {c['line']}" for c in rows]
    head = f"─ {title} "
    inner_w = max(len(head) + 1, max((visible_len(s) + 1 for s in inners), default=0))
    rest = inner_w - len(head)
    top = "  " + paint("┌─ ", fg) + paint(title, fg, bold=True) + paint(" " + "─" * rest + "┐", fg)
    bot = "  " + paint("└" + "─" * inner_w + "┘", fg)
    sides = []
    for inner in inners:
        pad = " " * (inner_w - visible_len(inner))
        sides.append("  " + paint("│", fg) + inner + pad + paint("│", fg))
    return top, sides, bot


def render_picker(reg: dict, choices: list[dict], git_top: str | None = None, wider: int | None = None,
                  width: int = 84) -> str:
    """New-tab surface: Active (census in a light-gray box), Resume, New Session. Numbers open a row."""
    path = tilde(reg["root"])
    right = reg.get("place_note", "")
    if git_top and wider is not None and wider > len(reg["live"]):
        right = f"g widen to {os.path.basename(git_top.rstrip('/'))} · {wider} running"
    gap = width - 2 - len(path) - len(right)
    out = [f"  {paint(path, SECONDARY, bold=True)}" + (f"{' ' * max(3, gap)}{paint(right, FAINT)}" if right else "")]
    n = 0
    for section, title, fg in PICKER_SECTIONS:
        rows = [c for c in choices if c.get("section") == section]
        if not rows:
            continue
        out.append("")
        if section == "running":
            top, sides, bot = _active_box(title, rows, fg)
            out.append(top)
            for c, line in zip(rows, sides):
                c["row"] = len(out)
                c["idx"] = None
                out.append(line)
            out.append(bot)
            continue
        out.append(section_title(title, fg))
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
    if seat and prompt_flag(h):
        argv += [prompt_flag(h), str(seat)]  # claude takes -file; pi's --append-system-prompt accepts a path
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


def live_names(snap: dict) -> set[str]:
    """Taken sittings: tab labels of live agent panes (the sidebar identity token)."""
    labels = tab_labels(snap)
    names: set[str] = set()
    for a in snap.get("agents") or []:
        lab = slug(labels.get(a.get("tab_id"), "") or "")
        if lab:
            names.add(lab)
    return names


# --- standalone (outside herdr) -----------------------------------------------------
# Same picker in any terminal. Running = herdr's agents (if a server is up) plus agent
# processes found by cwd. An interactive raise execs into this terminal; a non-tty
# raise (assemblies, --spawn) starts a new session and returns so N raises can follow.

AGENT_EXES = {"claude", "codex", "grok", "gemini", "agy", "opencode", "pi", "cursor-agent", "kimi", "amp",
              "copilot", "qwen", "droid", "omp", "kilo", "cline", "devin", "hermes", "kiro", "muse", "maki",
              "mastracode", "qodercli"}
INTERPRETERS = {"node", "bun", "deno", "python", "python3"}


def in_herdr_pane(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("HERDR_PANE_ID") or env.get("HERDR_PLUGIN_CONTEXT_JSON") or env.get("COWBELL_CTX"))


# Harnesses set these. COWBELL_CALLER=person|agent wins when a sitting must override.
_AGENT_ENV = ("GROK_AGENT", "CLAUDECODE", "CLAUDE_CODE")


def caller_is_agent(env=None) -> bool:
    """True when this process is an agent sitting, not a person at the keyboard."""
    env = os.environ if env is None else env
    who = (env.get("COWBELL_CALLER") or "").strip().lower()
    if who == "agent":
        return True
    if who == "person":
        return False
    return any(env.get(k) for k in _AGENT_ENV)


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
    lsof = shutil.which("lsof") or next((x for x in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.access(x, os.X_OK)), None)
    if not lsof:  # a spare PATH (launchd, a plugin host) often lacks /usr/sbin; no lsof means no cwds, not a crash
        return {}
    proc = subprocess.run([lsof, "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
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


def _here_exe(h: dict, cmd: list[str]) -> str:
    exe = cmd[0] if os.path.isabs(cmd[0]) else shutil.which(cmd[0])
    if not exe:
        raise SystemExit(f"cowbell: {cmd[0]} is not on PATH")
    return exe


def unix_confine(cat: dict, root: str, cmd: list[str]) -> list[str]:
    """Drop to the place's unix_user, then optional nono. agent-run if it is on PATH, else sudo -u."""
    import pwd
    pl = place_for(cat, root)
    defaults = cat.get("defaults") or {}
    uid = ((pl or {}).get("unix_user") or defaults.get("unix_user") or "").strip()
    require = (pl or {}).get("require_uid")
    if require is None:
        require = bool(defaults.get("require_uid"))
    if require and not uid:
        raise SystemExit(
            f"cowbell: refused — no unix_user for {tilde(root)}; "
            "set [places.*] unix_user or use agent-run <uid> -- …"
        )
    if not uid:
        return cmd
    try:
        pw = pwd.getpwnam(uid)
    except KeyError:
        raise SystemExit(f"cowbell: unix_user {uid!r} is not on this host")
    profile = os.path.expanduser((pl or {}).get("nono_profile") or defaults.get("nono_profile") or "")
    inner = list(cmd)
    if profile:
        nono = shutil.which("nono")
        if not nono:
            raise SystemExit("cowbell: nono is not on PATH (needed for this place)")
        inner = [nono, "run", "--silent", "--profile", profile, "--allow-cwd", "--"] + inner
    if os.geteuid() == pw.pw_uid:
        return inner
    helper = shutil.which("agent-run")
    if helper:
        return [helper, uid, "--"] + inner
    sudo = shutil.which("sudo")
    if not sudo:
        raise SystemExit(
            f"cowbell: cannot drop to unix user {uid!r} "
            "(agent-run is not installed and sudo is not on PATH)"
        )
    return [sudo, "-u", uid, "--"] + inner


def _record_here(h: dict, name: str, root: str, model: str, lane: str, allow: str, deny: str,
                 seat: Path | None, session: str, cmd: list[str], pane_id: str | None) -> None:
    rows = [r for r in load_roster() if r.get("name") != name]
    rows.append({"name": name, "cwd": root, "harness": h["id"], "kind": h["kind"], "model": model, "lane": lane,
                 "allow": allow, "deny": deny, "seat": str(seat) if seat else "", "session_id": session,
                 "pane_id": pane_id, "created": now(), "last_seen": now()[:10], "argv": cmd[1:], "source": "here"})
    save_roster(rows)


def exec_here(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", model="", lane="", allow="", deny="",
              seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None, focus: bool = True):
    """Hand this terminal to the agent. Does not return."""
    cmd, session = here_command(cat, h, name, model, lane, allow, deny, seat, resume_id, extra)
    cmd = unix_confine(cat, root, cmd)
    exe = _here_exe(h, cmd)
    _record_here(h, name, root, model, lane, allow, deny, seat, session, cmd, None)
    sys.stdout.write("\x1b[H\x1b[2J" if sys.stdout.isatty() else "")
    print(f"  {paint(name, INK, bold=True)}  {paint(shell_join(cmd), MUTED)}  {paint(tilde(root), FAINT)}")
    sys.stdout.flush()
    os.chdir(root)
    os.execv(exe, cmd)


def spawn_here(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", model="", lane="", allow="", deny="",
               seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None, focus: bool = True) -> dict:
    """Start the agent in a new session at root and return. Assemblies raise N times this way."""
    cmd, session = here_command(cat, h, name, model, lane, allow, deny, seat, resume_id, extra)
    exe = _here_exe(h, cmd)
    proc = subprocess.Popen(
        [exe, *cmd[1:]], cwd=root, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pane_id = f"pid:{proc.pid}"
    _record_here(h, name, root, model, lane, allow, deny, seat, session, cmd, pane_id)
    return {"pane_id": pane_id, "pid": proc.pid}


def launch(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", *args, **kw):
    """Start or resume through the host chosen once: a new herdr tab, or this terminal."""
    return ctx.launch.start(cat, h, name, root, ctx, *args, **kw)


# --- hosts ---------------------------------------------------------------------
# Two seams, chosen once in main(): where running agents come from (LiveSource) and where a
# started or resumed agent goes (HostLaunch). Nothing outside the herdr adapter runs `herdr`.

class LiveSource:
    """Running agents as herdr-shaped rows (pane_id, agent, cwd, agent_status, ...). The empty source."""
    herdr = False  # True when rows are herdr panes that can be focused, adopted, stopped

    def agents(self) -> list[dict]:
        return []

    def panes(self) -> list[dict]:
        return []

    def snapshot(self) -> dict:
        return {}

    def agent(self, name: str) -> dict | None:
        return None

    def refresh_sessions(self, rows: list[dict]) -> None:
        return None

    def focus(self, row: dict) -> str:
        """Bring a running agent forward and say so, or raise SystemExit saying why not."""
        raise SystemExit(f"cowbell: {row.get('name') or row.get('pane_id')} cannot be focused from here")


class ProcessScan(LiveSource):
    """Agent CLIs found by process working directory (outside herdr)."""

    def agents(self) -> list[dict]:
        return agent_processes()

    def focus(self, row: dict) -> str:
        raise SystemExit(f"{row.get('name')} runs on {row.get('tty')} (pid {row.get('pid')}); "
                         f"cowbell can't switch to another terminal")


class CombinedLive(LiveSource):
    def __init__(self, *sources: LiveSource | None):
        self.sources = [x for x in sources if x is not None]
        self.herdr = any(x.herdr for x in self.sources)

    def agents(self) -> list[dict]:
        return [a for x in self.sources for a in x.agents()]

    def panes(self) -> list[dict]:
        return [p for x in self.sources for p in x.panes()]

    def snapshot(self) -> dict:
        return next((snap for snap in (x.snapshot() for x in self.sources) if snap), {})

    def agent(self, name: str) -> dict | None:
        return next((a for a in (x.agent(name) for x in self.sources) if a), None)

    def refresh_sessions(self, rows: list[dict]) -> None:
        for x in self.sources:
            x.refresh_sessions(rows)

    def focus(self, row: dict) -> str:
        owner = ProcessScan if str(row.get("pane_id") or "").startswith("pid:") else HerdrLive
        src = next((x for x in self.sources if isinstance(x, owner)), None)
        return (src or LiveSource()).focus(row)


class HostLaunch:
    tabs = False  # True when a start opens a herdr tab

    def start(self, cat: dict, h: dict, name: str, root: str, ctx: "Ctx", *args, **kw) -> dict:
        raise NotImplementedError


class HereLaunch(HostLaunch):
    """Outside herdr: exec into this terminal, or spawn a new session and return."""

    def start(self, cat, h, name, root, ctx, *args, **kw):
        spawn = kw.pop("spawn", False)
        kw.pop("pen", None)  # a pen is a herdr workspace; this host has no tab
        fn = spawn_here if spawn else exec_here
        return fn(cat, h, name, root, ctx, *args, **kw)


# --- herdr adapter (the only code that runs the herdr CLI) ------------------------------------

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


def herdr_socket() -> Path:
    if os.environ.get("HERDR_SOCKET_PATH"):
        return Path(os.environ["HERDR_SOCKET_PATH"]).expanduser()
    cfg = os.environ.get("HERDR_CONFIG_PATH")
    return (Path(cfg).expanduser().parent if cfg else Path("~/.config/herdr").expanduser()) / "herdr.sock"


def has_herdr() -> bool:
    bin_path = os.environ.get("HERDR_BIN_PATH")
    if bin_path:
        return os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)
    return bool(shutil.which("herdr") or (os.path.isfile(os.path.expanduser("~/.local/bin/herdr")) and os.access(os.path.expanduser("~/.local/bin/herdr"), os.X_OK)))


def server_up() -> bool:
    """A herdr server is listening. Probes the socket, so it never runs the herdr CLI."""
    if not has_herdr():
        return False
    import socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        sock.connect(str(herdr_socket()))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def herdr_pane_cwd(pane_id: str) -> str | None:
    try:
        return (herdr("pane", "get", pane_id).get("pane") or {}).get("cwd")
    except (HerdrError, OSError):
        return None


def live_agents() -> list[dict]:
    try:
        return herdr("agent", "list").get("agents") or []
    except HerdrError:
        return []


def agent_get(pane_id: str) -> dict:
    info = herdr("agent", "get", pane_id)
    return info.get("agent") or info


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


def name_is_live(name: str) -> dict | None:
    try:
        info = herdr("agent", "get", name)
    except HerdrError:
        return None
    return info.get("agent") or info


def refresh_sessions(rows: list[dict], agents: list[dict] | None = None,
                     tab_labels: dict | None = None) -> None:
    """Keep roster pane bindings on the sitting that still occupies them.

    Same directory tree (or a tab that still bears the sitting name): fill
    session id, kind, and cwd from the live agent. A different project in that
    pane: unbind, so the name does not follow the new occupant.
    """
    if agents is None:
        snap = snapshot()
        agents = snap.get("agents") or live_agents()
        if tab_labels is None:
            tab_labels = {t.get("tab_id"): t.get("label") or "" for t in snap.get("tabs") or []}
    tab_labels = tab_labels or {}
    by_pane = {a["pane_id"]: a for a in agents if a.get("pane_id")}
    changed = False
    for r in rows:
        pane = str(r.get("pane_id") or "")
        if not pane or pane.startswith("pid:"):
            continue
        a = by_pane.get(pane)
        if not a:
            continue
        label = tab_labels.get(a.get("tab_id") or "", "")
        if occupant_same(r, a, label):
            before = (r.get("pane_id"), r.get("session_id"), r.get("kind"), r.get("harness"),
                      r.get("cwd"), r.get("tab_id"), r.get("workspace_id"), r.get("last_seen"))
            bind_sitting(r, a)
            if before != (r.get("pane_id"), r.get("session_id"), r.get("kind"), r.get("harness"),
                          r.get("cwd"), r.get("tab_id"), r.get("workspace_id"), r.get("last_seen")):
                changed = True
        else:
            unbind_sitting(r)
            changed = True
    if changed:
        save_roster(rows)


def pick_workspace(ctx: Ctx, root: str) -> str | None:
    """The caller's workspace when it has a pane at/under root; else any such workspace."""
    panes = live_panes()
    if ctx.workspace_id and any(p.get("workspace_id") == ctx.workspace_id and p.get("cwd") and at_or_beneath(p["cwd"], root) for p in panes):
        return ctx.workspace_id
    for p in panes:
        if p.get("cwd") and at_or_beneath(p["cwd"], root):
            return p.get("workspace_id")
    return ctx.workspace_id


def workspace_by_label(label: str) -> str | None:
    return next((w["workspace_id"] for w in workspaces() if w.get("label") == label), None)


def project_workspace(root: str, ctx: Ctx) -> str | None:
    """An existing project line for this directory. Never creates one."""
    base = os.path.basename(os.path.realpath(root).rstrip(os.sep))
    labelled = workspace_by_label(f"proj:{base}") if base else None
    if labelled:
        return labelled
    if ctx.workspace_id:
        return ctx.workspace_id
    for p in live_panes():
        if p.get("cwd") and at_or_beneath(p["cwd"], root) and p.get("workspace_id"):
            return p["workspace_id"]
    return None


def split_pane(workspace_id: str, root: str) -> dict | None:
    """A new pane inside an existing workspace. None when that workspace has no pane to split."""
    panes = [p for p in live_panes() if p.get("workspace_id") == workspace_id and p.get("pane_id")]
    panes.sort(key=lambda p: 0 if p.get("cwd") and at_or_beneath(p["cwd"], root) else 1)
    if not panes:
        return None
    res = herdr("pane", "split", panes[0]["pane_id"], "--cwd", root, "--direction", "right", "--no-focus")
    pane = res.get("pane") or res.get("root_pane") or res
    if not isinstance(pane, dict):
        return None
    pid = pane.get("pane_id")
    if not pid:
        return None
    return {
        "pane_id": pid,
        "tab_id": pane.get("tab_id") or panes[0].get("tab_id"),
        "workspace_id": pane.get("workspace_id") or workspace_id,
    }


def create_tab(workspace_id: str | None, root: str, label: str, workspace_label: str | None = None) -> dict:
    """New tab labelled with the sitting. Identity is the tab, never pane --title / rename."""
    if workspace_id:
        res = herdr("tab", "create", "--workspace", workspace_id, "--cwd", root, "--label", label, "--no-focus")
    else:
        res = herdr("workspace", "create", "--cwd", root, "--label", workspace_label or os.path.basename(root) or root,
                    "--no-focus")
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
                  focus: bool = True, pen: bool = False, split: bool = False) -> dict:
    argv = build_argv(cat, h, model, lane, allow, deny, seat, resume_id, sitting=name, extra=extra)
    place = place_for(cat, root)
    base = ([h["command"], *argv] if is_wrapper(h) else [h["kind"], *argv])
    confined = unix_confine(cat, root, base)
    # --pen is a new machine-list line (the place's workspace_label).
    # Otherwise create a tab in the project workspace that already exists (proj:<dir>).
    if pen and place and place.get("workspace_label"):
        pane = create_tab(workspace_by_label(place["workspace_label"]), root, name, place["workspace_label"])
    else:
        wid = project_workspace(root, ctx)
        if split:
            pane = split_pane(wid, root) if wid else None
            if not pane:
                pane = create_tab(pick_workspace(ctx, root), root, name)
        else:
            pane = create_tab(wid or pick_workspace(ctx, root), root, name)
    pid = pane["pane_id"]
    launched_session = ""
    try:
        if confined != base or is_wrapper(h):
            wait_shell(pid)
            herdr("pane", "run", pid, shell_join(confined), expect_json=False)
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
    report_identity(pid, name, h["kind"], model)   # one publish per raise (LAB-297)
    # Tab stays the sitting. The pane border caches the harness just launched;
    # `cowbell reconcile` rewrites it from the agent Herdr detects.
    mark_harness(pid, name, h["id"])
    if focus and pane.get("tab_id"):
        try:
            herdr("tab", "focus", pane["tab_id"])
        except HerdrError:
            pass
    return pane


def focus_agent(pane_id: str) -> None:
    herdr("agent", "focus", pane_id)


# herdr sidebar tokens (event-driven; never a loop) — LAB-297.
# Identity tokens for sittings cowbell knows, published once per event — a raise,
# an adopt, or a state change seen during a picker refresh — with --source cowbell.
# No [[startup]], no interval thread; bt-reporter (the clocks) stays a separate,
# disabled tool. Sittings cowbell does not know stay unbadged until adopt.
# Glyphs are the sidebar's table (agent-deck's, by Charles's eye in Menlo 2026-09-12).

COWBELL_SOURCE = "cowbell"
TOKEN_TTL_MS = "86400000"


def glyph_for(kind: str, model: str = "") -> str:
    m = (model or "").lower()
    if kind == "claude":   # heavier glyph = bigger model (✳ sonnet/haiku, ✼ opus/fable/unknown)
        return "✳" if ("haiku" in m or "sonnet" in m) else "✼"
    return {"grok": "ϟ", "pi": "◇", "gemini": "✦", "codex": "◈"}.get(kind, "·")


def report_identity(pane_id: str, name: str, kind: str, model: str = "") -> bool:
    """One event, one publish: $glyph (badge), $line1 (glyph + sitting), display_agent."""
    if not pane_id or str(pane_id).startswith("pid:"):
        return False  # a process on another terminal has no herdr pane to badge
    g = glyph_for(kind, model)
    try:
        herdr("pane", "report-metadata", pane_id, "--source", COWBELL_SOURCE,
              "--ttl-ms", TOKEN_TTL_MS, "--display-agent", g,
              "--token", f"glyph={g}", "--token", f"line1={g} {name}")
    except HerdrError:
        return False
    return True


def clear_identity(pane_id: str) -> bool:
    """Drop cowbell's badge on a pane that no longer holds that sitting."""
    if not pane_id or str(pane_id).startswith("pid:"):
        return False
    try:
        herdr("pane", "report-metadata", pane_id, "--source", COWBELL_SOURCE,
              "--clear-display-agent", "--clear-token", "glyph", "--clear-token", "line1")
    except HerdrError:
        return False
    return True


def identity_events(seen: dict, live_rows: list[dict]) -> list[dict]:
    """Rostered herdr rows whose (sitting, harness, state) changed since the last
    refresh — each is one publish. Updates `seen` in place; no event, no publish."""
    events = []
    for r in live_rows:
        pane_id = str(r.get("pane_id") or "")
        if not r.get("on_roster") or pane_id.startswith("pid:") or not pane_id:
            continue  # sittings cowbell does not know stay unbadged until adopt
        key = (r.get("name"), r.get("kind"), r.get("status"))
        if seen.get(pane_id) == key:
            continue
        seen[pane_id] = key
        events.append(r)
    for pane_id in [p for p in seen if p not in {str(r.get("pane_id") or "") for r in live_rows}]:
        seen.pop(pane_id, None)
    return events


class HerdrLive(LiveSource):
    herdr = True

    def agents(self) -> list[dict]:
        return live_agents()

    def panes(self) -> list[dict]:
        return live_panes()

    def snapshot(self) -> dict:
        return snapshot()

    def agent(self, name: str) -> dict | None:
        return name_is_live(name)

    def refresh_sessions(self, rows: list[dict]) -> None:
        refresh_sessions(rows)

    def focus(self, row: dict) -> str:
        focus_agent(row["pane_id"])
        return f"focused {row.get('name') or row['pane_id']} in {row['pane_id']}"


class HerdrLaunch(HostLaunch):
    tabs = True

    def start(self, cat, h, name, root, ctx, *args, **kw):
        kw.pop("spawn", None)  # here-only; a herdr tab is already its own session
        return raise_harness(cat, h, name, root, ctx, *args, **kw)


def mark_harness(pane_id: str, sitting: str, harness: str) -> None:
    """Cache `sitting · harness` on the pane border. A failed rename does not fail the raise."""
    if not pane_id or str(pane_id).startswith("pid:"):
        return
    harness = (harness or "").strip()
    if not HARNESS_TOKEN.match(harness):
        return
    label = coherent_pane_label(sitting, harness)
    if not label or label == (sitting or "").strip():
        return
    try:
        herdr("pane", "rename", pane_id, label)
    except HerdrError:
        pass


def coherence_rows() -> list[dict]:
    """Claimed panes whose border disagrees with the sitting and the agent Herdr detects.

    The unscoped pane list omits the border label, so each workspace is listed on its own.
    Panes that make no harness claim are left out.
    """
    snap = snapshot()
    labels = tab_labels(snap)
    by_id = {p.get("pane_id"): p for p in (snap.get("panes") or []) if p.get("pane_id")}
    ws_ids = [w.get("workspace_id") for w in (snap.get("workspaces") or []) if w.get("workspace_id")]
    if not ws_ids:
        ws_ids = [w.get("workspace_id") for w in workspaces() if w.get("workspace_id")]
    out, seen = [], set()
    for wid in ws_ids:
        try:
            listed = herdr("pane", "list", "--workspace", wid).get("panes") or []
        except HerdrError:
            continue
        for pane in listed:
            pid = pane.get("pane_id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            detected = ((by_id.get(pid) or {}).get("agent") or pane.get("agent") or "").strip()
            label = pane.get("label") or ""
            sitting = (labels.get(pane.get("tab_id")) or "").strip()
            new = reconcile_label(label, sitting, detected, idle_shell=pane_is_idle_shell({**pane, "agent": detected}))
            if new:
                out.append({"pane_id": pid, "old": label, "new": new})
    return out


def apply_coherence(dry_run: bool = False) -> list[dict]:
    """Print `pane_id old → new` and rename, unless dry_run. Exit path stays with the caller."""
    rows = coherence_rows()
    if not rows:
        print("panes agree with the harness Herdr detects")
        return rows
    for row in rows:
        print(f"{row['pane_id']} {row['old']} → {row['new']}")
        if dry_run:
            continue
        try:
            herdr("pane", "rename", row["pane_id"], row["new"])
        except HerdrError as e:
            print(f"cowbell: {row['pane_id']} rename failed: {e}", file=sys.stderr)
    return rows


# --- commands ------------------------------------------------------------------

def cmd_registry(ctx: Ctx, cat: dict, root: str | None = None, as_json: bool = False,
                 place_name: str = "") -> dict:
    if place_name:
        pl = places(cat).get(place_name)
        if not pl:
            raise SystemExit(f"cowbell: no place {place_name!r} in {catalog_path()} (have: {', '.join(places(cat)) or 'none'})")
        root = pl["cwd"]
    root, place, remapped = resolve_root(cat, root or ctx.cwd)
    cat = scoped_catalog(cat, place)
    rows = load_roster()
    ctx.live.refresh_sessions(rows)
    agents, panes = ctx.live.agents(), ctx.live.panes()
    reg = build_registry(cat, root, agents, panes, rows, subject=trust_subject(root),
                         tab_label=tab_labels(ctx.live.snapshot()), history=discover_sessions(root, cat))
    if place:
        reg["place_note"] = place["name"] + (" · from ~" if remapped and root == place["cwd"] else "")
    if as_json:
        print(json.dumps(registry_json(reg, place), indent=2))
        return reg
    top = git_root(root)
    wider = None
    if top and os.path.realpath(top) != os.path.realpath(root):
        wider = sum(1 for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], top))
    print(render_registry(reg, top, wider))
    return reg


def cmd_enter(ctx: Ctx, cat: dict, name: str, root: str | None = None, target: dict | None = None) -> None:
    root, place, _ = resolve_root(cat, root or ctx.cwd)
    scoped = scoped_catalog(cat, place)
    r = target
    if r is None:
        rows = load_roster()
        hits = [x for x in rows if x.get("name") == name and at_or_beneath(x.get("cwd", ""), root)]
        if not hits:
            hits = [x for x in rows if x.get("name") == name]
        if not hits:
            hits = [x for x in rows if x.get("session_id") == name or (x.get("session_id") or "").startswith(name)]
        if not hits:
            hits = [x for x in rows if name in x.get("name", "")]
        if not hits:
            history = discover_sessions(root, cat)
            hits = [x for x in history if x.get("name") == name]
            if not hits:
                hits = [x for x in history if x.get("session_id") == name or (x.get("session_id") or "").startswith(name)]
            if not hits:
                hits = [x for x in history if name in x.get("name", "")]
            if not hits:
                hits = [x for x in history if name in (x.get("title") or "")]
        if len(hits) == 1:
            r = hits[0]
        elif len(hits) > 1:
            exact = [x for x in hits if x.get("name") == name or x.get("session_id") == name]
            if len(exact) == 1:
                r = exact[0]
            else:
                matches = ", ".join(repr(x.get("name") or (x.get("session_id") or "")[:8]) for x in hits[:5])
                raise SystemExit(f"cowbell: ambiguous session {name!r} matches: {matches}")
        else:
            raise SystemExit(f"cowbell: no session {name!r} on roster or in history")
    live = ctx.live.agent(r["name"])
    if live and live.get("pane_id"):
        print(ctx.live.focus({**live, "name": r["name"]}))
        return
    for a in ctx.live.agents():
        if r.get("pane_id") and a.get("pane_id") == r.get("pane_id"):
            print(ctx.live.focus({**a, "name": r["name"]}))
            return
    hid = r.get("harness") or r.get("kind") or ""
    h = harness_by_id(scoped, hid) or next((x for x in scoped.get("harness", []) if x.get("kind") == r.get("kind")), None)
    if not h:
        where = f"place {place['name']}" if place else "this catalog"
        raise SystemExit(f"cowbell: {r['name']} uses harness {hid!r} which may not resume in {where}")
    cat = scoped
    seat = Path(r["seat"]) if r.get("seat") and Path(r["seat"]).is_file() else None
    pane = launch(cat, h, r["name"], r.get("cwd") or root or ctx.cwd, ctx, r.get("model", ""), r.get("lane", ""),
                  r.get("allow", ""), r.get("deny", ""), seat, resume_id=r.get("session_id", ""))
    pid = (pane or {}).get("pane_id") or ""
    print(f"resumed {r['name']} ({h['id']}){(' in ' + pid) if pid else ''}")


def adopt_pane_from_choice(choice: dict | None) -> str | None:
    """A highlighted Active row, when the cursor can land on one."""
    if choice and choice.get("action") == "focus":
        return (choice.get("target") or {}).get("pane_id")
    return None


def adopt_pane_for_picker(choice: dict | None, live: list[dict], focused_pane_id: str | None) -> str | None:
    """Which pane `a` adopts. Active is a census, so the cursor is usually on Resume.

    Prefer a highlighted Active row. Otherwise the focused agent, when that pane
    is in the census (the picker popup itself is not). One running sitting is
    enough on its own. Several, with focus on a shell, is not a guess.
    """
    pane = adopt_pane_from_choice(choice)
    if pane:
        return pane
    ids = [r.get("pane_id") for r in live if r.get("pane_id")]
    if focused_pane_id and focused_pane_id in ids:
        return focused_pane_id
    if len(ids) == 1:
        return ids[0]
    return None


def adopt_picker_flash(n_live: int) -> str:
    if n_live > 1:
        return "several running — cowbell adopt <name>"
    return "no running sitting here to adopt"


def sitting_of(a: dict, labels: dict | None = None) -> str:
    """The tab label (sidebar sitting), else herdr's live agent name."""
    return ((labels or {}).get(a.get("tab_id") or "", "") or a.get("name") or "").strip()


def live_agent_to_adopt(ctx: Ctx, pane_id: str | None = None, hint: str | None = None) -> dict:
    """A live herdr pane or process-scan row to put on the roster.

    `herdr agent list` often has focused=False on every row. The focused pane
    is snapshot.focused_pane_id (and panes[].focused). Typing `cowbell adopt`
    in a shell makes that shell the focused pane, so a sitting name or pane id
    in `hint` is tried before giving up. The picker passes a census pane as
    `pane_id` when it can tell which sitting `a` means.
    """
    agents = ctx.live.agents()
    by_pane = {a.get("pane_id"): a for a in agents if a.get("pane_id")}
    snap = ctx.live.snapshot() or {}
    labels = tab_labels(snap)

    def from_pane(pid: str | None) -> dict | None:
        return by_pane.get(pid) if pid else None

    def from_hint(word: str | None) -> dict | None:
        if not word:
            return None
        hit = from_pane(word)
        if hit:
            return hit
        hits = [a for a in agents if (sit := sitting_of(a, labels))
                and (sit == word or slug(sit) == slug(word))]
        if len(hits) > 1:
            raise SystemExit(f"cowbell: ambiguous sitting {word!r}")
        return hits[0] if hits else None

    if pane_id:
        hit = from_pane(pane_id)
        if not hit:
            raise SystemExit(f"cowbell: no live agent in pane {pane_id}")
        return hit
    # Typed inside an agent pane: adopt that sitting.
    hit = from_pane(ctx.pane_id)
    if hit:
        return hit
    hit = from_pane(snap.get("focused_pane_id"))
    if hit:
        return hit
    for p in snap.get("panes") or []:
        if p.get("focused"):
            hit = from_pane(p.get("pane_id"))
            if hit:
                return hit
    focused = [a for a in agents if a.get("focused")]
    if focused:
        return focused[0]
    hit = from_hint(hint)
    if hit:
        return hit
    here = [a for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], ctx.cwd)]
    if len(here) == 1:
        return here[0]
    if len(agents) == 1:
        return agents[0]
    show, scope = (here[:12], " here") if here else (agents[:12], "")
    lines = [f"cowbell: no focused agent pane to adopt. Live{scope}:"]
    for a in show:
        bits = [a.get("pane_id") or "?", sitting_of(a, labels), a.get("agent") or "?",
                tilde(a.get("cwd") or "")]
        lines.append("  " + "  ".join(b for b in bits if b))
    lines.append("cowbell adopt --pane ID  (or a sitting name)")
    raise SystemExit("\n".join(lines))


def cmd_adopt(ctx: Ctx, cat: dict, name: str | None = None, pane_id: str | None = None) -> dict:
    """Bind a live sitting cowbell did not start onto the roster. Returns the new row."""
    a = live_agent_to_adopt(ctx, pane_id, hint=name)
    kind = a.get("agent") or ""
    if not kind:
        raise SystemExit(f"cowbell: pane {a.get('pane_id')} hosts no recognized agent")
    if getattr(ctx.live, "herdr", False) and not str(a.get("pane_id") or "").startswith("pid:"):
        try:
            extra = agent_get(a["pane_id"])
            if extra:
                a = {**a, **extra}
                kind = a.get("agent") or kind
        except HerdrError:
            pass
    snap = ctx.live.snapshot()
    sitting = sitting_of(a, tab_labels(snap))
    title = a.get("terminal_title_stripped") or ""
    noisy = (not title or title.startswith("-") or " - grok " in title.lower()
             or "waiting" in title.lower() or "running:" in title.lower())
    # `cowbell adopt wK:pK` used the pane as identity, not as the roster sitting.
    roster_as = None if name and name == a.get("pane_id") else name
    base = slug(roster_as or sitting or (title if not noisy else kind))
    rows = load_roster()
    nm = unique_name(base, {r["name"] for r in rows if r.get("pane_id") != a.get("pane_id")})
    rows = [r for r in rows if r.get("pane_id") != a.get("pane_id")]
    h = harness_by_id(cat, kind)
    cwd = a.get("cwd") or ctx.cwd
    place = place_for(cat, cwd)
    row = {
        "name": nm, "cwd": cwd, "harness": h["id"] if h else kind, "kind": kind,
        "model": "", "lane": "", "session_id": (a.get("agent_session") or {}).get("value") or "",
        "pane_id": a.get("pane_id"), "tab_id": a.get("tab_id"), "workspace_id": a.get("workspace_id"),
        "label": title or nm, "created": now(), "source": "adopt",
        "place": (place or {}).get("name") or "",
    }
    rows.append(row)
    save_roster(rows)
    apane = str(a.get("pane_id") or "")
    if apane and not apane.startswith("pid:"):
        report_identity(apane, nm, kind)   # one publish per adopt (LAB-297)
    print(f"adopted {nm}  {kind}  {tilde(cwd)}")
    return row


def cmd_list(ctx: Ctx, all_rows: bool) -> None:
    rows = load_roster()
    ctx.live.refresh_sessions(rows)
    by_pane = {a["pane_id"]: a for a in ctx.live.agents() if a.get("pane_id")}
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
        print(f"cowbell: ! {trust_note(cat_h['id'], v, root)}", file=sys.stderr)
    return v


def cmd_new(ctx: Ctx, cat: dict, hid: str, model="", lane="", allow="", deny="", name="", no_seat=False,
            assume_yes=False, root: str | None = None, extra: list[str] | None = None, focus: bool = True,
            place: str = "", spawn=None, pen: bool = False, split: bool = False) -> None:
    if place:
        pl = places(cat).get(place)
        if not pl:
            raise SystemExit(f"cowbell: no place {place!r} in {catalog_path()} (have: {', '.join(places(cat)) or 'none'})")
        root = pl["cwd"]
    root, pl, _ = resolve_root(cat, root or ctx.cwd)
    scoped = scoped_catalog(cat, pl)
    if not hid:
        hid = (pl or {}).get("default_harness") or scoped.get("defaults", {}).get("default_harness") or ""
        if not hid:
            raise SystemExit("new --harness <id> (or set default_harness on the place)")
    if pl and harness_by_id(cat, hid) and not harness_by_id(scoped, hid):
        raise SystemExit(f"cowbell: {hid} may not start in place {pl['name']} "
                         f"(allowed: {', '.join(pl.get('allow_harness') or []) or 'none'})")
    cat = scoped
    h = harness_by_id(cat, hid)
    if not h:
        raise SystemExit(f"cowbell: unknown harness {hid!r} (see: cowbell catalog)")
    if not installed(h):
        raise SystemExit(f"cowbell: {hid} is not installed here ({h.get('command') or h['kind']})")
    lane = lane or ("" if is_wrapper(h) else cat["defaults"].get("lane", "yellow"))
    if lane and lane not in LANES:
        raise SystemExit(f"cowbell: lane must be one of {', '.join(LANES)}")
    model = model or (h["models"][0] if h["models"] else "")
    seat = None if (no_seat or not prompt_flag(h)) else seat_file(root)
    gate(h, root, interactive=False, assume_yes=assume_yes)
    base = slug(name or h["id"])
    nm = unique_name(base, live_names(ctx.live.snapshot()) | {r["name"] for r in load_roster()})
    pane = launch(cat, h, nm, root, ctx, model, lane, allow, deny, seat, extra=extra, focus=focus, spawn=spawn,
                  pen=pen)
    print(f"raised {nm}  {h['id']}{(' ' + model) if model else ''}{(' ' + lane) if lane else ''}  in {pane['pane_id']}  {tilde(root)}")


def cmd_dispatch(ctx: Ctx, cat: dict, hid: str, assume_yes: bool) -> None:
    """`cowbell claude`: attach to that harness here if one exists, else raise it with defaults."""
    root, place, _ = resolve_root(cat, ctx.cwd)
    reg = build_registry(scoped_catalog(cat, place), root, ctx.live.agents(), ctx.live.panes(), load_roster(),
                         matcher=lambda _tool: {"verdict": "unknown"})
    for r in reg["live"]:
        if r["harness"] == hid or r["kind"] == hid:
            print(ctx.live.focus(r))
            return
    for r in reg["cold"]:
        if (r.get("harness") or r.get("kind")) == hid:
            cmd_enter(ctx, cat, r["name"], root, target=r)
            return
    cmd_new(ctx, cat, hid, assume_yes=assume_yes, root=root)


def cmd_catalog(cat: dict) -> None:
    print(f"{'harness':<16} {'kind':<8} {'egress':<7} {'installed':<9} {'badge':<10} title")
    for h in cat.get("harness", []):
        print(f"{h['id']:<16} {h['kind']:<8} {GLYPH.get(h['egress'], '?') + ' ' + h['egress']:<7} "
              f"{'yes' if installed(h) else 'no':<9} {trust_badge(h['trust_tool']):<10} {h.get('title', '')}")


# --- files -------------------------------------------------------------------

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

    Returns ("pick", i), ("key", ch, i) for a hotkey, clicked hint or clicked `line_keys` line, ("cancel",),
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
    flash = ((flash or "").splitlines() or [""])[0]
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
                                return ("key", hit, selected)
                        elif line in (line_keys or {}) and y <= view:
                            return ("key", line_keys[line], selected)
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
                    return ("key", val.lower(), selected)
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
    note = {"blocked": paint("  ⊘ blocked here", RED), "overprivilege": paint("  ! more reach than needed", FAINT)}
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
    taken = live_names(ctx.live.snapshot()) | {r["name"] for r in load_roster()}
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
    seat = seat_file(root) if prompt_flag(h) else None
    if v["verdict"] == "overprivilege":
        print(f"  {paint('! ' + trust_note(h['id'], v, root), FAINT)}")
    argv = build_argv(cat, h, model, lane, "", "", seat, sitting=name)
    shown = shell_join([h["command"], *argv]) if is_wrapper(h) else shell_join([h["kind"], *argv])
    if ctx.mode != "here":
        print(f"\n  {paint(name, INK, bold=True)}  {paint(shown, MUTED)}")
    pane = launch(cat, h, name, root, ctx, model, lane, "", "", seat)
    print(f"  started {name} in {pane['pane_id']}")
    return True


def raise_quick(ctx: Ctx, cat: dict, hid: str, root: str) -> bool:
    """New with defaults: automatic name (unless [defaults] ask_name), default permissions, open now."""
    h = harness_by_id(cat, hid)
    if not h:
        print(f"  unknown harness {hid}"); return False
    if not installed(h):
        print(f"  {hid} is not installed here"); return False
    v = trust_match(h["trust_tool"], root)
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {hid} on {tilde(root)} is blocked {v['flag']}"); return False
    taken = live_names(ctx.live.snapshot()) | {r["name"] for r in load_roster()}
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
        print(f"  {paint('! ' + trust_note(h['id'], v, root), FAINT)}")
    model = h["models"][0] if h["models"] else ""
    seat = seat_file(root) if prompt_flag(h) else None
    pane = launch(cat, h, name, root, ctx, model, lane, "", "", seat)
    print(f"  started {name} in {pane['pane_id']}")
    return True


PICKER_HINTS = [("⏎", "open", "enter"), ("n", "new with options", "n"), ("a", "adopt", "a"),
                ("g", "widen", "g"), ("esc", "close", "esc")]
REFRESH_S = 2.0


def footer(can_widen: bool = False) -> str:
    hints = [("number", "= open"), ("[n]", "new with options")] + ([("[g]", "widen")] if can_widen else []) + [("[q]", "")]
    return "  " + "   ".join(f"{paint(k, GOLD if k == 'number' else MUTED)}{(' ' + paint(v, FAINT)) if v else ''}"
                            for k, v in hints)


def _reclaim_cold(live: dict, cold: list[dict]) -> dict | None:
    """A cold sitting that is this live agent, not a new name."""
    sid = live.get("session_id") or ""
    if sid:
        hits = [r for r in cold if r.get("session_id") == sid]
        if len(hits) == 1:
            return hits[0]
    name = live.get("name") or ""
    cwd = live.get("cwd") or ""
    by_name = [r for r in cold if name and r.get("name") == name]
    if len(by_name) == 1 and (not cwd or not by_name[0].get("cwd") or same_tree(cwd, by_name[0]["cwd"])):
        return by_name[0]
    by_cwd = [r for r in cold if cwd and r.get("cwd") and same_tree(cwd, r["cwd"])]
    if len(by_cwd) == 1:
        return by_cwd[0]
    return None


def remember_live(reg: dict, rows: list[dict]) -> int:
    """Running agents go on the roster as they are seen, so they can be resumed later. No adopt step.

    A cold sitting whose session, name+cwd, or unique cwd matches is rebound
    instead of minting a second name (herdr-dev-2).
    """
    taken = {r["name"] for r in rows}
    live_ids = {str(x.get("pane_id") or "") for x in reg["live"] if x.get("pane_id")}
    added = 0
    reclaimed = 0
    for r in reg["live"]:
        if r.get("on_roster") or not r.get("session_id"):
            continue
        cold = [x for x in rows if not x.get("pane_id") or str(x.get("pane_id") or "") not in live_ids]
        hit = _reclaim_cold(r, cold)
        if hit:
            bind_sitting(hit, r)
            r["on_roster"] = True
            reclaimed += 1
            continue
        name = unique_name(slug(r["name"]) or slug(r.get("kind") or "agent"), taken)
        rows.append({"name": name, "cwd": r["cwd"], "harness": r["harness"], "kind": r["kind"], "model": "",
                     "lane": "", "session_id": r["session_id"], "pane_id": r["pane_id"],
                     "workspace_id": r.get("workspace_id"), "label": r["name"], "created": now(),
                     "last_seen": now()[:10], "place": r.get("place", ""), "source": "seen"})
        r["on_roster"] = True
        taken.add(name)
        added += 1
    if added or reclaimed:
        save_roster(rows)
    return added


def picker(ctx: Ctx, cat: dict) -> int:
    full_cat = cat
    root, _, remapped = resolve_root(full_cat, ctx.cwd)
    tui = tui_ok()
    scr = Screen() if tui else None
    live = ctx.live
    sel_key, sel_at, flash, expanded = "", 0, "", set()
    verdicts: dict[tuple[str, str], dict] = {}
    git_tops: dict[str, str | None] = {}
    ident_seen: dict[str, tuple] = {}   # pane_id -> (name, kind, status) last published

    def matcher_for(r):
        def m(tool):
            if (tool, r) not in verdicts:
                verdicts[(tool, r)] = trust_match(tool, r)
            return verdicts[(tool, r)]
        return m

    try:
        while True:
            place = place_for(full_cat, root)
            cat = scoped_catalog(full_cat, place)  # flows below (wizard, quick start) see only this place's harnesses
            rows = load_roster()
            live.refresh_sessions(rows)
            snap = live.snapshot()
            agents, panes = live.agents(), live.panes()
            if root not in git_tops:
                git_tops[root] = git_root(root)
            top = git_tops[root]
            wider = None
            if top and os.path.realpath(top) != os.path.realpath(root):
                wider = sum(1 for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], top))
            reg = build_registry(cat, root, agents, panes, rows, matcher=matcher_for(root),
                                 tab_label=tab_labels(snap), history=discover_sessions(root, cat))
            if place:
                reg["place_note"] = place["name"] + (" · from ~" if remapped and root == place["cwd"] else "")
            remember_live(reg, rows)
            # LAB-297: publish sidebar identity once per event (a state change seen
            # here, or a name/harness change after an adopt) — never per refresh.
            for r in identity_events(ident_seen, reg["live"]):
                report_identity(r["pane_id"], r["name"], r.get("kind") or "", r.get("model") or "")
            ws_labels = {w.get("workspace_id"): w.get("label") or "" for w in snap.get("workspaces") or []}
            choices = picker_choices(reg, rows, ctx.workspace_id, ws_labels, expanded)
            width = min(shutil.get_terminal_size((84, 24)).columns, 100)
            text = render_picker(reg, choices, top, wider, width=width)
            can_widen = bool(top and wider is not None and wider > len(reg["live"]))
            if tui:
                openable = [c for c in choices if picker_selectable(c)]
                keys = [c.get("key") for c in openable]
                selected = keys.index(sel_key) if sel_key in keys else min(sel_at, max(0, len(keys) - 1))
                hints = [hh for hh in PICKER_HINTS if hh[2] != "g" or can_widen]
                res = menu([""] + text.split("\n"), [c["row"] + 1 for c in openable], hints,
                           selected=selected, hotkeys="nwga", flash=flash,
                           line_keys={1: "g"} if can_widen else None,
                           numbered=[i for i, c in enumerate(openable) if c.get("idx")], scr=scr, tick=REFRESH_S)
                if res[0] in ("pick", "tick") and openable:
                    sel_key, sel_at = openable[res[1]].get("key") or "", res[1]
                elif res[0] == "key" and len(res) > 2 and openable and 0 <= res[2] < len(openable):
                    sel_key, sel_at = openable[res[2]].get("key") or "", res[2]
                if res[0] == "tick":
                    continue
                flash = ""
                if res[0] == "cancel":
                    return 0
                if res[0] == "pick":
                    hit = openable[res[1]]
                    raw = ""
                elif res[0] == "key":
                    raw = res[1]
                    # only `a` means "this highlighted row"; n/w/g must not focus it
                    hit = (openable[sel_at] if raw == "a" and openable and 0 <= sel_at < len(openable)
                           else None)
                else:
                    hit, raw = None, res[1] if len(res) > 1 else ""
                if hit and hit["action"] == "more" and raw != "a":
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
            if raw == "a":
                pane = adopt_pane_for_picker(hit, reg["live"], (snap or {}).get("focused_pane_id"))
                if not pane and tui:
                    flash = adopt_picker_flash(len([r for r in reg["live"] if r.get("pane_id")]))
                    continue
                try:
                    cmd_adopt(ctx, full_cat, pane_id=pane)
                    flash = "adopted"
                except SystemExit as e:
                    if tui:
                        flash = str(e).splitlines()[0]
                    else:
                        print(f"  {e}")
                continue
            if raw not in ("w", "n") and not hit:
                continue
            if hit and hit["action"] == "focus":
                continue  # Active is a census; cowbell cannot take over a sitting already running
            if scr:
                scr.close()
                sys.stdout.write("\x1b[H\x1b[2J")  # prompts that follow start on a clean popup
            try:
                if raw in ("w", "n"):
                    done = wizard(ctx, cat, root)
                elif hit["action"] == "enter":
                    cmd_enter(ctx, cat, hit["target"]["name"], root, target=hit["target"])
                    done = True
                else:
                    done = raise_quick(ctx, cat, hit["target"]["harness"], root)
            except KeyboardInterrupt:  # esc / ctrl-c inside a prompt: back to the list
                continue
            except (HerdrError, SystemExit) as e:
                flash = ((str(e) or "").splitlines() or ["error"])[0]
                if not tui:
                    print(f"  {e}")
                continue
            if done:
                return 0
    finally:
        if scr:
            scr.close()


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
        elif a in ("--yes", "-y", "--all", "--no-seat", "--no-focus", "-i", "--json", "--spawn",
                   "--pen", "--split", "--dry-run", "--help", "-h"):
            opts[a.lstrip("-")] = True
        elif a.startswith("--") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]; i += 1
        else:
            rest.append(a)
        i += 1
    return opts, rest


def choose_host(herdr_up: bool, env=None) -> Ctx:
    """Once per run. An agent caller inside a herdr pane gets herdr tabs. A person, anywhere else, or
    COWBELL_MODE=here: this terminal. Herdr's agents are listed only when a server is actually listening."""
    env = os.environ if env is None else env
    wants_tab = env.get("COWBELL_MODE") == "tab" or (caller_is_agent(env) and env.get("COWBELL_MODE") != "here")
    if wants_tab and herdr_up and in_herdr_pane(env):
        return Ctx.from_env(env)
    return Ctx(cwd=os.getcwd(), mode="here", live=CombinedLive(HerdrLive() if herdr_up else None, ProcessScan()))


NEW_OPTS = frozenset({
    "harness", "model", "lane", "allow", "deny", "name", "place", "root",
    "spawn", "pen", "split", "no-seat", "no-focus", "yes", "y", "help", "h",
})


def new_usage(file=None) -> None:
    print("cowbell new [harness] [options...] [-- extra...]", file=file)
    print("  --harness ID    Harness to run (or the first positional)", file=file)
    print("  --model M       Override the harness default model", file=file)
    print("  --lane L        Queue lane (default: yellow)", file=file)
    print("  --allow T       Tool allow list passed to the harness", file=file)
    print("  --deny T        Tool deny list passed to the harness", file=file)
    print("  --name N        Sitting name", file=file)
    print("  --place P       Place whose cwd, workspace, and default harness apply", file=file)
    print("  --root DIR      Directory to raise in", file=file)
    print("  --spawn         Start the agent and return immediately", file=file)
    print("  --pen           Open the place's own herdr pen", file=file)
    print("  --split         Split a pane instead of opening a new tab", file=file)
    print("  --no-seat       Do not attach a seat file", file=file)
    print("  --no-focus      Do not focus the new pane", file=file)
    print("  -y, --yes       Assume yes to prompts", file=file)
    print("  -h, --help      Print this usage and exit", file=file)
    print("Tokens after -- are passed to the harness. Any other flag exits 2.", file=file)


def split_passthrough(args: list[str]) -> tuple[list[str], list[str]]:
    """Flags before the first -- belong to cowbell. Tokens after it go to the harness."""
    if "--" in args:
        i = args.index("--")
        return args[:i], args[i + 1:]
    return args, []


def new_unknown_flags(opts: dict, pos: list[str]) -> list[str]:
    bad = []
    for k in opts:
        if k not in NEW_OPTS:
            bad.append(f"-{k}" if len(k) == 1 else f"--{k}")
    bad += [p for p in pos if p.startswith("-")]
    return bad


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    if argv and argv[0] in ("-V", "--version"):
        print(f"cowbell {__version__}"); return 0
    if not argv:
        argv = ["picker"] if tui_ok() else ["status"]
    cmd, *rest = argv
    if cmd == "new":
        pre, passthrough = split_passthrough(rest)
        opts, pos = parse_flags(pre)
        if opts.get("help") or opts.get("h") or (pos and pos[0] in ("-h", "--help")):
            new_usage()
            return 0
        bad = new_unknown_flags(opts, pos)
        if bad:
            print(f"cowbell: unknown flag {bad[0]}", file=sys.stderr)
            new_usage(sys.stderr)
            return 2
        ctx = choose_host(server_up())
        cat = load_catalog()
        hid = opts.get("harness") or (pos[0] if pos else "")
        extra_pos = pos[1:] if pos and pos[0] == hid else pos
        cmd_new(ctx, cat, hid, opts.get("model", ""), opts.get("lane", ""), opts.get("allow", ""), opts.get("deny", ""),
                opts.get("name", ""), bool(opts.get("no-seat")), bool(opts.get("yes")), opts.get("root"),
                extra=extra_pos + passthrough, focus=not opts.get("no-focus"),
                place=opts.get("place", ""), spawn=bool(opts.get("spawn")), pen=bool(opts.get("pen")),
                split=bool(opts.get("split")))
        return 0
    opts, pos = parse_flags(rest)
    if cmd == "adopt" and (opts.get("help") or opts.get("h") or (pos and pos[0] in ("-h", "--help"))):
        print("cowbell adopt [name] [--pane ID]")
        print("  Bind a live sitting cowbell did not start onto the roster.")
        print("  Default: this pane if it is an agent, else the focused agent pane.")
        print("  name may be a pane id or a sitting. In the picker, a adopts the")
        print("  focused agent in Active, or the only running sitting.")
        return 0
    if cmd == "reconcile" and (opts.get("help") or opts.get("h")):
        print("cowbell reconcile [--dry-run]")
        print("  Rewrite pane borders that claim a harness so they match the agent Herdr detects.")
        print("  The tab label stays the sitting. A border with no harness claim is left alone.")
        return 0
    ctx = choose_host(server_up())
    cat = load_catalog()
    if cmd == "picker":
        return picker(ctx, cat)
    if cmd in ("registry", "here", "status"):
        cmd_registry(ctx, cat, opts.get("root"), as_json=bool(opts.get("json")),
                     place_name=opts.get("place") or ""); return 0
    if cmd == "list":
        cmd_list(ctx, bool(opts.get("all"))); return 0
    if cmd == "catalog":
        cmd_catalog(cat); return 0
    if cmd == "enter":
        if not pos:
            raise SystemExit("enter <name>")
        cmd_enter(ctx, cat, pos[0]); return 0
    if cmd == "adopt":
        cmd_adopt(ctx, cat, name=pos[0] if pos else None, pane_id=opts.get("pane"))
        return 0
    if cmd == "reconcile":
        apply_coherence(dry_run=bool(opts.get("dry-run")))
        return 0
    if harness_by_id(cat, cmd):
        cmd_dispatch(ctx, cat, cmd, bool(opts.get("yes"))); return 0
    raise SystemExit(f"cowbell: unknown command or harness {cmd!r} (see: cowbell catalog)")


def cli() -> None:
    """Console-script entry (`cowbell` when installed with pip or uv)."""
    try:
        sys.exit(main(sys.argv[1:]))
    except HerdrError as e:
        print(f"cowbell: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
