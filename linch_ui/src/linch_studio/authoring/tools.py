"""Read-only knowledge tools the authoring agent may call.

Both tools follow the linch duck-typed tool protocol and reach the knowledge base
through ``ToolContext.deps`` (see ``AuthoringDeps``); they never touch the
filesystem, the network, or the project store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from linch import ToolContext, ToolResult
from linch.tools.base import ToolScope

from .knowledge import DOCUMENT_CHARS, KnowledgeBase, UnknownAnchorError

MAX_QUERY_CHARS = 400
MAX_ANCHOR_CHARS = 300
MAX_PATH_CHARS = 300


@dataclass(frozen=True, slots=True)
class AuthoringDeps:
    """Dependency container handed to the authoring agent via ``Agent(deps=...)``."""

    knowledge: KnowledgeBase


def _require_text(raw: Any, key: str, cap: int) -> str:
    if not isinstance(raw, dict):
        raise ValueError("tool input must be an object")
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"'{key}' must not be blank")
    if len(cleaned) > cap:
        raise ValueError(f"'{key}' is limited to {cap} characters")
    return cleaned


def _optional_int(raw: Any, key: str, *, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(raw, dict):
        raise ValueError("tool input must be an object")
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"'{key}' must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
    return value


def _knowledge(ctx: Any) -> KnowledgeBase | None:
    return getattr(getattr(ctx, "deps", None), "knowledge", None)


class SearchDocsTool:
    name = "search_docs"
    description = (
        "Search the Linch SDK documentation and the capability catalog. Returns up to "
        "8 section anchors with snippets; read one in full with read_section."
    )
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    scope: ToolScope = "read"
    parallel = True
    retryable = False
    execution_timeout_ms = None

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"query": _require_text(raw, "query", MAX_QUERY_CHARS)}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"search_docs: {input['query'][:80]}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        knowledge = _knowledge(ctx)
        if knowledge is None:
            return ToolResult(is_error=True, content="knowledge base is not configured")
        hits = knowledge.search(input["query"])
        if not hits:
            return ToolResult(
                content="no matches; try different keywords, or read_section a toc path directly",
                summary="no matches",
            )
        lines = [f"{hit.anchor} — {hit.heading}: {hit.snippet}" for hit in hits]
        plural = "result" if len(hits) == 1 else "results"
        return ToolResult(content="\n".join(lines), summary=f"{len(hits)} {plural}")


class ReadSectionTool:
    name = "read_section"
    description = (
        "Read one documentation section by anchor, e.g. usage/tools.md#retry. Long "
        "sections are truncated; search_docs lists valid anchors."
    )
    input_schema = {
        "type": "object",
        "properties": {"anchor": {"type": "string"}},
        "required": ["anchor"],
        "additionalProperties": False,
    }
    scope: ToolScope = "read"
    parallel = True
    retryable = False
    execution_timeout_ms = None

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"anchor": _require_text(raw, "anchor", MAX_ANCHOR_CHARS)}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"read_section: {input['anchor'][:120]}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        knowledge = _knowledge(ctx)
        if knowledge is None:
            return ToolResult(is_error=True, content="knowledge base is not configured")
        try:
            section = knowledge.section(input["anchor"])
        except UnknownAnchorError as error:
            hint = ", ".join(error.suggestions) or "use search_docs to find anchors"
            return ToolResult(
                is_error=True,
                content=f"unknown anchor; close matches: {hint}",
                summary="unknown anchor",
            )
        content = section.text
        if section.truncated:
            content += "\n\n[truncated — the section continues beyond the cap]"
        return ToolResult(
            content=content,
            truncated=section.truncated,
            summary=f"{len(content)} chars",
        )


class ListDocsTool:
    """List corpus-relative document names for a bounded namespace."""

    name = "list_docs"
    description = "List documentation/example paths. Optional prefix narrows the list."
    input_schema = {
        "type": "object",
        "properties": {"prefix": {"type": "string"}},
        "additionalProperties": False,
    }
    scope: ToolScope = "read"
    parallel = True
    retryable = False
    execution_timeout_ms = None

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("tool input must be an object")
        prefix = raw.get("prefix", "")
        if not isinstance(prefix, str):
            raise ValueError("'prefix' must be a string")
        prefix = prefix.strip()
        if len(prefix) > MAX_PATH_CHARS:
            raise ValueError(f"'prefix' is limited to {MAX_PATH_CHARS} characters")
        return {"prefix": prefix}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"list_docs: {input['prefix'] or 'all'}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        knowledge = _knowledge(ctx)
        if knowledge is None:
            return ToolResult(is_error=True, content="knowledge base is not configured")
        paths = knowledge.list_documents(prefix=input["prefix"])
        capped = paths[:80]
        content = "\n".join(capped) or "no matching documents"
        if len(paths) > len(capped):
            content += f"\n[truncated — {len(paths) - len(capped)} more paths]"
        return ToolResult(
            content=content,
            truncated=len(paths) > len(capped),
            summary=f"{len(paths)} paths",
        )


class FindDocSymbolTool:
    """Use exact symbol/string lookup before a natural-language search."""

    name = "find_doc_symbol"
    description = "Find an exact SDK symbol, option name, or literal in docs/examples."
    input_schema = {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
        "additionalProperties": False,
    }
    scope: ToolScope = "read"
    parallel = True
    retryable = False
    execution_timeout_ms = None

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": _require_text(raw, "symbol", MAX_QUERY_CHARS)}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"find_doc_symbol: {input['symbol'][:80]}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        knowledge = _knowledge(ctx)
        if knowledge is None:
            return ToolResult(is_error=True, content="knowledge base is not configured")
        hits = knowledge.find_symbol(input["symbol"])
        if not hits:
            return ToolResult(content="no exact symbol matches", summary="no matches")
        content = "\n".join(f"{hit.anchor} — {hit.heading}: {hit.snippet}" for hit in hits)
        return ToolResult(content=content, summary=f"{len(hits)} matches")


class ReadDocTool:
    """Read a paginated corpus document when one section is not enough."""

    name = "read_doc"
    description = "Read a corpus document page by path; use nextOffset to continue."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    scope: ToolScope = "read"
    parallel = True
    retryable = False
    execution_timeout_ms = None

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": _require_text(raw, "path", MAX_PATH_CHARS),
            "offset": _optional_int(raw, "offset", default=0, minimum=0, maximum=10_000_000),
            "limit": _optional_int(
                raw,
                "limit",
                default=DOCUMENT_CHARS,
                minimum=1,
                maximum=DOCUMENT_CHARS,
            ),
        }

    def summarize(self, input: dict[str, Any]) -> str:
        return f"read_doc: {input['path']} @ {input['offset']}"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        knowledge = _knowledge(ctx)
        if knowledge is None:
            return ToolResult(is_error=True, content="knowledge base is not configured")
        try:
            page = knowledge.document(input["path"], offset=input["offset"], limit=input["limit"])
        except (UnknownAnchorError, ValueError) as error:
            return ToolResult(is_error=True, content=str(error), summary="unknown document")
        suffix = "" if page.next_offset is None else f"\n\n[nextOffset: {page.next_offset}]"
        return ToolResult(
            content=page.text + suffix,
            truncated=page.next_offset is not None,
            summary=f"{len(page.text)} chars" + ("; more available" if page.next_offset else ""),
        )


__all__ = [
    "MAX_ANCHOR_CHARS",
    "MAX_PATH_CHARS",
    "MAX_QUERY_CHARS",
    "AuthoringDeps",
    "FindDocSymbolTool",
    "ListDocsTool",
    "ReadDocTool",
    "ReadSectionTool",
    "SearchDocsTool",
]
