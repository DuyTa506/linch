import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { StudioApi } from "../api/client";
import type { SupportTurnResponse } from "../api/types";
import { useSupport } from "./useSupport";

const CONFIRMATION: SupportTurnResponse = {
  kind: "mode_confirmation",
  mode: "pipeline",
  answer: "Confirm before I enter the reviewable flow.",
  pipelineIntent: {
    summary: "A CI review pipeline.",
    proposedComponents: ["CI trigger", "parallel reviewers"],
    requiresProject: true,
  },
  evidence: [],
  coverage: "partial",
  questions: [],
  toolCalls: [],
};

describe("useSupport", () => {
  it("keeps the global transcript and only enters pipeline authoring after confirmation", async () => {
    const supportTurnStream = vi.fn().mockResolvedValue(CONFIRMATION);
    const api = {
      serviceInfo: vi.fn().mockResolvedValue({ supportAvailable: true }),
      supportTurnStream,
    } as unknown as StudioApi;
    const hook = renderHook(() => useSupport(api));

    await waitFor(() => expect(hook.result.current.state.loading).toBe(false));
    expect(hook.result.current.state.available).toBe(true);

    await act(async () => {
      await hook.result.current.actions.run("Build a CI review pipeline.", { projectId: "demo" });
    });
    expect(supportTurnStream).toHaveBeenLastCalledWith(
      {
        messages: [{ role: "user", content: "Build a CI review pipeline." }],
        requestedMode: "auto",
        pipelineConfirmed: false,
        projectId: "demo",
        stage: "chat",
        approvedPlanDigest: null,
      },
      expect.any(Object),
    );

    await act(async () => {
      await hook.result.current.actions.confirmPipeline("demo");
    });
    expect(supportTurnStream).toHaveBeenLastCalledWith(
      {
        messages: [
          { role: "user", content: "Build a CI review pipeline." },
          { role: "assistant", content: "Confirm before I enter the reviewable flow." },
          { role: "user", content: "Proceed with pipeline authoring." },
        ],
        requestedMode: "pipeline",
        pipelineConfirmed: true,
        projectId: "demo",
        stage: "chat",
        approvedPlanDigest: null,
      },
      expect.any(Object),
    );
  });

  it("exposes progressive response text before replacing it with the validated final turn", async () => {
    let resolveTurn: ((response: SupportTurnResponse) => void) | undefined;
    const pendingTurn = new Promise<SupportTurnResponse>((resolve) => {
      resolveTurn = resolve;
    });
    const supportTurnStream = vi.fn(
      (
        _request: unknown,
        handlers: {
          onResponseDelta?: (text: string) => void;
          onToolCallStart?: (update: {
            toolUseId: string;
            toolName: string;
            summary: string;
            detail: string | null;
            isError: boolean;
            durationMs: number;
          }) => void;
          onToolCallEnd?: (update: {
            toolUseId: string;
            toolName: string;
            summary: string;
            detail: string | null;
            isError: boolean;
            durationMs: number;
          }) => void;
        },
      ) => {
        handlers.onToolCallStart?.({
          toolUseId: "call_1",
          toolName: "search_docs",
          summary: "Searching docs",
          detail: null,
          isError: false,
          durationMs: 0,
        });
        handlers.onResponseDelta?.('{"kind":"answer","answer":"Use the ');
        handlers.onResponseDelta?.('documented workflow API."}');
        handlers.onToolCallEnd?.({
          toolUseId: "call_1",
          toolName: "search_docs",
          summary: "1 documented result",
          detail: "usage/workflows.md",
          isError: false,
          durationMs: 12,
        });
        return pendingTurn;
      },
    );
    const api = {
      serviceInfo: vi.fn().mockResolvedValue({ supportAvailable: true }),
      supportTurnStream,
    } as unknown as StudioApi;
    const hook = renderHook(() => useSupport(api));

    await waitFor(() => expect(hook.result.current.state.loading).toBe(false));
    let run: Promise<SupportTurnResponse | null> | undefined;
    await act(async () => {
      run = hook.result.current.actions.run("How do workflows work?");
      await Promise.resolve();
    });

    expect(supportTurnStream).toHaveBeenCalledTimes(1);
    expect(hook.result.current.state.busy).toBe(true);
    expect(hook.result.current.state.transcript).toEqual([
      { role: "user", content: "How do workflows work?" },
    ]);
    expect(hook.result.current.state.responseDraft).toBe(
      '{"kind":"answer","answer":"Use the documented workflow API."}',
    );
    expect(hook.result.current.state.streamingToolCalls).toEqual([
      {
        toolUseId: "call_1",
        toolName: "search_docs",
        summary: "Searching docs",
        resultSummary: "1 documented result",
        detail: "usage/workflows.md",
        isError: false,
        durationMs: 12,
        status: "completed",
      },
    ]);

    await act(async () => {
      resolveTurn?.({
        ...CONFIRMATION,
        kind: "answer",
        mode: "documentation",
        answer: "Use the documented workflow API.",
        pipelineIntent: undefined,
      });
      await run;
    });

    expect(hook.result.current.state.busy).toBe(false);
    expect(hook.result.current.state.responseDraft).toBeNull();
    expect(hook.result.current.state.streamingToolCalls).toBeNull();
    expect(hook.result.current.state.transcript).toEqual([
      { role: "user", content: "How do workflows work?" },
      {
        role: "assistant",
        content: "Use the documented workflow API.",
        response: expect.objectContaining({ kind: "answer" }),
      },
    ]);
  });
});
