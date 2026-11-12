# 07 — vendor pin

The plugin version in `herdr-plugin.toml` must match the vendored `cowbell.py` `__version__` and `vendor/VENDORED.json`. Release hygiene expects an automated check script.

## Preconditions

- Repository checkout (linked plugin or clone). Installed-only strangers can read `vendor/VENDORED.json` and `herdr-plugin.toml` from the install directory.

## Steps

1. From the plugin root, run packaging tests:

   ```bash
   python3 -m unittest tests.test_plugin.Packaging.test_vendored_core_is_pinned_and_clean -v
   ```

2. Optional full plugin suite:

   ```bash
   python3 -m unittest discover -s tests -v
   ```

3. Manual cross-check:

   ```bash
   python3 -c "
   import json, re, tomllib, pathlib
   root = pathlib.Path('.')
   pin = json.loads((root/'vendor/VENDORED.json').read_text())
   man = tomllib.loads((root/'herdr-plugin.toml').read_text())['version']
   ver = re.search(r'^__version__ = \"([^\"]+)\"', (root/'vendor/cowbell.py').read_text(), re.M).group(1)
   print('VENDORED.json', pin['version'])
   print('manifest', man)
   print('cowbell.py', ver)
   print('dirty', pin.get('dirty'))
   assert pin['version'] == man == ver
   assert pin['dirty'] is False
   "

   ```

4. **If present in your tree**, run the vendor guard script:

   ```bash
   scripts/check-vendor.sh
   ```

## Expected

| Check | Expected |
| --- | --- |
| Unit test | `test_vendored_core_is_pinned_and_clean` passes. |
| Versions | `vendor/VENDORED.json` `version`, `herdr-plugin.toml` `version`, and `vendor/cowbell.py` `__version__` are **identical**. |
| Pin metadata | `commit` records the vendored core git revision; `dirty` is `false` for release trees. |
| Core purity | Vendored `cowbell.py` does **not** contain plugin-only symbols (`keys_install_text`, `cmd_open`, etc.). |
| Seed harness | Root `harnesses.toml` is the same file as `vendor/harnesses.toml` (symlink or copy). |
| `check-vendor.sh` | **UNCLEAR:** named in the release checklist but **not** shipped in this repository at authoring time. When the script lands, this step must pass with exit code `0`; until then, step 1–3 are the authoritative pin check. |

## Pass

- All version strings match; vendored tree is clean; packaging test green.

## Fail

- Version mismatch between manifest, VENDORED.json, and vendored core.
- `dirty: true` on a release tag checkout.
- Plugin-only code found inside `vendor/cowbell.py`.
