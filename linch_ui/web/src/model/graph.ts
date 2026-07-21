import type { Blueprint, CapabilityCatalog, StatusId, WorkflowNodeSpec } from "../api/types";

/** Semantic Blueprint concepts that can appear in a scoped Studio canvas. */
export type NodeKind =
  | "runtime_agent"
  | "subagent"
  | "directed_workflow"
  | "workflow_step"
  | "routine"
  | "trigger"
  | "tool"
  | "skill";

export type Glyph = "●" | "◉" | "□" | "◇" | "▤" | "◌";
export type RelationKind =
  | "depends_on"
  | "subagent_binding"
  | "tool_filter"
  | "allowed_tool"
  | "trigger_binding"
  | "routine_target";

export interface StudioNodeData extends Record<string, unknown> {
  kind: NodeKind;
  glyph: Glyph;
  title: string;
  sub: string;
  status: StatusId;
  isOutput: boolean;
  isParallel: boolean;
  pointer: string;
  workflowId?: string;
  sourceId?: string;
  routineKind?: "agent_tick" | "workflow_run";
  canAttachIn?: boolean;
  canAttachOut?: boolean;
  hasAttachmentIn?: boolean;
  hasAttachmentOut?: boolean;
}

export interface GraphNode {
  id: string;
  data: StudioNodeData;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  variant: "flow" | "attach";
  relation: RelationKind;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  outputs: string[];
}

export type CanvasScope =
  | { kind: "project" }
  | { kind: "agent" }
  | { kind: "workflow"; id: string }
  | { kind: "routine"; id: string };

/** Allowed relations this frontend knows how to persist into strict v1alpha2 fields. */
export const SUPPORTED_RELATION_IDS = new Set([
  "workflow_step_depends_on_workflow_step",
  "subagent_binds_workflow_step",
  "tool_filters_agent_loop",
  "tool_filters_subagent",
  "tool_filters_workflow_step",
  "tool_allows_skill",
  "trigger_invokes_routine",
  "agent_tick_routine_targets_agent_loop",
  "workflow_run_routine_targets_directed_workflow",
]);

export interface NodePosition {
  x: number;
  y: number;
}

/** Column order for auto-placing nodes the layout file has never seen. */
export const AUTO_LAYOUT_COLUMN: Record<string, number> = {
  trigger: 0,
  runtime_agent: 1,
  subagent: 1,
  tool: 0,
  skill: 0,
  directed_workflow: 2,
  workflow_step: 2,
  routine: 1,
};

function columnAutoPosition(kind: string, index: number): NodePosition {
  const column = AUTO_LAYOUT_COLUMN[kind] ?? 2;
  return { x: 40 + column * 240, y: 60 + index * 90 };
}

// A subagent's only attachment handle pair is bottom (source) → top (target),
// so an unpositioned subagent must render above its bound workflow step: any
// other relation between the two renders as a long inverted loop, or the two
// cards can overlap outright.
const SUBAGENT_GAP_Y = 100;

/**
 * The workflow-step node id a subagent binds to, if any. A step accepts at
 * most one subagent (`connectSemanticRelation` rejects a second with
 * `"max_one"`), so this is never ambiguous.
 */
export function boundWorkflowStepId(graph: Graph, subagentNodeId: string): string | undefined {
  return graph.edges.find(
    (edge) => edge.relation === "subagent_binding" && edge.source === subagentNodeId,
  )?.target;
}

/**
 * Position for a node the layout file has never seen. Most kinds column-stack
 * by declaration order, but a bound subagent renders relative to its workflow
 * step's already-resolved position instead, so the fixed bottom→top
 * attachment edge stays short and points the right way. `columnIndex` is a
 * running per-column counter the caller owns across one full node pass.
 */
export function fallbackNodePosition(
  graph: Graph,
  node: GraphNode,
  columnIndex: number,
  saved: Map<string, NodePosition>,
): NodePosition {
  if (node.data.kind === "subagent") {
    const targetId = boundWorkflowStepId(graph, node.id);
    const target = targetId ? saved.get(targetId) : undefined;
    if (target) return { x: target.x, y: target.y - SUBAGENT_GAP_Y };
  }
  return columnAutoPosition(node.data.kind, columnIndex);
}

/** Select the smallest honest graph for one editor scope. */
export function graphForScope(graph: Graph, scope: CanvasScope): Graph {
  const include = new Set<string>();
  if (scope.kind === "project") {
    for (const node of graph.nodes) {
      if (["runtime_agent", "directed_workflow", "routine"].includes(node.data.kind)) {
        include.add(node.id);
      }
    }
  } else if (scope.kind === "agent") {
    for (const node of graph.nodes) {
      if (["runtime_agent", "subagent", "tool", "skill"].includes(node.data.kind)) {
        include.add(node.id);
      }
    }
  } else if (scope.kind === "workflow") {
    const steps = graph.nodes.filter(
      (node) => node.data.kind === "workflow_step" && node.data.workflowId === scope.id,
    );
    steps.forEach((node) => include.add(node.id));
    // Unbound capabilities must remain visible so the first subagent/tool
    // attachment can be authored from this workflow canvas.
    for (const node of graph.nodes) {
      if (["subagent", "tool"].includes(node.data.kind)) {
        include.add(node.id);
      }
    }
  } else {
    const routine = graph.nodes.find(
      (node) => node.data.kind === "routine" && node.data.sourceId === scope.id,
    );
    if (routine) {
      include.add(routine.id);
      for (const node of graph.nodes) {
        if (
          node.data.kind === "trigger" ||
          (routine.data.routineKind === "agent_tick" && node.data.kind === "runtime_agent") ||
          (routine.data.routineKind === "workflow_run" && node.data.kind === "directed_workflow")
        ) {
          include.add(node.id);
        }
      }
    }
  }
  const nodes = graph.nodes.filter((node) => include.has(node.id));
  const edges = graph.edges.filter(
    (edge) => include.has(edge.source) && include.has(edge.target),
  );
  return {
    nodes,
    edges,
    outputs: graph.outputs.filter((id) => include.has(id)),
  };
}

export type ConnectionReason =
  | "not_found"
  | "incompatible"
  | "cross_workflow"
  | "same_node"
  | "duplicate"
  | "cycle"
  | "max_one"
  | "required_by_child";

export interface ConnectionResult {
  blueprint: Blueprint;
  connected: boolean;
  reason?: ConnectionReason;
}

export interface DisconnectionResult {
  blueprint: Blueprint;
  disconnected: boolean;
  reason?: ConnectionReason;
}

/** Canvas ids remain valid layout keys and preserve the v1alpha1 migration layout. */
export function toLayoutId(...parts: string[]): string {
  const joined = parts
    .join("_")
    .toLowerCase()
    .replace(/[^a-z0-9_]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  return /^[a-z]/.test(joined) ? joined : `n_${joined}`;
}

const PRESET_GLYPH: Record<string, Glyph> = {
  standard_agent: "●",
  deep_agent: "◉",
  coordinator: "◉",
};

const PRESET_LABEL: Record<string, string> = {
  standard_agent: "standard agent loop",
  deep_agent: "deep agent · model-directed",
  coordinator: "coordinator · delegates",
};

export function blueprintToGraph(blueprint: Blueprint): Graph {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const outputs: string[] = [];
  const spec = blueprint.spec;
  const toolIds = new Map<string, string>();
  const subagentIds = new Map<string, string>();
  const skillIds = new Map<string, string>();
  const triggerIds = new Map<string, string>();
  const workflowIds = new Map<string, string>();
  const stepIds = new Map<string, Map<string, string>>();
  const routineIds = new Map<string, string>();

  const push = (id: string, data: StudioNodeData) => {
    nodes.push({ id, data });
    return id;
  };

  const runtime = spec.runtime?.agent;
  let runtimeId: string | undefined;
  if (runtime) {
    const preset = runtime.preset ?? "standard_agent";
    const completion = runtime.completion?.mode ?? "agent_judged";
    // Keep the legacy primary_agent layout key across in-memory migration.
    runtimeId = push(toLayoutId("primary_agent"), {
      kind: "runtime_agent",
      glyph: PRESET_GLYPH[preset] ?? "●",
      title: blueprint.metadata.name,
      sub: `${PRESET_LABEL[preset] ?? preset} · ${completion}`,
      status: "runtime_ready",
      isOutput: false,
      isParallel: false,
      pointer: "/spec/runtime/agent",
      sourceId: "runtime_agent",
    });
  }

  (spec.tools ?? []).forEach((tool, index) => {
    const kind = tool.kind ?? "function";
    const id = push(toLayoutId("tool", tool.id), {
      kind: "tool",
      glyph: "◇",
      title: tool.displayName || tool.id,
      sub: `${kind} tool`,
      status: "skeleton_todo",
      isOutput: false,
      isParallel: false,
      pointer: `/spec/tools/${index}`,
      sourceId: tool.id,
    });
    toolIds.set(tool.id, id);
  });

  if (runtimeId) {
    const primaryTools = runtime?.tools ?? (spec.tools ?? []).map((tool) => tool.id);
    for (const tool of primaryTools) {
      const source = toolIds.get(tool);
      if (source) addEdge(edges, source, runtimeId, "attach", "tool_filter");
    }
  }

  (spec.subagents ?? []).forEach((subagent, index) => {
    const id = push(toLayoutId("subagent", subagent.id), {
      kind: "subagent",
      glyph: "●",
      title: subagent.displayName || subagent.id,
      sub: "subagent · inherits runtime limits",
      status: "runtime_ready",
      isOutput: false,
      isParallel: false,
      pointer: `/spec/subagents/${index}`,
      sourceId: subagent.id,
    });
    subagentIds.set(subagent.id, id);
  });

  (spec.skills ?? []).forEach((skill, index) => {
    const id = push(toLayoutId("skill", skill.id), {
      kind: "skill",
      glyph: "▤",
      title: skill.displayName || skill.id,
      sub: "skill",
      status: "runtime_ready",
      isOutput: false,
      isParallel: false,
      pointer: `/spec/skills/${index}`,
      sourceId: skill.id,
    });
    skillIds.set(skill.id, id);
  });

  (spec.triggers ?? []).forEach((trigger, index) => {
    const id = push(toLayoutId("trigger", trigger.id), {
      kind: "trigger",
      glyph: "◌",
      title: trigger.displayName || trigger.id,
      sub: `${trigger.kind} trigger`,
      status: trigger.kind === "webhook" ? "skeleton_todo" : "runtime_ready",
      isOutput: false,
      isParallel: false,
      pointer: `/spec/triggers/${index}`,
      sourceId: trigger.id,
    });
    triggerIds.set(trigger.id, id);
  });

  (spec.workflows ?? []).forEach((workflow, workflowIndex) => {
    const workflowCanvasId = push(toLayoutId("workflow", workflow.id), {
      kind: "directed_workflow",
      glyph: "□",
      title: workflow.displayName || workflow.id,
      sub: "directed · replayable",
      status: "runtime_ready",
      isOutput: false,
      isParallel: false,
      pointer: `/spec/workflows/${workflowIndex}`,
      sourceId: workflow.id,
      workflowId: workflow.id,
    });
    workflowIds.set(workflow.id, workflowCanvasId);

    const localIds = new Map<string, string>();
    const depth = new Map<string, number>();
    for (const node of workflow.nodes ?? []) {
      localIds.set(node.id, toLayoutId("wf", workflow.id, node.id));
    }
    stepIds.set(workflow.id, localIds);

    const depthOf = (nodeId: string, seen = new Set<string>()): number => {
      if (depth.has(nodeId)) return depth.get(nodeId)!;
      if (seen.has(nodeId)) return 0;
      const branch = new Set(seen);
      branch.add(nodeId);
      const node = (workflow.nodes ?? []).find((item) => item.id === nodeId);
      const dependencies = node?.dependsOn ?? [];
      const value =
        dependencies.length === 0
          ? 0
          : Math.max(...dependencies.map((dependency) => depthOf(dependency, branch) + 1));
      depth.set(nodeId, value);
      return value;
    };

    (workflow.nodes ?? []).forEach((node, nodeIndex) => {
      const canvasId = localIds.get(node.id)!;
      const directTool = node.type === "direct_tool";
      const level = depthOf(node.id);
      const sameLevel = (workflow.nodes ?? []).filter(
        (other) => other.id !== node.id && depthOf(other.id) === level,
      );
      push(canvasId, {
        kind: "workflow_step",
        glyph: directTool ? "◇" : "□",
        title: node.label || node.id,
        sub: directTool ? "direct tool step · unsupported" : "agent-call step",
        status: directTool ? "unsupported" : "runtime_ready",
        isOutput: workflow.output === node.id,
        isParallel: sameLevel.length > 0,
        pointer: `/spec/workflows/${workflowIndex}/nodes/${nodeIndex}`,
        workflowId: workflow.id,
        sourceId: node.id,
      });

      for (const dependency of node.dependsOn ?? []) {
        const source = localIds.get(dependency);
        if (source) addEdge(edges, source, canvasId, "flow", "depends_on");
      }
      if (node.type === "agent_call") {
        if (node.subagent) {
          const source = subagentIds.get(node.subagent);
          if (source) addEdge(edges, source, canvasId, "attach", "subagent_binding");
        }
        for (const tool of node.tools ?? []) {
          const source = toolIds.get(tool);
          if (source) addEdge(edges, source, canvasId, "attach", "tool_filter");
        }
      }
    });

    if (workflow.output) {
      const outputId = localIds.get(workflow.output);
      if (outputId) outputs.push(outputId);
    }
  });

  for (const subagent of spec.subagents ?? []) {
    const target = subagentIds.get(subagent.id);
    if (!target) continue;
    for (const tool of subagent.tools ?? []) {
      const source = toolIds.get(tool);
      if (source) addEdge(edges, source, target, "attach", "tool_filter");
    }
  }

  for (const skill of spec.skills ?? []) {
    const target = skillIds.get(skill.id);
    if (!target) continue;
    for (const tool of skill.allowedTools ?? []) {
      const source = toolIds.get(tool);
      if (source) addEdge(edges, source, target, "attach", "allowed_tool");
    }
  }

  (spec.routines ?? []).forEach((routine, index) => {
    // Keep the legacy loop_<id> layout key so migrated canvases do not jump.
    const id = push(toLayoutId("loop", routine.id), {
      kind: "routine",
      glyph: "□",
      title: routine.displayName || routine.id,
      sub: routine.kind === "agent_tick" ? "agent tick routine" : "workflow run routine",
      status: "runtime_ready",
      isOutput: false,
      isParallel: false,
      pointer: `/spec/routines/${index}`,
      sourceId: routine.id,
      routineKind: routine.kind,
    });
    routineIds.set(routine.id, id);
  });

  for (const routine of spec.routines ?? []) {
    const routineId = routineIds.get(routine.id);
    if (!routineId) continue;
    for (const trigger of routine.triggers ?? []) {
      const source = triggerIds.get(trigger);
      if (source) addEdge(edges, source, routineId, "attach", "trigger_binding");
    }
    const target =
      routine.kind === "agent_tick" ? runtimeId : workflowIds.get(routine.target);
    if (target) addEdge(edges, routineId, target, "attach", "routine_target");
  }

  const known = new Set(nodes.map((node) => node.id));
  const liveEdges = edges.filter((edge) => known.has(edge.source) && known.has(edge.target));

  // Structural fact, independent of whether a *new* attachment is currently
  // eligible: an edge already anchors here, so the handle must stay visible
  // and measurable even when the node is otherwise at capacity (e.g. a
  // workflow step with its one allowed subagent already bound).
  const attachTargets = new Set(
    liveEdges.filter((edge) => edge.variant === "attach").map((edge) => edge.target),
  );
  const attachSources = new Set(
    liveEdges.filter((edge) => edge.variant === "attach").map((edge) => edge.source),
  );
  for (const node of nodes) {
    node.data.hasAttachmentIn = attachTargets.has(node.id);
    node.data.hasAttachmentOut = attachSources.has(node.id);
  }

  return {
    nodes,
    edges: liveEdges,
    outputs,
  };
}

/**
 * Every name the relation matrix may use for one card's endpoint.
 *
 * A routine answers to two: the matrix targets it generically as `routine` when a
 * trigger invokes it, but names it by kind when the routine itself is the source.
 */
function relationEndpointKinds(node: GraphNode): string[] {
  if (node.data.kind === "runtime_agent") return ["agent_loop"];
  if (node.data.kind === "routine") {
    return [
      node.data.routineKind === "agent_tick" ? "agent_tick_routine" : "workflow_run_routine",
      "routine",
    ];
  }
  return [node.data.kind];
}

/** Fail closed when the backend catalog does not advertise a relation the UI can persist. */
export function catalogAllowsConnection(
  catalog: CapabilityCatalog | null,
  blueprint: Blueprint,
  sourceCanvasId: string,
  targetCanvasId: string,
): boolean {
  const graph = blueprintToGraph(blueprint);
  const source = graph.nodes.find((node) => node.id === sourceCanvasId);
  const target = graph.nodes.find((node) => node.id === targetCanvasId);
  if (!source || !target || !catalog?.relationMatrix) return false;
  const sourceKinds = relationEndpointKinds(source);
  const targetKinds = relationEndpointKinds(target);
  return catalog.relationMatrix.relations.some(
    (relation) =>
      relation.decision === "allow" &&
      SUPPORTED_RELATION_IDS.has(relation.id) &&
      sourceKinds.includes(relation.sourceKind) &&
      targetKinds.includes(relation.targetKind),
  );
}

function addEdge(
  edges: GraphEdge[],
  source: string,
  target: string,
  variant: GraphEdge["variant"],
  relation: RelationKind,
): void {
  edges.push({ id: `${source}__${target}__${relation}`, source, target, variant, relation });
}

/** Apply one catalog-supported semantic relation without mutating the input Blueprint. */
export function connectSemanticRelation(
  blueprint: Blueprint,
  sourceCanvasId: string,
  targetCanvasId: string,
): ConnectionResult {
  if (sourceCanvasId === targetCanvasId) {
    return { blueprint, connected: false, reason: "same_node" };
  }
  const graph = blueprintToGraph(blueprint);
  const source = graph.nodes.find((node) => node.id === sourceCanvasId);
  const target = graph.nodes.find((node) => node.id === targetCanvasId);
  if (!source || !target) return { blueprint, connected: false, reason: "not_found" };

  if (source.data.kind === "workflow_step" && target.data.kind === "workflow_step") {
    return connectWorkflowDependency(blueprint, source, target);
  }
  if (source.data.kind === "tool") {
    return connectToolFilter(blueprint, source, target);
  }
  if (source.data.kind === "subagent" && target.data.kind === "workflow_step") {
    return connectSubagentBinding(blueprint, source, target);
  }
  if (source.data.kind === "trigger" && target.data.kind === "routine") {
    return connectTriggerBinding(blueprint, source, target);
  }
  if (source.data.kind === "routine") {
    return connectRoutineTarget(blueprint, source, target);
  }
  return { blueprint, connected: false, reason: "incompatible" };
}

/** Compatibility wrapper retained until canvas components switch to semantic relations. */
export function connectWorkflowNodes(
  blueprint: Blueprint,
  sourceCanvasId: string,
  targetCanvasId: string,
): ConnectionResult {
  return connectSemanticRelation(blueprint, sourceCanvasId, targetCanvasId);
}

/** Remove an optional semantic relation; required routine targets cannot be detached. */
export function disconnectSemanticRelation(
  blueprint: Blueprint,
  sourceCanvasId: string,
  targetCanvasId: string,
): DisconnectionResult {
  const graph = blueprintToGraph(blueprint);
  const source = graph.nodes.find((node) => node.id === sourceCanvasId);
  const target = graph.nodes.find((node) => node.id === targetCanvasId);
  if (!source || !target) return { blueprint, disconnected: false, reason: "not_found" };
  if (source.data.kind === "routine") {
    return { blueprint, disconnected: false, reason: "incompatible" };
  }
  if (source.data.kind === "workflow_step" && target.data.kind === "workflow_step") {
    if (source.data.workflowId !== target.data.workflowId) {
      return { blueprint, disconnected: false, reason: "cross_workflow" };
    }
    const next = structuredClone(blueprint) as Blueprint;
    const workflow = next.spec.workflows?.find((item) => item.id === source.data.workflowId);
    const node = workflow?.nodes?.find((item) => item.id === target.data.sourceId);
    if (node?.type !== "agent_call" || !source.data.sourceId) {
      return { blueprint, disconnected: false, reason: "incompatible" };
    }
    if (!(node.dependsOn ?? []).includes(source.data.sourceId)) {
      return { blueprint, disconnected: false, reason: "not_found" };
    }
    node.dependsOn = (node.dependsOn ?? []).filter((item) => item !== source.data.sourceId);
    return { blueprint: next, disconnected: true };
  }
  if (source.data.kind === "tool" && source.data.sourceId) {
    const next = structuredClone(blueprint) as Blueprint;
    let values: string[] | undefined;
    let assign: ((items: string[]) => void) | undefined;
    if (target.data.kind === "runtime_agent") {
      const agent = next.spec.runtime?.agent;
      if (!agent) return { blueprint, disconnected: false, reason: "not_found" };
      const usedByChild =
        (next.spec.subagents ?? []).some((item) =>
          (item.tools ?? []).includes(source.data.sourceId!),
        ) ||
        (next.spec.skills ?? []).some((item) =>
          (item.allowedTools ?? []).includes(source.data.sourceId!),
        ) ||
        (next.spec.workflows ?? []).some((workflow) =>
          (workflow.nodes ?? []).some(
            (node) =>
              node.type === "agent_call" &&
              (node.tools ?? []).includes(source.data.sourceId!),
          ),
        );
      if (usedByChild) {
        return { blueprint, disconnected: false, reason: "required_by_child" };
      }
      values = agent.tools ?? (next.spec.tools ?? []).map((tool) => tool.id);
      assign = (items) => {
        agent.tools = items;
      };
    } else if (target.data.kind === "subagent") {
      const item = next.spec.subagents?.find((subagent) => subagent.id === target.data.sourceId);
      if (item) {
        values = item.tools ?? [];
        assign = (items) => {
          item.tools = items;
        };
      }
    } else if (target.data.kind === "workflow_step") {
      const workflow = next.spec.workflows?.find((item) => item.id === target.data.workflowId);
      const item = workflow?.nodes?.find((node) => node.id === target.data.sourceId);
      if (item?.type === "agent_call") {
        values = item.tools ?? [];
        assign = (items) => {
          item.tools = items;
        };
      }
    } else if (target.data.kind === "skill") {
      const item = next.spec.skills?.find((skill) => skill.id === target.data.sourceId);
      if (item) {
        values = item.allowedTools ?? [];
        assign = (items) => {
          item.allowedTools = items;
        };
      }
    }
    if (!values || !assign) {
      return { blueprint, disconnected: false, reason: "incompatible" };
    }
    if (!values.includes(source.data.sourceId)) {
      return { blueprint, disconnected: false, reason: "not_found" };
    }
    assign(values.filter((item) => item !== source.data.sourceId));
    return { blueprint: next, disconnected: true };
  }
  if (source.data.kind === "subagent" && target.data.kind === "workflow_step") {
    const next = structuredClone(blueprint) as Blueprint;
    const workflow = next.spec.workflows?.find((item) => item.id === target.data.workflowId);
    const node = workflow?.nodes?.find((item) => item.id === target.data.sourceId);
    if (node?.type !== "agent_call" || node.subagent !== source.data.sourceId) {
      return { blueprint, disconnected: false, reason: "not_found" };
    }
    delete node.subagent;
    return { blueprint: next, disconnected: true };
  }
  if (source.data.kind === "trigger" && target.data.kind === "routine") {
    const next = structuredClone(blueprint) as Blueprint;
    const routine = next.spec.routines?.find((item) => item.id === target.data.sourceId);
    if (!routine || !source.data.sourceId || !(routine.triggers ?? []).includes(source.data.sourceId)) {
      return { blueprint, disconnected: false, reason: "not_found" };
    }
    routine.triggers = (routine.triggers ?? []).filter((item) => item !== source.data.sourceId);
    return { blueprint: next, disconnected: true };
  }
  return { blueprint, disconnected: false, reason: "incompatible" };
}

function connectWorkflowDependency(
  blueprint: Blueprint,
  source: GraphNode,
  target: GraphNode,
): ConnectionResult {
  if (source.data.workflowId !== target.data.workflowId) {
    return { blueprint, connected: false, reason: "cross_workflow" };
  }
  const workflowIndex = (blueprint.spec.workflows ?? []).findIndex(
    (workflow) => workflow.id === source.data.workflowId,
  );
  const workflow = blueprint.spec.workflows?.[workflowIndex];
  const sourceNode = workflow?.nodes?.find((node) => node.id === source.data.sourceId);
  const targetNode = workflow?.nodes?.find((node) => node.id === target.data.sourceId);
  if (!workflow || sourceNode?.type !== "agent_call" || targetNode?.type !== "agent_call") {
    return { blueprint, connected: false, reason: "incompatible" };
  }
  if (workflow.output === sourceNode.id) {
    return { blueprint, connected: false, reason: "incompatible" };
  }
  if ((targetNode.dependsOn ?? []).includes(sourceNode.id)) {
    return { blueprint, connected: false, reason: "duplicate" };
  }
  if (dependsOn(workflow.nodes ?? [], sourceNode.id, targetNode.id)) {
    return { blueprint, connected: false, reason: "cycle" };
  }
  const next = structuredClone(blueprint) as Blueprint;
  const nextTarget = next.spec.workflows?.[workflowIndex]?.nodes?.find(
    (node) => node.id === targetNode.id,
  );
  if (nextTarget?.type !== "agent_call") {
    return { blueprint, connected: false, reason: "not_found" };
  }
  nextTarget.dependsOn = [...(nextTarget.dependsOn ?? []), sourceNode.id];
  return { blueprint: next, connected: true };
}

function connectToolFilter(
  blueprint: Blueprint,
  source: GraphNode,
  target: GraphNode,
): ConnectionResult {
  const toolId = source.data.sourceId;
  if (!toolId) return { blueprint, connected: false, reason: "not_found" };
  const next = structuredClone(blueprint) as Blueprint;
  let values: string[] | undefined;
  let assign: ((items: string[]) => void) | undefined;
  if (target.data.kind === "runtime_agent") {
    const agent = next.spec.runtime?.agent;
    // null/omitted already exposes every declared tool, so the visual edge is
    // derived and a second attachment is a duplicate.
    if (!agent || agent.tools === null || agent.tools === undefined) {
      return { blueprint, connected: false, reason: "duplicate" };
    }
    values = agent.tools;
    assign = (items) => {
      agent.tools = items;
    };
  } else if (target.data.kind === "subagent") {
    const item = next.spec.subagents?.find((subagent) => subagent.id === target.data.sourceId);
    if (item) {
      values = item.tools ?? [];
      assign = (items) => {
        item.tools = items;
      };
    }
  } else if (target.data.kind === "workflow_step") {
    const workflow = next.spec.workflows?.find((item) => item.id === target.data.workflowId);
    const item = workflow?.nodes?.find((node) => node.id === target.data.sourceId);
    if (item?.type === "agent_call") {
      values = item.tools ?? [];
      assign = (items) => {
        item.tools = items;
      };
    }
  } else if (target.data.kind === "skill") {
    const item = next.spec.skills?.find((skill) => skill.id === target.data.sourceId);
    if (item) {
      values = item.allowedTools ?? [];
      assign = (items) => {
        item.allowedTools = items;
      };
    }
  }
  if (!values || !assign) return { blueprint, connected: false, reason: "incompatible" };
  if (values.includes(toolId)) return { blueprint, connected: false, reason: "duplicate" };
  assign([...values, toolId]);
  // Child and workflow filters can only narrow the primary registry. Keep an
  // explicit primary pool valid when a tool is attached further downstream.
  const primaryAgent = next.spec.runtime?.agent;
  const primaryTools = primaryAgent?.tools;
  if (
    target.data.kind !== "runtime_agent" &&
    primaryAgent &&
    primaryTools !== null &&
    primaryTools !== undefined &&
    !primaryTools.includes(toolId)
  ) {
    primaryAgent.tools = [...primaryTools, toolId];
  }
  return { blueprint: next, connected: true };
}

function connectSubagentBinding(
  blueprint: Blueprint,
  source: GraphNode,
  target: GraphNode,
): ConnectionResult {
  const workflowIndex = (blueprint.spec.workflows ?? []).findIndex(
    (workflow) => workflow.id === target.data.workflowId,
  );
  const targetNode = blueprint.spec.workflows?.[workflowIndex]?.nodes?.find(
    (node) => node.id === target.data.sourceId,
  );
  if (targetNode?.type !== "agent_call" || !source.data.sourceId) {
    return { blueprint, connected: false, reason: "incompatible" };
  }
  if (targetNode.subagent === source.data.sourceId) {
    return { blueprint, connected: false, reason: "duplicate" };
  }
  if (targetNode.subagent) return { blueprint, connected: false, reason: "max_one" };
  const next = structuredClone(blueprint) as Blueprint;
  const nextNode = next.spec.workflows?.[workflowIndex]?.nodes?.find(
    (node) => node.id === targetNode.id,
  );
  if (nextNode?.type !== "agent_call") {
    return { blueprint, connected: false, reason: "not_found" };
  }
  nextNode.subagent = source.data.sourceId;
  return { blueprint: next, connected: true };
}

function connectTriggerBinding(
  blueprint: Blueprint,
  source: GraphNode,
  target: GraphNode,
): ConnectionResult {
  const routineIndex = (blueprint.spec.routines ?? []).findIndex(
    (routine) => routine.id === target.data.sourceId,
  );
  const routine = blueprint.spec.routines?.[routineIndex];
  if (!routine || !source.data.sourceId) {
    return { blueprint, connected: false, reason: "not_found" };
  }
  if ((routine.triggers ?? []).includes(source.data.sourceId)) {
    return { blueprint, connected: false, reason: "duplicate" };
  }
  const next = structuredClone(blueprint) as Blueprint;
  const nextRoutine = next.spec.routines?.[routineIndex];
  if (!nextRoutine) return { blueprint, connected: false, reason: "not_found" };
  nextRoutine.triggers = [...(nextRoutine.triggers ?? []), source.data.sourceId];
  return { blueprint: next, connected: true };
}

function connectRoutineTarget(
  blueprint: Blueprint,
  source: GraphNode,
  target: GraphNode,
): ConnectionResult {
  const routineIndex = (blueprint.spec.routines ?? []).findIndex(
    (routine) => routine.id === source.data.sourceId,
  );
  const routine = blueprint.spec.routines?.[routineIndex];
  if (!routine) return { blueprint, connected: false, reason: "not_found" };
  if (routine.kind === "agent_tick") {
    if (target.data.kind !== "runtime_agent") {
      return { blueprint, connected: false, reason: "incompatible" };
    }
    return { blueprint, connected: false, reason: "duplicate" };
  }
  if (target.data.kind !== "directed_workflow" || !target.data.sourceId) {
    return { blueprint, connected: false, reason: "incompatible" };
  }
  if (routine.target === target.data.sourceId) {
    return { blueprint, connected: false, reason: "duplicate" };
  }
  const next = structuredClone(blueprint) as Blueprint;
  const nextRoutine = next.spec.routines?.[routineIndex];
  if (!nextRoutine || nextRoutine.kind !== "workflow_run") {
    return { blueprint, connected: false, reason: "not_found" };
  }
  nextRoutine.target = target.data.sourceId;
  return { blueprint: next, connected: true };
}

/** Return whether `nodeId` directly or transitively depends on `needle`. */
function dependsOn(
  nodes: WorkflowNodeSpec[],
  nodeId: string,
  needle: string,
  visited = new Set<string>(),
): boolean {
  if (nodeId === needle) return true;
  if (visited.has(nodeId)) return false;
  visited.add(nodeId);
  const node = nodes.find((item) => item.id === nodeId);
  if (!node || node.type !== "agent_call") return false;
  return (node.dependsOn ?? []).some((dependency) =>
    dependsOn(nodes, dependency, needle, visited),
  );
}
