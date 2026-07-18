import { useCallback, useEffect, useState } from "react";

import { StudioApiError, type StudioApi } from "../api/client";
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

export interface SupportState {
  available: boolean;
  loading: boolean;
  busy: boolean;
  transcript: SupportTranscriptEntry[];
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
    error: null,
  });

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
      setState((current) => ({ ...current, busy: true, error: null, transcript: [...current.transcript, user] }));
      try {
        const response = await api.supportTurn({
          messages,
          requestedMode: options.requestedMode ?? "auto",
          pipelineConfirmed: options.pipelineConfirmed ?? false,
          projectId: options.projectId ?? null,
          stage: options.stage ?? "chat",
          approvedPlanDigest: options.approvedPlanDigest ?? null,
        });
        const assistant: SupportTranscriptEntry = {
          role: "assistant",
          content: responseContent(response),
          response,
        };
        setState((current) => ({ ...current, transcript: [...current.transcript, assistant] }));
        return response;
      } catch (error) {
        const message = error instanceof StudioApiError ? error.message : "Support could not answer this request.";
        setState((current) => ({ ...current, error: message }));
        return null;
      } finally {
        setState((current) => ({ ...current, busy: false }));
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
    setState((current) => ({ ...current, transcript: [], error: null }));
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
