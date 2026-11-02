from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECTIONS = ("usage", "architecture")
PAGES = sorted(
    f"{section}/{path.name}"
    for section in SECTIONS
    for path in (ROOT / "docs" / section).glob("*.md")
    if path.name != "api-reference.md"
)
FENCE = re.compile(r"^```.*?^```$", re.M | re.S)
LINK = re.compile(r"\]\(([^)\s]+)\)")


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
    english, vietnamese = _body(ROOT / "docs" / page), _body(ROOT / "docs/vi" / page)
    assert FENCE.findall(vietnamese) == FENCE.findall(english)
    assert _headings(vietnamese) == _headings(english)


@pytest.mark.parametrize("page", PAGES)
def test_vietnamese_page_links_resolve(page: str) -> None:
    source = ROOT / "docs/vi" / page
    for target in LINK.findall(FENCE.sub("", _body(source))):
        path = target.split("#", 1)[0]
        if path and not path.startswith(("http://", "https://", "mailto:")):
            assert (source.parent / path).exists(), f"{page}: broken link {target}"
