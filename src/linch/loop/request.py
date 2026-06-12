"""Per-turn request assembly: user messages, context builds, provider
capability downgrades, and the :class:`ProviderRequest` builder."""

from __future__ import annotations

import time
from typing import Any, Literal, cast

from ..context import ContextBuildResult, apply_context_budget
from ..session import RunOptions, Session
from ..types import (
    ContentBlock,
    ImageBlock,
    Message,
    ProviderRequest,
    TextBlock,
)

ProviderEffort = Literal["low", "medium", "high", "xhigh", "max"]
CacheTtl = Literal["5m", "1h"]


def _provider_effort(value: str | None) -> ProviderEffort | None:
    if value in {"low", "medium", "high", "xhigh", "max"}:
        return cast(ProviderEffort, value)
    return None


def _cache_ttl(value: str | None) -> CacheTtl | None:
    if value in {"5m", "1h"}:
        return cast(CacheTtl, value)
    return None


def build_user_message(prompt: str, images: list[dict[str, str]] | None = None) -> Message:
    content: list[ContentBlock] = [
        TextBlock(text="<env>\nToday's date: " + time.strftime("%Y-%m-%d") + "\n</env>"),
        TextBlock(text=prompt),
    ]
    for image in images or []:
        if "url" in image:
            content.append(ImageBlock(source={"type": "url", "url": image["url"]}))
        else:
            content.append(
                ImageBlock(
                    source={
                        "type": "base64",
                        "media_type": image["media_type"],
                        "data": image["data"],
                    }
                )
            )
    return Message(role="user", content=content)


def final_text(message: Message) -> str | None:
    for block in message.content:
        if isinstance(block, TextBlock):
            return block.text
    return None


def _re_inject_skill_context(session: Session) -> None:
    agent = session.agent
    if not agent.skill_listing_text and not session.invoked_skills:
        return
    from ..skills.system_reminder import wrap_in_system_reminder

    if agent.skill_listing_text:
        text = wrap_in_system_reminder(agent.skill_listing_text)
        session.provider_view.append(Message(role="user", content=[TextBlock(text=text)]))
    for rec in session.invoked_skills:
        text = wrap_in_system_reminder(
            f"Below is the body of a previously invoked skill "
            f"named '{rec.name}'.\n\n{rec.substituted_body}"
        )
        session.provider_view.append(Message(role="user", content=[TextBlock(text=text)]))


async def _build_context_result(session: Session, turn_index: int) -> ContextBuildResult | None:
    agent = session.agent
    context_hooks = [
        hook
        for hook in getattr(agent, "hooks", [])
        if callable(getattr(hook, "build_context", None))
    ]
    if not context_hooks:
        return None
    merged = ContextBuildResult()
    for hook in context_hooks:
        result = await hook.build_context(session, turn_index)
        if result is None:
            continue
        merged.system_blocks.extend(result.system_blocks)
        merged.messages.extend(result.messages)
        if result.selected_tools is not None:
            merged.selected_tools = result.selected_tools
        if result.budget.max_tokens is not None:
            merged.budget.max_tokens = result.budget.max_tokens
        merged.budget.used_tokens += result.budget.used_tokens
        if result.budget.remaining_tokens is not None:
            merged.budget.remaining_tokens = result.budget.remaining_tokens
        merged.budget.trimmed = merged.budget.trimmed or result.budget.trimmed
        merged.metadata.update(result.metadata)
    if (
        not merged.system_blocks
        and not merged.messages
        and merged.selected_tools is None
        and not merged.metadata
    ):
        return None
    # Each hook budgets its own result in isolation; with more than one context
    # hook their concatenation could exceed the intended cap, so re-apply the
    # budget over the merged union (no-op for a single hook / unset budget).
    if len(context_hooks) > 1 and merged.budget.max_tokens is not None:
        return apply_context_budget(
            merged,
            estimator=getattr(agent, "token_estimator", None),
            model=agent.model,
        )
    return merged


def apply_provider_capabilities(req: ProviderRequest, caps: Any) -> ProviderRequest:
    """Downgrade *req* fields to match what *caps* says the provider supports.

    * ``prompt_cache=False`` → clears ``req.cache_prompt`` and
      ``req.cache_ttl`` so providers that ignore caching don't receive dead
      flags (fixes current dead-plumbing where every request sends
      ``cache_prompt=True`` regardless of provider).
    * ``tool_choice=False`` → clears ``req.tool_choice``.
    * ``structured_output=False`` → clears ``req.output_schema``; the loop
      still text-parses using ``opts/agent.output_schema`` at
      :func:`run_loop` line ~452, so the host's intent is preserved.
    * ``parallel_tool_calls`` is informational and has no ``req`` field yet.

    Modifies *req* in place and returns it.
    """
    if not caps.prompt_cache:
        req.cache_prompt = None
        req.cache_ttl = None
    if not caps.tool_choice:
        req.tool_choice = None
    if not caps.structured_output:
        req.output_schema = None
    return req


def _build_turn_request(
    session: Session,
    opts: RunOptions,
    *,
    context: ContextBuildResult | None = None,
    model_override: str | None = None,
) -> ProviderRequest:
    """Build the :class:`ProviderRequest` for one provider call.

    Collapses the two near-identical request builders (normal path and
    ContextLengthError retry path) into one place.  Applies provider
    capability downgrades before returning.
    """
    agent = session.agent

    base_system = list(session.system_blocks_override or agent.system_blocks)
    if context and context.system_blocks:
        base_system = base_system + list(context.system_blocks)

    messages = list(session.provider_view)
    if context and context.messages:
        messages.extend(context.messages)

    tools = _select_context_tools(session, context)

    req = ProviderRequest(
        model=model_override or agent.model,
        system=base_system,
        tools=tools.schemas(),
        messages=messages,
        max_output_tokens=opts.max_output_tokens or agent.max_output_tokens,
        temperature=opts.temperature,
        thinking=opts.thinking,
        effort=_provider_effort(opts.effort),
        output_schema=opts.output_schema or agent.output_schema,
        tool_choice=opts.tool_choice or agent.tool_choice,
        max_retries=agent.max_retries,
        cache_ttl=_cache_ttl(agent.cache_ttl),
        cache_prompt=True,
    )

    # Apply provider capability downgrades (e.g. clear cache_prompt for
    # providers that don't support it, strip output_schema when the
    # provider has no native structured output, etc.).
    if hasattr(agent.provider, "capabilities"):
        caps = agent.provider.capabilities(req.model)
        apply_provider_capabilities(req, caps)

    return req


def _select_context_tools(session: Session, context: ContextBuildResult | None) -> Any:
    registry = session.tools_override or session.agent.tools
    if context is None or context.selected_tools is None:
        return registry

    selected = context.selected_tools
    if hasattr(selected, "schemas") and hasattr(selected, "get"):
        return selected
    if isinstance(selected, str):
        return registry.select(names={selected})
    if isinstance(selected, dict):
        names = selected.get("names")
        tags = selected.get("tags")
        return registry.select(
            names={str(name) for name in names} if isinstance(names, (list, set, tuple)) else None,
            tags={str(tag) for tag in tags} if isinstance(tags, (list, set, tuple)) else None,
        )
    if isinstance(selected, (list, set, tuple)):
        return registry.select(names={str(name) for name in selected})
    return registry


def _context_selected_tool_names(context: ContextBuildResult | None) -> list[str] | None:
    if context is None or context.selected_tools is None:
        return None
    selected = context.selected_tools
    if hasattr(selected, "list"):
        return sorted(tool.name for tool in selected.list())
    if isinstance(selected, str):
        return [selected]
    if isinstance(selected, dict):
        names = selected.get("names")
        tags = selected.get("tags")
        parts: list[str] = []
        if isinstance(names, (list, set, tuple)):
            parts.extend(str(name) for name in names)
        if isinstance(tags, (list, set, tuple)):
            parts.extend(f"tag:{tag}" for tag in tags)
        return sorted(parts)
    if isinstance(selected, (list, set, tuple)):
        return sorted(str(name) for name in selected)
    return None
