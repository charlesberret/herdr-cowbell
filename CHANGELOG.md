# Changelog

## Unreleased

- Vendor cowbell 0.3.2: a pane border follows the current tab sitting; a bare label that is not the tab is stale; `identity sync` restamps `sitting · kind` on `pane.agent_detected`.
- Restamp or clear `$glyph` when herdr reports `pane.agent_detected` or `pane.exited`, so a tab that swaps harnesses does not keep the old mark.
- Restamp live `$glyph` from detector kind on plugin startup.
- Vendor cowbell 0.3.1 through `fab919d` (picker Lobe marks, Resume scoped to the launch uid, picker `e` starts).

## 0.3.1 — 2026-09-30

- Reassert `prefix+c` on herdr startup when a prior `install-keys` claim exists and an upgrade rewrote `config.toml`.
- Vendor cowbell 0.3.1: history-discovered resume, advisory over-privilege, adopt via the focused pane.
- Stranger README and MIT licence.
- Plugin is a thin skin over the vendored core.
