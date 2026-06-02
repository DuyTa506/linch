from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from ..tools.base import ToolResult


def map_mcp_result(result: CallToolResult) -> ToolResult:
    is_error = result.isError or False
    content_blocks: list[dict[str, object]] = []

    for block in result.content:
        if isinstance(block, TextContent):
            content_blocks.append({"type": "text", "text": block.text})
        elif hasattr(block, "type") and block.type == "image":
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": getattr(block, "mimeType", "image/png"),
                        "data": getattr(block, "data", ""),
                    },
                }
            )
        elif hasattr(block, "type") and block.type == "audio":
            content_blocks.append({"type": "text", "text": "[audio content omitted]"})
        elif hasattr(block, "type") and block.type == "resource":
            content_blocks.append({"type": "text", "text": "[resource content omitted]"})
        else:
            content_blocks.append({"type": "text", "text": "[unsupported content omitted]"})

    if not content_blocks:
        return ToolResult(
            content="(no content)",
            summary="(no content)",
            is_error=is_error,
        )

    summary = _summarize(content_blocks, is_error)

    text_parts: list[str] = []
    for b in content_blocks:
        if b.get("type") == "text":
            text_parts.append(str(b["text"]))
        else:
            text_parts.append(f"[{b.get('type', 'block')}]")

    return ToolResult(
        content="\n".join(text_parts),
        summary=summary,
        is_error=is_error,
    )


def _summarize(blocks: list[dict[str, object]], is_error: bool) -> str:
    prefix = "error: " if is_error else ""
    for b in blocks:
        if b.get("type") == "text":
            one_line = " ".join(str(b["text"]).split())
            if len(one_line) > 80:
                one_line = one_line[:80] + "…"
            return prefix + (one_line or "(empty)")
    return prefix + f"{len(blocks)} content block(s)"
