# 05 — uninstall-keys

Release **prefix+c** back to herdr’s built-in new-tab binding; remove claim and marked block; preserve backup discipline.

## Preconditions

- Scenario **04** completed (keys installed, claim file present).
- Keep the backup from install for audit; copy current config:

  ```bash
  cp ~/.config/herdr/config.toml /tmp/herdr-config.with-keys
  ```

## Steps

1. Uninstall keys:

   ```bash
   herdr plugin action invoke herdr-cowbell.uninstall-keys
   ```

2. Compare restored file to the pre-install snapshot from scenario 04 (`/tmp/herdr-config.before` or `~/herdr-config-pre-uat.toml`):

   ```bash
   diff -u /tmp/herdr-config.before ~/.config/herdr/config.toml
   ```

3. Confirm claim removed:

   ```bash
   test ! -f ~/.config/cowbell/prefix-c.claimed && echo "claim absent"
   ```

4. Reload:

   ```bash
   herdr server reload-config
   ```

5. Press **prefix+c** and observe behaviour (should match pre-install new-tab, not cowbell).

## Expected

| Check | Expected |
| --- | --- |
| Stdout | `uninstalled prefix+c binding in … (backup: config.toml.bak-…)` — a **new** timestamped backup is written before the uninstall write (same `cmd_keys` backup rules as install). |
| `new_tab` | Restored **byte-for-byte** to the pre-install `config.toml` for the whole file when install used only cowbell’s text transforms (plugin unit test: `test_uninstall_restores_original`). |
| Markers | No `# >>> herdr-cowbell keybindings >>>` block; no lines ending with `# cowbell` or `# cowbell saved:` left behind. |
| Claim | `prefix-c.claimed` deleted. |
| Backup handling | Prior `config.toml.bak-*` from install **remains** on disk (uninstall adds another backup; old backups are not auto-deleted). **UNCLEAR:** operators may prune backups manually; document what you keep. |
| Key | **prefix+c** runs herdr new-tab again, not the picker. |

## Pass

- `diff -u` against pre-install is empty (or explains any intentional operator edits outside cowbell markers).
- Claim gone; marked block gone; new-tab binding behaves as before install.

## Fail

- `new_tab` left blank after uninstall.
- Cowbell block or claim file remains.
- **prefix+c** still opens cowbell after reload.
