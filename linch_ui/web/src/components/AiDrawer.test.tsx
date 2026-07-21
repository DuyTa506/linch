import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ProposalResponse } from "../api/types";
import { WORKFLOW_BLUEPRINT } from "../model/fixtures";
import type { Studio, StudioState, TranscriptEntry, TranscriptToolCall } from "../state/useStudio";
import { AiDrawer } from "./AiDrawer";

function proposalFor(id: string): ProposalResponse {
  return {
    id,
    projectId: "demo",
    baseDigest: "a".repeat(64),
    candidateDigest: "b".repeat(64),
    candidate: WORKFLOW_BLUEPRINT,
    semanticDiff: [
      { operation: "replace", path: "/metadata/title", before: "Demo", after: "Better" },
    ],
    diagnostics: [],
    exportReady: true,
    createdAt: "2026-07-17T00:00:00Z",
    summary: "retitled the project",
  };
}

function studioWith(
  transcript: TranscriptEntry[],
  proposals: ProposalResponse[] = [],
  streamingThinking: string | null = null,
  streamingToolCalls: TranscriptToolCall[] | null = null,
): Studio {
  const state = {
    authoringAvailable: true,
    doc: { id: "demo", digest: "a".repeat(64) },
    proposals,
    transcript,
    streamingThinking,
    streamingToolCalls,
  } as unknown as StudioState;
  const actions = {
    converse: vi.fn(),
    approvePlan: vi.fn(),
    propose: vi.fn(),
    acceptProposal: vi.fn(),
    rejectProposal: vi.fn(),
  } as unknown as Studio["actions"];
  return { state, blueprint: null, actions } as Studio;
}

describe("AiDrawer transcript", () => {
  it("renders user and question bubbles in order", () => {
    const studio = studioWith([
      { role: "user", kind: "text", content: "build me a reviewer" },
      {
        role: "agent",
        kind: "questions",
        content: "plan: one agent\n1. Which provider?",
        note: "plan: one agent",
        questions: [
          { question: "Which provider?", options: ["openai", "anthropic", "a local model"] },
          { question: "How often should it run?", options: ["nightly", "hourly"] },
        ],
        thinking: "the user has no trigger yet, so ask about cadence",
      },
    ]);

    const { container } = render(<AiDrawer studio={studio} onClose={() => {}} />);

    expect(screen.getByText("build me a reviewer")).toBeTruthy();
    expect(screen.getByText("plan: one agent")).toBeTruthy();
    expect(screen.getByText("Which provider?")).toBeTruthy();
    expect(screen.getByText("How often should it run?")).toBeTruthy();
    // Every model option is clickable, and the UI adds its own free-form one.
    expect(screen.getByText("openai")).toBeTruthy();
    expect(screen.getByText("anthropic")).toBeTruthy();
    expect(screen.getAllByText("other…")).toHaveLength(2);
    expect(container.querySelector(".msg--user")?.textContent).toBe("build me a reviewer");
    expect(container.querySelectorAll(".msg--agent")).toHaveLength(1);
    // Reasoning renders dimmed and collapsed; expanding reveals the trace.
    const details = container.querySelector("details.msg__thinking");
    expect(details).toBeTruthy();
    expect(details?.textContent).toContain("the user has no trigger yet, so ask about cadence");
    expect(details?.querySelector("summary")?.textContent).toContain("thinking");
  });

  it("collects option clicks and a free answer, then sends one combined message", () => {
    const studio = studioWith([
      { role: "user", kind: "text", content: "build me a reviewer" },
      {
        role: "agent",
        kind: "questions",
        content: "1. Which provider?\n2. How often should it run?",
        questions: [
          { question: "Which provider?", options: ["openai", "anthropic"] },
          { question: "How often should it run?", options: ["nightly", "hourly"] },
        ],
      },
    ]);

    render(<AiDrawer studio={studio} onClose={() => {}} />);

    const sendAnswers = screen.getByText("send answers");
    expect(sendAnswers.closest("button")?.disabled).toBe(true);
    fireEvent.click(screen.getByText("openai"));
    fireEvent.click(screen.getAllByText("other…")[1]);
    fireEvent.change(screen.getByPlaceholderText("type your own answer"), {
      target: { value: "every monday" },
    });
    fireEvent.click(sendAnswers);

    expect(studio.actions.converse).toHaveBeenCalledWith(
      "Which provider? → openai\nHow often should it run? → every monday",
    );
  });

  it("shows the reasoning stream live while a turn is in flight", () => {
    const studio = studioWith(
      [{ role: "user", kind: "text", content: "build me a reviewer" }],
      [],
      "weighing cron against webhook",
    );

    const { container } = render(<AiDrawer studio={studio} onClose={() => {}} />);

    const live = container.querySelector(".msg--stream");
    expect(live).toBeTruthy();
    expect(live?.textContent).toContain("thinking");
    expect(live?.textContent).toContain("weighing cron against webhook");
    // The live trace is already open — no collapsed drop-down mid-stream.
    expect(live?.querySelector("details")).toBeNull();
  });

  it("renders the plan with an approve gate at the newest turn", () => {
    const studio = studioWith([
      { role: "user", kind: "text", content: "openai please" },
      {
        role: "agent",
        kind: "plan",
        content: "1. one agent\n2. nightly cron",
        plan: "1. one agent\n2. nightly cron",
        note: "approve to build",
      },
    ]);

    render(<AiDrawer studio={studio} onClose={() => {}} />);

    expect(screen.getByText(/1\. one agent/)).toBeTruthy();
    expect(screen.getByText("approve to build")).toBeTruthy();
    // The user either approves the plan or keeps editing through the input.
    fireEvent.click(screen.getByText("build this plan"));
    expect(studio.actions.approvePlan).toHaveBeenCalled();
  });

  it("renders the proposal card at its turn in the conversation", () => {
    const studio = studioWith(
      [
        { role: "user", kind: "text", content: "openai please" },
        {
          role: "agent",
          kind: "proposal",
          content: "retitled the project",
          note: "delivering the agreed design",
          proposalId: "prop-1",
        },
      ],
      [proposalFor("prop-1")],
    );

    render(<AiDrawer studio={studio} onClose={() => {}} />);

    expect(screen.getByText("delivering the agreed design")).toBeTruthy();
    expect(screen.getByText("retitled the project")).toBeTruthy();
    expect(screen.getByText("accept proposal")).toBeTruthy();
    // The recomputed diff shows the actual values, not only the path.
    expect(screen.getByText(/"Demo"/)).toBeTruthy();
    expect(screen.getByText(/"Better"/)).toBeTruthy();
  });

  it("marks an accepted turn whose proposal is no longer pending", () => {
    const studio = studioWith([
      {
        role: "agent",
        kind: "proposal",
        content: "retitled the project",
        proposalId: "prop-1",
        accepted: true,
      },
    ]);

    render(<AiDrawer studio={studio} onClose={() => {}} />);

    expect(screen.getByText(/accepted into the blueprint/)).toBeTruthy();
    expect(screen.queryByText("accept proposal")).toBeNull();
  });

  it("renders a completed tool call as one compact line, collapsed until expanded", () => {
    const studio = studioWith([
      { role: "user", kind: "text", content: "build me a reviewer" },
      {
        role: "agent",
        kind: "questions",
        content: "1. Which provider?",
        questions: [{ question: "Which provider?", options: ["openai", "anthropic"] }],
        toolCalls: [
          {
            toolUseId: "call_1",
            toolName: "search_docs",
            summary: "search_docs: fan-out",
            status: "done",
            resultSummary: "3 results",
            detail: "full section text goes here",
            durationMs: 42,
          },
        ],
      },
    ]);

    const { container } = render(<AiDrawer studio={studio} onClose={() => {}} />);

    const details = container.querySelector("details.msg__tool-call");
    expect(details).toBeTruthy();
    expect(details?.hasAttribute("open")).toBe(false);
    // Both the pre-call summary and the post-call result sit on the one line.
    const summary = details?.querySelector("summary");
    expect(summary?.textContent).toContain("search_docs: fan-out");
    expect(summary?.textContent).toContain("3 results");
    expect(details?.textContent).toContain("full section text goes here");

    fireEvent.click(summary!);
    expect(details?.hasAttribute("open")).toBe(true);

    // The app's UI is plain-text/bracket markers only — never emoji.
    expect(container.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });

  it("shows a running tool call as a single live line with no expand affordance yet", () => {
    const studio = studioWith(
      [{ role: "user", kind: "text", content: "build me a reviewer" }],
      [],
      "",
      [{ toolUseId: "call_1", toolName: "search_docs", summary: "search_docs: fan-out", status: "running" }],
    );

    const { container } = render(<AiDrawer studio={studio} onClose={() => {}} />);

    expect(container.querySelector("div.msg__tool-call")).toBeTruthy();
    expect(container.querySelector("details.msg__tool-call")).toBeNull();
    expect(container.textContent).toContain("search_docs: fan-out");
    expect(container.textContent).toContain("running");
    expect(container.querySelector(".msg__tool-call .blink")).toBeTruthy();

    expect(container.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });
});
