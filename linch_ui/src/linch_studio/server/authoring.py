"""Dependency-injection seam for optional AI blueprint proposals."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from linch_studio.authoring import (
    AuthoringConfig,
    AuthoringMessage,
    AuthoringQuestion,
    AuthoringStage,
    AuthoringTurn,
    LinchProposalService,
    Proposal,
    ToolCallRecord,
    ToolCallUpdate,
)
from linch_studio.spec import Blueprint

from .errors import AuthoringUnavailable


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """A complete candidate returned by a stateless authoring service."""

    candidate: Blueprint
    semantic_diff: tuple[dict[str, JsonValue], ...] = ()
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class TurnDraft:
    """One conversational authoring turn crossing the server boundary."""

    kind: Literal["questions", "plan", "proposal"]
    questions: tuple[AuthoringQuestion, ...] = ()
    plan: str | None = None
    note: str | None = None
    proposal: ProposalDraft | None = None
    # Display-only reasoning trace; never persisted, never re-sent in transcripts.
    thinking: str | None = None
    # Display-only tool-call trace; never persisted, never re-sent in transcripts.
    tool_calls: tuple[ToolCallRecord, ...] = ()


class AuthoringService(Protocol):
    """Optional service; implementations must not mutate project state directly."""

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft: ...

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: Sequence[AuthoringMessage],
        stage: AuthoringStage = "chat",
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> TurnDraft: ...


class ProposalGenerator(Protocol):
    async def propose(self, current: Blueprint, instruction: str) -> Proposal: ...

    async def converse(
        self,
        current: Blueprint,
        messages: Sequence[AuthoringMessage],
        *,
        stage: AuthoringStage = "chat",
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> AuthoringTurn: ...


class UnavailableAuthoringService:
    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        del current, base_digest, instruction
        raise AuthoringUnavailable

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: Sequence[AuthoringMessage],
        stage: AuthoringStage = "chat",
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_thinking, on_tool_call
        raise AuthoringUnavailable


class LinchAuthoringService:
    """Adapt the stateless Linch authoring service to the server boundary.

    The file project store owns proposal persistence and compare-and-swap
    acceptance.  The underlying service therefore only generates candidates.
    """

    def __init__(self, service: ProposalGenerator) -> None:
        self._service = service

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        proposal = await self._service.propose(current, instruction)
        return self._draft(proposal, base_digest)

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: Sequence[AuthoringMessage],
        stage: AuthoringStage = "chat",
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> TurnDraft:
        turn = await self._service.converse(
            current,
            list(messages),
            stage=stage,
            on_thinking=on_thinking,
            on_tool_call=on_tool_call,
        )
        if turn.kind == "questions":
            return TurnDraft(
                kind="questions",
                questions=turn.questions,
                note=turn.note,
                thinking=turn.thinking,
                tool_calls=turn.tool_calls,
            )
        if turn.kind == "plan":
            return TurnDraft(
                kind="plan",
                plan=turn.plan,
                note=turn.note,
                thinking=turn.thinking,
                tool_calls=turn.tool_calls,
            )
        if turn.proposal is None:
            raise RuntimeError("authoring turn is proposal-kind but carries no proposal")
        return TurnDraft(
            kind="proposal",
            note=turn.note,
            proposal=self._draft(turn.proposal, base_digest),
            thinking=turn.thinking,
            tool_calls=turn.tool_calls,
        )

    def _draft(self, proposal: Proposal, base_digest: str) -> ProposalDraft:
        if proposal.base_digest != base_digest:
            raise RuntimeError("authoring proposal did not match its base blueprint")
        return ProposalDraft(
            candidate=proposal.candidate,
            semantic_diff=tuple(
                entry.model_dump(mode="json", by_alias=True) for entry in proposal.diff
            ),
            summary=proposal.summary,
        )


def authoring_service_from_env(
    environ: Mapping[str, str] | None = None,
) -> AuthoringService:
    """Create optional BYO-model authoring without probing providers at startup."""

    if environ is None:
        import os

        values = os.environ
    else:
        values = environ
    provider = values.get("LINCH_STUDIO_PROVIDER", "").strip()
    model = values.get("LINCH_STUDIO_MODEL", "").strip()
    if not provider and not model:
        return UnavailableAuthoringService()
    config = AuthoringConfig.from_env(values)
    return LinchAuthoringService(LinchProposalService(config, record_proposals=False))


__all__ = [
    "AuthoringService",
    "LinchAuthoringService",
    "ProposalDraft",
    "TurnDraft",
    "UnavailableAuthoringService",
    "authoring_service_from_env",
]
