"""Workflow recipes — pre-configured Agent factories for common patterns.

Each recipe returns a fully configured :class:`~linch.agent.Agent` that
you can use directly or adapt further.  They are deliberately thin: they call
the same public API you would write yourself and serve as both working
starters and living documentation of how the primitives compose.

Recipes:

.. list-table::
   :header-rows: 1

   * - Module
     - Use-case
     - Key primitives
   * - :mod:`linch.recipes.rag`
     - Retrieval-Augmented Generation
     - :class:`~linch.context.ContextBuilder`, ``deps``,
       :class:`~linch.types.OutputSchema`
   * - :mod:`linch.recipes.text_to_sql`
     - Natural-language → SQL
     - ``final_tool_name``, :class:`~linch.types.OutputSchema`,
       ``deps``
   * - :mod:`linch.recipes.doc_analysis`
     - Document / image structured extraction
     - :class:`~linch.types.OutputSchema`, image inputs

Custom domains
--------------
Use :func:`build_agent` as a starting point — it wires up the common
configuration knobs in one call::

    from linch.recipes import build_agent
    from linch.types import OutputSchema

    agent = build_agent(
        model="gpt-5",
        system_instructions="You are a customer-support assistant.",
        tools=my_tools,
        output_schema=OutputSchema(
            name="support_reply",
            schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        ),
        deps={"kb": knowledge_base},
    )
"""

from __future__ import annotations

from typing import Any

from ..agent import Agent
from ..config import FeatureFlags, SystemPromptConfig
from ..tools.registry import ToolRegistry


def build_agent(
    *,
    model: str,
    system_instructions: str | None = None,
    tools: ToolRegistry | None = None,
    output_schema: Any = None,
    tool_choice: Any = None,
    final_tool_name: str | None = None,
    context_builder: Any = None,
    deps: Any = None,
    replace_default_system: bool = False,
    disable_skills: bool = True,
    disable_subagents: bool = True,
    disable_mcp: bool = True,
    **agent_kwargs: Any,
) -> Agent:
    """Create a domain-agnostic :class:`~linch.agent.Agent`.

    This is the generic scaffold all built-in recipes build on.  It is the
    recommended starting point for custom domains: set the knobs you care
    about and leave the rest as defaults.

    Args:
        model: Model identifier (e.g. ``"gpt-5"``).
        system_instructions: Text that replaces (when
            *replace_default_system* is ``True``) or appends to the built-in
            Linch system prompt.
        tools: Tool registry.  Pass ``empty_tools(...)`` or
            ``tools_from_defaults(exclude={...})`` as needed.
        output_schema: :class:`~linch.types.OutputSchema` for structured
            JSON output.
        tool_choice: :class:`~linch.types.ToolChoice` override.
        final_tool_name: Name of a "terminal" tool whose invocation stops the
            loop and sets ``ResultEvent.structured_output``.
        context_builder: :class:`~linch.context.ContextBuilder` instance
            run before each provider call.
        deps: Application-state dependency object available in every tool's
            ``ctx.deps``.
        replace_default_system: When ``True``, the SWE identity + protocol
            blocks are omitted; *system_instructions* is the whole prompt.
        disable_skills / disable_subagents / disable_mcp: Skip loading those
            subsystems in :meth:`~linch.agent.Agent.session`.  Defaults
            to ``True`` because most domain-specific agents don't use them.
        **agent_kwargs: Any additional kwargs are forwarded to
            :class:`~linch.agent.Agent`.
    """
    cfg = SystemPromptConfig(
        append=system_instructions,
        replace_defaults=replace_default_system,
    )
    features = FeatureFlags(
        skills=not disable_skills,
        subagents=not disable_subagents,
        mcp=not disable_mcp,
    )
    return Agent(
        model=model,
        tools=tools,
        system_prompt_config=cfg,
        output_schema=output_schema,
        tool_choice=tool_choice,
        final_tool_name=final_tool_name,
        context_builder=context_builder,
        deps=deps,
        features=features,
        **agent_kwargs,
    )
