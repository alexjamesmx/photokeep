import re
from pathlib import Path

from photokeep import cli

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _docs() -> list[Path]:
    guides = [ROOT / "AGENTS.md", ROOT / "README.md"]
    return guides + [p for p in (ROOT / "docs").rglob("*.md") if "superpowers" not in p.parts]


def test_agents_md_mentions_every_cli_command():
    text = (ROOT / "AGENTS.md").read_text()
    missing = [c for c in cli.COMMANDS if f"photokeep {c}" not in text]
    assert missing == []


def test_runbooks_linked_from_agents_md():
    text = (ROOT / "AGENTS.md").read_text()
    runbooks = list((ROOT / "docs/runbooks").glob("*.md"))
    assert runbooks
    assert all(f"docs/runbooks/{p.name}" in text for p in runbooks)


def test_relative_links_resolve():
    broken = []
    for doc in _docs():
        for target in LINK.findall(doc.read_text()):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (doc.parent / target.split("#")[0]).exists():
                broken.append(f"{doc.relative_to(ROOT)}: {target}")
    assert broken == []
