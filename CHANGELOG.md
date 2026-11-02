# Changelog

## Unreleased

- Vendor cowbell 0.3.2 from `d5ce185` (was `787ae8b`): the launch sheet. `n`, and `o` on a New Session row, show every setting an agent takes and the command it will run before it starts; `o` on a Resume row resumes with changed settings and rewrites the roster row. New settings: effort (`effort_flag`, `efforts`, `cowbell new --effort`) and other directories (`add_dir_flag`, `cowbell new --add-dir`). The seed catalog gains an `omp` (oh-my-pi) entry, and pi resumes the session it names. Also: a resume passes the model id it launched with, never a display name such as "Sonnet 5"; the hint bar fits the popup; two sessions with one title keep it for one and give the rest a session-id tail.
- Vendor cowbell 0.3.2 from `37edc4d` (was `24427b4`). Resume reads `$CLAUDE_CONFIG_DIR` when it is set, and `~/.claude` when it is not. The same pin includes resume in a still-present pane, `catalog --check`, and a New Session list that names the harness first and fits the window.
- Vendor cowbell 0.3.2: a pane border follows the current tab sitting; a bare label that is not the tab is stale; `identity sync` restamps `sitting · kind` on `pane.agent_detected`.
- Restamp or clear `$glyph` when herdr reports `pane.agent_detected` or `pane.exited`, so a tab that swaps harnesses does not keep the old mark.
- Restamp live `$glyph` from detector kind on plugin startup.
- Vendor cowbell 0.3.1 through `fab919d` (picker Lobe marks, Resume scoped to the launch uid, picker `e` starts).

## 0.3.1 — 2026-09-30

- Reassert `prefix+c` on herdr startup when a prior `install-keys` claim exists and an upgrade rewrote `config.toml`.
- Vendor cowbell 0.3.1: history-discovered resume, advisory over-privilege, adopt via the focused pane.
- Stranger README and MIT licence.
- Plugin is a thin skin over the vendored core.
