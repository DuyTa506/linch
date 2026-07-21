"""Stable failures for the optional authoring boundary."""

from __future__ import annotations

from collections.abc import Sequence

from linch_studio.spec import Diagnostic

from .models import ProposalTelemetry


class AuthoringError(RuntimeError):
    code = "authoring.error"

    def __init__(
        self,
        message: str,
        *,
        telemetry: ProposalTelemetry | None = None,
        diagnostics: Sequence[Diagnostic] = (),
        cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self.telemetry = telemetry
        self.diagnostics = tuple(diagnostics)
        # The crashing exception's class name only (e.g. "TimeoutError") — never
        # its message, which may embed request/response detail. A safe lead for
        # the aggregate-only failure log, not part of the client-facing wire
        # response.
        self.cause = cause


class AuthoringConfigurationError(AuthoringError):
    code = "authoring.configuration"


class InvalidInstructionError(AuthoringError):
    code = "authoring.invalid_instruction"


class ProposalGenerationError(AuthoringError):
    code = "authoring.generation_failed"


class MalformedProposalError(AuthoringError):
    code = "authoring.malformed_proposal"


class SemanticProposalError(AuthoringError):
    code = "authoring.semantic_invalid"


class ManualOnlyFieldError(AuthoringError):
    code = "authoring.manual_only"


class ProposalTimeoutError(AuthoringError):
    code = "authoring.timeout"


class ProposalBudgetError(AuthoringError):
    code = "authoring.budget_exhausted"


class ProposalNotFoundError(AuthoringError):
    code = "authoring.proposal_not_found"


class ProposalStateError(AuthoringError):
    code = "authoring.proposal_state"


class StaleProposalError(AuthoringError):
    code = "authoring.stale_proposal"


__all__ = [
    "AuthoringConfigurationError",
    "AuthoringError",
    "InvalidInstructionError",
    "MalformedProposalError",
    "ManualOnlyFieldError",
    "ProposalBudgetError",
    "ProposalGenerationError",
    "ProposalNotFoundError",
    "ProposalStateError",
    "ProposalTimeoutError",
    "SemanticProposalError",
    "StaleProposalError",
]
