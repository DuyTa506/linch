"""Shared private wire-format versioning for persisted Linch state."""

from __future__ import annotations

WIRE_VERSION = 3


class UnsupportedWireVersionError(ValueError):
    """Persisted state belongs to a wire generation this runtime cannot read."""


def require_wire_version(value: object) -> None:
    """Require the exact current wire version, rejecting bools and omissions."""

    if type(value) is not int or value != WIRE_VERSION:
        raise UnsupportedWireVersionError("state was written by Linch < 3.0; start a new run")
