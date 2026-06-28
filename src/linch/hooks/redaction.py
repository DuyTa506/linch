"""Pattern-based redaction hook — a policy-free governance seam.

Linch ships the *mechanism* (apply caller-supplied regex rules to the text that
flows through the loop) and **no default patterns**. The embedder owns the
policy: which patterns count as sensitive, what to replace them with, and which
surfaces to scrub. With an empty rule set the hook is a no-op, so adding it
keeps loop behavior byte-identical until the host supplies rules.

Surfaces (each independently toggleable):

- ``PostToolUse`` — scrub ``ToolResult`` text before it re-enters the
  provider view and reaches the model.
- ``BeforeFinalAnswer`` — scrub the final answer text returned to the caller.
- ``UserPromptSubmit`` — scrub the user's prompt before it is recorded
  (off by default).

Example::

    from linch import Agent, RedactionHook, RedactionConfig, RedactionRule

    redact = RedactionHook(
        RedactionConfig(
            rules=(
                RedactionRule(r"[\\w.+-]+@[\\w-]+\\.[\\w.-]+", "[EMAIL]"),
                RedactionRule(r"sk-[A-Za-z0-9]{20,}", "[API_KEY]"),
            )
        )
    )
    agent = Agent(..., hooks=[redact])
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ..tools import ToolResult
from .contexts import BeforeFinalAnswerContext, PostToolUseContext, UserPromptSubmitContext
from .types import HookResult


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """One redaction rule: replace text matching ``pattern`` with ``replacement``.

    ``pattern`` is a regular expression compiled with :func:`re.compile`. An
    invalid pattern raises at :class:`RedactionHook` construction, not at
    runtime. Linch ships no rules — the host supplies them.
    """

    pattern: str
    replacement: str = "[REDACTED]"
    flags: int = 0


@dataclass(frozen=True, slots=True)
class RedactionConfig:
    """Configuration for :class:`RedactionHook`.

    With the default empty ``rules`` the hook does nothing, so attaching it is
    safe before any policy exists.
    """

    rules: tuple[RedactionRule, ...] = field(default_factory=tuple)
    #: Scrub ``ToolResult`` content/summary/recovery_hint at ``PostToolUse``.
    redact_tool_results: bool = True
    #: Scrub the final answer text at ``BeforeFinalAnswer``.
    redact_final_answer: bool = True
    #: Scrub the user's prompt at ``UserPromptSubmit`` (off by default — the
    #: prompt is the host's own input, so scrubbing it is an explicit choice).
    redact_user_prompt: bool = False


class RedactionHook:
    """Apply caller-supplied regex redaction rules to loop text.

    Mechanism only: with no rules it is a no-op. The host decides what is
    sensitive and how to mask it, so no domain policy lives in core.
    """

    name = "redaction"

    def __init__(self, config: RedactionConfig | None = None) -> None:
        self.config = config or RedactionConfig()
        # Compile eagerly so a bad pattern fails fast at construction and each
        # call avoids recompiling.
        self._compiled: tuple[tuple[re.Pattern[str], str], ...] = tuple(
            (re.compile(rule.pattern, rule.flags), rule.replacement) for rule in self.config.rules
        )

    def redact(self, text: str) -> str:
        """Apply every rule in order. Public so hosts can reuse the same masking
        elsewhere (e.g. log scrubbing) with one source of truth."""
        if not text or not self._compiled:
            return text
        for pattern, replacement in self._compiled:
            text = pattern.sub(replacement, text)
        return text

    async def on_post_tool_use(self, ctx: PostToolUseContext) -> HookResult | None:
        if not self.config.redact_tool_results or not self._compiled:
            return None
        result = ctx.result
        if result is None:
            return None
        content = self.redact(result.content)
        summary = self.redact(result.summary)
        recovery_hint = self.redact(result.recovery_hint)
        if (
            content == result.content
            and summary == result.summary
            and recovery_hint == result.recovery_hint
        ):
            return None
        scrubbed: ToolResult = replace(
            result, content=content, summary=summary, recovery_hint=recovery_hint
        )
        return HookResult.mutate(tool_result=scrubbed)

    async def on_before_final_answer(self, ctx: BeforeFinalAnswerContext) -> HookResult | None:
        if not self.config.redact_final_answer or not self._compiled:
            return None
        text = ctx.final_text
        if not text:
            return None
        redacted = self.redact(text)
        if redacted == text:
            return None
        return HookResult.mutate(final_text=redacted)

    async def on_user_prompt_submit(self, ctx: UserPromptSubmitContext) -> HookResult | None:
        if not self.config.redact_user_prompt or not self._compiled:
            return None
        text = ctx.prompt
        if not text:
            return None
        redacted = self.redact(text)
        if redacted == text:
            return None
        return HookResult.mutate(prompt=redacted)
