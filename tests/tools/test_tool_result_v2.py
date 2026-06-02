from __future__ import annotations

from linch.tools import Citation, ToolResult


def test_tool_result_v2_supports_citations_metadata_and_truncation() -> None:
    citation = Citation(
        id="c1",
        source="docs",
        label="Usage Guide",
        chunk="Agent Kit supports runtime tools.",
        score=0.92,
        metadata={"page": 3},
    )

    result = ToolResult(
        content="Runtime tools are supported.",
        summary="1 cited result",
        metadata={"query": "runtime tools"},
        citations=[citation],
        truncated=True,
    )

    assert result.content == "Runtime tools are supported."
    assert result.citations == [citation]
    assert result.metadata == {"query": "runtime tools"}
    assert result.truncated is True
    assert result.is_error is False


def test_tool_result_still_accepts_simple_text_result() -> None:
    result = ToolResult(content="ok")

    assert result.content == "ok"
    assert result.summary == ""
    assert result.citations == []
    assert result.metadata == {}
