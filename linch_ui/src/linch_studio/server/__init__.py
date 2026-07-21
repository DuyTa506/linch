"""Local Studio server primitives.

The application factory is imported lazily so file-store users do not require
FastAPI until they actually construct the web application.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .authoring import (
    AuthoringService,
    LinchAuthoringService,
    ProposalDraft,
    TurnDraft,
    UnavailableAuthoringService,
    authoring_service_from_env,
)
from .store import FileProjectStore, ProjectSnapshot, StoredProposal
from .support import SupportService, UnavailableSupportService, support_service_from_env

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(
    workspace: str | Path,
    *,
    static_dir: str | Path | None = None,
    authoring_service: AuthoringService | None = None,
    support_service: SupportService | None = None,
) -> FastAPI:
    from .app import create_app as factory

    return factory(
        workspace,
        static_dir=static_dir,
        authoring_service=authoring_service,
        support_service=support_service,
    )


__all__ = [
    "AuthoringService",
    "FileProjectStore",
    "LinchAuthoringService",
    "ProjectSnapshot",
    "ProposalDraft",
    "StoredProposal",
    "SupportService",
    "TurnDraft",
    "UnavailableAuthoringService",
    "UnavailableSupportService",
    "authoring_service_from_env",
    "support_service_from_env",
    "create_app",
]
