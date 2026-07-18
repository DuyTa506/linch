from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from linch_studio.authoring import (
    AuthoringConfig,
    AuthoringConfigurationError,
    InMemoryProposalRepository,
    Proposal,
    ProposalStateError,
    ProposalTelemetry,
    StaleProposalError,
    deterministic_layout_for_new_nodes,
    manual_only_diagnostics,
    semantic_diff,
    workflow_node_key,
)
from linch_studio.spec import Blueprint, canonical_digest, default_blueprint


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


def _telemetry() -> ProposalTelemetry:
    return ProposalTelemetry(
        provider="openai_responses",
        model="gpt-5",
        duration_ms=10,
        input_tokens=3,
        output_tokens=5,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        status="success",
    )


def test_authoring_config_reads_only_prefixed_values_and_hides_key_from_repr() -> None:
    config = AuthoringConfig.from_env(
        {
            "LINCH_STUDIO_PROVIDER": "openai-responses",
            "LINCH_STUDIO_MODEL": "gpt-5",
            "LINCH_STUDIO_API_KEY": "do-not-log",
            "LINCH_STUDIO_MAX_OUTPUT_TOKENS": "4096",
            "LINCH_STUDIO_TOKEN_BUDGET": "12000",
            "LINCH_STUDIO_TIMEOUT_SECONDS": "30",
            "OPENAI_API_KEY": "must-not-be-consulted",
        }
    )

    assert config.provider == "openai_responses"
    assert config.max_output_tokens == 4096
    assert config.token_budget == 12_000
    assert "do-not-log" not in repr(config)
    assert "must-not-be-consulted" not in repr(config)


def test_authoring_config_rejects_implicit_cloud_keys_and_local_urls() -> None:
    with pytest.raises(AuthoringConfigurationError):
        AuthoringConfig.from_env(
            {
                "LINCH_STUDIO_PROVIDER": "anthropic",
                "LINCH_STUDIO_MODEL": "claude-test",
            }
        )
    with pytest.raises(AuthoringConfigurationError):
        AuthoringConfig.from_env(
            {
                "LINCH_STUDIO_PROVIDER": "vllm",
                "LINCH_STUDIO_MODEL": "local-test",
            }
        )


def test_reasoning_defaults_on_and_rejects_unknown_levels() -> None:
    base = {
        "LINCH_STUDIO_PROVIDER": "openai-responses",
        "LINCH_STUDIO_MODEL": "gpt-5",
        "LINCH_STUDIO_API_KEY": "offline-test-key",
    }
    assert AuthoringConfig.from_env(base).reasoning == "medium"
    assert (
        AuthoringConfig.from_env({**base, "LINCH_STUDIO_REASONING": " HIGH "}).reasoning == "high"
    )
    assert AuthoringConfig.from_env({**base, "LINCH_STUDIO_REASONING": "off"}).reasoning == "off"
    with pytest.raises(AuthoringConfigurationError):
        AuthoringConfig.from_env({**base, "LINCH_STUDIO_REASONING": "max"})


def test_reasoning_levels_map_to_native_provider_options() -> None:
    from linch_studio.authoring.providers import (
        _anthropic_thinking,
        _deepseek_options,
        _openai_reasoning,
    )

    def openai(level: str) -> Any:
        return AuthoringConfig(
            provider="openai_responses",
            model="gpt-5",
            api_key="offline-test-key",
            reasoning=level,
        )

    def anthropic(level: str) -> Any:
        return AuthoringConfig(
            provider="anthropic",
            model="claude-test",
            api_key="offline-test-key",
            reasoning=level,
        )

    def deepseek(level: str) -> Any:
        return AuthoringConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="offline-test-key",
            base_url="https://api.deepseek.com",
            reasoning=level,
        )

    high = _openai_reasoning(openai("high"))
    assert high is not None
    assert high.effort == "high"
    assert high.summary == "auto"
    assert _openai_reasoning(openai("off")) is None

    assert _anthropic_thinking(anthropic("low")) == {"type": "adaptive"}
    assert _anthropic_thinking(anthropic("medium")) == {"type": "adaptive"}
    assert _anthropic_thinking(anthropic("high")) == {"type": "adaptive"}
    assert _anthropic_thinking(anthropic("off")) == {"type": "disabled"}

    assert _deepseek_options(deepseek("off")).thinking == "disabled"
    assert _deepseek_options(deepseek("off")).effort is None
    assert _deepseek_options(deepseek("medium")).thinking == "enabled"
    assert _deepseek_options(deepseek("medium")).effort == "medium"


def test_deepseek_config_requires_native_base_url_and_factory_uses_native_provider() -> None:
    from linch import DeepSeekProvider

    from linch_studio.authoring.providers import create_authoring_provider

    with pytest.raises(AuthoringConfigurationError):
        AuthoringConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="offline-test-key",
        )
    with pytest.raises(AuthoringConfigurationError, match="not /anthropic"):
        AuthoringConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="offline-test-key",
            base_url="https://api.deepseek.com/anthropic",
        )
    with pytest.raises(AuthoringConfigurationError, match="compatibility mode"):
        AuthoringConfig(
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="offline-test-key",
            base_url="https://api.deepseek.com/anthropic",
        )

    config = AuthoringConfig(
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="offline-test-key",
        base_url="https://api.deepseek.com",
    )
    assert isinstance(create_authoring_provider(config), DeepSeekProvider)


def test_json_mode_flag_parses_and_reaches_the_chat_provider_options() -> None:
    from linch_studio.authoring.providers import _chat_options

    base = {
        "LINCH_STUDIO_PROVIDER": "openai-chat",
        "LINCH_STUDIO_MODEL": "deepseek-v4-flash",
        "LINCH_STUDIO_API_KEY": "offline-test-key",
        "LINCH_STUDIO_BASE_URL": "https://api.deepseek.example",
    }
    assert AuthoringConfig.from_env(base).json_mode is False
    enabled = AuthoringConfig.from_env({**base, "LINCH_STUDIO_JSON_MODE": " True "})
    assert enabled.json_mode is True
    with pytest.raises(AuthoringConfigurationError):
        AuthoringConfig.from_env({**base, "LINCH_STUDIO_JSON_MODE": "sometimes"})

    # JSON-object mode for schema-less providers (e.g. DeepSeek); the loop's
    # text-parse + validation gates still enforce the strict turn schema.
    assert _chat_options(enabled).json_mode is True
    assert _chat_options(AuthoringConfig.from_env(base)).json_mode is False


def test_semantic_diff_uses_normalized_json_paths() -> None:
    current = _blueprint()
    candidate = _blueprint(
        {
            "metadata": {"title": "Reviewed Demo"},
            "spec": {
                "tools": [
                    {
                        "kind": "function",
                        "id": "lookup",
                        "displayName": "Lookup",
                        "description": "Look up one record.",
                    }
                ]
            },
        }
    )

    changes = semantic_diff(current, candidate)

    assert [(item.operation, item.path) for item in changes] == [
        ("replace", "/metadata/title"),
        ("add", "/spec/tools/0"),
    ]


def test_manual_only_guard_covers_redaction_mcp_and_dangerous_permissions() -> None:
    current = _blueprint()
    candidate = _blueprint(
        {
            "spec": {
                "capabilities": {
                    "hooks": {
                        "redactionRules": [{"pattern": "(?i)secret", "replacement": "[MASKED]"}]
                    },
                    "extensions": {
                        "mcpServers": [
                            {
                                "kind": "stdio",
                                "id": "local_docs",
                                "command": "docs-server",
                                "args": ["--stdio"],
                            }
                        ]
                    },
                    "permissions": {
                        "mode": "trusted",
                        "rules": [
                            {
                                "kind": "bash",
                                "patterns": ["git *"],
                                "decision": "allow",
                            }
                        ],
                    },
                }
            }
        }
    )

    diagnostics = manual_only_diagnostics(current, candidate)

    assert {item.path for item in diagnostics} == {
        "/spec/capabilities/extensions/mcpServers/0/args",
        "/spec/capabilities/extensions/mcpServers/0/command",
        "/spec/capabilities/hooks/redactionRules",
        "/spec/capabilities/permissions/mode",
        "/spec/capabilities/permissions/rules",
    }
    assert {item.code for item in diagnostics} == {"authoring.manual_only_field"}


def test_deterministic_layout_preserves_existing_and_places_new_nodes_by_depth() -> None:
    current = _blueprint(
        {
            "spec": {
                "workflows": [
                    {
                        "id": "review",
                        "displayName": "Review",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "collect",
                                "label": "Collect",
                                "prompt": "Collect evidence.",
                            }
                        ],
                        "output": "collect",
                    }
                ]
            }
        }
    )
    candidate_data = current.model_dump(mode="json", by_alias=True)
    candidate_data["spec"]["workflows"][0]["nodes"].append(
        {
            "type": "agent_call",
            "id": "summarize",
            "label": "Summarize",
            "prompt": "Summarize evidence.",
            "dependsOn": ["collect"],
        }
    )
    candidate_data["spec"]["workflows"][0]["output"] = "summarize"
    candidate = Blueprint.model_validate(candidate_data, strict=True)
    existing = {"wf_review_collect": {"x": 12.0, "y": 34.0, "selected": True}}

    first = deterministic_layout_for_new_nodes(current, candidate, existing)
    second = deterministic_layout_for_new_nodes(current, candidate, existing)

    assert first == second
    # Keys must match the canvas's toLayoutId("wf", workflow, node) scheme so
    # the frontend actually reads the placed coordinates.
    assert workflow_node_key("review", "summarize") == "wf_review_summarize"
    assert first["wf_review_collect"] == {"x": 12.0, "y": 34.0, "selected": True}
    assert first["wf_review_summarize"] == {"x": 280.0, "y": 0.0}
    assert existing == {"wf_review_collect": {"x": 12.0, "y": 34.0, "selected": True}}


def test_repository_requires_review_and_rejects_stale_or_repeated_acceptance() -> None:
    current = _blueprint()
    candidate = _blueprint({"metadata": {"title": "Candidate"}})
    proposal = Proposal(
        id="proposal-1",
        base_digest=canonical_digest(current),
        candidate=candidate,
        diagnostics=(),
        diff=semantic_diff(current, candidate),
        telemetry=_telemetry(),
    )
    repository = InMemoryProposalRepository()
    repository.add(proposal)

    assert repository.state(proposal.id) == "pending"
    with pytest.raises(StaleProposalError):
        repository.accept(proposal.id, _blueprint({"metadata": {"title": "Concurrent Edit"}}))
    assert repository.state(proposal.id) == "pending"

    assert repository.accept(proposal.id, current) == candidate
    assert repository.state(proposal.id) == "accepted"
    with pytest.raises(ProposalStateError):
        repository.accept(proposal.id, current)
