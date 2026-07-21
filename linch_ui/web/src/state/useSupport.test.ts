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
    const supportTurn = vi.fn().mockResolvedValue(CONFIRMATION);
    const api = {
      serviceInfo: vi.fn().mockResolvedValue({ supportAvailable: true }),
      supportTurn,
    } as unknown as StudioApi;
    const hook = renderHook(() => useSupport(api));

    await waitFor(() => expect(hook.result.current.state.loading).toBe(false));
    expect(hook.result.current.state.available).toBe(true);

    await act(async () => {
      await hook.result.current.actions.run("Build a CI review pipeline.", { projectId: "demo" });
    });
    expect(supportTurn).toHaveBeenLastCalledWith({
      messages: [{ role: "user", content: "Build a CI review pipeline." }],
      requestedMode: "auto",
      pipelineConfirmed: false,
      projectId: "demo",
      stage: "chat",
      approvedPlanDigest: null,
    });

    await act(async () => {
      await hook.result.current.actions.confirmPipeline("demo");
    });
    expect(supportTurn).toHaveBeenLastCalledWith({
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
    });
  });
});
