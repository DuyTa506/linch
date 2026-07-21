import { describe, expect, it } from "vitest";
import { parse } from "yaml";

import type { Blueprint, CapabilityCatalog } from "../api/types";
import { emitBlueprintYaml, pruneEmpty } from "./emit";
import { MINIMAL_BLUEPRINT, WORKFLOW_BLUEPRINT } from "./fixtures";
import catalogFixture from "./__fixtures__/catalog.json";
import {
  AUTO_LAYOUT_COLUMN,
  SUPPORTED_RELATION_IDS,
  blueprintToGraph,
  boundWorkflowStepId,
  catalogAllowsConnection,
  connectSemanticRelation,
  connectWorkflowNodes,
  disconnectSemanticRelation,
  fallbackNodePosition,
  graphForScope,
  toLayoutId,
} from "./graph";

const LAYOUT_ID = /^[a-z][a-z0-9_]*$/;
const CATALOG = catalogFixture as unknown as CapabilityCatalog;

describe("toLayoutId", () => {
  it("produces ids the layout API accepts", () => {
    expect(toLayoutId("wf", "pr-review", "fetch diff")).toMatch(LAYOUT_ID);
    expect(toLayoutId("tool", "HTTP.Fetch")).toMatch(LAYOUT_ID);
    expect(toLayoutId("9lives")).toMatch(LAYOUT_ID);
  });
});

describe("graphForScope", () => {
  const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);

  it("keeps project-map concepts separate from workflow steps", () => {
    const scoped = graphForScope(graph, { kind: "project" });
    expect(new Set(scoped.nodes.map((node) => node.data.kind))).toEqual(
      new Set(["runtime_agent", "directed_workflow", "routine"]),
    );
  });

  it("shows one directed workflow with only its relevant attachments", () => {
    const scoped = graphForScope(graph, { kind: "workflow", id: "pr_review" });
    expect(scoped.nodes.some((node) => node.data.kind === "workflow_step")).toBe(true);
    expect(scoped.nodes.filter((node) => node.data.kind === "subagent")).toHaveLength(2);
    expect(scoped.nodes.filter((node) => node.data.kind === "tool")).toHaveLength(2);
    expect(scoped.nodes.some((node) => node.data.kind === "routine")).toBe(false);
    expect(scoped.edges.every((edge) => edge.relation !== "routine_target")).toBe(true);
  });

  it("keeps unbound capabilities visible for their first workflow attachment", () => {
    const unbound = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    unbound.spec.subagents?.push({
      id: "new_reviewer",
      displayName: "new-reviewer",
      instructions: "Review a new dimension.",
      tools: [],
    });
    unbound.spec.tools?.push({
      kind: "function",
      id: "new_tool",
      displayName: "new-tool",
      description: "New workflow tool.",
    });
    const scoped = graphForScope(blueprintToGraph(unbound), {
      kind: "workflow",
      id: "pr_review",
    });
    expect(scoped.nodes.map((node) => node.id)).toContain("subagent_new_reviewer");
    expect(scoped.nodes.map((node) => node.id)).toContain("tool_new_tool");
  });

  it("shows one routine as trigger to routine to target", () => {
    const scoped = graphForScope(graph, { kind: "routine", id: "pr_review_run" });
    expect(scoped.nodes.map((node) => node.id).sort()).toEqual(
      ["loop_pr_review_run", "trigger_cron", "trigger_manual", "workflow_pr_review"].sort(),
    );
    expect(scoped.edges).toHaveLength(2);
    expect(scoped.edges.some((edge) => edge.source === "trigger_cron")).toBe(false);
  });
});

describe("blueprintToGraph", () => {
  it("maps the runtime agent without drawing provider or capability rails as flow nodes", () => {
    const graph = blueprintToGraph(MINIMAL_BLUEPRINT);
    expect(graph.nodes.map((node) => node.data.kind)).toEqual(["runtime_agent"]);
    expect(graph.nodes[0].data.pointer).toBe("/spec/runtime/agent");
  });

  it("preserves legacy runtime and routine layout ids after migration", () => {
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    expect(graph.nodes.some((node) => node.id === "primary_agent")).toBe(true);
    expect(graph.nodes.some((node) => node.id === "loop_agent_review_tick")).toBe(true);
    expect(graph.nodes.some((node) => node.id === "loop_pr_review_run")).toBe(true);
  });

  it("renders an explicit directed-workflow container and its scoped steps", () => {
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    const workflow = graph.nodes.find((node) => node.id === "workflow_pr_review");
    const step = graph.nodes.find((node) => node.id === "wf_pr_review_fetch_diff");
    expect(workflow?.data).toMatchObject({ kind: "directed_workflow", sourceId: "pr_review" });
    expect(step?.data).toMatchObject({ kind: "workflow_step", workflowId: "pr_review" });
  });

  it("emits every canvas id in layout-id form", () => {
    for (const node of blueprintToGraph(WORKFLOW_BLUEPRINT).nodes) {
      expect(node.id).toMatch(LAYOUT_ID);
    }
  });

  it("derives dependency, tool, subagent, trigger, and routine-target edges", () => {
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    const edge = (source: string, target: string, relation: string) =>
      graph.edges.some(
        (item) => item.source === source && item.target === target && item.relation === relation,
      );
    expect(edge("wf_pr_review_fetch_diff", "wf_pr_review_style_step", "depends_on")).toBe(true);
    expect(edge("subagent_style_reviewer", "wf_pr_review_style_step", "subagent_binding")).toBe(
      true,
    );
    expect(edge("tool_review_tool", "primary_agent", "tool_filter")).toBe(true);
    expect(edge("tool_review_tool", "subagent_style_reviewer", "tool_filter")).toBe(true);
    expect(edge("tool_review_tool", "skill_review_checklist", "allowed_tool")).toBe(true);
    expect(edge("trigger_manual", "loop_pr_review_run", "trigger_binding")).toBe(true);
    expect(edge("loop_pr_review_run", "workflow_pr_review", "routine_target")).toBe(true);
    expect(edge("loop_agent_review_tick", "primary_agent", "routine_target")).toBe(true);
  });

  it("flags nodes already anchoring an attach edge, independent of new-attachment eligibility", () => {
    // Live regression: a workflow step's `attachment-in` handle was hidden via
    // `display:none` whenever no *new* subagent could attach there — which is
    // always true for a step whose one allowed subagent is already bound.
    // React Flow then can't measure the handle its own existing edge needs,
    // and the edge's target end collapses to an unrelated fallback point,
    // rendering as a long, wrongly-directed line. This structural flag must
    // stay true for an already-bound pair regardless of that eligibility.
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    const node = (id: string) => graph.nodes.find((item) => item.id === id)!;
    expect(node("wf_pr_review_style_step").data.hasAttachmentIn).toBe(true);
    expect(node("subagent_style_reviewer").data.hasAttachmentOut).toBe(true);
    expect(node("wf_pr_review_fetch_diff").data.hasAttachmentIn).toBe(false);
    expect(node("wf_pr_review_fetch_diff").data.hasAttachmentOut).toBe(false);
  });

  it("marks the explicit output and same-depth fan-out", () => {
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    expect(graph.outputs).toEqual(["wf_pr_review_synthesize"]);
    expect(graph.nodes.find((node) => node.id === "wf_pr_review_style_step")?.data.isParallel).toBe(
      true,
    );
    expect(graph.nodes.find((node) => node.id === "wf_pr_review_fetch_diff")?.data.isParallel).toBe(
      false,
    );
  });

  it("renders a direct_tool step as unsupported rather than hiding it", () => {
    const withDirectTool = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    withDirectTool.spec.workflows?.[0].nodes?.push({
      type: "direct_tool",
      id: "raw_call",
      label: "raw-call",
      tool: "review_tool",
    });
    const node = blueprintToGraph(withDirectTool).nodes.find(
      (item) => item.id === "wf_pr_review_raw_call",
    );
    expect(node?.data.status).toBe("unsupported");
  });

  it("never emits an edge to a missing node", () => {
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    const ids = new Set(graph.nodes.map((node) => node.id));
    expect(graph.edges.every((edge) => ids.has(edge.source) && ids.has(edge.target))).toBe(true);
  });
});

describe("boundWorkflowStepId", () => {
  it("finds the workflow step a subagent is bound to", () => {
    const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
    expect(boundWorkflowStepId(graph, "subagent_style_reviewer")).toBe("wf_pr_review_style_step");
    expect(boundWorkflowStepId(graph, "subagent_security_reviewer")).toBe(
      "wf_pr_review_security_step",
    );
  });

  it("returns undefined for a subagent with no bound workflow step", () => {
    const unbound = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    unbound.spec.subagents?.push({
      id: "new_reviewer",
      displayName: "new-reviewer",
      instructions: "Review a new dimension.",
      tools: [],
    });
    const graph = blueprintToGraph(unbound);
    expect(boundWorkflowStepId(graph, "subagent_new_reviewer")).toBeUndefined();
  });
});

describe("fallbackNodePosition", () => {
  const graph = blueprintToGraph(WORKFLOW_BLUEPRINT);
  const styleStep = graph.nodes.find((node) => node.id === "wf_pr_review_style_step")!;
  const styleReviewer = graph.nodes.find((node) => node.id === "subagent_style_reviewer")!;
  const securityReviewer = graph.nodes.find((node) => node.id === "subagent_security_reviewer")!;

  it("renders a bound subagent directly above its workflow step's resolved position", () => {
    const saved = new Map([
      ["wf_pr_review_style_step", { x: 600, y: 460 }],
      ["wf_pr_review_security_step", { x: 600, y: 620 }],
    ]);
    const stylePosition = fallbackNodePosition(graph, styleReviewer, 0, saved);
    expect(stylePosition).toEqual({ x: 600, y: 360 });
    expect(stylePosition.y).toBeLessThan(saved.get("wf_pr_review_style_step")!.y);

    // Regardless of declaration order among unrelated columns, each subagent
    // still tracks its own bound target rather than a shared column stack.
    const securityPosition = fallbackNodePosition(graph, securityReviewer, 1, saved);
    expect(securityPosition).toEqual({ x: 600, y: 520 });
  });

  it("falls back to the column stack for an unbound subagent or an unresolved target", () => {
    const noTargetSaved = new Map<string, { x: number; y: number }>();
    expect(fallbackNodePosition(graph, styleReviewer, 3, noTargetSaved)).toEqual({
      x: 40 + AUTO_LAYOUT_COLUMN.subagent * 240,
      y: 60 + 3 * 90,
    });
    expect(fallbackNodePosition(graph, styleStep, 2, noTargetSaved)).toEqual({
      x: 40 + AUTO_LAYOUT_COLUMN.workflow_step * 240,
      y: 60 + 2 * 90,
    });
  });
});

describe("semantic relations", () => {
  const fetch = "wf_pr_review_fetch_diff";
  const style = "wf_pr_review_style_step";
  const security = "wf_pr_review_security_step";
  const output = "wf_pr_review_synthesize";

  it("fails frontend drift when the backend adds an unimplemented editable relation", () => {
    const advertised = new Set(
      CATALOG.relationMatrix?.relations
        .filter((relation) => relation.decision === "allow")
        .map((relation) => relation.id),
    );
    expect(advertised).toEqual(SUPPORTED_RELATION_IDS);
    expect(
      catalogAllowsConnection(CATALOG, WORKFLOW_BLUEPRINT, "tool_review_tool", "primary_agent"),
    ).toBe(true);
    expect(
      catalogAllowsConnection(
        CATALOG,
        WORKFLOW_BLUEPRINT,
        "subagent_style_reviewer",
        "primary_agent",
      ),
    ).toBe(false);
  });

  it("binds a trigger to a routine of either kind", () => {
    // The matrix names a trigger's target generically ("routine") but a routine's
    // own target by kind ("agent_tick_routine"). A routine card has to answer to
    // both vocabularies or trigger binding can never be authored on the canvas.
    expect(
      catalogAllowsConnection(CATALOG, WORKFLOW_BLUEPRINT, "trigger_manual", "loop_agent_review_tick"),
    ).toBe(true);
    expect(
      catalogAllowsConnection(CATALOG, WORKFLOW_BLUEPRINT, "trigger_cron", "loop_pr_review_run"),
    ).toBe(true);
    expect(
      catalogAllowsConnection(CATALOG, WORKFLOW_BLUEPRINT, "loop_agent_review_tick", "primary_agent"),
    ).toBe(true);
  });

  it("persists same-workflow dependencies and rejects duplicates and cycles", () => {
    const result = connectWorkflowNodes(WORKFLOW_BLUEPRINT, security, style);
    expect(result.connected).toBe(true);
    expect(result.blueprint.spec.workflows?.[0].nodes?.find((node) => node.id === "style_step"))
      .toMatchObject({ dependsOn: ["fetch_diff", "security_step"] });

    expect(connectWorkflowNodes(WORKFLOW_BLUEPRINT, fetch, style).reason).toBe("duplicate");
    expect(connectWorkflowNodes(WORKFLOW_BLUEPRINT, style, fetch).reason).toBe("cycle");
    expect(connectWorkflowNodes(WORKFLOW_BLUEPRINT, output, fetch).reason).toBe("incompatible");
  });

  it("rejects cross-workflow flow explicitly", () => {
    const blueprint = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    blueprint.spec.workflows?.push({
      kind: "directed",
      id: "release",
      displayName: "release",
      output: "publish",
      nodes: [{ type: "agent_call", id: "publish", label: "publish", prompt: "Publish." }],
    });
    expect(connectSemanticRelation(blueprint, fetch, "wf_release_publish").reason).toBe(
      "cross_workflow",
    );
  });

  it("persists tool filters on subagents, steps, and skills", () => {
    const subagent = connectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "tool_security_tool",
      "subagent_style_reviewer",
    );
    expect(subagent.connected).toBe(true);
    expect(subagent.blueprint.spec.subagents?.[0].tools).toEqual([
      "review_tool",
      "security_tool",
    ]);

    const step = connectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "tool_review_tool",
      "wf_pr_review_security_step",
    );
    expect(step.connected).toBe(true);
    expect(step.blueprint.spec.workflows?.[0].nodes?.find((node) => node.id === "security_step"))
      .toMatchObject({ tools: ["security_tool", "review_tool"] });

    const skill = connectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "tool_security_tool",
      "skill_review_checklist",
    );
    expect(skill.connected).toBe(true);
    expect(skill.blueprint.spec.skills?.[0].allowedTools).toEqual([
      "review_tool",
      "security_tool",
    ]);
  });

  it("persists primary/deep-agent tool attachments and keeps child filters inside that pool", () => {
    const explicit = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    explicit.spec.runtime!.agent!.tools = [];
    for (const subagent of explicit.spec.subagents ?? []) subagent.tools = [];
    for (const skill of explicit.spec.skills ?? []) skill.allowedTools = [];
    for (const workflow of explicit.spec.workflows ?? []) {
      for (const node of workflow.nodes ?? []) {
        if (node.type === "agent_call") node.tools = [];
      }
    }

    const root = connectSemanticRelation(explicit, "tool_review_tool", "primary_agent");
    expect(root.connected).toBe(true);
    expect(root.blueprint.spec.runtime?.agent?.tools).toEqual(["review_tool"]);

    const child = connectSemanticRelation(
      explicit,
      "tool_security_tool",
      "subagent_style_reviewer",
    );
    expect(child.connected).toBe(true);
    expect(child.blueprint.spec.runtime?.agent?.tools).toEqual(["security_tool"]);
    expect(child.blueprint.spec.subagents?.[0].tools).toEqual(["security_tool"]);
  });

  it("enforces at most one subagent binding", () => {
    expect(
      connectSemanticRelation(
        WORKFLOW_BLUEPRINT,
        "subagent_style_reviewer",
        "wf_pr_review_style_step",
      ).reason,
    ).toBe("duplicate");
    expect(
      connectSemanticRelation(
        WORKFLOW_BLUEPRINT,
        "subagent_security_reviewer",
        "wf_pr_review_style_step",
      ).reason,
    ).toBe("max_one");
  });

  it("persists trigger bindings and enforces routine-target compatibility", () => {
    const trigger = connectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "trigger_cron",
      "loop_pr_review_run",
    );
    expect(trigger.connected).toBe(true);
    expect(trigger.blueprint.spec.routines?.[1].triggers).toEqual(["manual", "cron"]);

    expect(
      connectSemanticRelation(
        WORKFLOW_BLUEPRINT,
        "loop_agent_review_tick",
        "workflow_pr_review",
      ).reason,
    ).toBe("incompatible");
    expect(
      connectSemanticRelation(WORKFLOW_BLUEPRINT, "loop_pr_review_run", "primary_agent").reason,
    ).toBe("incompatible");

    const withRelease = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    withRelease.spec.workflows?.push({
      kind: "directed",
      id: "release",
      displayName: "release",
      nodes: [],
    });
    const retargeted = connectSemanticRelation(
      withRelease,
      "loop_pr_review_run",
      "workflow_release",
    );
    expect(retargeted.connected).toBe(true);
    expect(retargeted.blueprint.spec.routines?.[1]).toMatchObject({ target: "release" });
  });

  it("forbids direct agent-to-agent links", () => {
    expect(
      connectSemanticRelation(WORKFLOW_BLUEPRINT, "primary_agent", "subagent_style_reviewer")
        .reason,
    ).toBe("incompatible");
  });

  it("removes optional dependency, tool, subagent, and trigger relations", () => {
    const dependency = disconnectSemanticRelation(WORKFLOW_BLUEPRINT, fetch, style);
    expect(dependency.disconnected).toBe(true);
    expect(dependency.blueprint.spec.workflows?.[0].nodes?.find((node) => node.id === "style_step"))
      .toMatchObject({ dependsOn: [] });

    const tool = disconnectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "tool_review_tool",
      "subagent_style_reviewer",
    );
    expect(tool.disconnected).toBe(true);
    expect(tool.blueprint.spec.subagents?.[0].tools).toEqual([]);

    const binding = disconnectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "subagent_style_reviewer",
      style,
    );
    expect(binding.disconnected).toBe(true);
    expect(binding.blueprint.spec.workflows?.[0].nodes?.find((node) => node.id === "style_step"))
      .not.toHaveProperty("subagent");

    const trigger = disconnectSemanticRelation(
      WORKFLOW_BLUEPRINT,
      "trigger_manual",
      "loop_pr_review_run",
    );
    expect(trigger.disconnected).toBe(true);
    expect(trigger.blueprint.spec.routines?.[1].triggers).toEqual([]);
  });

  it("does not remove a tool from the primary pool while a child still references it", () => {
    expect(
      disconnectSemanticRelation(WORKFLOW_BLUEPRINT, "tool_review_tool", "primary_agent").reason,
    ).toBe("required_by_child");

    const detached = structuredClone(WORKFLOW_BLUEPRINT) as Blueprint;
    detached.spec.subagents![0].tools = [];
    detached.spec.skills![0].allowedTools = [];
    const result = disconnectSemanticRelation(detached, "tool_review_tool", "primary_agent");
    expect(result.disconnected).toBe(true);
    expect(result.blueprint.spec.runtime?.agent?.tools).toEqual(["security_tool"]);
  });
});

describe("emitBlueprintYaml", () => {
  it("round-trips a v1alpha2 blueprint through YAML unchanged", () => {
    const yaml = emitBlueprintYaml(MINIMAL_BLUEPRINT);
    expect(parse(yaml)).toEqual(pruneEmpty(MINIMAL_BLUEPRINT));
  });

  it("drops nulls but keeps meaningful falsy values", () => {
    expect(pruneEmpty({ a: null, b: undefined, c: 0, d: false, e: "" })).toEqual({
      c: 0,
      d: false,
      e: "",
    });
  });

  it("emits v1alpha2 aliases without legacy execution fields", () => {
    const yaml = emitBlueprintYaml(WORKFLOW_BLUEPRINT);
    expect(yaml).toContain("apiVersion: studio.linch.dev/v1alpha2");
    expect(yaml).toContain("runtime:");
    expect(yaml).toContain("completion:");
    expect(yaml).toContain("routines:");
    expect(yaml).toContain("dependsOn:");
    expect(yaml).not.toContain("primaryAgent:");
    expect(yaml).not.toContain("loops:");
  });
});
