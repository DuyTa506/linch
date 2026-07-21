"""Process-local proposal review state."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from linch_studio.spec import Blueprint, canonical_digest

from .errors import ProposalNotFoundError, ProposalStateError, StaleProposalError
from .models import Proposal, ProposalState


@dataclass(slots=True)
class _StoredProposal:
    proposal: Proposal
    state: ProposalState = "pending"


class InMemoryProposalRepository:
    """Keep candidates pending until an explicit accept or reject action."""

    def __init__(self) -> None:
        self._proposals: dict[str, _StoredProposal] = {}
        self._lock = RLock()

    def add(self, proposal: Proposal) -> None:
        with self._lock:
            if proposal.id in self._proposals:
                raise ProposalStateError("A proposal with this ID already exists.")
            self._proposals[proposal.id] = _StoredProposal(proposal)

    def get(self, proposal_id: str) -> Proposal:
        with self._lock:
            return self._require(proposal_id).proposal

    def state(self, proposal_id: str) -> ProposalState:
        with self._lock:
            return self._require(proposal_id).state

    def list(self, *, state: ProposalState | None = None) -> tuple[Proposal, ...]:
        with self._lock:
            return tuple(
                record.proposal
                for record in self._proposals.values()
                if state is None or record.state == state
            )

    def accept(self, proposal_id: str, current: Blueprint) -> Blueprint:
        """Return the whole candidate only when its original base is current."""

        with self._lock:
            record = self._require_pending(proposal_id)
            if canonical_digest(current) != record.proposal.base_digest:
                raise StaleProposalError(
                    "The project changed after this proposal was generated; request a new proposal."
                )
            record.state = "accepted"
            return record.proposal.candidate

    def reject(self, proposal_id: str) -> None:
        with self._lock:
            record = self._require_pending(proposal_id)
            record.state = "rejected"

    def _require(self, proposal_id: str) -> _StoredProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise ProposalNotFoundError("Proposal not found.") from exc

    def _require_pending(self, proposal_id: str) -> _StoredProposal:
        record = self._require(proposal_id)
        if record.state != "pending":
            raise ProposalStateError("Only a pending proposal can be accepted or rejected.")
        return record


__all__ = ["InMemoryProposalRepository"]
