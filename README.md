# herdr-cowbell

Launch or re-launch a coding agent in this directory with the settings you choose, and see the exact command before it runs.

`herdr-cowbell` is a plugin for [herdr](https://github.com/herdrdev/herdr). It opens a popup picker for the directory you are in. The same picker runs outside herdr as the `cowbell` command, which lives in [cowbell-core](https://github.com/charlesberret/cowbell-core).

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

**`install-keys` takes over `prefix+c`**, which is herdr's own new-tab binding. It is a separate, opt-in step. It backs up `config.toml`, blanks the built-in binding, keeps the original as a comment, and appends one marked block. It also writes `~/.config/cowbell/prefix-c.claimed`. After that, a herdr server start reasserts the block if an upgrade rewrote `config.toml` and gave `prefix+c` back to the bare new-tab command. `uninstall-keys` restores the original binding and forgets the claim. Skip this if you would rather keep `prefix+c` as it is, and bind the `open` action yourself.

**Requirements.** herdr 0.9.0 or newer, and python 3.11 or newer on your PATH. macOS ships `/usr/bin/python3` at 3.9, which is too old. `brew install python` or `uv python install 3.12` is enough. No other dependencies. The plugin is stdlib Python. It runs on macOS and Linux.

If you have previously run `herdr plugin link` on a local copy, unlink it first. herdr refuses to install over a linked plugin.

The popup shows every coding agent already working in this directory, every one that stopped here and can pick up, and the ones you can start. Resume and New Session land in a new herdr tab.

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

  ⏎ open   n new   + add   a adopt   o shape   r rename   esc close
```

`o` on a row opens its **launch sheet** (shown at the popup's width, with model, effort and permissions changed):

```
  api  claude · Claude Code  new tab in /home/you/api

  ▶  start
     name           api
     model          claude-opus-5
     effort         high
     permissions    green
     tools allow    all
     tools deny     none
     also in        none
     seat file      none
     runs as        you

  $ claude --model claude-opus-5 --effort high
    --permission-mode plan

  ⏎ start   d defaults   a save as   esc cancel
```

## Status

Pre-1.0. The plugin is version 0.3.x and vendors a pinned copy of the cowbell command (see `vendor/`). Interfaces may still change between releases; changes are listed in [CHANGELOG.md](CHANGELOG.md).

## What the three sections mean

- **Active.** Agents herdr can see whose working directory is this one or below it. It is a census in a light-gray box. The rows are not numbered and cannot be chosen. Cowbell cannot take over a session that is already running.
- **Resume.** Agents that ran here before and stopped. Gold heading. Choosing one restarts it with its previous session, where the agent supports that. `o` opens the launch sheet over the recorded settings first, so you can resume with another model, effort, permission level, tool list or extra directories; the roster then remembers what ran.
- **New Session.** The agents you have configured, most-used-here first. Green heading. Enter starts one immediately with an automatic name and the agent's defaults. `n`, or `o` on the row, opens the launch sheet instead: one screen with every setting that agent takes (name, model, effort, permissions, allowed and denied tools, other directories, the user it runs as) and the exact command it will run. `o` starts from how you last launched that agent here. Settings at their default are dim, changed ones are bright; `s` starts, `d` resets, `a` saves the shape as a new agent with its own row, Esc cancels. Blue numbers mark the rows you can open.

The popup is mouse-first: click or arrow to a row, Enter or the ⏎ button to open, Esc to close. Digits jump to a row, every hint in the bottom bar is clickable, and the list refreshes every two seconds without losing your place. A dim `! more reach than needed` marks an agent your trust checker calls over-privileged for this directory. It is advisory and never stops a launch; agents the checker blocks are left off. Neither appears without a checker.

## Which agents it can start

Agents are declared in `~/.config/cowbell/harnesses.toml`, which ships with entries for the common CLIs and is meant to be hand-edited:

```toml
[[harness]]
id = "codex"
kind = "codex"
title = "Codex"
model_flag = ["--model"]
resume = ["resume"]
```

A directory can also carry its own rules for which agents are allowed, and which herdr workspace its tabs open in:

```toml
[places.acme]
cwd = "~/code/acme"
workspace_label = "acme"
allow_harness = ["claude", "grok"]
```

Configuration lives in `~/.config/cowbell/`, and what has been started is remembered in `~/.local/state/cowbell/`. `COWBELL_CONFIG_DIR` and `COWBELL_STATE_DIR` override both. `NO_COLOR` turns colour off.

## The `cowbell` command

The picker is a standalone terminal program too, useful outside herdr or over ssh. Run `cowbell` from a shell; install steps are in the [cowbell-core README](https://github.com/charlesberret/cowbell-core#install). Inside herdr it opens a tab. Elsewhere it replaces the current shell with the agent you picked. The plugin ships a pinned copy, and if the `cowbell` on your PATH is the same version or newer, the plugin runs that one instead. Upgrading the command upgrades the popup. Both read the same configuration.

## What it does not do

- It does not replace herdr's agent detection. Live rows come from herdr itself; this plugin adds directory scope and a memory of what ran where.
- It does not build layouts or manage sessions beyond starting and finding them.
- It does not decide what an agent is allowed to touch. If a trust checker is configured it asks it and reports the answer; the ruling is not cowbell's.

## Troubleshooting

`cowbell doctor` checks the running herdr answers everything `herdr-plugin.toml` declares: the server version is at least `min_herdr_version`, the `pane.agent_detected`/`pane.exited` events and `popup` placement exist in the server's API schema, the plugin context env keys are populated, and the startup hooks have run. Each line prints `pass` or `FAIL` and the command exits non-zero on any miss — a FAIL names the element this herdr does not answer, which means an upgrade is needed or the server predates that surface.

## Development

```bash
git clone https://github.com/charlesberret/herdr-cowbell
herdr plugin link ./herdr-cowbell
python3 -m unittest discover -s tests -v
```

The plugin is a thin skin: `herdr-plugin.toml` declares the actions and the popup, `herdr_cowbell.py` holds the herdr-side commands, and `vendor/` carries the pinned picker with the commit it came from. Issues and pull requests are welcome.

## About

Built by [Charles Berret](https://github.com/charlesberret) as a third-party plugin for herdr; not affiliated with or endorsed by the herdr project. MIT licensed; see [LICENSE](LICENSE).
