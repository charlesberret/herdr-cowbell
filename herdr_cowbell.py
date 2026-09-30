#!/usr/bin/env python3
"""herdr-cowbell — the herdr skin over cowbell.

  bin/cowbell open                 open the picker popup for the caller's directory (prefix+c)
  bin/cowbell adopt [name]         put the focused agent pane on the roster (core)
  bin/cowbell stop <name>          ctrl-c a running sitting; the roster keeps it
  bin/cowbell import-seats         pull herdr-seats' seats.json into the roster
  bin/cowbell keys install|uninstall|ensure   bind / restore prefix+c; ensure reasserts a prior claim
  bin/cowbell <anything else>      the cowbell core (picker, status, new, enter, ...)

Which core runs: a `cowbell` on PATH whose --version is at least the vendored one
(vendor/cowbell.py) wins; otherwise the vendored copy. The plugin commands above
always use the vendored core. Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
SELF_BIN = HERE / "bin" / "cowbell"
PLUGIN_ID = "herdr-cowbell"
PLUGIN_COMMANDS = {"open", "adopt", "stop", "import-seats", "keys"}
HERDR_CONFIG = Path("~/.config/herdr/config.toml").expanduser()
SEATS_JSON = Path("~/.config/herdr/plugins/config/herdr-seats/seats.json").expanduser()
CLAIM_FILE = "prefix-c.claimed"
CLAIM_BODY = (
    "# herdr-cowbell: prefix+c is claimed. `cowbell keys uninstall` releases it.\n"
    "# A herdr upgrade that rewrites config.toml is reasserted on the next server start.\n"
)
KEYS_ACTIONS = ("install", "uninstall", "ensure")
CTX_KEYS = (
    "workspace_id", "workspace_cwd", "tab_id", "tab_label",
    "focused_pane_id", "focused_pane_cwd", "focused_pane_agent",
)
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


# --- which core ------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...] | None:
    m = VERSION_RE.search(text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def vendored_version() -> tuple[int, ...]:
    m = re.search(r'^__version__ = "([^"]+)"', (VENDOR / "cowbell.py").read_text(), re.M)
    v = parse_version(m.group(1) if m else "")
    if not v:
        raise SystemExit("herdr-cowbell: vendor/cowbell.py has no __version__ (run scripts/vendor-core)")
    return v


def path_cowbell(env=None) -> str | None:
    """A `cowbell` on PATH that is not this plugin's own shim."""
    env = os.environ if env is None else env
    for d in (env.get("PATH") or "").split(os.pathsep):
        exe = os.path.join(d, "cowbell")
        if os.path.isfile(exe) and os.access(exe, os.X_OK) and Path(exe).resolve() != SELF_BIN.resolve():
            return exe
    return None


def path_version(exe: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(out) if out.startswith("cowbell ") else None


def choose_core(env=None) -> str | None:
    """The PATH cowbell to exec when it is at least as new as the vendored core, else None."""
    exe = path_cowbell(env)
    if not exe:
        return None
    v = path_version(exe)
    return exe if v and v >= vendored_version() else None


def core():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    import cowbell  # the vendored core
    return cowbell


# --- open (prefix+c) --------------------------------------------------------------

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
            cwd = core().herdr_pane_cwd(pane_id)
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


def cmd_open(env=None) -> int:
    """Action entry: open the popup, forwarding the caller's context (the popup's own is not the caller's)."""
    cb = core()
    try:
        cb.herdr(*picker_open_args(invocation_ctx(env)))
    except cb.HerdrError as e:
        print(f"cowbell: {e}", file=sys.stderr)
        return 1
    return 0


# --- adopt / stop / import-seats --------------------------------------------------

def cmd_stop(name: str) -> None:
    cb = core()
    rows = cb.load_roster()
    hits = [r for r in rows if r["name"] == name]
    if not hits:
        raise SystemExit(f"cowbell: no roster entry {name!r}")
    live = cb.HerdrLive().agent(name) or {}
    pid = live.get("pane_id") or hits[0].get("pane_id")
    if not pid:
        raise SystemExit(f"cowbell: {name} is not live")
    cb.herdr("agent", "send-keys", pid, "ctrl+c")
    print(f"ctrl+c → {name} ({pid})")


def import_seats(seats_path: Path = SEATS_JSON) -> int:
    cb = core()
    if not seats_path.exists():
        return 0
    data = json.loads(seats_path.read_text())
    seats = data.get("seats", data) if isinstance(data, dict) else data
    rows = cb.load_roster()
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
            "label": s.get("label", s["name"]), "created": cb.now(), "source": "herdr-seats",
        })
        names.add(s["name"]); added += 1
    cb.save_roster(rows)
    return added


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


def cowbell_config_dir() -> Path:
    """Same lookup as the vendored core, without importing it (and without mkdir)."""
    base = os.environ.get("COWBELL_CONFIG_DIR") or \
        Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "cowbell"
    return Path(base).expanduser()


def keys_claim_path() -> Path:
    return cowbell_config_dir() / CLAIM_FILE


def keys_claimed(path: Path | None = None) -> bool:
    return (path or keys_claim_path()).is_file()


def write_keys_claim(claimed: bool, path: Path | None = None) -> None:
    p = path or keys_claim_path()
    if claimed:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file() or p.read_text(encoding="utf-8") != CLAIM_BODY:
            p.write_text(CLAIM_BODY, encoding="utf-8")
    elif p.is_file():
        p.unlink()


def _reload_config_after_ensure() -> None:
    cb = core()
    if not cb.server_up():
        print("run: herdr server reload-config")
        return
    try:
        cb.herdr("server", "reload-config", expect_json=False)
    except cb.HerdrError as e:
        print(f"cowbell: wrote the binding; reload-config failed ({e})", file=sys.stderr)
        print("run: herdr server reload-config")


def cmd_keys(action: str, path: Path = HERDR_CONFIG, *, claim: Path | None = None,
             reload: bool | None = None) -> None:
    """install/uninstall are opt-in. ensure is silent unless a prior install was clobbered.

    `reload` defaults to on for ensure against the live herdr config, off otherwise
    (tests pass a temp path; they must not bounce the running server).
    """
    if action not in KEYS_ACTIONS:
        raise SystemExit(f"cowbell keys: unknown action {action!r} (install|uninstall|ensure)")
    claim_path = claim if claim is not None else keys_claim_path()
    if action == "ensure" and not keys_claimed(claim_path):
        return
    if action == "uninstall":
        write_keys_claim(False, claim_path)
        desired = "uninstall"
    else:
        write_keys_claim(True, claim_path)
        desired = "install"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new = keys_install_text(text) if desired == "install" else keys_uninstall_text(text)
    if new == text:
        if action != "ensure":
            print(f"{path}: already {action}ed")
        return
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    n = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}.{n}"); n += 1
    if path.exists():
        shutil.copy2(path, backup)
    core().write_atomic(path, new)
    if action == "ensure":
        print(f"reasserted prefix+c binding in {path} (backup: {backup.name})")
        should_reload = reload if reload is not None else path.resolve() == HERDR_CONFIG.resolve()
        if should_reload:
            _reload_config_after_ensure()
        return
    print(f"{action}ed prefix+c binding in {path} (backup: {backup.name})")
    print("run: herdr server reload-config")


# --- main ------------------------------------------------------------------------

def plugin_main(argv: list[str]) -> int:
    cmd, rest = argv[0], argv[1:]
    if cmd == "open":
        return cmd_open()
    if cmd == "keys":
        cmd_keys(rest[0] if rest else "install"); return 0
    if cmd == "import-seats":
        print(f"imported {import_seats()} seats from herdr-seats"); return 0
    if cmd == "adopt":
        exe = choose_core()
        if exe:
            os.execv(exe, [exe, "adopt", *rest])
        return core().main(["adopt", *rest])
    cb = core()
    if not cb.server_up():
        print(f"cowbell: {cmd} needs a running Herdr server", file=sys.stderr); return 2
    if not rest:
        raise SystemExit("stop <name>")
    cmd_stop(rest[0]); return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in PLUGIN_COMMANDS:
        return plugin_main(argv)
    exe = choose_core()
    if exe:
        os.execv(exe, [exe, *argv])
    return core().main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # herdr errors from the vendored core read as one line, like the core's own entry
        if type(e).__name__ == "HerdrError":
            print(f"cowbell: {e}", file=sys.stderr)
            sys.exit(1)
        raise
