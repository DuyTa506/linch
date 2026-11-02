from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGES = sorted(path.name for path in (ROOT / "docs/usage").glob("*.md"))
PAGES.remove("api-reference.md")
FENCE = re.compile(r"^```.*?^```$", re.M | re.S)


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _headings(text: str) -> list[int]:
    return [
        len(line) - len(line.lstrip("#"))
        for line in FENCE.sub("", text).splitlines()
        if re.match(r"#{1,6} ", line)
    ]


@pytest.mark.parametrize("page", PAGES)
def test_vietnamese_page_keeps_code_and_structure(page: str) -> None:
    english, vietnamese = _body(ROOT / "docs/usage" / page), _body(ROOT / "docs/vi/usage" / page)
    assert FENCE.findall(vietnamese) == FENCE.findall(english)
    assert _headings(vietnamese) == _headings(english)
