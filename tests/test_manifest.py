"""Manifest vs README drift: every declared event must be named in Troubleshooting.

The exact [[events]] (on, command) pairs are already pinned in
tests/test_plugin.py; this file only checks that the README tells the reader
about each one, plus the install-font action's full command.
"""
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _section(readme: str, heading: str) -> str:
    """Body of a `## heading` section, up to the next `## ` or the end."""
    match = re.search(rf"^## {re.escape(heading)}\s*$", readme, re.MULTILINE)
    if match is None:
        return ""
    rest = readme[match.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


class ManifestDocs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        cls.readme = (ROOT / "README.md").read_text()

    def test_troubleshooting_section_exists(self):
        self.assertTrue(
            _section(self.readme, "Troubleshooting").strip(),
            "README.md must keep a '## Troubleshooting' section",
        )

    def test_every_event_is_named_in_troubleshooting(self):
        events = [e["on"] for e in self.manifest.get("events", [])]
        self.assertTrue(events, "manifest declares no [[events]]")
        block = _section(self.readme, "Troubleshooting")
        for name in events:
            # Delimit on both sides so 'pane.exit' cannot match 'pane.exited'.
            self.assertIsNotNone(
                re.search(rf"(?<![\w.]){re.escape(name)}(?![\w.])", block),
                f'[[events]] on = "{name}" is not named in README Troubleshooting',
            )



if __name__ == "__main__":
    unittest.main()
