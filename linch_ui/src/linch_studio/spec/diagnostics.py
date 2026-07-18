"""Stable, value-safe diagnostics for blueprint parsing and validation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["error", "warning", "info"]
PathPart = str | int

_SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


def json_path(*parts: PathPart) -> str:
    """Build an RFC 6901-style JSON pointer, using ``/`` for the root.

    Diagnostics intentionally contain paths and fixed messages only. They never
    interpolate rejected values, YAML snippets, or Pydantic's ``input`` field.
    """

    if not parts:
        return "/"
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped)


class Diagnostic(BaseModel):
    """A machine-stable validation finding safe to return through the API."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    severity: Severity
    path: str = Field(min_length=1, pattern=r"^/")
    message: str = Field(min_length=1, max_length=2_048)
    remediation: str = Field(min_length=1, max_length=2_048)


def sort_diagnostics(diagnostics: Iterable[Diagnostic]) -> tuple[Diagnostic, ...]:
    """Return diagnostics in a deterministic UI/CLI order."""

    return tuple(
        sorted(
            diagnostics,
            key=lambda item: (
                _SEVERITY_ORDER[item.severity],
                item.path,
                item.code,
                item.message,
            ),
        )
    )


def has_errors(diagnostics: Sequence[Diagnostic]) -> bool:
    """Whether a diagnostic collection contains an export-blocking error."""

    return any(item.severity == "error" for item in diagnostics)


__all__ = [
    "Diagnostic",
    "PathPart",
    "Severity",
    "has_errors",
    "json_path",
    "sort_diagnostics",
]
