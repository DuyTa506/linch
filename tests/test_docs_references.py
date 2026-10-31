"""Keep current documentation references aligned with the shipped source tree."""

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "src/linch"
_NAME = r"[A-Za-z_]\w*"
_IMPORT = re.compile(
    rf"\bfrom linch import\s+(\([^)]*\)|{_NAME}(?:[ \t]+as[ \t]+{_NAME})?"
    rf"(?:[ \t]*,[ \t]*{_NAME}(?:[ \t]+as[ \t]+{_NAME})?)*)"
)
_NON_API_CONTEXT = re.compile(
    r'(?:\[\s*["\']linch\.(?:workflow|session|tool_search)["\']\s*\]|'
    r"`linch\.tool_search`\s+metadata\b|"
    r"linch\.(?:store\.duration|run_id|turn_index|run:default)(?![\w-]|\.[\w-])|"
    r'linch\.schedule[`"\']?\s+job\b)'
)


def _references(text: str) -> list[tuple[int, str]]:
    masked = list(text)
    for match in _NON_API_CONTEXT.finditer(text):
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    refs = [
        (match.start(), match.group(1))
        for match in re.finditer(rf"(?<![\w/])linch\.({_NAME})", "".join(masked))
    ]
    for match in _IMPORT.finditer(text):
        imported = ast.parse("from linch import " + match.group(1)).body[0]
        assert isinstance(imported, ast.ImportFrom)
        refs.extend((match.start(), alias.name) for alias in imported.names)
    return refs


def test_references_only_skip_namespace_literals_in_their_known_contexts() -> None:
    text = """linch.workflow
from linch import workflow
["linch.workflow"] ["linch.session"] ["linch.tool_search"]
linch.store.duration linch.run_id linch.turn_index
linch.run:default and a deferred `linch.schedule` job"""

    names = [name for _, name in _references(text)]

    assert names == ["workflow", "workflow"]


def test_documented_api_names_and_source_paths_exist() -> None:
    """Reject removed API names and source paths in maintained documentation."""
    tree = ast.parse((_PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    exported = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    )
    names = {*exported, "__all__"}
    names.update(path.stem for path in _PACKAGE.glob("*.py"))
    names.update(path.name for path in _PACKAGE.iterdir() if (path / "__init__.py").is_file())
    errors = []
    paths = [_ROOT / "README.md", _ROOT / "CHANGELOG.md", *sorted((_ROOT / "docs").rglob("*.md"))]
    for path in paths:
        if "superpowers" in path.relative_to(_ROOT).parts:
            continue
        text = path.read_text(encoding="utf-8")
        if path.name == "CHANGELOG.md":
            text = re.split(r"(?m)^## (?!.*unreleased).*\d+\.\d+", text, maxsplit=1)[0]
        missing = [(pos, f"linch.{name}") for pos, name in _references(text) if name not in names]
        missing.extend(
            (match.start(), match.group())
            for match in re.finditer(r"\bsrc/linch/[\w./-]+\.py\b", text)
            if not (_ROOT / match.group()).is_file()
        )
        errors.extend(
            f"{path.relative_to(_ROOT)}:{text.count(chr(10), 0, pos) + 1}: {reference}"
            for pos, reference in sorted(set(missing))
        )
    assert not errors, "Stale documentation references:\n" + "\n".join(errors)
