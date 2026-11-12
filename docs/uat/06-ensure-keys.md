# 06 — ensure-keys on server start

The plugin registers a startup hook: `./bin/cowbell keys ensure`. It reasserts the **prefix+c** binding when a prior `install-keys` claim exists and an upgrade clobbered `config.toml`; it does nothing without a claim.

## Preconditions

- Ability to restart or start the herdr server (or trigger plugin startup hooks).
- For the positive path: complete scenario **04** so `~/.config/cowbell/prefix-c.claimed` exists and config contains the cowbell block.

## Steps — no claim (negative)

1. Ensure **no** claim file:

   ```bash
   rm -f ~/.config/cowbell/prefix-c.claimed
   ```

2. Use a minimal config without cowbell markers, e.g.:

   ```bash
   printf '%s\n' '[keys]' 'new_tab = "prefix+c"' > /tmp/minimal.toml
   cp /tmp/minimal.toml ~/.config/herdr/config.toml
   ```

3. Restart herdr server (or run the ensure path your build uses on startup). **UNCLEAR:** exact restart command varies — `herdr server stop` / `herdr server start`, or service manager.

4. Read `~/.config/herdr/config.toml` — it should still match the minimal file (no cowbell block added).

## Steps — claim present, config clobbered (positive)

1. Run `install-keys` again (scenario 04) if needed so claim exists and block is present.
2. Simulate a herdr upgrade that **removes** the `[keys]` table and cowbell block while leaving unrelated stanzas:

   ```bash
   cp ~/.config/herdr/config.toml /tmp/with-keys.toml
   # Manually edit: delete [keys], new_tab, and the herdr-cowbell block; keep [theme] etc.
   ```

   Or overwrite with something like:

   ```bash
   printf '%s\n' '[theme]' 'name = "default"' > ~/.config/herdr/config.toml
   ```

3. Trigger ensure:

   - **Option A:** Restart herdr server (startup hook runs `keys ensure`).
   - **Option B (developer):** From the plugin checkout or install dir, run:

     ```bash
     ./bin/cowbell keys ensure
     ```

     When a live server is up, ensure may call `herdr server reload-config` automatically for the real config path.

4. Inspect config and stdout.

## Expected

| Check | Expected |
| --- | --- |
| No claim | `ensure` is a silent no-op; config bytes unchanged (see `test_ensure_without_claim_leaves_config_alone`). |
| Claim + clobber | Config regains `[keys]` with `new_tab = ""  # cowbell`, saved line if applicable, and `herdr-cowbell.open` on `prefix+c`; stdout includes `reasserted prefix+c binding in … (backup: …)` when a write was needed. |
| Reload | When ensure mutates the live herdr config and server is up, reload is attempted (or prints `run: herdr server reload-config` if server down). |
| Claim retention | Claim file still present after positive ensure. |

## Pass

- Negative path leaves config untouched.
- Positive path restores binding exactly as install would, with backup on write.

## Fail

- Ensure adds cowbell bindings without a claim file.
- Claim present but config stays clobbered after server start.
- Ensure deletes unrelated config sections.
