from __future__ import annotations

import re
from typing import Any

import pytest

from linch import RedactionRule
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.stores import memory_session_store


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

    assert hook.redact("anything user@x.com") == "anything user@x.com"
    assert await hook.on_post_tool_use(_post_ctx(ToolResult(content="user@x.com"))) is None
    assert await hook.on_before_final_answer(_final_ctx("user@x.com")) is None
    assert await hook.on_user_prompt_submit(_prompt_ctx("user@x.com")) is None


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


@pytest.mark.parametrize(
    "result, config",
    [
        ("no secrets here", {}),
        ("a@b.com", {"redact_tool_results": False}),
        (None, {}),
    ],
)
async def test_post_tool_use_no_mutation_cases(result: str | None, config: dict[str, Any]) -> None:
    from linch import ToolResult

    assert (
        await _email_hook(**config).on_post_tool_use(
            _post_ctx(None if result is None else ToolResult(content=result))
        )
        is None
    )


# ── Final answer scrubbing (BeforeFinalAnswer) ────────────────────────────────


@pytest.mark.parametrize(
    "final_text, config, expected",
    [
        ("contact me@here.com please", {}, "contact [EMAIL] please"),
        ("nothing sensitive", {}, None),
        ("", {}, None),
        (None, {}, None),
        ("a@b.com", {"redact_final_answer": False}, None),
    ],
)
async def test_before_final_answer_cases(
    final_text: str | None, config: dict[str, Any], expected: str | None
) -> None:
    out = await _email_hook(**config).on_before_final_answer(_final_ctx(final_text))
    if expected is None:
        assert out is None
    else:
        assert out is not None and out.action == "mutate"
        assert out.final_text == expected


# ── Prompt scrubbing (UserPromptSubmit) — off by default ──────────────────────


@pytest.mark.parametrize(
    "prompt, enabled, expected",
    [("a@b.com", False, None), ("from a@b.com", True, "from [EMAIL]")],
)
async def test_user_prompt_redaction_cases(
    prompt: str, enabled: bool, expected: str | None
) -> None:
    out = await _email_hook(redact_user_prompt=enabled).on_user_prompt_submit(_prompt_ctx(prompt))
    if expected is None:
        assert out is None
    else:
        assert out is not None and out.action == "mutate"
        assert out.prompt == expected


# ── Rule semantics ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rules, text, expected",
    [
        (
            (RedactionRule(r"secret", "[S]"), RedactionRule(r"\[S\]-token", "[TOKEN]")),
            "the secret-token here",
            "the [TOKEN] here",
        ),
        (
            (RedactionRule(r"password", "[PW]", flags=re.IGNORECASE),),
            "PASSWORD and password",
            "[PW] and [PW]",
        ),
        ((RedactionRule(r"(secret)", r"[\1]"),), "secret", "[secret]"),
        (
            (RedactionRule(r"(?P<token>secret)", r"[\g<token>]"),),
            "secret",
            "[secret]",
        ),
    ],
)
async def test_rule_replacement_cases(rules: tuple[Any, ...], text: str, expected: str) -> None:
    from linch import RedactionConfig, RedactionHook

    assert RedactionHook(RedactionConfig(rules=rules)).redact(text) == expected


async def test_invalid_pattern_raises_at_construction() -> None:
    import re

    from linch import RedactionConfig, RedactionHook, RedactionRule

    with pytest.raises(re.error):
        RedactionHook(RedactionConfig(rules=(RedactionRule(r"(unclosed", "x"),)))


@pytest.mark.parametrize("replacement", [r"\1-XX-XXXX", r"\g<missing>", r"\x"])
def test_invalid_replacement_raises_config_error_at_construction(replacement: str) -> None:
    from linch import ConfigError, RedactionConfig, RedactionHook, RedactionRule

    with pytest.raises(ConfigError, match="replacement"):
        RedactionHook(RedactionConfig(rules=(RedactionRule(r"\d{3}-\d{2}-\d{4}", replacement),)))


# ── Public API ────────────────────────────────────────────────────────────────


def test_redaction_symbols_are_public() -> None:
    import linch

    for name in ("RedactionHook", "RedactionConfig", "RedactionRule"):
        assert name in linch.__all__


# ── Integration through the loop ──────────────────────────────────────────────


async def test_final_answer_is_redacted_end_to_end(tmp_path: Any) -> None:
    from linch import RedactionConfig, RedactionHook, ResultEvent

    hook = RedactionHook(
        RedactionConfig(rules=(RedactionRule(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL]"),))
    )
    agent = make_agent(
        model="m",
        provider=ScriptedProvider([TextTurn("Done — ping me at agent@corp.com")]),
        cwd=str(tmp_path),
        permissions={"mode": "skip-dangerous"},
        session_store=memory_session_store(),
        run_store=None,
        hooks=[hook],
    )

    session = await agent.session()
    events = [event async for event in session.run("go")]
    result = next(event for event in events if isinstance(event, ResultEvent))

    assert "agent@corp.com" not in (result.final_text or "")
    assert "[EMAIL]" in (result.final_text or "")
