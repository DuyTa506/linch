"""The ``Schedule`` value type and next-run computation.

A schedule fires either on a cron expression or a fixed interval. The ``payload``
is opaque to the SDK — the embedder decides what a fired schedule means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .cron import next_cron_time, validate_cron


def _new_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class Schedule:
    """A single time trigger. Exactly one of ``cron`` / ``interval_s`` is set."""

    payload: str = ""
    cron: str | None = None
    interval_s: float | None = None
    next_run: float | None = None
    enabled: bool = True
    created_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_new_id)

    def __post_init__(self) -> None:
        if (self.cron is None) == (self.interval_s is None):
            raise ValueError("Schedule needs exactly one of cron or interval_s")
        if self.cron is not None:
            validate_cron(self.cron)
        if self.interval_s is not None and self.interval_s <= 0:
            raise ValueError("interval_s must be positive")

    def compute_next_run(self, after_epoch: float) -> float:
        """Next fire time strictly after *after_epoch* (epoch seconds)."""
        if self.cron is not None:
            return next_cron_time(self.cron, after_epoch)
        assert self.interval_s is not None
        return after_epoch + self.interval_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "payload": self.payload,
            "cron": self.cron,
            "interval_s": self.interval_s,
            "next_run": self.next_run,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        return cls(
            payload=data.get("payload", ""),
            cron=data.get("cron"),
            interval_s=data.get("interval_s"),
            next_run=data.get("next_run"),
            enabled=bool(data.get("enabled", True)),
            created_at=data.get("created_at"),
            metadata=dict(data.get("metadata") or {}),
            id=data.get("id") or _new_id(),
        )
