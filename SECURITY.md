# Security

## `install-keys`

`herdr plugin action invoke herdr-cowbell.install-keys` edits `~/.config/herdr/config.toml`. It:

- blanks herdr's built-in `new_tab` binding (`prefix+c`)
- comments the original value so uninstall can put it back
- appends a marked block that binds `prefix+c` to `herdr-cowbell.open`
- writes `~/.config/cowbell/prefix-c.claimed`

Before writing, it copies `config.toml` to `config.toml.bak-<timestamp>` in the same directory.

`uninstall-keys` restores the original `new_tab` binding, removes the marked block, and deletes the claim file.

`ensure` (plugin startup) only rewrites when that claim file exists. A fresh install does not steal the key.

## Reporting a problem

Open a private GitHub security advisory on [charlesberret/herdr-cowbell](https://github.com/charlesberret/herdr-cowbell), or email the maintainer named in `LICENSE`.
