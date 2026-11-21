"""0.3.3 ship notes: changelog heading and the handoff a stranger follows."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ShipNotes(unittest.TestCase):
    def test_first_dated_changelog_heading_is_0_3_3(self):
        lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
        dated = [ln for ln in lines if ln.startswith("## ") and "Unreleased" not in ln]
        self.assertTrue(dated, "changelog has no dated heading")
        self.assertEqual(dated[0], "## 0.3.3 — 2026-11-13")

    def test_nothing_shipped_in_0_3_3_is_still_unreleased(self):
        # At the 0.3.3 cut this read "Unreleased has no bullets". New work now
        # lands there, so the check is that no 0.3.3 line was left behind in it.
        sections, current = {}, None
        for line in (ROOT / "CHANGELOG.md").read_text().splitlines():
            if line.startswith("## "):
                current = line
                sections[current] = []
            elif current and line.startswith("- "):
                sections[current].append(line)
        unreleased = sections.get("## Unreleased", [])
        shipped = sections.get("## 0.3.3 — 2026-11-13", [])
        self.assertTrue(shipped, "0.3.3 section has no bullets")
        self.assertEqual([b for b in unreleased if b in shipped], [])

    def test_handoff_names_the_suite_and_the_vendor_rule(self):
        text = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("python3 -m unittest discover -s tests -v", text)
        self.assertIn("scripts/vendor-core", text)
        self.assertIn("at least `0.3.3`", text)
        self.assertIn("Do not hand-edit `vendor/`.", text)

    def test_doctor_note_does_not_call_a_failed_read_an_empty_herd(self):
        for name in ("CHANGELOG.md", "CONTRIBUTING.md"):
            text = (ROOT / name).read_text()
            self.assertIn("A snapshot that cannot be read fails that line.", text, name)
            self.assertIn("It is not treated as an empty herd.", text, name)
            self.assertIn(
                "A snapshot value that is not an object fails the identity line and does not raise.",
                text,
                name,
            )
            self.assertIn("A missing snapshot key is still an empty herd.", text, name)

    def test_readme_development_points_at_handoff(self):
        text = (ROOT / "README.md").read_text()
        dev = text.split("## Development", 1)[1].split("## ", 1)[0]
        line = (
            "See [CONTRIBUTING.md](CONTRIBUTING.md) for the version pin, the doctor contract, "
            "and where a change goes."
        )
        self.assertIn(line, dev)
        trouble = text.split("## Troubleshooting", 1)[1].split("## ", 1)[0]
        self.assertNotIn("CONTRIBUTING.md", trouble)
        for banned in ("LA" "B-", "/" "Users/", "avicen" "na", "fici" "no", "hesi" "od"):
            self.assertNotIn(banned, line)

    def test_ship_notes_name_no_house_path_issue_id_or_machine(self):
        for name in ("CHANGELOG.md", "CONTRIBUTING.md"):
            text = (ROOT / name).read_text()
            for banned in ("LA" "B-", "/" "Users/", "avicen" "na", "fici" "no", "hesi" "od"):
                self.assertNotIn(banned, text, name)
