# History audit — public-release QA-C3 (LAB-625)

Read-only scan of every branch/tag that would go public. **No history was altered.**

## cowbell-core (`~/cloud/git-projects/cowbell`) — 118 commits, scanned 2026-10-17

| Pattern | Count | Severity | Evidence / remedy |
|---|---|---|---|
| Agent trailers (`Co-Authored-By`, `Generated with`, `Claude-Session`, `claude.ai/code`, `Signed-off-by`) | 0 | — | clean; the September hygiene pass landed here |
| House paths (`/Users/charles`, `~/cloud`, `cloud/sync`, `git-projects`) | 0 | — | clean |
| Overlay/private IPs | 0 | — | clean |
| Secret-shaped blobs (`.pem`, `.key`, `.env`, `credentials`, `.sqlite`, `.inbox/`) | 0 | — | never committed |
| House hostnames | 1 | cosmetic | merge subject `hesiod-wt/main` — `6316d23` |
| Linear ids (`LAB-n`) | 45 | house-internal | convention in subjects; harmless but references private board — `filter-repo --message-callback` if unwanted |
| `HANDOFF.md` blobs | ~45 commits | **house-internal** | sitting records tracked 2026-09-12 → removed `080b422` (2026-10-14), now gitignored; contents = session notes across the QA run — `filter-repo --path HANDOFF.md --invert-paths` |
| Patron personal emails | 3 commits | personal | `charles@berret.net` ×2, `charles.berret@gmail.com` ×1 vs noreply everywhere else — mailmap won't fix history |
| Agent identity author | ~9 commits | cosmetic | `Cursor Agent` authored commits on `cursor/*` PR branches |
| Fixture | 1 | none | `tests/fixtures/public-surface/dirty/house-handoff.txt` — a deliberately dirty fixture for the scanner |
| Stale refs going public | — | — | tags `v0.2.0`, `v0.3.0`, `v0.3.1`; `origin/butler/*` (2 stale), `origin/cursor/*` (5 draft-PR branches, alive); local `hesiod*` remotes do not push |

## herdr-cowbell (`~/cloud/git-projects/herdr-cowbell`) — 52 commits, scanned 2026-10-17

| Pattern | Count | Severity | Evidence / remedy |
|---|---|---|---|
| Agent trailers | 0 | — | clean |
| House paths | 0 | — | clean |
| IPs / hostnames | 0 | — | clean |
| Secret-shaped blobs / `.inbox/` / `HANDOFF.md` | 0 | — | never committed |
| Linear ids | 20 | house-internal | same convention — optional `--message-callback` |
| Patron personal email | 3 commits | personal | `charles.berret@gmail.com` ×3 |
| Agent identity author | 1 | cosmetic | `Cursor Agent` ×1 |
| Stale refs | — | — | tags `v0.2.0`, `v0.3.0`, `v0.3.1`; `origin/main` only |

## Verdict

| Repo | Rewrite needed? | Deciding fact |
|---|---|---|
| cowbell-core | **yes** | ~45 `HANDOFF.md` blobs (house sitting records) spread across Sep–Oct history; removal is a `--path` rewrite the patron must sanction |
| herdr-cowbell | **no** | nothing but 3 personal-email author lines and Linear ids — cosmetics the patron can accept without a rewrite |
