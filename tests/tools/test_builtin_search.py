"""Tests for Grep / Glob / gitignore behaviour in builtin search tools.

Covers four confirmed bugs:

- Grep with a malformed regex reports an error (consistent across rg / fallback).
- Glob honours explicitly-requested dotfile patterns.
- gitignore matching respects path-segment boundaries (no sibling over-exclude).
"""

from __future__ import annotations

import pytest

from linch.tools import builtin as builtin_mod
from linch.tools.builtin import GlobTool, GrepTool


def _make_ctx(tmp_path):
    from linch.tools.base import ToolContext

    return ToolContext(
        cwd=str(tmp_path),
        session_id="s",
        run_id="r",
        session_store=None,
    )


@pytest.mark.parametrize("use_rg", [False, True])
@pytest.mark.asyncio
async def test_grep_malformed_regex_reports_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path, use_rg: bool
) -> None:
    if use_rg:
        monkeypatch.setattr(builtin_mod, "_RG_PATH", "/usr/bin/rg")

        async def fake_rg(args, search_root, timeout=120.0):
            return "", 2, "regex parse error"

        monkeypatch.setattr(builtin_mod, "_run_grep_via_rg_async", fake_rg)
    else:
        monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    result = await tool.execute(tool.validate({"pattern": "("}), _make_ctx(tmp_path))

    if use_rg:
        assert result.is_error is True
    else:
        assert "Invalid regex" in result.content
    assert result.content != "No matches found."


@pytest.mark.asyncio
@pytest.mark.skipif(builtin_mod._RG_PATH is None, reason="ripgrep not on PATH")
async def test_grep_malformed_regex_rg_reports_error(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "("})
    result = await tool.execute(validated, ctx)

    assert result.is_error is True
    assert result.content != "No matches found."


@pytest.mark.parametrize("use_rg", [False, True])
@pytest.mark.asyncio
async def test_grep_no_match_is_consistently_non_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path, use_rg: bool
) -> None:
    if use_rg:
        monkeypatch.setattr(builtin_mod, "_RG_PATH", "/usr/bin/rg")

        async def fake_rg(args, search_root, timeout=120.0):
            return "", 1, ""

        monkeypatch.setattr(builtin_mod, "_run_grep_via_rg_async", fake_rg)
    else:
        monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "zzz"})
    result = await tool.execute(validated, ctx)

    assert result.content == "No matches found."
    assert result.is_error is False


@pytest.mark.parametrize(
    ("pattern", "files", "expected", "absent"),
    [
        (
            ".env*",
            [(".env", "SECRET=1\n"), (".env.local", "SECRET=2\n")],
            (".env", ".env.local"),
            (),
        ),
        (".github/**/*.yml", [(".github/workflows/ci.yml", "name: ci\n")], ("ci.yml",), ()),
        (
            "*.txt",
            [("visible.txt", "x\n"), (".hidden.txt", "y\n")],
            ("visible.txt",),
            (".hidden.txt",),
        ),
    ],
)
@pytest.mark.asyncio
async def test_glob_patterns_respect_dotfile_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path, pattern, files, expected, absent
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    for relative, content in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    tool = GlobTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"glob_pattern": pattern})
    result = await tool.execute(validated, ctx)

    for text in expected:
        assert text in result.content
    for text in absent:
        assert text not in result.content


@pytest.mark.asyncio
async def test_grep_gitignore_does_not_over_exclude_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / ".gitignore").write_text("build\n")

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "x.py").write_text("needle = 1\n")
    (tmp_path / "builder").mkdir()
    (tmp_path / "builder" / "x.py").write_text("needle = 1\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "needle"})
    result = await tool.execute(validated, ctx)

    assert "builder/x.py" in result.content
    assert "build/x.py" not in result.content
