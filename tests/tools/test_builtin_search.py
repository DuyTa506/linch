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


# ---------------------------------------------------------------------------
# BUG 1 — Grep malformed regex must not be a silent success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_malformed_regex_fallback_reports_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Force the pure-Python fallback path (rg disabled).
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "("})
    result = await tool.execute(validated, ctx)

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


@pytest.mark.asyncio
async def test_grep_rg_error_exit_code_surfaces_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """rg exit code 2 (error) must not be reported as a silent 'No matches found.'.

    Drives the rg path without requiring rg on PATH by faking the rg runner.
    """
    monkeypatch.setattr(builtin_mod, "_RG_PATH", "/usr/bin/rg")

    async def fake_rg(args, search_root, timeout=120.0):
        # rg returncode 2 == error (e.g. malformed regex), empty stdout.
        return "", 2, "regex parse error"

    monkeypatch.setattr(builtin_mod, "_run_grep_via_rg_async", fake_rg)
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "("})
    result = await tool.execute(validated, ctx)

    assert result.is_error is True
    assert result.content != "No matches found."


@pytest.mark.asyncio
async def test_grep_rg_exit_code_1_is_no_match(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """rg exit code 1 (no match) must still map to a non-error 'No matches found.'."""
    monkeypatch.setattr(builtin_mod, "_RG_PATH", "/usr/bin/rg")

    async def fake_rg(args, search_root, timeout=120.0):
        return "", 1, ""

    monkeypatch.setattr(builtin_mod, "_run_grep_via_rg_async", fake_rg)
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "zzz"})
    result = await tool.execute(validated, ctx)

    assert result.is_error is False
    assert result.content == "No matches found."


@pytest.mark.asyncio
async def test_grep_no_match_still_reports_no_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The normal no-match path must remain a non-error 'No matches found.'."""
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / "a.txt").write_text("hello\n")

    tool = GrepTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"pattern": "zzz-nope"})
    result = await tool.execute(validated, ctx)

    assert result.content == "No matches found."
    assert result.is_error is False


# ---------------------------------------------------------------------------
# BUG 2 — Glob must honour explicitly-requested dotfile patterns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glob_dotfile_pattern_returns_dotfiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".env.local").write_text("SECRET=2\n")

    tool = GlobTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"glob_pattern": ".env*"})
    result = await tool.execute(validated, ctx)

    assert ".env" in result.content
    assert ".env.local" in result.content


@pytest.mark.asyncio
async def test_glob_dotdir_pattern_returns_dotdir_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    gh = tmp_path / ".github" / "workflows"
    gh.mkdir(parents=True)
    (gh / "ci.yml").write_text("name: ci\n")

    tool = GlobTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"glob_pattern": ".github/**/*.yml"})
    result = await tool.execute(validated, ctx)

    assert "ci.yml" in result.content


@pytest.mark.asyncio
async def test_glob_normal_pattern_still_hides_dotfiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    (tmp_path / "visible.txt").write_text("x\n")
    (tmp_path / ".hidden.txt").write_text("y\n")

    tool = GlobTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"glob_pattern": "*.txt"})
    result = await tool.execute(validated, ctx)

    assert "visible.txt" in result.content
    assert ".hidden.txt" not in result.content


# ---------------------------------------------------------------------------
# BUG 3 — gitignore matching must respect path-segment boundaries
# ---------------------------------------------------------------------------


def test_matches_gitignore_respects_segment_boundary() -> None:
    entries = ["build"]
    assert builtin_mod._matches_gitignore("build", entries) is True
    assert builtin_mod._matches_gitignore("build/x.py", entries) is True
    assert builtin_mod._matches_gitignore("builder/x.py", entries) is False


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
