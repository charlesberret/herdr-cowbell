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

## `install-font`

`herdr plugin action invoke herdr-cowbell.install-font` runs `cowbell font install` (wired in `herdr-plugin.toml` through `./bin/cowbell`). The behaviour and pins live in the vendored core (`vendor/cowbell.py`):

- **What is fetched.** One HTTPS GET to a GitHub release asset URL on `github.com/hschne/lobe-icons-font` (Lobe Icons Font, MIT). The release tag, filename (`lobe-icons.ttf`), URL, and expected SHA-256 are constants in the vendored core; the command refuses any response whose hash does not match the pin.
- **What is written.** The verified bytes are written as `lobe-icons.ttf` in the user’s primary font directory: on macOS `~/Library/Fonts`; on Linux `$XDG_DATA_HOME/fonts` if set, otherwise `~/.local/share/fonts`. Parent directories are created as needed. A temporary `.part` file is renamed into place. On Linux, when `fc-cache` is on `PATH`, the install directory is refreshed.
- **Integrity.** SHA-256 of the downloaded file must equal the pinned digest before anything is written. If a file already exists at the destination and its hash matches the pin, the command exits without downloading again.
- **Undo.** Delete `lobe-icons.ttf` from the directory where install wrote it (see `cowbell font status` or the install success message). There is no separate uninstall action; cowbell falls back to one ASCII letter per agent kind when the font is absent.

Network use is limited to that single pinned URL during install (plus whatever your system needs for TLS). The font file is not executed; it is only read by your terminal emulator when you map the Lobe codepoint range.

## Reporting a problem

Open a private GitHub security advisory on [charlesberret/herdr-cowbell](https://github.com/charlesberret/herdr-cowbell), or email the maintainer named in `LICENSE`.
