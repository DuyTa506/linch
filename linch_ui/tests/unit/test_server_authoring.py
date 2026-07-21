from __future__ import annotations

import asyncio

import pytest

from linch_studio.authoring import (
    AuthoringConfigurationError,
    Proposal,
    ProposalTelemetry,
    semantic_diff,
)
from linch_studio.server import (
    LinchAuthoringService,
    UnavailableAuthoringService,
    authoring_service_from_env,
)
from linch_studio.spec import canonical_digest, default_blueprint


def _blueprint(title: str = "Demo"):
    blueprint = default_blueprint("demo")
    provider = blueprint.spec.runtime.provider.model_copy(
        update={"kind": "openai_responses", "model": "gpt-5"}
    )
    runtime = blueprint.spec.runtime.model_copy(update={"provider": provider})
    metadata = blueprint.metadata.model_copy(update={"title": title})
    return blueprint.model_copy(
        update={
            "metadata": metadata,
            "spec": blueprint.spec.model_copy(update={"runtime": runtime}),
        }
    )


class _GeneratedProposalService:
    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal

    async def propose(self, current, instruction: str) -> Proposal:
        assert current == _blueprint()
        assert instruction == "Improve the title."
        return self.proposal


def _telemetry() -> ProposalTelemetry:
    return ProposalTelemetry(
        provider="openai_responses",
        model="gpt-5",
        duration_ms=1,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        status="success",
    )


def test_server_authoring_adapter_forwards_candidate_without_local_persistence() -> None:
    current = _blueprint()
    candidate = _blueprint("Improved")
    proposal = Proposal(
        id="proposal-1",
        base_digest=canonical_digest(current),
        candidate=candidate,
        diff=semantic_diff(current, candidate),
        telemetry=_telemetry(),
    )
    adapter = LinchAuthoringService(_GeneratedProposalService(proposal))

    draft = asyncio.run(
        adapter.propose(
            current=current,
            base_digest=canonical_digest(current),
            instruction="Improve the title.",
        )
    )

    assert draft.candidate == candidate
    assert draft.semantic_diff == (
        {
            "operation": "replace",
            "path": "/metadata/title",
            "before": "Demo",
            "after": "Improved",
        },
    )


def test_authoring_service_from_env_is_optional_but_rejects_partial_configuration() -> None:
    assert isinstance(authoring_service_from_env({}), UnavailableAuthoringService)
    with pytest.raises(AuthoringConfigurationError):
        authoring_service_from_env({"LINCH_STUDIO_PROVIDER": "openai_responses"})
