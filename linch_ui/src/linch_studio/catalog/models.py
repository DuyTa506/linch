"""Immutable records for the hand-authored Studio capability catalog."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

StatusId = Literal["runtime_ready", "skeleton_todo", "unsupported"]
RelationDecision = Literal["allow", "deny"]
RelationEndpointKind = Literal[
    "agent_loop",
    "agent_tick_routine",
    "directed_workflow",
    "hook",
    "routine",
    "skill",
    "subagent",
    "tool",
    "trigger",
    "workflow_run_routine",
    "workflow_step",
]
RelationConstraint = Literal["acyclic", "max_one_per_target", "same_workflow"]


@dataclass(frozen=True, slots=True)
class CatalogVersion:
    """Version metadata for one catalog document."""

    api_version: str
    revision: int
    target_linch: str
    source: Literal["hand_authored"] = "hand_authored"

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": self.api_version,
            "revision": self.revision,
            "targetLinch": self.target_linch,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """UI badge and export behavior for a capability status."""

    id: StatusId
    badge: str
    description: str
    order: int
    export_allowed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "badge": self.badge,
            "description": self.description,
            "order": self.order,
            "exportAllowed": self.export_allowed,
        }


@dataclass(frozen=True, slots=True)
class CapabilityArea:
    """Stable grouping used by the palette and readiness views."""

    id: str
    title: str
    description: str
    order: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """One explicit generator capability; never inferred from runtime signatures."""

    id: str
    title: str
    summary: str
    area: CapabilityArea
    status: CapabilityStatus
    execution_model: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "area": self.area.id,
            "status": self.status.id,
            "badge": self.status.badge,
            "exportAllowed": self.status.export_allowed,
            "executionModel": self.execution_model,
        }


@dataclass(frozen=True, slots=True)
class CapabilityBadge:
    """Small JSON-ready projection used by selected nodes and inspector fields."""

    capability_id: str
    status: StatusId
    label: str
    export_allowed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "capabilityId": self.capability_id,
            "status": self.status,
            "label": self.label,
            "exportAllowed": self.export_allowed,
        }


@dataclass(frozen=True, slots=True)
class RelationMatrixVersion:
    """Version metadata for editable canvas relation semantics."""

    api_version: str
    revision: int
    blueprint_api_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": self.api_version,
            "revision": self.revision,
            "blueprintApiVersion": self.blueprint_api_version,
        }


@dataclass(frozen=True, slots=True)
class EditableRelation:
    """One ordered allow/deny rule for a semantic canvas relation."""

    id: str
    source_kind: RelationEndpointKind
    target_kind: RelationEndpointKind
    relation: str
    decision: RelationDecision
    field: str | None
    constraints: tuple[RelationConstraint, ...]
    reason: str
    order: int

    def __post_init__(self) -> None:
        if not self.id or not self.relation or not self.reason:
            raise ValueError("relation id, relation, and reason must be non-empty")
        if self.decision == "allow" and self.field is None:
            raise ValueError(f"allowed relation {self.id!r} must declare its Blueprint field")
        if self.decision == "deny" and self.field is not None:
            raise ValueError(f"denied relation {self.id!r} cannot declare a Blueprint field")
        _require_unique(f"constraint for relation {self.id!r}", self.constraints)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sourceKind": self.source_kind,
            "targetKind": self.target_kind,
            "relation": self.relation,
            "decision": self.decision,
            "field": self.field,
            "constraints": list(self.constraints),
            "reason": self.reason,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class EditableRelationMatrix:
    """Complete versioned relation matrix shared by every Studio authoring surface."""

    version: RelationMatrixVersion
    relations: tuple[EditableRelation, ...]

    def __post_init__(self) -> None:
        _require_unique("relation", (item.id for item in self.relations))
        _require_unique("relation order", (str(item.order) for item in self.relations))
        _require_unique(
            "relation signature",
            (
                ":".join(
                    (
                        item.source_kind,
                        item.target_kind,
                        item.relation,
                    )
                )
                for item in self.relations
            ),
        )
        if tuple(item.order for item in self.relations) != tuple(
            sorted(item.order for item in self.relations)
        ):
            raise ValueError("relations must be ordered by ascending order")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version.to_dict(),
            "relations": [item.to_dict() for item in self.relations],
        }


@dataclass(frozen=True, slots=True)
class CapabilityCatalog:
    """Complete immutable catalog and deterministic JSON projection."""

    version: CatalogVersion
    statuses: tuple[CapabilityStatus, ...]
    areas: tuple[CapabilityArea, ...]
    capabilities: tuple[CapabilityRecord, ...]
    relation_matrix: EditableRelationMatrix

    def __post_init__(self) -> None:
        _require_unique("status", (item.id for item in self.statuses))
        _require_unique("area", (item.id for item in self.areas))
        _require_unique("capability", (item.id for item in self.capabilities))
        _require_unique("status order", (str(item.order) for item in self.statuses))

        expected_export_policy = {
            "runtime_ready": True,
            "skeleton_todo": True,
            "unsupported": False,
        }
        actual_status_ids = {item.id for item in self.statuses}
        if actual_status_ids != set(expected_export_policy):
            raise ValueError("catalog must declare exactly the three stable capability statuses")
        for status in self.statuses:
            if status.export_allowed is not expected_export_policy[status.id]:
                raise ValueError(
                    f"status {status.id!r} has an invalid export policy: "
                    f"expected {expected_export_policy[status.id]}"
                )

        statuses = set(self.statuses)
        areas = set(self.areas)
        for capability in self.capabilities:
            if capability.status not in statuses:
                raise ValueError(f"capability {capability.id!r} references an undeclared status")
            if capability.area not in areas:
                raise ValueError(f"capability {capability.id!r} references an undeclared area")

    def to_dict(self) -> dict[str, object]:
        return {
            "catalogVersion": self.version.to_dict(),
            "statuses": [item.to_dict() for item in self.statuses],
            "areas": [item.to_dict() for item in self.areas],
            "capabilities": [item.to_dict() for item in self.capabilities],
            "relationMatrix": self.relation_matrix.to_dict(),
        }


def _require_unique(kind: str, values: Iterable[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        rendered = ", ".join(sorted(duplicates))
        raise ValueError(f"duplicate {kind} ids: {rendered}")


__all__ = [
    "CapabilityArea",
    "CapabilityBadge",
    "CapabilityCatalog",
    "CapabilityRecord",
    "CapabilityStatus",
    "CatalogVersion",
    "EditableRelation",
    "EditableRelationMatrix",
    "RelationConstraint",
    "RelationDecision",
    "RelationEndpointKind",
    "RelationMatrixVersion",
    "StatusId",
]
