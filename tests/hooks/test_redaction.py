from __future__ import annotations

from typing import Any

import pytest


def _post_ctx(result: Any) -> Any:
    from linch import PostToolUseContext

    return PostToolUseContext(
        session=object(),
        run_id="r1",
        turn_index=0,
        tool_use_id="t1",
        tool_name="Bash",
        input={},
        result=result,
    )


def _final_ctx(final_text: str | None) -> Any:
    from linch import BeforeFinalAnswerContext

    return BeforeFinalAnswerContext(
        session=object(),
        run_id="r1",
        turn_index=0,
        final_text=final_text,
    )


def _prompt_ctx(prompt: str) -> Any:
    from linch import UserPromptSubmitContext

    return UserPromptSubmitContext(
        session=object(),
        run_id="r1",
        turn_index=0,
        prompt=prompt,
    )


def _email_hook(**config_kwargs: Any) -> Any:
    from linch import RedactionConfig, RedactionHook, RedactionRule

    rules = (RedactionRule(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL]"),)
    return RedactionHook(RedactionConfig(rules=rules, **config_kwargs))


# ── No-op / default behavior ──────────────────────────────────────────────────


async def test_empty_config_is_a_no_op_on_every_surface() -> None:
    from linch import RedactionHook, ToolResult

    hook = RedactionHook()  # no rules

    assert await hook.on_post_tool_use(_post_ctx(ToolResult(content="user@x.com"))) is None
    assert await hook.on_before_final_answer(_final_ctx("user@x.com")) is None
    assert await hook.on_user_prompt_submit(_prompt_ctx("user@x.com")) is None


async def test_redact_returns_text_unchanged_when_no_rules() -> None:
    from linch import RedactionHook

    assert RedactionHook().redact("anything user@x.com") == "anything user@x.com"


# ── Tool result scrubbing (PostToolUse) ───────────────────────────────────────


async def test_post_tool_use_scrubs_content_summary_and_recovery_hint() -> None:
    from linch import ToolResult

    hook = _email_hook()
    result = ToolResult(
        content="reach me at a@b.com",
        summary="mailed c@d.com",
        recovery_hint="retry as e@f.com",
        is_error=True,
        duration_ms=42,
    )

    out = await hook.on_post_tool_use(_post_ctx(result))

    assert out is not None and out.action == "mutate"
    scrubbed = out.tool_result
    assert scrubbed.content == "reach me at [EMAIL]"
    assert scrubbed.summary == "mailed [EMAIL]"
    assert scrubbed.recovery_hint == "retry as [EMAIL]"
    # Non-text fields are preserved.
    assert scrubbed.is_error is True
    assert scrubbed.duration_ms == 42
    # Original is not mutated in place.
    assert result.content == "reach me at a@b.com"


async def test_post_tool_use_returns_none_when_nothing_matches() -> None:
    from linch import ToolResult

    hook = _email_hook()
    assert await hook.on_post_tool_use(_post_ctx(ToolResult(content="no secrets here"))) is None


async def test_post_tool_use_respects_disable_toggle() -> None:
    from linch import ToolResult

    hook = _email_hook(redact_tool_results=False)
    assert await hook.on_post_tool_use(_post_ctx(ToolResult(content="a@b.com"))) is None


async def test_post_tool_use_handles_missing_result() -> None:
    hook = _email_hook()
    assert await hook.on_post_tool_use(_post_ctx(None)) is None


# ── Final answer scrubbing (BeforeFinalAnswer) ────────────────────────────────


async def test_before_final_answer_scrubs_text() -> None:
    hook = _email_hook()
    out = await hook.on_before_final_answer(_final_ctx("contact me@here.com please"))

    assert out is not None and out.action == "mutate"
    assert out.final_text == "contact [EMAIL] please"


async def test_before_final_answer_returns_none_when_clean_or_empty() -> None:
    hook = _email_hook()
    assert await hook.on_before_final_answer(_final_ctx("nothing sensitive")) is None
    assert await hook.on_before_final_answer(_final_ctx("")) is None
    assert await hook.on_before_final_answer(_final_ctx(None)) is None


async def test_before_final_answer_respects_disable_toggle() -> None:
    hook = _email_hook(redact_final_answer=False)
    assert await hook.on_before_final_answer(_final_ctx("a@b.com")) is None


# ── Prompt scrubbing (UserPromptSubmit) — off by default ──────────────────────


async def test_user_prompt_not_scrubbed_by_default() -> None:
    hook = _email_hook()  # redact_user_prompt defaults False
    assert await hook.on_user_prompt_submit(_prompt_ctx("a@b.com")) is None


async def test_user_prompt_scrubbed_when_enabled() -> None:
    hook = _email_hook(redact_user_prompt=True)
    out = await hook.on_user_prompt_submit(_prompt_ctx("from a@b.com"))
    assert out is not None and out.action == "mutate"
    assert out.prompt == "from [EMAIL]"


# ── Rule semantics ────────────────────────────────────────────────────────────


async def test_multiple_rules_apply_in_order() -> None:
    from linch import RedactionConfig, RedactionHook, RedactionRule

    hook = RedactionHook(
        RedactionConfig(
            rules=(
                RedactionRule(r"secret", "[S]"),
                RedactionRule(r"\[S\]-token", "[TOKEN]"),
            )
        )
    )
    # First rule turns "secret-token" into "[S]-token", second collapses it.
    assert hook.redact("the secret-token here") == "the [TOKEN] here"


async def test_rule_flags_are_honored() -> None:
    import re

    from linch import RedactionConfig, RedactionHook, RedactionRule

    hook = RedactionHook(
        RedactionConfig(rules=(RedactionRule(r"password", "[PW]", flags=re.IGNORECASE),))
    )
    assert hook.redact("PASSWORD and password") == "[PW] and [PW]"


async def test_invalid_pattern_raises_at_construction() -> None:
    import re

    from linch import RedactionConfig, RedactionHook, RedactionRule

    with pytest.raises(re.error):
        RedactionHook(RedactionConfig(rules=(RedactionRule(r"(unclosed", "x"),)))


# ── Public API ────────────────────────────────────────────────────────────────


def test_redaction_symbols_are_public() -> None:
    import linch

    for name in ("RedactionHook", "RedactionConfig", "RedactionRule"):
        assert name in linch.__all__


# ── Integration through the loop ──────────────────────────────────────────────


async def test_final_answer_is_redacted_end_to_end(tmp_path: Any) -> None:
    from linch import Agent, RedactionConfig, RedactionHook, RedactionRule, ResultEvent
    from linch.config import FeatureFlags
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore

    hook = RedactionHook(
        RedactionConfig(rules=(RedactionRule(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL]"),))
    )
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("Done — ping me at agent@corp.com")]),
        cwd=str(tmp_path),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        hooks=[hook],
    )

    session = await agent.session()
    events = [event async for event in session.run("go")]
    result = next(event for event in events if isinstance(event, ResultEvent))

    assert "agent@corp.com" not in (result.final_text or "")
    assert "[EMAIL]" in (result.final_text or "")
