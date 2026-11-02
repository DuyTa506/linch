"""Keep builder-facing troubleshooting quotes tied to source, without imports."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/usage/troubleshooting.md"
QUOTES = re.compile(r"^\| (Literal|Fragment) \| `([^`]+)` \| `([^`]+)` \|", re.MULTILINE)


def assert_source_quote(kind: str, quote: str, source: Path) -> None:
    """AST Constants include adjacent literals and fixed f-string segments."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    dynamic = {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for value in node.values
        if isinstance(value, ast.Constant)
    }
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (kind == "Fragment" or id(node) not in dynamic)
    ]
    assert any(quote == value if kind == "Literal" else quote in value for value in strings), (
        f"{kind} quote absent from {source}: {quote!r}"
    )


def test_usage_pages_open_with_a_use_case() -> None:
    missing = []
    for page in sorted((ROOT / "docs/usage").glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        if not (lines[0].startswith("# ") and lines[2].startswith("Use ")):
            missing.append(page.name)
    assert not missing, "Missing first use-case line: " + ", ".join(missing)


def test_troubleshooting_quotes_exist_in_source() -> None:
    assert PAGE.is_file(), "Add the source-backed troubleshooting page"
    quotes = QUOTES.findall(PAGE.read_text(encoding="utf-8"))
    assert quotes, "Troubleshooting must declare Literal/Fragment message rows"
    sources = {source for _, _, source in quotes}
    assert {
        "src/linch/agent.py",
        "src/linch/tools/registry.py",
        "src/linch/testing/contracts.py",
        "src/linch/providers/gemini.py",
        "src/linch/run_store.py",
        "src/linch/loop/runner.py",
    } <= sources
    for kind, quote, source in quotes:
        assert source.startswith("src/linch/") and source.endswith(".py")
        assert_source_quote(kind, quote, ROOT / source)


@pytest.mark.parametrize("kind", ["Literal", "Fragment"])
def test_source_quote_rejects_fabricated_message(kind) -> None:
    with pytest.raises(AssertionError, match="quote absent"):
        assert_source_quote(kind, "Linch invented this error", ROOT / "src/linch/agent.py")


def test_source_quote_handles_concatenation_and_dynamic_fragments(tmp_path) -> None:
    source = tmp_path / "errors.py"
    source.write_text('raise Error("first " "second")\nraise Error(f"prefix: {detail}")\n')
    assert_source_quote("Literal", "first second", source)
    assert_source_quote("Fragment", "prefix: ", source)
    with pytest.raises(AssertionError, match="quote absent"):
        assert_source_quote("Literal", "prefix: ", source)
    with pytest.raises(AssertionError, match="quote absent"):
        assert_source_quote("Literal", "prefix: invented detail", source)
