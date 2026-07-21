"""Conversational ask → plan → approve → build turns for the authoring service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from linch import ScriptedProvider, TextTurn, ToolUseTurn, Usage

from linch_studio.authoring import (
    AgentBuildRequest,
    AuthoringConfig,
    AuthoringMessage,
    InvalidInstructionError,
    LinchProposalService,
    ProposalGenerationError,
    ProposalNotFoundError,
    SemanticProposalError,
    build_conversation_prompt,
    create_authoring_agent,
)
from linch_studio.authoring.knowledge import KnowledgeBase, build_toc
from linch_studio.spec import Blueprint, Diagnostic, canonical_json, default_blueprint


class RecordingScriptedProvider(ScriptedProvider):
    def __init__(self, turns: list[TextTurn]) -> None:
        super().__init__(turns)
        self.requests: list[Any] = []

    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(request)
        async for event in super().stream(request):
            yield event


class ThinkingScriptedProvider(RecordingScriptedProvider):
    """Injects one thinking_delta right after each scripted turn starts."""

    def __init__(self, turns: list[TextTurn], thoughts: list[str | None]) -> None:
        super().__init__(turns)
        self._thoughts = thoughts

    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        index = len(self.requests)
        thought = self._thoughts[index] if index < len(self._thoughts) else None
        async for event in super().stream(request):
            yield event
            if event.get("type") == "message_start" and thought:
                yield {"type": "thinking_delta", "text": thought}


def _config(**update: Any) -> AuthoringConfig:
    values: dict[str, Any] = {
        "provider": "openai_responses",
        "model": "gpt-5",
        "api_key": "offline-test-key",
        "timeout_seconds": 10.0,
    }
    values.update(update)
    return AuthoringConfig(**values)


def _blueprint(update: dict[str, Any] | None = None) -> Blueprint:
    data = default_blueprint("demo").model_dump(mode="json", by_alias=True)
    data["spec"]["runtime"]["provider"] = {"kind": "openai_responses", "model": "gpt-5"}
    if update:
        _merge(data, update)
    return Blueprint.model_validate(data, strict=True)


def _merge(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _q(text: str, *options: str) -> dict[str, Any]:
    return {"question": text, "options": list(options) or ["openai", "anthropic", "a local model"]}


def _turn_json(
    *,
    blueprint: Blueprint | None = None,
    questions: list[dict[str, Any]] | None = None,
    plan: str | None = None,
    note: str | None = None,
    summary: str | None = None,
) -> str:
    rendered = "null" if blueprint is None else canonical_json(blueprint)
    head = json.dumps({"questions": questions, "plan": plan, "note": note, "summary": summary})
    return head[:-1] + ',"blueprint":' + rendered + "}"


def _request_text(request: Any) -> str:
    return "\n".join(
        block.text
        for message in request.messages
        for block in message.content
        if getattr(block, "text", None) is not None
    )


def _service(
    provider: ScriptedProvider,
    *,
    knowledge: KnowledgeBase | None = None,
    agent_factory: Any = create_authoring_agent,
) -> LinchProposalService:
    return LinchProposalService(
        _config(),
        provider_factory=lambda _: provider,
        agent_factory=agent_factory,
        id_factory=lambda: "turn-proposal-1",
        knowledge=knowledge,
    )


def _tmp_knowledge(tmp_path: Path) -> KnowledgeBase:
    (tmp_path / "sdk" / "usage").mkdir(parents=True)
    text = "# Tools\n\n## Retry\n\nretry guidance\n"
    (tmp_path / "sdk" / "usage" / "tools.md").write_text(text, encoding="utf-8")
    toc = build_toc({"usage/tools.md": text})
    (tmp_path / "toc.json").write_text(json.dumps(toc), encoding="utf-8")
    return KnowledgeBase(root=tmp_path)


def _user(content: str) -> AuthoringMessage:
    return AuthoringMessage(role="user", content=content)


def _assistant(content: str) -> AuthoringMessage:
    return AuthoringMessage(role="assistant", content=content)


async def test_first_turn_returns_option_backed_questions_and_records_nothing() -> None:
    provider = RecordingScriptedProvider(
        [
            TextTurn(
                _turn_json(
                    questions=[_q("Which provider?", "openai", "anthropic", "local vllm")],
                    note="plan: one agent",
                ),
                usage=Usage(input_tokens=5, output_tokens=7),
            )
        ]
    )
    service = _service(provider)

    turn = await service.converse(_blueprint(), [_user("build me a review workflow")])

    assert turn.kind == "questions"
    assert len(turn.questions) == 1
    assert turn.questions[0].question == "Which provider?"
    assert turn.questions[0].options == ["openai", "anthropic", "local vllm"]
    assert turn.note == "plan: one agent"
    assert turn.plan is None
    assert turn.proposal is None
    assert turn.telemetry.status == "questions"
    with pytest.raises(ProposalNotFoundError):
        service.repository.state("turn-proposal-1")


async def test_first_turn_blueprint_is_retried_into_questions() -> None:
    candidate = _blueprint({"metadata": {"title": "Too Eager"}})
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=candidate)),
            TextTurn(_turn_json(questions=[_q("What should trigger it?")])),
        ]
    )
    service = _service(provider)

    turn = await service.converse(_blueprint(), [_user("automate my reviews")])

    assert turn.kind == "questions"
    assert len(provider.requests) == 2
    assert "clarifying questions" in _request_text(provider.requests[1])


async def test_followup_chat_turn_presents_a_plan_instead_of_building() -> None:
    provider = RecordingScriptedProvider(
        [TextTurn(_turn_json(plan="1. one agent\n2. nightly cron routine", note="ready to build?"))]
    )
    service = _service(provider)

    turn = await service.converse(
        _blueprint(),
        [
            _user("build me a review workflow"),
            _assistant('{"questions": [{"question": "Which provider?"}]}'),
            _user("Which provider? → openai"),
        ],
    )

    assert turn.kind == "plan"
    assert turn.plan == "1. one agent\n2. nightly cron routine"
    assert turn.note == "ready to build?"
    assert turn.proposal is None
    assert turn.telemetry.status == "plan"
    transcript = _request_text(provider.requests[0])
    assert "Which provider? → openai" in transcript
    assert '"stage":"chat"' in transcript


async def test_chat_stage_blueprint_is_retried_until_plan_approval() -> None:
    candidate = _blueprint({"metadata": {"title": "Unapproved"}})
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=candidate)),
            TextTurn(_turn_json(plan="1. one agent")),
        ]
    )
    service = _service(provider)

    turn = await service.converse(
        _blueprint(),
        [_user("go"), _assistant("plan"), _user("looks close")],
    )

    assert turn.kind == "plan"
    assert len(provider.requests) == 2
    assert "approve" in _request_text(provider.requests[1])


async def test_build_stage_returns_the_proposal_with_summary() -> None:
    candidate = _blueprint({"metadata": {"title": "Agreed Design"}})
    provider = RecordingScriptedProvider(
        [TextTurn(_turn_json(blueprint=candidate, summary="one agent, cron routine"))]
    )
    service = _service(provider)

    turn = await service.converse(
        _blueprint(),
        [
            _user("build me a review workflow"),
            _assistant("plan: one agent + cron"),
            _user("Proceed with this plan."),
        ],
        stage="build",
    )

    assert turn.kind == "proposal"
    assert turn.proposal is not None
    assert turn.proposal.candidate == candidate
    assert turn.proposal.summary == "one agent, cron routine"
    assert turn.proposal.telemetry.status == "success"
    assert '"stage":"build"' in _request_text(provider.requests[0])


async def test_build_stage_retries_non_blueprint_replies() -> None:
    candidate = _blueprint({"metadata": {"title": "Finally Built"}})
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(plan="still planning")),
            TextTurn(_turn_json(blueprint=candidate)),
        ]
    )
    service = _service(provider)

    turn = await service.converse(
        _blueprint(),
        [_user("go"), _assistant("plan"), _user("Proceed with this plan.")],
        stage="build",
    )

    assert turn.kind == "proposal"
    assert len(provider.requests) == 2
    assert "approved" in _request_text(provider.requests[1])


def test_conversation_prompt_surfaces_the_drafts_outstanding_errors() -> None:
    # A freshly created project is a draft with no provider selected; the agent
    # has to learn that from the request or it will ship a candidate that fails
    # the same semantic validation.
    draft = default_blueprint("demo")
    prompt = build_conversation_prompt(draft, [_user("build a review pipeline")])
    assert '"currentDiagnostics":' in prompt
    assert "semantic.provider_required" in prompt
    assert "A provider must be selected" in prompt

    settled = build_conversation_prompt(_blueprint(), [_user("tweak the title")])
    assert '"currentDiagnostics":[]' in settled


async def test_manual_only_retry_feedback_carries_the_guard_explanation() -> None:
    trusted = _blueprint({"spec": {"capabilities": {"permissions": {"mode": "trusted"}}}})
    fixed = _blueprint({"metadata": {"title": "Kept Permissions"}})
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=trusted)),
            TextTurn(_turn_json(blueprint=fixed)),
        ]
    )
    service = _service(provider)

    turn = await service.converse(
        _blueprint(),
        [_user("go"), _assistant("plan"), _user("Proceed with this plan.")],
        stage="build",
    )

    assert turn.kind == "proposal"
    assert len(provider.requests) == 2
    retry_text = _request_text(provider.requests[1])
    assert "authoring.manual_only_field" in retry_text
    # The guard's explanation reaches the model, not just a bare code and path —
    # without it a provider keeps re-proposing the same forbidden change.
    assert "cannot enable, disable, or otherwise change trusted mode" in retry_text


async def test_exactly_one_of_questions_plan_or_blueprint_is_enforced() -> None:
    candidate = _blueprint({"metadata": {"title": "Both Set"}})
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=candidate, questions=[_q("and this?")])),
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
        ]
    )
    service = _service(provider)

    turn = await service.converse(_blueprint(), [_user("do something")])

    assert turn.kind == "questions"
    assert len(provider.requests) == 2
    assert "exactly one" in _request_text(provider.requests[1])


async def test_transcript_shape_and_caps_are_enforced() -> None:
    provider = RecordingScriptedProvider([TextTurn(_turn_json(questions=[_q("?")]))])
    service = _service(provider)
    current = _blueprint()

    with pytest.raises(InvalidInstructionError):
        await service.converse(current, [])
    with pytest.raises(InvalidInstructionError):
        await service.converse(current, [_user("hello"), _assistant("hi")])
    with pytest.raises(InvalidInstructionError):
        await service.converse(current, [_user("x")] * 25)
    with pytest.raises(InvalidInstructionError):
        await service.converse(current, [_user("y" * 32_000)] * 5)
    assert provider.requests == []


async def test_agent_gets_knowledge_tools_toc_prompt_and_turn_ceiling(tmp_path: Path) -> None:
    provider = RecordingScriptedProvider([TextTurn(_turn_json(questions=[_q("?")]))])
    build_requests: list[AgentBuildRequest] = []

    def agent_factory(request: AgentBuildRequest) -> Any:
        build_requests.append(request)
        return create_authoring_agent(request)

    service = _service(provider, knowledge=_tmp_knowledge(tmp_path), agent_factory=agent_factory)
    await service.converse(_blueprint(), [_user("plan a workflow")])

    request = build_requests[0]
    assert "usage/tools.md" in request.system_prompt
    assert "clarifying questions" in request.system_prompt
    assert request.deps is not None
    agent = create_authoring_agent(request)
    try:
        assert agent.max_turns == 12
        assert sorted(tool.name for tool in agent.tools.list()) == [
            "read_section",
            "search_docs",
        ]
        assert agent.deps is request.deps
    finally:
        await agent.close()


async def test_compile_dry_run_failures_retry_then_surface_as_semantic_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from linch_studio.authoring import service as service_module

    candidate = _blueprint({"metadata": {"title": "Compiles Badly"}})
    failure = Diagnostic(
        severity="error",
        code="compiler.pack_failure",
        message="a pack refused this design",
        path="/spec",
        remediation="simplify the design",
    )
    monkeypatch.setattr(service_module, "_compile_diagnostics", lambda _: (failure,))
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=candidate)),
            TextTurn(_turn_json(blueprint=candidate)),
        ]
    )
    service = _service(provider)

    with pytest.raises(SemanticProposalError) as failure_info:
        await service.converse(
            _blueprint(),
            [_user("go"), _assistant("plan"), _user("Proceed with this plan.")],
            stage="build",
        )
    assert any(item.code == "compiler.pack_failure" for item in failure_info.value.diagnostics)
    # The loop runner honors exactly one verifier-induced re-entry per run
    # (its anti-bounce guard), so the second compile failure surfaces here.
    assert len(provider.requests) == 2


class ExplodingProvider(ScriptedProvider):
    """Raises before any provider event, simulating an unexpected SDK/provider crash."""

    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        del request
        raise RuntimeError("simulated provider crash")
        yield {}  # pragma: no cover - unreachable; keeps this an async generator


async def test_unexpected_crash_reports_a_safe_cause_not_just_a_dash() -> None:
    """A provider crash must not vanish into an unlabeled `diagnostics=[-]` log line.

    Live regression: a hard multi-subagent build turn failed with
    `authoring.generation_failed status=runtime_error` and the value-safe log
    line carried no lead at all on what broke. The SDK turns an unhandled
    provider exception into a `ResultEvent(subtype="error")`, never lets it
    escape raw — so the cause has to come from `subtype`/`stop_reason`, a
    closed, static vocabulary (never free-form provider text), not from
    catching the original exception ourselves.
    """
    service = _service(ExplodingProvider([]))

    with pytest.raises(ProposalGenerationError) as failure_info:
        await service.converse(_blueprint(), [_user("go")], stage="chat")
    assert failure_info.value.cause == "error/error"


async def test_legacy_propose_translates_question_turns_into_typed_failure() -> None:
    provider = RecordingScriptedProvider(
        [
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
        ]
    )
    service = _service(provider)

    with pytest.raises(ProposalGenerationError):
        await service.propose(_blueprint(), "make it better")


async def test_thinking_across_retries_is_captured_uncapped() -> None:
    candidate = _blueprint({"metadata": {"title": "Too Eager"}})
    long_thought = "reconsidering the trigger design " * 2_000
    provider = ThinkingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=candidate)),
            TextTurn(_turn_json(questions=[_q("What should trigger it?")])),
        ],
        thoughts=["I could propose immediately.", long_thought],
    )
    service = _service(provider)

    turn = await service.converse(_blueprint(), [_user("automate my reviews")])

    assert turn.kind == "questions"
    # The whole trace survives: nothing is truncated.
    assert turn.thinking == "I could propose immediately.\n\n" + long_thought


async def test_thinking_is_absent_without_thinking_output() -> None:
    plain = RecordingScriptedProvider([TextTurn(_turn_json(questions=[_q("?")]))])
    quiet = await _service(plain).converse(_blueprint(), [_user("hi")])
    assert quiet.thinking is None


async def test_thinking_streams_through_the_callback_as_it_arrives() -> None:
    candidate = _blueprint({"metadata": {"title": "Too Eager"}})
    provider = ThinkingScriptedProvider(
        [
            TextTurn(_turn_json(blueprint=candidate)),
            TextTurn(_turn_json(questions=[_q("What should trigger it?")])),
        ],
        thoughts=["first attempt reasoning", "second attempt reasoning"],
    )
    service = _service(provider)
    live: list[str] = []

    turn = await service.converse(
        _blueprint(),
        [_user("automate my reviews")],
        on_thinking=live.append,
    )

    assert turn.kind == "questions"
    # Each provider chunk reached the callback during the run, in order,
    # across the verifier retry boundary.
    assert live == ["first attempt reasoning", "second attempt reasoning"]
    # The joined display trace is unchanged by streaming.
    assert turn.thinking == "first attempt reasoning\n\nsecond attempt reasoning"


async def test_tool_calls_are_captured_bounded_and_attached_to_the_turn() -> None:
    # A real search_docs round trip against the knowledge tool (not mocked),
    # so the captured record reflects genuine ToolCallStartEvent/ToolCallEndEvent
    # data the chat UI renders as one compact, expandable line.
    provider = RecordingScriptedProvider(
        [
            ToolUseTurn(tool_name="search_docs", tool_input={"query": "workflow"}),
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
        ]
    )
    service = _service(provider)

    turn = await service.converse(_blueprint(), [_user("build me a review workflow")])

    assert turn.kind == "questions"
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.tool_name == "search_docs"
    assert call.summary == "search_docs: workflow"
    assert call.is_error is False
    assert call.duration_ms >= 0
    assert call.result_summary is not None and "result" in call.result_summary
    assert call.detail  # full bounded content, shown only on explicit expand


async def test_tool_calls_stream_through_the_callback_as_start_then_end() -> None:
    provider = RecordingScriptedProvider(
        [
            ToolUseTurn(tool_name="search_docs", tool_input={"query": "workflow"}),
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
        ]
    )
    service = _service(provider)
    phases: list[str] = []

    turn = await service.converse(
        _blueprint(),
        [_user("build me a review workflow")],
        on_tool_call=lambda update: phases.append(update.phase),
    )

    assert turn.kind == "questions"
    assert phases == ["start", "end"]


async def test_unknown_anchor_tool_call_is_captured_as_a_bounded_error() -> None:
    provider = RecordingScriptedProvider(
        [
            ToolUseTurn(tool_name="read_section", tool_input={"anchor": "usage/nope.md#gone"}),
            TextTurn(_turn_json(questions=[_q("Which provider?")])),
        ]
    )
    service = _service(provider)

    turn = await service.converse(_blueprint(), [_user("build me a review workflow")])

    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.tool_name == "read_section"
    assert call.is_error is True
    assert call.result_summary == "unknown anchor"
