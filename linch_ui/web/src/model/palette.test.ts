import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import type { Blueprint, CapabilityCatalog } from "../api/types";
import { MINIMAL_BLUEPRINT, WORKFLOW_BLUEPRINT } from "./fixtures";
import { blueprintToGraph } from "./graph";
import {
  SHARED_TEMPLATE_CHOICES,
  addPaletteItem,
  buildPalette,
  declaredHint,
  removeAtPointer,
  unsupportedItems,
} from "./palette";

/** Focused v1alpha2 catalog projection for palette capability resolution. */
const catalog = JSON.parse(
  readFileSync(resolve(__dirname, "__fixtures__", "catalog.json"), "utf-8"),
) as CapabilityCatalog;

describe("buildPalette", () => {
  it("resolves every palette item against the catalog", () => {
    const items = buildPalette(catalog);
    expect(items.length).toBeGreaterThan(0);
    expect(items.some((item) => item.id === "directed_workflow")).toBe(true);
    for (const item of items) {
      expect(catalog.capabilities.some((record) => record.id === item.capabilityId)).toBe(true);
    }
  });

  it("takes badge status from the catalog and never enables unsupported entries", () => {
    const items = buildPalette(catalog);
    expect(items.find((item) => item.id === "database_tool")?.status).toBe("skeleton_todo");
    expect(items.find((item) => item.id === "function_tool")?.status).toBe("skeleton_todo");
    expect(items.every((item) => item.status !== "unsupported" || !item.creatable)).toBe(true);
  });

  it("never offers direct-tool, branch, condition, or retry nodes", () => {
    const ids = buildPalette(catalog).map((item) => item.capabilityId);
    expect(ids).not.toContain("execution.arbitrary_branch_nodes");
    expect(ids).not.toContain("execution.condition_nodes");
    expect(ids).not.toContain("execution.retry_nodes");
    expect(ids).not.toContain("execution.direct_tool_nodes");
  });

  it("still exposes unsupported capabilities as documentation", () => {
    const ids = unsupportedItems(catalog).map((record) => record.id);
    expect(ids).toContain("execution.direct_tool_nodes");
    expect(ids).toContain("execution.condition_nodes");
  });

  it("returns nothing without a catalog rather than guessing", () => {
    expect(buildPalette(null)).toEqual([]);
  });
});

describe("shared template choices", () => {
  it("uses the five canonical template ids in stable order", () => {
    expect(SHARED_TEMPLATE_CHOICES.map((template) => template.id)).toEqual([
      "agent",
      "goal_verified",
      "directed_workflow",
      "coordinator",
      "routine",
    ]);
  });
});

describe("addPaletteItem", () => {
  const items = buildPalette(catalog);
  const byId = (id: string) => items.find((item) => item.id === id)!;

  it("adds a subagent without obsolete worker-specific limits", () => {
    const { blueprint, added } = addPaletteItem(MINIMAL_BLUEPRINT, byId("subagent"));
    expect(added).toBe("subagent");
    expect(blueprint.spec.subagents?.[0]).not.toHaveProperty("maxTurns");
    expect(blueprint.spec.subagents?.[0]).not.toHaveProperty("budget");
    expect(blueprintToGraph(blueprint).nodes.map((node) => node.data.kind)).toContain("subagent");
  });

  it("creates a bound agent-call when a subagent is added inside a workflow", () => {
    const result = addPaletteItem(WORKFLOW_BLUEPRINT, byId("subagent"), {
      workflowId: "pr_review",
    });
    const step = result.blueprint.spec.workflows?.[0].nodes?.at(-1);
    expect(step).toMatchObject({
      type: "agent_call",
      id: "subagent_step",
      subagent: "subagent",
    });
    expect(result.blueprint.spec.workflows?.[0].output).toBe("subagent_step");
  });

  it("does not mutate the input Blueprint", () => {
    const before = structuredClone(MINIMAL_BLUEPRINT);
    addPaletteItem(MINIMAL_BLUEPRINT, byId("subagent"));
    expect(MINIMAL_BLUEPRINT).toEqual(before);
  });

  it("adds an explicit empty directed-workflow container", () => {
    const result = addPaletteItem(MINIMAL_BLUEPRINT, byId("directed_workflow"));
    expect(result.added).toBe("workflow");
    expect(result.blueprint.spec.workflows?.[0]).toMatchObject({
      kind: "directed",
      id: "workflow",
      nodes: [],
    });
  });

  it("never guesses a workflow when adding an agent-call step", () => {
    const withoutScope = addPaletteItem(WORKFLOW_BLUEPRINT, byId("agent_call"));
    expect(withoutScope).toMatchObject({
      blueprint: WORKFLOW_BLUEPRINT,
      added: null,
      reason: "workflow_required",
    });
  });

  it("adds a step only to the explicitly selected workflow", () => {
    const blueprint = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    blueprint.spec.workflows?.push({
      kind: "directed",
      id: "release",
      displayName: "release",
      nodes: [],
    });
    const result = addPaletteItem(blueprint, byId("agent_call"), { workflowId: "release" });
    expect(result.added).toBe("step");
    expect(result.blueprint.spec.workflows?.[0].nodes).toHaveLength(4);
    expect(result.blueprint.spec.workflows?.[1].nodes).toHaveLength(1);
    expect(result.blueprint.spec.workflows?.[1].output).toBe("step");
  });

  it("advances the terminal when appending an A2A workflow step", () => {
    const result = addPaletteItem(WORKFLOW_BLUEPRINT, byId("agent_call"), {
      workflowId: "pr_review",
    });
    expect(result.blueprint.spec.workflows?.[0].output).toBe("step");
    expect(result.blueprint.spec.workflows?.[0].nodes?.at(-1)).toMatchObject({
      id: "step",
      type: "agent_call",
    });
    expect(result.blueprint.spec.workflows?.[0].nodes?.at(-1)).not.toHaveProperty("dependsOn");
  });

  it("creates bounded agent-tick and workflow-run routines", () => {
    const tick = addPaletteItem(MINIMAL_BLUEPRINT, byId("agent_tick_routine"));
    expect(tick.blueprint.spec.routines?.[0]).toMatchObject({
      kind: "agent_tick",
      target: "runtime_agent",
      maxTurns: 8,
      budget: { maxTokens: 20000 },
    });

    const run = addPaletteItem(WORKFLOW_BLUEPRINT, byId("workflow_run_routine"), {
      workflowId: "pr_review",
    });
    expect(run.blueprint.spec.routines?.at(-1)).toMatchObject({
      kind: "workflow_run",
      target: "pr_review",
      maxTurns: 8,
      budget: { maxTokens: 20000 },
    });
  });

  it("composes a host-owned scheduled workflow outside the workflow graph", () => {
    const result = addPaletteItem(WORKFLOW_BLUEPRINT, byId("scheduled_workflow"), {
      workflowId: "pr_review",
    });
    expect(result.blueprint.spec.triggers?.at(-1)).toMatchObject({
      kind: "cron",
      cron: "0 */2 * * *",
      timezone: "UTC",
    });
    expect(result.blueprint.spec.routines?.at(-1)).toMatchObject({
      kind: "workflow_run",
      target: "pr_review",
      triggers: ["every_2h"],
      maxTurns: 8,
      budget: { maxTokens: 20000 },
    });
  });

  it("emits the strict cron field and timezone", () => {
    const result = addPaletteItem(MINIMAL_BLUEPRINT, byId("cron_trigger"));
    expect(result.blueprint.spec.triggers?.[0]).toMatchObject({
      kind: "cron",
      cron: "0 2 * * *",
      timezone: "UTC",
    });
    expect(result.blueprint.spec.triggers?.[0]).not.toHaveProperty("schedule");
  });

  it("creates every host-owned trigger envelope explicitly", () => {
    const manual = addPaletteItem(MINIMAL_BLUEPRINT, byId("manual_trigger"));
    const ci = addPaletteItem(manual.blueprint, byId("ci_trigger"));
    const webhook = addPaletteItem(ci.blueprint, byId("webhook_trigger"));
    expect(webhook.blueprint.spec.triggers).toMatchObject([
      { kind: "manual" },
      { kind: "ci", provider: "generic" },
      { kind: "webhook", signingSecretEnv: "WEBHOOK_SIGNING_SECRET" },
    ]);
  });

  it("gives repeated components unique ids and refuses unsupported items", () => {
    let current = MINIMAL_BLUEPRINT;
    for (let index = 0; index < 3; index += 1) {
      current = addPaletteItem(current, byId("subagent")).blueprint;
    }
    expect(new Set(current.spec.subagents?.map((item) => item.id)).size).toBe(3);

    const unsupported = { ...byId("subagent"), creatable: false, status: "unsupported" as const };
    const result = addPaletteItem(MINIMAL_BLUEPRINT, unsupported);
    expect(result.added).toBeNull();
    expect(result.blueprint).toBe(MINIMAL_BLUEPRINT);
  });
});

describe("removeAtPointer", () => {
  it("removes an array element by v1alpha2 pointer", () => {
    const next = removeAtPointer(WORKFLOW_BLUEPRINT, "/spec/subagents/0");
    expect(next.spec.subagents).toHaveLength(1);
    expect(next.spec.subagents?.[0].id).toBe("security_reviewer");
  });

  it("removes an object member and leaves unresolved pointers alone", () => {
    const next = removeAtPointer(WORKFLOW_BLUEPRINT, "/spec/runtime/provider");
    expect(next.spec.runtime?.provider).toBeUndefined();
    expect(removeAtPointer(MINIMAL_BLUEPRINT, "/spec/nope/3")).toBe(MINIMAL_BLUEPRINT);
  });
});

describe("declaredHint", () => {
  it("tells a fresh Tool apart from an attached one", () => {
    // `runtime.agent.tools: []` is an explicit empty allowlist — nothing is attached.
    expect(declaredHint("function_tool", { inWorkflow: false, attached: false })).toEqual({
      key: "toolDeclaredHint",
      marker: "[!!]",
    });
    // A null allowlist means every declared tool is already in the primary pool,
    // so claiming "not attached yet" would be a lie.
    expect(declaredHint("function_tool", { inWorkflow: false, attached: true })).toEqual({
      key: "toolAttachedHint",
      marker: "[ok]",
    });
  });

  it("separates a runtime-member Subagent from a bound agent-call step", () => {
    expect(declaredHint("subagent", { inWorkflow: false, attached: false })).toEqual({
      key: "subagentDeclaredHint",
      marker: "[!!]",
    });
    expect(declaredHint("subagent", { inWorkflow: true, attached: false })).toEqual({
      key: "subagentBoundHint",
      marker: "[ok]",
    });
  });

  it("explains Skill discovery and that Studio starts no scheduler", () => {
    expect(declaredHint("skill", { inWorkflow: false, attached: false })).toEqual({
      key: "skillDeclaredHint",
      marker: "[ok]",
    });
    expect(declaredHint("scheduled_workflow", { inWorkflow: true, attached: false })).toEqual({
      key: "scheduledDeclaredHint",
      marker: "[ok]",
    });
  });

  it("stays silent for items with no attachment story", () => {
    expect(declaredHint("directed_workflow", { inWorkflow: false, attached: false })).toBeNull();
  });
});
