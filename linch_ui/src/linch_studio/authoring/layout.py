"""Deterministic design-time placement for proposal-created workflow nodes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from linch_studio.spec import Blueprint

HORIZONTAL_GAP = 280.0
VERTICAL_GAP = 160.0


def workflow_node_key(workflow_id: str, node_id: str) -> str:
    """Return the canvas layout key for a node scoped to one workflow.

    Must mirror the frontend's ``toLayoutId("wf", workflowId, nodeId)`` — both
    ids already match the spec's ``^[a-z][a-z0-9_]*$`` pattern, so plain
    joining produces the exact key the canvas reads.
    """

    return f"wf_{workflow_id}_{node_id}"


def deterministic_layout_for_new_nodes(
    current: Blueprint,
    candidate: Blueprint,
    existing_layout: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Preserve existing layout and place only newly proposed workflow nodes.

    X is topological depth and Y is declaration order within that depth. If an
    invalid cyclic draft reaches this helper, unresolved nodes fall back to
    depth zero; semantic validation still prevents acceptance/export.
    """

    layout = {key: dict(value) for key, value in existing_layout.items()}
    current_nodes = {
        (workflow.id, node.id) for workflow in current.spec.workflows for node in workflow.nodes
    }

    for workflow in candidate.spec.workflows:
        depths = _node_depths(workflow.nodes)
        row_by_depth: dict[int, int] = {}
        positions: dict[str, tuple[float, float]] = {}
        for node in workflow.nodes:
            depth = depths.get(node.id, 0)
            row = row_by_depth.get(depth, 0)
            row_by_depth[depth] = row + 1
            positions[node.id] = (depth * HORIZONTAL_GAP, row * VERTICAL_GAP)

        for node in workflow.nodes:
            if (workflow.id, node.id) in current_nodes:
                continue
            key = workflow_node_key(workflow.id, node.id)
            if key in layout or node.id in layout:
                continue
            x, y = positions[node.id]
            layout[key] = {"x": x, "y": y}
    return layout


def _node_depths(nodes: list[Any]) -> dict[str, int]:
    node_ids = {node.id for node in nodes}
    pending = list(nodes)
    depths: dict[str, int] = {}
    while pending:
        progressed = False
        remaining: list[Any] = []
        for node in pending:
            dependencies = [item for item in node.depends_on if item in node_ids]
            if all(item in depths for item in dependencies):
                depths[node.id] = max((depths[item] + 1 for item in dependencies), default=0)
                progressed = True
            else:
                remaining.append(node)
        if not progressed:
            for node in remaining:
                depths.setdefault(node.id, 0)
            break
        pending = remaining
    return depths


__all__ = [
    "HORIZONTAL_GAP",
    "VERTICAL_GAP",
    "deterministic_layout_for_new_nodes",
    "workflow_node_key",
]
