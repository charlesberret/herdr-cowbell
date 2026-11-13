# Handoff
This repository is a herdr plugin that opens the cowbell picker.
Install stays in the README. This file does not repeat those steps.

## Versions

The 0.3.3 stamp was made by hand on 2026-11-13, because cowbell-core was still 0.3.2. Three places read `0.3.3`:

- `herdr-plugin.toml`, the `version` field
- `vendor/VENDORED.json`, the `version` field
- `vendor/cowbell.py`, `__version__`

The pin commit stays `06a5f3e`. In `vendor/VENDORED.json`, `dirty` stays `false`.

Run `scripts/vendor-core` only from a core whose `__version__` is at least `0.3.3`. Do not hand-edit `vendor/`.

## Doctor

A missing prefix-c claim is a pass, because `install-keys` was never run and the startup ensure leaves `prefix+c` alone. Doctor does not create a claim, write herdr config, or reload. An empty agent list passes the identity stamp. A live agent with no glyph fails that line. A snapshot that cannot be read fails that line. It is not treated as an empty herd. A snapshot value that is not an object fails the identity line and does not raise. A missing snapshot key is still an empty herd.

## Tests

From the repo root. sudo: no.

```bash
python3 -m unittest discover -s tests -v
python3 tests/test_plugin.py
```

Both commands should exit 0. They need Python 3.11 or newer, the same bar as the plugin. They do not need a herdr server, a config directory, or a network.

## Where a change goes

A change belongs in `herdr_cowbell.py`, `herdr-plugin.toml`, `tests/`, `README.md`, `CHANGELOG.md`, `SECURITY.md`, or `bin/cowbell`. Nothing in this file asks you to edit them just to read it.

Continuous integration is `.github/workflows/test.yml`. It runs `python3 -m unittest discover -s tests -v` on push and pull request, for Python 3.11 and the current 3.x.

Do not add an unattended beacon. This is a client plugin, so it carries no tag step of its own. The repo is still tagged per release; the patron cuts each tag.
