# cowbell

Call up a sitting here. Herdr plugin **herdr-cowbell**, CLI **cowbell**.

A cowbell is how a herder hears where each animal is (silence means one is
missing) and rings to call them in. Per-directory herd registry behind the
new-tab key: attach to an established harness, or raise one with the model,
lane, and tools chosen on purpose.

Press `prefix+c` in a directory and cowbell shows the three ways to get an
agent there: **running here** (agents whose cwd is this directory or below),
**resume** (ones that stopped and can pick up their session), and **start new**
(the agents in `harnesses.toml` that are installed and allowed here, most used
here first).

```
  ~/code/notes/nomogenesis            g widen to metawork · 4 running

  Running here
    1  nomogenesis-claude     claude      idle
    2  philologist            grok        working  14s   in sys:clubhouse

  Resume
    3  kimi-ballots           oll-mid     2 days ago

  Start new
    4  claude                 Claude Code
    5  oll-mid                kimi k2.7 cloud — default coding
    6  grok                   Grok CLI                           ! asks first
    7  agy                    Antigravity (Gemini)
       + 9 more

  ⏎ open   n new with options   g widen   esc close
```

The popup is mouse-first, like herdr: click or arrow to a row, Enter (or the ⏎
button) to open it, Esc to close. A digit jumps straight to its row, and every
hint in the bottom bar is clickable. It refreshes itself every two seconds.

- **Enter on Start new** opens the agent at once with an automatic name
  (`claude`, `claude-2`) and the default permissions. Set `ask_name = true` in
  `[defaults]` to be asked for the name first (the house setting: a name claims a seat).
- **`n` new with options** asks for agent, name, model, and permissions.
- **Trust** appears only if `agent-deck-trust` is installed, and only as an
  exception: `! asks first` on an over-privileged row; blocked agents are left off.
- **Running agents are remembered** as they are seen, so they show under Resume
  once they stop. (`cowbell adopt` still exists on the CLI.)
- A section longer than five rows folds to four and `+ N more`.

Config and state live in `~/.config/cowbell/` and `~/.local/state/cowbell/`
(`COWBELL_CONFIG_DIR` / `COWBELL_STATE_DIR`, or the XDG homes). Versions up to 0.3
kept them in herdr's plugin dirs; the first run copies them over once, leaving the
originals and a dated backup, and records it in `~/.config/cowbell/.migrated_from`.
Both files carry `schema = 1`; a cowbell that finds a newer schema reads the file but
will not overwrite it.

Bare `cowbell` still prints the long registry in house vocabulary (session ids,
cwd, the absent line). Off a terminal, or with `COWBELL_PLAIN=1`, the popup falls
back to a typed-number prompt. `NO_COLOR` turns colour off.

## Vocabulary

| Word | Means |
|------|-------|
| **harness** | A way of raising an agent: executable (kind or house wrapper), model, lane, tools, trust surface. Declared in `harnesses.toml`. |
| **constitution** | The catalog of harnesses that *may* be raised — `~/.config/cowbell/harnesses.toml`, shared by the plugin and the CLI. Hand-edited. |
| **roster** | What *has* been raised and where — `~/.local/state/cowbell/roster.json`. Machine-written; one row per named harness with its cwd, session id, pane. |
| **lane** | House shorthand for write/bash knobs (`green` / `yellow` / `red`), mapped to per-kind flags in the catalog. Not a trust model. |
| **subject / verdict** | From `agent-deck-trust`: the sensitivity class of the directory, and whether a harness is `ok`, over-privileged (`!`, visible), or blocked (`⊘`, refused). cowbell never re-encodes trust law; it asks. |

## What it does not do

- It does not replace `herdr`'s own agent detection. Live rows come from
  `herdr agent list`; cowbell only adds directory scope and the roster.
- It does not spin up layouts (that is herdr-plus Projects / sessionizer).
- It does not decide trust. `agent-deck-trust match <tool> <path>` does;
  exit 4 refuses, exit 3 warns and asks.

## Install (local link)

```bash
herdr plugin link ~/code/herdr-cowbell
herdr plugin action invoke herdr-cowbell.install-keys    # prefix+c → registry popup
herdr server reload-config
cowbell import-seats                                     # optional: absorb herdr-seats' roster
```

`install-keys` blanks the built-in `new_tab` binding (saving the original as a
comment), appends one marked `[[keys.command]]` block, backs up `config.toml`
first, rewrites it atomically, and is reversible with `uninstall-keys`.

## Outside herdr

The same picker runs in any terminal: type `cowbell`. It needs no herdr server.

- **Running here** lists herdr's agents for this directory when a herdr server is
  up (opening one focuses it in herdr), plus agent CLIs found running in plain
  terminals: processes on a tty whose working directory is here, skipping anything
  under herdr or nested inside another agent. Those rows say where they run
  (`on ttys012`); cowbell can't switch to another terminal window.
- **Start new** and **Resume** hand this terminal to the agent (`exec`), in the
  chosen directory. Quit the agent and you are back at your shell.
- **Resume** lists only sessions cowbell can actually resume. Claude started here is
  given its session id up front (`session_id_flag = "--session-id"`) and its name
  (`name_flag = "--name"`); agents without such a flag start fresh and are not listed.
- Inside a herdr pane, bare `cowbell` behaves like the popup and opens new tabs.

## CLI

```
cowbell                      the picker (on a terminal); piped, the long registry
cowbell status               long registry: live · cold · absent, session ids, trust
cowbell claude               attach to a claude harness here, or raise one with defaults
cowbell new --harness oll-mid --name kimi-notes
cowbell new --harness claude --model claude-opus-5 --lane green --allow Read,Grep --yes
cowbell enter <name>         focus if live, else resume (new tab in herdr, this terminal outside)
cowbell adopt [name]         put the focused agent pane on the roster (herdr)
cowbell stop <name>          ctrl-c; the roster keeps the row (herdr)
cowbell list [--all]
cowbell catalog              every harness with egress, installed?, trust badge
```

The house launcher is `code/bin/cowbell` → `bin/cowbell` here, which picks a
python ≥ 3.11 (tomllib) and follows symlinks. macOS `/usr/bin/python3` is 3.9 and
must not run this.

## How a harness is raised

1. Scope = the caller's cwd (realpath). A pane *above* it never counts.
2. Trust gate: `agent-deck-trust match <trust_tool> <cwd>`.
3. `herdr tab create --workspace <caller ws> --cwd <cwd> --label <sitting> --no-focus`.
   The **tab label is the sitting** (sidebar identity). cowbell never
   `--title`s or pane-renames a pane to carry identity — Grok's terminal title
   is the prompt; Claude's is whatever `/rename` last set.
4. Native kinds: `herdr agent start <sitting> --kind <kind> --pane <id> -- <argv>`
   so herdr detects the process and the sidebar reports working / blocked / idle.
   Grok also gets `--agent <sitting-head>` when `~/.grok/agents/<head>.md` exists
   (`builder-minaret` → `builder`). House wrappers (`oll-*`, `local-*`, `wk-*`,
   `bt-*`): `herdr pane run <id> "<wrapper> …"`.
5. Roster row written; tab focused.

Sitting names must match `[a-z][a-z0-9_-]{0,31}` and be unique among live
tabs; the default is the harness id (`grok`, `claude`) with `-2`, `-3` suffixes.
`--name` is the sitting / tab label.

A fresh Claude in a never-trusted directory will sit `blocked` at Claude's own
"trust this folder" prompt. That is not cowbell's gate.

## Lineage

- `herdr-seats` (Sep 2026): persisted named sittings, name-first. cowbell
  absorbs its roster (`import-seats`) and adds directory scope, absence, the
  constitution, and the new-tab entry point.
- `softwarecrafts/herdr-tab-new`: cwd-scoped resume-or-start with `--kind`;
  acts by default where cowbell reports. Its at-or-beneath workspace rule is
  reused here.
- Naming: `metawork/nomogenesis/ballots/2026-09-11-herdmark.md` (chosen as
  herdmark 2026-09-11; reopened 2026-09-13 → **herdr-cowbell** / **cowbell**).

## Development

```bash
python3 -m unittest discover -s tests -v
herdr plugin log list --plugin herdr-cowbell
```

Stdlib only.
