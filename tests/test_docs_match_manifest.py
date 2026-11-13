"""README surface matches herdr-plugin.toml: actions, code blocks, events, Python floor, local links."""
from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import herdr_cowbell as hm  # noqa: E402

README = ROOT / "README.md"
MANIFEST = ROOT / "herdr-plugin.toml"
BIN_COWBELL = ROOT / "bin" / "cowbell"

FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
PLUGIN_ACTION_RE = re.compile(r"\bherdr-cowbell\.([a-z][a-z0-9-]*)\b")
COWBELL_CLI_RE = re.compile(r"(?:^|[\s|`])(?:\./)?bin/cowbell\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?")
README_PY_RE = re.compile(r"python\s+(3\.\d+)", re.I)
SHIM_PY_RE = re.compile(r"sys\.version_info\s*>=\s*\(\s*3\s*,\s*(\d+)\s*\)")


def load_manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def manifest_action_ids(manifest: dict) -> set[str]:
    return {a["id"] for a in manifest["actions"]}


def manifest_event_names(manifest: dict) -> list[str]:
    return [e["on"] for e in manifest.get("events", [])]


def cowbell_paths_from_manifest(manifest: dict) -> set[tuple[str, ...]]:
    """Every ./bin/cowbell argv tail declared in the manifest."""
    paths: set[tuple[str, ...]] = set()
    for key in ("actions", "startup", "events", "panes"):
        for entry in manifest.get(key, []):
            cmd = entry.get("command") or []
            if len(cmd) >= 2 and cmd[0] == "./bin/cowbell":
                paths.add(tuple(cmd[1:]))
    return paths


def cowbell_top_level_commands(manifest: dict) -> set[str]:
    tops = {p[0] for p in cowbell_paths_from_manifest(manifest)}
    tops |= hm.PLUGIN_COMMANDS
    return tops


def troubleshooting_and_events_text(md: str) -> str:
    parts: list[str] = []
    for heading in ("## Troubleshooting", "## Events"):
        m = re.search(rf"^{re.escape(heading)}\s*$", md, re.MULTILINE)
        if not m:
            continue
        rest = md[m.end() :]
        nxt = re.search(r"^## ", rest, re.MULTILINE)
        parts.append(rest[: nxt.start()] if nxt else rest)
    return "\n".join(parts)


def fenced_code_blocks(md: str) -> list[str]:
    return [m.group(1) for m in FENCE_RE.finditer(md)]


def parse_readme_python_minor(md: str) -> tuple[int, int] | None:
    m = README_PY_RE.search(md)
    if not m:
        return None
    major, minor = m.group(1).split(".", 1)
    return int(major), int(minor)


def shim_python_floor() -> tuple[int, int]:
    text = BIN_COWBELL.read_text(encoding="utf-8")
    m = SHIM_PY_RE.search(text)
    if not m:
        raise AssertionError("bin/cowbell has no python version guard")
    return 3, int(m.group(1))


def local_markdown_link_targets(md: str) -> list[str]:
    return [
        dest
        for dest in re.findall(r"\[[^\]]*\]\(([^)]+)\)", md)
        if not re.match(r"^[a-z][a-z0-9+.-]*:", dest, re.I) and not dest.startswith("#")
    ]


class DocsMatchManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        cls.readme = readme_text()

    def test_every_manifest_action_id_mentioned_in_readme(self):
        missing = sorted(
            aid for aid in manifest_action_ids(self.manifest) if aid not in self.readme
        )
        self.assertEqual(missing, [], f"README never mentions action id(s): {', '.join(missing)}")

    def test_readme_code_blocks_use_manifest_actions_or_cowbell_commands(self):
        tops = cowbell_top_level_commands(self.manifest)
        paths = cowbell_paths_from_manifest(self.manifest)
        unknown_actions: list[str] = []
        unknown_cli: list[str] = []
        for block in fenced_code_blocks(self.readme):
            for aid in PLUGIN_ACTION_RE.findall(block):
                if aid not in manifest_action_ids(self.manifest):
                    unknown_actions.append(aid)
            for m in COWBELL_CLI_RE.finditer(block):
                cmd, sub = m.group(1), m.group(2)
                tail = (cmd, sub) if sub else (cmd,)
                if cmd not in tops and tail not in paths:
                    unknown_cli.append(" ".join(tail))
        self.assertEqual(unknown_actions, [], f"unknown herdr-cowbell actions in code blocks: {unknown_actions}")
        self.assertEqual(unknown_cli, [], f"unknown bin/cowbell commands in code blocks: {unknown_cli}")

    def test_every_manifest_event_in_troubleshooting_or_events(self):
        """Every manifest event is named in README Troubleshooting or Events."""
        blob = troubleshooting_and_events_text(self.readme)
        missing = [ev for ev in manifest_event_names(self.manifest) if ev not in blob]
        self.assertEqual(
            missing,
            [],
            "document each manifest [[events]] on= value in README Troubleshooting or Events",
        )

    def test_readme_python_minimum_matches_shim(self):
        readme_floor = parse_readme_python_minor(self.readme)
        self.assertIsNotNone(readme_floor, "README should state a minimum python version")
        pyproject = ROOT / "pyproject.toml"
        if pyproject.is_file():
            spec = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            requires = spec.get("project", {}).get("requires-python", "")
            m = re.search(r">=\s*3\.(\d+)", requires)
            expected = (3, int(m.group(1))) if m else shim_python_floor()
        else:
            expected = shim_python_floor()
        self.assertGreaterEqual(readme_floor, expected, "README python minimum is below the runtime floor")

    def test_readme_local_file_links_resolve(self):
        missing = [
            target
            for target in local_markdown_link_targets(self.readme)
            if not (ROOT / target).is_file()
        ]
        self.assertEqual(missing, [], f"broken local README links: {missing}")


if __name__ == "__main__":
    unittest.main()
