"""Catch the mermaid mistakes that stop a diagram rendering on GitHub."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = sorted((ROOT / "docs").glob("[au]*/**/*.md")) + sorted((ROOT / "docs/vi").rglob("*.md"))
BLOCK = re.compile(r"^```mermaid\n(.*?)^```$", re.M | re.S)


def _blocks() -> list[tuple[str, str]]:
    return [
        (path.relative_to(ROOT).as_posix(), block)
        for path in PAGES
        for block in BLOCK.findall(path.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize(("page", "block"), _blocks(), ids=lambda value: value[:40])
def test_diagram_avoids_syntax_mermaid_rejects(page: str, block: str) -> None:
    kind = block.split(maxsplit=1)[0]
    for line in block.splitlines():
        if kind == "sequenceDiagram":
            assert ";" not in line, f"{page}: ';' ends a sequence statement: {line.strip()}"
        if kind == "classDiagram" and line.strip().startswith("+"):
            assert "{" not in line, f"{page}: '{{' breaks a class member: {line.strip()}"
