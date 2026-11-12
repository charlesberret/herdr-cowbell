# 01 — stranger install

Install the plugin from the public GitHub remote on a machine that does **not** already have a local `herdr plugin link` to this tree.

## Preconditions

- herdr server is running; `herdr plugin list` does **not** show `herdr-cowbell` (or you have removed a prior install).
- No `herdr plugin link` pointing at a checkout of this repository (herdr refuses install over a link).
- **Precondition (remote):** the repository `charlesberret/herdr-cowbell` is reachable as a public GitHub remote. If the repo is private or missing, expect `herdr plugin install` to fail — the scenario is still valid to record as **Skip** with that reason.

## Steps

1. Note whether any linked copy exists: `herdr plugin list` (look for `source.kind` of `link` vs `github`).
2. Install from GitHub:

   ```bash
   herdr plugin install charlesberret/herdr-cowbell
   ```

3. Inspect registration:

   ```bash
   herdr plugin list
   ```

   If your herdr build exposes JSON:

   ```bash
   herdr plugin list --json
   ```

4. Invoke open once (sanity, not the focus of this scenario):

   ```bash
   herdr plugin action invoke herdr-cowbell.open
   ```

## Expected

| Check | Expected |
| --- | --- |
| Install exit code | `0` when the remote is public and requirements are met. |
| Plugin id | `herdr-cowbell` appears in `herdr plugin list`. |
| Install source | Record shows remote install, not a local link. The issue spec calls for `source.kind = "github"` in plugin metadata. **UNCLEAR:** exact field names and JSON shape depend on herdr CLI version — capture the listing you get and attach to results. |
| Open | Cowbell popup appears (see scenario 02 for deeper checks). |

## Pass

- Install succeeds on a machine with no prior link.
- Metadata indicates GitHub (or equivalent remote) source, not `link`.

## Fail

- Install errors while the remote is known public and herdr version satisfies `min_herdr_version`.
- Plugin shows `link` source after a stranger install command.
