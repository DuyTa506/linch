import { useCallback, useEffect, useRef, useState } from "react";

import { StudioApiError, type StudioApi, type ToolCallStreamUpdate } from "../api/client";
import type {
  ResolvedSupportMode,
  SupportMode,
  SupportTurnResponse,
  TurnMessage,
} from "../api/types";

export interface SupportTranscriptEntry {
  role: "user" | "assistant";
  content: string;
  response?: SupportTurnResponse;
}

export interface StreamingSupportToolCall extends ToolCallStreamUpdate {
  status: "running" | "completed" | "error";
  resultSummary?: string | null;
}

export interface SupportState {
  available: boolean;
  loading: boolean;
  busy: boolean;
  transcript: SupportTranscriptEntry[];
  /** Raw structured-output fragments; components must render an extracted preview only. */
  responseDraft: string | null;
  streamingToolCalls: StreamingSupportToolCall[] | null;
  error: string | null;
}

function responseContent(response: SupportTurnResponse): string {
  if (response.kind === "answer") return response.answer ?? "";
  if (response.kind === "recipe") return response.recipe?.overview ?? "";
  if (response.kind === "plan") return response.plan ?? "";
  if (response.kind === "questions") {
    return response.questions.map((item) => `${item.question} (${item.options.join(" / ")})`).join("\n");
  }
  if (response.kind === "proposal") return response.proposal?.summary ?? "Pipeline proposal ready.";
  return response.answer ?? response.pipelineIntent?.summary ?? "";
}

export function useSupport(api: StudioApi) {
  const [state, setState] = useState<SupportState>({
    available: false,
    loading: true,
    busy: false,
    transcript: [],
    responseDraft: null,
    streamingToolCalls: null,
    error: null,
  });
  const turnToken = useRef(0);

  useEffect(() => {
    let cancelled = false;
    void api
      .serviceInfo()
      .then((info) => {
        if (!cancelled) setState((current) => ({ ...current, available: info.supportAvailable, loading: false }));
      })
      .catch(() => {
        if (!cancelled) setState((current) => ({ ...current, loading: false }));
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const run = useCallback(
    async (
      text: string,
      options: {
        projectId?: string | null;
        requestedMode?: SupportMode;
        pipelineConfirmed?: boolean;
        stage?: "chat" | "build";
        approvedPlanDigest?: string | null;
      } = {},
    ): Promise<SupportTurnResponse | null> => {
      const content = text.trim();
      if (!content || state.busy) return null;
      const user: SupportTranscriptEntry = { role: "user", content };
      const messages: TurnMessage[] = [...state.transcript, user].map((entry) => ({
        role: entry.role === "user" ? "user" : "assistant",
        content: entry.content,
      }));
      const token = ++turnToken.current;
      const isCurrent = () => turnToken.current === token;
      setState((current) => ({
        ...current,
        busy: true,
        error: null,
        transcript: [...current.transcript, user],
        responseDraft: "",
        streamingToolCalls: [],
      }));
      try {
        const response = await api.supportTurnStream(
          {
            messages,
            requestedMode: options.requestedMode ?? "auto",
            pipelineConfirmed: options.pipelineConfirmed ?? false,
            projectId: options.projectId ?? null,
            stage: options.stage ?? "chat",
            approvedPlanDigest: options.approvedPlanDigest ?? null,
          },
          {
            onResponseDelta: (text) => {
              if (!isCurrent()) return;
              setState((current) => ({
                ...current,
                responseDraft: (current.responseDraft ?? "") + text,
              }));
            },
            onToolCallStart: (update) => {
              if (!isCurrent()) return;
              setState((current) => ({
                ...current,
                streamingToolCalls: [
                  ...(current.streamingToolCalls ?? []),
                  { ...update, status: "running" },
                ],
              }));
            },
            onToolCallEnd: (update) => {
              if (!isCurrent()) return;
              setState((current) => {
                const calls = current.streamingToolCalls ?? [];
                const completed: StreamingSupportToolCall = {
                  ...update,
                  status: update.isError ? "error" : "completed",
                  resultSummary: update.summary,
                };
                const existing = calls.findIndex((item) => item.toolUseId === update.toolUseId);
                const streamingToolCalls =
                  existing < 0
                    ? [...calls, completed]
                    : calls.map((item, index) =>
                        index === existing
                          ? { ...completed, summary: item.summary }
                          : item,
                      );
                return { ...current, streamingToolCalls };
              });
            },
          },
        );
        if (!isCurrent()) return null;
        const assistant: SupportTranscriptEntry = {
          role: "assistant",
          content: responseContent(response),
          response,
        };
        setState((current) => ({
          ...current,
          transcript: [...current.transcript, assistant],
          responseDraft: null,
          streamingToolCalls: null,
        }));
        return response;
      } catch (error) {
        if (!isCurrent()) return null;
        const message = error instanceof StudioApiError ? error.message : "Support could not answer this request.";
        setState((current) => ({
          ...current,
          error: message,
          responseDraft: null,
          streamingToolCalls: null,
        }));
        return null;
      } finally {
        if (isCurrent()) setState((current) => ({ ...current, busy: false }));
      }
    },
    [api, state.busy, state.transcript],
  );

  const confirmPipeline = useCallback(
    (projectId?: string | null) =>
      run("Proceed with pipeline authoring.", {
        requestedMode: "pipeline",
        pipelineConfirmed: true,
        projectId,
      }),
    [run],
  );

  const turnRecipeIntoPipeline = useCallback(
    (projectId?: string | null) =>
      run("Turn this implementation into a reviewable Studio pipeline.", {
        requestedMode: "pipeline",
        pipelineConfirmed: true,
        projectId,
      }),
    [run],
  );

  const answerPipelineQuestions = useCallback(
    (answer: string, projectId?: string | null) =>
      run(answer, { requestedMode: "pipeline", pipelineConfirmed: true, projectId }),
    [run],
  );

  const approvePipelinePlan = useCallback(
    (projectId?: string | null) => {
      const lastPlan = [...state.transcript]
        .reverse()
        .find((entry) => entry.response?.kind === "plan")?.response;
      if (!lastPlan?.planDigest) return Promise.resolve(null);
      return run("Proceed with this plan.", {
        requestedMode: "pipeline",
        pipelineConfirmed: true,
        projectId,
        stage: "build",
        approvedPlanDigest: lastPlan.planDigest,
      });
    },
    [run, state.transcript],
  );

  const clear = useCallback(() => {
    turnToken.current += 1;
    setState((current) => ({
      ...current,
      busy: false,
      transcript: [],
      responseDraft: null,
      streamingToolCalls: null,
      error: null,
    }));
  }, []);

  return {
    state,
    actions: {
      run,
      confirmPipeline,
      turnRecipeIntoPipeline,
      answerPipelineQuestions,
      approvePipelinePlan,
      clear,
    },
  };
}

export type Support = ReturnType<typeof useSupport>;
export type { ResolvedSupportMode };
