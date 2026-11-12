# Troubleshooting herdr-cowbell

Messages and failure states taken from this plugin’s own code paths (`bin/cowbell`, `herdr_cowbell.py`, and the vendored `vendor/cowbell.py`). Each cause cites the file that emits or defines it.

## Install

### `cowbell: no python >= 3.11 found (need tomllib)`

**Cause.** The `bin/cowbell` shim tried every candidate interpreter on your machine and none reported Python 3.11 or newer (`bin/cowbell`).

**Fix.** Install a current Python (for example via your package manager or `uv python install 3.12`) and ensure its `python3` is on `PATH` before `/usr/bin/python3`. macOS system Python is too old for this plugin.

### `cowbell: python >= 3.11 required (tomllib)`

**Cause.** The vendored core was started with an interpreter below 3.11, so `tomllib` is missing (`vendor/cowbell.py`, `load_catalog`).

**Fix.** Same as above: run the plugin through `bin/cowbell`, which refuses old interpreters, or point `herdr` at a Python 3.11+ binary.

### `herdr-cowbell: vendor/cowbell.py has no __version__ (run scripts/vendor-core)`

**Cause.** The vendored copy is incomplete or was edited without a `__version__` line (`herdr_cowbell.py`, `vendored_version`).

**Fix.** On a development checkout, run `scripts/vendor-core` to refresh `vendor/`. On an install from GitHub, reinstall the plugin or report a broken release.

### `cowbell: could not download <url>: …`

**Cause.** `install-font` (or `cowbell font install`) could not fetch the pinned Lobe Icons Font over the network (`vendor/cowbell.py`, `cmd_font_install`).

**Fix.** Check network access and retry `herdr plugin action invoke herdr-cowbell.install-font`. You can still use the picker; marks fall back to single letters per agent kind.

### `cowbell: refused <url>: sha256 …, expected …`

**Cause.** The downloaded font bytes did not match the pinned checksum (`vendor/cowbell.py`, `cmd_font_install`).

**Fix.** Retry once in case of a corrupted download. If it persists, open an issue with the URL and hashes from the message.

### `cowbell: no catalog at …`

**Cause.** The harness catalog file is missing under your cowbell config directory (`vendor/cowbell.py`, `load_catalog`). A normal first run should seed it from the vendored `harnesses.toml`.

**Fix.** Ensure `COWBELL_CONFIG_DIR` (if set) is writable, or remove a broken empty config tree so the plugin can recreate defaults. Reinstalling the plugin also restores the seed file in the package.

## Open

### `cowbell: <message>` (stderr, exit 1)

**Cause.** The `open` action called `herdr plugin pane open …` and the vendored core raised `HerdrError` with the server’s error text (`herdr_cowbell.py`, `cmd_open`; errors originate in `vendor/cowbell.py`, `herdr`).

**Fix.** Read the appended message: common cases include the server not running, socket path mismatch, or API rejection. Confirm `herdr status` shows a live server and that plugin panes are supported on your herdr version (see `cowbell doctor` below).

### `herdr unavailable: …`

**Cause.** The `herdr` binary could not be executed (`vendor/cowbell.py`, `herdr`).

**Fix.** Install herdr, put `herdr` on `PATH`, or set `HERDR_BIN_PATH` to the binary you use. The picker and `open` need a reachable CLI even when the server is down (they fail on the next step with a clearer error).

### `herdr … timed out after 5s`

**Cause.** A herdr subprocess did not finish within `HERDR_TIMEOUT` (`vendor/cowbell.py`, `herdr`).

**Fix.** Restart or upgrade a stuck herdr server. Heavy disk load or a wedged server can cause this when opening the popup.

### `herdr … exited <n>` / `herdr …: non-JSON output: …`

**Cause.** Herdr returned a non-zero status or output that is not valid JSON when JSON was expected (`vendor/cowbell.py`, `herdr`).

**Fix.** Run the same herdr subcommand manually for details. Upgrade herdr if the plugin expects a newer API shape.

### `FAIL server version unreachable >= …` (and other `FAIL` lines from `cowbell doctor`)

**Cause.** `cowbell doctor` compares the live server to `herdr-plugin.toml` (`herdr_cowbell.py`, `cmd_doctor`): minimum version, declared events, popup placement, context env keys in the API schema, startup hooks, claim file, and identity stamp on a live agent.

**Fix.** Upgrade herdr to at least `min_herdr_version` (currently declared in `herdr-plugin.toml`). Each `FAIL` line names the missing piece. The command exits non-zero until every check passes.

## Adopt

### `cowbell: ambiguous sitting '…'`

**Cause.** More than one live agent matched the name you passed (`vendor/cowbell.py`, `live_agent_to_adopt`).

**Fix.** Disambiguate with `cowbell adopt --pane ID` or a unique sitting name from the listing in the error.

### `cowbell: no live agent in pane <id>`

**Cause.** The pane id you passed (or the picker passed) has no agent herdr reports (`vendor/cowbell.py`, `live_agent_to_adopt`).

**Fix.** Confirm the pane is still running and herdr’s agent detector sees it (`herdr agent list` or the picker **Active** section). Adopt only works on live sittings, not roster history.

### `cowbell: no focused agent pane to adopt. Live …` (multi-line)

**Cause.** No single focused agent could be inferred: focus may be on a shell pane, several agents match the directory, or none are in scope (`vendor/cowbell.py`, `live_agent_to_adopt`).

**Fix.** Focus the agent pane you want, use `a` in the picker on a specific **Active** row, or run `cowbell adopt --pane ID` / `cowbell adopt <sitting-name>` as the footer suggests.

### `several running — cowbell adopt <name>` / `no running sitting here to adopt` (picker flash)

**Cause.** Picker **Adopt** (`a`) could not pick one pane: either no census row was eligible or multiple live agents need a name (`vendor/cowbell.py`, `adopt_picker_flash`).

**Fix.** Name the sitting on the command line, or focus one agent before pressing `a`.

### `cowbell: pane … hosts no recognized agent`

**Cause.** Herdr reported a pane but without a known `agent` kind (`vendor/cowbell.py`, `cmd_adopt`).

**Fix.** Wait for detection to settle, or start a supported CLI so herdr labels the pane. Generic shell panes cannot be adopted onto the cowbell roster.

## Keys and server reload

### `cowbell keys: unknown action '…' (install|uninstall|ensure)`

**Cause.** The keys subcommand received something other than `install`, `uninstall`, or `ensure` (`herdr_cowbell.py`, `cmd_keys`).

**Fix.** Use `herdr plugin action invoke herdr-cowbell.install-keys` / `uninstall-keys`, or `bin/cowbell keys install|uninstall|ensure`.

### `run: herdr server reload-config`

**Cause.** Keys were written to `config.toml` but the plugin did not reload the server automatically (`herdr_cowbell.py`, `cmd_keys` and `_reload_config_after_ensure`). Printed after `install-keys`, `uninstall-keys`, or when `ensure` could not reload.

**Fix.** Run `herdr server reload-config` so `prefix+c` (or the restored `new_tab` binding) takes effect.

### `cowbell: wrote the binding; reload-config failed (…)`

**Cause.** `keys ensure` updated the file but `herdr server reload-config` failed (`herdr_cowbell.py`, `_reload_config_after_ensure`).

**Fix.** Reload manually when the server is healthy. The on-disk binding is already correct.

### `reasserted prefix+c binding in … (backup: …)`

**Cause.** Startup hook `keys ensure` found a prior `install-keys` claim but the binding block was missing from `config.toml`, so it was rewritten (`herdr_cowbell.py`, `cmd_keys`). Typical after a herdr upgrade overwrote config.

**Fix.** No action required if you still want `prefix+c` on cowbell. Run `uninstall-keys` if you meant to release the claim.

### `FAIL startup keys.ensure answered (claim file)` (`cowbell doctor`)

**Cause.** Doctor expects `prefix-c.claimed` under your cowbell config dir after `install-keys` (`herdr_cowbell.py`, `cmd_doctor`). Missing claim means keys were never installed or the file was removed.

**Fix.** Run `install-keys` if you want the binding, or ignore this line if you open the picker only via `herdr plugin action invoke herdr-cowbell.open`.

### `cowbell: stop needs a running Herdr server`

**Cause.** The plugin `stop` command requires herdr’s server (`herdr_cowbell.py`, `plugin_main`; `vendor/cowbell.py`, `server_up`).

**Fix.** Start herdr, then run stop again. Exit code is 2.

### `cowbell: no roster entry '…'` / `cowbell: … is not live`

**Cause.** `stop` targets a roster name that does not exist or has no live pane id (`herdr_cowbell.py`, `cmd_stop`).

**Fix.** Use `cowbell list` for names. Stop only applies to sittings cowbell knows about that are currently running in herdr.
