"""Grounded documentation/implementation support turns stay non-mutating and bounded."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from linch import ScriptedProvider, TextTurn, ToolUseTurn

from linch_studio.authoring.config import AuthoringConfig
from linch_studio.authoring.errors import MalformedProposalError
from linch_studio.support import SupportMessage
from linch_studio.support.service import (
    _MAX_RETRIEVAL_TURNS,
    SUPPORT_SYSTEM_PROMPT,
    LinchSupportService,
    _support_output_schema_instruction,
    _SupportRetrievalLimit,
    create_support_agent,
)


def _config() -> AuthoringConfig:
    return AuthoringConfig(provider="openai_responses", model="gpt-5", api_key="offline-test-key")


def _anthropic_config() -> AuthoringConfig:
    return AuthoringConfig(provider="anthropic", model="claude-test", api_key="offline-test-key")


def _answer() -> str:
    return json.dumps(
        {
            "kind": "answer",
            "answer": "Use `run_workflow()` for a deterministic directed workflow.",
            "recipe": None,
            "evidence": [
                {
                    "anchor": "usage/workflows.md#run_workflow-fn",
                    "claim": "The workflow API exposes run_workflow.",
                    "excerpt": None,
                }
            ],
            "coverage": "documented",
            "follow_up": None,
        }
    )


def _recipe(*, invalid_python: bool = False, omit_tests: bool = False) -> str:
    code = "from linch import Agent\n\nagent = Agent\n" if not invalid_python else "def broken(:\n"
    return json.dumps(
        {
            "kind": "recipe",
            "answer": None,
            "recipe": {
                "title": "Scheduled team review",
                "overview": (
                    "Host cron invokes one bounded workflow; it is not a deployed scheduler."
                ),
                "intent": {
                    "summary": "Run a bounded scheduled review.",
                    "capabilities": ["workflow", "subagent"],
                    "schedule": "Host-owned cron",
                    "workflow_shape": "directed workflow",
                    "constraints": ["Do not deploy a scheduler from Studio."],
                },
                "files": [
                    {
                        "path": "src/team_review/agent.py",
                        "language": "python",
                        "content": code,
                        "provenance": "composed",
                        "evidence": ["usage/loop-runner.md#cron-ci-and-webhooks"],
                        "explanation": "A static composition of documented primitives.",
                    }
                ],
                "tests": (
                    []
                    if omit_tests
                    else [
                        {
                            "path": "tests/test_agent.py",
                            "language": "python",
                            "content": "def test_offline() -> None:\n    assert True\n",
                            "provenance": "copied",
                            "evidence": [
                                "examples/recipes/loop_runner.py.md#"
                                "example-examples-recipes-loop_runner-py"
                            ],
                            "explanation": None,
                        }
                    ]
                ),
                "handoff": {
                    "start_here": ["Read the generated agent module."],
                    "environment": ["LINCH_MODEL"],
                    "commands": [{"command": "pytest", "purpose": "Run offline tests."}],
                    "todos": [
                        {
                            "description": "Connect the host cron scheduler.",
                            "blocking": True,
                            "owner": "host",
                        }
                    ],
                },
            },
            "evidence": [
                {
                    "anchor": "usage/loop-runner.md#cron-ci-and-webhooks",
                    "claim": "Cron and CI are host-owned invocations.",
                    "excerpt": None,
                },
                {
                    "anchor": (
                        "examples/recipes/loop_runner.py.md#example-examples-recipes-loop_runner-py"
                    ),
                    "claim": "The example supplies an audited loop motif.",
                    "excerpt": None,
                },
            ],
            "coverage": "partial",
            "follow_up": "Review the host scheduler integration before deployment.",
        }
    )


class ThinkingThenTextProvider(ScriptedProvider):
    """Exercises partial-event filtering without changing the final scripted output."""

    async def stream(self, req):
        async for event in super().stream(req):
            if event["type"] == "text_delta":
                yield {"type": "thinking_delta", "text": "private provider reasoning"}
            yield event


def test_support_prompt_caps_iterative_retrieval_before_a_final_answer() -> None:
    assert "at most four documentation-tool calls" in SUPPORT_SYSTEM_PROMPT
    assert "stop retrieving and return the final JSON" in SUPPORT_SYSTEM_PROMPT


def test_support_schema_instruction_gives_json_object_providers_the_real_shape() -> None:
    from linch import OutputSchema

    instruction = _support_output_schema_instruction(
        OutputSchema(
            name="support_turn",
            schema={"type": "object", "required": ["kind"], "properties": {"kind": {}}},
        )
    )

    assert "Authoritative final JSON Schema" in instruction
    assert '"required":["kind"]' in instruction


async def test_support_retrieval_limit_removes_corpus_tools_for_the_final_turn() -> None:
    limiter = _SupportRetrievalLimit()

    before = await limiter.build(SimpleNamespace(turn_index=_MAX_RETRIEVAL_TURNS - 1))
    final = await limiter.build(SimpleNamespace(turn_index=_MAX_RETRIEVAL_TURNS))

    assert before.selected_tools is None
    assert final.selected_tools == []
    assert final.metadata == {"support_retrieval": "final_output"}


async def test_documentation_turn_returns_valid_evidence_without_project_mutation() -> None:
    provider = ScriptedProvider([TextTurn(_answer())])
    service = LinchSupportService(_config(), provider_factory=lambda _: provider)

    turn = await service.turn(
        messages=[SupportMessage(role="user", content="How does a directed workflow run?")],
        mode="documentation",
    )

    assert turn.kind == "answer"
    assert turn.answer is not None
    assert turn.coverage == "documented"
    assert [item.anchor for item in turn.evidence] == ["usage/workflows.md#run_workflow-fn"]


async def test_support_forwards_only_text_partials_before_its_validated_final_answer() -> None:
    provider = ThinkingThenTextProvider([TextTurn(_answer())])
    created_agents = []

    def agent_factory(request):
        agent = create_support_agent(request)
        created_agents.append(agent)
        return agent

    service = LinchSupportService(
        _config(),
        provider_factory=lambda _: provider,
        agent_factory=agent_factory,
    )
    response_deltas: list[str] = []

    turn = await service.turn(
        messages=[SupportMessage(role="user", content="How does a directed workflow run?")],
        mode="documentation",
        on_response_delta=response_deltas.append,
    )

    assert created_agents[0].include_partial_messages is True
    assert response_deltas == [_answer()]
    assert turn.answer == "Use `run_workflow()` for a deterministic directed workflow."


async def test_implementation_recipe_is_static_checked_and_carries_handoff() -> None:
    provider = ScriptedProvider([TextTurn(_recipe())])
    service = LinchSupportService(_config(), provider_factory=lambda _: provider)

    turn = await service.turn(
        messages=[
            SupportMessage(
                role="user",
                content="Show a schedule + loop + multi-agent implementation.",
            )
        ],
        mode="implementation",
    )

    assert turn.kind == "recipe"
    assert turn.recipe is not None
    assert turn.recipe.files[0].path == "src/team_review/agent.py"
    assert turn.recipe.handoff.todos[0].owner == "host"


async def test_anthropic_support_finishes_as_validated_json_after_bounded_retrieval() -> None:
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="search_docs", tool_input={"query": "workflow"}, tool_id="t1"),
            ToolUseTurn(tool_name="search_docs", tool_input={"query": "cron"}, tool_id="t2"),
            ToolUseTurn(tool_name="search_docs", tool_input={"query": "routine"}, tool_id="t3"),
            TextTurn(_recipe()),
        ]
    )
    service = LinchSupportService(_anthropic_config(), provider_factory=lambda _: provider)

    turn = await service.turn(
        messages=[SupportMessage(role="user", content="Show a scheduled workflow implementation.")],
        mode="implementation",
    )

    assert turn.kind == "recipe"
    assert turn.recipe is not None
    assert turn.recipe.files[0].path == "src/team_review/agent.py"


async def test_recipe_with_invalid_python_is_retried_then_fails_safely() -> None:
    provider = ScriptedProvider(
        [TextTurn(_recipe(invalid_python=True)), TextTurn(_recipe(invalid_python=True))]
    )
    service = LinchSupportService(_config(), provider_factory=lambda _: provider)

    with pytest.raises(MalformedProposalError):
        await service.turn(
            messages=[
                SupportMessage(
                    role="user",
                    content="Show a schedule + loop + multi-agent implementation.",
                )
            ],
            mode="implementation",
        )


async def test_recipe_without_an_offline_test_gets_an_honest_smoke_test_skeleton() -> None:
    provider = ScriptedProvider([TextTurn(_recipe(omit_tests=True))])
    service = LinchSupportService(_config(), provider_factory=lambda _: provider)

    turn = await service.turn(
        messages=[SupportMessage(role="user", content="Show a workflow implementation.")],
        mode="implementation",
    )

    assert turn.recipe is not None
    assert turn.recipe.tests[0].provenance == "skeleton"
    assert "test_recipe_files_exist" in turn.recipe.tests[0].content
