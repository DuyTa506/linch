import type { Blueprint } from "../api/types";
import { blueprintToGraph, connectSemanticRelation } from "./graph";

export const MAGNET_RADIUS = 96;
export const INVALID_DROP_RADIUS = 64;

export interface PositionedCanvasNode {
  id: string;
  position: { x: number; y: number };
  width?: number;
  height?: number;
  measured?: { width?: number; height?: number };
}

export interface MagneticConnection {
  source: string;
  target: string;
  distance: number;
}

const DEFAULT_WIDTH = 190;
const DEFAULT_HEIGHT = 58;

function size(node: PositionedCanvasNode): { width: number; height: number } {
  return {
    width: node.measured?.width ?? node.width ?? DEFAULT_WIDTH,
    height: node.measured?.height ?? node.height ?? DEFAULT_HEIGHT,
  };
}

/** Distance between card rectangles: zero while cards overlap. */
function cardDistance(left: PositionedCanvasNode, right: PositionedCanvasNode): number {
  const leftSize = size(left);
  const rightSize = size(right);
  const dx = Math.max(
    left.position.x - (right.position.x + rightSize.width),
    right.position.x - (left.position.x + leftSize.width),
    0,
  );
  const dy = Math.max(
    left.position.y - (right.position.y + rightSize.height),
    right.position.y - (left.position.y + leftSize.height),
    0,
  );
  return Math.hypot(dx, dy);
}

function attachmentPortDistance(
  source: PositionedCanvasNode,
  target: PositionedCanvasNode,
): number {
  const sourceSize = size(source);
  const targetSize = size(target);
  const sourcePort = {
    x: source.position.x + sourceSize.width / 2,
    y: source.position.y + sourceSize.height,
  };
  const targetPort = {
    x: target.position.x + targetSize.width / 2,
    y: target.position.y,
  };
  return Math.hypot(sourcePort.x - targetPort.x, sourcePort.y - targetPort.y);
}

function isWorkflowFlowPair(blueprint: Blueprint, sourceId: string, targetId: string): boolean {
  const byId = new Map(blueprintToGraph(blueprint).nodes.map((node) => [node.id, node]));
  return (
    byId.get(sourceId)?.data.kind === "workflow_step" &&
    byId.get(targetId)?.data.kind === "workflow_step"
  );
}

/**
 * Find the nearest schema-backed attachment relation for a card being dragged.
 * Workflow step control flow is deliberately excluded: it remains handle-only.
 */
export function findMagneticConnection(
  blueprint: Blueprint,
  dragged: PositionedCanvasNode,
  nodes: PositionedCanvasNode[],
  radius = MAGNET_RADIUS,
  relationAllowed: (sourceId: string, targetId: string) => boolean = () => true,
): MagneticConnection | null {
  let nearest: MagneticConnection | null = null;
  for (const candidate of nodes) {
    if (candidate.id === dragged.id) continue;

    const directions = [
      [dragged.id, candidate.id],
      [candidate.id, dragged.id],
    ] as const;
    for (const [source, target] of directions) {
      const sourceNode = source === dragged.id ? dragged : candidate;
      const targetNode = target === dragged.id ? dragged : candidate;
      const distance = attachmentPortDistance(sourceNode, targetNode);
      if (distance > radius || (nearest && distance >= nearest.distance)) continue;
      if (isWorkflowFlowPair(blueprint, source, target)) continue;
      if (!relationAllowed(source, target)) continue;
      if (connectSemanticRelation(blueprint, source, target).connected) {
        nearest = { source, target, distance };
        break;
      }
    }
  }
  return nearest;
}

export function findNearbyNode(
  dragged: PositionedCanvasNode,
  nodes: PositionedCanvasNode[],
  radius = INVALID_DROP_RADIUS,
): PositionedCanvasNode | null {
  let nearest: PositionedCanvasNode | null = null;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const candidate of nodes) {
    if (candidate.id === dragged.id) continue;
    const distance = cardDistance(dragged, candidate);
    if (distance <= radius && distance < nearestDistance) {
      nearest = candidate;
      nearestDistance = distance;
    }
  }
  return nearest;
}

export function isRuntimeSubagentPair(
  blueprint: Blueprint,
  firstId: string,
  secondId: string,
): boolean {
  const byId = new Map(blueprintToGraph(blueprint).nodes.map((node) => [node.id, node]));
  return new Set([byId.get(firstId)?.data.kind, byId.get(secondId)?.data.kind]).size === 2 &&
    [byId.get(firstId)?.data.kind, byId.get(secondId)?.data.kind].includes("runtime_agent") &&
    [byId.get(firstId)?.data.kind, byId.get(secondId)?.data.kind].includes("subagent");
}

/** Align attachment-out above attachment-in while leaving a readable gap. */
export function magneticSnapPosition(
  nodes: PositionedCanvasNode[],
  draggedId: string,
  connection: MagneticConnection,
  gap = 34,
): { x: number; y: number } | null {
  const source = nodes.find((node) => node.id === connection.source);
  const target = nodes.find((node) => node.id === connection.target);
  if (!source || !target) return null;
  const sourceSize = size(source);
  const targetSize = size(target);

  if (draggedId === source.id) {
    return {
      x: target.position.x + (targetSize.width - sourceSize.width) / 2,
      y: target.position.y - sourceSize.height - gap,
    };
  }
  if (draggedId === target.id) {
    return {
      x: source.position.x + (sourceSize.width - targetSize.width) / 2,
      y: source.position.y + sourceSize.height + gap,
    };
  }
  return null;
}
