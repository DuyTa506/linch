from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from .types import ProviderRequest, SystemBlock

# A per-tool identity used to detect cache-prefix-breaking changes across turns:
# the wire-ordered ``(name, canonical-JSON of the schema)`` pairs of ``req.tools``.
ToolSignature = tuple[tuple[str, str], ...]
PromptCacheReason = Literal["tool_set_changed", "model_changed"]


@dataclass(frozen=True, slots=True)
class PromptCacheWire:
    """Provider-specific wire shape for Linch's unified prompt-cache intent."""

    block_field: str | None = None
    extra_body_field: str | None = None
    extra_body_value: Any = True
    emit_default_ttl: bool = False


ANTHROPIC_PROMPT_CACHE = PromptCacheWire(block_field="cache_control")
LLAMACPP_PROMPT_CACHE = PromptCacheWire(extra_body_field="cache_prompt")


def prompt_cache_enabled(cache: bool | None) -> bool:
    return bool(cache)


def cache_block(ttl: str | None, wire: PromptCacheWire) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "ephemeral"}
    if ttl == "1h" or (ttl == "5m" and wire.emit_default_ttl):
        block["ttl"] = ttl
    return block


def last_cacheable_system_index(blocks: list[SystemBlock]) -> int | None:
    """Index of the last system block marked cacheable."""
    for index in range(len(blocks) - 1, -1, -1):
        if getattr(blocks[index], "cacheable", False):
            return index
    return None


def mark_system_cache_breakpoint(
    payload_blocks: list[dict[str, Any]],
    source_blocks: list[SystemBlock],
    *,
    cache: bool | None,
    ttl: str | None,
    wire: PromptCacheWire,
) -> None:
    if not prompt_cache_enabled(cache) or wire.block_field is None:
        return
    boundary = last_cacheable_system_index(source_blocks)
    if boundary is not None:
        payload_blocks[boundary][wire.block_field] = cache_block(ttl, wire)


def mark_last_cacheable_message_content(
    entries: list[dict[str, Any]],
    *,
    cache: bool | None,
    ttl: str | None,
    wire: PromptCacheWire,
    content_types: set[str],
) -> None:
    if not prompt_cache_enabled(cache) or wire.block_field is None:
        return
    for entry in reversed(entries):
        content = entry.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") in content_types:
                block[wire.block_field] = cache_block(ttl, wire)
                return


def apply_extra_body_cache(
    extra_body: dict[str, Any],
    req: ProviderRequest,
    *,
    wire: PromptCacheWire,
) -> None:
    if (
        prompt_cache_enabled(req.cache_prompt)
        and wire.extra_body_field is not None
        and wire.extra_body_field not in extra_body
    ):
        extra_body[wire.extra_body_field] = wire.extra_body_value


def openai_cached_tokens(usage: Any) -> int:
    details = getattr(usage, "prompt_tokens_details", None)
    if isinstance(details, dict):
        return int(
            details.get("cached_tokens")
            or details.get("cache_read_tokens")
            or details.get("prompt_cache_hit_tokens")
            or 0
        )
    cached = (
        getattr(details, "cached_tokens", None)
        or getattr(details, "cache_read_tokens", None)
        or getattr(details, "prompt_cache_hit_tokens", None)
        or getattr(usage, "cached_tokens", None)
        or getattr(usage, "cache_read_tokens", None)
        or getattr(usage, "prompt_cache_hit_tokens", None)
        or 0
    )
    return int(cached)


def openai_responses_cached_tokens(raw_usage: dict[str, Any] | None) -> int:
    details = (raw_usage or {}).get("input_tokens_details") or {}
    if not isinstance(details, dict):
        return 0
    return int(details.get("cached_tokens") or 0)


def gemini_cached_tokens(usage_metadata: Any) -> int:
    return int(getattr(usage_metadata, "cached_content_token_count", 0) or 0)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def tool_signature(tools: list[dict[str, Any]]) -> ToolSignature:
    """Stable identity of a request's tool list, in wire order.

    Each entry is ``(name, canonical-JSON of the whole schema)`` so a reorder, a
    renamed tool, or an edited input schema all change the signature — anything
    that would move or invalidate the cached request prefix.
    """
    signature: list[tuple[str, str]] = []
    for schema in tools:
        name = str(schema.get("name", "")) if isinstance(schema, dict) else ""
        signature.append((name, _canonical_json(schema)))
    return tuple(signature)


def prompt_cache_advisories(
    *,
    prev_model: str | None,
    prev_signature: ToolSignature | None,
    model: str,
    signature: ToolSignature,
) -> list[tuple[PromptCacheReason, str]]:
    """Return ``(reason, detail)`` advisories for prefix-breaking changes.

    Empty on the first provider call of a session (no baseline yet) and whenever
    the cached prefix is stable. ``prev_*`` are the previous call's model and
    tool signature; ``None`` means "no prior call, nothing to compare".
    """
    advisories: list[tuple[PromptCacheReason, str]] = []
    if prev_model is not None and model != prev_model:
        advisories.append(
            (
                "model_changed",
                f"model changed from {prev_model!r} to {model!r}; the prompt cache is "
                "keyed per model, so the next request starts from a cold prefix.",
            )
        )
    if prev_signature is not None and signature != prev_signature:
        prev_names = [name for name, _ in prev_signature]
        names = [name for name, _ in signature]
        prev_set, cur_set = set(prev_names), set(names)
        added = [name for name in names if name not in prev_set]
        removed = [name for name in prev_names if name not in cur_set]
        if added or removed:
            detail = f"tool set changed (added={added}, removed={removed})"
        else:
            detail = "tool order or input schemas changed"
        advisories.append(
            (
                "tool_set_changed",
                f"{detail}; tools lead the cached request prefix, so this invalidates it.",
            )
        )
    return advisories
