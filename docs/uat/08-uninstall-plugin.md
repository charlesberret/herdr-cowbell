# 08 — uninstall plugin

Remove the plugin with herdr’s uninstall command. If keys were installed, config and claim should be restored; no stray plugin files in the herdr plugins directory.

## Preconditions

- Plugin installed (scenario **01**).
- Decide branch:
  - **A:** Keys never installed — uninstall only.
  - **B:** Keys installed (scenario **04**) — uninstall should still leave a sane `config.toml` (run `uninstall-keys` first if your process requires a clean key state; record which path you took).

## Steps

1. Note plugin install location:

   ```bash
   herdr plugin list
   ```

   **UNCLEAR:** path reporting varies by herdr version.

2. If keys are active, optionally capture config:

   ```bash
   cp ~/.config/herdr/config.toml /tmp/herdr-config.pre-uninstall
   ```

3. Uninstall:

   ```bash
   herdr plugin uninstall herdr-cowbell
   ```

4. Confirm removal:

   ```bash
   herdr plugin list
   ```

   `herdr-cowbell` should not appear.

5. Inspect filesystem under herdr’s plugins directory for leftover `herdr-cowbell` trees. **UNCLEAR:** default layout is version-specific — record the path you checked.

6. If scenario **B:** verify `~/.config/cowbell/prefix-c.claimed` and cowbell key markers. Expected policy:
   - **Ideal:** operator ran `uninstall-keys` before plugin uninstall; `config.toml` matches pre-keys snapshot.
   - **If keys left installed:** document whether herdr uninstall alone removes bindings (**UNCLEAR** — not implemented in this plugin repo; uninstall-keys is the supported restore path).

7. Confirm invoke fails:

   ```bash
   herdr plugin action invoke herdr-cowbell.open
   ```

## Expected

| Check | Expected |
| --- | --- |
| Uninstall exit | `0`; plugin absent from `herdr plugin list`. |
| Stray files | No remaining checkout under plugins dir for `herdr-cowbell` (herdr may leave empty parents — note what you see). |
| Config | If `uninstall-keys` was run first, `config.toml` has no cowbell key block and no claim file. |
| Cowbell state | Roster/state under `~/.local/state/cowbell/` may remain (user data); uninstall plugin does not delete user cowbell state — **expected**. |
| Open invoke | Fails with unknown plugin / action error. |

## Pass

- Plugin removed from herdr; open action unavailable; no orphaned plugin directory; key restore policy documented and satisfied for branch B.

## Fail

- Plugin still listed or actions still invoke after uninstall.
- `config.toml` left with blank `new_tab` and no way to restore after branch B without manual edit.
- Partial delete breaks herdr server start.
