"""Tests for Grep / Glob / gitignore behaviour in builtin search tools.

Covers four confirmed bugs:

- Grep with a malformed regex reports an error (consistent across rg / fallback).
- Glob honours explicitly-requested dotfile patterns.
- gitignore matching respects path-segment boundaries (no sibling over-exclude).
"""

from __future__ import annotations

import pytest

import linch.filesystem.disk as disk_mod
from linch.errors import ToolExecutionError
from linch.tools import builtin as builtin_mod
from linch.tools.builtin import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from linch.tools.file_tracker import FileReadTracker


def _make_ctx(tmp_path):
    from linch.tools.base import ToolContext

    return ToolContext(
        cwd=str(tmp_path),
        session_id="s",
        run_id="r",
        session_store=None,
    )


@pytest.mark.parametrize(
    ("tool_cls", "raw", "error"),
    [
        (ReadTool, {"path": "a.txt"}, ValueError),
        (WriteTool, {"path": "a.txt", "content": "text"}, ValueError),
        (
            EditTool,
            {"path": "a.txt", "old": "before", "new": "after"},
            ValueError,
        ),
        (GlobTool, {"pattern": "*.py"}, ToolExecutionError),
    ],
)
def test_builtin_legacy_argument_aliases_are_rejected(tool_cls, raw, error) -> None:
    with pytest.raises(error):
        tool_cls().validate(raw)


def test_glob_path_alias_does_not_select_target_directory() -> None:
    assert GlobTool().validate({"glob_pattern": "*.py", "path": "other"}) == {
        "glob_pattern": "*.py"
    }


@pytest.mark.asyncio
async def test_grep_pattern_is_an_option_value_and_paths_follow_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(builtin_mod, "_RG_PATH", "/usr/bin/rg")

    async def fake_rg(args, search_root, timeout=120.0):
        captured["args"] = args
        return "a.txt\n", 0, ""

    monkeypatch.setattr(builtin_mod, "_run_grep_via_rg_async", fake_rg)
    (tmp_path / "a.txt").write_text("needle\n")

    tool = GrepTool()
    await tool.execute(
        tool.validate({"pattern": "--pre=rm", "glob": "--pre=glob", "type": "--pre=type"}),
        _make_ctx(tmp_path),
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[args.index("-e") + 1] == "--pre=rm"
    assert "--glob=--pre=glob" in args
    assert "--type=--pre=type" in args
    assert args[args.index("--") + 1] == str(tmp_path)


@pytest.mark.asyncio
async def test_glob_pattern_is_an_option_value_and_path_follows_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(builtin_mod, "_RG_PATH", "/usr/bin/rg")

    async def fake_rg(args, search_root, timeout=120.0):
        captured["args"] = args
        return "a.txt\n", 0, ""

    monkeypatch.setattr(builtin_mod, "_run_grep_via_rg_async", fake_rg)
    (tmp_path / "a.txt").write_text("needle\n")

    tool = GlobTool()
    await tool.execute(tool.validate({"glob_pattern": "--pre=rm"}), _make_ctx(tmp_path))

    args = captured["args"]
    assert isinstance(args, list)
    assert "--glob=--pre=rm" in args
    assert args[args.index("--") + 1] == str(tmp_path)


@pytest.mark.asyncio
async def test_windowed_read_only_grants_edit_for_seen_lines(tmp_path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    tracker = FileReadTracker()
    ctx = _make_ctx(tmp_path)
    ctx.file_read_tracker = tracker
    read = ReadTool()
    edit = EditTool()

    await read.execute(read.validate({"file_path": "a.txt", "offset": 2, "limit": 1}), ctx)

    unseen = await edit.execute(
        edit.validate({"file_path": "a.txt", "old_string": "line 3", "new_string": "changed"}),
        ctx,
    )
    assert unseen.is_error
    assert "before editing" in unseen.content

    seen = await edit.execute(
        edit.validate({"file_path": "a.txt", "old_string": "line 2", "new_string": "changed"}),
        ctx,
    )
    assert not seen.is_error
    assert "changed" in target.read_text(encoding="utf-8")


async def test_windowed_read_allows_edit_through_seen_line_ending(tmp_path) -> None:
    target = tmp_path / "line-ending.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    ctx.file_read_tracker = FileReadTracker()
    await ReadTool().execute({"file_path": target.name, "offset": 2, "limit": 1}, ctx)

    result = await EditTool().execute(
        {"file_path": target.name, "old_string": "b\n", "new_string": "B\n"}, ctx
    )

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"


@pytest.mark.asyncio
async def test_windowed_read_replace_all_requires_every_match_region(tmp_path) -> None:
    target = tmp_path / "repeated.txt"
    target.write_text("same\nother\nsame\n", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    ctx.file_read_tracker = FileReadTracker()
    read = ReadTool()
    edit = EditTool()

    await read.execute(read.validate({"file_path": target.name, "offset": 1, "limit": 1}), ctx)
    result = await edit.execute(
        edit.validate(
            {
                "file_path": target.name,
                "old_string": "same",
                "new_string": "changed",
                "replace_all": True,
            }
        ),
        ctx,
    )

    assert result.is_error
    assert target.read_text(encoding="utf-8") == "same\nother\nsame\n"


@pytest.mark.asyncio
async def test_windowed_read_beyond_eof_records_no_range(tmp_path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("line\n", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    tracker = FileReadTracker()
    ctx.file_read_tracker = tracker

    await ReadTool().execute(
        ReadTool().validate({"file_path": target.name, "offset": 99, "limit": 1}),
        ctx,
    )

    assert not tracker.has_read_range(str(target), 99, 99)


async def test_zero_read_limit_keeps_default_window(tmp_path) -> None:
    target = tmp_path / "long.txt"
    target.write_text("line\n" * 2000 + "outside-default-window", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    ctx.file_read_tracker = FileReadTracker()

    result = await ReadTool().execute({"file_path": target.name, "limit": 0}, ctx)

    assert "outside-default-window" not in result.content
    assert not ctx.file_read_tracker.has_read_range(str(target), 2001, 2001)


def test_partial_read_ranges_are_not_exported_as_whole_file_evidence() -> None:
    tracker = FileReadTracker()
    tracker.mark_read("/partial.txt", 2, 3)

    assert tracker.files() == []
    assert len(tracker) == 0
    assert "/partial.txt" not in tracker


@pytest.mark.asyncio
async def test_full_read_keeps_missing_edit_text_error(tmp_path) -> None:
    target = tmp_path / "missing.txt"
    target.write_text("content", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    ctx.file_read_tracker = FileReadTracker()
    read = ReadTool()
    edit = EditTool()

    await read.execute(read.validate({"file_path": target.name}), ctx)
    with pytest.raises(ToolExecutionError, match="old text not found"):
        await edit.execute(
            edit.validate({"file_path": target.name, "old_string": "missing", "new_string": "x"}),
            ctx,
        )


@pytest.mark.asyncio
async def test_glob_fallback_rejects_absolute_and_parent_escaping_patterns(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.key").write_text("TOP SECRET\n")
    tool = GlobTool()
    ctx = _make_ctx(workspace)

    absolute = await tool.execute(tool.validate({"glob_pattern": f"{tmp_path}/*.key"}), ctx)
    parent = await tool.execute(tool.validate({"glob_pattern": "../*.key"}), ctx)

    assert absolute.is_error
    assert parent.is_error
    assert "secret.key" not in absolute.content
    assert "secret.key" not in parent.content


@pytest.mark.asyncio
async def test_grep_fallback_rejects_parent_escaping_glob(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (tmp_path / "secret.key").write_text("TOP SECRET\n")
    tool = GrepTool()
    ctx = _make_ctx(workspace)

    result = await tool.execute(
        tool.validate({"pattern": "SECRET", "glob": "src/../../*.key", "output_mode": "content"}),
        ctx,
    )

    assert result.is_error
    assert "TOP SECRET" not in result.content


@pytest.mark.asyncio
async def test_fallback_globs_skip_symlink_results_outside_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(builtin_mod, "_RG_PATH", None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET\n")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    ctx = _make_ctx(workspace)

    glob_result = await GlobTool().execute(
        GlobTool().validate({"glob_pattern": "**/*"}),
        ctx,
    )
    grep_result = await GrepTool().execute(
        GrepTool().validate({"pattern": "TOP SECRET", "output_mode": "content"}),
        ctx,
    )

    assert "secret.txt" not in glob_result.content
    assert "TOP SECRET" not in grep_result.content


@pytest.mark.asyncio
async def test_workspace_file_tools_preserve_crlf_and_lone_cr(tmp_path) -> None:
    target = tmp_path / "line-endings.txt"
    target.write_bytes(b"one\r\ntwo\rthree\n")
    ctx = _make_ctx(tmp_path)
    ctx.file_read_tracker = FileReadTracker()
    read = ReadTool()
    edit = EditTool()

    await read.execute(read.validate({"file_path": target.name}), ctx)
    edited = await edit.execute(
        edit.validate(
            {
                "file_path": target.name,
                "old_string": "two\rthree",
                "new_string": "changed\rthree",
            }
        ),
        ctx,
    )

    assert not edited.is_error
    assert target.read_bytes() == b"one\r\nchanged\rthree\n"

    await WriteTool().execute(
        {"file_path": target.name, "content": "a\r\nb\rc\n"},
        ctx,
    )
    assert target.read_bytes() == b"a\r\nb\rc\n"


@pytest.mark.asyncio
async def test_failed_workspace_write_keeps_original_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    target = tmp_path / "atomic.txt"
    target.write_bytes(b"original\r\n")

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(disk_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        await WriteTool().execute(
            {"file_path": target.name, "content": "replacement"},
            _make_ctx(tmp_path),
        )

    assert target.read_bytes() == b"original\r\n"


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
