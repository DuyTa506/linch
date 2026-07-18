"""Canonical semantic serialization and blueprint digests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Blueprint


def canonical_data(blueprint: Blueprint) -> dict[str, Any]:
    """Return the complete normalized semantic document using wire aliases."""

    return blueprint.model_dump(mode="json", by_alias=True, exclude_none=False)


def canonical_json(blueprint: Blueprint) -> str:
    """Serialize a blueprint as stable, compact, sorted JSON.

    Defaults are included so omitted defaults and explicitly written defaults
    share a digest. ``allow_nan=False`` makes the invariant explicit even for a
    model constructed outside the hardened YAML loader.
    """

    return json.dumps(
        canonical_data(blueprint),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_bytes(blueprint: Blueprint) -> bytes:
    return canonical_json(blueprint).encode("utf-8")


def canonical_digest(blueprint: Blueprint) -> str:
    """SHA-256 digest of semantic blueprint data; layout is not in the model."""

    return hashlib.sha256(canonical_bytes(blueprint)).hexdigest()


# Descriptive aliases for API callers.
blueprint_digest = canonical_digest
canonical_blueprint_json = canonical_json


__all__ = [
    "blueprint_digest",
    "canonical_blueprint_json",
    "canonical_bytes",
    "canonical_data",
    "canonical_digest",
    "canonical_json",
]
