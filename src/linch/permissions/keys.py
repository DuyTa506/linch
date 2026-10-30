"""Stable permission-decision keys for durable HITL approval.

The key is ``"{tool_name}:{json.dumps(input, sort_keys=True, separators=(',',':'))}"``
— shared with the LoopGuard canonicalisation. It is
stable across provider calls because it is derived from the model's proposed
``input`` dict, not the ephemeral ``tool_use_id`` that changes each call.
"""

from __future__ import annotations

import json
from typing import Any

from .._wire import WIRE_VERSION, require_wire_version


def permission_decision_key(tool_name: str, input: dict[str, Any]) -> str:
    return f"{tool_name}:{json.dumps(input, sort_keys=True, separators=(',', ':'))}"


def permission_decision_to_dict(decision: Any) -> dict[str, Any]:
    return {
        "schema_version": WIRE_VERSION,
        "decision": decision.decision,
        "reason": decision.reason,
        # Retain the field for wire compatibility, but Linch 2 never persists
        # post-approval mutation.
        "updated_input": None,
    }


def permission_decision_from_dict(raw: dict[str, Any]) -> Any:
    from .engine import PermissionDecision

    if not isinstance(raw, dict):
        require_wire_version(None)
    require_wire_version(raw.get("schema_version"))
    decision = raw.get("decision")
    if decision not in {"allow", "deny"}:
        raise ValueError("stored permission decision must be 'allow' or 'deny'")
    if raw.get("updated_input") is not None:
        raise ValueError("stored permission decision with updated_input is not replayable")
    return PermissionDecision(
        decision=decision,
        reason=raw.get("reason"),
    )
