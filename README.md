# cowbell

Call up a sitting here. Herdr plugin **herdr-cowbell**, CLI **cowbell**.

A cowbell is how a herder hears where each animal is (silence means one is
missing) and rings to call them in. Per-directory herd registry behind the
new-tab key: attach to an established harness, or raise one with the model,
lane, and tools chosen on purpose.

Press `prefix+c` in a directory and cowbell answers three questions before
anything is created: **what is raised here** (live agents whose cwd is this
directory or below), **what is cold here** (harnesses with a resumable
session), and **what is absent** (every harness the constitution allows,
with its trust verdict for this path). A number attaches or raises with
defaults; `[w]` is the long wizard (model, lane).

```
cowbell  ~/code/notes/nomogenesis   subject: internal
  git root: ~/code/notes   (4 live under it)   [g] widen

  LIVE
    1  nomogenesis-claude     claude       idle     w11:p3

  COLD
    2  kimi-ballots           oll-mid      cold     —

  RAISE  (yellow · sitting asked once)
    3  grok                   ↑!
    4  agy                    ↑ok
    5  oll-mid                ↑ok
    ⊘  local-ornith

  number = attach or raise   [w] wizard   [a] adopt   [g] widen   [q]
```

Bare `cowbell` still prints the long registry (session ids, cwd, the absent line). `prefix+c` is the short list: a number attaches or raises, and the popup closes.

## Vocabulary

| Word | Means |
|------|-------|
| **harness** | A way of raising an agent: executable (kind or house wrapper), model, lane, tools, trust surface. Declared in `harnesses.toml`. |
| **constitution** | The catalog of harnesses that *may* be raised — `harnesses.toml` in the plugin config dir. Hand-edited. |
| **roster** | What *has* been raised and where — `roster.json` in the plugin state dir. Machine-written; one row per named harness with its cwd, session id, pane. |
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
herdr plugin link ~/code/herdr-cowbell-core
herdr plugin action invoke herdr-cowbell.install-keys    # prefix+c → registry popup
herdr server reload-config
cowbell import-seats                                     # optional: absorb herdr-seats' roster
```

`install-keys` blanks the built-in `new_tab` binding (saving the original as a
comment), appends one marked `[[keys.command]]` block, backs up `config.toml`
first, rewrites it atomically, and is reversible with `uninstall-keys`.

## CLI (works outside Herdr too, as long as a server is running)

```
cowbell                      registry for the current directory
cowbell claude               attach to a claude harness here, or raise one with defaults
cowbell new --harness oll-mid --name kimi-notes
cowbell new --harness claude --model claude-opus-5 --lane green --allow Read,Grep --yes
cowbell enter <name>         focus if live, else resume in a new tab
cowbell adopt [name]         put the focused agent pane on the roster
cowbell stop <name>          ctrl-c; the roster keeps the row
cowbell list [--all]
cowbell catalog              every harness with egress, installed?, trust badge
```

The house launcher is `code/bin/cowbell` → `bin/cowbell` here, which picks a
python ≥ 3.11 (tomllib). macOS `/usr/bin/python3` is 3.9 and must not run this.

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
