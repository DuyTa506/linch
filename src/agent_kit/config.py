"""Typed configuration objects for AgentKit.

These replace the loose ``dict`` / ``Any`` parameters on ``Agent`` with
typed, IDE-friendly dataclasses while keeping all existing dict/camelCase
kwargs working for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class FeatureFlags:
    """Controls which optional subsystems are enabled when creating a session.

    Setting a flag to ``False`` skips the corresponding ``connect_*`` call in
    :meth:`Agent.session`, so the subsystem's tools and prompt additions are
    never loaded.

    Example::

        agent = Agent(
            model="gpt-5",
            features=FeatureFlags(skills=False, mcp=False),
        )
    """

    skills: bool = True
    subagents: bool = True
    mcp: bool = True
    filesystem: bool = True


@dataclass
class SystemPromptSection:
    """Named system-prompt section rendered as a :class:`SystemBlock`.

    ``placement`` controls where the section is inserted relative to the
    built-in prompt layers:

    - ``"before_defaults"``: before built-in identity/protocol blocks.
    - ``"after_defaults"``: after identity/protocol and before environment.
    - ``"after_env"``: after the environment block and before appended user
      instructions.

    Section names are metadata for callers/tests; the rendered block contains
    only ``text``.
    """

    name: str
    text: str
    cacheable: bool = True
    placement: Literal["before_defaults", "after_defaults", "after_env"] = (
        "before_defaults"
    )


@dataclass
class SystemPromptConfig:
    """Controls how the agent system prompt is constructed.

    Attributes:
        append:
            Text appended as a ``"User-provided instructions"`` block,
            equivalent to the legacy ``system_prompt`` / ``systemPrompt``
            parameter.  If both ``append`` and ``system_prompt`` are set,
            ``append`` wins.
        blocks:
            Additional :class:`~agent_kit.types.SystemBlock` objects to
            include.  When ``replace_defaults=False`` these are *prepended*
            before the built-in identity / protocol blocks; when
            ``replace_defaults=True`` they are the only blocks (besides
            ``env_text`` and ``append``).
        sections:
            Additional named prompt sections.  These are converted to
            ``SystemBlock`` objects and inserted according to each section's
            ``placement``.  Use this for reusable prompt policies without
            replacing the default AgentKit prompt.
        replace_defaults:
            When ``True`` the built-in SWE identity and protocol blocks are
            omitted entirely.  Use this for non-SWE agents (RAG, text-to-SQL,
            document analysis) where those descriptions are misleading.

    Example — fully custom prompt::

        config = SystemPromptConfig(
            replace_defaults=True,
            append="You are a SQL expert. Answer only in valid SQL.",
        )
        agent = Agent(model="gpt-5", system_prompt_config=config)
    """

    append: str | None = None
    # list[SystemBlock] typed as Any to avoid a circular import at module level
    blocks: list[Any] | None = field(default=None)
    sections: list[SystemPromptSection] | None = field(default=None)
    replace_defaults: bool = False
