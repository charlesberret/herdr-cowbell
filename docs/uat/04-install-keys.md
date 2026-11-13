# 04 — install-keys

Opt in to `prefix+c` opening the cowbell picker. Verify `config.toml` diff, backup file, claim marker, reload, and key behaviour.

## Preconditions

- Plugin installed.
- herdr `config.toml` at `~/.config/herdr/config.toml` (or `XDG_CONFIG_HOME` equivalent).
- **Before this scenario:** `config.toml` must **not** yet contain the marked cowbell key block or `# cowbell` markers from a prior install (use a backup or fresh config).
- Save a copy: `cp ~/.config/herdr/config.toml ~/herdr-config-pre-uat.toml`.

## Steps

1. Capture a before snapshot:

   ```bash
   cp ~/.config/herdr/config.toml /tmp/herdr-config.before
   grep -n 'new_tab\|herdr-cowbell\|cowbell' /tmp/herdr-config.before || true
   ```

2. Install keys:

   ```bash
   herdr plugin action invoke herdr-cowbell.install-keys
   ```

3. Note printed backup name (`config.toml.bak-YYYYMMDD-HHMMSS` in the same directory).
4. Diff before vs after:

   ```bash
   diff -u /tmp/herdr-config.before ~/.config/herdr/config.toml
   ```

5. Confirm claim file:

   ```bash
   cat ~/.config/cowbell/prefix-c.claimed
   ```

6. Reload herdr config:

   ```bash
   herdr server reload-config
   ```

7. Focus a normal pane and press **prefix+c** (your herdr prefix, then `c`).

## Expected

| Check | Expected |
| --- | --- |
| Stdout | `installed prefix+c binding in … (backup: config.toml.bak-…)` and reminder to run `herdr server reload-config` (install itself may print that even if you reload in step 6). |
| Backup | Timestamped `config.toml.bak-*` exists beside `config.toml`; original file preserved in backup. |
| `new_tab` | Built-in `new_tab` under `[keys]` is set to empty string with trailing `# cowbell` marker; prior value kept on a `# cowbell saved: …` line when one existed. |
| Key block | Appended marked block `# >>> herdr-cowbell keybindings >>>` … `prefix+c` → `type = "plugin_action"`, `command = "herdr-cowbell.open"` … `# <<< herdr-cowbell keybindings <<<`. |
| Other bindings | Existing `[[keys.command]]` entries (e.g. other plugins) unchanged in content. |
| Claim | `~/.config/cowbell/prefix-c.claimed` exists with the claim body from plugin source. |
| Diff scope | **Only** the cowbell-managed key changes plus the appended block (no unrelated edits). |
| After reload | herdr accepts config; **prefix+c** opens the cowbell popup (same as scenario 02). |
| Idempotence | Running `install-keys` again prints `already installed` and does not multiply blocks (per `keys_install_text` idempotence). |

## Pass

- Diff matches the install contract above; backup and claim exist; reload succeeds; **prefix+c** opens the picker.

## Fail

- `config.toml` changes before any install-keys invoke.
- `new_tab` not saved/restorable pattern; block missing `herdr-cowbell.open`.
- No backup on first install.
- **prefix+c** still runs plain new-tab after reload.
