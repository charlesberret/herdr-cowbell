# Contributing
This repository is a herdr plugin that opens the cowbell picker.
Install stays in the README. This file does not repeat those steps.

## Versions

Version `0.3.3` was set by hand on 2026-11-13, because cowbell-core was still 0.3.2. Three places read `0.3.3`:

- `herdr-plugin.toml`, the `version` field
- `vendor/VENDORED.json`, the `version` field
- `vendor/cowbell.py`, `__version__`

The commit recorded in `vendor/VENDORED.json` stays `06a5f3e`, and its `dirty` field stays `false`.

Run `scripts/vendor-core` only from a cowbell-core checkout whose `__version__` is at least `0.3.3`. Do not hand-edit `vendor/`.

## Doctor

The terms: `install-keys` leaves a `prefix+c` claim file; the identity stamp publishes a glyph (a pane's agent-kind mark) for every live agent; a snapshot is what `herdr api snapshot` returns; an empty herd is a snapshot with no agents.

A missing prefix-c claim is a pass, because `install-keys` was never run and the startup ensure leaves `prefix+c` alone. Doctor does not create a claim, write herdr config, or reload. An empty agent list passes the identity stamp. A live herd fails that line when no agent carries a stamped glyph. A snapshot that cannot be read fails that line. It is not treated as an empty herd. A snapshot value that is not an object fails the identity line and does not raise. A missing snapshot key is still an empty herd.

## Tests

From the repo root. sudo: no.

```bash
python3 -m unittest discover -s tests -v
python3 tests/test_plugin.py
```

Both commands should exit 0. They need Python 3.11 or newer, the same bar as the plugin. If `python3 --version` is older, the Requirements line in the [README install section](README.md#install) says how to get a newer one; then run both commands with that interpreter in place of `python3`. They do not need a herdr server, a config directory, or a network.

## Where a change goes

A change belongs in `herdr_cowbell.py`, `herdr-plugin.toml`, `tests/`, `README.md`, `CHANGELOG.md`, `SECURITY.md`, or `bin/cowbell`. Nothing in this file asks you to edit them just to read it.

Continuous integration is `.github/workflows/test.yml`. It runs `python3 -m unittest discover -s tests -v` on push and pull request, for Python 3.11 and the current 3.x.

Do not add a background job that reports home on its own. This is a client plugin, so it carries no tag step of its own. The repo is still tagged per release; the maintainer cuts each tag.
