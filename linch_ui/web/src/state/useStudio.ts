import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { StudioApiError, type StudioApi } from "../api/client";
import type {
  Blueprint,
  CapabilityCatalog,
  Diagnostic,
  LayoutDocument,
  ProjectDocument,
  ProjectSummary,
  ProposalResponse,
  TurnToolCall,
} from "../api/types";
import { emitBlueprintYaml } from "../model/emit";
import {
  blueprintToGraph,
  connectWorkflowNodes,
  disconnectSemanticRelation,
} from "../model/graph";
import { removeAtPointer } from "../model/palette";
import { hasErrors } from "./derive";
import type { SaveState } from "./derive";

export type Tab = "design" | "yaml" | "diagnostics" | "files" | "export";

export interface Toast {
  marker: "[ok]" | "[!!]" | "[xx]";
  text: string;
}

export interface RecentConnection {
  edgeId: string;
  sourceId: string;
  sourceTitle: string;
  targetId: string;
  targetTitle: string;
  relation: string;
}

export interface TranscriptQuestion {
  question: string;
  options: string[];
}

export interface TranscriptToolCall {
  toolUseId: string;
  toolName: string;
  /** Pre-call, one line (e.g. "search_docs: fan-out review"). */
  summary: string;
  status: "running" | "done" | "error";
  /** Post-call, one line (e.g. "3 results"); unset while still running. */
  resultSummary?: string | null;
  /** Full bounded content, shown only behind an explicit expand toggle. */
  detail?: string | null;
  durationMs?: number;
}

export interface TranscriptEntry {
  role: "user" | "agent";
  kind: "text" | "questions" | "plan" | "proposal";
  /** The exact text re-sent to the server for this turn. */
  content: string;
  note?: string | null;
  questions?: TranscriptQuestion[];
  plan?: string | null;
  /** Display-only reasoning trace; never re-sent to the server. */
  thinking?: string | null;
  /** Display-only tool-call trace, oldest first; never re-sent to the server. */
  toolCalls?: TranscriptToolCall[];
  proposalId?: string;
  accepted?: boolean;
}

/** The fixed approval message that moves a conversation into the build stage. */
export const PLAN_APPROVAL_MESSAGE = "Proceed with this plan.";

/** The server's final tool-call trace, reshaped for the persisted transcript entry. */
function finalToolCalls(calls?: TurnToolCall[]): TranscriptToolCall[] {
  return (calls ?? []).map((call) => ({
    toolUseId: call.toolUseId,
    toolName: call.toolName,
    summary: call.summary,
    status: call.isError ? "error" : "done",
    resultSummary: call.resultSummary,
    detail: call.detail,
    durationMs: call.durationMs,
  }));
}

export interface StudioState {
  ready: boolean;
  bootError: string | null;
  authoringAvailable: boolean;
  catalog: CapabilityCatalog | null;
  projects: ProjectSummary[];
  projectsLoading: boolean;
  doc: ProjectDocument | null;
  layout: LayoutDocument;
  buffer: string;
  bufferDirty: boolean;
  /** Diagnostics for the *buffer* when it differs from disk, else for the saved doc. */
  diagnostics: Diagnostic[];
  structurallyValid: boolean;
  exportReady: boolean;
  saveState: SaveState;
  conflictDigest: string | null;
  tab: Tab;
  selectedId: string | null;
  selectedEdgeId: string | null;
  editHistory: Blueprint[];
  toast: Toast | null;
  recentConnection: RecentConnection | null;
  proposals: ProposalResponse[];
  transcript: TranscriptEntry[];
  /** Reasoning accumulated for the turn in flight; null when nothing streams. */
  streamingThinking: string | null;
  /** Tool calls seen so far for the turn in flight; null when nothing streams. */
  streamingToolCalls: TranscriptToolCall[] | null;
}

const EMPTY_LAYOUT: LayoutDocument = { nodes: [], viewport: { x: 0, y: 0, zoom: 1 } };

export function useStudio(api: StudioApi) {
  const [state, setState] = useState<StudioState>({
    ready: false,
    bootError: null,
    authoringAvailable: false,
    catalog: null,
    projects: [],
    projectsLoading: true,
    doc: null,
    layout: EMPTY_LAYOUT,
    buffer: "",
    bufferDirty: false,
    diagnostics: [],
    structurallyValid: true,
    exportReady: false,
    saveState: "saved",
    conflictDigest: null,
    tab: "design",
    selectedId: null,
    selectedEdgeId: null,
    editHistory: [],
    toast: null,
    recentConnection: null,
    proposals: [],
    transcript: [],
    streamingThinking: null,
    streamingToolCalls: null,
  });

  const patch = useCallback((next: Partial<StudioState>) => {
    setState((current) => ({ ...current, ...next }));
  }, []);

  const toast = useCallback(
    (marker: Toast["marker"], text: string) => patch({ toast: { marker, text } }),
    [patch],
  );

  const fail = useCallback(
    (error: unknown, fallback: string) => {
      const message = error instanceof StudioApiError ? error.message : fallback;
      toast("[xx]", message);
    },
    [toast],
  );

  // ---- boot -------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [info, catalog] = await Promise.all([api.serviceInfo(), api.catalog()]);
        if (cancelled) return;
        patch({ ready: true, authoringAvailable: info.authoringAvailable, catalog });
      } catch (error) {
        if (cancelled) return;
        patch({
          ready: true,
          bootError: error instanceof StudioApiError ? error.message : String(error),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, patch]);

  const refreshProjects = useCallback(async () => {
    patch({ projectsLoading: true });
    try {
      patch({ projects: await api.listProjects(), projectsLoading: false });
    } catch (error) {
      patch({ projectsLoading: false });
      fail(error, "Could not list projects.");
    }
  }, [api, patch, fail]);

  useEffect(() => {
    if (state.ready && !state.bootError) void refreshProjects();
  }, [state.ready, state.bootError, refreshProjects]);

  // ---- project lifecycle ------------------------------------------------
  const adopt = useCallback(
    (doc: ProjectDocument, layout: LayoutDocument) => {
      setState((current) => ({
        ...current,
        doc,
        layout,
        buffer: doc.yaml,
        bufferDirty: false,
        diagnostics: doc.diagnostics,
        structurallyValid: true,
        exportReady: doc.exportReady,
        saveState: hasErrors(doc.diagnostics) ? "draft" : "saved",
        conflictDigest: null,
        selectedId: null,
        selectedEdgeId: null,
        recentConnection: null,
        editHistory: [],
        proposals: [],
        transcript: [],
        streamingThinking: null,
      }));
    },
    [],
  );

  const openProject = useCallback(
    async (id: string) => {
      try {
        const doc = await api.openProject(id);
        const layout = await api.getLayout(id);
        adopt(doc, layout.layout);
      } catch (error) {
        fail(error, "Could not open the project.");
      }
    },
    [api, adopt, fail],
  );

  const createProject = useCallback(
    async (
      id: string,
      title?: string,
      template: "agent" | "goal_verified" | "directed_workflow" | "coordinator" | "routine" =
        "agent",
    ) => {
      try {
        const doc = await api.createProject(id, title, template);
        const layout = await api.getLayout(doc.id);
        adopt(doc, layout.layout);
        void refreshProjects();
        return true;
      } catch (error) {
        fail(error, "Could not create the project.");
        return false;
      }
    },
    [api, adopt, fail, refreshProjects],
  );

  const closeProject = useCallback(() => {
    patch({ doc: null, tab: "design", selectedId: null, selectedEdgeId: null, editHistory: [] });
    void refreshProjects();
  }, [patch, refreshProjects]);

  // ---- buffer validation (debounced) ------------------------------------
  const validateTimer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => {
    if (!state.doc || !state.bufferDirty) return;
    clearTimeout(validateTimer.current);
    validateTimer.current = setTimeout(async () => {
      try {
        const result = await api.validateBuffer(state.buffer);
        setState((current) => ({
          ...current,
          diagnostics: result.diagnostics,
          structurallyValid: result.structurallyValid,
          exportReady: false,
          // A structural error can never be written; keep it in the buffer only.
          saveState: result.structurallyValid ? current.saveState : "buffer",
        }));
      } catch {
        // A failed probe must not block typing.
      }
    }, 300);
    return () => clearTimeout(validateTimer.current);
  }, [api, state.buffer, state.bufferDirty, state.doc]);

  const setBuffer = useCallback((yaml: string) => {
    setState((current) => ({
      ...current,
      buffer: yaml,
      bufferDirty: yaml !== current.doc?.yaml,
    }));
  }, []);

  const revertBuffer = useCallback(() => {
    setState((current) =>
      current.doc
        ? {
            ...current,
            buffer: current.doc.yaml,
            bufferDirty: false,
            diagnostics: current.doc.diagnostics,
            structurallyValid: true,
            exportReady: current.doc.exportReady,
            saveState: hasErrors(current.doc.diagnostics) ? "draft" : "saved",
            conflictDigest: null,
          }
        : current,
    );
  }, []);

  /** Compare-and-swap save. Returns false on stale digest or structural rejection. */
  const save = useCallback(
    async (yaml?: string): Promise<boolean> => {
      const doc = state.doc;
      if (!doc) return false;
      const source = yaml ?? state.buffer;
      patch({ saveState: "saving" });
      try {
        const saved = await api.saveBlueprint(doc.id, source, doc.digest);
        setState((current) => ({
          ...current,
          doc: saved,
          buffer: saved.yaml,
          bufferDirty: false,
          diagnostics: saved.diagnostics,
          structurallyValid: true,
          exportReady: saved.exportReady,
          saveState: hasErrors(saved.diagnostics) ? "draft" : "saved",
          conflictDigest: null,
        }));
        void refreshProjects();
        return true;
      } catch (error) {
        if (error instanceof StudioApiError && error.is("blueprint.stale_digest")) {
          patch({ saveState: "conflict", conflictDigest: error.currentDigest ?? null });
          return false;
        }
        if (error instanceof StudioApiError && error.is("blueprint.structural_invalid")) {
          patch({ saveState: "buffer", diagnostics: error.diagnostics, structurallyValid: false });
          return false;
        }
        patch({ saveState: "draft" });
        fail(error, "Could not save the blueprint.");
        return false;
      }
    },
    [api, state.doc, state.buffer, patch, fail, refreshProjects],
  );

  /** Reload from disk, discarding the local buffer. Resolves a save conflict. */
  const reloadFromDisk = useCallback(async () => {
    if (!state.doc) return;
    await openProject(state.doc.id);
    toast("[ok]", "Reloaded from disk.");
  }, [state.doc, openProject, toast]);

  /**
   * Apply a canvas/inspector edit: mutate the Blueprint, re-emit YAML, save.
   * The server is the authority — an invalid emit comes back as 422 and is
   * never persisted.
   */
  const applyBlueprint = useCallback(
    async (next: Blueprint) => {
      const previous = state.doc?.blueprint;
      const yaml = emitBlueprintYaml(next);
      setBuffer(yaml);
      const saved = await save(yaml);
      if (saved && previous) {
        setState((current) => ({
          ...current,
          editHistory: [...current.editHistory, previous].slice(-50),
        }));
      }
      return saved;
    },
    [save, setBuffer, state.doc?.blueprint],
  );

  const undoLastEdit = useCallback(async () => {
    const previous = state.editHistory.at(-1);
    if (!previous) return false;
    const yaml = emitBlueprintYaml(previous);
    setBuffer(yaml);
    const saved = await save(yaml);
    if (saved) {
      setState((current) => ({
        ...current,
        editHistory: current.editHistory.slice(0, -1),
        selectedId: null,
        selectedEdgeId: null,
      }));
    }
    return saved;
  }, [save, setBuffer, state.editHistory]);

  /**
   * Remove the selected node. Shared by the inspector's delete button and the
   * canvas Del key so both write through exactly one path.
   */
  const removeSelected = useCallback(async () => {
    const current = state.doc?.blueprint;
    if (!current || !state.selectedId) return;
    const node = blueprintToGraph(current).nodes.find((item) => item.id === state.selectedId);
    if (!node) return;
    const next = removeAtPointer(current, node.data.pointer);
    // `removeAtPointer` answers with the same object when the pointer is not
    // removable; saving then would be a pointless round-trip.
    if (next === current) return;
    patch({ selectedId: null, selectedEdgeId: null });
    await applyBlueprint(next);
  }, [state.doc, state.selectedId, patch, applyBlueprint]);

  /** Persist a valid deterministic-workflow dependency created on the canvas. */
  const connectionTimer = useRef<ReturnType<typeof setTimeout>>();
  const connectNodes = useCallback(
    async (sourceId: string, targetId: string) => {
      const current = state.doc?.blueprint;
      if (!current) return false;
      const result = connectWorkflowNodes(current, sourceId, targetId);
      if (!result.connected) return false;
      const saved = await applyBlueprint(result.blueprint);
      if (saved) {
        const nextGraph = blueprintToGraph(result.blueprint);
        const edge = nextGraph.edges.find(
          (item) => item.source === sourceId && item.target === targetId,
        );
        const source = nextGraph.nodes.find((item) => item.id === sourceId);
        const target = nextGraph.nodes.find((item) => item.id === targetId);
        if (edge && source && target) {
          const feedback: RecentConnection = {
            edgeId: edge.id,
            sourceId,
            sourceTitle: source.data.title,
            targetId,
            targetTitle: target.data.title,
            relation: edge.relation,
          };
          clearTimeout(connectionTimer.current);
          patch({ recentConnection: feedback });
          connectionTimer.current = setTimeout(() => {
            setState((currentState) =>
              currentState.recentConnection?.edgeId === feedback.edgeId
                ? { ...currentState, recentConnection: null }
                : currentState,
            );
          }, 3_200);
          toast("[ok]", `${source.data.title} → ${target.data.title} connected.`);
        } else {
          toast("[ok]", "Connection saved.");
        }
      }
      return saved;
    },
    [state.doc, applyBlueprint, patch, toast],
  );

  const disconnectEdge = useCallback(
    async (edgeId: string) => {
      const current = state.doc?.blueprint;
      if (!current) return false;
      const edge = blueprintToGraph(current).edges.find((item) => item.id === edgeId);
      if (!edge) return false;
      const result = disconnectSemanticRelation(current, edge.source, edge.target);
      if (!result.disconnected) return false;
      const saved = await applyBlueprint(result.blueprint);
      if (saved) {
        patch({ selectedEdgeId: null });
        toast("[ok]", "Connection removed. Undo is available.");
      }
      return saved;
    },
    [state.doc?.blueprint, applyBlueprint, patch, toast],
  );

  // ---- layout (never touches the digest) --------------------------------
  const layoutTimer = useRef<ReturnType<typeof setTimeout>>();
  const saveLayout = useCallback(
    (layout: LayoutDocument) => {
      setState((current) => ({ ...current, layout }));
      const id = state.doc?.id;
      if (!id) return;
      clearTimeout(layoutTimer.current);
      layoutTimer.current = setTimeout(async () => {
        try {
          await api.saveLayout(id, layout);
        } catch (error) {
          fail(error, "Could not save the canvas layout.");
        }
      }, 400);
    },
    [api, state.doc?.id, fail],
  );

  useEffect(
    () => () => {
      clearTimeout(layoutTimer.current);
      clearTimeout(connectionTimer.current);
    },
    [],
  );

  // ---- proposals --------------------------------------------------------
  const propose = useCallback(
    async (instruction: string) => {
      if (!state.doc) return;
      try {
        const proposal = await api.createProposal(state.doc.id, instruction);
        setState((current) => ({ ...current, proposals: [...current.proposals, proposal] }));
      } catch (error) {
        if (error instanceof StudioApiError && error.is("authoring.unavailable")) {
          patch({ authoringAvailable: false });
          return;
        }
        fail(error, "The authoring provider did not return a valid proposal.");
      }
    },
    [api, state.doc, patch, fail],
  );

  /**
   * One conversational turn. The transcript lives here and is re-sent whole on
   * every request — the server keeps no conversation state. Chat-stage turns
   * come back as questions or a plan; the build stage delivers the proposal.
   */
  const turnTokenRef = useRef(0);
  const runTurn = useCallback(
    async (message: string, stage: "chat" | "build"): Promise<boolean> => {
      const doc = state.doc;
      if (!doc) return false;
      // Guards every setState below against two races: a newer runTurn call
      // superseding this one, and the user switching projects mid-flight.
      const myTurn = ++turnTokenRef.current;
      const isCurrent = (current: StudioState) =>
        turnTokenRef.current === myTurn && current.doc?.id === doc.id;
      const userEntry: TranscriptEntry = { role: "user", kind: "text", content: message };
      const outgoing = [...state.transcript, userEntry].map((entry) => ({
        role: entry.role === "user" ? ("user" as const) : ("assistant" as const),
        content: entry.content,
      }));
      setState((current) => ({
        ...current,
        transcript: [...current.transcript, userEntry],
        streamingThinking: "",
        streamingToolCalls: [],
      }));
      try {
        const turn = await api.converseStream(doc.id, outgoing, stage, {
          onThinking: (text) =>
            setState((current) =>
              !isCurrent(current) || current.streamingThinking === null
                ? current
                : { ...current, streamingThinking: current.streamingThinking + text },
            ),
          onToolCallStart: (update) =>
            setState((current) =>
              !isCurrent(current) || current.streamingToolCalls === null
                ? current
                : {
                    ...current,
                    streamingToolCalls: [
                      ...current.streamingToolCalls,
                      {
                        toolUseId: update.toolUseId,
                        toolName: update.toolName,
                        summary: update.summary,
                        status: "running",
                      },
                    ],
                  },
            ),
          onToolCallEnd: (update) =>
            setState((current) =>
              !isCurrent(current) || current.streamingToolCalls === null
                ? current
                : {
                    ...current,
                    streamingToolCalls: current.streamingToolCalls.map((call) =>
                      call.toolUseId === update.toolUseId
                        ? {
                            ...call,
                            status: update.isError ? "error" : "done",
                            resultSummary: update.summary,
                            detail: update.detail,
                            durationMs: update.durationMs,
                          }
                        : call,
                    ),
                  },
            ),
        });
        const note = turn.planNote ?? null;
        const toolCalls = finalToolCalls(turn.toolCalls);
        if (turn.kind === "questions") {
          const questions = turn.questions ?? [];
          const entry: TranscriptEntry = {
            role: "agent",
            kind: "questions",
            content: [
              note ?? "",
              ...questions.map(
                (item, index) =>
                  `${index + 1}. ${item.question} (${item.options.join(" / ")})`,
              ),
            ]
              .filter(Boolean)
              .join("\n"),
            note,
            questions,
            thinking: turn.thinking ?? null,
            toolCalls,
          };
          setState((current) =>
            isCurrent(current)
              ? { ...current, transcript: [...current.transcript, entry] }
              : current,
          );
          return true;
        }
        if (turn.kind === "plan") {
          const plan = turn.plan ?? "";
          const entry: TranscriptEntry = {
            role: "agent",
            kind: "plan",
            content: [plan, note ?? ""].filter(Boolean).join("\n"),
            note,
            plan,
            thinking: turn.thinking ?? null,
            toolCalls,
          };
          setState((current) =>
            isCurrent(current)
              ? { ...current, transcript: [...current.transcript, entry] }
              : current,
          );
          return true;
        }
        const proposal = turn.proposal;
        if (!proposal) return false;
        const entry: TranscriptEntry = {
          role: "agent",
          kind: "proposal",
          content: proposal.summary ?? note ?? "Proposed a full candidate blueprint.",
          note,
          thinking: turn.thinking ?? null,
          toolCalls,
          proposalId: proposal.id,
        };
        setState((current) =>
          isCurrent(current)
            ? {
                ...current,
                transcript: [...current.transcript, entry],
                proposals: [...current.proposals, proposal],
              }
            : current,
        );
        return true;
      } catch (error) {
        // The echoed user message stays visible so nothing typed is lost.
        if (error instanceof StudioApiError && error.is("authoring.unavailable")) {
          patch({ authoringAvailable: false });
          return false;
        }
        fail(error, "The authoring agent could not answer this turn.");
        return false;
      } finally {
        setState((current) =>
          turnTokenRef.current !== myTurn ||
          (current.streamingThinking === null && current.streamingToolCalls === null)
            ? current
            : { ...current, streamingThinking: null, streamingToolCalls: null },
        );
      }
    },
    [api, state.doc, state.transcript, patch, fail],
  );

  const converse = useCallback(
    async (message: string) => {
      await runTurn(message, "chat");
    },
    [runTurn],
  );

  /** The plan-approval gate: a fixed user message flips the turn to build stage. */
  const approvePlan = useCallback(async () => {
    const ok = await runTurn(PLAN_APPROVAL_MESSAGE, "build");
    if (!ok) {
      // The approval message is synthetic, not typed: rolling it back leaves
      // the plan as the newest entry, so its gate re-arms for another try.
      setState((current) => {
        const last = current.transcript.at(-1);
        if (last?.role === "user" && last.content === PLAN_APPROVAL_MESSAGE) {
          return { ...current, transcript: current.transcript.slice(0, -1) };
        }
        return current;
      });
    }
  }, [runTurn]);

  const acceptProposal = useCallback(
    async (proposalId: string) => {
      if (!state.doc) return;
      try {
        const saved = await api.acceptProposal(state.doc.id, proposalId);
        const layout = await api.getLayout(saved.id);
        // Accepting is part of the conversation, not a context switch: keep the
        // transcript and only mark this proposal's entry as landed.
        const kept = state.transcript.map((entry) =>
          entry.proposalId === proposalId ? { ...entry, accepted: true } : entry,
        );
        adopt(saved, layout.layout);
        patch({ transcript: kept });
        toast("[ok]", "Proposal accepted.");
        void refreshProjects();
      } catch (error) {
        if (error instanceof StudioApiError && error.is("blueprint.stale_digest")) {
          patch({ conflictDigest: error.currentDigest ?? null });
          toast("[!!]", "The blueprint changed since this proposal — it cannot be accepted.");
          return;
        }
        fail(error, "Could not accept the proposal.");
      }
    },
    [api, state.doc, state.transcript, adopt, toast, patch, fail, refreshProjects],
  );

  const rejectProposal = useCallback(
    async (proposalId: string) => {
      if (!state.doc) return;
      try {
        await api.rejectProposal(state.doc.id, proposalId);
      } catch {
        // Rejection is best-effort; drop it locally regardless.
      }
      setState((current) => ({
        ...current,
        proposals: current.proposals.filter((item) => item.id !== proposalId),
      }));
    },
    [api, state.doc],
  );

  const blueprint = useMemo(() => state.doc?.blueprint ?? null, [state.doc]);

  return {
    state,
    blueprint,
    actions: {
      patch,
      toast,
      refreshProjects,
      openProject,
      createProject,
      closeProject,
      setBuffer,
      revertBuffer,
      save,
      reloadFromDisk,
      applyBlueprint,
      undoLastEdit,
      removeSelected,
      connectNodes,
      disconnectEdge,
      saveLayout,
      propose,
      converse,
      approvePlan,
      acceptProposal,
      rejectProposal,
      setTab: (tab: Tab) => patch({ tab }),
      select: (selectedId: string | null) => patch({ selectedId, selectedEdgeId: null }),
      selectEdge: (selectedEdgeId: string | null) => patch({ selectedEdgeId, selectedId: null }),
      dismissToast: () => patch({ toast: null }),
    },
  };
}

export type Studio = ReturnType<typeof useStudio>;
