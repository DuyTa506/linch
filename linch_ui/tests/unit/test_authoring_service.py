from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import pytest
from linch import ScriptedProvider, TextTurn, Usage

from linch_studio.authoring import (
    AUTHORING_SYSTEM_PROMPT,
    AgentBuildRequest,
    AuthoringConfig,
    LinchProposalService,
    MalformedProposalError,
    ManualOnlyFieldError,
    ProposalBudgetError,
    ProposalTimeoutError,
    SemanticProposalError,
    build_proposal_prompt,
    create_authoring_agent,
)
from linch_studio.spec import Blueprint, canonical_json, default_blueprint, validate_blueprint


class RecordingScriptedProvider(ScriptedProvider):
    def __init__(self, turns: list[TextTurn]) -> None:
        super().__init__(turns)
        self.requests: list[Any] = []

    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(request)
        async for event in super().stream(request):
            yield event


class SlowScriptedProvider(RecordingScriptedProvider):
    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(0.2)
        async for event in super().stream(request):
            yield event


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
    data["spec"]["runtime"]["provider"] = {
        "kind": "openai_responses",
        "model": "gpt-5",
    }
    if update:
        _merge(data, update)
    return Blueprint.model_validate(data, strict=True)


def _merge(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _request_text(request: Any) -> str:
    return "\n".join(
        block.text
        for message in request.messages
        for block in message.content
        if getattr(block, "text", None) is not None
    )


def _turn_json(candidate: Blueprint) -> str:
    """Wrap a blueprint in the ask/plan/build turn shape the output schema requires."""
    head = json.dumps({"questions": None, "plan": None, "note": None, "summary": None})
    return head[:-1] + ',"blueprint":' + canonical_json(candidate) + "}"


def _invalid_workflow_candidate() -> Blueprint:
    return _blueprint(
        {
            "spec": {
                "workflows": [
                    {
                        "id": "incomplete",
                        "displayName": "Incomplete",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "only_step",
                                "label": "Only Step",
                                "prompt": "Do the work.",
                            }
                        ],
                    }
                ]
            }
        }
    )


async def test_valid_proposal_is_pending_bounded_tool_free_and_aggregate_only() -> None:
    current = _blueprint()
    candidate = _blueprint({"metadata": {"title": "Reviewed Demo"}})
    provider = RecordingScriptedProvider(
        [
            TextTurn(
                _turn_json(candidate),
                usage=Usage(input_tokens=11, output_tokens=13),
            )
        ]
    )
    build_requests: list[AgentBuildRequest] = []

    def agent_factory(request: AgentBuildRequest) -> Any:
        build_requests.append(request)
        return create_authoring_agent(request)

    service = LinchProposalService(
        _config(),
        provider_factory=lambda _: provider,
        agent_factory=agent_factory,
        id_factory=lambda: "proposal-1",
    )

    proposal = await service.propose(current, "Change only the title to Reviewed Demo")

    assert proposal.candidate == candidate
    assert service.repository.state(proposal.id) == "pending"
    assert current.metadata.title == "Demo"
    assert proposal.telemetry.input_tokens == 11
    assert proposal.telemetry.output_tokens == 13
    assert set(proposal.telemetry.model_dump()) == {
        "provider",
        "model",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "status",
    }
    request = build_requests[0]
    agent = agent_factory(request)
    try:
        assert agent.max_turns == 12
        assert agent.structured_output_retries == 2
        assert agent.budget.max_tokens == 200_000
        # The only capabilities are the two read-only knowledge tools.
        assert sorted(tool.name for tool in agent.tools.list()) == [
            "read_section",
            "search_docs",
        ]
        assert not agent.features.skills
        assert not agent.features.subagents
        assert not agent.features.mcp
        assert not agent.features.filesystem
        assert agent.result_offload is None
    finally:
        await agent.close()
    assert len(provider.requests[0].tools) == 2
    assert provider.requests[0].output_schema is not None


async def test_instruction_can_propose_subagents_and_referenced_directed_workflow() -> None:
    current = _blueprint()
    candidate = _blueprint(
        {
            "spec": {
                "subagents": [
                    {
                        "id": "researcher",
                        "displayName": "Researcher",
                        "description": "Collect evidence for the review.",
                        "instructions": "Collect relevant evidence and return concise findings.",
                    },
                    {
                        "id": "reviewer",
                        "displayName": "Reviewer",
                        "description": "Review collected evidence.",
                        "instructions": "Review evidence and produce the final decision.",
                    },
                ],
                "workflows": [
                    {
                        "id": "evidence_review",
                        "displayName": "Evidence Review",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "collect",
                                "label": "Collect",
                                "prompt": "Collect evidence from the trigger input.",
                                "subagent": "researcher",
                            },
                            {
                                "type": "agent_call",
                                "id": "review",
                                "label": "Review",
                                "prompt": "Review the predecessor result.",
                                "dependsOn": ["collect"],
                                "subagent": "reviewer",
                            },
                        ],
                        "output": "review",
                    }
                ],
            }
        }
    )
    assert validate_blueprint(candidate) == ()
    provider = RecordingScriptedProvider([TextTurn(_turn_json(candidate))])
    service = LinchProposalService(
        _config(),
        provider_factory=lambda _: provider,
        id_factory=lambda: "workflow-proposal",
    )

    proposal = await service.propose(
        current,
        "Add researcher and reviewer specialists, then run a deterministic evidence review.",
    )

    workflow = proposal.candidate.spec.workflows[0]
    assert [agent.id for agent in proposal.candidate.spec.subagents] == [
        "researcher",
        "reviewer",
    ]
    assert workflow.nodes[1].depends_on == ["collect"]
    assert workflow.nodes[1].subagent == "reviewer"
    assert workflow.output == "review"
    prompt = _request_text(provider.requests[0])
    assert '"existingIdentifiers"' in prompt
    assert "deterministic evidence review" in prompt


async def test_each_request_is_fresh_and_contains_only_latest_instruction() -> None:
    current = _blueprint()
    first_candidate = _blueprint({"metadata": {"title": "First"}})
    second_candidate = _blueprint({"metadata": {"title": "Second"}})
    providers = [
        RecordingScriptedProvider([TextTurn(_turn_json(first_candidate))]),
        RecordingScriptedProvider([TextTurn(_turn_json(second_candidate))]),
    ]
    ids = iter(["first-proposal", "second-proposal"])
    service = LinchProposalService(
        _config(),
        provider_factory=lambda _: providers.pop(0),
        id_factory=lambda: next(ids),
    )
    # Keep references after pop so the captured prompts remain inspectable.
    first_provider = providers[0]
    second_provider = providers[1]

    await service.propose(current, "FIRST-INSTRUCTION-MARKER")
    await service.propose(current, "SECOND-INSTRUCTION-MARKER")

    first_prompt = _request_text(first_provider.requests[0])
    second_prompt = _request_text(second_provider.requests[0])
    assert "FIRST-INSTRUCTION-MARKER" in first_prompt
    assert "FIRST-INSTRUCTION-MARKER" not in second_prompt
    assert "SECOND-INSTRUCTION-MARKER" in second_prompt
    assert canonical_json(current) in second_prompt
    assert len(second_provider.requests[0].messages) == 1


async def test_structural_output_retries_are_bounded_then_report_malformed() -> None:
    current = _blueprint()
    provider = RecordingScriptedProvider([TextTurn("not-json") for _ in range(3)])
    service = LinchProposalService(_config(), provider_factory=lambda _: provider)

    with pytest.raises(MalformedProposalError) as raised:
        await service.propose(current, "Make any valid change")

    assert len(provider.requests) == 3
    assert raised.value.telemetry is not None
    assert raised.value.telemetry.status == "malformed"


async def test_semantic_verifier_retries_and_accepts_corrected_full_candidate() -> None:
    current = _blueprint()
    invalid = _invalid_workflow_candidate()
    corrected = _blueprint({"metadata": {"title": "Corrected"}})
    provider = RecordingScriptedProvider(
        [TextTurn(_turn_json(invalid)), TextTurn(_turn_json(corrected))]
    )
    service = LinchProposalService(_config(), provider_factory=lambda _: provider)

    proposal = await service.propose(current, "Create a valid proposal")

    assert proposal.candidate.metadata.title == "Corrected"
    assert len(provider.requests) == 2


async def test_semantic_and_manual_only_exhaustion_are_safe_failures() -> None:
    current = _blueprint()
    invalid = _invalid_workflow_candidate()
    semantic_provider = RecordingScriptedProvider([TextTurn(_turn_json(invalid)) for _ in range(3)])
    semantic_service = LinchProposalService(_config(), provider_factory=lambda _: semantic_provider)
    with pytest.raises(SemanticProposalError) as semantic_error:
        await semantic_service.propose(current, "Keep this invalid workflow")
    assert semantic_error.value.telemetry is not None
    assert semantic_error.value.telemetry.status == "semantic_invalid"

    manual = _blueprint(
        {"spec": {"capabilities": {"hooks": {"redactionRules": [{"pattern": "private-value"}]}}}}
    )
    manual_provider = RecordingScriptedProvider([TextTurn(_turn_json(manual)) for _ in range(3)])
    manual_service = LinchProposalService(_config(), provider_factory=lambda _: manual_provider)
    with pytest.raises(ManualOnlyFieldError) as manual_error:
        await manual_service.propose(current, "Add private-value redaction")
    assert manual_error.value.telemetry is not None
    assert manual_error.value.telemetry.status == "manual_only"
    assert {item.code for item in manual_error.value.diagnostics} >= {"authoring.manual_only_field"}


async def test_timeout_and_budget_failures_expose_only_aggregate_telemetry() -> None:
    current = _blueprint()
    slow = SlowScriptedProvider([TextTurn(_turn_json(current))])
    timeout_service = LinchProposalService(
        _config(timeout_seconds=0.01), provider_factory=lambda _: slow
    )
    instruction = "SENSITIVE-INSTRUCTION-MARKER"
    with pytest.raises(ProposalTimeoutError) as timeout_error:
        await timeout_service.propose(current, instruction)
    assert timeout_error.value.telemetry is not None
    serialized = json.dumps(timeout_error.value.telemetry.model_dump())
    assert instruction not in serialized
    assert canonical_json(current) not in serialized

    invalid = _invalid_workflow_candidate()
    budget_provider = RecordingScriptedProvider(
        [
            TextTurn(
                _turn_json(invalid),
                usage=Usage(input_tokens=2),
            )
        ]
    )
    budget_service = LinchProposalService(
        _config(token_budget=1), provider_factory=lambda _: budget_provider
    )
    with pytest.raises(ProposalBudgetError) as budget_error:
        await budget_service.propose(current, "Trigger one semantic retry")
    assert budget_error.value.telemetry is not None
    assert budget_error.value.telemetry.status == "budget_exhausted"
    assert budget_error.value.telemetry.input_tokens == 2


def test_prompt_builder_includes_complete_current_blueprint_and_latest_instruction() -> None:
    current = _blueprint(
        {
            "spec": {
                "tools": [
                    {
                        "kind": "function",
                        "id": "existing_lookup",
                        "displayName": "Lookup",
                        "description": "Lookup data.",
                    }
                ]
            }
        }
    )

    prompt = build_proposal_prompt(current, "Add a unique reviewer")

    assert canonical_json(current) in prompt
    assert '"tools":["existing_lookup"]' in prompt
    assert '"instruction":"Add a unique reviewer"' in prompt
    assert '"routines":[]' in prompt


def test_authoring_prompt_defines_v1alpha2_axes_and_forbids_executable_verifiers() -> None:
    assert "studio.linch.dev/v1alpha2" in AUTHORING_SYSTEM_PROMPT
    assert "Agent loop preset" in AUTHORING_SYSTEM_PROMPT
    assert "Directed workflow" in AUTHORING_SYSTEM_PROMPT
    assert "Completion" in AUTHORING_SYSTEM_PROMPT
    assert "Routine" in AUTHORING_SYSTEM_PROMPT
    assert "custom_todo" in AUTHORING_SYSTEM_PROMPT
    assert "Never\nadd Python" in AUTHORING_SYSTEM_PROMPT
    # The one manual-only rule providers actually trip over needs a concrete
    # do-this instruction, not just a prohibition list.
    assert "permissions and redaction sections" in AUTHORING_SYSTEM_PROMPT
    assert "unchanged" in AUTHORING_SYSTEM_PROMPT
    # The headless-routine catch-22: exec tools cannot be permission-approved by
    # a proposal, and trusted mode is manual-only — the prompt must name the way
    # out instead of letting providers bounce between the two rejections.
    assert "never declare shell or exec tools" in AUTHORING_SYSTEM_PROMPT
    assert "currentDiagnostics" in AUTHORING_SYSTEM_PROMPT
    # Live regression: told to use a "custom_todo seam", DeepSeek invented a
    # `function_skeleton` tool kind (a catalog capability ID, not a union tag)
    # and cycled to schema-repair exhaustion. The prompt must name the only
    # legal tool kinds and keep custom_todo typed as a verifier.
    assert "function, class, or database" in AUTHORING_SYSTEM_PROMPT
    assert "function_skeleton" in AUTHORING_SYSTEM_PROMPT
    assert "custom_todo verifier" in AUTHORING_SYSTEM_PROMPT
    # Live regression: "settle in the chat stage" read as permission to hand
    # back a build-stage blueprint with provider still unset. The prompt must
    # split the duty by stage: chat asks, build defaults and discloses.
    assert "nothing may stay unset" in AUTHORING_SYSTEM_PROMPT
    assert "sensible default" in AUTHORING_SYSTEM_PROMPT
    # Live regression: candidates satisfied the headless rules one at a time
    # (read-only tools but provider unset, then provider set but a write-scope
    # tool back in). The headless requirements must read as one simultaneous
    # checklist, including that unresolved write-scope tools block too.
    assert "must satisfy all of" in AUTHORING_SYSTEM_PROMPT
    assert "scope read" in AUTHORING_SYSTEM_PROMPT
    assert "write-scope" in AUTHORING_SYSTEM_PROMPT
