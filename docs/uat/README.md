# herdr-cowbell — user acceptance scenarios

Manual checks for the herdr plugin: install from a remote source, open the popup, adopt a focused pane, optional `prefix+c` keybinding takeover with backup and restore, startup `ensure-keys`, vendored core pin, and clean uninstall.

These scenarios complement automated tests in `tests/`. They are written for a **live herdr server** (0.9.0 or newer) and Python 3.11+ on the runner’s PATH.

## Who runs this

A patron or outsider with a normal user account, herdr installed, and network access to clone or install the plugin. No house-specific hostnames or credentials appear in this pack.

## Environment

| Requirement | Notes |
| --- | --- |
| herdr | `herdr status` shows a running server at or above the plugin’s `min_herdr_version` in `herdr-plugin.toml`. |
| Python | 3.11+ on `PATH` (the plugin shim refuses older system Pythons). |
| cowbell config | Default `~/.config/cowbell/` and state under `~/.local/state/cowbell/` unless you override with env vars. |
| Agents (optional) | Harness entries in `harnesses.toml` and at least one installed agent CLI for resume/new-session scenarios. |

Use a **throwaway herdr config** when scenarios edit `config.toml` (install-keys, uninstall-keys, ensure-keys). Copy aside `~/.config/herdr/config.toml` first if you rely on a production layout.

## How to run

1. Work through scenarios **01 → 08** in order where dependencies apply (keys scenarios assume a installed plugin; stranger install assumes no local link).
2. For each scenario, follow **Steps**, compare **Expected**, mark **Pass** or **Fail** on the sheet.
3. Copy [RESULTS-template.md](RESULTS-template.md) to `RESULTS.md` (local only; do not commit filled results unless your process requires it).

Invoke plugin actions the way end users do:

```bash
herdr plugin action invoke herdr-cowbell.<action-id>
```

Key-related actions map to `./bin/cowbell keys install|uninstall|ensure` inside the plugin tree.

## Scenario index

| # | File | What it exercises |
| --- | --- | --- |
| 01 | [01-stranger-install.md](01-stranger-install.md) | Remote `herdr plugin install`; `source.kind` for GitHub |
| 02 | [02-open-picker.md](02-open-picker.md) | `open` popup in an empty directory and with active agents |
| 03 | [03-adopt.md](03-adopt.md) | `adopt` on the focused agent pane; idempotence |
| 04 | [04-install-keys.md](04-install-keys.md) | `install-keys`; backup; reload; `prefix+c` opens picker |
| 05 | [05-uninstall-keys.md](05-uninstall-keys.md) | `uninstall-keys`; byte restore of `new_tab`; claim removed |
| 06 | [06-ensure-keys.md](06-ensure-keys.md) | Startup `ensure` after config clobber; no-op without claim |
| 07 | [07-vendor-pin.md](07-vendor-pin.md) | Vendored core version matches manifest |
| 08 | [08-uninstall-plugin.md](08-uninstall-plugin.md) | `herdr plugin uninstall`; config and files cleaned up |

## Ground rules (clerk)

| Action | Behaviour under test |
| --- | --- |
| `open` | Popup picker for the caller’s directory (`herdr-cowbell.open`). |
| `adopt` | Focused pane recorded on the cowbell roster. |
| `install-keys` | Opt-in: backup `config.toml`, blank built-in `new_tab`, add `prefix+c` → `herdr-cowbell.open`, write claim file. |
| `uninstall-keys` | Restore saved `new_tab`, remove marked block, delete claim. |
| `ensure-keys` | One-shot on server start: reassert binding only if claim file exists. |

`config.toml` must be **unchanged** by the plugin until `install-keys` runs. `uninstall-keys` must restore `new_tab` to its pre-install bytes (for the text the plugin saved).

## Sign-off

This pack is **authoring only**. Consolidated sign-off (patron + outsider, both core and plugin packs) is a separate QA step; do not treat a green sheet here as release approval.
