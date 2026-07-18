import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { parse } from "yaml";

import type { StudioApi } from "../api/client";
import type {
  Blueprint,
  ProjectDocument,
  ProposalResponse,
  TurnMessage,
  TurnResponse,
} from "../api/types";
import { emitBlueprintYaml } from "../model/emit";
import { blueprintToGraph } from "../model/graph";
import { WORKFLOW_BLUEPRINT } from "../model/fixtures";
import { useStudio } from "./useStudio";

function docFor(blueprint: Blueprint): ProjectDocument {
  return {
    id: "demo",
    digest: "a".repeat(64),
    yaml: emitBlueprintYaml(blueprint),
    blueprint,
    diagnostics: [],
    exportReady: true,
    layout: { nodes: [], viewport: { x: 0, y: 0, zoom: 1 } },
  } as ProjectDocument;
}

/** Records every saved YAML so a test can assert what the server would receive. */
function fakeApi(blueprint: Blueprint) {
  const saved: string[] = [];
  const api = {
    serviceInfo: vi.fn().mockResolvedValue({ authoringAvailable: false }),
    catalog: vi.fn().mockResolvedValue(null),
    listProjects: vi.fn().mockResolvedValue([]),
    openProject: vi.fn().mockResolvedValue(docFor(blueprint)),
    getLayout: vi.fn().mockResolvedValue({ id: "demo", layout: { nodes: [] } }),
    saveBlueprint: vi.fn(async (_id: string, yaml: string) => {
      saved.push(yaml);
      return { ...docFor(parse(yaml) as Blueprint), yaml };
    }),
    validateBuffer: vi.fn().mockResolvedValue({ diagnostics: [], structurallyValid: true }),
  } as unknown as StudioApi;
  return { api, saved };
}

async function openedStudio(blueprint: Blueprint) {
  const { api, saved } = fakeApi(blueprint);
  const hook = renderHook(() => useStudio(api));
  await waitFor(() => expect(hook.result.current.state.ready).toBe(true));
  await act(async () => {
    await hook.result.current.actions.openProject("demo");
  });
  return { hook, saved };
}

describe("removeSelected", () => {
  it("removes the selected node and saves the re-emitted blueprint", async () => {
    const { hook, saved } = await openedStudio(WORKFLOW_BLUEPRINT);

    act(() => hook.result.current.actions.select("subagent_style_reviewer"));
    await act(async () => {
      await hook.result.current.actions.removeSelected();
    });

    expect(saved).toHaveLength(1);
    const sent = parse(saved[0]) as Blueprint;
    expect(sent.spec.subagents?.map((item) => item.id)).not.toContain("style_reviewer");
    expect(hook.result.current.state.selectedId).toBeNull();
  });

  it("does nothing when no node is selected", async () => {
    const { hook, saved } = await openedStudio(WORKFLOW_BLUEPRINT);

    await act(async () => {
      await hook.result.current.actions.removeSelected();
    });

    expect(saved).toHaveLength(0);
  });

  it("does not save when the selection maps to nothing removable", async () => {
    const { hook, saved } = await openedStudio(WORKFLOW_BLUEPRINT);

    act(() => hook.result.current.actions.select("no_such_node"));
    await act(async () => {
      await hook.result.current.actions.removeSelected();
    });

    expect(saved).toHaveLength(0);
  });
});

describe("semantic edge editing", () => {
  it("deletes an optional edge and restores it through undo", async () => {
    const { hook, saved } = await openedStudio(WORKFLOW_BLUEPRINT);
    const edge = blueprintToGraph(WORKFLOW_BLUEPRINT).edges.find(
      (item) => item.relation === "depends_on",
    );
    expect(edge).toBeDefined();

    await act(async () => {
      await hook.result.current.actions.disconnectEdge(edge!.id);
    });
    expect(saved).toHaveLength(1);
    expect(hook.result.current.state.editHistory).toHaveLength(1);

    await act(async () => {
      await hook.result.current.actions.undoLastEdit();
    });
    expect(saved).toHaveLength(2);
    const restored = parse(saved[1]) as Blueprint;
    const style = restored.spec.workflows?.[0]?.nodes?.find((node) => node.id === "style_step");
    expect(style?.dependsOn).toContain("fetch_diff");
    expect(hook.result.current.state.editHistory).toHaveLength(0);
  });
});

describe("connection feedback", () => {
  it("describes a saved connection so the canvas can point at both ends", async () => {
    const { hook } = await openedStudio(WORKFLOW_BLUEPRINT);
    const security = "wf_pr_review_security_step";
    const style = "wf_pr_review_style_step";

    await act(async () => {
      await hook.result.current.actions.connectNodes(security, style);
    });

    // The canvas flashes both endpoints and badges the new edge from this, so it
    // has to name the real edge and carry titles rather than raw ids.
    const recent = hook.result.current.state.recentConnection;
    expect(recent).toMatchObject({ sourceId: security, targetId: style, relation: "depends_on" });
    expect(recent?.edgeId).toBe(`${security}__${style}__depends_on`);
    expect(recent?.sourceTitle.length).toBeGreaterThan(0);
    expect(recent?.targetTitle.length).toBeGreaterThan(0);
  });

  it("reports nothing when the relation was refused", async () => {
    const { hook } = await openedStudio(WORKFLOW_BLUEPRINT);

    await act(async () => {
      await hook.result.current.actions.connectNodes("subagent_style_reviewer", "primary_agent");
    });

    expect(hook.result.current.state.recentConnection).toBeNull();
  });
});

describe("conversational authoring", () => {
  function proposalFor(blueprint: Blueprint, id = "prop-1"): ProposalResponse {
    return {
      id,
      projectId: "demo",
      baseDigest: "a".repeat(64),
      candidateDigest: "b".repeat(64),
      candidate: blueprint,
      semanticDiff: [
        { operation: "replace", path: "/metadata/title", before: "Demo", after: "Better" },
      ],
      diagnostics: [],
      exportReady: true,
      createdAt: "2026-07-17T00:00:00Z",
      summary: "retitled the project",
    };
  }

  function conversationalApi(blueprint: Blueprint) {
    const turns: TurnResponse[] = [];
    const converse = vi.fn(
      async (
        _id: string,
        _messages: TurnMessage[],
        _stage?: "chat" | "build",
        handlers?: { onThinking?: (text: string) => void },
      ) => {
        const turn = turns.shift();
        if (!turn) throw new Error("no scripted turn");
        if (turn.thinking) handlers?.onThinking?.(turn.thinking);
        return turn;
      },
    );
    const api = {
      serviceInfo: vi.fn().mockResolvedValue({ authoringAvailable: true }),
      catalog: vi.fn().mockResolvedValue(null),
      listProjects: vi.fn().mockResolvedValue([]),
      openProject: vi.fn().mockResolvedValue(docFor(blueprint)),
      getLayout: vi.fn().mockResolvedValue({ id: "demo", layout: { nodes: [] } }),
      validateBuffer: vi.fn().mockResolvedValue({ diagnostics: [], structurallyValid: true }),
      converseStream: converse,
      acceptProposal: vi.fn(async () => docFor(blueprint)),
      rejectProposal: vi.fn(async () => undefined),
    } as unknown as StudioApi;
    return { api, converse, turns };
  }

  async function conversationalStudio(blueprint: Blueprint) {
    const { api, converse, turns } = conversationalApi(blueprint);
    const hook = renderHook(() => useStudio(api));
    await waitFor(() => expect(hook.result.current.state.ready).toBe(true));
    await act(async () => {
      await hook.result.current.actions.openProject("demo");
    });
    return { hook, converse, turns };
  }

  const PROVIDER_QUESTION = {
    question: "Which provider?",
    options: ["openai", "anthropic", "a local model"],
  };

  it("echoes the user message and lands the agent's option-backed questions", async () => {
    const { hook, converse, turns } = await conversationalStudio(WORKFLOW_BLUEPRINT);
    turns.push({
      kind: "questions",
      questions: [PROVIDER_QUESTION],
      plan: null,
      planNote: "plan: one agent",
      proposal: null,
      thinking: "weighed cron vs webhook",
    });

    await act(async () => {
      await hook.result.current.actions.converse("build me a reviewer");
    });

    const transcript = hook.result.current.state.transcript;
    expect(transcript).toHaveLength(2);
    expect(transcript[0]).toMatchObject({
      role: "user",
      kind: "text",
      content: "build me a reviewer",
    });
    expect(transcript[1]).toMatchObject({
      role: "agent",
      kind: "questions",
      note: "plan: one agent",
      questions: [PROVIDER_QUESTION],
      thinking: "weighed cron vs webhook",
    });
    expect(converse).toHaveBeenCalledWith(
      "demo",
      [{ role: "user", content: "build me a reviewer" }],
      "chat",
      expect.anything(),
    );
  });

  it("exposes live thinking while a turn streams and clears it when the turn lands", async () => {
    const { api } = conversationalApi(WORKFLOW_BLUEPRINT);
    let handlersRef: { onThinking?: (text: string) => void } = {};
    let release: (turn: TurnResponse) => void = () => undefined;
    (api as { converseStream: unknown }).converseStream = vi.fn(
      (
        _id: string,
        _messages: TurnMessage[],
        _stage?: "chat" | "build",
        handlers?: { onThinking?: (text: string) => void },
      ) => {
        handlersRef = handlers ?? {};
        return new Promise<TurnResponse>((resolve) => {
          release = resolve;
        });
      },
    );
    const hook = renderHook(() => useStudio(api));
    await waitFor(() => expect(hook.result.current.state.ready).toBe(true));
    await act(async () => {
      await hook.result.current.actions.openProject("demo");
    });

    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = hook.result.current.actions.converse("build me a reviewer");
    });
    // The turn is in flight: deltas surface immediately, in arrival order.
    act(() => {
      handlersRef.onThinking?.("weighing ");
      handlersRef.onThinking?.("options");
    });
    expect(hook.result.current.state.streamingThinking).toBe("weighing options");

    await act(async () => {
      release({
        kind: "questions",
        questions: [PROVIDER_QUESTION],
        plan: null,
        planNote: null,
        proposal: null,
        thinking: "weighing options",
      });
      await pending;
    });

    expect(hook.result.current.state.streamingThinking).toBeNull();
    expect(hook.result.current.state.transcript.at(-1)).toMatchObject({
      kind: "questions",
      thinking: "weighing options",
    });
  });

  it("shows a running tool call live, then settles it once the turn lands", async () => {
    const { api } = conversationalApi(WORKFLOW_BLUEPRINT);
    type ToolCallHandlers = {
      onToolCallStart?: (update: { toolUseId: string; toolName: string; summary: string }) => void;
      onToolCallEnd?: (update: {
        toolUseId: string;
        toolName: string;
        summary: string;
        detail: string | null;
        isError: boolean;
        durationMs: number;
      }) => void;
    };
    let handlersRef: ToolCallHandlers = {};
    let release: (turn: TurnResponse) => void = () => undefined;
    (api as { converseStream: unknown }).converseStream = vi.fn(
      (_id: string, _messages: TurnMessage[], _stage?: "chat" | "build", handlers?: ToolCallHandlers) => {
        handlersRef = handlers ?? {};
        return new Promise<TurnResponse>((resolve) => {
          release = resolve;
        });
      },
    );
    const hook = renderHook(() => useStudio(api));
    await waitFor(() => expect(hook.result.current.state.ready).toBe(true));
    await act(async () => {
      await hook.result.current.actions.openProject("demo");
    });

    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = hook.result.current.actions.converse("build me a reviewer");
    });
    act(() => {
      handlersRef.onToolCallStart?.({
        toolUseId: "call_1",
        toolName: "search_docs",
        summary: "search_docs: fan-out",
      });
    });
    expect(hook.result.current.state.streamingToolCalls).toEqual([
      { toolUseId: "call_1", toolName: "search_docs", summary: "search_docs: fan-out", status: "running" },
    ]);

    act(() => {
      handlersRef.onToolCallEnd?.({
        toolUseId: "call_1",
        toolName: "search_docs",
        summary: "3 results",
        detail: "full text",
        isError: false,
        durationMs: 42,
      });
    });
    expect(hook.result.current.state.streamingToolCalls).toEqual([
      {
        toolUseId: "call_1",
        toolName: "search_docs",
        summary: "search_docs: fan-out",
        status: "done",
        resultSummary: "3 results",
        detail: "full text",
        durationMs: 42,
      },
    ]);

    await act(async () => {
      release({
        kind: "questions",
        questions: [PROVIDER_QUESTION],
        plan: null,
        planNote: null,
        proposal: null,
        thinking: null,
        toolCalls: [
          {
            toolUseId: "call_1",
            toolName: "search_docs",
            summary: "search_docs: fan-out",
            resultSummary: "3 results",
            detail: "full text",
            isError: false,
            durationMs: 42,
          },
        ],
      });
      await pending;
    });

    // The turn is in flight no longer; the persisted transcript entry (not the
    // transient streaming scratch state) carries the tool-call trace onward.
    expect(hook.result.current.state.streamingToolCalls).toBeNull();
    expect(hook.result.current.state.transcript.at(-1)).toMatchObject({
      kind: "questions",
      toolCalls: [
        {
          toolUseId: "call_1",
          toolName: "search_docs",
          summary: "search_docs: fan-out",
          status: "done",
          resultSummary: "3 results",
          detail: "full text",
          durationMs: 42,
        },
      ],
    });
  });

  it("presents the plan in chat stage and builds only after approval", async () => {
    const { hook, converse, turns } = await conversationalStudio(WORKFLOW_BLUEPRINT);
    turns.push(
      {
        kind: "questions",
        questions: [PROVIDER_QUESTION],
        plan: null,
        planNote: null,
        proposal: null,
        thinking: "weighed cron vs webhook",
      },
      {
        kind: "plan",
        questions: [],
        plan: "1. one agent\n2. nightly cron routine",
        planNote: "approve to build",
        proposal: null,
      },
      {
        kind: "proposal",
        questions: [],
        plan: null,
        planNote: "delivering the approved plan",
        proposal: proposalFor(WORKFLOW_BLUEPRINT),
      },
    );

    await act(async () => {
      await hook.result.current.actions.converse("build me a reviewer");
    });
    await act(async () => {
      await hook.result.current.actions.converse("Which provider? → openai");
    });

    expect(hook.result.current.state.transcript[3]).toMatchObject({
      role: "agent",
      kind: "plan",
      plan: "1. one agent\n2. nightly cron routine",
      note: "approve to build",
    });
    expect(converse.mock.calls[1][2]).toBe("chat");

    await act(async () => {
      await hook.result.current.actions.approvePlan();
    });

    expect(converse.mock.calls[2][2]).toBe("build");
    const wire = converse.mock.calls[2][1];
    expect(wire).toHaveLength(5);
    expect(wire[0]).toEqual({ role: "user", content: "build me a reviewer" });
    expect(wire[1].role).toBe("assistant");
    expect(String(wire[1].content)).toContain("Which provider?");
    // Thinking is display-only: it never travels back to the server.
    expect(String(wire[1].content)).not.toContain("weighed cron vs webhook");
    expect(wire[3].role).toBe("assistant");
    expect(String(wire[3].content)).toContain("1. one agent");
    expect(wire[4]).toEqual({ role: "user", content: "Proceed with this plan." });

    const transcript = hook.result.current.state.transcript;
    expect(transcript).toHaveLength(6);
    expect(transcript[5]).toMatchObject({
      role: "agent",
      kind: "proposal",
      proposalId: "prop-1",
      note: "delivering the approved plan",
    });
    expect(hook.result.current.state.proposals.map((item) => item.id)).toEqual(["prop-1"]);
  });

  it("keeps the conversation after accepting and only marks the entry accepted", async () => {
    const { hook, turns } = await conversationalStudio(WORKFLOW_BLUEPRINT);
    turns.push(
      {
        kind: "plan",
        questions: [],
        plan: "1. one agent",
        planNote: null,
        proposal: null,
      },
      {
        kind: "proposal",
        questions: [],
        plan: null,
        planNote: null,
        proposal: proposalFor(WORKFLOW_BLUEPRINT),
      },
    );
    await act(async () => {
      await hook.result.current.actions.converse("build me a reviewer");
    });
    await act(async () => {
      await hook.result.current.actions.approvePlan();
    });

    await act(async () => {
      await hook.result.current.actions.acceptProposal("prop-1");
    });

    const transcript = hook.result.current.state.transcript;
    expect(transcript).toHaveLength(4);
    expect(transcript[3]).toMatchObject({ kind: "proposal", accepted: true });
    expect(hook.result.current.state.proposals).toEqual([]);

    // Switching projects is a real context change: the transcript resets.
    await act(async () => {
      await hook.result.current.actions.openProject("demo");
    });
    expect(hook.result.current.state.transcript).toEqual([]);
  });

  it("keeps the user's message visible when the turn fails", async () => {
    const { hook } = await conversationalStudio(WORKFLOW_BLUEPRINT);

    await act(async () => {
      await hook.result.current.actions.converse("build me a reviewer");
    });

    const transcript = hook.result.current.state.transcript;
    expect(transcript).toHaveLength(1);
    expect(transcript[0]).toMatchObject({ role: "user", content: "build me a reviewer" });
    expect(hook.result.current.state.toast?.marker).toBe("[xx]");
  });

  it("re-arms the plan gate when the build turn fails", async () => {
    const { hook, turns } = await conversationalStudio(WORKFLOW_BLUEPRINT);
    turns.push({
      kind: "plan",
      questions: [],
      plan: "1. one agent",
      planNote: null,
      proposal: null,
    });
    await act(async () => {
      await hook.result.current.actions.converse("build me a reviewer");
    });

    // No scripted turn is left, so the build call fails; the synthetic
    // approval message rolls back and the plan is the newest entry again.
    await act(async () => {
      await hook.result.current.actions.approvePlan();
    });

    const transcript = hook.result.current.state.transcript;
    expect(transcript).toHaveLength(2);
    expect(transcript[1]).toMatchObject({ kind: "plan" });
    expect(hook.result.current.state.toast?.marker).toBe("[xx]");
  });
});
