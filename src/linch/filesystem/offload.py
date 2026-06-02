"""Automatic offload of oversized tool results to the virtual filesystem.

When a tool returns more content than ``OffloadConfig.threshold_tokens``, the
scheduler swaps the bulky ``ToolResult.content`` for a short preview plus a path
reference, and stashes the full text in the filesystem backend.  The model then
pulls back only what it needs via the ``read_file`` tool.  This mirrors the Deep
Agents behaviour: *"detect a tool response exceeding 20,000 tokens, offload it to
the filesystem, and substitute a file path reference and a preview of the first
10 lines."*

Opt-in: nothing happens unless ``Agent(result_offload=OffloadConfig(...))`` is
set.  The full result still travels on ``ToolCallEndEvent.tool_result`` for
observers; only what the LLM sees in conversation history is shrunk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools.base import ToolResult
from .backend import FileBackend, normalize_path


@dataclass
class OffloadConfig:
    """Configuration for automatic large-result offloading.

    Attributes:
        enabled: Master switch.  ``False`` disables offload even when a config
            object is present.
        threshold_tokens: Results estimated above this many tokens are
            offloaded.  ``None`` (the default) means *derive from the model's
            context window*: ``Agent.__init__`` resolves it to
            ``int(context_window * threshold_fraction)`` at construction time
            so the threshold scales with the model.  Pass an explicit integer
            to override (e.g. ``threshold_tokens=5_000``).
        threshold_fraction: Fraction of the model's context window used as the
            threshold when ``threshold_tokens`` is ``None``.  Defaults to
            ``0.1`` (10 %).  A 200 k-token model → 20 k; a 32 k model → 3.2 k.
        preview_lines: Number of leading lines kept inline as a preview.
        path_prefix: Virtual directory offloaded results are written under.
        skip_tools: Tool names never offloaded.  The filesystem tools
            themselves are always skipped so reading a large file back does not
            re-offload it.
    """

    enabled: bool = True
    threshold_tokens: int | None = None
    threshold_fraction: float = 0.1
    preview_lines: int = 10
    path_prefix: str = "/offload"
    skip_tools: frozenset[str] = field(
        default_factory=lambda: frozenset({"read_file", "write_file", "edit_file", "ls"})
    )


# Conservative chars-per-token proxy, matching the loop's default estimator
# (``len(text) // 4``).  Avoids importing a tokenizer for a coarse threshold.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str, estimator: Any = None, model: str | None = None) -> int:
    """Estimate token count for *text*, using *estimator* when available."""
    if callable(estimator):
        try:
            # Agent token estimators take (messages, model); wrap the string in
            # a minimal message-like shim so a custom estimator can run.
            from ..types import Message, TextBlock

            msg = Message(role="user", content=[TextBlock(text=text)])
            value: Any = estimator([msg], model)
            return max(0, int(value))
        except Exception:
            pass
    return len(text) // _CHARS_PER_TOKEN


def _preview(text: str, lines: int) -> str:
    rows = text.split("\n")
    head = "\n".join(rows[:lines])
    return head


async def maybe_offload(
    result: ToolResult,
    *,
    tool_name: str,
    call_id: str,
    backend: FileBackend,
    config: OffloadConfig,
    token_estimator: Any = None,
    model: str | None = None,
) -> ToolResult:
    """Offload *result* to *backend* when it exceeds the configured threshold.

    Returns the (possibly mutated) result.  Never offloads error results, the
    filesystem tools' own output, or content already under the threshold.  On
    any backend write failure the original result is returned unchanged so a
    storage hiccup never breaks the run.
    """
    if not config.enabled or result.is_error or tool_name in config.skip_tools:
        return result

    threshold = config.threshold_tokens
    if threshold is None:
        # threshold was not resolved at Agent init (e.g. a bare OffloadConfig
        # used outside Agent) — skip offload rather than use an arbitrary value.
        return result

    content = result.content
    if not isinstance(content, str):
        return result

    tokens = estimate_tokens(content, token_estimator, model)
    if tokens <= threshold:
        return result

    path = normalize_path(f"{config.path_prefix}/{tool_name}_{call_id}.txt")
    try:
        await backend.write(path, content)
    except Exception:
        return result

    preview = _preview(content, config.preview_lines)
    result.content = (
        f"{preview}\n\n"
        f"[Tool result truncated: ~{tokens} tokens ({len(content)} chars) offloaded to "
        f"'{path}'. Showing the first {config.preview_lines} lines. "
        f'Use read_file(path="{path}", offset=..., limit=...) to read the rest, '
        f'or ls("{config.path_prefix}") to list offloaded results.]'
    )
    result.truncated = True
    result.metadata = {
        **result.metadata,
        "offloaded_to": path,
        "original_tokens": tokens,
        "original_chars": len(content),
    }
    return result
