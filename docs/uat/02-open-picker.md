# 02 — open picker (empty directory and with agents)

Exercise `herdr-cowbell.open`: the popup picker scoped to the caller’s directory, mirroring cowbell-core flows for an empty tree, resume rows, and new sessions — but inside herdr’s popup and landing in a **named tab** in the correct workspace.

## Preconditions

- Scenario **01** (or equivalent) completed: plugin installed.
- herdr server running; at least one workspace available.
- Optional for “with agents”: one agent CLI allowed in `harnesses.toml` / place rules, or an existing stopped session in state for the test directory.

## Steps — empty directory

1. Create an empty project directory and enter it in a herdr pane (or focus a pane whose cwd is that directory):

   ```bash
   mkdir -p /tmp/cowbell-uat-empty && cd /tmp/cowbell-uat-empty
   ```

2. Open the picker without keybinding (action invoke):

   ```bash
   herdr plugin action invoke herdr-cowbell.open
   ```

3. Observe the popup: title bar shows cowbell branding; **Active** may be empty or a light census box with no selectable numbered rows.
4. Confirm **New Session** lists allowed harnesses (or an empty list if none configured).
5. Press `Esc` to close without launching.

## Steps — resume or new session

1. Use a directory where a prior cowbell session exists **or** configure `allow_harness` for a harness you can start.
2. Invoke `herdr-cowbell.open` again from a pane cwd in that directory.
3. If **Resume** shows a row, select it (Enter or mouse). If not, pick a **New Session** row and confirm (Enter or launch sheet + start).
4. After launch, inspect herdr: a **new tab** should appear with a name derived from cowbell’s sitting name (not an anonymous shell tab).
5. Confirm the new tab’s cwd matches the directory you opened from (or the place rule for that project).

## Expected

| Check | Expected |
| --- | --- |
| Popup placement | Overlay popup (`placement = popup` in manifest); not a full-workspace takeover. |
| Cwd scope | Picker headings and rows reflect the invocation directory (Active census scoped under that cwd). |
| Empty tree | Resume / New Session sections still render; no crash on zero resume rows. |
| Launch | Chosen resume or new session opens in a **new herdr tab** in the active workspace (or `workspace_label` from place config when set). **UNCLEAR:** exact tab naming string varies by harness and sitting name — record the label you see. |
| Close | `Esc` dismisses popup without starting a process. |

## Pass

- Popup opens from the action in both empty and populated cases.
- A deliberate launch creates a new named tab in the expected workspace with the agent or shell cowbell selected.

## Fail

- Popup fails to open (`cowbell: …` on stderr, non-zero invoke).
- Launch replaces the current pane instead of opening a tab when `COWBELL_MODE=tab` (herdr path).
- Picker cwd does not match the focused pane’s project directory.
