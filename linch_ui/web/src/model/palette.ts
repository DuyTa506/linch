import type { Blueprint, CapabilityCatalog, StatusId } from "../api/types";
import type { Glyph } from "./graph";

export interface PaletteItem {
  id: string;
  category: string;
  glyph: Glyph;
  name: string;
  tip: string;
  status: StatusId;
  capabilityId: string;
  creatable: boolean;
}

export type StudioTemplateId =
  | "agent"
  | "goal_verified"
  | "directed_workflow"
  | "coordinator"
  | "routine";

/** The five canonical choices shared by CLI, TUI, web, and AI authoring. */
export const SHARED_TEMPLATE_CHOICES: ReadonlyArray<{
  id: StudioTemplateId;
  label: string;
  capabilityId: string;
}> = [
  { id: "agent", label: "Agent", capabilityId: "execution.standard_agent" },
  { id: "goal_verified", label: "Goal-verified", capabilityId: "execution.goal_verified" },
  {
    id: "directed_workflow",
    label: "Directed workflow",
    capabilityId: "execution.directed_workflow",
  },
  { id: "coordinator", label: "Coordinator", capabilityId: "execution.coordinator" },
  { id: "routine", label: "Routine", capabilityId: "execution.routine" },
];

const ITEMS: Omit<PaletteItem, "status" | "creatable">[] = [
  {
    id: "subagent",
    category: "AGENT LOOP",
    glyph: "●",
    name: "subagent",
    tip: "inherits runtime turn and budget limits",
    capabilityId: "execution.standard_agent",
  },
  {
    id: "directed_workflow",
    category: "WORKFLOWS",
    glyph: "□",
    name: "directed workflow",
    tip: "explicit replayable workflow container",
    capabilityId: "execution.directed_workflow",
  },
  {
    id: "agent_call",
    category: "WORKFLOWS",
    glyph: "□",
    name: "agent-call step",
    tip: "add inside the selected directed workflow",
    capabilityId: "execution.directed_workflow",
  },
  {
    id: "agent_tick_routine",
    category: "ROUTINES",
    glyph: "□",
    name: "agent tick routine",
    tip: "one bounded LoopRunner tick; host owns lifetime",
    capabilityId: "execution.routine",
  },
  {
    id: "workflow_run_routine",
    category: "ROUTINES",
    glyph: "□",
    name: "workflow run routine",
    tip: "invoke the selected directed workflow",
    capabilityId: "execution.routine",
  },
  {
    id: "scheduled_workflow",
    category: "ROUTINES",
    glyph: "□",
    name: "scheduled workflow",
    tip: "create a workflow-run routine with a host-owned 2-hour cron trigger",
    capabilityId: "execution.routine",
  },
  {
    id: "function_tool",
    category: "TOOLS",
    glyph: "◇",
    name: "function tool",
    tip: "typed function tool",
    capabilityId: "tools.function_skeleton",
  },
  {
    id: "database_tool",
    category: "TOOLS",
    glyph: "◇",
    name: "database tool",
    tip: "your implementation — emits a TODO seam",
    capabilityId: "tools.database_implementation",
  },
  {
    id: "skill",
    category: "AGENT LOOP",
    glyph: "▤",
    name: "skill",
    tip: "instructions with an explicit tool allowlist",
    capabilityId: "extensions.skill_files",
  },
  {
    id: "cron_trigger",
    category: "TRIGGERS",
    glyph: "◌",
    name: "cron trigger",
    tip: "host-owned cron delivery",
    capabilityId: "execution.routine",
  },
  {
    id: "manual_trigger",
    category: "TRIGGERS",
    glyph: "◌",
    name: "manual trigger",
    tip: "host-owned one-shot invocation",
    capabilityId: "execution.standard_agent",
  },
  {
    id: "ci_trigger",
    category: "TRIGGERS",
    glyph: "◌",
    name: "CI trigger",
    tip: "host-owned CI delivery envelope",
    capabilityId: "execution.routine",
  },
  {
    id: "webhook_trigger",
    category: "TRIGGERS",
    glyph: "◌",
    name: "webhook trigger",
    tip: "emits a blocking signature-verification TODO",
    capabilityId: "execution.routine",
  },
];

export function buildPalette(catalog: CapabilityCatalog | null): PaletteItem[] {
  if (!catalog) return [];
  const byId = new Map(catalog.capabilities.map((record) => [record.id, record]));
  const items: PaletteItem[] = [];
  for (const item of ITEMS) {
    const record = byId.get(item.capabilityId);
    if (!record) continue;
    items.push({
      ...item,
      status: record.status,
      creatable: record.status !== "unsupported",
    });
  }
  return items;
}

export function unsupportedItems(catalog: CapabilityCatalog | null) {
  if (!catalog) return [];
  return catalog.capabilities.filter((record) => record.status === "unsupported");
}

export function groupPalette(items: PaletteItem[]): [string, PaletteItem[]][] {
  const groups = new Map<string, PaletteItem[]>();
  for (const item of items) {
    const bucket = groups.get(item.category) ?? [];
    bucket.push(item);
    groups.set(item.category, bucket);
  }
  return [...groups.entries()];
}

function uniqueId(base: string, taken: Set<string>): string {
  const clean =
    base
      .toLowerCase()
      .replace(/[^a-z0-9_]/g, "_")
      .replace(/^_+|_+$/g, "") || "node";
  const seed = /^[a-z]/.test(clean) ? clean : `n_${clean}`;
  if (!taken.has(seed)) return seed;
  for (let index = 2; ; index += 1) {
    const candidate = `${seed}_${index}`;
    if (!taken.has(candidate)) return candidate;
  }
}

function collectIds(blueprint: Blueprint): Set<string> {
  const spec = blueprint.spec;
  return new Set<string>([
    ...(spec.subagents ?? []).map((item) => item.id),
    ...(spec.skills ?? []).map((item) => item.id),
    ...(spec.tools ?? []).map((item) => item.id),
    ...(spec.triggers ?? []).map((item) => item.id),
    ...(spec.routines ?? []).map((item) => item.id),
    ...(spec.workflows ?? []).flatMap((workflow) => [
      workflow.id,
      ...(workflow.nodes ?? []).map((node) => node.id),
    ]),
  ]);
}

export interface PaletteTarget {
  /** Required when adding an agent-call step or workflow-run routine. */
  workflowId?: string;
}

/** Palette items that declare a callable tool under `/spec/tools`. */
const TOOL_ITEM_IDS = new Set(["function_tool", "database_tool"]);

/** i18n key + toast marker naming what a freshly added card is, and is not, wired to. */
export interface DeclaredHint {
  key: string;
  marker: "[ok]" | "[!!]";
}

/**
 * Explain what adding a palette item did — declaring a component and attaching it
 * are separate steps, and only some items attach themselves.
 *
 * Args:
 *     itemId: Palette item that was just added
 *     context: Whether a directed-workflow scope was open, and (for tools) whether
 *         the new card already sits in the primary runtime pool
 *
 * Returns:
 *     The hint to toast, or null when the item has no attachment story to tell
 */
export function declaredHint(
  itemId: string,
  context: { inWorkflow: boolean; attached: boolean },
): DeclaredHint | null {
  if (TOOL_ITEM_IDS.has(itemId)) {
    // A null `runtime.agent.tools` already grants every declared tool, so an
    // "attach it yourself" nudge would be false.
    return context.attached
      ? { key: "toolAttachedHint", marker: "[ok]" }
      : { key: "toolDeclaredHint", marker: "[!!]" };
  }
  if (itemId === "subagent") {
    return context.inWorkflow
      ? { key: "subagentBoundHint", marker: "[ok]" }
      : { key: "subagentDeclaredHint", marker: "[!!]" };
  }
  if (itemId === "skill") return { key: "skillDeclaredHint", marker: "[ok]" };
  if (itemId === "scheduled_workflow") return { key: "scheduledDeclaredHint", marker: "[ok]" };
  return null;
}

/** Add a real v1alpha2 component without guessing an implicit workflow scope. */
export function addPaletteItem(
  blueprint: Blueprint,
  item: PaletteItem,
  target: PaletteTarget = {},
): { blueprint: Blueprint; added: string | null; reason?: string } {
  if (!item.creatable) {
    return { blueprint, added: null, reason: "unsupported" };
  }
  const next = structuredClone(blueprint) as Blueprint;
  const spec = next.spec;
  const taken = collectIds(next);

  switch (item.id) {
    case "subagent": {
      const id = uniqueId("subagent", taken);
      spec.subagents = [
        ...(spec.subagents ?? []),
        {
          id,
          displayName: id.replace(/_/g, "-"),
          instructions: "TODO: describe this subagent.",
          tools: [],
        },
      ];
      if (target.workflowId) {
        const workflow = spec.workflows?.find((item) => item.id === target.workflowId);
        if (!workflow) return { blueprint, added: null, reason: "workflow_not_found" };
        const stepId = uniqueId(`${id}_step`, new Set([...taken, id]));
        workflow.nodes = [
          ...(workflow.nodes ?? []),
          {
            type: "agent_call",
            id: stepId,
            label: id.replace(/_/g, "-"),
            prompt: `Delegate this workflow step to ${id}.`,
            subagent: id,
            tools: [],
          },
        ];
        workflow.output = stepId;
      }
      return { blueprint: next, added: id };
    }
    case "directed_workflow": {
      const id = uniqueId("workflow", taken);
      spec.workflows = [
        ...(spec.workflows ?? []),
        {
          kind: "directed",
          id,
          displayName: id.replace(/_/g, "-"),
          nodes: [],
          maxConcurrency: 4,
        },
      ];
      return { blueprint: next, added: id };
    }
    case "agent_call": {
      if (!target.workflowId) {
        return { blueprint, added: null, reason: "workflow_required" };
      }
      const workflow = spec.workflows?.find((item) => item.id === target.workflowId);
      if (!workflow) return { blueprint, added: null, reason: "workflow_not_found" };
      const id = uniqueId("step", taken);
      workflow.nodes = [
        ...(workflow.nodes ?? []),
        {
          type: "agent_call",
          id,
          label: id.replace(/_/g, "-"),
          prompt: "TODO: describe this step.",
          tools: [],
        },
      ];
      // The newest step becomes the terminal so the previous terminal can be
      // connected forward immediately while authoring an A2A chain.
      workflow.output = id;
      return { blueprint: next, added: id };
    }
    case "agent_tick_routine": {
      const id = uniqueId("routine", taken);
      spec.routines = [
        ...(spec.routines ?? []),
        {
          kind: "agent_tick",
          id,
          displayName: id.replace(/_/g, "-"),
          charter: "TODO: describe this routine.",
          prompt: "TODO: describe the bounded tick.",
          target: "runtime_agent",
          triggers: [],
          maxTurns: 8,
          budget: { maxTokens: 20_000 },
        },
      ];
      return { blueprint: next, added: id };
    }
    case "workflow_run_routine": {
      if (!target.workflowId) {
        return { blueprint, added: null, reason: "workflow_required" };
      }
      if (!(spec.workflows ?? []).some((workflow) => workflow.id === target.workflowId)) {
        return { blueprint, added: null, reason: "workflow_not_found" };
      }
      const id = uniqueId("workflow_routine", taken);
      spec.routines = [
        ...(spec.routines ?? []),
        {
          kind: "workflow_run",
          id,
          displayName: id.replace(/_/g, "-"),
          target: target.workflowId,
          triggers: [],
          maxTurns: 8,
          budget: { maxTokens: 20_000 },
        },
      ];
      return { blueprint: next, added: id };
    }
    case "scheduled_workflow": {
      if (!target.workflowId) {
        return { blueprint, added: null, reason: "workflow_required" };
      }
      if (!(spec.workflows ?? []).some((workflow) => workflow.id === target.workflowId)) {
        return { blueprint, added: null, reason: "workflow_not_found" };
      }
      const routineId = uniqueId("scheduled_workflow", taken);
      const triggerId = uniqueId("every_2h", new Set([...taken, routineId]));
      spec.triggers = [
        ...(spec.triggers ?? []),
        {
          kind: "cron",
          id: triggerId,
          displayName: "Every 2 hours",
          cron: "0 */2 * * *",
          timezone: "UTC",
        },
      ];
      spec.routines = [
        ...(spec.routines ?? []),
        {
          kind: "workflow_run",
          id: routineId,
          displayName: "Scheduled workflow",
          target: target.workflowId,
          triggers: [triggerId],
          maxTurns: 8,
          budget: { maxTokens: 20_000 },
        },
      ];
      return { blueprint: next, added: routineId };
    }
    case "function_tool": {
      const id = uniqueId("tool", taken);
      spec.tools = [
        ...(spec.tools ?? []),
        {
          kind: "function",
          id,
          displayName: id.replace(/_/g, "-"),
          description: "TODO: describe this tool.",
        },
      ];
      return { blueprint: next, added: id };
    }
    case "database_tool": {
      const id = uniqueId("database", taken);
      spec.tools = [
        ...(spec.tools ?? []),
        {
          kind: "database",
          id,
          displayName: id.replace(/_/g, "-"),
          description: "TODO: describe this database tool.",
        },
      ];
      return { blueprint: next, added: id };
    }
    case "skill": {
      const id = uniqueId("skill", taken);
      spec.skills = [
        ...(spec.skills ?? []),
        {
          id,
          displayName: id.replace(/_/g, "-"),
          instructions: "TODO: describe this skill.",
          allowedTools: [],
        },
      ];
      return { blueprint: next, added: id };
    }
    case "cron_trigger": {
      const id = uniqueId("cron", taken);
      spec.triggers = [
        ...(spec.triggers ?? []),
        { kind: "cron", id, displayName: id.replace(/_/g, "-"), cron: "0 2 * * *", timezone: "UTC" },
      ];
      return { blueprint: next, added: id };
    }
    case "manual_trigger": {
      const id = uniqueId("manual", taken);
      spec.triggers = [
        ...(spec.triggers ?? []),
        { kind: "manual", id, displayName: id.replace(/_/g, "-") },
      ];
      return { blueprint: next, added: id };
    }
    case "ci_trigger": {
      const id = uniqueId("ci", taken);
      spec.triggers = [
        ...(spec.triggers ?? []),
        { kind: "ci", id, displayName: id.replace(/_/g, "-"), provider: "generic" },
      ];
      return { blueprint: next, added: id };
    }
    case "webhook_trigger": {
      const id = uniqueId("webhook", taken);
      spec.triggers = [
        ...(spec.triggers ?? []),
        {
          kind: "webhook",
          id,
          displayName: id.replace(/_/g, "-"),
          signingSecretEnv: "WEBHOOK_SIGNING_SECRET",
        },
      ];
      return { blueprint: next, added: id };
    }
    default:
      return { blueprint, added: null, reason: "unknown" };
  }
}

export function setAtPointer(blueprint: Blueprint, pointer: string, value: unknown): Blueprint {
  const parts = pointer.slice(1).split("/");
  const next = structuredClone(blueprint) as Blueprint;
  let parent: unknown = next;
  for (const part of parts.slice(0, -1)) {
    if (parent === null || typeof parent !== "object") return blueprint;
    const container = parent as Record<string, unknown>;
    if (container[part] === undefined || container[part] === null) container[part] = {};
    parent = container[part];
  }
  if (parent === null || typeof parent !== "object") return blueprint;
  const leaf = parts[parts.length - 1];
  if (value === undefined) {
    delete (parent as Record<string, unknown>)[leaf];
  } else {
    (parent as Record<string, unknown>)[leaf] = value;
  }
  return next;
}

export function removeAtPointer(blueprint: Blueprint, pointer: string): Blueprint {
  const parts = pointer.slice(1).split("/");
  const next = structuredClone(blueprint) as Blueprint;
  let parent: unknown = next;
  for (const part of parts.slice(0, -1)) {
    if (parent === null || typeof parent !== "object") return blueprint;
    parent = (parent as Record<string, unknown>)[part];
  }
  const leaf = parts[parts.length - 1];
  if (Array.isArray(parent)) {
    const index = Number(leaf);
    if (Number.isInteger(index)) parent.splice(index, 1);
    return next;
  }
  if (parent && typeof parent === "object") {
    delete (parent as Record<string, unknown>)[leaf];
    return next;
  }
  return blueprint;
}
