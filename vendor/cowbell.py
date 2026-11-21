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
  cowbell rename <name-or-id> <new>  pin a session name; refresh will not replace it
  cowbell adopt [name] [--pane ID]  put a live sitting cowbell did not start on the roster
  cowbell list [--all]          roster rows (default: this directory)
  cowbell catalog               every harness: kind, egress, installed, trust badge
  cowbell accept [id ...]       run each row's `check` now and record it. Launch does this itself
                                when a row's last pass is over 7 days old, after its `requires`
  cowbell reconcile [--dry-run] rewrite a pane border from the sitting and the agent Herdr detects
  cowbell identity stamp        publish $glyph for every live herdr agent pane (detector kind)
  cowbell identity sync         restamp or clear one pane from a herdr occupancy event
  cowbell font [install]        is the icon font here; install fetches the pinned
                                lobe-icons.ttf (sha256 checked). Without it, kinds show
                                as letters (C claude, G grok, X codex …). COWBELL_GLYPHS=lobe|text forces it
  COWBELL_DEBUG=1 or --debug    append a timed trace to debug.log in the state dir
                                (override the file with COWBELL_DEBUG_LOG). Off unless set.

A person running cowbell gets the session in this terminal, including inside herdr
(`COWBELL_CALLER=person` forces that). An agent caller inside a herdr pane opens a new tab
in the project workspace (`COWBELL_MODE=here` forces this terminal; `COWBELL_MODE=tab` forces
a tab, which is how prefix+c still opens a tab). `--split` asks for a pane split instead of a
tab. `--pen` opens the place's own pen (a new machine-list line). Running also lists agent
processes found by working directory.

The herdr plugin (herdr-cowbell) adds prefix+c, stop and import-seats on top of this.

Stdlib only; python >= 3.11 (tomllib). The picker is raw termios/ANSI.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

__version__ = "0.3.4"
PLUGIN_ID = "herdr-cowbell"  # only to find config and state that 0.1–0.3 kept in herdr's plugin dirs
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
LANES = ("green", "yellow", "red")
READY = {"idle", "done"}
SHELLS = {"zsh", "bash", "fish", "sh", "dash", "ksh"}
GLYPH = {"local": "⌂", "mesh": "⌂", "vendor": "↑"}  # ⌂ ↑
LEGEND = "⌂ local  ↑ vendor · ok  ! more reach  ⊘ blocked  ? unknown"
DASH = "—"
GROK_AGENTS_DIR = Path("~/.grok/agents").expanduser()

# Timed trace for a slow launch. Off unless COWBELL_DEBUG or --debug is set, so a
# normal start does not write a file. The picker owns the terminal, so the trace
# goes to a file. One megabyte, then the previous file is kept as debug.log.1.
_DEBUG_ON = frozenset({"1", "true", "yes", "on", "verbose"})
DEBUG_LOG_MAX = 1_000_000


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _trace_field(value) -> str:
    text = str(value).replace("\n", " ").strip()
    text = text[:300]
    if not text or any(ch.isspace() for ch in text):
        return '"' + text.replace('"', "'") + '"'
    return text


class Trace:
    """Append-only phase log. A write that fails is ignored: debugging must not break a launch."""

    def __init__(self) -> None:
        self._forced = False
        self._dead = False
        self._lock = threading.Lock()
        self._fh = None
        self._born = time.perf_counter()

    def enabled(self) -> bool:
        if self._dead:
            return False
        if self._forced:
            return True
        return os.environ.get("COWBELL_DEBUG", "").strip().lower() in _DEBUG_ON

    def force(self) -> None:
        self._forced = True
        self._dead = False

    def reset(self) -> None:
        """Drop the open file. Tests use this so one case cannot write into the next."""
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            self._forced = False
            self._dead = False
            self._born = time.perf_counter()

    def event(self, kind: str, **fields) -> None:
        if not self.enabled():
            return
        parts = [f"{key}={_trace_field(val)}" for key, val in fields.items() if val is not None and val != ""]
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        line = f"{stamp}  {time.perf_counter() - self._born:8.3f}s  {kind}  " + " ".join(parts)
        try:
            with self._lock:
                if self._dead:
                    return
                fh = self._open()
                if fh is None:
                    return
                fh.write(line[:2000] + "\n")
                fh.flush()
        except OSError:
            self._dead = True

    def path(self) -> Path | None:
        override = os.environ.get("COWBELL_DEBUG_LOG", "").strip()
        if override:
            return Path(override).expanduser()
        try:
            return state_dir() / "debug.log"
        except OSError:
            return None

    def _open(self):
        if self._fh is not None:
            return self._fh
        path = self.path()
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > DEBUG_LOG_MAX:
            os.replace(path, path.with_name(path.name + ".1"))
        # 0600 at os.open creation, never a umask-mode window; O_NONBLOCK so a fifo
        # planted at the log path fails ENXIO instead of hanging the launch.
        # O_NOFOLLOW: a symlink planted at the log path is refused, never opened and fchmod'd.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):  # as _open_regular: a fifo, socket or device is no log
                raise OSError("not a regular file")
            os.fchmod(fd, 0o600)  # a log left by an older run keeps its old mode otherwise
            self._fh = os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        return self._fh


trace = Trace()


# --- redaction ----------------------------------------------------------------
# The trace is opt-in debugging, not a credential store: argv and free text go
# through these first. Flag names and the executable basename survive so the
# event still reads as a command line; every value that could carry a secret is
# masked. The launch sheet's printed command is the product and is never redacted.
REDACT = "***"
# A flag is sensitive when one of its dash/underscore/dot-separated parts names a secret:
# --api-key, --auth-token, --system-prompt, --apikey. A substring test also caught
# --dangerously-bypass-approvals-and-sandbox ("bypass" holds "pass"), a boolean flag.
_SECRET_PART = re.compile(
    r"(?:key|token|secret|prompt|instruction)s?$|^(?:pass(?:word|wd|phrase|code)?s?|o?auth(?:entication|orization)?|bearer|credentials?)$", re.I)
_FLAG_NAME_ONLY = re.compile(r"(?<![\w-])-{1,2}[A-Za-z][\w.-]*")
# one shell word: quoted runs and escapes join, so 'It'"'"'s a prompt' is masked whole
_FLAG_SEP_VALUE = re.compile(r"(=|[ \t]+)((?:'[^']*'|\"(?:[^\"\\]|\\.)*\"|\\.|[^\s'\"\\])+)")
_ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
# the name prefix before the secret word is optional so a name that starts with it
# (instructions=x, token=x) masks like one that only carries it (api_token=x);
# keyword-anywhere-in-name (unlike _SECRET_PART's whole-part rule) also catches
# safe-side names like author=/keyboard= — intentional, pinned in test_rc_redact.
#
# _mask_name_values (below) replaced a single backtracking regex here: a dotted run
# with no keyword (e.g. 'a.'*50000) made the old \b(prefix?)(keyword)[\w.]*= pattern
# retry its whole backtrack at every "a", O(n^2) on argv a launcher must not hang on.
_NAME_RUN = re.compile(r"[\w.]+")
_KEYWORD = re.compile(r"key|token|secret|pass|auth|bearer|credential|prompt|instruction", re.I)
_NAME_START_CHAR = re.compile(r"[A-Za-z_]", re.I)  # re.I matches the same 4 exotic codepoints the old pattern did
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]{1,15}://)[^/\s@'\"]+@")
_CRED_SHAPE = re.compile(
    r"\b(?:sk|pk)-[0-9A-Za-z_-]{4,}"
    r"|\bgh[pousr]_[0-9A-Za-z]{4,}"
    r"|\bgithub_pat_[0-9A-Za-z_]{6,}"
    r"|\bBearer[ \t]+[^\s'\"]+", re.I)


def _sensitive_flag(flag: str) -> bool:
    """True when a flag name (dashes included) names a secret by one of its whole parts."""
    return any(_SECRET_PART.search(part) for part in re.split(r"[-_.]+", flag.lstrip("-")) if part)


def _mask_flag_values(s: str) -> str:
    """Mask the value after each sensitive flag in free text. A flag consumes only its own
    value, so `--bypass --token abc` still masks abc; a bare -- is never a value."""
    out: list[str] = []
    pos = 0
    for m in _FLAG_NAME_ONLY.finditer(s):
        if m.start() < pos or not _sensitive_flag(m[0]):
            continue
        vm = _FLAG_SEP_VALUE.match(s, m.end())
        if not vm or vm[2] == "--":
            continue
        out += [s[pos:m.end()], vm[1], REDACT]
        pos = vm.end()
    out.append(s[pos:])
    return "".join(out)


def _name_value_mask_start(name: str):
    """Smallest index in `name` (a run of [\\w.] chars) a NAME=value match could start
    at: index 0 if it's a letter/underscore, else the first letter/underscore right
    after a '.' — the only two places \\b plus the old pattern's required first
    character could land. A smaller start always sees a superset of a larger one's
    text, so if this earliest start's tail has no keyword, no later start would
    either; one check replaces the old per-position backtrack."""
    if name and _NAME_START_CHAR.match(name[0]):
        return 0
    for i in range(1, len(name)):
        if name[i - 1] == "." and _NAME_START_CHAR.match(name[i]):
            return i
    return None


def _mask_name_values(s: str) -> str:
    """Mask the value of every NAME=value word whose name carries a secret keyword
    (api_token=x, model.instructions=x, author=x — see _NAME_START_CHAR comment above).
    Linear: runs of [\\w.] chars are found once (plain `+`, no backtracking), and a
    masked run's value — which can itself contain '=' — is skipped whole, exactly as
    the old regex's greedy value consumed it, so nothing inside is rematched."""
    runs = list(_NAME_RUN.finditer(s))
    out: list[str] = []
    pos = 0
    i = 0
    n = len(runs)
    while i < n:
        m = runs[i]
        a, b = m.start(), m.end()
        if b < len(s) and s[b] == "=":
            start = _name_value_mask_start(s[a:b])
            if start is not None and _KEYWORD.search(s[a + start:b]):
                c = b + 1
                while c < len(s) and not s[c].isspace() and s[c] not in "'\"":
                    c += 1
                out.append(s[pos:b + 1])
                out.append(REDACT)
                pos = c
                i += 1
                while i < n and runs[i].start() < pos:
                    i += 1
                continue
        i += 1
    out.append(s[pos:])
    return "".join(out)


def redact_text(s: str) -> str:
    """Mask credential shapes inside free text (check output, herdr errors): userinfo,
    sensitive flag values, NAME=value, and token-shaped words. Context around them survives.
    Other words are not masked: free text has no structure to tell a secret from prose
    (docs/debug-trace.md)."""
    s = _URL_USERINFO.sub(r"\g<1>***@", s)
    s = _mask_flag_values(s)
    s = _mask_name_values(s)
    return _CRED_SHAPE.sub(REDACT, s)


def redact_argv(argv) -> list[str]:
    """argv safe to log: basename of the executable and flag names stay; everything
    past a literal --, the value of a sensitive flag, and every NAME=value word is
    masked, and credential shapes inside a word are masked too."""
    out: list[str] = []
    after_dd = False   # a literal "--" masks the rest of the line
    flag_val = False   # previous word was a sensitive flag taking its value next
    for i, word in enumerate(argv):
        w = str(word)
        if i == 0:
            out.append(redact_text(os.path.basename(w) or w))
            continue
        if after_dd:
            out.append(REDACT)
            continue
        if flag_val and w != "--":  # a bare -- ends the options; it is never a flag's value
            out.append(REDACT)
            flag_val = False
            continue
        flag_val = False
        if w == "--":
            after_dd = True
        elif _ENV_ASSIGN.match(w):
            w = f"{_ENV_ASSIGN.match(w).group(1)}={REDACT}"
        elif w.startswith("-"):
            flag, eq, val = w.partition("=")
            if _sensitive_flag(flag):
                if eq:
                    w = f"{flag}={REDACT}"
                else:
                    flag_val = True
            elif eq:
                w = f"{flag}={redact_text(val)}"
        else:
            w = redact_text(w)
        out.append(w)
    return out


def redact_launch(cmd, passthrough) -> list[str]:
    """redact_argv for a launch command, plus the words typed after `--` in `cowbell new`.
    split_passthrough drops that `--`, so they ride in the command as ordinary words:
    find the run by position (its last occurrence) and mask every word of it."""
    out = redact_argv(cmd)
    words = [str(w) for w in cmd]
    tail = [str(w) for w in passthrough or []]
    n = len(tail)
    if n:
        for i in range(len(words) - n, 0, -1):
            if words[i:i + n] == tail:
                out[i:i + n] = [REDACT] * n
                break
    return out


class HerdrError(RuntimeError):
    pass


# --- colour ------------------------------------------------------------------
# Palette: mostly grey, with one accent. Reads the terminal theme, not a config file.
# Gold is Resume. Green is New Session. Blue is the numbers on rows you can open.
# Cyan (teal) is the folder path. Active heading and box are light gray (secondary).
# Sitting and harness names are ink.

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
    out = [f"  {paint('cowbell', INK, bold=True)}  {paint(tilde(reg['root']), TEAL)}   "
           f"{paint('subject:', FAINT)} {paint(reg['subject'] or '?', MUTED)}"
           + (f"   {paint('place:', FAINT)} {paint(note, MUTED)}" if note else "")]
    if git_top and os.path.realpath(git_top) != os.path.realpath(reg["root"]):
        hint = f"   ({wider} live under it)" if wider is not None else ""
        out.append(paint(f"  git root: {tilde(git_top)}{hint}", FAINT)
                   + (f"   {paint('[g]', MUTED)} {paint('widen', FAINT)}" if widen_hint else ""))
    out += warning_lines(reg)
    return out


def warning_lines(reg: dict, width: int | None = None) -> list[str]:
    """One dim line per thing that could not be read, under the header; cut to `width` in the picker."""
    lines = [f"  ! {w}" for w in reg.get("warnings") or []]
    return [paint(x[:max(20, width - 2)] if width else x, FAINT) for x in lines]


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


SCHEMA = 1  # catalog, roster, and pins format this cowbell reads and writes


class SchemaError(SystemExit):
    pass


_TIGHTEN_WARNED: set = set()


def _warn_tighten_once(p: Path, e: OSError) -> None:
    """Warn once per path per process — config_dir/state_dir run every picker frame."""
    if str(p) not in _TIGHTEN_WARNED:
        _TIGHTEN_WARNED.add(str(p))
        print(f"cowbell: could not tighten {p} to 0700: {e}", file=sys.stderr)


def config_dir() -> Path:
    """~/.config/cowbell (COWBELL_CONFIG_DIR, then XDG_CONFIG_HOME). Shared by the plugin and the CLI."""
    base = os.environ.get("COWBELL_CONFIG_DIR") or \
        Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "cowbell"
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.is_dir() and not p.is_symlink() and p.stat().st_mode & 0o777 != 0o700:
        try:
            os.chmod(p, 0o700)  # the catalog can carry secrets in check/requires argv
        except OSError as e:
            _warn_tighten_once(p, e)
    return p


def state_dir() -> Path:
    """~/.local/state/cowbell (COWBELL_STATE_DIR, then XDG_STATE_HOME)."""
    base = os.environ.get("COWBELL_STATE_DIR") or \
        Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "cowbell"
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.is_dir() and not p.is_symlink() and p.stat().st_mode & 0o777 != 0o700:
        try:
            os.chmod(p, 0o700)  # roster, accept.json and debug.log live here — owner only
        except OSError as e:
            _warn_tighten_once(p, e)
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
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise SchemaError(f"cowbell: {what}: schema must be an int")
    if schema > SCHEMA:
        raise SchemaError(f"cowbell: {what} is schema {schema}; cowbell {__version__} writes schema {SCHEMA}. "
                          f"Upgrade cowbell before changing it.")


def _open_regular(path: Path):
    """open() that cannot hang on a fifo: O_NONBLOCK returns at once without a writer,
    then fstat refuses anything that is not a regular file (fifo, socket, device)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _open_regular_write(path: Path):
    """Write side of _open_regular: O_NONBLOCK answers ENXIO at once on a fifo
    with no reader instead of blocking, then fstat refuses non-regular files."""
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        os.fchmod(fd, 0o600)  # a file left by an older run keeps its old mode otherwise
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _file_error(e: Exception) -> str:
    """One clause for a file cowbell refuses: the OS reason without the path
    repeated, a plain verdict on undecodable bytes, or the parser's detail."""
    if isinstance(e, UnicodeDecodeError):
        return "not valid UTF-8"
    return getattr(e, "strerror", None) or str(e)


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
    fill_missing_seed_kinds(p)
    return p


# Seed ids spliced once into an existing house catalog when `which` finds them.
# Not a PATH dump of AGENT_EXES; gemini/opencode omitted from a house copy stay omitted.
SEED_FILL_IDS = ("droid", "devin")


def seed_fill_marker_path() -> Path:
    return state_dir() / "seed-fill.json"


def _seed_fill_considered() -> set[str]:
    p = seed_fill_marker_path()
    if not p.is_file():
        return set()
    try:
        with _open_regular(p) as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return set()
    ids = data.get("ids") if isinstance(data, dict) else None
    return set(ids) if isinstance(ids, list) else set()


def _seed_fill_record(ids: set[str]) -> None:
    payload = json.dumps({"schema": 1, "ids": sorted(ids)}, indent=0) + "\n"
    write_atomic(seed_fill_marker_path(), payload)


def fill_missing_seed_kinds(path: Path) -> None:
    """Once: splice SEED_FILL_IDS from the seed if installed(). Not on every picker tick after the marker is complete."""
    if not path.is_file() or tomllib is None:
        return
    considered = _seed_fill_considered()
    pending = [hid for hid in SEED_FILL_IDS if hid not in considered]
    if not pending:
        return
    seed = seed_path()
    if not seed or not seed.is_file():
        return
    try:
        with _open_regular(seed) as fh:
            seed_cat = tomllib.load(fh)
        with _open_regular(path) as fh:
            house = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return
    by_id = {h["id"]: h for h in (seed_cat.get("harness") or []) if h.get("id")}
    rows = house.get("harness")
    have = {h.get("id") for h in rows if isinstance(h, dict)} if isinstance(rows, list) else set()
    now = set(considered)
    for hid in pending:
        rec = by_id.get(hid)
        if rec is None:
            continue
        if hid in have:
            now.add(hid)
            continue
        if installed(rec):
            try:  # a newer-schema catalog is read, never overwritten — skip the fill
                harness_insert({
                    "id": rec["id"],
                    "kind": rec.get("kind") or rec["id"],
                    "title": rec.get("title") or rec["id"],
                    "models": rec.get("models") or [],
                    "resume": rec.get("resume") or [],
                }, path=path)
            except SchemaError:
                pass
        now.add(hid)
    if now != considered:
        _seed_fill_record(now)


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
    # `model` is what the row launches with. herdr's quota_model token is a display
    # name ("Sonnet 5"), never a CLI's model id, so it does not land here.
    model = str(live.get("model") or "")
    if model and " " not in model.strip():
        row["model"] = model
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
    proc = run_bounded(["git", "-C", path, "rev-parse", "--show-toplevel"], GIT_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


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


# Continue-nudges and one-key replies. A harness title or a tab can still use
# these words; only a raw user line is rejected for being one of them.
_NUDGES = frozenset({".", "..", "...", "ok", "okay", "yes", "y", "n", "k", "continue"})


def usable_slug(text: str) -> str:
    """A session name taken from text that was already a name.

    Unlike `slug`, this does not invent an `h-` prefix. `slug(".")` is `h-`,
    and `h-` matches NAME_RE, but a dot is not a session. Empty when the
    cleaned form is shorter than two characters, is not a name, or is the
    fallback word `harness`.
    """
    s = re.sub(r"[^a-z0-9_-]+", "-", (text or "").strip().lower()).strip("-")[:32]
    if (not s or len(s) < 2 or not s[0].isalpha() or s in {"harness", "h", "h-"}
            or not NAME_RE.match(s)):
        return ""
    return s


def title_is_chrome(text: str) -> bool:
    """Harness chrome and terminal decorations are not a session name."""
    raw = (text or "").strip()
    if not raw:
        return True
    lower = raw.lower()
    if lower in _GENERIC_TITLES or lower.startswith("running:"):
        return True
    return raw.startswith("-")


def utterance_name(text: str) -> str:
    """First user line, after injected preambles are already gone. Nudges are not a name."""
    raw = " ".join((text or "").split())
    if not raw or raw.casefold() in _NUDGES or title_is_chrome(raw):
        return ""
    return usable_slug(raw)


def tab_name(text: str) -> str:
    if title_is_chrome(text):
        return ""
    return usable_slug(text)


def terminal_name(text: str) -> str:
    """A terminal title can name a session only when it is one short token, not a sentence."""
    raw = (text or "").strip()
    if raw.lower().startswith("devin:"):
        raw = raw.split(":", 1)[1].strip()
    if title_is_chrome(raw):
        return ""
    head = raw.split(" - ")[0].strip()
    if not head or " " in head or title_is_chrome(head):
        return ""
    return usable_slug(head)


def fallback_name(kind: str, session_id: str) -> str:
    """Never empty, never `.`, never the word `harness`.

    No session id yet: the harness id (`grok`). A real id: `{kind}` plus a short tail.
    """
    k = usable_slug(kind) or "session"
    tail = re.sub(r"[^a-z0-9]", "", (session_id or "").lower())[:4]
    if not tail:
        return k
    name = f"{k}-{tail}"[:32].strip("-")
    if NAME_RE.match(name) and len(name) >= 2 and name not in {"harness", "h", "h-"}:
        return name
    return k


def canonical_name(pinned: str = "", harness_title: str = "", tab: str = "", stored: str = "",
                   utterance: str = "", terminal: str = "", kind: str = "", session_id: str = "") -> tuple[str, str]:
    """One name for a resumable session, and which source supplied it.

    First hit wins. A pin (the name prompt, `cowbell rename`, adopt with a
    name) is the only source a later refresh will not replace. A harness
    title beats the herdr tab. The tab is where the session sits. A usable
    name already on the roster beats a user line, so a sitting recorded
    before any title file existed keeps its name. A non-chrome terminal
    title is a weak hint. Otherwise `{kind}` plus a short session-id tail.
    """
    if pinned:
        kept = pinned if (NAME_RE.match(pinned) and pinned not in {"harness", "h", "h-"}) else slug(pinned)
        if kept and kept not in {"harness", "h", "h-"} and NAME_RE.match(kept):
            return kept, "pinned"
    if not title_is_chrome(harness_title):
        hit = usable_slug(harness_title)
        if hit:
            return hit, "harness"
    hit = tab_name(tab)
    if hit:
        return hit, "tab"
    hit = usable_slug(stored)
    if hit:
        return hit, "roster"
    hit = utterance_name(utterance)
    if hit:
        return hit, "utterance"
    hit = terminal_name(terminal)
    if hit:
        return hit, "terminal"
    return fallback_name(kind, session_id), "fallback"


# Who keeps a shared title. A harness title is the session's own name. A tab
# is a place, and several panes can share it. A stored roster name is what an
# earlier pass wrote.
_BARE_RANK = {"harness": 0, "roster": 1, "tab": 2, "utterance": 3, "terminal": 4, "fallback": 5}


def _bare_owner(group: list[dict]) -> dict:
    """The row that keeps the shared name: best source, then earliest created."""
    def key(item: tuple[int, dict]) -> tuple:
        i, r = item
        return (
            _BARE_RANK.get(r.get("name_source") or "", 9),
            r.get("created") or "9999",
            r.get("session_id") or "",
            i,
        )
    return min(enumerate(group), key=key)[1]


def _name_with_tail(name: str, tail: str) -> str:
    if not tail:
        return ""
    cand = f"{name[: max(1, 31 - len(tail))]}-{tail}".strip("-")
    if NAME_RE.match(cand) and len(cand) >= 2 and cand not in {"harness", "h", "h-"}:
        return cand
    return ""


def disambiguate_names(rows: list[dict]) -> None:
    """Two sessions can share a title. One keeps it. The rest take a session-id tail.

    The harness title keeps the bare name ahead of a tab label or a stored
    roster name. Among equal sources, the earliest created row keeps it, then
    the smallest session id. A pin is never rewritten. When a pin already
    holds the bare name, every other row in the group takes a tail.
    """
    pins = {r.get("name") for r in rows if r.get("name_source") == "pinned" and r.get("name")}
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("name_source") == "pinned":
            continue
        groups.setdefault(r.get("name") or "", []).append(r)
    owners: dict[str, dict | None] = {}
    reserved = set(pins)
    for name, group in groups.items():
        if not name or (len(group) < 2 and name not in pins):
            if name:
                reserved.add(name)
            continue
        owner = None if name in pins else _bare_owner(group)
        owners[name] = owner
        if owner is not None:
            reserved.add(name)

    def take_tail(name: str, rest: list[dict]) -> None:
        for n in (4, 8, 12):
            cands, used = [], set(reserved)
            ok = True
            for r in rest:
                tail = re.sub(r"[^a-z0-9]", "", (r.get("session_id") or "").lower())[:n]
                cand = _name_with_tail(name, tail)
                if not cand or cand in used:
                    ok = False
                    break
                used.add(cand)
                cands.append(cand)
            if ok:
                for r, cand in zip(rest, cands):
                    r["name"] = cand
                    reserved.add(cand)
                return
        n = 2
        for r in rest:
            while True:
                cand = f"{name[:28]}-{n}"
                n += 1
                if cand not in reserved and NAME_RE.match(cand):
                    break
            r["name"] = cand
            reserved.add(cand)

    for name, owner in owners.items():
        rest = [r for r in groups[name] if r is not owner]
        take_tail(name, rest)


# --- catalog -------------------------------------------------------------------

# Two hooks onto tools cowbell does not own, overridable in [defaults].
# trust_command: asked `subject <path>`, `match <tool> <path>`, `badge <tool>`;
# absent from PATH means no trust verdicts at all. seat_file: a brief found by
# walking up from the directory, handed to a harness via its system_prompt_flag.
EXT = {"trust_command": "harness-trust", "seat_file": "SEAT.md"}
TRUST_TIMEOUT = 2.5  # seconds; a slow checker reads as no verdict, never a stalled list


def apply_ext(cat: dict) -> None:
    d = cat.get("defaults", {})
    for key in EXT:
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            EXT[key] = v.strip()


def prompt_flag(h: dict) -> str | None:
    """Flag that hands a harness a system-prompt file (`seat_flag` until 0.3)."""
    return h.get("system_prompt_flag") or h.get("seat_flag")


def catalog_shape_error(cat: dict) -> str | None:
    """The first structure no command can read, in one clause, or None. These refuse the load;
    `catalog --check` reports the row-level problems that come after."""
    for key in ("defaults", "lanes", "places"):
        if key in cat and not isinstance(cat[key], dict):
            return f"{key} must be a table"
    if "schema" in cat and (isinstance(cat["schema"], bool) or not isinstance(cat["schema"], int)):
        return "schema must be an int"
    for name, lane in (cat.get("lanes") or {}).items():
        if not isinstance(lane, dict):
            return f"lanes.{name} must be a table"
    for name, pl in (cat.get("places") or {}).items():
        if not isinstance(pl, dict):
            return f"places.{name} must be a table"
        if "allow_harness" in pl and not isinstance(pl["allow_harness"], list):
            return f"places.{name}.allow_harness must be a list"
        if isinstance(pl.get("allow_harness"), list) and not all(
                isinstance(x, str) for x in pl["allow_harness"]):
            return f"places.{name}.allow_harness must be a list of strings"
        if "default_harness" in pl and not isinstance(pl["default_harness"], str):
            return f"places.{name}.default_harness must be a string"
    if "harness" in cat and not isinstance(cat["harness"], list):
        return "harness must be a list of [[harness]] tables"
    for h in cat.get("harness") or []:
        if not isinstance(h, dict):
            return "each [[harness]] row must be a table"
        for key in ("id", "kind", "command", "trust_tool"):
            if key in h and not isinstance(h[key], str):
                return f"harness {key} must be a string"
    return None


def load_catalog() -> dict:
    if tomllib is None:
        raise SystemExit("cowbell: python >= 3.11 required (tomllib)")
    p = catalog_path()
    if not p.exists():
        raise SystemExit(f"cowbell: no catalog at {p}")
    try:
        with _open_regular(p) as fh:
            cat = tomllib.load(fh)
    except (OSError, ValueError) as e:  # ValueError covers TOMLDecodeError and bad UTF-8
        detail = f"{p}: {_file_error(e)}"
        raise SystemExit(f"cowbell: {detail}")
    if not cat:
        raise SystemExit(f"cowbell: {p}: empty catalog")
    shape = catalog_shape_error(cat)
    if shape:
        detail = f"{p}: {shape}"
        raise SystemExit(f"cowbell: {detail}")
    cat.setdefault("schema", 1)  # a catalog without the key predates it and is schema 1
    cat.setdefault("defaults", {})
    cat.setdefault("lanes", {})
    apply_ext(cat)
    for h in cat.get("harness", []):
        h.setdefault("kind", h.get("id") or "")
        h.setdefault("family", h["kind"])
        h.setdefault("egress", "vendor")
        h.setdefault("trust_tool", h.get("id") or h["kind"])
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
        out["harness"] = [h for h in cat.get("harness", []) if h.get("id") in allowed]
        # keep allow_harness order so a default guest door can sit first
        order = {hid: i for i, hid in enumerate(allow)}
        out["harness"].sort(key=lambda h: order.get(h.get("id"), len(order)))
    return out


def harness_by_id(cat: dict, hid: str) -> dict | None:
    for h in cat.get("harness", []):
        if h.get("id") == hid:
            return h
    return None


def is_wrapper(h: dict) -> bool:
    return bool(h.get("command"))


def installed(h: dict) -> bool:
    return os.path.exists(h["command"]) if is_wrapper(h) else bool(shutil.which(h["kind"]))


# A row may carry `requires` (argvs run before every launch, e.g. start the model server it talks
# to) and `check` (one argv: its acceptance). A row whose check has no pass in ACCEPT_MAX_AGE runs
# the check at launch, after `requires`, and opens only if it passes. `cowbell accept` runs it ahead.

ACCEPT_MAX_AGE = 7 * 24 * 3600


def accept_path() -> Path:
    return state_dir() / "accept.json"


def load_accepts() -> dict:
    try:
        with _open_regular(accept_path()) as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def accepted(h: dict, now: float | None = None, accepts: dict | None = None) -> bool:
    """True for a row with no check, or whose check last passed within ACCEPT_MAX_AGE.

    A catalog row is keyed by `id`. A startable row is keyed by `harness`.
    """
    if not h.get("check"):
        return True
    hid = h.get("id") or h.get("harness") or ""
    rec = (load_accepts() if accepts is None else accepts).get(hid) or {}
    return bool(rec.get("ok")) and (now or time.time()) - float(rec.get("ts") or 0) < ACCEPT_MAX_AGE


def unavailable_reason(h: dict) -> str:
    """'' when the row can start; otherwise why not, in words a person can act on."""
    if not installed(h):
        return f"{h['id']} is not installed here ({h.get('command') or h['kind']})"
    return ""


# Subprocess bounds: nothing cowbell runs for its own bookkeeping may hang the
# picker. Every call goes through run_bounded or passes an explicit timeout.
REQUIRES_TIMEOUT = 60   # a requires argv may start a model server; give it a minute
CHECK_TIMEOUT = 5       # one argv, run at launch when its last pass is stale
GIT_TIMEOUT = 5
PS_TIMEOUT = 5
LSOF_TIMEOUT = 5
KILL_GRACE = 2.0        # a killed child gets this long to be reaped, then the wait is abandoned


def run_bounded(argv: list[str], timeout: float, attached: bool = False) -> subprocess.CompletedProcess | None:
    """Run argv without ever waiting past `timeout` (+ KILL_GRACE to reap).

    Captured (default): the child leads its own session, reads EOF on stdin and
    has no controlling tty, so it cannot prompt; a timeout, or an interrupt such
    as Ctrl-C (which a session leader does not receive), SIGKILLs the whole
    process group, so a grandchild holding the captured pipe open cannot stretch
    the wait or outlive us. Worst case: timeout + KILL_GRACE. Attached: the child
    inherits this terminal — a sudo prompt on the tty still works — and a timeout
    kills the child alone, never the group; worst case: timeout + 2 x KILL_GRACE
    (terminate, then kill, each given KILL_GRACE to reap).
    Returns the CompletedProcess, or None when the bound was hit. A binary that
    will not start is rc 127 with the OS error on stderr, like a shell.
    """
    try:
        proc = subprocess.Popen(
            argv, start_new_session=not attached,
            **({} if attached else {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                    "stderr": subprocess.PIPE, "text": True}),
        )
    except (OSError, ValueError) as e:  # ValueError: a NUL byte in argv
        return subprocess.CompletedProcess(argv, 127, None if attached else "", str(e))
    try:
        if attached:
            proc.wait(timeout=timeout)
            return subprocess.CompletedProcess(argv, proc.returncode)
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        if attached:
            proc.terminate()  # the tty is shared: never killpg on this path
            try:
                proc.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the child led its own session: pgid == pid
            except OSError:
                pass
        try:
            proc.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass  # a child that will not reap (D-state) is abandoned, never waited on
        if not attached:
            for fh in (proc.stdout, proc.stderr):
                try:
                    fh.close()
                except OSError:
                    pass
        return None
    except BaseException:
        if not attached:  # Ctrl-C: start_new_session took the child out of our SIGINT group
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=KILL_GRACE)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise


def is_argv(v) -> bool:
    """One argv: a non-empty list of strings. A bare str is not one; it is never wrapped or split."""
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) for x in v)


def run_check(h: dict) -> tuple[bool, str]:
    """Run the row's check; record and return (passed, last line of its output)."""
    ck = h.get("check")
    argv = ck if is_argv(ck) else []
    if not argv:  # a bare str is refused, as `catalog --check` refuses it: never wrapped, never split
        ok, last = False, "check must be one argv, a list of strings"
    elif (proc := run_bounded(argv, CHECK_TIMEOUT)) is None:
        ok, last = False, f"check timed out after {CHECK_TIMEOUT:g}s"
    else:
        out = (proc.stdout or "").strip().splitlines() or (proc.stderr or "").strip().splitlines()  # verdict on stdout
        last = out[-1] if out else f"exit {proc.returncode}"
        ok = proc.returncode == 0

    accepts = load_accepts()
    accepts[h["id"]] = {"ok": ok, "ts": time.time(), "detail": last}
    tmp = accept_path().with_suffix(".tmp")
    try:
        tmp.unlink(missing_ok=True)  # a stale tmp keeps its old mode: start fresh so the
        # 0600 in os.open is the creation mode, with no umask-mode window between opens.
        # O_EXCL|O_NOFOLLOW: a tmp planted after the unlink is refused, never followed or reused.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(accepts, indent=2) + "\n")
        tmp.replace(accept_path())
    except OSError as e:
        raise SystemExit(f"cowbell: {tmp}: {_file_error(e)}")
    return ok, last


def run_requires(h: dict) -> None:
    """Run each `requires` argv in order, output to this terminal; the first failure refuses the launch."""
    reqs = h.get("requires")
    if reqs is None:
        return
    if not (isinstance(reqs, list) and all(is_argv(a) for a in reqs)):  # checked whole, before any entry runs
        raise SystemExit(f"cowbell: refused — {h['id']}: requires must be a list of argvs (lists of strings)")
    for argv in reqs:
        trace.event("requires", harness=h.get("id", ""), cmd=shell_join(redact_argv(argv)))
        proc = run_bounded(argv, REQUIRES_TIMEOUT, attached=True)
        if proc is None:
            raise SystemExit(
                f"cowbell: refused — {h['id']} requires {shell_join(argv)!r}, "
                f"which timed out after {REQUIRES_TIMEOUT:g}s")
        if proc.returncode:
            if proc.stderr:  # a binary that would not start carries the OS error here (rc 127)
                raise SystemExit(f"cowbell: refused — {h['id']} requires {shell_join(argv)!r}: {proc.stderr.strip()}")
            raise SystemExit(f"cowbell: refused — {h['id']} requires {shell_join(argv)!r}, which exited {proc.returncode}")


def ensure_accepted(h: dict) -> None:
    """A row with a check and no fresh pass runs it now; a failure refuses the launch."""
    if accepted(h):
        return
    print(f"cowbell: checking {h['id']} (no pass in the last 7 days)", file=sys.stderr)
    ok, last = run_check(h)
    trace.event("check", harness=h.get("id", ""), ok=ok, detail=redact_text(last))
    if not ok:
        raise SystemExit(f"cowbell: refused — {h['id']} failed its check: {last}")
    print(f"cowbell: {last}", file=sys.stderr)


# --- roster --------------------------------------------------------------------

def roster_schema(p: Path | None = None) -> int:
    p = p or roster_path()
    try:
        with _open_regular(p) as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return SCHEMA
    return data.get("schema", 1) if isinstance(data, dict) else 1


def load_roster() -> list[dict]:
    p = roster_path()
    if not p.exists():
        return []
    try:
        with _open_regular(p) as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError) as e:
        raise SystemExit(f"cowbell: {p}: {_file_error(e)}")
    return data.get("harnesses", []) if isinstance(data, dict) else data


def save_roster(rows: list[dict]) -> None:
    p = roster_path()
    check_writable(roster_schema(p), str(p))
    rows.sort(key=lambda r: (r.get("cwd", ""), r.get("name", "")))
    tmp = p.with_suffix(".json.tmp")
    # the roster holds raw argv, prompts included — born 0600 like accept.json and the debug log
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": SCHEMA, "harnesses": rows}, indent=2) + "\n")
    os.replace(tmp, p)


def pins_path() -> Path:
    return state_dir() / "pins.json"


def _norm_root(root: str) -> str:
    return os.path.realpath(os.path.expanduser(root)) if root else ""


def pins_schema(p: Path | None = None) -> int:
    p = p or pins_path()
    try:
        with _open_regular(p) as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return SCHEMA
    return data.get("schema", SCHEMA) if isinstance(data, dict) else SCHEMA


def load_pins() -> dict:
    """Per-directory pin/hide. Keyed by realpath, then harness id (`pin` or `hide`)."""
    p = pins_path()
    if not p.exists():
        return {"schema": SCHEMA, "dirs": {}}
    try:
        with _open_regular(p) as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return {"schema": SCHEMA, "dirs": {}}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "dirs": {}}
    data.setdefault("schema", SCHEMA)
    data.setdefault("dirs", {})
    if not isinstance(data["dirs"], dict):
        data["dirs"] = {}
    return data


def save_pins(data: dict) -> None:
    p = pins_path()
    check_writable(pins_schema(p), str(p))
    dirs = data.get("dirs") or {}
    write_atomic(p, json.dumps({"schema": SCHEMA, "dirs": dirs}, indent=2) + "\n")


def dir_prefs(root: str, data: dict | None = None) -> dict:
    """`{"pin": [ids…], "hide": [ids…]}` for this directory. Pin order is insertion order."""
    data = data if data is not None else load_pins()
    entry = (data.get("dirs") or {}).get(_norm_root(root)) or {}
    pin, hide = [], []
    if isinstance(entry, dict):
        for hid, kind in entry.items():
            if kind == "pin":
                pin.append(hid)
            elif kind == "hide":
                hide.append(hid)
    return {"pin": pin, "hide": hide}


def _set_pref(root: str, hid: str, kind: str | None) -> None:
    if not hid:
        raise SystemExit("cowbell: pin/hide needs a harness id")
    data = load_pins()
    key = _norm_root(root)
    dirs = dict(data.get("dirs") or {})
    entry = dict(dirs.get(key) or {})
    if kind is None:
        entry.pop(hid, None)
    else:
        entry[hid] = kind
    if entry:
        dirs[key] = entry
    else:
        dirs.pop(key, None)
    data["dirs"] = dirs
    save_pins(data)


def pin_harness(root: str, hid: str) -> None:
    _set_pref(root, hid, "pin")


def hide_harness(root: str, hid: str) -> None:
    _set_pref(root, hid, "hide")


def clear_pref(root: str, hid: str) -> None:
    _set_pref(root, hid, None)


def rename_harness_refs(old: str, new: str) -> None:
    """Point pins and roster rows at a renamed harness id. No-op when unchanged."""
    if not old or not new or old == new:
        return
    data = load_pins()
    dirs = data.get("dirs") or {}
    changed = False
    for entry in dirs.values():
        if not isinstance(entry, dict) or old not in entry:
            continue
        kind = entry.pop(old)
        entry.setdefault(new, kind)
        changed = True
    if changed:
        save_pins(data)
    rows = load_roster()
    hit = False
    for row in rows:
        if row.get("harness") == old:
            row["harness"] = new
            hit = True
    if hit:
        save_roster(rows)


def drop_harness_prefs(hid: str) -> None:
    """Strip pin/hide for a deleted harness in every directory. No-op when absent."""
    if not hid:
        return
    data = load_pins()
    dirs = data.get("dirs") or {}
    new_dirs = {}
    changed = False
    for cwd, entry in dirs.items():
        if not isinstance(entry, dict) or hid not in entry:
            new_dirs[cwd] = entry
            continue
        kept = {k: v for k, v in entry.items() if k != hid}
        changed = True
        if kept:
            new_dirs[cwd] = kept
    if not changed:
        return
    data["dirs"] = new_dirs
    save_pins(data)


def roster_here(rows: list[dict], root: str) -> list[dict]:
    return [r for r in rows if r.get("cwd") and at_or_beneath(r["cwd"], root)]


def now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# --- live (pure: reads a herdr snapshot already fetched) -------------------------

def tab_labels(snap: dict) -> dict[str, str]:
    return {t["tab_id"]: (t.get("label") or "") for t in snap.get("tabs") or [] if t.get("tab_id")}


# A pane border may cache `sitting · harness`. The tab label is the sitting.
# Herdr's detected agent is the harness. A bare label that is not the sitting
# is a stale sitting, not a claim to preserve.
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
    """The border the pane should wear, or None when it should stay.

    A detected agent stamps `sitting · detected`. An idle shell with no agent
    wears the sitting alone. A bare label that is not the current sitting is
    stale and is rewritten. A person's one-off label that already equals the
    sitting stays until detection upgrades it to `sitting · kind`. An empty
    border is left alone (detection restamps it via mark_harness). A wrapper
    Herdr cannot see keeps the claim it has.
    """
    label = label or ""
    bare = label.strip()
    claim = harness_claim(label)
    if claim:
        name, _tool = claim
        sitting = (sitting or name).strip() or name
    else:
        sitting = (sitting or "").strip()
    detected = (detected or "").strip()
    if detected:
        want = coherent_pane_label(sitting, detected)
        if not want:
            return None
        if claim or bare:
            return None if want == bare else want
        return None
    if idle_shell:
        if not sitting:
            return None
        if claim or (bare and bare != sitting):
            return None if sitting == bare else sitting
        return None
    if claim:
        return None
    if bare and sitting and bare != sitting:
        return sitting
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
        with _open_regular(state_dir() / "history-cache.json") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def write_history_cache(cache: dict) -> None:
    try:
        p = state_dir() / "history-cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        with _open_regular_write(p) as fh:
            fh.write(json.dumps(cache)[:4_000_000].encode("utf-8"))
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


# Terminal controls in text read from a harness's store: OSC (title, hyperlink) to BEL or ST,
# CSI, then any other ESC pair. A title is printed into the picker, so none may reach it.
TERM_CTRL_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-?]*[ -/]*[@-~]|\x1b.?", re.S)


def session_title(v, limit: int = 60) -> str:
    """A title from a user message, with the harness's injected blocks and terminal controls dropped."""
    raw = message_text(v)
    m = re.search(r"<user_query>\s*(.+?)\s*</user_query>", raw, re.S)
    if m:
        raw = m.group(1)
    for tag in ("system-reminder", "user_info", "command-name", "command-message", "local-command-stdout"):
        raw = re.sub(rf"<{tag}>.*?</{tag}>", " ", raw, flags=re.S)
    raw = re.sub(r"<[^>]{1,40}>", " ", raw)
    raw = TERM_CTRL_RE.sub("", raw)
    raw = "".join(c if c.isprintable() else " " for c in raw)
    raw = " ".join(raw.split())
    return raw[:limit].strip()


def claude_store(root: str, home: Path | None = None) -> Path:
    """Claude Code keys a project dir by the cwd with / and . flattened to -.

    An explicit `home` (a launch-uid scan) wins; else $CLAUDE_CONFIG_DIR; else ~/.claude."""
    if home is not None:
        base = Path(home) / ".claude" / "projects"
    elif os.environ.get("CLAUDE_CONFIG_DIR"):
        base = Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "projects"
    else:
        base = Path.home() / ".claude" / "projects"
    return base / re.sub(r"[/.]", "-", os.path.realpath(root))


def _claude_hit(f: Path) -> dict:
    """customTitle and the first real user line from one Claude session file."""
    hit = {"session_id": f.stem, "harness_title": "", "utterance": "", "started": "", "last": ""}
    try:
        with _open_regular(f) as fh:
            for i, raw in enumerate(fh):
                if i > 400:
                    break
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                ts = d.get("timestamp") or ""
                if ts:
                    hit["started"] = hit["started"] or ts
                    hit["last"] = max(hit["last"], ts)
                if d.get("type") == "custom-title" and d.get("customTitle"):
                    hit["harness_title"] = str(d["customTitle"])[:60]
                elif not hit["utterance"] and d.get("type") == "user":
                    t = session_title(d.get("message"))
                    if t and not INJECTED.match(t):
                        hit["utterance"] = t
    except OSError:
        pass
    return hit


def _history_row(hit: dict, harness: str) -> dict:
    """Cache stores the raw title fields. The menu name is chosen at read time."""
    title = hit.get("harness_title") or hit.get("utterance") or ""
    name, source = canonical_name(
        harness_title=hit.get("harness_title") or "",
        utterance=hit.get("utterance") or "",
        kind=harness,
        session_id=hit.get("session_id") or "",
    )
    return {
        "session_id": hit.get("session_id") or "",
        "title": title,
        "harness_title": hit.get("harness_title") or "",
        "utterance": hit.get("utterance") or "",
        "started": hit.get("started") or "",
        "last": hit.get("last") or "",
        "name": name,
        "name_source": source,
        "harness": harness,
        "kind": harness,
    }


def claude_sessions(root: str, cache: dict, home: Path | None = None) -> list[dict]:
    t0, out, hits, misses = time.perf_counter(), [], 0, 0
    store = claude_store(root, home)
    if not store.is_dir():
        return out
    for f in sorted(store.glob("*.jsonl")):
        key = cache_key(f)
        hit = cache.get(key)
        if not isinstance(hit, dict) or "utterance" not in hit:
            misses += 1
            hit = _claude_hit(f)
            cache[key] = hit
        else:
            hits += 1
        out.append(_history_row(hit, "claude"))
    trace.event("history", store="claude", ms=_ms(t0), files=len(out), hit=hits, miss=misses)
    return out


def grok_store(root: str, home: Path | None = None) -> Path:
    """Grok keys a session dir by the percent-encoded cwd."""
    base = (home or Path.home()) / ".grok" / "sessions"
    return base / urllib.parse.quote(os.path.realpath(root), safe="")


def _read_json(path: Path) -> dict:
    try:
        with _open_regular(path) as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _grok_hit(d: Path) -> dict:
    """generated_title from summary.json, else the first real user line.

    Both files sit in the session directory Grok already writes. The user
    line is read only when the title is missing or is chrome.
    """
    hit = {"session_id": d.name, "harness_title": "", "utterance": "", "started": "", "last": ""}
    title = _read_json(d / "summary.json").get("generated_title")
    if isinstance(title, str):
        hit["harness_title"] = title[:60]
    chat = d / "chat_history.jsonl"
    try:
        st = chat.stat()
    except OSError:
        return hit
    hit["last"] = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat()
    hit["started"] = _dt.datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime),
                                                 _dt.timezone.utc).isoformat()
    kept = hit["harness_title"] and not title_is_chrome(hit["harness_title"]) and usable_slug(hit["harness_title"])
    if kept:
        return hit
    try:
        with _open_regular(chat) as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                try:
                    r = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if r.get("type") == "user":
                    t = session_title(r.get("content"))
                    if t and INJECTED.match(t):
                        continue
                    if t:
                        hit["utterance"] = t
                        break
    except OSError:
        pass
    return hit


def grok_sessions(root: str, cache: dict, home: Path | None = None) -> list[dict]:
    t0, out, hits, misses = time.perf_counter(), [], 0, 0
    store = grok_store(root, home)
    if not store.is_dir():
        return out
    for d in sorted(store.iterdir()):
        chat = d / "chat_history.jsonl"
        if not chat.is_file():
            continue
        summary = d / "summary.json"
        key = cache_key(chat) + "|" + cache_key(summary)
        hit = cache.get(key)
        if not isinstance(hit, dict) or "utterance" not in hit:
            misses += 1
            hit = _grok_hit(d)
            cache[key] = hit
        else:
            hits += 1
        out.append(_history_row(hit, "grok"))
    trace.event("history", store="grok", ms=_ms(t0), files=len(out), hit=hits, miss=misses)
    return out


def session_fields(kind: str, root: str, session_id: str, home: Path | None = None) -> dict:
    """Title and first user line for one session. No full-store scan.

    A live pane already carries the session id. History can be off. Only
    Claude and Grok have a store format this tree reads; other kinds wait
    until a test fixture shows the file.
    """
    if not kind or not root or not session_id:
        return {}
    if kind == "claude":
        f = claude_store(root, home) / f"{session_id}.jsonl"
        if not f.is_file():
            return {}
        return _claude_hit(f)
    if kind == "grok":
        d = grok_store(root, home) / session_id
        if not (d / "chat_history.jsonl").is_file() and not (d / "summary.json").is_file():
            return {}
        return _grok_hit(d)
    return {}


HISTORY_PROVIDERS = {"claude": claude_sessions, "grok": grok_sessions}


def history_warning(text: str) -> str:
    """A discovery failure as one display line: redacted like the trace, terminal controls dropped
    (the detail may be another account's stderr)."""
    line = (str(text or "").strip().splitlines() or ["session discovery failed"])[-1]
    return CONTROL_WIDE.sub("", redact_text(line))[:240]


def launch_history(cat: dict, root: str, warnings: list[str] | None = None) -> list[dict]:
    """Discover history as the uid that will launch here, crossing its home boundary when needed.

    With `warnings` (a list), a failed discovery costs only its Resume rows: the failure is added
    there once and no history is returned. Without it the failure exits, as an explicit lookup
    (`cowbell enter`) should."""
    if warnings is None:
        return _launch_history(cat, root)
    try:
        return _launch_history(cat, root)
    except SystemExit as e:
        msg = history_warning(str(e))
        trace.event("history", store="as-user", warn=msg[:120])
        if msg not in warnings:
            warnings.append(msg)
        return []


def _launch_history(cat: dict, root: str) -> list[dict]:
    import pwd
    if not history_on(cat):
        return discover_sessions(root, cat)
    pl = place_for(cat, root)
    uid = ((pl or {}).get("unix_user") or (cat.get("defaults") or {}).get("unix_user") or "").strip()
    if not uid:
        return discover_sessions(root, cat)
    try:
        pw = pwd.getpwnam(uid)
    except KeyError:
        raise SystemExit(f"cowbell: unix_user {uid!r} is not on this host")
    home = Path(pw.pw_dir)
    if os.geteuid() == pw.pw_uid:
        return discover_sessions(root, cat, home=home)
    if not _home_ok(pw):
        raise SystemExit(f"cowbell: refused — the home of {uid!r} is not a directory it owns")
    helper = shutil.which("agent-run")
    if not helper:
        raise SystemExit(
            f"cowbell: cannot discover sessions as unix user {uid!r} "
            "(agent-run is not on PATH; cowbell does not fall back to sudo -u)"
        )
    prefix = [helper, uid, "--"]
    history_cat = {
        "defaults": {"session_history": history_mode(cat)},
        "harness": [{"id": h["id"]} for h in cat.get("harness", []) if h.get("id")],
    }
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            prefix + [sys.executable, "-I", str(Path(__file__).resolve()), "_history"],
            input=json.dumps({"root": os.path.realpath(root), "home": str(home), "cat": history_cat}),
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # a stalled as-user lookup must not crash a picker frame: no history for this place
        trace.event("history", store="as-user", user=uid, ms=_ms(t0), err=type(e).__name__)
        return []
    trace.event("history", store="as-user", user=uid, ms=_ms(t0), rc=proc.returncode)
    if proc.returncode:
        detail = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        raise SystemExit(f"cowbell: session discovery as {uid!r} failed: {detail}")
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"cowbell: session discovery as {uid!r} returned invalid metadata")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SystemExit(f"cowbell: session discovery as {uid!r} returned invalid metadata")
    want = os.path.realpath(root)
    for row in rows:  # rows come from another account's stores: their text is untrusted
        sid, cwd = row.get("session_id"), row.get("cwd")
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            raise SystemExit(f"cowbell: session discovery as {uid!r} returned a malformed session id")
        if not (isinstance(cwd, str) and cwd == want and _plain_abs(cwd) and _uid_owns(cat, cwd, pw)):
            raise SystemExit(f"cowbell: session discovery as {uid!r} returned a folder it does not own")
        for key in ("title", "harness_title", "utterance", "name"):
            if isinstance(row.get(key), str):
                row[key] = CONTROL_WIDE.sub("", row[key])
    return rows


def discover_sessions(root: str, cat: dict, home: Path | None = None) -> list[dict]:
    """Sessions this directory has seen, from the harnesses' own stores. [] when off."""
    if not history_on(cat):
        return []
    ids = {h.get("id") for h in cat.get("harness", [])}
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
        if not r.get("name"):
            name, source = canonical_name(
                harness_title=r.get("harness_title") or "",
                utterance=r.get("utterance") or "",
                kind=r.get("kind") or "",
                session_id=r.get("session_id") or "",
            )
            r["name"] = name
            r["name_source"] = source
        r["cwd"], r["source"] = os.path.realpath(root), "history"
        r["last_seen"] = (r.get("last") or "")[:10]
    disambiguate_names(rows)
    rows.sort(key=lambda r: r.get("last") or "", reverse=True)
    return rows


# --- trust (delegated to EXT["trust_command"]; never re-encoded here) ----------------

def trust_available() -> bool:
    return shutil.which(EXT["trust_command"]) is not None


def trust_subject(root: str) -> str:
    if not trust_available():
        return "?"
    proc = run_bounded([EXT["trust_command"], "subject", root], TRUST_TIMEOUT)
    return (proc.stdout.strip() or "?") if proc else "?"


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
    t0 = time.perf_counter()
    if not trust_available():
        trace.event("trust", tool=tool, ms=0, verdict="unavailable")
        return {"verdict": "unknown", "flag": "?", "exit": -1}
    try:
        proc = subprocess.run([EXT["trust_command"], "match", tool, root], capture_output=True, text=True,
                              timeout=TRUST_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        trace.event("trust", tool=tool, ms=_ms(t0), verdict="unknown", err=type(e).__name__)
        return {"verdict": "unknown", "flag": "?", "exit": -1}
    if proc.returncode not in (0, 3, 4):
        trace.event("trust", tool=tool, ms=_ms(t0), verdict="unknown", rc=proc.returncode)
        return {"verdict": "unknown", "flag": "?", "exit": proc.returncode}
    verdict = parse_match(proc.stdout, proc.returncode)
    trace.event("trust", tool=tool, ms=_ms(t0), verdict=redact_text(verdict.get("verdict") or "unknown"))
    return verdict


def trust_badge(tool: str) -> str:
    if not trust_available():
        return "?"
    try:
        proc = subprocess.run([EXT["trust_command"], "badge", tool], capture_output=True, text=True,
                              timeout=TRUST_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return "?"
    return proc.stdout.strip() or "?"


def verdict_mark(v: dict) -> str:
    return {"ok": "ok", "overprivilege": "!", "blocked": "⊘", "unknown": "?"}.get(v.get("verdict") or "unknown", "?")


# --- registry ------------------------------------------------------------------

def build_registry(cat: dict, root: str, agents: list[dict], panes: list[dict], roster: list[dict],
                   matcher=None, subject: str = "", tab_label: dict | None = None,
                   history: list[dict] | None = None, home: Path | None = None) -> dict:
    """Partition the herd for one root: live here, cold here, absent here.

    Each row's name is `canonical_name`. A herdr tab that differs is `tab_label`
    on the row, for the faint location, not a second name. `home` is the
    harness store root (tests); a stock install uses the process user's home.
    """
    matcher = matcher or (lambda tool: trust_match(tool, root))
    labels = tab_label or {}
    live = [a for a in agents if a.get("cwd") and at_or_beneath(a["cwd"], root)]
    live_by_pane = {a["pane_id"]: a for a in live}
    pane_ids = {p.get("pane_id") for p in panes}
    rows_here = roster_here(roster, root)
    by_pane: dict[str, dict] = {str(r["pane_id"]): r for r in rows_here if r.get("pane_id")}
    # identify an exe by catalog: a kind only one harness in this catalog has is that harness
    # id; a kind several share (pi wrappers) stays the exe name.
    kind_id: dict[str, str] = {}
    for h in cat.get("harness", []):
        prev = kind_id.setdefault(h["kind"], h.get("id"))
        if prev != h.get("id"):
            kind_id[h["kind"]] = ""

    t_names, n_names = time.perf_counter(), 0
    live_rows, seen_pane = [], set()
    for a in live:
        r = by_pane.get(a["pane_id"])
        lab = labels.get(a.get("tab_id"), "")
        kind = a.get("agent") or ""
        sid = (a.get("agent_session") or {}).get("value") or (r or {}).get("session_id", "") or ""
        fields = session_fields(kind, a.get("cwd") or root, sid, home)
        n_names += 1
        pinned = ((r or {}).get("name") or "") if (r or {}).get("name_source") == "pinned" else ""
        stored = "" if pinned else ((r or {}).get("name") or "")
        name, source = canonical_name(
            pinned=pinned,
            harness_title=fields.get("harness_title") or "",
            tab=lab,
            stored=stored,
            utterance=fields.get("utterance") or "",
            terminal=a.get("terminal_title_stripped") or "",
            kind=kind,
            session_id=sid,
        )
        live_rows.append({
            "name": name,
            "name_source": source,
            "tab_label": lab,
            "harness": (r or {}).get("harness") or kind_id.get(kind, "") or kind,
            "kind": kind, "model": (r or {}).get("model", ""), "lane": (r or {}).get("lane", ""),
            "status": a.get("agent_status") or "live", "pane_id": a["pane_id"], "tab_id": a.get("tab_id"),
            "workspace_id": a.get("workspace_id"), "cwd": a.get("cwd"),
            "place": (place_for(cat, a.get("cwd") or "") or {}).get("name") or "",
            "session_id": sid,
            "on_roster": r is not None,
            # turn clock, only where a sidebar reporter publishes one ($since while working, $total when done)
            "since": ((a.get("tokens") or {}).get("since") or (a.get("tokens") or {}).get("total") or "").strip(),
            "pid": a.get("pid"), "tty": a.get("tty", ""),
        })
        seen_pane.add(a["pane_id"])

    allowed = {h.get("id") for h in cat.get("harness", [])}
    live_sids = {x.get("session_id") for x in live_rows if x.get("session_id")}
    cold_rows = []
    for r in rows_here:
        if r.get("pane_id") in seen_pane:
            continue
        sid = r.get("session_id") or ""
        if sid and sid in live_sids:
            continue  # this session is already in Active; Resume must not list it again
        hid = r.get("harness") or r.get("kind") or ""
        if allowed and hid not in allowed:
            continue  # place catalog: do not offer resume of a harness the door does not admit
        pane_alive = r.get("pane_id") in pane_ids and r.get("pane_id") not in live_by_pane
        named = dict(r)
        if named.get("name_source") != "pinned":
            fields = session_fields(named.get("kind") or "", named.get("cwd") or root, sid, home)
            n_names += 1
            name, source = canonical_name(
                harness_title=fields.get("harness_title") or "",
                stored=named.get("name") or "",
                utterance=fields.get("utterance") or "",
                kind=named.get("kind") or "",
                session_id=sid,
            )
            named["name"] = name
            named["name_source"] = source
        cold_rows.append({**named, "status": "unknown" if pane_alive else "cold", "pane_alive": pane_alive})
    trace.event("names", ms=_ms(t_names), reads=n_names)

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
        row = {"harness": h.get("id"), "kind": h["kind"], "family": h["family"], "egress": h["egress"],
               "title": h.get("title", ""), "installed": installed(h),
               "trust": verdicts.get(h["trust_tool"], {"verdict": "unknown"})}
        if h.get("check"):
            row["check"] = h["check"]
        startable.append(row)
        if h.get("id") not in present:
            absent.append(row)
    disambiguate_names(live_rows + cold_rows)
    return {"root": root, "subject": subject, "live": live_rows, "cold": cold_rows, "absent": absent,
            "startable": startable, "default_harness": cat.get("defaults", {}).get("default_harness", "")}


def row_host(r: dict) -> str:
    """Where the sitting is attached: a herdr pane, this terminal, or nowhere (cold)."""
    pane = str(r.get("pane_id") or "")
    if pane.startswith("pid:") or r.get("tty"):
        return "tty"
    if pane.startswith("hc:"):
        return "herdr app"
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
            "uid": a.get("uid") or reg.get("resolved_uid") or "",
        }

    return {
        "root": root,
        "place": place_name,
        # what could not be read (a failed session discovery); the rest of the answer stands
        "warnings": list(reg.get("warnings") or []),
        "running": [sitting_row(r) for r in reg.get("live") or []],
        # Resume only what can resume: a row without a session id would just start fresh.
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
            name = f"{r['name']:<24}"
            g = glyph_for(r.get("kind") or r.get("harness") or "")
            rest = f"{r['pane_id']:<8} {(r.get('session_id') or '')[:8]:<8} {tilde(r['cwd'])}{tag}"
            out.append(f"  {key(idx)}  {paint(name, INK, bold=True)} {g} "
                       f"{status_cell(r['status'], 9)} {paint(rest, FAINT)}")
    if reg["cold"]:
        # Resume only what can resume: a row without a session id would just start fresh.
        cold = [r for r in reg["cold"] if r.get("session_id")]
        if cold:
            out.append("")
            out.append(section("COLD HERE", "resumable"))
        for r in cold:
            idx += 1
            r["idx"] = idx
            name = f"{r['name']:<24}"
            g = glyph_for(r.get("kind") or r.get("harness") or "")
            rest = f"{r.get('pane_id') or DASH:<8} {(r.get('session_id') or '')[:8]:<8} {tilde(r['cwd'])}"
            out.append(f"  {key(idx)}  {paint(name, SECONDARY)} {g} "
                       f"{status_cell(r['status'], 9)} {paint(rest, FAINT)}")
    if not reg["live"] and not reg["cold"]:
        out.append("")
        out.append(paint("  nothing raised here yet", FAINT))
    if reg["absent"]:
        out.append("")
        out.append(section("ABSENT HERE", LEGEND))
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


FOLD = 5  # Active and Resume: longer than this shows FOLD-1 rows and a "+ N more" row
START_FOLD = 10  # New Session rows before "+ N more" when the window height is unknown
# Census first (not a menu), then the two menus chroma marks as openable.
PICKER_SECTIONS = (("running", "Active", SECONDARY), ("resume", "Resume", GOLD), ("start", "New Session", GREEN))


def picker_selectable(c: dict) -> bool:
    """Rows the cursor, digits, and Enter can land on. Active sittings are a census."""
    return c.get("section") != "running" or c.get("action") == "more"


def rank_harnesses(ids: list[str], roster: list[dict], root: str, default: str = "",
                   prefs: dict | None = None) -> list[str]:
    """Most used here first (a start at or beneath root counts 3), then anywhere, then catalog order.
    A place default_harness stays first so usage of a house grok in the pad does not bury the guest door.
    Pin/hide overlay: pinned ids lead, hidden ids are omitted. Widen passes the widened root."""
    prefs = prefs if prefs is not None else dir_prefs(root)
    hidden = set(prefs.get("hide") or [])
    ids = [h for h in ids if h not in hidden]
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
    pinned = [h for h in (prefs.get("pin") or []) if h in out]
    rest = [h for h in out if h not in set(pinned)]
    return pinned + rest


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


def _folded_count(n: int) -> int:
    """Rows Active or Resume actually occupy, including a '+ N more' line."""
    if n <= 0:
        return 0
    return n if n <= FOLD else FOLD


def start_row_limit(lines: int | None = None, running: int = 0, resume: int = 0) -> int:
    """New Session rows to show before '+ N more'.

    Unknown height shows 10. A known height shows as many as fit under the
    path, the hint bar, and the Active and Resume blocks already on screen.
    """
    if not lines:
        return START_FOLD
    used = 6  # path, hint bar, New Session heading, blanks
    if running:
        used += 3 + _folded_count(running)  # blank and the census box rules
    if resume:
        used += 2 + _folded_count(resume)
    return max(1, lines - used)


def _with_more(rows: list[dict], section: str, expanded: set[str], show: int) -> list[dict]:
    """Show `show` data rows, then '+ N more' when the section is longer."""
    if section in expanded or len(rows) <= show:
        return rows
    hidden = len(rows) - show
    return rows[:show] + [{"action": "more", "section": section, "key": f"more:{section}",
                           "hidden": hidden,
                           "line": paint(f"+ {hidden} more", FAINT)}]


def _live_where(r: dict, here_ws: str | None, ws_labels: dict[str, str]) -> str:
    """Faint location: a herdr tab that is not the session name, else another workspace, else the tty."""
    bits = []
    tab = (r.get("tab_label") or "").strip()
    shown = tab_name(tab)
    if shown and shown != r.get("name"):
        bits.append(tab)
    if r.get("workspace_id") and r.get("workspace_id") != here_ws:
        ws = ws_labels.get(r.get("workspace_id") or "", "")
        if ws:
            bits.append(f"in {ws}")
    if not bits and r.get("tty"):
        bits.append(f"on {r['tty']}")
    return "  ".join(bits)


def _cut_to(text: str, room: int) -> str:
    """Fit `text` in `room` cells. A cut ends with `…`."""
    if len(text) <= room:
        return text
    if room <= 1:
        return "…" if room == 1 else ""
    head = text[:room - 1].rstrip()
    return (head + "…") if head else "…"


def _start_prefix(id_cell: str, glyph: str) -> int:
    """Cells on a drawn New Session row before the title.

    render_picker draws `  ` + a 3-wide key + `  ` + id + ` ` + glyph + `  `.
    """
    return 2 + 3 + 2 + len(id_cell) + 1 + len(glyph) + 2


def _paint_reach(plain: str) -> str:
    """Trust warn. The three leading spaces stay outside the paint when they survived the cut."""
    if not plain:
        return ""
    if plain.startswith("   ") and plain[3:]:
        return f"   {paint(plain[3:], FAINT)}"
    return paint(plain, FAINT)


def _fit_unproven_row(title: str, pad_to: int, warn: str, mark: str, prefix: int,
                      width: int | None) -> tuple[str, str]:
    """(title, warn) so a drawn row keeps `unproven` inside `width`.

    Width None, or under 43, leaves the row alone. Alignment padding goes first,
    then the title, down to `…`. The warn shrinks only after that, and only if
    the word still sticks out.
    """
    shown = f"{title:<{pad_to}}" if pad_to else title
    if width is None or width < 43 or "unproven" not in mark:
        return shown, warn

    def length(t: str, w: str) -> int:
        return prefix + len(t) + len(w) + len(mark)

    if length(shown, warn) <= width:
        return shown, warn
    if length(title, warn) <= width:
        return title, warn
    spare = width - prefix - len(mark) - len(warn)
    fitted = _cut_to(title, max(1, spare)) if title else ""
    if length(fitted, warn) <= width:
        return fitted, warn
    warn_room = width - prefix - len(mark) - len(fitted)
    if warn_room < 0 and fitted == "…":
        fitted = ""
        warn_room = width - prefix - len(mark)
    return fitted, _cut_to(warn, max(0, warn_room))


def picker_choices(reg: dict, roster: list[dict] | None = None, here_ws: str | None = None,
                   ws_labels: dict[str, str] | None = None, expanded: set[str] | None = None,
                   lines: int | None = None, width: int | None = None) -> list[dict]:
    """Popup rows: active census, resume, start new. Only Resume and New Session are a menu.

    `width` is the picker width. None does not shorten. A New Session row whose
    `unproven` would fall past that width loses title first (then the trust warn).
    Width under 43 is left as drawn.
    """
    expanded = expanded or set()
    ws_labels = ws_labels or {}
    live: list[dict] = []
    for r in sorted(reg["live"], key=lambda r: bool(here_ws) and r.get("workspace_id") != here_ws):
        name = f"{r['name']:<22}"
        g = glyph_for(r.get("kind") or r.get("harness") or "")
        where = _live_where(r, here_ws, ws_labels)
        clock_w = max((len(x.get("since") or "") for x in reg["live"]), default=0)
        clock = f"{r.get('since', ''):<{clock_w + 2}}" if clock_w else ""
        extra = f"{clock}{where}".rstrip()
        live.append({"action": "focus", "target": r, "key": f"focus:{r['pane_id']}",
                     "line": f"{paint(name, INK, bold=True)} {g}  {status_cell(r['status'])}"
                             + (f" {paint(extra, FAINT)}" if extra else "")})
    # Resume only what can resume: a row without a session id would just start fresh.
    cold = sorted((r for r in reg["cold"] if r.get("session_id")),
                  key=lambda r: r.get("last_seen") or r.get("created") or "", reverse=True)
    resume = []
    for r in cold:
        name = f"{r['name']:<22}"
        g = glyph_for(r.get("kind") or r.get("harness") or "")
        resume.append({"action": "enter", "target": r, "key": f"enter:{r['name']}",
                       "line": f"{paint(name, INK, bold=True)} {g}  "
                               f"{paint(ago(r.get('last_seen') or r.get('created') or ''), FAINT)}"})
    pool = {a["harness"]: a for a in reg.get("startable", reg["absent"])
            if a.get("installed") and a.get("trust", {}).get("verdict") != "blocked"}
    start = []
    ranked = rank_harnesses(list(pool), roster or [], reg["root"], default=reg.get("default_harness") or "")
    start_limit = start_row_limit(lines, running=len(live), resume=len(cold))
    shown = ranked if "start" in expanded or len(ranked) <= start_limit else ranked[:start_limit]
    title_w = min(34, max((len(pool[h].get("title") or "") for h in shown), default=0))
    accepts = load_accepts() if any(a.get("check") for a in pool.values()) else {}
    for hid in ranked:
        a = pool[hid]
        over = a.get("trust", {}).get("verdict") == "overprivilege"
        warn_plain = "   ! more reach than needed" if over else ""
        mark_plain = "" if accepted(a, accepts=accepts) else "  unproven"
        g = glyph_for(a.get("kind") or hid)
        id_cell = f"{hid:<22}"
        title, warn_plain = _fit_unproven_row(
            a.get("title") or "", title_w if over else 0, warn_plain, mark_plain,
            _start_prefix(id_cell, g), width)
        warn = _paint_reach(warn_plain)
        mark = f"  {paint('unproven', FAINT)}" if mark_plain else ""
        start.append({"action": "raise", "target": a, "key": f"raise:{hid}",
                      "line": f"{paint(id_cell, INK, bold=True)} {g}  {paint(title, MUTED)}{warn}{mark}"})
    out = []
    for section, rows in (("running", live), ("resume", resume), ("start", start)):
        folded = (_with_more(rows, section, expanded, start_limit) if section == "start"
                  else fold(rows, section, expanded))
        out += [dict(c, section=section) for c in folded]
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
    out = [f"  {paint(path, TEAL, bold=True)}" + (f"{' ' * max(3, gap)}{paint(right, FAINT)}" if right else "")]
    out += warning_lines(reg, width)
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


def _wrapper_catalog_argv(cat: dict, h: dict, model: str, lane: str, allow: str, deny: str) -> list[str]:
    """Args a wrapper opted into. A launch-time model or lane the row did not set stays out.

    The script still bakes its own flags. These are appended, and the agent takes the last one.
    """
    argv: list[str] = []
    catalog_models = list(h.get("models") or [])
    if model and h.get("model_flag") and (not catalog_models or model in catalog_models):
        argv += list(h["model_flag"]) + [model]
    if lane and lane == (h.get("lane") or ""):
        lane_map = cat.get("lanes", {}).get(h.get("kind") or "", {})
        if lane in lane_map:
            argv += list(lane_map[lane])
    if allow and h.get("tools_allow_flag") and allow == (h.get("tools_allow") or ""):
        argv += [h["tools_allow_flag"], allow]
    if deny and h.get("tools_deny_flag") and deny == (h.get("tools_deny") or ""):
        argv += [h["tools_deny_flag"], deny]
    return argv


def effort_argv(h: dict, effort: str) -> list[str]:
    """`effort_flag` with the level. A `{}` in the flag is where the level goes
    (codex: `["-c", "model_reasoning_effort={}"]`); otherwise the level follows it."""
    flag = h.get("effort_flag")
    if not effort or not isinstance(flag, list) or not flag:
        return []
    if any("{}" in part for part in flag):
        return [part.replace("{}", effort) for part in flag]
    return list(flag) + [effort]


def dirs_argv(h: dict, dirs: list[str] | None) -> list[str]:
    """`add_dir_flag` once per extra directory the agent may also work in."""
    flag = h.get("add_dir_flag")
    if not dirs or not isinstance(flag, str) or not flag:
        return []
    out: list[str] = []
    for d in dirs:
        if d:
            out += [flag, d]
    return out


def build_argv(cat: dict, h: dict, model: str = "", lane: str = "", allow: str = "", deny: str = "",
               seat: Path | None = None, resume_id: str = "", sitting: str = "",
               extra: list[str] | None = None, agents_dir: Path | None = None,
               effort: str = "", dirs: list[str] | None = None) -> list[str]:
    """Agent-side argv (after `--` for native kinds; after the wrapper for house wrappers)."""
    argv: list[str] = []
    if is_wrapper(h):
        argv += _wrapper_catalog_argv(cat, h, model, lane, allow, deny)
        argv += effort_argv(h, effort)
        argv += dirs_argv(h, dirs)
        if resume_id:
            argv += list(h.get("resume") or ["--continue"])
        return argv + list(extra or [])
    provider = h.get("provider")
    if isinstance(provider, str) and provider and isinstance(h.get("provider_flag"), list) and h["provider_flag"]:
        argv += list(h["provider_flag"]) + [provider]
    if model and h.get("model_flag"):
        argv += list(h["model_flag"]) + [model]
    argv += effort_argv(h, effort)
    models = h.get("models") or []
    if isinstance(h.get("cycle_flag"), list) and h["cycle_flag"] and models:
        argv += list(h["cycle_flag"]) + [",".join(models)]
    lane_map = cat.get("lanes", {}).get(h["kind"], {})
    if lane and lane in lane_map:
        argv += list(lane_map[lane])
    if allow and h.get("tools_allow_flag"):
        argv += [h["tools_allow_flag"], allow]
    if deny and h.get("tools_deny_flag"):
        argv += [h["tools_deny_flag"], deny]
    argv += dirs_argv(h, dirs)
    if seat and prompt_flag(h):
        argv += [prompt_flag(h), str(seat)]  # claude takes -file; pi's --append-system-prompt accepts a path
    sys_prompt = h.get("system_prompt")
    if isinstance(sys_prompt, str) and sys_prompt and prompt_flag(h):
        argv += [prompt_flag(h), sys_prompt]
    argv += claim_argv(h, sitting, agents_dir=agents_dir)
    if resume_id:
        # Ids follow a flag (`--resume <id>`) or sit before options (`codex resume <id> --model`), so
        # `--` cannot guard them without breaking those CLIs; an id that reads as an option is refused.
        if str(resume_id).startswith("-"):
            raise SystemExit(f"cowbell: refused — session id {resume_id!r} reads as an option")
        spec = list(h.get("resume") or [])
        if spec == ["resume"]:
            # A subcommand goes first: `codex resume <id> --model …`. Its own options are the
            # ones a resume honours; flags before it belong to the parent command.
            argv = ["resume", resume_id] + argv
        elif spec and spec[0].endswith("="):
            argv += [spec[0] + resume_id]
        elif spec:
            argv += spec + [resume_id]
    return argv + list(extra or [])


# C0 controls and DEL: a line typed into a pane's shell acts on them even inside quotes (^U, \r).
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Display text from another account's stores also loses C1 controls (terminal escapes).
CONTROL_WIDE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Starts with a letter or digit: never an option (`--dangerously-skip-permissions`) nor `..`.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def shell_join(parts: list[str]) -> str:
    """Quote words for a shell. A word with a control character is refused, never quoted: shlex
    keeps the byte, and an interactive shell's line editor acts on it before quoting applies."""
    import shlex
    for p in parts:
        if CONTROL.search(str(p)):
            raise SystemExit(f"cowbell: refused — a control character in a command word: {str(p)!r}")
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


def in_hc_app(env=None) -> bool:
    """A terminal of the Herdr desktop app (herdr-core, `hc`), not a herdr TUI pane.

    COWBELL_BACKEND=hc forces it; COWBELL_BACKEND=herdr forces the herdr TUI path. An app mod's popup
    picker (COWBELL_HC_SPACE or COWBELL_HC_CWD set) is the app whatever else the core's environment
    carries. Otherwise an hc terminal id with no herdr pane context: a herdr TUI run inside an app
    terminal is still herdr.
    """
    env = os.environ if env is None else env
    forced = (env.get("COWBELL_BACKEND") or "").strip().lower()
    if forced in ("hc", "app"):
        return True
    if forced in ("herdr", "tui"):
        return False
    if (env.get("COWBELL_HC_SPACE") or "").strip() or (env.get("COWBELL_HC_CWD") or "").strip():
        return True
    return bool(env.get("HC_TERMINAL_ID")) and not in_herdr_pane(env)


def under_hc_core(argv: list[str]) -> bool:
    """The process is a herdr-core terminal holder. Its path may hold a space (`Herdr Core.app`),
    so ps splits it: look at the first few words for a herdr-core path component."""
    return bool(re.search(r"(^|/)herdr-core(/|\s|$)", " ".join(argv[:3])))


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
        proc = run_bounded(["ps", "-axo", "pid=,ppid=,tty=,args="], PS_TIMEOUT)
        text = proc.stdout if proc else ""
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
    proc = run_bounded([lsof, "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))], LSOF_TIMEOUT)
    if proc is None:
        return {}
    out, pid = {}, None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def agent_processes(table: dict[int, dict] | None = None, cwds=process_cwds, skip_hc: bool = False) -> list[dict]:
    """Agent CLIs on a terminal, not under herdr (herdr lists its own) and not nested in another agent.
    `skip_hc` also leaves out agents under herdr-core, when the census reads those from `hc`."""
    t0 = time.perf_counter()
    scanned_ps = table is None
    table = ps_table() if table is None else table
    kinds = {pid: exe_kind(p["argv"]) for pid, p in table.items()}

    def outside(pid: int) -> bool:
        seen, cur = set(), table[pid]["ppid"]
        while cur in table and cur not in seen:
            seen.add(cur)
            if kinds.get(cur) or os.path.basename(table[cur]["argv"][0]) == "herdr":
                return False
            if skip_hc and under_hc_core(table[cur]["argv"]):
                return False
            cur = table[cur]["ppid"]
        return True

    found = [pid for pid, k in kinds.items()
             if k and table[pid]["tty"] not in ("??", "?", "-") and outside(pid)]
    where = cwds(found)
    trace.event("ps", ms=_ms(t0), scanned=len(table), agents=len(found), ps=int(scanned_ps))
    return [{"pane_id": f"pid:{pid}", "pid": pid, "tty": table[pid]["tty"], "agent": kinds[pid],
             "cwd": where[pid], "agent_status": "running", "tab_id": None, "workspace_id": None,
             "terminal_title_stripped": kinds[pid], "agent_session": {}}
            for pid in found if pid in where]


def here_command(cat: dict, h: dict, name: str, model="", lane="", allow="", deny="", seat: Path | None = None,
                 resume_id: str = "", extra: list[str] | None = None, effort: str = "",
                 dirs: list[str] | None = None) -> tuple[list[str], str]:
    """Full command line for this terminal, and the session id cowbell can record for a later resume."""
    import uuid
    argv = build_argv(cat, h, model, lane, allow, deny, seat, resume_id, sitting=name, extra=extra, effort=effort,
                      dirs=dirs)
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


def _plain_abs(path: str) -> bool:
    """Absolute, and no symlink anywhere in it (its realpath is its own normal form)."""
    return os.path.isabs(path) and os.path.realpath(path) == os.path.normpath(path)


def _home_ok(pw) -> bool:
    """pw's home is an absolute directory other than /, owned by that account."""
    home = str(getattr(pw, "pw_dir", "") or "")
    if not os.path.isabs(home) or os.path.realpath(home) == "/":
        return False
    try:
        st = os.stat(home)
    except OSError:
        return False
    import stat
    return stat.S_ISDIR(st.st_mode) and st.st_uid == getattr(pw, "pw_uid", None)


def _uid_owns(cat: dict, d: str, pw) -> bool:
    """d is at or under pw's home (a sound one, _home_ok), or inside a place whose own unix_user
    is that account."""
    home = str(getattr(pw, "pw_dir", "") or "")
    if _home_ok(pw) and at_or_beneath(d, os.path.realpath(home)):
        return True
    name = str(getattr(pw, "pw_name", "") or "")
    return bool(name) and any(str(p.get("unix_user") or "").strip() == name and at_or_beneath(d, p["cwd"])
                              for p in places(cat).values())


def launch_root(cat: dict, root: str, h: dict | None = None) -> str:
    """Where a launch starts. A harness with its own `cwd` (a tier's own folder) starts there, never
    in the caller's directory; otherwise root. The folder must be absolute with no symlink in it,
    belong to the account the harness runs as (under its home, or inside a place that account
    owns), and be enterable by the invoking user: cowbell starts the process there before the drop."""
    import pwd
    hh = h or {}
    fixed = str(hh.get("cwd") or "").strip()
    if not fixed:
        return root
    who = hh.get("id") or "this harness"
    if not _plain_abs(fixed):
        raise SystemExit(f"cowbell: refused — {who}: cwd {fixed!r} must be an absolute path with no symlink in it")
    d = os.path.normpath(fixed)
    uid = str(hh.get("unix_user") or "").strip()
    try:
        pw = pwd.getpwnam(uid) if uid else pwd.getpwuid(os.geteuid())
    except KeyError:
        raise SystemExit(f"cowbell: unix_user {uid!r} is not on this host")
    if not _home_ok(pw):
        raise SystemExit(f"cowbell: refused — {who}: the home of {uid or 'this user'} is not a directory it owns")
    if not _uid_owns(cat, d, pw):
        raise SystemExit(
            f"cowbell: refused — {who}: cwd {tilde(d)} is not under {uid or 'your'} home "
            "nor inside a place that account owns"
        )
    if not os.path.isdir(d) or not os.access(d, os.X_OK):
        raise SystemExit(
            f"cowbell: refused — {who} starts in {tilde(d)}, which this user cannot enter; "
            "this user needs traverse (search) on it"
        )
    return d


def _wall_file_problem(path: str) -> str:
    """Why a nono profile path cannot be trusted as a wall, or "" when it can: absolute, a regular
    file (not a symlink), owned by root, not writable by group or other."""
    import stat
    if not os.path.isabs(path):
        return "is not an absolute path"
    try:
        st = os.lstat(path)
    except OSError:
        return "does not exist"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_uid != 0:
        return "is not owned by root"
    if st.st_mode & 0o022:
        return "is writable by group or other"
    return ""


def _door_problem(h: dict, target_uid: int | None = None) -> str:
    """Why a door harness's command cannot be trusted to raise its own wall, or "". The command
    and every directory above it are owned by root (a door raises a wall for another account, so
    like a nono_profile it is root-installed: a copy in a synced or user-writable tree is refused),
    and none is writable by group or other (a root-owned sticky directory such as /tmp is allowed:
    others cannot replace what they do not own there)."""
    import stat
    command = str(h.get("command") or "")
    if not command or not os.path.isabs(command):
        return "a door needs an absolute `command` (the program that raises the wall)"
    if not _plain_abs(command):
        return f"door command {command} has a symlink in its path; name the real file"
    real = os.path.realpath(command)
    owners = {0} - ({target_uid} if target_uid is not None else set())
    try:
        st = os.stat(real)
    except OSError:
        return f"door command {command} does not exist"
    if not stat.S_ISREG(st.st_mode):
        return f"door command {command} is not a regular file"
    if st.st_uid not in owners:
        return f"door command {command} is owned by uid {st.st_uid}, not root"
    if st.st_mode & 0o022:
        return f"door command {command} is writable by group or other"
    d = os.path.dirname(real)
    while True:
        try:
            ds = os.stat(d)
        except OSError:
            return f"door directory {d} cannot be read"
        if ds.st_uid not in owners:
            return f"door directory {d} is owned by uid {ds.st_uid}, not root"
        if ds.st_mode & 0o022 and not (ds.st_mode & stat.S_ISVTX and ds.st_uid == 0):
            return f"door directory {d} is writable by group or other"
        if d == os.path.dirname(d):
            return ""
        d = os.path.dirname(d)


def unix_confine(cat: dict, root: str, cmd: list[str], h: dict | None = None) -> list[str]:
    """Drop to the harness, place or default unix_user through agent-run, inside its wall.

    The level that names the uid names the wall: a harness unix_user needs that harness's own
    nono_profile or wall = "door"; a place's, the place's nono_profile; [defaults]', its own. A
    harness wall without a harness unix_user is refused. Fails closed: a drop to another account
    with no wall refuses, door and nono_profile anywhere in the chain refuse, a nono_profile must
    be an absolute root-owned file nobody else can write, and a drop needs agent-run (no sudo -u
    fallback: that would skip agent-run-helper's checks). `--allow-cwd` is passed only when no uid
    is dropped, the harness starts in its own cwd, or the launch is inside a place whose own
    unix_user is the dropping account."""
    import pwd
    root = launch_root(cat, root, h)
    pl = place_for(cat, root)
    hh, pp = h or {}, pl or {}
    defaults = cat.get("defaults") or {}
    who = hh.get("id") or "this launch"

    def val(level: dict, key: str) -> str:
        return str(level.get(key) or "").strip()

    h_uid, h_prof, h_wall = val(hh, "unix_user"), val(hh, "nono_profile"), val(hh, "wall")
    if h_wall and h_wall != "door":
        raise SystemExit(f'cowbell: harness wall = {h_wall!r}: the only value is "door" (the command raises its own wall)')
    if (h_prof or h_wall) and not h_uid:
        raise SystemExit(f"cowbell: refused — {who} names a wall but no unix_user of its own; "
                         "the level that names the account names the wall")
    for level, where in ((pp, "place"), (defaults, "[defaults]")):
        if val(level, "wall"):
            raise SystemExit(f'cowbell: refused — wall = "door" on a {where}; it belongs on the harness '
                             "that raises its own wall, with that harness's unix_user")
    if h_uid:
        uid, prof, door, port = h_uid, h_prof, h_wall == "door", hh.get("nono_proxy_port")
    elif val(pp, "unix_user"):
        uid, prof, door, port = val(pp, "unix_user"), val(pp, "nono_profile"), False, pp.get("nono_proxy_port")
    else:
        uid, prof, door, port = (val(defaults, "unix_user"), val(defaults, "nono_profile"), False,
                                 defaults.get("nono_proxy_port"))
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535):
        raise SystemExit(f"cowbell: refused — {who}: nono_proxy_port {port!r} must be a port number 1024-65535")
    if port is not None and not prof:
        raise SystemExit(f"cowbell: refused — {who}: nono_proxy_port needs a nono_profile beside it")
    if door and any(val(level, "nono_profile") for level in (hh, pp, defaults)):
        raise SystemExit(f'cowbell: refused — {who}: wall = "door" and a nono_profile in the same launch; pick one')
    require = pp.get("require_uid")
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
    drop = os.geteuid() != pw.pw_uid
    if door:
        why = _door_problem(hh, pw.pw_uid)
        if why:
            raise SystemExit(f"cowbell: refused — {who}: {why}")
    elif drop and not prof:
        raise SystemExit(
            f"cowbell: refused — {who} runs as {uid!r} with no wall; set nono_profile beside that "
            'unix_user, or wall = "door" on a harness whose command raises its own'
        )
    inner = list(cmd)
    if prof:
        why = _wall_file_problem(prof)
        if why:
            raise SystemExit(f"cowbell: refused — nono_profile {prof} {why}")
        nono = shutil.which("nono")
        if not nono:
            raise SystemExit("cowbell: nono is not on PATH (needed for this place)")
        own_folder = bool(h_uid and val(hh, "cwd"))
        own_place = val(pp, "unix_user") == uid
        flags = ["--allow-cwd"] if (not drop or own_folder or own_place) else []
        if port is not None:  # the loopback port the account's firewall passes to nono's proxy
            flags += ["--proxy-port", str(port)]
        inner = [nono, "run", "--silent", "--profile", prof, *flags, "--"] + inner
    if not drop:
        return inner
    helper = shutil.which("agent-run")
    if not helper:
        raise SystemExit(
            f"cowbell: refused — dropping to unix user {uid!r} needs agent-run on PATH "
            "(cowbell does not fall back to sudo -u, which would skip agent-run's checks)"
        )
    return [helper, uid, "--"] + inner


def _sitting_kept(rows: list[dict], name: str, session_id: str) -> tuple[list[dict], dict | None]:
    """Drop rows that are this sitting (same name, or the same session id). Keep the first as `old`."""
    kept, old = [], None
    for r in rows:
        same = (name and r.get("name") == name) or (session_id and r.get("session_id") == session_id)
        if same:
            old = old or r
            continue
        kept.append(r)
    return kept, old


def _apply_name_source(row: dict, old: dict | None, name_source: str) -> None:
    """A pin survives a relaunch. A new explicit name pins. An automatic name does not."""
    if name_source == "pinned":
        row["name_source"] = "pinned"
        return
    if old and old.get("name_source") == "pinned":
        row["name"] = old.get("name") or row["name"]
        row["name_source"] = "pinned"
        return
    if name_source:
        row["name_source"] = name_source
    elif old and old.get("name_source"):
        row["name_source"] = old["name_source"]


def _record_here(h: dict, name: str, root: str, model: str, lane: str, allow: str, deny: str,
                 seat: Path | None, session: str, cmd: list[str], pane_id: str | None,
                 name_source: str = "", extra: list[str] | None = None, effort: str = "",
                 dirs: list[str] | None = None) -> None:
    rows, old = _sitting_kept(load_roster(), name, session)
    row = {"name": name, "cwd": root, "harness": h["id"], "kind": h["kind"], "model": model, "lane": lane,
           "allow": allow, "deny": deny, "seat": str(seat) if seat else "", "session_id": session,
           "pane_id": pane_id, "created": (old or {}).get("created") or now(), "last_seen": now()[:10],
           "argv": cmd[1:], "source": "here"}
    if effort:
        row["effort"] = effort
    if dirs:
        row["dirs"] = list(dirs)
    if extra:
        row["extra"] = list(extra)
    _apply_name_source(row, old, name_source)
    rows.append(row)
    save_roster(rows)


def _expected_cwd(h: dict, root: str) -> str:
    """The physical directory a launch must land in: a harness's own folder is checked to be
    symlink-free (launch_root), so it must be exactly that; otherwise wherever root resolves now."""
    return os.path.normpath(root) if str((h or {}).get("cwd") or "").strip() else os.path.realpath(root)


def exec_here(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", model="", lane="", allow="", deny="",
              seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None, focus: bool = True,
              name_source: str = "", effort: str = "", dirs: list[str] | None = None, pane_id: str | None = None):
    """Hand this terminal to the agent. Does not return. `pane_id` is this terminal, when a host
    can find it again (an hc terminal); None elsewhere."""
    root = launch_root(cat, root, h)
    cmd, session = here_command(cat, h, name, model, lane, allow, deny, seat, resume_id, extra, effort=effort,
                                dirs=dirs)
    cmd = unix_confine(cat, root, cmd, h=h)
    exe = _here_exe(h, cmd)
    _record_here(h, name, root, model, lane, allow, deny, seat, session, cmd, pane_id, name_source, extra, effort,
                 dirs)
    trace.event("exec", harness=h.get("id", ""), cmd=shell_join(redact_launch(cmd, extra)))
    sys.stdout.write("\x1b[H\x1b[2J" if sys.stdout.isatty() else "")
    print(f"  {paint(name, INK, bold=True)}  {paint(shell_join(cmd), MUTED)}  {paint(tilde(root), FAINT)}")
    sys.stdout.flush()
    expected = _expected_cwd(h, root)
    os.chdir(root)
    if os.getcwd() != expected:
        raise SystemExit(f"cowbell: refused — {tilde(root)} resolved to {tilde(os.getcwd())} after the change of directory")
    os.execv(exe, cmd)


def spawn_here(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", model="", lane="", allow="", deny="",
               seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None, focus: bool = True,
               name_source: str = "", effort: str = "", dirs: list[str] | None = None) -> dict:
    """Start the agent in a new session at root and return. Assemblies raise N times this way."""
    root = launch_root(cat, root, h)
    cmd, session = here_command(cat, h, name, model, lane, allow, deny, seat, resume_id, extra, effort=effort,
                                dirs=dirs)
    cmd = unix_confine(cat, root, cmd, h=h)
    exe = _here_exe(h, cmd)
    expected = _expected_cwd(h, root)

    def landed() -> None:  # runs in the child after its chdir(root), before exec
        if os.getcwd() != expected:
            raise OSError(f"{root} resolved to {os.getcwd()}")

    try:
        # preexec_fn is safe only while no other thread exists at fork (cowbell starts none on this
        # path). If threads are added here, switch to a `sh -c 'cd -P -- "$1" && [ "$(pwd -P)" = "$1" ]
        # && shift && exec "$@"'` wrapper like the herdr path's.
        proc = subprocess.Popen(
            [exe, *cmd[1:]], cwd=root, start_new_session=True, preexec_fn=landed,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.SubprocessError as e:
        raise SystemExit(f"cowbell: refused — the launch folder moved under it ({e})")
    pane_id = f"pid:{proc.pid}"
    _record_here(h, name, root, model, lane, allow, deny, seat, session, cmd, pane_id, name_source, extra, effort, dirs)
    trace.event("spawn", harness=h.get("id", ""), pid=proc.pid, cmd=shell_join(redact_launch(cmd, extra)))
    return {"pane_id": pane_id, "pid": proc.pid}


def launch(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", *args, **kw):
    """Start or resume through the host chosen once: a new herdr tab, or this terminal.
    The row's `requires` run first, here, so a model server is up before the agent asks it; then a
    stale `check` runs, so an unproven row never opens."""
    run_requires(h)
    ensure_accepted(h)
    return ctx.launch.start(cat, h, name, root, ctx, *args, **kw)


# --- hosts ---------------------------------------------------------------------
# Two seams, chosen once in main(): where running agents come from (LiveSource) and where a
# started or resumed agent goes (HostLaunch). Nothing outside the herdr adapter runs `herdr`.

class LiveSource:
    """Running agents as herdr-shaped rows (pane_id, agent, cwd, agent_status, ...). The empty source."""
    herdr = False  # True when rows are herdr panes that can be focused, adopted, stopped
    hc = False  # True when rows are herdr-core (`hc`) terminals of the Herdr desktop app

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

    def __init__(self, skip_hc: bool = False):
        self.skip_hc = skip_hc  # the hc census lists herdr-core terminals itself

    def agents(self) -> list[dict]:
        return agent_processes(skip_hc=True) if self.skip_hc else agent_processes()

    def focus(self, row: dict) -> str:
        raise SystemExit(f"{row.get('name')} runs on {row.get('tty')} (pid {row.get('pid')}); "
                         f"cowbell can't switch to another terminal")


class CombinedLive(LiveSource):
    def __init__(self, *sources: LiveSource | None):
        self.sources = [x for x in sources if x is not None]
        self.herdr = any(x.herdr for x in self.sources)
        self.hc = any(x.hc for x in self.sources)

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
        pane = str(row.get("pane_id") or "")
        owner = ProcessScan if pane.startswith("pid:") else HcLive if pane.startswith("hc:") else HerdrLive
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
        kw.pop("split", None)  # a split is a herdr pane; this host has none
        fn = spawn_here if spawn else exec_here
        return fn(cat, h, name, root, ctx, *args, **kw)


# --- herdr adapter (the only code that runs the herdr CLI) ------------------------------------

def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")


HERDR_TIMEOUT = 5.0  # seconds; a hung herdr server must not freeze the picker tick


_HERDR_DOWN_UNTIL = 0.0  # one tick-fast timeout marks herdr down briefly, so a stalled CLI costs a frame ~5s, not 5s per call


def herdr(*args: str, expect_json: bool = True, timeout: float = HERDR_TIMEOUT,
          log_argv: list[str] | None = None) -> dict:
    """Run the herdr CLI. `log_argv` is what the trace shows instead of `args` when one arg
    packs words that only the caller can tell apart (a whole launch line to `pane run`)."""
    global _HERDR_DOWN_UNTIL
    # the breaker exists for the picker's ~5s tick; a caller that paid for a longer wait
    # (agent start's 35s, launch cleanup) neither trips it nor is skipped by it.
    # a sub-tick timeout still arms the full window — callers must pay the tick
    tick = timeout <= HERDR_TIMEOUT
    if tick and time.monotonic() < _HERDR_DOWN_UNTIL:
        raise HerdrError(f"herdr {' '.join(args[:2])} skipped: marked down after a timeout")
    t0 = time.perf_counter()
    shown = " ".join(redact_argv(args if log_argv is None else log_argv))
    try:
        proc = subprocess.run([herdr_bin(), *args], capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        if tick:
            _HERDR_DOWN_UNTIL = time.monotonic() + 30
        trace.event("herdr", ms=_ms(t0), err="timeout", argv=shown)
        raise HerdrError(f"herdr {' '.join(args[:2])} timed out after {timeout:g}s") from e
    except OSError as e:  # herdr not installed: standalone cowbell still works
        trace.event("herdr", ms=_ms(t0), err="os", argv=shown)
        raise HerdrError(f"herdr unavailable: {e}") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        trace.event("herdr", ms=_ms(t0), rc=proc.returncode, argv=shown, err=redact_text(err or "exit")[:120])
        raise HerdrError(err or f"herdr {' '.join(args)} exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    if not out:
        trace.event("herdr", ms=_ms(t0), rc=0, argv=shown)
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        if expect_json:
            trace.event("herdr", ms=_ms(t0), err="json", argv=shown)
            raise HerdrError(f"herdr {' '.join(args)}: non-JSON output: {out[:120]}")
        trace.event("herdr", ms=_ms(t0), rc=0, argv=shown)
        return {"raw": out}
    if isinstance(payload, dict) and "error" in payload:
        e = payload["error"]
        trace.event("herdr", ms=_ms(t0), err="payload", argv=shown)
        raise HerdrError(e.get("message", str(e)) if isinstance(e, dict) else str(e))
    trace.event("herdr", ms=_ms(t0), rc=0, argv=shown, bytes=len(out))
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
    panes = live_panes()
    labels = {w.get("workspace_id"): w.get("label") or "" for w in workspaces()}
    grouped = [p for p in panes
               if p.get("cwd") and at_or_beneath(p["cwd"], root)
               and labels.get(p.get("workspace_id") or "", "").startswith("proj:")]
    if grouped:
        grouped.sort(key=lambda p: 0 if os.path.realpath(p["cwd"]) == os.path.realpath(root) else 1)
        return grouped[0]["workspace_id"]
    if ctx.workspace_id:
        return ctx.workspace_id
    for p in panes:
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
    t0, polls, ready = time.perf_counter(), 0, 0
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        polls += 1
        try:
            if shell_ready_from_process(herdr("pane", "process-info", "--pane", pane_id)):
                ready = 1
                break
        except (HerdrError, OSError):
            pass
        time.sleep(0.15)
    trace.event("shell", pane=pane_id, ms=_ms(t0), polls=polls, ready=ready)


def start_native(name: str, kind: str, pane_id: str, argv: list[str]) -> dict:
    wait_shell(pane_id)
    args = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "30000"]
    if argv:
        args += ["--", *argv]
    last: HerdrError | None = None
    for attempt in range(5):
        try:
            return herdr(*args, timeout=35)  # herdr itself waits up to 30s (--timeout 30000)
        except HerdrError as e:
            last = e
            msg = str(e)
            if any(k in msg for k in ("not_ready", "blocked", "agent_not_ready")):
                return {}
            if "agent_pane_busy" in msg or "not an available shell" in msg:
                trace.event("start", pane=pane_id, attempt=attempt + 1, busy=1)
                time.sleep(0.4)
                continue
            raise
    raise last or HerdrError(f"agent start {name} failed")


def raise_harness(cat: dict, h: dict, name: str, root: str, ctx: Ctx, model="", lane="", allow="", deny="",
                  seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None,
                  focus: bool = True, pen: bool = False, split: bool = False,
                  reuse_pane: dict | None = None, name_source: str = "", effort: str = "",
                  dirs: list[str] | None = None) -> dict:
    t0 = time.perf_counter()
    trace.event("raise", phase="begin", harness=h.get("id", ""), agent=h.get("kind", ""),
                name=name, resume=1 if resume_id else 0)
    root = launch_root(cat, root, h)
    argv = build_argv(cat, h, model, lane, allow, deny, seat, resume_id, sitting=name, extra=extra, effort=effort,
                      dirs=dirs)
    place = place_for(cat, root)
    base = ([h["command"], *argv] if is_wrapper(h) else [h["kind"], *argv])
    confined = unix_confine(cat, root, base, h=h)
    # --pen is a new machine-list line (the place's workspace_label).
    # Otherwise create a tab in the project workspace that already exists (proj:<dir>).
    # reuse_pane resumes into a pane that is still present — never a new tab.
    if reuse_pane:
        pane, how = reuse_pane, "reuse"
    elif pen and place and place.get("workspace_label"):
        pane, how = create_tab(workspace_by_label(place["workspace_label"]), root, name, place["workspace_label"]), "pen"
    else:
        wid = project_workspace(root, ctx)
        if split:
            pane = split_pane(wid, root) if wid else None
            how = "split"
            if not pane:
                pane, how = create_tab(pick_workspace(ctx, root), root, name), "tab"
        else:
            pane, how = create_tab(wid or pick_workspace(ctx, root), root, name), "tab"
    pid = pane["pane_id"]
    trace.event("raise", phase="pane", how=how, pane=pid, ms=_ms(t0))
    launched_session = ""
    mode = "run"
    try:
        if reuse_pane or confined != base or is_wrapper(h):
            wait_shell(pid)
            if any(CONTROL.search(str(w)) for w in [*confined, root]):
                raise SystemExit("cowbell: refused — a control character in the launch line or its folder")
            line = shell_join(confined)
            # the trace gets the same line built from the masked argv, never the packed line
            # re-parsed as free text (it cannot tell passthrough words, or an apostrophe, apart)
            shown = shell_join(redact_launch(confined, extra))
            if confined != base:  # a reused pane may sit anywhere: start the drop in its folder, physically
                q = shell_join([root])
                line = f'cd -P -- {q} && [ "$(pwd -P)" = {q} ] && {line}'
                shown = f'cd -P -- {q} && [ "$(pwd -P)" = {q} ] && {shown}'
            herdr("pane", "run", pid, line, expect_json=False, log_argv=["pane", "run", pid, shown])
        else:
            mode = "native"
            res = start_native(name, h["kind"], pid, argv)
            launched_session = ((res.get("agent") or {}).get("agent_session") or {}).get("value") or ""
    except (HerdrError, SystemExit):
        trace.event("raise", phase="fail", how=how, pane=pid, ms=_ms(t0))
        if pane.get("tab_id") and not reuse_pane:  # do not leave an empty tab behind a failed or refused launch
            try:
                # longer than HERDR_TIMEOUT so the cleanup still runs while a timed-out tick has herdr marked down
                herdr("tab", "close", pane["tab_id"], timeout=HERDR_TIMEOUT * 2)
            except HerdrError:
                pass
        raise
    session = launched_session or resume_id
    rows, old = _sitting_kept(load_roster(), name, session)
    row = {
        "name": name, "cwd": root, "harness": h["id"], "kind": h["kind"], "model": model, "lane": lane,
        "allow": allow, "deny": deny, "seat": str(seat) if seat else "",
        "session_id": session, "pane_id": pid, "tab_id": pane.get("tab_id"),
        "workspace_id": pane.get("workspace_id"), "created": (old or {}).get("created") or now(), "argv": argv,
    }
    if effort:
        row["effort"] = effort
    if dirs:
        row["dirs"] = list(dirs)
    if extra:
        row["extra"] = list(extra)
    _apply_name_source(row, old, name_source)
    rows.append(row)
    save_roster(rows)
    report_identity(pid, row["name"], h["kind"], model)   # one publish per raise
    # Tab stays the label create_tab was given. The pane border caches the
    # harness just launched; `cowbell reconcile` rewrites it from the agent Herdr detects.
    mark_harness(pid, row["name"], h["id"])
    if focus and pane.get("tab_id"):
        try:
            herdr("tab", "focus", pane["tab_id"])
        except HerdrError:
            pass
    trace.event("raise", phase="done", how=how, mode=mode, pane=pid, ms=_ms(t0), cmd=shell_join(redact_launch(base, extra)))
    return pane


def focus_agent(pane_id: str) -> None:
    herdr("agent", "focus", pane_id)


# herdr sidebar tokens (event-driven; never a loop).
# Kind is the occupant herdr detects, not who last launched via cowbell. Publish
# on raise, adopt, picker refresh, plugin startup stamp, and herdr occupancy
# hooks (`pane.agent_detected`, `pane.exited`). No interval thread; bt-reporter
# stays a separate, disabled tool.
# $glyph is a Lobe Icons Font plane-15 mark (hschne/lobe-icons-font) when that font
# is installed, else one ASCII letter (TEXT_GLYPH). `cowbell font install` fetches it. No TTL: the plate paints this
# token, and a 24h overlay made the badges vanish. Occupancy events clear or
# overwrite the overlay when the pane's agent changes.

COWBELL_SOURCE = "cowbell"

# Kind → Lobe Icons Font codepoint (codepoints.json in hschne/lobe-icons-font).
LOBE_GLYPH = {
    "claude":       "\U000F4036",
    "claudecode":   "\U000F4037",
    "grok":         "\U000F4079",
    "gemini":       "\U000F406B",
    "geminicli":    "\U000F406C",
    "agy":          "\U000F4015",  # Antigravity
    "antigravity":  "\U000F4015",
    "codex":        "\U000F403E",
    "openai":       "\U000F40D0",
    "pi":           "\U000F413E",
    "copilot":      "\U000F4046",
    "devin":        "\U000F4056",
    "ollama":       "\U000F40CF",
}


# Without the font a plane-15 mark draws as a box, so each kind also has one
# ASCII letter. Same width (one cell), so every row stays aligned.
TEXT_GLYPH = {
    "claude":       "C",
    "claudecode":   "C",
    "grok":         "G",
    "gemini":       "M",
    "geminicli":    "M",
    "agy":          "A",
    "antigravity":  "A",
    "codex":        "X",
    "openai":       "O",
    "pi":           "P",
    "copilot":      "H",
    "devin":        "D",
    "ollama":       "L",
}

# The font, pinned. `cowbell font install` fetches exactly this file and refuses
# any other bytes. MIT-licensed; the marks themselves are their owners' trademarks.
LOBE_FONT_VERSION = "v5.23.0"
LOBE_FONT_URL = ("https://github.com/hschne/lobe-icons-font/releases/download/"
                 f"{LOBE_FONT_VERSION}/lobe-icons.ttf")
LOBE_FONT_SHA256 = "2e6a3dd83437d51db9b990a0f9ad8e5bc4919d79a8db3a9a83ec1939ab4f4b3e"
LOBE_FONT_FILE = "lobe-icons.ttf"
LOBE_FONT_RANGE = "U+F4000-U+F47FF"
_GLYPH_MODE: str | None = None


def font_dirs() -> list[Path]:
    """Where an installed lobe-icons.ttf can live. The first entry is where install writes."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Fonts", Path("/Library/Fonts")]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    return [data / "fonts", home / ".fonts", Path("/usr/local/share/fonts"), Path("/usr/share/fonts")]


def lobe_font_path() -> Path | None:
    """The installed font file, or None. Looks in the font directories only; no fc-list."""
    for d in font_dirs():
        f = d / LOBE_FONT_FILE
        if f.is_file():
            return f
        if sys.platform != "darwin" and d.is_dir():
            try:
                hit = next(d.glob(f"*/{LOBE_FONT_FILE}"), None)
            except OSError:
                hit = None
            if hit:
                return hit
    return None


def glyph_mode() -> str:
    """`lobe` or `text`. COWBELL_GLYPHS=lobe|text forces it (say lobe when the font is on the
    machine that draws the terminal, not this one, e.g. over ssh). Default: lobe only if the
    font is installed here."""
    global _GLYPH_MODE
    forced = (os.environ.get("COWBELL_GLYPHS") or "").strip().lower()
    if forced in ("lobe", "text"):
        return forced
    if _GLYPH_MODE is None:
        _GLYPH_MODE = "lobe" if lobe_font_path() else "text"
    return _GLYPH_MODE


def glyph_for(kind: str, model: str = "") -> str:
    """One mark per harness kind: a Lobe mark when the font is there, else one ASCII letter.
    `model` stays on the call sites (quota_model)."""
    table = LOBE_GLYPH if glyph_mode() == "lobe" else TEXT_GLYPH
    return table.get(kind or "", "·")


def font_terminal_lines() -> str:
    return (f"  Ghostty  font-codepoint-map = {LOBE_FONT_RANGE}=lobe-icons\n"
            f"  kitty    symbol_map {LOBE_FONT_RANGE} lobe-icons\n"
            "  Then open a new terminal window; an open window keeps its old fonts.")


def cmd_font_status() -> int:
    path = lobe_font_path()
    forced = (os.environ.get("COWBELL_GLYPHS") or "").strip().lower()
    print(f"font:  {path if path else 'not installed (run: cowbell font install)'}")
    print(f"marks: {glyph_mode()}" + (" (COWBELL_GLYPHS)" if forced in ("lobe", "text") else ""))
    print("If marks draw as boxes, map the range to the font in your terminal:")
    print(font_terminal_lines())
    return 0


def cmd_font_install(fetch=None) -> int:
    """Fetch the pinned font, check its sha256, write it to the user font directory."""
    import hashlib
    dest_dir = font_dirs()[0]
    dest = dest_dir / LOBE_FONT_FILE
    if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == LOBE_FONT_SHA256:
        print(f"already installed: {dest} ({LOBE_FONT_VERSION})")
        return 0
    if fetch is None:
        import urllib.request
        def fetch(url: str) -> bytes:
            with urllib.request.urlopen(url, timeout=30) as r:
                return r.read()
    try:
        data = fetch(LOBE_FONT_URL)
    except Exception as e:
        print(f"cowbell: could not download {LOBE_FONT_URL}: {e}", file=sys.stderr)
        return 1
    got = hashlib.sha256(data).hexdigest()
    if got != LOBE_FONT_SHA256:
        print(f"cowbell: refused {LOBE_FONT_URL}: sha256 {got}, expected {LOBE_FONT_SHA256}",
              file=sys.stderr)
        return 1
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    if sys.platform != "darwin" and shutil.which("fc-cache"):
        subprocess.run(["fc-cache", "-f", str(dest_dir)], capture_output=True, timeout=30)
    print(f"installed: {dest} ({LOBE_FONT_VERSION}, sha256 checked)")
    print("If marks still draw as boxes, map the range in your terminal:")
    print(font_terminal_lines())
    return 0


# Titles the agent chrome writes. They are not a sitting.
_GENERIC_TITLES = frozenset({
    "", "claude", "claude code", "agy", "grok", "codex", "devin", "opencode", "pi", "cb",
})


def session_label(title: str, tab: str) -> str:
    """The row names the sitting. The sitting is the tab.

    A terminal title is the work in the pane (`- Stamp live glyphs…`,
    `Suggest a Coding Improvement`, `Mint sysadmin… - Grok`). It is not a
    rename of the sitting. Two harnesses in one sitting share that name;
    `$glyph` tells them apart. An empty tab falls back to a short title
    that is not chrome and not a sentence.
    """
    tab = (tab or "").strip()
    if tab:
        return tab
    raw = (title or "").strip()
    lower = raw.lower()
    if lower.startswith("devin:"):
        raw = raw.split(":", 1)[1].strip()
        lower = raw.lower()
    if not raw or lower.startswith(("functions.", "π", "-")):
        return ""
    head = raw.split(" - ")[0].strip()
    if not head or head.lower() in _GENERIC_TITLES or " " in head:
        return ""
    return head


def badge_name(agent: dict | None, tab: str, title: str = "") -> str:
    """Name published on a pane badge. Same order as the menu. Does not rename the tab."""
    agent = agent or {}
    sid = ((agent.get("agent_session") or {}).get("value") or "")
    kind = str(agent.get("agent") or agent.get("kind") or "")
    cwd = str(agent.get("cwd") or "")
    fields = session_fields(kind, cwd, sid) if kind and cwd and sid else {}
    pinned = stored = ""
    if sid:
        matches = [r for r in load_roster() if r.get("session_id") == sid]
        if len(matches) == 1:
            if matches[0].get("name_source") == "pinned":
                pinned = matches[0].get("name") or ""
            else:
                stored = matches[0].get("name") or ""
    name, _source = canonical_name(
        pinned=pinned,
        harness_title=fields.get("harness_title") or "",
        tab=tab,
        stored=stored,
        utterance=fields.get("utterance") or "",
        terminal=title or str(agent.get("terminal_title_stripped") or ""),
        kind=kind,
        session_id=sid,
    )
    return name


def report_identity(pane_id: str, name: str, kind: str, model: str = "") -> bool:
    """One event, one publish: $glyph (badge), $name (the sitting), $line1, display_agent."""
    if not pane_id or str(pane_id).startswith("pid:"):
        return False  # a process on another terminal has no herdr pane to badge
    if str(pane_id).startswith("hc:"):  # the app draws its own agent icon; the name is a label
        return hc_label(hc_addr(pane_id), name)
    g = glyph_for(kind, model)
    try:
        herdr("pane", "report-metadata", pane_id, "--source", COWBELL_SOURCE,
              "--display-agent", g,
              "--token", f"glyph={g}",
              "--token", f"name={name}",
              "--token", f"line1={g} {name}")
    except HerdrError:
        return False
    return True


def clear_identity(pane_id: str) -> bool:
    """Drop cowbell's badge on a pane that no longer holds that sitting."""
    if not pane_id or str(pane_id).startswith("pid:"):
        return False
    if str(pane_id).startswith("hc:"):
        return hc_label(hc_addr(pane_id), "")
    try:
        herdr("pane", "report-metadata", pane_id, "--source", COWBELL_SOURCE,
              "--clear-display-agent",
              "--clear-token", "glyph", "--clear-token", "name", "--clear-token", "line1")
    except HerdrError:
        return False
    return True


def stamp_live_identities(snap: dict | None = None) -> int:
    """Publish `$glyph` for every live herdr agent pane. Kind is the detector
    `.agent` field (plus Claude `quota_model` when present). No TTL."""
    snap = snapshot() if snap is None else snap
    labels = tab_labels(snap)
    n = 0
    for a in snap.get("agents") or []:
        pane_id = str(a.get("pane_id") or "")
        kind = str(a.get("agent") or "").strip()
        if not pane_id or pane_id.startswith("pid:") or not kind:
            continue
        tab = labels.get(a.get("tab_id"), "") or ""
        name = badge_name(a, tab) or pane_id
        tokens = a.get("tokens") or {}
        model = tokens.get("quota_model") or "" if isinstance(tokens, dict) else ""
        if report_identity(pane_id, name, kind, model):
            n += 1
    return n


def _pane_agent(snap: dict, pane_id: str) -> dict | None:
    for agent in snap.get("agents") or []:
        if str(agent.get("pane_id") or "") == pane_id:
            return agent if isinstance(agent, dict) else None
    return None


def _plugin_json_env(name: str) -> dict:
    raw = os.environ.get(name) or ""
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def sync_identity_from_plugin_event(event: dict | None = None, context: dict | None = None) -> str:
    """Restamp or clear one pane from a herdr plugin occupancy event.

    Herdr puts the envelope in `HERDR_PLUGIN_EVENT_JSON` and the invocation
    context in `HERDR_PLUGIN_CONTEXT_JSON`. `pane.exited` or
    `pane.agent_detected` with `released` / no agent clears cowbell's overlay;
    a new detector kind overwrites it. Returns stamped, cleared, or skipped.
    """
    event = _plugin_json_env("HERDR_PLUGIN_EVENT_JSON") if event is None else event
    context = _plugin_json_env("HERDR_PLUGIN_CONTEXT_JSON") if context is None else context
    if not isinstance(event, dict):
        event = {}
    if not isinstance(context, dict):
        context = {}
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    name = str(event.get("event") or os.environ.get("HERDR_PLUGIN_EVENT") or "")
    pane_id = str(data.get("pane_id") or "")
    if not pane_id and name:
        pane_id = str(context.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID") or "")
    if not name or not pane_id or pane_id.startswith("pid:"):
        return "skipped"
    released = bool(data.get("released"))
    agent = str(data.get("agent") or "").strip()
    if name == "pane.exited" or (name == "pane.agent_detected" and (released or not agent)):
        return "cleared" if clear_identity(pane_id) else "skipped"
    snap = snapshot()
    live = _pane_agent(snap, pane_id)
    if name == "pane.agent_status_changed":
        if not live or not str(live.get("agent") or "").strip():
            return "cleared" if clear_identity(pane_id) else "skipped"
        agent = str(live.get("agent") or "").strip()
    elif not agent and live:
        agent = str(live.get("agent") or "").strip()
    if not agent:
        return "skipped"
    tab = ""
    title = ""
    model = ""
    current_glyph = ""
    current_name = ""
    if live:
        labels = tab_labels(snap)
        tab = labels.get(live.get("tab_id"), "") or ""
        title = str(live.get("terminal_title_stripped") or "")
        tokens = live.get("tokens") or {}
        if isinstance(tokens, dict):
            model = str(tokens.get("quota_model") or "")
            current_glyph = str(tokens.get("glyph") or "")
            current_name = str(tokens.get("name") or "")
    if not tab:
        tab = str(context.get("tab_label") or "")
    row_name = badge_name(live, tab, title) or pane_id
    border = slug(tab) or row_name
    if name == "pane.agent_status_changed" and current_glyph == glyph_for(agent, model) and current_name == row_name:
        return "skipped"
    ok = report_identity(pane_id, row_name, agent, model)
    if ok and name != "pane.agent_status_changed":
        mark_harness(pane_id, border, agent)
        return "stamped"
    return "stamped" if ok else "skipped"


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


# herdr-core (`hc`) and the Herdr desktop app (`herdr-app`). Same rule: only this section runs them.
# An app terminal is a core terminal labelled herdr.space=<id> and herdr.tab=<id>; a tab id the app does
# not know becomes a new tab. cowbell names a sitting with the label cowbell.name. Row pane ids are
# `hc:<address>` (local addresses only; `machine/tN` rows are other machines' and are left out).

HC_NAME_LABEL = "cowbell.name"
# The app's names for agents it detects, where they differ from the CLI cowbell starts.
HC_APP_KIND = {"claude-code": "claude"}


class HcError(HerdrError):
    pass


def hc_bin() -> str:
    return os.environ.get("HC_BIN_PATH") or shutil.which("hc") or os.path.expanduser("~/.local/bin/hc")


def herdr_app_bin() -> str:
    return (os.environ.get("HERDR_APP_BIN_PATH") or shutil.which("herdr-app")
            or os.path.expanduser("~/.local/bin/herdr-app"))


def _run_json(exe: str, args: list[str], who: str, timeout: float) -> dict:
    t0 = time.perf_counter()
    shown = " ".join(redact_argv(args))
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        trace.event(who, ms=_ms(t0), err="timeout", argv=shown)
        raise HcError(f"{who} {' '.join(args[:2])} timed out after {timeout:g}s") from e
    except OSError as e:
        trace.event(who, ms=_ms(t0), err="os", argv=shown)
        raise HcError(f"{who} unavailable: {e}") from e
    out = (proc.stdout or "").strip()
    payload = None
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict) and payload.get("error"):
        e = payload["error"]
        trace.event(who, ms=_ms(t0), err="payload", argv=shown)
        raise HcError(e.get("message", str(e)) if isinstance(e, dict) else str(e))
    if proc.returncode != 0:
        err = (proc.stderr or out or "").strip()
        trace.event(who, ms=_ms(t0), rc=proc.returncode, argv=shown, err=redact_text(err or "exit")[:120])
        raise HcError(err or f"{who} {' '.join(args)} exited {proc.returncode}")
    trace.event(who, ms=_ms(t0), rc=0, argv=shown, bytes=len(out))
    if isinstance(payload, dict):
        return payload
    return {"value": payload} if payload is not None else ({"raw": out} if out else {})


def hc(*args: str, timeout: float = HERDR_TIMEOUT) -> dict:
    """Run the herdr-core CLI with --json, given right after the subcommand: anything after `--`
    belongs to the program `hc run` starts."""
    argv = list(args)
    own = argv[:argv.index("--")] if "--" in argv else argv
    if own and "--json" not in own:
        argv.insert(1, "--json")
    return _run_json(hc_bin(), argv, "hc", timeout)


def herdr_app(command: str, timeout: float = HERDR_TIMEOUT, **fields) -> dict:
    """`herdr-app do <command> --field value`: a structural command on the open window.
    A list value goes as JSON (argv); a bool as true/false."""
    args = ["do", command]
    for k, v in fields.items():
        if v is None:
            continue
        val = json.dumps(v) if isinstance(v, (list, dict)) else ("true" if v is True else "false" if v is False
                                                                  else str(v))
        args += [f"--{k}", val]
    return _run_json(herdr_app_bin(), args, "herdr-app", timeout)


def hc_space_override(env=None) -> str:
    """COWBELL_HC_SPACE: the space a popup picker's launches go to (the one the person was looking at).
    Ids are local to this machine; a `machine/` prefix is dropped."""
    env = os.environ if env is None else env
    raw = (env.get("COWBELL_HC_SPACE") or "").strip()
    return raw.rsplit("/", 1)[-1] if raw else ""


def hc_addr(pane_id) -> str:
    p = str(pane_id or "")
    return p[3:] if p.startswith("hc:") else ""


def hc_terminals() -> list[dict]:
    """Live terminals on this machine, as `hc ls` lists them."""
    try:
        ts = hc("ls").get("terminals") or []
    except HcError:
        return []
    return [t for t in ts if isinstance(t, dict) and t.get("address") and "/" not in str(t["address"])
            and (t.get("state") or "live") == "live"]


def hc_get(addr: str) -> dict:
    return hc("get", addr)


def hc_kind(t: dict, info: dict) -> str:
    """Which agent CLI holds the terminal: its foreground program, else what the app detected."""
    fg = info.get("foreground") or {}
    for word in (fg.get("name"), os.path.basename(str((fg.get("argv") or [""])[0]))):
        k = exe_kind([word]) if word else ""
        if k:
            return k
    for app in ((info.get("status") or {}).get("app"), t.get("agent")):
        app = str(app or "")
        k = HC_APP_KIND.get(app) or (app if app in AGENT_EXES else "")
        if k:
            return k
    return ""


def hc_tab_name(t: dict) -> str:
    """The app tab's name from `where` (`<space> / <tab>`). A number is a place, not a name."""
    where = str(t.get("where") or "")
    tab = where.split(" / ", 1)[1].strip() if " / " in where else ""
    return "" if tab.isdigit() else tab


def hc_agent_rows() -> list[dict]:
    """herdr-shaped rows for terminals that hold an agent. Shells and plain programs are left out."""
    rows = []
    for t in hc_terminals():
        if not t.get("status") and (str(t.get("agent") or "") in SHELLS or not t.get("agent")):
            continue
        try:
            info = hc_get(t["address"])
        except HcError:
            continue
        kind = hc_kind(t, info)
        if not kind:
            continue
        labels = info.get("labels") or {}
        status = info.get("status") or {}
        fg = info.get("foreground") or {}
        records = info.get("records") or {}
        title = next((r.get("title") for r in records.values() if isinstance(r, dict) and r.get("title")), "")
        rows.append({
            "pane_id": f"hc:{t['address']}", "agent": kind,
            "cwd": fg.get("cwd") or info.get("cwd") or t.get("cwd") or "",
            "agent_status": status.get("state") or t.get("status") or "live",
            "tab_id": labels.get("herdr.tab"), "workspace_id": labels.get("herdr.space"),
            "terminal_title_stripped": title or info.get("title") or "", "agent_session": {},
            "hc_name": labels.get(HC_NAME_LABEL) or "", "tab_name": hc_tab_name(t),
        })
    return rows


class HcLive(LiveSource):
    """Agents in herdr-core terminals (the Herdr desktop app)."""
    hc = True

    def __init__(self):
        self._rows: list[dict] | None = None

    def agents(self) -> list[dict]:
        self._rows = hc_agent_rows()
        return self._rows

    def panes(self) -> list[dict]:
        return [{"pane_id": f"hc:{t['address']}", "cwd": t.get("cwd") or "", "agent": t.get("agent") or ""}
                for t in hc_terminals()]

    def snapshot(self) -> dict:
        rows = self._rows if self._rows is not None else self.agents()
        tabs, seen = [], set()
        for r in rows:
            if r.get("tab_id") and r["tab_id"] not in seen:
                seen.add(r["tab_id"])
                tabs.append({"tab_id": r["tab_id"], "label": r.get("tab_name") or ""})
        return {"agents": rows, "tabs": tabs}

    def agent(self, name: str) -> dict | None:
        return next((r for r in self.agents() if name and r.get("hc_name") == name), None)

    def focus(self, row: dict) -> str:
        addr = hc_addr(row.get("pane_id"))
        try:
            herdr_app("agent.focus", terminal=addr)
        except HcError as e:
            raise SystemExit(f"cowbell: could not show {row.get('name') or addr} in the Herdr app: {e}")
        return f"focused {row.get('name') or addr} in {row['pane_id']}"


def hc_caller_space(ctx: "Ctx") -> str:
    """Where a new tab goes: COWBELL_HC_SPACE (ctx.workspace_id), else the caller terminal's space."""
    if ctx.workspace_id:
        return ctx.workspace_id
    addr = hc_addr(ctx.pane_id)
    if addr:
        try:
            space = (hc_get(addr).get("labels") or {}).get("herdr.space") or ""
        except HcError:
            space = ""
        if space:
            return space
    raise SystemExit("cowbell: this terminal is not in a Herdr app space (no herdr.space label); "
                     "set COWBELL_HC_SPACE to the space a new tab should open in")


def _hc_guarded(argv: list[str], expected: str) -> list[str]:
    """Start argv only once the physical directory is the one the launch was checked for."""
    return ["/bin/sh", "-c", 'cd -P -- "$1" && [ "$(pwd -P)" = "$1" ] && shift && exec "$@"', "sh",
            expected, *argv]


def hc_open_tab(space: str, root: str, argv: list[str], name: str) -> tuple[str, str]:
    """A new app tab in `space` running argv. Returns (address, tab id).

    `hc run --bg --keep` with the app's labels, not `herdr-app do terminal.open`: the core takes a target
    space, works with the window closed, and returns the address; terminal.open only places beside the
    focused pane. --no-parent: the tab must outlive the cowbell process that opened it."""
    import uuid
    tab = "tb" + uuid.uuid4().hex
    args = ["run", "--bg", "--keep", "--no-parent", "--cwd", root,
            "--label", f"herdr.space={space}", "--label", f"herdr.tab={tab}"]
    if name:
        args += ["--label", f"{HC_NAME_LABEL}={name}"]
    res = hc(*args, "--", *argv)
    addr = res.get("address") or res.get("terminal") or (res.get("raw") or "").strip()
    if not addr:
        raise HcError(f"hc run returned no address: {res}")
    return str(addr), tab


def hc_pane_of(addr: str) -> str | None:
    """The app pane that shows terminal `addr` (from `herdr-app ls`), or None."""
    if not addr:
        return None
    try:
        layout = _run_json(herdr_app_bin(), ["ls"], "herdr-app", HERDR_TIMEOUT)
    except HcError:
        return None
    for sp in layout.get("spaces") or []:
        for tab in sp.get("tabs") or []:
            for t in tab.get("terminals") or []:
                if t.get("terminal") == addr and t.get("pane"):
                    return t["pane"]
    return None


def hc_split_right(ctx: "Ctx", argv: list[str], name: str) -> str:
    """A new terminal to the right of the caller's pane, or of the focused pane for a popup picker
    (COWBELL_HC_SPACE), whose own terminal has no pane. The core has no split label, so this goes
    through the app (`pane.split`); the new address is the terminal that appears."""
    pane = None if ctx.workspace_id else hc_pane_of(hc_addr(ctx.pane_id))
    before = {t["address"] for t in hc_terminals()}
    res = herdr_app("pane.split", direction="right", pane=pane, argv=argv)
    new = str(res.get("terminal") or "")
    deadline = time.time() + 3
    while not new and time.time() < deadline:
        fresh = [t["address"] for t in hc_terminals() if t["address"] not in before]
        if fresh:
            new = fresh[0]
            break
        time.sleep(0.15)
    if not new:
        raise HcError("the split opened, but its terminal did not appear in hc ls")
    if name:
        hc_label(new, name)
    return new


def hc_label(addr: str, name: str) -> bool:
    """Name the sitting on its terminal (`cowbell.name`); an empty name removes the label."""
    try:
        hc("label", addr, f"{HC_NAME_LABEL}={name}")
    except HcError:
        return False
    return True


def hc_shell_ready(addr: str) -> bool:
    """The terminal's foreground is a shell prompt, so a typed line starts there."""
    try:
        fg = hc_get(addr).get("foreground") or {}
    except HcError:
        return False
    return str(fg.get("name") or "").lstrip("-") in SHELLS


def hc_raise(cat: dict, h: dict, name: str, root: str, ctx: "Ctx", model="", lane="", allow="", deny="",
             seat: Path | None = None, resume_id: str = "", extra: list[str] | None = None, focus: bool = True,
             split: bool = False, reuse_pane: dict | None = None, name_source: str = "", effort: str = "",
             dirs: list[str] | None = None) -> dict:
    """Start the agent in an app terminal: a new tab in the caller's space (or COWBELL_HC_SPACE), a split
    to the right, or a still-open terminal whose shell takes the line. The command is the one this
    terminal would exec (a minted session id where the harness takes one), so a resume can replay it."""
    t0 = time.perf_counter()
    root = launch_root(cat, root, h)
    cmd, session = here_command(cat, h, name, model, lane, allow, deny, seat, resume_id, extra, effort=effort,
                                dirs=dirs)
    cmd = unix_confine(cat, root, cmd, h=h)
    argv = [_here_exe(h, cmd), *cmd[1:]]  # absolute: the core's PATH is not this shell's
    expected = _expected_cwd(h, root)
    if any(CONTROL.search(str(w)) for w in [*argv, root, expected]):
        raise SystemExit("cowbell: refused — a control character in the launch line or its folder")
    shown = shell_join(redact_launch(cmd, extra))
    space, tab, how = ctx.workspace_id, None, "tab"
    addr = hc_addr((reuse_pane or {}).get("pane_id"))
    if addr and hc_shell_ready(addr):
        q = shell_join([expected])
        hc("send", addr, f'cd -P -- {q} && [ "$(pwd -P)" = {q} ] && {shell_join(argv)}')
        how = "reuse"
        hc_label(addr, name)
    elif split:
        addr, how = hc_split_right(ctx, _hc_guarded(argv, expected), name), "split"
    else:
        space = hc_caller_space(ctx)
        addr, tab = hc_open_tab(space, root, _hc_guarded(argv, expected), name)
    pane_id = f"hc:{addr}"
    _record_here(h, name, root, model, lane, allow, deny, seat, session, cmd, pane_id, name_source, extra, effort,
                 dirs)
    if tab:
        # The app adopts the labelled terminal a moment later; name its tab after the sitting, best-effort.
        deadline = time.time() + 2
        while True:
            try:
                herdr_app("tab.rename", id=tab, name=name)
                break
            except HcError:
                if time.time() > deadline:
                    break
                time.sleep(0.2)
    if focus:
        try:
            herdr_app("agent.focus", terminal=addr)
        except HcError:
            pass
    trace.event("raise", phase="done", how=how, mode="hc", pane=pane_id, ms=_ms(t0), cmd=shown)
    return {"pane_id": pane_id, "tab_id": tab, "workspace_id": space}


class HcLaunch(HostLaunch):
    """The Herdr desktop app. A person execs here (this terminal is recorded, so enter can find it
    again). An agent caller, --spawn, --split and a popup picker open an app terminal."""
    tabs = True

    def start(self, cat, h, name, root, ctx, *args, **kw):
        spawn = kw.pop("spawn", False)
        split = kw.pop("split", False)
        reuse = kw.pop("reuse_pane", None)
        kw.pop("pen", None)  # a pen is a herdr TUI workspace
        own = hc_addr(ctx.pane_id)
        if reuse and own and hc_addr(reuse.get("pane_id")) == own and ctx.mode == "here":
            reuse = None  # resuming into this very terminal: exec, do not type into it
        if reuse or spawn or split or ctx.mode == "app":
            return hc_raise(cat, h, name, root, ctx, *args, split=split, reuse_pane=reuse, **kw)
        return exec_here(cat, h, name, root, ctx, *args, pane_id=ctx.pane_id, **kw)


def mark_harness(pane_id: str, sitting: str, harness: str) -> None:
    """Cache `sitting · harness` on the pane border. A failed rename does not fail the raise."""
    if not pane_id or str(pane_id).startswith(("pid:", "hc:")):
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
    """Panes whose border disagrees with the sitting and the agent Herdr detects.

    The unscoped pane list omits the border label, so each workspace is listed on its own.
    An empty border is left out. A stale bare sitting is rewritten.
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
    if not has_herdr():
        print("herdr is not installed; nothing to reconcile")
        return []
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
    uid = ((place or {}).get("unix_user") or (cat.get("defaults") or {}).get("unix_user") or "").strip()
    cat = scoped_catalog(cat, place)
    rows = load_roster()
    ctx.live.refresh_sessions(rows)
    agents, panes = ctx.live.agents(), ctx.live.panes()
    warnings: list[str] = []
    reg = build_registry(cat, root, agents, panes, rows, subject=trust_subject(root),
                         tab_label=tab_labels(ctx.live.snapshot()), history=launch_history(cat, root, warnings))
    reg["warnings"] = warnings
    reg["resolved_uid"] = uid  # the uid a `new` row would launch as ("" = no drop)
    for a in reg.get("absent") or []:
        a["uid"] = uid
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


def _row_dirs(r: dict) -> list[str]:
    d = r.get("dirs")
    return [x for x in d if isinstance(x, str) and x] if isinstance(d, list) else []


def cmd_enter(ctx: Ctx, cat: dict, name: str, root: str | None = None, target: dict | None = None,
              shape=None) -> bool:
    """Focus the sitting if it is live, else resume it. False when `shape` was cancelled.

    `shape(cat, h, row, root)` returns the LaunchSpec to resume with, or None to cancel;
    without it the recorded row is replayed as it is.
    """
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
            history = launch_history(cat, root)
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
        return True
    for a in ctx.live.agents():
        if r.get("pane_id") and a.get("pane_id") == r.get("pane_id"):
            print(ctx.live.focus({**a, "name": r["name"]}))
            return True
    hid = r.get("harness") or r.get("kind") or ""
    h = harness_by_id(scoped, hid) or next((x for x in scoped.get("harness", []) if x.get("kind") == r.get("kind")), None)
    if not h:
        where = f"place {place['name']}" if place else "this catalog"
        raise SystemExit(f"cowbell: {r['name']} uses harness {hid!r} which may not resume in {where}")
    cat = scoped
    seat = Path(r["seat"]) if r.get("seat") and Path(r["seat"]).is_file() else None
    r = {**r, "model": row_launch_model(h, r)}
    if shape is not None:
        spec = shape(cat, h, r, r.get("cwd") or root or ctx.cwd)
        if spec is None:
            return False
        r = {**r, "model": spec.model, "effort": spec.effort, "lane": spec.lane,
             "allow": spec.allow, "deny": spec.deny, "dirs": list(spec.dirs)}
    # a still-present pane takes the resume in place; a torn-down one gets a new tab.
    pane_row = next((p for p in ctx.live.panes() if p.get("pane_id") == r.get("pane_id")), None)
    if pane_row and getattr(ctx.live, "herdr", False):
        pane = raise_harness(cat, h, r["name"], r.get("cwd") or root or ctx.cwd, ctx,
                             r.get("model", ""), r.get("lane", ""), r.get("allow", ""), r.get("deny", ""),
                             seat, resume_id=r.get("session_id", ""), reuse_pane=pane_row, focus=False,
                             effort=r.get("effort", ""), dirs=_row_dirs(r))
        print(f"resumed {r['name']} ({h['id']}) in {pane_row['pane_id']}")
        return True
    if pane_row and getattr(ctx.live, "hc", False) and str(pane_row.get("pane_id") or "").startswith("hc:"):
        # an app terminal still open at its shell takes the resume; HcLaunch opens a tab when it cannot
        pane = launch(cat, h, r["name"], r.get("cwd") or root or ctx.cwd, ctx, r.get("model", ""),
                      r.get("lane", ""), r.get("allow", ""), r.get("deny", ""), seat,
                      resume_id=r.get("session_id", ""), reuse_pane=pane_row, focus=False,
                      effort=r.get("effort", ""), dirs=_row_dirs(r))
        print(f"resumed {r['name']} ({h['id']}) in {(pane or {}).get('pane_id') or pane_row['pane_id']}")
        return True
    pane = launch(cat, h, r["name"], r.get("cwd") or root or ctx.cwd, ctx, r.get("model", ""), r.get("lane", ""),
                  r.get("allow", ""), r.get("deny", ""), seat, resume_id=r.get("session_id", ""),
                  effort=r.get("effort", ""), dirs=_row_dirs(r))
    pid = (pane or {}).get("pane_id") or ""
    print(f"resumed {r['name']} ({h['id']}){(' in ' + pid) if pid else ''}")
    return True


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
    if roster_as:
        row["name_source"] = "pinned"  # an explicit name; a guessed one stays replaceable
    rows.append(row)
    save_roster(rows)
    apane = str(a.get("pane_id") or "")
    if apane and not apane.startswith("pid:"):
        report_identity(apane, nm, kind)   # one publish per adopt
    print(f"adopted {nm}  {kind}  {tilde(cwd)}")
    return row


def accepted_pin(text: str) -> str:
    """A name a person typed. Nudges are allowed; `.` and the word harness are not."""
    s = slug(text or "")
    if s in {"", "harness", "h", "h-"} or len(s) < 2 or not NAME_RE.match(s):
        raise SystemExit(f"cowbell: {text!r} is not a session name")
    return s


def pin_session_name(current: str, new: str, target: dict | None = None) -> str:
    """Pin `new` on the roster row `current` names, or whose session id is `current`.

    Works with herdr down: the badge publish is best-effort. A history row that
    was never on the roster is added so the pin survives the next refresh.
    """
    rows = load_roster()
    nm = accepted_pin(new)
    hits = [r for r in rows if r.get("name") == current or (current and r.get("session_id") == current)]
    if len(hits) > 1:
        exact = [r for r in hits if r.get("name") == current]
        if len(exact) == 1:
            hits = exact
    if not hits and target:
        sid = target.get("session_id") or ""
        if sid:
            hits = [r for r in rows if r.get("session_id") == sid]
        if not hits:
            row = {
                "name": current, "cwd": target.get("cwd") or "",
                "harness": target.get("harness") or target.get("kind") or "",
                "kind": target.get("kind") or "",
                "session_id": sid, "pane_id": target.get("pane_id") or "",
                "model": target.get("model") or "", "lane": target.get("lane") or "",
                "created": now(), "source": "rename",
            }
            rows.append(row)
            hits = [row]
    if len(hits) != 1:
        if not hits:
            raise SystemExit(f"cowbell: no session {current!r}")
        raise SystemExit(f"cowbell: ambiguous session {current!r}")
    row = hits[0]
    taken = {r.get("name") for r in rows if r is not row and r.get("name")}
    nm = unique_name(nm, taken)
    row["name"] = nm
    row["name_source"] = "pinned"
    save_roster(rows)
    pane = str(row.get("pane_id") or "")
    if pane and pane != "gone" and not pane.startswith("pid:"):
        report_identity(pane, nm, row.get("kind") or "", row.get("model") or "")
    return nm


def cmd_list(ctx: Ctx, all_rows: bool, cat: dict | None = None) -> None:
    cat = cat or load_catalog()
    rows = load_roster()
    ctx.live.refresh_sessions(rows)
    agents = [a for a in ctx.live.agents() if isinstance(a, dict)]
    panes = ctx.live.panes()
    try:
        snap = ctx.live.snapshot()
    except HerdrError:
        snap = {}
    labels = tab_labels(snap if isinstance(snap, dict) else {})
    scope = rows if all_rows else roster_here(rows, ctx.cwd)
    roots = {os.path.realpath(r.get("cwd") or ctx.cwd) for r in scope} or {os.path.realpath(ctx.cwd)}
    changed = False
    for root in roots:
        reg = build_registry(cat, root, agents, panes, roster_here(rows, root),
                             matcher=lambda _t: {"verdict": "unknown"}, tab_label=labels)
        if persist_canonical(reg, rows):
            changed = True
    if changed:
        save_roster(rows)
    by_pane = {a["pane_id"]: a for a in agents if a.get("pane_id")}
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
            place: str = "", spawn=None, pen: bool = False, split: bool = False, effort: str = "",
            dirs: list[str] | None = None) -> None:
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
    if unavailable_reason(h):
        raise SystemExit(f"cowbell: {unavailable_reason(h)}")
    lane = lane or harness_lane(h, cat)
    allow = allow or harness_tools(h)[0]
    deny = deny or harness_tools(h)[1]
    if lane and lane not in LANES:
        raise SystemExit(f"cowbell: lane must be one of {', '.join(LANES)}")
    model = model or (h["models"][0] if h["models"] else "")
    effort = effort or h.get("effort") or ""
    if effort and not h.get("effort_flag"):
        raise SystemExit(f"cowbell: {hid} has no effort_flag in the catalog; --effort has nowhere to go")
    dirs = parse_dirs(dirs or [])
    if dirs and not h.get("add_dir_flag"):
        raise SystemExit(f"cowbell: {hid} has no add_dir_flag in the catalog; --add-dir has nowhere to go")
    seat = None if (no_seat or not prompt_flag(h)) else seat_file(root)
    gate(h, root, interactive=False, assume_yes=assume_yes)
    base = slug(name or h["id"])
    nm = unique_name(base, live_names(ctx.live.snapshot()) | {r["name"] for r in load_roster()})
    pane = launch(cat, h, nm, root, ctx, model, lane, allow, deny, seat, extra=extra, focus=focus, spawn=spawn,
                  pen=pen, split=split, name_source="pinned" if name else "", effort=effort, dirs=dirs or None)
    print(f"raised {nm}  {h['id']}{(' ' + model) if model else ''}{(' ' + effort) if effort else ''}"
          f"{(' ' + lane) if lane else ''}  in {pane['pane_id']}  {tilde(root)}")


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
        egress = h.get("egress")
        egress = egress if isinstance(egress, str) else repr(egress)
        print(f"{(h.get('id') or '(missing id)'):<16} {h['kind']:<8} {GLYPH.get(egress, '?') + ' ' + egress:<7} "
              f"{('yes' if accepted(h) else 'unproven') if installed(h) else 'no':<9} "
              f"{trust_badge(h['trust_tool']):<10} {h.get('title', '')}")
    print(paint(LEGEND, FAINT))


KIND_PROBE = {  # argv that prints usage without launching a session
    "claude": ["--help"], "codex": ["--help"], "grok": ["--help"], "gemini": ["--help"],
    "agy": ["--help"], "opencode": ["--help"], "pi": ["--help"], "devin": ["--help"],
    "droid": ["--help"], "aider": ["--help"], "bash": ["--help"], "lab": ["--help"], "omp": ["--help"],
}


def declared_flags(h: dict) -> list[str]:
    """Every argv token the catalog declares for this harness — `*_flag` fields plus `resume`
    (which can be a subcommand, e.g. `codex resume`; it is still probed by name)."""
    out = []
    for k, v in h.items():
        if k == "resume" or k.endswith("_flag"):
            out += [v] if isinstance(v, str) else list(v or [])
    return [a for a in out if isinstance(a, str) and a and "{}" not in a]


def flag_probe(h: dict, run=subprocess.run) -> list | None:
    """Declared args the installed CLI's help does not mention. `None` = the probe could
    not run (absent binary, timeout) — unchecked, not a failure. Never launches a session."""
    exe = h.get("command") if is_wrapper(h) else h.get("kind")
    if not exe:
        return None
    argv = [exe] + KIND_PROBE.get(h.get("kind"), ["--help"])
    if len(argv) < 2:
        return None
    try:
        proc = run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    help_text = (proc.stdout or "") + (proc.stderr or "")
    return [a for a in declared_flags(h) if a not in help_text]


def cmd_accept(cat: dict, ids: list[str]) -> int:
    """`cowbell accept [id ...]`: run each row's check (default: every row that has one) and record it."""
    for i in ids:
        if not harness_by_id(cat, i):
            raise SystemExit(f"cowbell: unknown harness {i!r} (see: cowbell catalog)")
    rows = [harness_by_id(cat, i) for i in ids] if ids else [h for h in cat.get("harness", []) if h.get("check") and h.get("id")]
    if not rows:
        print("cowbell: no row has a check")
        return 0
    bad = 0
    for h in rows:
        if not h.get("check"):
            print(f"{h['id']:<20} no check (always offered)")
            continue
        ok, last = run_check(h)
        bad += not ok
        print(f"{h['id']:<20} {'pass' if ok else 'FAIL'}  {last}")
    return 1 if bad else 0


def cmd_catalog_check(cat: dict) -> int:
    """`cowbell catalog --check`: reject a malformed row by name, and probe
    each installed harness's help for its declared flags. A help miss is a `warn` (help
    docs lag real argv — codex `-m` vs `--model` is the shape), not a failure.
    Not-installed or unprobeable harnesses are reported, not failed."""
    problems, warns, unchecked = [], [], []
    # A file one schema rev ahead is a normal newer file (read, never rewritten; warn it);
    # further ahead or not an int is not a readable catalog — name it.
    schema = cat.get("schema", 1)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        problems.append(f"catalog schema {schema} must be a positive int")
    elif schema > SCHEMA + 1:
        problems.append(f"catalog is schema {schema}; cowbell {__version__} checks up to schema {SCHEMA + 1} (upgrade cowbell)")
    elif schema > SCHEMA:
        warns.append(f"catalog is schema {schema}; this cowbell writes schema {SCHEMA} (newer file: read, never rewritten)")
    ids = set()
    for h in cat.get("harness", []):
        hid = h.get("id") or "(missing id)"
        if not h.get("id"):
            problems.append("harness row with no id")
        elif hid in ids:
            problems.append(f"harness {hid}: duplicate id")
        ids.add(hid)
        if not h.get("kind"):
            problems.append(f"harness {hid}: missing kind")
        if h.get("egress") and (not isinstance(h["egress"], str) or h["egress"] not in GLYPH):
            problems.append(f"harness {hid}: unknown egress {h['egress']!r}")
        if h.get("lane") and h["lane"] not in ("green", "yellow", "red"):
            problems.append(f"harness {hid}: unknown lane {h['lane']!r}")
        if "models" in h and not isinstance(h["models"], list):
            problems.append(f"harness {hid}: models must be a list")
        ef = h.get("effort_flag")
        if ef is not None and not (isinstance(ef, list) and ef and all(isinstance(x, str) for x in ef)):
            problems.append(f"harness {hid}: effort_flag must be a list of strings")
        if "efforts" in h and not (isinstance(h["efforts"], list) and all(isinstance(x, str) for x in h["efforts"])):
            problems.append(f"harness {hid}: efforts must be a list of strings")
        adf = h.get("add_dir_flag")
        if adf is not None and not (isinstance(adf, str) and adf):
            problems.append(f"harness {hid}: add_dir_flag must be a flag string")
        if h.get("effort") and not ef:
            problems.append(f"harness {hid}: effort {h['effort']!r} set but no effort_flag")
        ck = h.get("check")
        if ck is not None and not is_argv(ck):
            problems.append(f"harness {hid}: check must be one argv, a list of strings")
        rq = h.get("requires")
        if rq is not None and not (isinstance(rq, list) and all(is_argv(a) for a in rq)):
            problems.append(f"harness {hid}: requires must be a list of argvs (lists of strings)")
        if not installed(h):
            unchecked.append(hid)
            continue
        missing = flag_probe(h)
        if missing is None:
            unchecked.append(hid)  # probe could not run
        elif missing:
            warns.append(f"harness {hid}: {', '.join(missing)} not in {h.get('kind')} help")
    for name, pl in (cat.get("places") or {}).items():
        for hid in pl.get("allow_harness") or []:
            if hid not in ids:
                problems.append(f"place {name}: allow_harness names unknown harness {hid!r}")
        dh = pl.get("default_harness")
        if dh and dh not in ids:
            problems.append(f"place {name}: default_harness names unknown harness {dh!r}")
    for p in problems:
        print(f"  FAIL {p}")
    for w in warns:
        print(f"  warn {w}")
    for hid in unchecked:
        print(f"  skip {hid}: not installed")
    if problems:
        n = len(problems)
        print(f"cowbell: check failed ({n} {'problem' if n == 1 else 'problems'})", file=sys.stderr)
        return 1
    print(f"  ok: {len(ids)} harnesses, {len(cat.get('places') or {})} places")
    return 0


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


# --- catalog writer ------------------------------------------------
# Splice [[harness]] blocks as text so comments and neighbouring tables stay.
# Block = [[harness]] through the next [[…]] / […] / EOF. tomllib only reads.

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_ASSIGN = re.compile(r'^[ \t]*id[ \t]*=[ \t]*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')')


def _table_header(line: str) -> str | None:
    """Return a normalised header (`[[harness]]`, `[places.x]`) or None."""
    s = line.split("\n", 1)[0].strip()
    if not s.startswith("["):
        return None
    end = s.rfind("]")
    if end < 0:
        return None
    tail = s[end + 1:].strip()
    if tail and not tail.startswith("#"):
        return None
    s = s[:end + 1].strip()
    if s.startswith("[[") and s.endswith("]]"):
        return f"[[{s[2:-2].strip()}]]"
    if s.startswith("[") and s.endswith("]"):
        return f"[{s[1:-1].strip()}]"
    return None


def _toml_dump_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        raise TypeError("cowbell: cannot write a null TOML value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_dump_value(x) for x in value) + "]"
    raise TypeError(f"cowbell: cannot write {type(value).__name__} as TOML")


def _parse_value_and_suffix(rest: str) -> tuple[str, str]:
    """Split `rest` (after `=`) into the value source and the trailing comment/newline."""
    i, n = 0, len(rest)
    while i < n and rest[i] in " \t":
        i += 1
    if i >= n:
        return "", rest
    start, ch = i, rest[i]
    if ch in "\"'":
        q = ch
        i += 1
        while i < n:
            if rest[i] == "\\" and q == '"':
                i += 2
                continue
            if rest[i] == q:
                i += 1
                break
            i += 1
    elif ch == "[":
        depth, in_str, q = 0, False, ""
        while i < n:
            c = rest[i]
            if in_str:
                if c == "\\" and q == '"':
                    i += 2
                    continue
                if c == q:
                    in_str = False
                i += 1
                continue
            if c in "\"'":
                in_str, q = True, c
                i += 1
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                i += 1
                if depth == 0:
                    break
                continue
            i += 1
    else:
        while i < n and rest[i] not in " \t#\n\r":
            i += 1
    return rest[start:i], rest[i:]


def _block_id(block: str) -> str:
    for ln in block.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        m = _ID_ASSIGN.match(ln)
        if m:
            raw = m.group(1)
            return bytes(raw, "utf-8").decode("unicode_escape").strip("\"'")
    return ""


def harness_blocks(text: str) -> list[dict]:
    """Each `[[harness]]` span: id, start, end (end exclusive), text."""
    lines = text.splitlines(keepends=True)
    spans, i, pos, n = [], 0, 0, len(lines)
    while i < n:
        hdr = _table_header(lines[i])
        if hdr == "[[harness]]":
            start = pos
            endpos = pos + len(lines[i])
            j = i + 1
            while j < n:
                if _table_header(lines[j]) is not None:
                    break
                endpos += len(lines[j])
                j += 1
            block = text[start:endpos]
            spans.append({"id": _block_id(block), "start": start, "end": endpos, "text": block})
            pos, i = endpos, j
            continue
        pos += len(lines[i])
        i += 1
    return spans


def _format_harness_block(h: dict) -> str:
    if not h.get("id"):
        raise SystemExit("cowbell: harness needs an id")
    keys = ["id"] + [k for k in h if k != "id"]
    lines = ["[[harness]]"]
    for k in keys:
        if not _BARE_KEY.match(k):
            raise SystemExit(f"cowbell: invalid harness field {k!r}")
        v = h[k]
        if v is None:
            continue
        lines.append(f"{k} = {_toml_dump_value(v)}")
    return "\n".join(lines) + "\n"


def _file_schema(path: Path) -> int:
    if tomllib is None:
        raise SystemExit("cowbell: python >= 3.11 required (tomllib)")
    try:
        with _open_regular(path) as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"cowbell: {path}: {_file_error(e)}")
    return data.get("schema", 1) if isinstance(data, dict) else 1


def _backup_timestamped(path: Path) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    n = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}.{n}")
        n += 1
    shutil.copy2(path, backup)
    return backup


def _snapshot_catalog(path: Path) -> None:
    """A second home for the written catalog: ~/.config is not synced."""
    try:
        d = state_dir() / "catalog"
        d.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = d / f"{path.name}.{stamp}"
        n = 2
        while out.exists():
            out = d / f"{path.name}.{stamp}.{n}"
            n += 1
        shutil.copy2(path, out)
    except OSError:
        pass


def _commit_catalog(path: Path, new_text: str) -> None:
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"cowbell: no catalog at {path}")
    check_writable(_file_schema(path), str(path))
    try:
        with _open_regular(path) as fh:
            old = fh.read().decode("utf-8")
    except (OSError, ValueError) as e:
        raise SystemExit(f"cowbell: {path}: {_file_error(e)}")
    if old == new_text:
        return
    _backup_timestamped(path)
    write_atomic(path, new_text)
    # NB: catalog_path() fills missing seed rows (it writes), so compare against config_dir instead.
    if os.path.realpath(path) == os.path.realpath(config_dir() / "harnesses.toml"):
        _snapshot_catalog(path)


def _catalog_text(path: Path | None) -> tuple[Path, str]:
    p = Path(path) if path is not None else catalog_path()
    if not p.is_file():
        raise SystemExit(f"cowbell: no catalog at {p}")
    try:
        with _open_regular(p) as fh:
            return p, fh.read().decode("utf-8")
    except (OSError, ValueError) as e:
        raise SystemExit(f"cowbell: {p}: {_file_error(e)}")


def _find_block(spans: list[dict], hid: str) -> dict | None:
    hits = [s for s in spans if s["id"] == hid]
    return hits[0] if hits else None


def _splice(text: str, start: int, end: int, block: str) -> str:
    return text[:start] + block + text[end:]


def harness_insert(harness: dict, path: Path | None = None) -> None:
    """Append a new `[[harness]]` after the last existing one."""
    p, text = _catalog_text(path)
    block = _format_harness_block(harness)
    hid = harness["id"]
    spans = harness_blocks(text)
    if _find_block(spans, hid):
        raise SystemExit(f"cowbell: harness {hid!r} already exists")
    if spans:
        at = spans[-1]["end"]
        new = _splice(text, at, at, block)
    else:
        pad = "" if not text or text.endswith("\n") else "\n"
        new = text + pad + block
    _commit_catalog(p, new)


def harness_replace(hid: str, harness: dict, path: Path | None = None) -> None:
    """Rewrite the `[[harness]]` block for `hid`. Comments inside that block are lost."""
    p, text = _catalog_text(path)
    spans = harness_blocks(text)
    span = _find_block(spans, hid)
    if span is None:
        raise SystemExit(f"cowbell: no harness {hid!r}")
    new_h = dict(harness)
    new_h.setdefault("id", hid)
    _commit_catalog(p, _splice(text, span["start"], span["end"], _format_harness_block(new_h)))


def harness_delete(hid: str, path: Path | None = None) -> None:
    """Remove the `[[harness]]` block for `hid`. Missing id is a no-op.
    Pin/hide entries for that id are dropped so the state file keeps no orphans."""
    p, text = _catalog_text(path)
    span = _find_block(harness_blocks(text), hid)
    if span is not None:
        _commit_catalog(p, _splice(text, span["start"], span["end"], ""))
    drop_harness_prefs(hid)


def _set_key_in_block(block: str, key: str, value) -> str:
    dumped = _toml_dump_value(value)
    lines = block.splitlines(keepends=True)
    key_re = re.compile(rf"^([ \t]*)({re.escape(key)})([ \t]*=[ \t]*)")
    last_assign = 0
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("#") or not stripped.strip():
            continue
        if _table_header(ln) == "[[harness]]":
            last_assign = i
            continue
        m = key_re.match(ln)
        if m:
            _, suffix = _parse_value_and_suffix(ln[m.end():])
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{dumped}{suffix}"
            return "".join(lines)
        if "=" in ln.split("#", 1)[0]:
            last_assign = i
    lines.insert(last_assign + 1, f"{key} = {dumped}\n")
    return "".join(lines)


def place_blocks(text: str) -> list[dict]:
    """Each `[places.*]` span: start, end (end exclusive), text."""
    lines = text.splitlines(keepends=True)
    spans, i, pos, n = [], 0, 0, len(lines)
    while i < n:
        hdr = _table_header(lines[i])
        if hdr and hdr.startswith("[places."):
            start = pos
            endpos = pos + len(lines[i])
            j = i + 1
            while j < n:
                if _table_header(lines[j]) is not None:
                    break
                endpos += len(lines[j])
                j += 1
            spans.append({"start": start, "end": endpos, "text": text[start:endpos]})
            pos, i = endpos, j
            continue
        pos += len(lines[i])
        i += 1
    return spans


def _rename_place_ids(old: str, new: str, path: Path | None = None) -> None:
    """Rewrite allow_harness and default_harness that name `old`. Comments stay."""
    p, text = _catalog_text(path)
    for span in reversed(place_blocks(text)):
        try:
            parsed = tomllib.loads(span["text"])
        except tomllib.TOMLDecodeError:
            continue
        places = parsed.get("places") or {}
        if not places:
            continue
        pl = next(iter(places.values()))
        block = span["text"]
        allow = list(pl.get("allow_harness") or [])
        if old in allow:
            block = _set_key_in_block(block, "allow_harness", [new if x == old else x for x in allow])
        if pl.get("default_harness") == old:
            block = _set_key_in_block(block, "default_harness", new)
        if block != span["text"]:
            text = text[:span["start"]] + block + text[span["end"]:]
    _commit_catalog(p, text)


def harness_set_field(hid: str, key: str, value, path: Path | None = None) -> None:
    """Set one field inside the `[[harness]]` block for `hid`, keeping comments."""
    if not _BARE_KEY.match(key):
        raise SystemExit(f"cowbell: invalid harness field {key!r}")
    p, text = _catalog_text(path)
    span = _find_block(harness_blocks(text), hid)
    if span is None:
        raise SystemExit(f"cowbell: no harness {hid!r}")
    if key == "id":
        new_id = value if isinstance(value, str) else str(value)
        if new_id != hid and _find_block(harness_blocks(text), new_id):
            raise SystemExit(f"cowbell: harness {new_id!r} already exists")
    new_block = _set_key_in_block(span["text"], key, value)
    _commit_catalog(p, _splice(text, span["start"], span["end"], new_block))


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
        # TCSADRAIN, not TCSAFLUSH: a key typed while a prompt is closing must
        # still be there when the picker reads again.
        tty.setraw(self.fd, termios.TCSADRAIN)
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


def hint_bar(hints: list[tuple[str, str, str]], width: int | None = None) -> tuple[str, list[tuple[int, int, str]]]:
    """[(key label, words, event)] -> (painted line, clickable [x0, x1) spans, 1-based columns).

    Given a `width`, a bar that would not fit sheds words right to left (the keys stay),
    then whole hints right to left, keeping the first and the last (esc)."""
    hints = list(hints)
    size = lambda hs: 2 + sum(len(l) + (1 + len(w) if w else 0) for l, w, _ in hs) + 3 * (len(hs) - 1)
    if width:
        for i in range(len(hints) - 1, -1, -1):
            if size(hints) <= width:
                break
            hints[i] = (hints[i][0], "", hints[i][2])
        while size(hints) > width and len(hints) > 2:
            del hints[-2]
    parts, spans, col = [], [], 3
    for label, words, ev in hints:
        plain = label + (" " + words if words else "")
        parts.append(paint(label, MUTED) + (" " + paint(words, FAINT) if words else ""))
        spans.append((col, col + len(plain), ev))
        col += len(plain) + 3
    return "  " + "   ".join(parts), spans


_MENU_PENDING: list = []  # events after a terminating key, saved for the next menu() call


def menu(body: list[str], rows: list[int], hints, selected: int = 0,
         hotkeys: str = "", flash: str = "", line_keys: dict[int, str] | None = None,
         numbered: list[int] | None = None, scr: Screen | None = None, tick: float | None = None,
         foot: list[str] | None = None) -> tuple:
    """Pick one of `rows` (line indexes into body).

    `foot` lines are pinned above the hint bar and never scroll with the body.

    Returns ("pick", i), ("key", ch, i) for a hotkey, clicked hint or clicked `line_keys` line, ("cancel",),
    or ("tick", i) after `tick` idle seconds so the caller can refresh (pass `scr` to keep the screen up).
    `numbered[n-1]` is the row a typed digit n selects (default: rows in order).
    Click or arrows / j k / wheel select; enter (or the ⏎ button) confirms; digits jump (and pick when
    unambiguous); esc or q cancels. Buttons and rows answer the press, so a popup host that forwards
    only presses still works; releases are ignored.
    `hints` is `[(label, words, event), ...]` or a callable `selected ->` that list, so the bar can
    follow the highlighted row.
    """
    selected = max(0, min(selected, len(rows) - 1))
    by_line = {ln: i for i, ln in enumerate(rows)}
    numbered = list(range(len(rows))) if numbered is None else numbered
    digits, top = "", 0
    flash = ((flash or "").splitlines() or [""])[0]
    own = scr is None
    scr = (scr or Screen()).open()
    try:
        while True:
            cols, height = shutil.get_terminal_size((100, 24))
            bar, spans = hint_bar(hints(selected) if callable(hints) else hints, cols - 1)
            pinned = ([""] + list(foot)) if foot else []
            foot_ = pinned + ["", bar] + ([paint("  " + flash, RED)] if flash else [])
            view = max(3, height - len(foot_))
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
            bar_y = view + len(pinned) + 2  # 1-based screen row of the hint bar
            scr.draw(shown + foot_)
            # fast-typed keys arrive batched; leftovers carry to the next menu() call
            evs = _MENU_PENDING if _MENU_PENDING else scr.events(tick)
            _MENU_PENDING.clear()
            if tick is not None and not evs:
                return ("tick", selected)

            def done(res):
                _MENU_PENDING.extend(evs[i + 1:])
                return res

            for i, ev in enumerate(evs):
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
                                    return done(("cancel",))
                                if hit == "enter":
                                    if rows:
                                        return done(("pick", selected))
                                    continue
                                return done(("key", hit, selected))
                        elif line in (line_keys or {}) and y <= view:
                            return done(("key", line_keys[line], selected))
                        elif line in by_line and y <= view:
                            selected = by_line[line]
                    continue
                kind, val = ev
                if kind == "key":
                    if val == "esc":
                        return done(("cancel",))
                    if val == "enter":
                        if rows:
                            return done(("pick", selected))
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
                            return done(("pick", selected))
                elif val in ("j", "k"):
                    selected = max(0, min(len(rows) - 1, selected + (1 if val == "j" else -1)))
                elif val == "q":
                    return done(("cancel",))
                elif val.lower() in hotkeys:
                    return done(("key", val.lower(), selected))
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
    buf, tail = default, b""
    tty.setraw(fd, termios.TCSADRAIN)
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


def choose(title: str, items: list[str], default: int = 0, intro: str = "", mark: str = "default") -> int | None:
    """Numbered choice: the menu on a terminal (click, arrows, enter, esc), typed numbers otherwise.

    `mark` labels the preselected item: "default" where it is one, "current" where it is the value now set.
    """
    if not items:
        return None
    if tui_ok():
        body = ([f"  {intro}", ""] if intro else []) + [section(title.upper())]
        rows = []
        for i, s_ in enumerate(items):
            rows.append(len(body))
            body.append(f"  {key(i + 1)}  {s_}{paint('   ' + mark, FAINT) if i == default and mark else ''}")
        res = menu([""] + body, [r + 1 for r in rows],
                   [("↑↓", "move", ""), ("⏎", "pick", "enter"), ("esc", "cancel", "esc")], selected=default)
        return res[1] if res[0] == "pick" else None
    print(f"  {title}")
    for i, s in enumerate(items):
        print(f"  {i + 1:>3}  {s}{f'   ({mark})' if i == default and mark else ''}")
    raw = ask("  number (empty = default, q = cancel): ", str(default + 1))
    if raw.lower() == "q":
        return None
    try:
        i = int(raw) - 1
    except ValueError:
        return None
    return i if 0 <= i < len(items) else None


def parse_model_list(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def harness_lane(h: dict, cat: dict) -> str:
    """A wrapper uses its script's own permissions unless this row sets `lane`."""
    if h.get("lane"):
        return h["lane"]
    if is_wrapper(h):
        return ""
    return cat.get("defaults", {}).get("lane", "yellow") or ""


def harness_tools(h: dict) -> tuple[str, str]:
    return (h.get("tools_allow") or "", h.get("tools_deny") or "")


def models_with_default(models: list[str], default: str) -> list[str]:
    models = [m for m in models if m]
    if not default:
        return models
    return [default] + [m for m in models if m != default]


def harness_preset(src: dict, hid: str, title: str, model: str, lane: str,
                   allow: str, deny: str, effort: str = "") -> dict:
    """A new [[harness]] copied whole from `src`, with this sitting's model / lane / tools /
    effort as its defaults. Every other key rides along (seat and name flags, unix_user,
    provider), so the saved agent launches the way its source does; a list of keys to
    keep would drop the next field a harness gains."""
    sheet_keys = {"id", "title", "models", "lane", "tools_allow", "tools_deny", "effort"}
    out = {"id": hid, "kind": src.get("kind") or hid}
    out.update({k: v for k, v in src.items() if k not in sheet_keys | {"kind"} and v not in (None, "", [])})
    models = models_with_default(list(src.get("models") or []), model)
    if models:
        out["models"] = models
    if title:
        out["title"] = title
    if effort and out.get("effort_flag"):
        out["effort"] = effort
    if lane:
        out["lane"] = lane
    if allow:
        out["tools_allow"] = allow
    if deny:
        out["tools_deny"] = deny
    return out


def offer_save_as(src: dict, model: str, lane: str, allow: str, deny: str, effort: str = "") -> str:
    """Ask for a new harness id and write it. Empty skip or Esc writes nothing. Returns the
    id, or "". An id already in the catalog raises SystemExit."""
    try:
        raw = ask("  save as harness id (empty skip): ", "")
    except KeyboardInterrupt:
        return ""
    if not (raw or "").strip():
        return ""
    hid = slug(raw)
    title = (src.get("title") or src.get("id") or hid) + (f" · {model}" if model else "")
    harness_insert(harness_preset(src, hid, title, model, lane, allow, deny, effort))
    return hid


def _lane_rows(cat: dict, h: dict) -> list[str]:
    return [f"{paint(f'{ln:<8}', SECONDARY)} "
            f"{paint(' '.join(cat.get('lanes', {}).get(h.get('kind', ''), {}).get(ln, [])) or 'agent default', MUTED)}"
            for ln in LANES]


def _ask_tools(h: dict, *, even_empty: bool = False) -> tuple[str, str]:
    """Prompt for tools. The launch path skips empty boxes."""
    allow, deny = harness_tools(h)
    if is_wrapper(h):
        return "", ""
    if not even_empty and not allow and not deny:
        return "", ""
    if h.get("tools_allow_flag"):
        allow = ask("  tools allow (empty skip): ", allow)
    if h.get("tools_deny_flag"):
        deny = ask("  tools deny (empty skip): ", deny)
    return allow, deny


def model_menu_items(h: dict) -> list[str]:
    """Model choices for this sitting. Empty means the harness has no model_flag.
    A long list gives the first ten; "(type another)" takes the rest by hand."""
    if not h.get("model_flag"):
        return []
    models = list(h.get("models") or [])
    if models:
        return models[:10] + ["(type another)"]
    return ["(agent default)", "(type another)"]


def _edit_row(label: str, value: str, val_color: str = "") -> str:
    shown = value if value else "—"
    val_c = val_color or (MUTED if value else FAINT)
    return f"{paint(f'{label:<16}', SECONDARY)} {paint(shown, val_c)}"


def display_command(path: str) -> str:
    """Show a launcher path with a tilde. The catalog keeps that form."""
    path = path or ""
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


# A kind's flags, used when a row has not named them yet. Verified against the CLIs.
_KIND_MODEL_FLAG = {"pi": ["--model"], "claude": ["--model"], "codex": ["--model"], "grok": ["-m"]}
_KIND_TOOL_FLAGS = {
    "pi": ("--tools", "--exclude-tools"),
    "claude": ("--allowedTools", "--disallowedTools"),
    "grok": ("--allowedTools", "--disallowed-tools"),
}
# Selectable values for fields that used to be a blank line. "(none)" clears.
# Model ids match the seed. Claude and pi tool names match those CLIs' own docs
# and the seed lanes. Kinds without a list still get none and type-another.
_NONE = "(none)"
_TYPE = "(type another)"
_PROBE = "(probe installed)"
_KIND_MODELS = {
    "claude": ["claude-sonnet-5", "claude-opus-5", "claude-fable-5-1", "claude-haiku-4-5-20251001"],
    "grok": ["grok-4.6", "grok-4.7", "grok-4.7-build-fast", "grok-4.5"],
}
_PROVIDERS = ["ollama", "openai", "anthropic", "google", "xai", "openrouter"]
_KIND_ALLOW = {
    "claude": ["Read", "Read,Grep,Glob", "Read,Edit,Grep,Glob", "Read,Edit,Write,Bash,Grep,Glob"],
    "pi": ["read", "read,edit,write"],
}
_KIND_DENY = {
    "claude": ["Bash", "WebFetch,WebSearch", "Bash,WebFetch,WebSearch"],
    "pi": ["bash"],
}


def probe_installed_models(h: dict, run=subprocess.run) -> list[str]:
    """Find models actually available locally for this harness kind/CLI."""
    found: list[str] = []
    kind = h.get("kind") or ""
    provider = h.get("provider") or ""

    # 1. Ollama CLI & local server
    if kind == "ollama" or provider == "ollama":
        if shutil.which("ollama"):
            try:
                res = run(["ollama", "list"], capture_output=True, text=True, timeout=3)
                if res.returncode == 0:
                    for line in res.stdout.splitlines()[1:]:
                        parts = line.strip().split()
                        if parts:
                            found.append(parts[0])
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not found:
            try:
                import urllib.request
                import json
                with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=0.8) as resp:
                    data = json.loads(resp.read().decode())
                    for m in data.get("models", []):
                        if m.get("name"):
                            found.append(m["name"])
            except Exception:
                pass

    # 2. Codex
    if kind == "codex" and shutil.which("codex"):
        try:
            res = run(["codex", "--help"], capture_output=True, text=True, timeout=3)
            text = (res.stdout or "") + (res.stderr or "")
            for m in re.findall(r"(?:gpt-4o|o1|o3-mini|claude-[\w.-]+)", text):
                found.append(m)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 3. Generic CLI --help scan if not found yet
    if not found:
        exe = h.get("command") if is_wrapper(h) else h.get("kind")
        if exe and shutil.which(os.path.expanduser(exe)):
            try:
                res = run([os.path.expanduser(exe), "--help"], capture_output=True, text=True, timeout=3)
                text = (res.stdout or "") + (res.stderr or "")
                matches = re.findall(r"(?:claude-[\w.-]+|gpt-[\w.-]+|grok-[\w.-]+|llama[\w.:-]+)", text)
                found.extend(matches)
            except (OSError, subprocess.TimeoutExpired):
                pass

    return list(dict.fromkeys(found))


def _preset_choice(title: str, presets: list[str], current: str, intro: str = "",
                   probe_fn=None) -> str | None:
    """Pick a known value, clear it, or type one. None means the choice was cancelled."""
    options = list(dict.fromkeys(x for x in presets if x))
    items = options + [_NONE, _TYPE]
    if probe_fn:
        items.append(_PROBE)
    if current and current in options:
        default = options.index(current)
    elif not current:
        default = len(options)
    else:
        default = len(options) + 1
    j = choose(title, items, default=default, intro=intro)
    if j is None:
        return None
    if items[j] == _NONE:
        return ""
    if items[j] == _TYPE:
        return ask(f"  {title}: ", current)
    if probe_fn and items[j] == _PROBE:
        probed = probe_fn()
        if not probed:
            print(f"  no installed models detected for {title}")
            return _preset_choice(title, presets, current, intro=intro, probe_fn=probe_fn)
        new_presets = list(dict.fromkeys(presets + probed))
        return _preset_choice(title, new_presets, current, intro=intro, probe_fn=None)
    return options[j]


def harness_edit_rows(cat: dict, h: dict) -> list[tuple[str, str]]:
    """Catalog fields `e` can change, in menu order. (key, rendered row).

    Every harness gets the same controls. A launcher script also lists its command.
    Empty models, permissions, and tools stay on the menu so they can be set.
    """
    models = list(h.get("models") or [])
    lane = harness_lane(h, cat)
    lane_c = {"green": GREEN, "yellow": GOLD, "red": RED}.get(lane, MUTED)
    rows = [
        ("name", _edit_row("name", h.get("id") or "")),
        ("description", _edit_row("description", h.get("title") or "")),
    ]
    if is_wrapper(h):
        rows.append(("command", _edit_row("command", display_command(h.get("command") or ""))))
    rows += [
        ("models", _edit_row("models", ", ".join(models))),
        ("default", _edit_row("default model", models[0] if models else "")),
        ("permissions", _edit_row("permissions", lane, val_color=lane_c)),
        ("allow", _edit_row("tools allow", h.get("tools_allow") or "")),
        ("deny", _edit_row("tools deny", h.get("tools_deny") or "")),
        ("provider", _edit_row("provider", h.get("provider") or "")),
        ("cycle", _edit_row("model cycle", " ".join(h.get("cycle_flag") or []))),
        ("prompt", _edit_row("system prompt", h.get("system_prompt") or "")),
        ("user", _edit_row("unix user", h.get("unix_user") or "")),
        ("preview", _edit_row("launch preview", "inspect argv & confinement", val_color=BLUE)),
        ("clone", _edit_row("clone harness", "duplicate to new preset", val_color=GOLD)),
        ("verify", _edit_row("verify harness", "check binary, flags & tools", val_color=GREEN)),
    ]
    return rows
    return rows


def _ensure_model_flag(h: dict) -> None:
    if h.get("model_flag"):
        return
    flag = _KIND_MODEL_FLAG.get(h.get("kind") or "")
    if flag:
        harness_set_field(h["id"], "model_flag", flag)


def _ensure_tool_flag(h: dict, which: str) -> None:
    key = "tools_allow_flag" if which == "allow" else "tools_deny_flag"
    if h.get(key):
        return
    pair = _KIND_TOOL_FLAGS.get(h.get("kind") or "")
    if not pair:
        return
    harness_set_field(h["id"], key, pair[0] if which == "allow" else pair[1])


def _rename_harness(old: str) -> str | None:
    """Edit the harness id. Esc or an unchanged name writes nothing. Returns the new id."""
    raw = ask("  name: ", old)
    if not (raw or "").strip():
        return None
    new = slug(raw)
    if new == old:
        return None
    harness_set_field(old, "id", new)
    rename_harness_refs(old, new)
    _rename_place_ids(old, new)
    return new


def _edit_harness_field(cat: dict, h: dict, key: str) -> bool:
    """Write one catalog field. A cancelled choice writes nothing."""
    hid = h["id"]
    intro = paint(hid, INK, bold=True)
    if key == "description":
        harness_set_field(hid, "title", ask("  description: ", h.get("title") or ""))
        return True
    if key == "command":
        raw = ask("  command: ", display_command(h.get("command") or ""))
        if not (raw or "").strip():
            return False
        harness_set_field(hid, "command", display_command(os.path.expanduser(raw.strip())))
        return True
    if key == "models":
        current = ", ".join(h.get("models") or [])
        presets = list(_KIND_MODELS.get(h.get("kind") or "", []))
        for model in h.get("models") or []:
            if model not in presets:
                presets.append(model)
        picked = _preset_choice("models", presets, current, intro=intro,
                                probe_fn=lambda: probe_installed_models(h))
        if picked is None or picked == current:
            return False
        if not picked:
            models = []
        elif picked in presets:
            models = [picked]
        else:
            models = parse_model_list(picked)
        cur = (h.get("models") or [None])[0]
        if cur and cur in models:
            models = models_with_default(models, cur)
        if models:
            _ensure_model_flag(h)
        harness_set_field(hid, "models", models)
        return True
    if key == "default":
        models = list(h.get("models") or [])
        presets = models or list(_KIND_MODELS.get(h.get("kind") or "", []))
        if not presets:
            probed = probe_installed_models(h)
            if probed:
                presets = probed
        if not presets:
            name = (ask("  default model: ", "") or "").strip()
            if not name:
                return False
            _ensure_model_flag(h)
            harness_set_field(hid, "models", [name])
            return True
        picked = _preset_choice("default model", presets, models[0] if models else "", intro=intro,
                                probe_fn=lambda: probe_installed_models(h))
        if picked is None or not picked:
            return False
        if picked not in presets:
            models = models_with_default(models, picked) if models else [picked]
        else:
            models = models_with_default(models or presets, picked)
        _ensure_model_flag(h)
        harness_set_field(hid, "models", models)
        return True
    if key == "permissions":
        default_lane = harness_lane(h, cat) or "yellow"
        j = choose("default permissions", _lane_rows(cat, h),
                   default=LANES.index(default_lane) if default_lane in LANES else 1,
                   intro=paint(hid, INK, bold=True))
        if j is None:
            return False
        harness_set_field(hid, "lane", LANES[j])
        return True
    if key == "allow":
        cur = h.get("tools_allow") or ""
        val = _preset_choice("tools allow", list(_KIND_ALLOW.get(h.get("kind") or "", [])), cur, intro=intro)
        if val is None or val == cur:
            return False
        if val:
            _ensure_tool_flag(h, "allow")
        harness_set_field(hid, "tools_allow", val)
        return True
    if key == "deny":
        cur = h.get("tools_deny") or ""
        val = _preset_choice("tools deny", list(_KIND_DENY.get(h.get("kind") or "", [])), cur, intro=intro)
        if val is None or val == cur:
            return False
        if val:
            _ensure_tool_flag(h, "deny")
        harness_set_field(hid, "tools_deny", val)
        return True
    if key == "provider":
        cur = h.get("provider") or ""
        val = _preset_choice("provider", _PROVIDERS, cur, intro=intro)
        if val is None or val == cur:
            return False
        harness_set_field(hid, "provider", val)
        if val and not h.get("provider_flag") and (h.get("kind") or "") == "pi":
            harness_set_field(hid, "provider_flag", ["--provider"])
        return True
    if key == "cycle":
        if (h.get("kind") or "") == "pi":
            harness_set_field(hid, "cycle_flag", [] if h.get("cycle_flag") else ["--models"])
            return True
        cur = " ".join(h.get("cycle_flag") or [])
        val = _preset_choice("model cycle", ["--models"], cur, intro=intro)
        if val is None or val == cur:
            return False
        harness_set_field(hid, "cycle_flag", [val] if val else [])
        return True
    if key == "prompt":
        cur = h.get("system_prompt") or ""
        val = _preset_choice("system prompt", [], cur, intro=intro)
        if val is None or val == cur:
            return False
        harness_set_field(hid, "system_prompt", val)
        if val and not h.get("system_prompt_flag") and (h.get("kind") or "") == "pi":
            harness_set_field(hid, "system_prompt_flag", "--append-system-prompt")
        return True
    if key == "user":
        cur = h.get("unix_user") or ""
        presets: list[str] = []
        for pl in (cat.get("places") or {}).values():
            u = (pl.get("unix_user") or "").strip()
            if u and u not in presets:
                presets.append(u)
        def_u = ((cat.get("defaults") or {}).get("unix_user") or "").strip()
        if def_u and def_u not in presets:
            presets.append(def_u)
        val = _preset_choice("unix user", presets, cur, intro=intro)
        if val is None or val == cur:
            return False
        harness_set_field(hid, "unix_user", val)
        return True
    return False


def preview_harness_launch(cat: dict, h: dict, root: str = "") -> None:
    """Display the exact command line, environment, and confinement cowbell will use."""
    root = root or os.getcwd()
    models = list(h.get("models") or [])
    model = models[0] if models else ""
    lane = harness_lane(h, cat)
    allow = h.get("tools_allow") or ""
    deny = h.get("tools_deny") or ""
    cmd, session = here_command(cat, h, name="preview-agent", model=model, lane=lane,
                                allow=allow, deny=deny, seat=None, resume_id="")
    confined = unix_confine(cat, root, list(cmd), h=h)

    lane_c = {"green": GREEN, "yellow": GOLD, "red": RED}.get(lane, MUTED)
    user_str = h.get("unix_user") or (place_for(cat, root) or {}).get("unix_user") or "(current user)"

    print()
    print(f"  {paint('Launch Preview', INK, bold=True)}  {paint(h.get('id') or '', TEAL)}")
    print(f"  {paint('─' * 52, FAINT)}")
    print(f"  {paint('harness:', SECONDARY):<18} {paint(h.get('id', ''), INK)} {paint('(' + (h.get('kind') or '') + ')', FAINT)}")
    print(f"  {paint('directory:', SECONDARY):<18} {paint(tilde(root), TEAL)}")
    print(f"  {paint('unix user:', SECONDARY):<18} {paint(user_str, GOLD if h.get('unix_user') else MUTED)}")
    print(f"  {paint('permissions:', SECONDARY):<18} {paint(lane, lane_c, bold=True)}")
    if model:
        print(f"  {paint('model:', SECONDARY):<18} {paint(model, INK)}")
    if allow:
        print(f"  {paint('tools allow:', SECONDARY):<18} {paint(allow, MUTED)}")
    if deny:
        print(f"  {paint('tools deny:', SECONDARY):<18} {paint(deny, MUTED)}")
    print(f"  {paint('session id:', SECONDARY):<18} {paint(session, FAINT)}")
    print(f"  {paint('command argv:', SECONDARY):<18}")
    print(f"    {paint(shell_join(confined), GOLD)}")
    print(f"  {paint('─' * 52, FAINT)}")
    try:
        ask("  press Enter to return", "")
    except (EOFError, KeyboardInterrupt):
        pass
    print()


def clone_harness(cat: dict, h: dict) -> str | None:
    """Duplicate this harness preset into a new catalog entry."""
    old_id = h.get("id") or ""
    raw = ask(f"  new harness id [{old_id}-copy]: ", f"{old_id}-copy")
    if not (raw or "").strip():
        return None
    new_id = slug(raw.strip())
    if not new_id or new_id == old_id:
        return None
    if harness_by_id(cat, new_id):
        print(f"  harness {new_id!r} already exists")
        return None
    old_title = h.get("title") or old_id
    new_title = ask("  description: ", f"{old_title} (copy)")
    new_rec = {k: v for k, v in h.items() if k not in ("id", "title") and _BARE_KEY.match(k)}
    new_rec["id"] = new_id
    new_rec["title"] = new_title or f"{old_title} (copy)"
    harness_insert(new_rec)
    print(f"  cloned {old_id} -> {new_id}")
    return new_id


def verify_harness_interactive(cat: dict, h: dict) -> None:
    """Run an interactive sanity check on binary, flags, permissions, and unix user."""
    hid = h.get("id") or ""
    print()
    print(f"  {paint('Verify Harness', INK, bold=True)}  {paint(hid, TEAL)}")
    print(f"  {paint('─' * 52, FAINT)}")

    target = h.get("command") if is_wrapper(h) else h.get("kind")
    if not target:
        print(f"  {paint('FAIL', RED, bold=True)}  target executable/kind is not specified")
    else:
        expanded = os.path.expanduser(target)
        exe = expanded if os.path.isabs(expanded) and os.path.exists(expanded) else shutil.which(target)
        if exe:
            print(f"  {paint(' OK ', GREEN, bold=True)}  binary: {paint(exe, INK)}")
        else:
            print(f"  {paint('FAIL', RED, bold=True)}  binary not found on PATH: {paint(target, GOLD)}")

    u = (h.get("unix_user") or "").strip()
    if u:
        import pwd
        try:
            pw = pwd.getpwnam(u)
            helper = shutil.which("agent-run")
            helper_str = f"via {helper}" if helper else "agent-run not on PATH (no sudo fallback)"
            h_color = GREEN if helper else RED
            print(f"  {paint(' OK ' if helper else 'WARN', h_color, bold=True)}  unix user: {paint(u, INK)} (uid {pw.pw_uid}, {helper_str})")
        except KeyError:
            print(f"  {paint('FAIL', RED, bold=True)}  unix user {u!r} does not exist on this host")
    else:
        print(f"  {paint('INFO', FAINT)}  unix user: none specified (runs as current user)")

    models = list(h.get("models") or [])
    if models:
        mflag = h.get("model_flag") or _KIND_MODEL_FLAG.get(h.get("kind") or "")
        if mflag:
            print(f"  {paint(' OK ', GREEN, bold=True)}  model flag: {paint(' '.join(mflag) if isinstance(mflag, list) else str(mflag), INK)} ({len(models)} models)")
        else:
            print(f"  {paint('WARN', GOLD, bold=True)}  models defined ({len(models)}) but no model_flag set")
    else:
        print(f"  {paint('INFO', FAINT)}  models: none specified (uses agent default)")

    allow = h.get("tools_allow")
    allow_flag = h.get("tools_allow_flag") or (_KIND_TOOL_FLAGS.get(h.get("kind") or "") or (None, None))[0]
    if allow:
        if allow_flag:
            print(f"  {paint(' OK ', GREEN, bold=True)}  tools allow flag: {paint(str(allow_flag), INK)}")
        else:
            print(f"  {paint('WARN', GOLD, bold=True)}  tools_allow set but tools_allow_flag missing")

    deny = h.get("tools_deny")
    deny_flag = h.get("tools_deny_flag") or (_KIND_TOOL_FLAGS.get(h.get("kind") or "") or (None, None))[1]
    if deny:
        if deny_flag:
            print(f"  {paint(' OK ', GREEN, bold=True)}  tools deny flag: {paint(str(deny_flag), INK)}")
        else:
            print(f"  {paint('WARN', GOLD, bold=True)}  tools_deny set but tools_deny_flag missing")

    misses = flag_probe(h)
    if misses is None:
        print(f"  {paint('INFO', FAINT)}  help probe: CLI could not be queried or timed out")
    elif misses:
        print(f"  {paint('WARN', GOLD, bold=True)}  flags not found in CLI --help: {paint(', '.join(misses), GOLD)}")
    else:
        print(f"  {paint(' OK ', GREEN, bold=True)}  all declared flags verified in CLI help")

    print(f"  {paint('─' * 52, FAINT)}")
    try:
        ask("  press Enter to return", "")
    except (EOFError, KeyboardInterrupt):
        pass
    print()


def edit_harness(cat: dict, hid: str) -> bool:
    """Menu of catalog fields for one harness. Picker `e`.

    Esc on the menu returns to the list. Esc inside a field cancels that field.
    `cat` is unused; each pass reloads the catalog so the rows show what was just written.
    """
    del cat
    wrote = False
    while True:
        fresh = load_catalog()
        h = harness_by_id(fresh, hid)
        if not h:
            print(f"  unknown harness {hid}")
            return wrote
        spec = harness_edit_rows(fresh, h)
        lane = harness_lane(h, fresh)
        lane_c = {"green": GREEN, "yellow": GOLD, "red": RED}.get(lane, MUTED)
        intro = f"{paint(hid, INK, bold=True)}  {paint('[' + (h.get('kind') or 'agent') + ']', SECONDARY)}  {paint(lane, lane_c)}  {paint(h.get('title') or '', FAINT)}"
        j = choose("edit", [row for _, row in spec], intro=intro)
        if j is None:
            return wrote
        try:
            key = spec[j][0]
            if key == "name":
                new = _rename_harness(hid)
                if new:
                    hid = new
                    wrote = True
            elif key == "clone":
                new = clone_harness(fresh, h)
                if new:
                    hid = new
                    wrote = True
            elif key == "preview":
                preview_harness_launch(fresh, h)
            elif key == "verify":
                verify_harness_interactive(fresh, h)
            elif _edit_harness_field(fresh, h, key):
                wrote = True
        except KeyboardInterrupt:
            continue
        except SystemExit as e:
            print(f"  {e}")
            continue


def add_harness(root: str, cat: dict) -> bool:
    """Add from a herdr kind or a launcher script; unhide if this directory hid any."""
    hidden = list(dir_prefs(root).get("hide") or [])
    opts = ["herdr agent kind", "launcher script"] + (["unhide a hidden harness"] if hidden else [])
    i = choose("add", opts)
    if i is None:
        return False
    if hidden and i == 2:
        j = choose("unhide", hidden)
        if j is None:
            return False
        clear_pref(root, hidden[j])
        print(f"  unhid {hidden[j]}")
        return True
    if i == 0:
        kinds = sorted(AGENT_EXES)
        j = choose("kind", kinds)
        if j is None:
            return False
        kind = kinds[j]
        hid = slug(ask(f"  id [{kind}]: ", kind))
        title = ask("  description: ", kind)
        models = parse_model_list(ask("  models (comma-separated, empty skip): ", ""))
        rec: dict = {"id": hid, "kind": kind, "title": title}
        if models:
            rec["models"] = models
        harness_insert(rec)
        print(f"  added {hid}")
        return True
    path = ask("  launcher path: ", "")
    if not (path or "").strip():
        return False
    path = os.path.expanduser(path.strip())
    base = slug(Path(path).stem)
    hid = slug(ask(f"  id [{base}]: ", base))
    title = ask("  description: ", hid)
    kinds = sorted(AGENT_EXES)
    kind_default = Path(path).stem if Path(path).stem in AGENT_EXES else "pi"
    j = choose("kind", kinds, default=kinds.index(kind_default) if kind_default in kinds else 0)
    if j is None:
        return False
    harness_insert({"id": hid, "kind": kinds[j], "command": path, "title": title})
    print(f"  added {hid}")
    return True


def remove_harness(hid: str) -> bool:
    j = choose("remove", [f"remove {hid} from the catalog", "keep"], default=1)
    if j != 0:
        return False
    harness_delete(hid)
    print(f"  removed {hid}")
    return True


def toggle_pin(root: str, hid: str) -> str:
    if hid in dir_prefs(root).get("pin", []):
        clear_pref(root, hid)
        return f"unpinned {hid}"
    pin_harness(root, hid)
    return f"pinned {hid}"


def apply_hide(root: str, hid: str) -> str:
    hide_harness(root, hid)
    return f"hidden {hid}"


def picker_hints(can_widen: bool, start_row: bool, resume_row: bool = False) -> list[tuple[str, str, str]]:
    """Hint bar for the picker. o on Start new and Resume rows; e / p / h / − only on Start new; r only on Resume."""
    crowded = start_row  # the row actions fill the bar; ⏎ keeps its key, not its word
    hints = [("⏎", "" if crowded else "open", "enter"),
             ("n", "new", "n"),
             ("+", "add", "+"),
             ("a", "adopt", "a")]
    if can_widen and not start_row:  # the header's [g] widen is clickable too
        hints.append(("g", "widen", "g"))
    if resume_row and not start_row:
        hints += [("o", "shape", "o"), ("r", "rename", "r")]
    if start_row:
        hints += [("o", "shape", "o"), ("e", "edit", "e"), ("p", "pin", "p"), ("h", "hide", "h"), ("-", "rm", "-")]
    hints.append(("esc", "" if start_row else "close", "esc"))
    return hints


# --- launch sheet ----------------------------------------------------------------
# One screen for one launch: every setting the harness takes, its value, and the
# command that will run. A setting left at the harness default is dim; one this
# launch changed is ink. `n`, and `e` on a New Session row, open it.

@dataclass
class LaunchSpec:
    """How one sitting starts. The roster row's launch fields, with a name.

    A field an older roster row lacks is the harness default, never an error.
    `extra` is argv passed through after `--`; the roster keeps it so a relaunch
    from this record does not drop it.
    """
    harness: str
    name: str = ""
    model: str = ""
    effort: str = ""
    lane: str = ""
    allow: str = ""
    deny: str = ""
    seat: str = ""
    resume_id: str = ""
    extra: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)

    def seat_path(self) -> Path | None:
        return Path(self.seat) if self.seat else None

    def launch_args(self) -> tuple:
        """Positional args for launch(), raise_harness() and exec_here() after ctx."""
        return (self.model, self.lane, self.allow, self.deny, self.seat_path())


def spec_default(cat: dict, h: dict, root: str, name: str = "") -> LaunchSpec:
    """What Enter on New Session would start: the harness's own defaults."""
    allow, deny = harness_tools(h)
    seat = seat_file(root) if prompt_flag(h) else None
    return LaunchSpec(
        harness=h["id"], name=name, model=(h.get("models") or [""])[0], effort=h.get("effort") or "",
        lane=harness_lane(h, cat), allow=allow, deny=deny, seat=str(seat) if seat else "",
    )


def row_launch_model(h: dict, row: dict) -> str:
    """The model id a roster row launches with. bind_sitting stores the live display
    name ("Sonnet 5") for the glyph, so the id comes from the recorded argv when it has
    one; a name with a space is never a CLI's model id and falls back to the default."""
    flag, argv = list(h.get("model_flag") or []), row.get("argv")
    if flag and isinstance(argv, list):
        for i in range(len(argv) - len(flag)):
            if argv[i:i + len(flag)] == flag:
                return str(argv[i + len(flag)])
    model = str(row.get("model") or "")
    return "" if " " in model.strip() else model


def spec_from_row(cat: dict, h: dict, row: dict, root: str = "") -> LaunchSpec:
    """A roster row as a launch spec. Missing fields fall back to the harness defaults."""
    base = spec_default(cat, h, root or row.get("cwd") or os.getcwd(), row.get("name") or "")
    got = {k: row.get(k) for k in ("model", "effort", "lane", "allow", "deny", "seat") if k in row}
    if "model" in got:
        got["model"] = row_launch_model(h, row)
    extra = row.get("extra")
    dirs = _row_dirs(row) if "dirs" in row else list(base.dirs)
    return LaunchSpec(
        harness=h["id"], name=row.get("name") or base.name,
        model=got.get("model", base.model) or "", effort=got.get("effort", base.effort) or "",
        lane=got.get("lane", base.lane) or "",
        allow=got.get("allow", base.allow) or "", deny=got.get("deny", base.deny) or "",
        seat=got.get("seat", base.seat) or "", resume_id=row.get("session_id") or "",
        extra=list(extra) if isinstance(extra, list) else [], dirs=dirs,
    )


def spec_argv(cat: dict, h: dict, spec: LaunchSpec) -> list[str]:
    return build_argv(cat, h, spec.model, spec.lane, spec.allow, spec.deny, spec.seat_path(),
                      spec.resume_id, sitting=spec.name, extra=spec.extra or None, effort=spec.effort,
                      dirs=spec.dirs or None)


def spec_command(cat: dict, h: dict, spec: LaunchSpec, root: str, mode: str = "tab") -> list[str]:
    """The command line this spec runs, uid drop included. In this terminal a new
    session id is shown as `<new id>`; cowbell picks the real one at launch."""
    if mode in ("here", "app"):  # the app path starts the same command line as this terminal would
        cmd, session = here_command(cat, h, spec.name, *spec.launch_args(), resume_id=spec.resume_id,
                                    extra=spec.extra or None, effort=spec.effort, dirs=spec.dirs or None)
        if session and session != spec.resume_id:
            cmd = ["<new id>" if p == session else p for p in cmd]
    else:
        argv = spec_argv(cat, h, spec)
        cmd = [h["command"], *argv] if is_wrapper(h) else [h["kind"], *argv]
    return unix_confine(cat, root, cmd, h=h)


def sheet_fields(cat: dict, h: dict, spec: LaunchSpec, root: str,
                 base: LaunchSpec | None = None) -> list[tuple[str, str, str, bool]]:
    """(key, label, shown value, changed from the baseline), in sheet order.

    Only settings this harness can take are listed. The baseline is the harness
    default for a new sitting and the recorded launch for a resume. A resume keeps
    its name and its seat file, so neither is a row.
    """
    resume = bool(spec.resume_id)
    d = base or spec_default(cat, h, root, spec.name)
    rows: list[tuple[str, str, str, bool]] = [] if resume else [("name", "name", spec.name, False)]
    if h.get("model_flag"):
        rows.append(("model", "model", spec.model or "agent default", spec.model != d.model))
    if h.get("effort_flag"):
        rows.append(("effort", "effort", spec.effort or "agent default", spec.effort != d.effort))
    if not is_wrapper(h) or h.get("lane"):
        rows.append(("lane", "permissions", spec.lane or "agent default", spec.lane != d.lane))
    if h.get("tools_allow_flag") and not is_wrapper(h):
        rows.append(("allow", "tools allow", spec.allow or "all", spec.allow != d.allow))
    if h.get("tools_deny_flag") and not is_wrapper(h):
        rows.append(("deny", "tools deny", spec.deny or "none", spec.deny != d.deny))
    if h.get("add_dir_flag"):
        shown = ", ".join(tilde(x) for x in spec.dirs) if spec.dirs else "none"
        rows.append(("dirs", "also in", shown, spec.dirs != d.dirs))
    if prompt_flag(h) and not is_wrapper(h) and not resume:
        shown = tilde(spec.seat) if spec.seat else "none"
        rows.append(("seat", "seat file", shown, spec.seat != d.seat))
    return rows


def _sheet_value(key: str, value: str, changed: bool) -> str:
    if key == "lane" and value in LANES:
        c = {"green": GREEN, "yellow": GOLD, "red": RED}[value]
        return paint(value, c, bold=changed) if changed else paint(value, c)
    return paint(value, INK, bold=True) if changed else paint(value, FAINT)


def sheet_body(cat: dict, h: dict, spec: LaunchSpec, root: str, mode: str = "tab",
               cols: int = 100, base: LaunchSpec | None = None) -> tuple[list[str], list[int], list[str], list[str]]:
    """Lines of the sheet, the line index of each pickable row, the row keys, and
    the command lines.

    Row 0 is start. The command lines are what will run, or the refusal from the
    uid check; the menu pins them above the hint bar so a short popup still shows them.
    """
    where = "new tab in" if mode != "here" else "in this terminal ·"
    if spec.resume_id:
        where = f"resume {spec.resume_id[:8]} ·"
    who = h.get("title") or h["id"]
    if spec.name and spec.name != h["id"]:
        who = f"{h['id']} · {who}"
    body = [f"  {paint(spec.name or h['id'], INK, bold=True)}  {paint(who, MUTED)}  "
            f"{paint(where, FAINT)} {paint(tilde(root), TEAL)}", ""]
    rows, keys = [len(body)], ["start"]
    verb = "resume" if spec.resume_id else "start"
    body.append(f"  {paint('▶', GREEN, bold=True)}  {paint(verb, INK, bold=True)}")
    for k, label, shown, changed in sheet_fields(cat, h, spec, root, base):
        rows.append(len(body))
        keys.append(k)
        shown = _middle_cut(shown, max(12, cols - 24))
        body.append(f"     {paint(f'{label:<14}', SECONDARY)} {_sheet_value(k, shown, changed)}")
    uid = (h.get("unix_user") or (place_for(cat, root) or {}).get("unix_user") or "")
    runs_as = f"{'runs as':<14}"
    body.append(f"     {paint(runs_as, SECONDARY)} "
                f"{paint(uid, GOLD) if uid else paint(os.environ.get('USER') or 'you', FAINT)}")
    try:
        words = [w if w == "<new id>" else shell_join([w]) for w in spec_command(cat, h, spec, root, mode)]
        chunks = _wrap_words(words, max(20, cols - 6))
        cmd = [f"  {paint('$', FAINT)} {paint(chunks[0], MUTED)}"] + [f"    {paint(c, MUTED)}" for c in chunks[1:]]
    except SystemExit as e:
        cmd = [f"  {paint(str(e), RED)}"]
    return body, rows, keys, cmd


def parse_dirs(raw: list[str]) -> list[str]:
    """Directories as absolute paths, `~` expanded, duplicates and blanks dropped.

    A directory that does not exist is refused: the agent would be told to work in nothing.
    """
    out: list[str] = []
    for d in raw:
        d = (d or "").strip()
        if not d:
            continue
        full = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(full):
            raise SystemExit(f"cowbell: {d} is not a directory")
        if full not in out:
            out.append(full)
    return out


def slug_effort(raw: str) -> str:
    """A typed effort level: one word of letters, digits, dot, dash or underscore."""
    raw = (raw or "").strip()
    return raw if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", raw) else ""


def _middle_cut(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    keep = width - 1
    return text[: keep // 2] + "…" + text[len(text) - (keep - keep // 2):]


def _wrap_words(words: list[str], width: int) -> list[str]:
    """Lines of at most `width`, broken between words. A word longer than a line gets its own."""
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    return lines + [cur] if cur or not lines else lines


def _sheet_pick_model(h: dict, spec: LaunchSpec) -> str | None:
    items = model_menu_items(h)
    models = list(h.get("models") or [])
    cur = models.index(spec.model) if spec.model in models[:10] else 0
    j = choose("model", items, default=cur, mark="current" if spec.model else "")
    if j is None:
        return None
    if j == len(items) - 1:
        return ask("  model id: ", spec.model) or spec.model
    return "" if not models else models[j]


def sheet_edit(cat: dict, h: dict, spec: LaunchSpec, key_: str, root: str, taken: set[str]) -> str:
    """Change one setting in place. Esc inside a field leaves it as it was.

    Returns a refusal to show on the sheet ("" when the change took)."""
    try:
        if key_ == "name":
            raw = ask(f"  name [{spec.name}]: ", spec.name)
            if (raw or "").strip() and slug(raw) != spec.name:
                spec.name = unique_name(slug(raw), taken)
        elif key_ == "model":
            m = _sheet_pick_model(h, spec)
            if m is not None:
                spec.model = m
        elif key_ == "effort":
            levels = [x for x in (h.get("efforts") or []) if isinstance(x, str) and x]
            items = ["(agent default)"] + levels + ["(type another)"]
            cur = 1 + levels.index(spec.effort) if spec.effort in levels else 0
            j = choose("effort", items, default=cur, mark="current")
            if j is not None:
                if j == 0:
                    spec.effort = ""
                elif j == len(items) - 1:
                    spec.effort = slug_effort(ask("  effort: ", spec.effort))
                else:
                    spec.effort = levels[j - 1]
        elif key_ == "lane":
            cur = LANES.index(spec.lane) if spec.lane in LANES else 1
            j = choose("permissions", _lane_rows(cat, h), default=cur, mark="current")
            if j is not None:
                spec.lane = LANES[j]
        elif key_ == "allow":
            spec.allow = ask("  tools allow (empty = all): ", spec.allow)
        elif key_ == "deny":
            spec.deny = ask("  tools deny (empty = none): ", spec.deny)
        elif key_ == "dirs":
            cur = ", ".join(tilde(x) for x in spec.dirs)
            raw = ask("  other directories, comma-separated (empty = none): ", cur)
            spec.dirs = parse_dirs(raw.split(","))
        elif key_ == "seat":
            found = seat_file(root)
            spec.seat = "" if spec.seat else (str(found) if found else "")
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        return str(e).removeprefix("cowbell: ")
    return ""


def sheet_hints(keys: list[str], resume: bool = False):
    go, reset = ("resume", "as recorded") if resume else ("start", "defaults")

    def hints(i: int):
        save = [] if resume else [("a", "save as", "a")]
        if keys[i] == "start":
            return [("⏎", go, "enter"), ("d", reset, "d"), *save, ("esc", "cancel", "esc")]
        return [("⏎", "change", "enter"), ("s", go, "s"), ("d", reset, "d"), *save, ("esc", "cancel", "esc")]
    return hints


def sheet_save_as(h: dict, spec: LaunchSpec) -> tuple[str, str]:
    """Sheet `a`: keep this shape as a new [[harness]] row (offer_save_as). Returns
    (saved id or "", the line the sheet shows). Launches nothing."""
    try:
        hid = offer_save_as(h, spec.model, spec.lane, spec.allow, spec.deny, spec.effort)
    except SystemExit as e:
        return "", str(e).removeprefix("cowbell: ")
    return hid, (f"saved {hid} (a New Session row)" if hid else "")


def launch_sheet(ctx: Ctx, cat: dict, h: dict, root: str, spec: LaunchSpec,
                 taken: set[str] | None = None, base: LaunchSpec | None = None,
                 saved: list[str] | None = None) -> LaunchSpec | None:
    """Show the sheet until start (returns the spec) or esc (returns None).

    `base` is what `d` restores and what a dim value means; default: the harness defaults.
    `a` (not on a resume) saves the shape as a new harness; its line goes in `saved`.
    """
    taken = set(taken or ())
    spec = replace(spec, extra=list(spec.extra), dirs=list(spec.dirs))
    selected, flash = 0, ""
    while True:
        cols = shutil.get_terminal_size((100, 24))[0]
        body, rows, keys, cmd = sheet_body(cat, h, spec, root, ctx.mode, cols, base)
        if tui_ok():
            res = menu(body, rows, sheet_hints(keys, bool(spec.resume_id)), selected=selected,
                       hotkeys="sd" if spec.resume_id else "sda", foot=cmd, flash=flash)
            flash = ""
            if res[0] == "cancel":
                return None
            selected = res[-1]
            k = keys[selected] if res[0] == "pick" else res[1]
        else:
            print("\n".join([""] + body + [""] + cmd))
            labels = {k_: l_ for k_, l_, *_ in sheet_fields(cat, h, spec, root, base)}
            for n, kk in enumerate(keys[1:], 1):
                print(f"  {n:>3}  change {labels.get(kk, kk)}")
            raw = ask("  number to change (empty = start, a = save as, q = cancel): " if not spec.resume_id
                      else "  number to change (empty = resume, q = cancel): ", "")
            if raw.lower() == "q":
                return None
            if raw.lower() == "a" and not spec.resume_id:
                k = "a"
                raw = None
            if raw is not None:
                try:
                    n = int(raw) if raw else 0
                except ValueError:
                    continue
                if not 0 <= n < len(keys):
                    continue
                k = keys[n]
        if k == "a" and not spec.resume_id:
            hid, flash = sheet_save_as(h, spec)
            if hid and saved is not None:
                saved.append(hid)
            if flash and not tui_ok():
                print(f"  {flash}")
            continue
        if k in ("start", "s"):
            return spec
        if k == "d":
            if base is not None:
                spec = replace(base, name=spec.name, extra=list(base.extra), dirs=list(base.dirs))
            else:
                fresh = spec_default(cat, h, root, spec.name)
                fresh.resume_id, fresh.extra = spec.resume_id, spec.extra
                spec = fresh
            continue
        flash = sheet_edit(cat, h, spec, k, root, taken)
        if flash and not tui_ok():
            print(f"  {flash}")


def resume_sheet(ctx: Ctx, cat: dict, h: dict, row: dict, root: str) -> LaunchSpec | None:
    """The sheet over a recorded session: what it ran with, ready to change, then resume."""
    rec = spec_from_row(cat, h, row, root)
    rec.extra = []  # a resume does not replay passthrough argv
    return launch_sheet(ctx, cat, h, root, rec, base=rec)


def last_launch_here(cat: dict, h: dict, root: str, rows: list[dict] | None = None) -> dict | None:
    """The newest roster row for this harness in this directory: how it was last started here."""
    here = os.path.realpath(root)
    mine = [r for r in (load_roster() if rows is None else rows)
            if r.get("harness") == h["id"] and r.get("cwd") and os.path.realpath(r["cwd"]) == here]
    if not mine:
        return None
    # A herdr raise writes `created` and no `last_seen`; a here launch writes both. The day
    # is the later of the two, so a fresh herdr row is not outranked by an old here row.
    return max(mine, key=lambda r: (max(r.get("last_seen") or "", (r.get("created") or "")[:10]),
                                    r.get("created") or ""))


def seed_spec(cat: dict, h: dict, root: str, name: str, rows: list[dict] | None = None) -> LaunchSpec:
    """What the sheet opens on for a new sitting: the last launch here, else the harness defaults.

    Only the launch settings carry over; the name, session and passthrough argv are new.
    """
    last = last_launch_here(cat, h, root, rows)
    if not last:
        return spec_default(cat, h, root, name)
    spec = spec_from_row(cat, h, last, root)
    spec.name, spec.resume_id, spec.extra = name, "", []
    if spec.seat and not Path(spec.seat).is_file():
        spec.seat = spec_default(cat, h, root, name).seat
    return spec


def raise_with_options(ctx: Ctx, cat: dict, h: dict, root: str, v: dict, name: str,
                       *, save_as: bool, taken: set[str] | None = None, seed: LaunchSpec | None = None) -> bool:
    """Open the launch sheet for this harness, then start what it shows. Esc cancels."""
    saved: list[str] = []
    spec = launch_sheet(ctx, cat, h, root, seed or spec_default(cat, h, root, name), taken=taken, saved=saved)
    if spec is None:
        return False
    if save_as and not saved:
        try:
            if hid := offer_save_as(h, spec.model, spec.lane, spec.allow, spec.deny, spec.effort):
                print(f"  saved {hid}")
        except SystemExit as e:
            print(f"  {e}")
    if v["verdict"] == "overprivilege":
        print(f"  {paint('! ' + trust_note(h['id'], v, root), FAINT)}")
    if ctx.mode != "here":
        argv = spec_argv(cat, h, spec)
        shown = shell_join([h["command"], *argv]) if is_wrapper(h) else shell_join([h["kind"], *argv])
        print(f"\n  {paint(spec.name, INK, bold=True)}  {paint(shown, MUTED)}")
    pane = launch(cat, h, spec.name, root, ctx, *spec.launch_args(), extra=spec.extra or None,
                  name_source="pinned", effort=spec.effort, dirs=spec.dirs or None)
    print(f"  started {spec.name} in {pane['pane_id']}")
    return True


def edit_sitting(ctx: Ctx, cat: dict, hid: str, root: str) -> bool:
    """New Session `o`: the launch sheet for this harness, opened on how it was last
    started here (else its defaults). Writes the catalog only on `a` (save as). Esc returns to the picker.
    """
    h = harness_by_id(cat, hid)
    if not h:
        print(f"  unknown harness {hid}"); return False
    if unavailable_reason(h):
        print(f"  {unavailable_reason(h)}"); return False
    v = trust_match(h["trust_tool"], root)
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {hid} on {tilde(root)} is blocked {v['flag']}"); return False
    rows = load_roster()
    taken = live_names(ctx.live.snapshot()) | {r["name"] for r in rows}
    name = unique_name(slug(h["id"]), taken)
    return raise_with_options(ctx, cat, h, root, v, name, save_as=False, taken=taken,
                              seed=seed_spec(cat, h, root, name, rows))


def wizard(ctx: Ctx, cat: dict, root: str) -> bool:
    """New with options: pick an agent, then its launch sheet, then optional save-as."""
    hs = [h for h in cat.get("harness", []) if h.get("id")]  # a row with no id is named by catalog --check, never launchable
    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = dict(zip([h["id"] for h in hs], pool.map(lambda h: trust_match(h["trust_tool"], root), hs)))
    note = {"blocked": paint("  ⊘ blocked here", RED), "overprivilege": paint("  ! more reach than needed", FAINT)}
    rows = []
    for h in hs:
        ok = installed(h)
        hid, desc = f"{h['id']:<22}", h.get("title", "") if ok else "not installed"
        rows.append(f"{paint(hid, SECONDARY if ok else FAINT)} {paint(desc, MUTED if ok else FAINT)}"
                    f"{note.get(verdicts[h['id']]['verdict'], '')}")
    intro = f"{paint('new tab in' if ctx.mode != 'here' else 'in this terminal ·', FAINT)} {paint(tilde(root), TEAL)}"
    if not tui_ok():
        print(f"\n  {intro}")
    i = choose("agent", rows, intro=intro)
    if i is None:
        return False
    h = hs[i]
    if unavailable_reason(h):
        print(f"  {unavailable_reason(h)}"); return False
    v = verdicts[h["id"]]
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {h['id']} on {tilde(root)} is blocked {v['flag']}"); return False
    taken = live_names(ctx.live.snapshot()) | {r["name"] for r in load_roster()}
    name = unique_name(slug(h["id"]), taken)
    return raise_with_options(ctx, cat, h, root, v, name, save_as=True, taken=taken)


def raise_quick(ctx: Ctx, cat: dict, hid: str, root: str) -> bool:
    """New with defaults: automatic name (unless [defaults] ask_name), harness lane/tools, open now."""
    h = harness_by_id(cat, hid)
    if not h:
        print(f"  unknown harness {hid}"); return False
    if unavailable_reason(h):
        print(f"  {unavailable_reason(h)}"); return False
    v = trust_match(h["trust_tool"], root)
    if v["verdict"] == "blocked":
        print(f"  ⊘ refused: {hid} on {tilde(root)} is blocked {v['flag']}"); return False
    taken = live_names(ctx.live.snapshot()) | {r["name"] for r in load_roster()}
    base = unique_name(slug(h["id"]), taken)
    lane = harness_lane(h, cat)
    allow, deny = harness_tools(h)
    if ctx.mode != "here":  # outside herdr, exec_here prints the command it hands the terminal to
        print(f"  {paint(h['id'], INK, bold=True)}  {paint('new tab in', FAINT)} {paint(tilde(root), TEAL)}")
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
    pane = launch(cat, h, name, root, ctx, model, lane, allow, deny, seat, effort=h.get("effort") or "",
                  name_source="pinned" if cat["defaults"].get("ask_name") else "")
    print(f"  started {name} in {pane['pane_id']}")
    return True


REFRESH_S = 2.0
PICKER_HOTKEYS = "nwgae+ph-ro"


def footer(can_widen: bool = False, start_row: bool = False) -> str:
    hints = [("number", "= open"), ("[n]", "new"), ("[+]", "add")]
    if can_widen:
        hints.append(("[g]", "widen"))
    if start_row:
        hints += [("[e]", "edit"), ("[p]", "pin"), ("[h]", "hide"), ("[-]", "rm")]
    hints.append(("[q]", ""))
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


def persist_canonical(reg: dict, rows: list[dict]) -> bool:
    """Write recomputed names back onto unpinned roster rows. A pin is left alone.

    The registry names are already unique. A row this pass will rewrite does
    not count as still holding its old name, so the session that keeps the
    bare title is not pushed aside by a sibling that is about to take a tail.
    """
    def match(src: dict) -> dict | None:
        pane = str(src.get("pane_id") or "")
        if pane and pane != "gone" and not pane.startswith("pid:"):
            hits = [r for r in rows if str(r.get("pane_id") or "") == pane]
            if len(hits) == 1:
                return hits[0]
        sid = src.get("session_id") or ""
        if sid:
            hits = [r for r in rows if r.get("session_id") == sid]
            if len(hits) == 1:
                return hits[0]
        return None

    matched: list[tuple[dict, dict]] = []
    for src in list(reg.get("live") or []) + list(reg.get("cold") or []):
        if src.get("name_source") == "pinned":
            continue
        target = match(src)
        if target is None or target.get("name_source") == "pinned" or not src.get("name"):
            continue
        matched.append((src, target))
    rewriting = {id(target) for _, target in matched}
    taken = {r.get("name") for r in rows if id(r) not in rewriting and r.get("name")}
    changed = False
    for src, target in matched:
        desired = unique_name(src["name"], taken)
        taken.add(desired)
        if target.get("name") == desired:
            continue
        target["name"] = desired
        if src.get("name_source"):
            target["name_source"] = src["name_source"]
        changed = True
    return changed


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
    root, _, remapped = resolve_root(cat, ctx.cwd)
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
            t_frame = time.perf_counter()
            full_cat = load_catalog()
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
            t_hist = time.perf_counter()
            warnings: list[str] = []
            history = launch_history(full_cat, root, warnings)
            hist_ms = _ms(t_hist)
            t_reg = time.perf_counter()
            reg = build_registry(cat, root, agents, panes, rows, matcher=matcher_for(root),
                                 tab_label=tab_labels(snap), history=history)
            trace.event("frame", ms=_ms(t_frame), history_ms=hist_ms, registry_ms=_ms(t_reg),
                        live=len(reg["live"]), cold=len(reg["cold"]), startable=len(reg["startable"]))
            if place:
                reg["place_note"] = place["name"] + (" · from ~" if remapped and root == place["cwd"] else "")
            reg["warnings"] = warnings
            remember_live(reg, rows)
            if persist_canonical(reg, rows):
                save_roster(rows)
            # publish sidebar identity once per event (a state change seen
            # here, or a name/harness change after an adopt) — never per refresh.
            for r in identity_events(ident_seen, reg["live"]):
                report_identity(r["pane_id"], r["name"], r.get("kind") or "", r.get("model") or "")
            ws_labels = {w.get("workspace_id"): w.get("label") or "" for w in snap.get("workspaces") or []}
            term = shutil.get_terminal_size((84, 24))
            width = min(term.columns, 100)
            choices = picker_choices(reg, rows, ctx.workspace_id, ws_labels, expanded,
                                     lines=term.lines, width=width)
            text = render_picker(reg, choices, top, wider, width=width)
            can_widen = bool(top and wider is not None and wider > len(reg["live"]))
            if tui:
                openable = [c for c in choices if picker_selectable(c)]
                keys = [c.get("key") for c in openable]
                selected = keys.index(sel_key) if sel_key in keys else min(sel_at, max(0, len(keys) - 1))

                def hints_for(sel, _openable=openable, _widen=can_widen):
                    action = (_openable[sel].get("action") if _openable and 0 <= sel < len(_openable) else "")
                    return picker_hints(_widen, action == "raise", action == "enter")

                res = menu([""] + text.split("\n"), [c["row"] + 1 for c in openable], hints_for,
                           selected=selected, hotkeys=PICKER_HOTKEYS, flash=flash,
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
                    # a / e / p / h / − / r / o use the highlighted row; n / w / g / + do not
                    hit = (openable[sel_at] if raw in "aeph-ro" and openable and 0 <= sel_at < len(openable)
                           else None)
                else:
                    hit, raw = None, res[1] if len(res) > 1 else ""
                if hit and hit["action"] == "more" and raw not in ("a", "e", "p", "h", "-", "o"):
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
                if hit and hit.get("action") == "enter":
                    if scr:
                        scr.close()
                        sys.stdout.write("\x1b[H\x1b[2J")
                    try:
                        cur = hit["target"].get("name") or ""
                        typed = ask(f"  rename {cur} to: ", cur)
                        if typed.strip() and slug(typed) != cur:
                            nm = pin_session_name(cur, typed, target=hit["target"])
                            flash = f"pinned {nm}"
                    except KeyboardInterrupt:
                        pass
                    except SystemExit as e:
                        flash = ((str(e) or "").splitlines() or ["error"])[0]
                        if not tui:
                            print(f"  {e}")
                continue
            if raw == "g":
                root = top or root
                sel_key = ""
                continue
            if raw == "p":
                if hit and hit.get("action") == "raise":
                    flash = toggle_pin(root, hit["target"]["harness"])
                continue
            if raw == "h":
                if hit and hit.get("action") == "raise":
                    flash = apply_hide(root, hit["target"]["harness"])
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
            prompts = raw in ("w", "n", "+", "e", "-")
            if not prompts and not hit:
                continue
            if hit and hit["action"] == "focus":
                continue  # Active is a census; cowbell cannot take over a sitting already running
            if raw in ("e", "-") and not (hit and hit.get("action") == "raise"):
                continue
            if raw == "o" and not (hit and hit.get("action") in ("raise", "enter")):
                continue
            if scr:
                scr.close()
                sys.stdout.write("\x1b[H\x1b[2J")  # prompts that follow start on a clean popup
            try:
                if raw in ("w", "n"):
                    done = wizard(ctx, cat, root)
                elif raw == "+":
                    add_harness(root, full_cat)
                    done = False
                elif raw == "e":
                    edit_harness(full_cat, hit["target"]["harness"])
                    done = False
                elif raw == "-":
                    remove_harness(hit["target"]["harness"])
                    done = False
                elif raw == "o" and hit["action"] == "enter":
                    done = cmd_enter(ctx, cat, hit["target"]["name"], root, target=hit["target"],
                                     shape=lambda c, h, row, rt: resume_sheet(ctx, c, h, row, rt))
                elif raw == "o":
                    done = edit_sitting(ctx, cat, hit["target"]["harness"], root)
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
                   "--pen", "--split", "--dry-run", "--check", "--help", "-h"):
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
    if in_hc_app(env):
        # The Herdr desktop app. Census from `hc`; an agent caller, --spawn and --split open app tabs, a
        # person execs here. A popup picker (COWBELL_HC_SPACE) always opens a tab: its terminal closes.
        target = hc_space_override(env)
        term = (env.get("HC_TERMINAL_ID") or "").strip()
        cwd = (env.get("COWBELL_HC_CWD") or "").strip() or os.getcwd()
        app_tab = bool(target) or (wants_tab and env.get("COWBELL_MODE") != "here")
        ctx = Ctx(cwd=cwd, pane_id=f"hc:{term}" if term else None, workspace_id=target or None,
                  mode="app" if app_tab else "here",
                  live=CombinedLive(HcLive(), ProcessScan(skip_hc=True)), launch=HcLaunch())
        trace.event("host", mode=ctx.mode, herdr=0, hc=1)
        return ctx
    if wants_tab and herdr_up and in_herdr_pane(env):
        ctx = Ctx.from_env(env)
    else:
        ctx = Ctx(cwd=os.getcwd(), mode="here", live=CombinedLive(HerdrLive() if herdr_up else None, ProcessScan()))
    trace.event("host", mode=ctx.mode, herdr=int(bool(getattr(ctx.live, "herdr", False))))
    return ctx


NEW_OPTS = frozenset({
    "harness", "model", "effort", "add-dir", "lane", "allow", "deny", "name", "place", "root",
    "spawn", "pen", "split", "no-seat", "no-focus", "yes", "y", "help", "h",
})


def new_usage(file=None) -> None:
    print("cowbell new [harness] [options...] [-- extra...]", file=file)
    print("  --harness ID    Harness to run (or the first positional)", file=file)
    print("  --model M       Override the harness default model", file=file)
    print("  --effort E      Reasoning effort, through the harness's effort_flag", file=file)
    print("  --add-dir D,E   Other directories the agent may work in, through add_dir_flag", file=file)
    print("  --lane L        Permissions preset: green, yellow or red (default: yellow)", file=file)
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
    print("  -y, --yes       Accepted and unused; this path has no prompt", file=file)
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


def take_debug(argv: list[str]) -> list[str]:
    """`--debug` before `--` turns the trace on and is not a command or a harness flag."""
    if "--" in argv:
        cut = argv.index("--")
        head, tail = argv[:cut], argv[cut:]
    else:
        head, tail = argv, []
    if "--debug" in head:
        trace.force()
        head = [a for a in head if a != "--debug"]
    return head + tail


def main(argv: list[str]) -> int:
    argv = take_debug(list(argv))
    if trace.enabled():
        trace.event("start", argv=" ".join(redact_argv(argv)) or "(default)", cwd=os.getcwd())
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    if argv and argv[0] in ("-V", "--version"):
        print(f"cowbell {__version__}"); return 0
    if argv and argv[0] == "_history":
        payload = json.load(sys.stdin)
        rows = discover_sessions(payload["root"], payload["cat"], home=Path(payload["home"]))
        print(json.dumps(rows))
        return 0
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
                split=bool(opts.get("split")), effort=opts.get("effort", ""),
                dirs=[d for d in (opts.get("add-dir") or "").split(",") if d])
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
        print("  Rewrite pane borders so they match the sitting and the agent Herdr detects.")
        print("  The tab label stays the sitting. A bare border that is not the sitting is stale.")
        print("  With no herdr binary: 'herdr is not installed; nothing to reconcile'.")
        return 0
    ctx = choose_host(server_up())
    cat = load_catalog()
    if cmd == "picker":
        return picker(ctx, cat)
    if cmd in ("registry", "here", "status"):
        cmd_registry(ctx, cat, opts.get("root"), as_json=bool(opts.get("json")),
                     place_name=opts.get("place") or ""); return 0
    if cmd == "list":
        cmd_list(ctx, bool(opts.get("all")), cat); return 0
    if cmd == "catalog":
        if opts.get("check"):
            return cmd_catalog_check(cat)
        cmd_catalog(cat); return 0
    if cmd == "accept":
        return cmd_accept(cat, pos)
    if cmd == "enter":
        if not pos:
            raise SystemExit("enter <name>")
        cmd_enter(ctx, cat, pos[0]); return 0
    if cmd == "rename":
        if opts.get("help") or opts.get("h") or len(pos) != 2:
            print("cowbell rename <name-or-session-id> <new-name>")
            print("  Pin a session name. A later refresh will not replace a pin.")
            print("  The herdr tab is left as it is. Works with herdr down.")
            if len(pos) != 2 and not (opts.get("help") or opts.get("h")):
                return 2
            return 0
        print(f"pinned {pin_session_name(pos[0], pos[1])}")
        return 0
    if cmd == "adopt":
        cmd_adopt(ctx, cat, name=pos[0] if pos else None, pane_id=opts.get("pane"))
        return 0
    if cmd == "reconcile":
        apply_coherence(dry_run=bool(opts.get("dry-run")))
        return 0
    if cmd == "identity":
        action = pos[0] if pos else ""
        if action == "stamp":
            n = stamp_live_identities()
            print(f"stamped {n} pane{'s' if n != 1 else ''}")
            return 0
        if action == "sync":
            print(sync_identity_from_plugin_event())
            return 0
        print("cowbell identity stamp|sync", file=sys.stderr)
        print("  stamp  Publish $glyph for every live herdr agent pane (detector kind).", file=sys.stderr)
        print("  sync   Restamp or clear one pane from HERDR_PLUGIN_EVENT_JSON.", file=sys.stderr)
        return 2
    if cmd == "font":
        action = pos[0] if pos else "status"
        if action == "status":
            return cmd_font_status()
        if action == "install":
            return cmd_font_install()
        print("cowbell font [status|install]", file=sys.stderr)
        return 2
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
