"""Value-safe server and project-store errors."""

from __future__ import annotations

from collections.abc import Sequence

from linch_studio.spec import Diagnostic


class StudioServerError(Exception):
    """Base error rendered by the API without rejected values or filesystem details."""

    status_code = 500
    code = "server.error"
    message = "The Studio request could not be completed."

    def __init__(
        self,
        *,
        diagnostics: Sequence[Diagnostic] = (),
        current_digest: str | None = None,
    ) -> None:
        super().__init__(self.message)
        self.diagnostics = tuple(diagnostics)
        self.current_digest = current_digest


class InvalidProjectId(StudioServerError):
    status_code = 422
    code = "project.invalid_id"
    message = "The project ID does not use the supported identifier format."


class ProjectNotFound(StudioServerError):
    status_code = 404
    code = "project.not_found"
    message = "The requested Studio project does not exist."


class ProjectAlreadyExists(StudioServerError):
    status_code = 409
    code = "project.already_exists"
    message = "A Studio project with this ID already exists."


class UnsafeProjectPath(StudioServerError):
    status_code = 409
    code = "project.unsafe_path"
    message = "The project path is not a safe directory inside the workspace."


class CorruptProject(StudioServerError):
    status_code = 500
    code = "project.corrupt"
    message = "Stored Studio project state is unreadable or structurally invalid."


class StructuralBlueprintRejected(StudioServerError):
    status_code = 422
    code = "blueprint.structural_invalid"
    message = "The blueprint is structurally invalid and was not saved."


class StaleDigest(StudioServerError):
    status_code = 409
    code = "blueprint.stale_digest"
    message = "The blueprint changed after the supplied base digest."


class ExportBlocked(StudioServerError):
    status_code = 422
    code = "export.validation_blocked"
    message = "Export is blocked until blueprint diagnostics are resolved."


class ExportConflict(StudioServerError):
    status_code = 409
    code = "export.target_unavailable"
    message = "The export target must be nonexistent or an empty safe directory."


class InvalidLayout(StudioServerError):
    status_code = 422
    code = "layout.invalid"
    message = "The layout document is invalid and was not saved."


class InvalidRequest(StudioServerError):
    status_code = 422
    code = "api.request_invalid"
    message = "The request does not match the Studio API contract."


class ProposalNotFound(StudioServerError):
    status_code = 404
    code = "proposal.not_found"
    message = "The requested proposal does not exist."


class AuthoringUnavailable(StudioServerError):
    status_code = 501
    code = "authoring.unavailable"
    message = "AI authoring is not configured for this Studio server."


class AuthoringFailed(StudioServerError):
    status_code = 502
    code = "authoring.failed"
    message = "The authoring provider did not return a valid proposal."


class SupportUnavailable(StudioServerError):
    status_code = 501
    code = "support.unavailable"
    message = "AI support is not configured for this Studio server."


class SupportFailed(StudioServerError):
    status_code = 502
    code = "support.failed"
    message = "The support provider did not return a grounded response."


__all__ = [
    "AuthoringFailed",
    "AuthoringUnavailable",
    "CorruptProject",
    "ExportBlocked",
    "ExportConflict",
    "InvalidLayout",
    "InvalidProjectId",
    "InvalidRequest",
    "ProjectAlreadyExists",
    "ProjectNotFound",
    "ProposalNotFound",
    "StaleDigest",
    "StructuralBlueprintRejected",
    "StudioServerError",
    "SupportFailed",
    "SupportUnavailable",
    "UnsafeProjectPath",
]
