# 03 — adopt

Put the **focused agent pane** onto the cowbell roster via `herdr-cowbell.adopt`, and confirm a second adopt is idempotent.

## Preconditions

- Plugin installed; herdr server running.
- A pane running an **agent** herdr can detect (not a plain shell), focused in the workspace under test.
- That sitting is **not** already on the roster (or note its name before starting).

## Steps

1. Focus the agent pane. Note `pane_id` if your herdr build exposes it in the UI or via `herdr api snapshot`.
2. Adopt from the plugin action (pane context):

   ```bash
   herdr plugin action invoke herdr-cowbell.adopt
   ```

   **UNCLEAR:** some builds require invoking from pane context menus only; if CLI invoke fails, use the UI action bound to the same command.

3. From any shell with cowbell config available, list roster:

   ```bash
   cowbell status
   ```

   Or run the plugin shim:

   ```bash
   "$(herdr plugin path herdr-cowbell)/bin/cowbell" status
   ```

   **UNCLEAR:** exact path helper for installed plugins — use your herdr version’s plugin directory layout.

4. Repeat step 2 **twice more** on the same focused pane without restarting the agent.

5. Run `cowbell status` again.

## Expected

| Check | Expected |
| --- | --- |
| First adopt | Stdout along the lines of `adopted <name>  <kind>  <cwd>` (from core `cmd_adopt`). |
| Roster | New or updated row for the sitting name with cwd matching the pane’s project directory. |
| Second adopt | No duplicate roster rows for the same logical sitting; no error. **Expected from code:** adopt updates or no-ops on the same pane identity rather than multiplying entries. |
| Focus | Adopt does **not** re-exec or change the pane’s unix user (see plugin tests). |

## Pass

- First adopt adds or refreshes the roster entry for the focused agent pane.
- Repeated adopts do not create conflicting duplicates or errors.

## Fail

- Adopt with a focused agent pane exits non-zero.
- Roster never shows the sitting after adopt.
- Second adopt corrupts roster state or crashes the server.
