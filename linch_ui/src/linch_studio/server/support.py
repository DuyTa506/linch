"""Dependency-injection seam for the global documentation support service."""

from __future__ import annotations

from collections.abc import Mapping

from linch_studio.authoring.config import AuthoringConfig
from linch_studio.support.service import (
    LinchSupportService,
    SupportService,
    UnavailableSupportService,
)


def support_service_from_env(environ: Mapping[str, str] | None = None) -> SupportService:
    """Configure Support from the same explicit BYO-model settings as authoring.

    This intentionally does not probe a provider at startup.  A server with no
    model configuration still serves local projects and reports support as
    unavailable instead of failing boot.
    """

    if environ is None:
        import os

        values = os.environ
    else:
        values = environ
    provider = values.get("LINCH_STUDIO_PROVIDER", "").strip()
    model = values.get("LINCH_STUDIO_MODEL", "").strip()
    if not provider and not model:
        return UnavailableSupportService()
    return LinchSupportService(AuthoringConfig.from_env(values))


__all__ = ["SupportService", "UnavailableSupportService", "support_service_from_env"]
