# UAT results — herdr-cowbell

| Field | Value |
| --- | --- |
| Plugin version | (from `herdr plugin list` or `herdr-plugin.toml`) |
| herdr server version | (from `herdr status`) |
| Python on PATH | (`python3 --version`) |
| OS | |
| Runner | (initials or role — no internal agent names) |
| Date | |

## Summary

| Scenario | Pass | Fail | Skip | Notes |
| --- | :---: | :---: | :---: | --- |
| [01 — stranger install](01-stranger-install.md) | | | | |
| [02 — open picker](02-open-picker.md) | | | | |
| [03 — adopt](03-adopt.md) | | | | |
| [04 — install-keys](04-install-keys.md) | | | | |
| [05 — uninstall-keys](05-uninstall-keys.md) | | | | |
| [06 — ensure-keys](06-ensure-keys.md) | | | | |
| [07 — vendor pin](07-vendor-pin.md) | | | | |
| [08 — uninstall plugin](08-uninstall-plugin.md) | | | | |

## Blockers

List any **Fail** or **Skip** with enough detail to reproduce (commands, stderr, config snippets). Omit secrets and private hostnames.

## Outcome

- [ ] All required scenarios **Pass**
- [ ] Documented waivers for **Skip** with owner approval
- [ ] Ready for external sign-off step (separate process)
