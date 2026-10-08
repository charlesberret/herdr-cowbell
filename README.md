# herdr-cowbell

Pick an agent for this directory — running here, resume, or start new.

`herdr-cowbell` is a plugin for [herdr](https://github.com/herdrdev/herdr). The
same picker runs outside herdr as the `cowbell` command.

## Install

```bash
herdr plugin install charlesberret/herdr-cowbell
herdr plugin action invoke herdr-cowbell.open
```

That second line opens the picker. To reach it with a key instead:

```bash
herdr plugin action invoke herdr-cowbell.install-keys
herdr server reload-config
```

**`install-keys` takes over `prefix+c`**, which is herdr's own new-tab binding.
It is a separate, opt-in step: it backs up `config.toml`, blanks the built-in
binding (keeping the original as a comment), and appends one marked block. It
also writes `~/.config/cowbell/prefix-c.claimed`. After that, a herdr server
start reasserts the block if an upgrade rewrote `config.toml` and gave
`prefix+c` back to the bare new-tab command. `uninstall-keys` restores the
original binding and forgets the claim. Skip this if you would rather keep
`prefix+c` as it is, and bind the `open` action yourself.

**Requirements.** herdr 0.9.0 or newer, and python 3.11 or newer on your PATH.
macOS ships `/usr/bin/python3` at 3.9, which is too old; `brew install python`
or `uv python install 3.12` is enough. No other dependencies — the plugin is
stdlib Python.

If you have previously run `herdr plugin link` on a local copy, unlink it first:
herdr refuses to install over a linked plugin.

The popup shows every coding agent already working in this directory, every one
that stopped here and can pick up, and the ones you can start. Resume and New
Session land in a new herdr tab.

```
  ~/code/acme                                  g widen to ~/code · 4 running

  ┌─ Active ─────────────────────────────────────────────────────────────────┐
  │    api-claude             claude      idle                               │
  │    docs                   grok        working  14s   in ~/code/acme/docs │
  └──────────────────────────────────────────────────────────────────────────┘

  Resume
    1  migration              oll-mid     2 days ago

  New Session
    2  claude                 Claude Code
    3  oll-mid                kimi k2.7 cloud — default coding
    4  grok                   Grok CLI                           ! more reach than needed
       + 9 more

  ⏎ open   n new with options   g widen   esc close
```

## What the three sections mean

- **Active** — agents herdr can see whose working directory is this one or
  below it. A census in a light-gray box: the rows are not numbered and cannot
  be chosen. Cowbell cannot take over a sitting that is already running.
- **Resume** — agents that ran here before and stopped. Gold heading. Choosing
  one restarts it with its previous session, where the agent supports that.
- **New Session** — the agents you have configured, most-used-here first. Green
  heading. Enter starts one immediately with an automatic name; `n` asks for
  name, model and permissions first. Blue numbers mark the rows you can open.

The popup is mouse-first: click or arrow to a row, Enter (or the ⏎ button) to
open, Esc to close. Digits jump to a row, every hint in the bottom bar is
clickable, and the list refreshes every two seconds without losing your place.
A dim `! more reach than needed` marks an agent your trust checker calls
over-privileged for this directory. It is advisory and never stops a launch;
agents the checker blocks are left off. Neither appears without a checker.

## Which agents it can start

Agents are declared in `~/.config/cowbell/harnesses.toml`, which ships with
entries for the common CLIs and is meant to be hand-edited:

```toml
[[harness]]
id = "codex"
kind = "codex"
title = "Codex"
model_flag = ["--model"]
resume = ["resume"]
```

A directory can also carry its own rules — which agents are allowed, which herdr
workspace its tabs open in:

```toml
[places.acme]
cwd = "~/code/acme"
workspace_label = "acme"
allow_harness = ["claude", "grok"]
```

Configuration lives in `~/.config/cowbell/`, and what has been started is
remembered in `~/.local/state/cowbell/` (`COWBELL_CONFIG_DIR` /
`COWBELL_STATE_DIR` override both). `NO_COLOR` turns colour off.

## The `cowbell` command

The picker is a standalone terminal program too, useful outside herdr or over
ssh. Run `cowbell` from a shell; inside herdr it opens a tab, elsewhere it
replaces the current shell with the agent you picked. The plugin ships a pinned
copy, and if the `cowbell` on your PATH is the same version or newer, the plugin
runs that one instead — so upgrading the command upgrades the popup. Both read
the same configuration.

## What it does not do

- It does not replace herdr's agent detection. Live rows come from herdr itself;
  this plugin adds directory scope and a memory of what ran where.
- It does not build layouts or manage sessions beyond starting and finding them.
- It does not decide what an agent is allowed to touch. If a trust checker is
  configured it asks it and reports the answer; the ruling is not cowbell's.

## Development

```bash
git clone https://github.com/charlesberret/herdr-cowbell
herdr plugin link ./herdr-cowbell
python3 -m unittest discover -s tests -v
```

The plugin is a thin skin: `herdr-plugin.toml` declares the actions and the
popup, `herdr_cowbell.py` holds the herdr-side commands, and `vendor/` carries
the pinned picker with the commit it came from. Issues and pull requests are
welcome.

## About

Built by [Charles Berret](https://github.com/charlesberret) as a third-party
plugin for herdr; not affiliated with or endorsed by the herdr project. MIT
licensed.
