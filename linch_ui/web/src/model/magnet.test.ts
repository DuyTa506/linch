import { describe, expect, it } from "vitest";

import type { Blueprint } from "../api/types";
import { WORKFLOW_BLUEPRINT } from "./fixtures";
import {
  findMagneticConnection,
  findNearbyNode,
  isRuntimeSubagentPair,
  magneticSnapPosition,
  type PositionedCanvasNode,
} from "./magnet";

function card(id: string, x: number, y: number): PositionedCanvasNode {
  return { id, position: { x, y }, width: 180, height: 58 };
}

describe("magnetic attachment cards", () => {
  it("previews a schema-backed subagent binding and snaps its attachment ports", () => {
    const subagent = card("subagent_style_reviewer", 0, 0);
    const step = card("wf_pr_review_fetch_diff", 0, 120);
    const nodes = [subagent, step];

    const connection = findMagneticConnection(WORKFLOW_BLUEPRINT, subagent, nodes);
    expect(connection).toMatchObject({
      source: "subagent_style_reviewer",
      target: "wf_pr_review_fetch_diff",
    });
    expect(magneticSnapPosition(nodes, subagent.id, connection!)).toEqual({ x: 0, y: 28 });
  });

  it("keeps workflow control flow handle-only", () => {
    const style = card("wf_pr_review_style_step", 0, 0);
    const security = card("wf_pr_review_security_step", 0, 80);
    expect(findMagneticConnection(WORKFLOW_BLUEPRINT, style, [style, security])).toBeNull();
  });

  it("recognizes the unsupported runtime-to-subagent gesture for an explanatory rejection", () => {
    const runtime = card("primary_agent", 0, 0);
    const subagent = card("subagent_style_reviewer", 0, 80);
    expect(findMagneticConnection(WORKFLOW_BLUEPRINT, subagent, [runtime, subagent])).toBeNull();
    expect(findNearbyNode(subagent, [runtime, subagent])?.id).toBe(runtime.id);
    expect(isRuntimeSubagentPair(WORKFLOW_BLUEPRINT, runtime.id, subagent.id)).toBe(true);
  });

  it("does not attract distant cards", () => {
    const subagent = card("subagent_style_reviewer", 0, 0);
    const step = card("wf_pr_review_fetch_diff", 500, 500);
    expect(findMagneticConnection(WORKFLOW_BLUEPRINT, subagent, [subagent, step])).toBeNull();
  });

  it("attaches a Tool card to a primary or deep runtime with an explicit pool", () => {
    const explicit = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    explicit.spec.runtime!.agent!.tools = [];
    const tool = card("tool_review_tool", 0, 0);
    const runtime = card("primary_agent", 0, 100);
    expect(findMagneticConnection(explicit, tool, [tool, runtime])).toMatchObject({
      source: tool.id,
      target: runtime.id,
    });
  });
});
