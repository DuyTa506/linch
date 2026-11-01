from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Identify the tenant, user, and roles used for one permission evaluation.

    Args:
        tenant: Tenant identifier associated with the caller.
        user: User identifier associated with the caller.
        roles: Immutable set of role names granted to the caller.

    Attributes:
        tenant: The non-empty tenant identifier.
        user: The non-empty user identifier.
        roles: The caller's validated, immutable role set.
    """

    tenant: str
    user: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        _validate_identity(self.tenant, "tenant")
        _validate_identity(self.user, "user")
        if isinstance(self.roles, (str, bytes, Mapping)):
            raise TypeError("roles must be an iterable of strings")
        try:
            roles = frozenset(self.roles)
        except TypeError as exc:
            raise TypeError("roles must be an iterable of strings") from exc
        if any(not isinstance(role, str) for role in roles):
            raise TypeError("roles must contain only strings")
        if any(not role.strip() for role in roles):
            raise ValueError("roles must contain only non-empty strings")
        object.__setattr__(self, "roles", roles)


def _validate_identity(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
