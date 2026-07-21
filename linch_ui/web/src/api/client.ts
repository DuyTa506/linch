import type {
  Blueprint,
  CapabilityCatalog,
  DirectoryExportResponse,
  ErrorBody,
  ExportPreviewResponse,
  LayoutDocument,
  ProjectDocument,
  ProjectSummary,
  ProposalResponse,
  ServiceInfo,
  SupportTurnRequest,
  SupportTurnResponse,
  StudioErrorCode,
  TurnMessage,
  TurnResponse,
  TurnToolCall,
  ValidationResponse,
} from "./types";

/** A tool-call start/end notice as it streams, before the turn is final. */
export type ToolCallStreamUpdate = Omit<TurnToolCall, "resultSummary">;

/**
 * A typed failure from the Studio API.
 *
 * The server always renders `{"error": {code, message, diagnostics, currentDigest}}`
 * and deliberately never reflects rejected values, so `message` is safe to show.
 */
export class StudioApiError extends Error {
  readonly status: number;
  readonly code: StudioErrorCode | string;
  readonly diagnostics: ErrorBody["diagnostics"];
  readonly currentDigest?: string;

  constructor(status: number, body: ErrorBody) {
    super(body.message);
    this.name = "StudioApiError";
    this.status = status;
    this.code = body.code;
    this.diagnostics = body.diagnostics ?? [];
    this.currentDigest = body.currentDigest ?? undefined;
  }

  is(code: StudioErrorCode): boolean {
    return this.code === code;
  }
}

export interface ZipExport {
  blob: Blob;
  filename: string;
  blueprintDigest: string | null;
}

interface ClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

const FALLBACK_ERROR: ErrorBody = {
  code: "server.error",
  message: "The Studio server returned an unreadable response.",
  diagnostics: [],
};

export function createStudioApi(options: ClientOptions = {}) {
  const baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);

  async function raise(response: Response): Promise<never> {
    let body: ErrorBody = FALLBACK_ERROR;
    try {
      const parsed = (await response.json()) as { error?: ErrorBody };
      if (parsed && typeof parsed === "object" && parsed.error) {
        body = parsed.error;
      }
    } catch {
      // Keep the fallback; never surface raw transport text.
    }
    throw new StudioApiError(response.status, body);
  }

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
    if (!response.ok) {
      await raise(response);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  const project = (id: string) => `/api/v1/projects/${encodeURIComponent(id)}`;

  return {
    serviceInfo: () => request<ServiceInfo>("/api/v1"),
    catalog: () => request<CapabilityCatalog>("/api/v1/catalog"),

    /** Global, project-optional documentation/implementation/pipeline support turn. */
    supportTurn: (body: SupportTurnRequest) =>
      request<SupportTurnResponse>("/api/v1/support/turns", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    listProjects: async (): Promise<ProjectSummary[]> =>
      (await request<{ projects: ProjectSummary[] }>("/api/v1/projects")).projects,

    createProject: (
      id: string,
      title?: string,
      template: "agent" | "goal_verified" | "directed_workflow" | "coordinator" | "routine" =
        "agent",
    ) =>
      request<ProjectDocument>("/api/v1/projects", {
        method: "POST",
        body: JSON.stringify({ id, ...(title ? { title } : {}), template }),
      }),

    openProject: (id: string) => request<ProjectDocument>(project(id)),

    /** Compare-and-swap write. A stale `baseDigest` raises `blueprint.stale_digest` (409). */
    saveBlueprint: (id: string, yaml: string, baseDigest: string) =>
      request<ProjectDocument>(`${project(id)}/blueprint`, {
        method: "PUT",
        body: JSON.stringify({ yaml, baseDigest }),
      }),

    validateBuffer: (yaml: string) =>
      request<ValidationResponse>("/api/v1/validate", {
        method: "POST",
        body: JSON.stringify({ yaml }),
      }),

    validateProject: (id: string) =>
      request<ValidationResponse>(`${project(id)}/validate`, { method: "POST" }),

    getLayout: (id: string) => request<{ id: string; layout: LayoutDocument }>(`${project(id)}/layout`),

    /** Takes a bare LayoutDocument but answers with `{id, layout}` — asymmetric by design. */
    saveLayout: (id: string, layout: LayoutDocument) =>
      request<{ id: string; layout: LayoutDocument }>(`${project(id)}/layout`, {
        method: "PUT",
        body: JSON.stringify(layout),
      }),

    previewExport: (id: string) =>
      request<ExportPreviewResponse>(`${project(id)}/export/preview`, { method: "POST" }),

    exportDirectory: (id: string, target: string) =>
      request<DirectoryExportResponse>(`${project(id)}/export/directory`, {
        method: "POST",
        body: JSON.stringify({ target }),
      }),

    exportZip: async (id: string): Promise<ZipExport> => {
      const response = await fetchImpl(`${baseUrl}${project(id)}/export/zip`, {
        method: "POST",
        headers: { Accept: "application/zip" },
      });
      if (!response.ok) {
        await raise(response);
      }
      return {
        blob: await response.blob(),
        filename: `${id}.zip`,
        blueprintDigest: response.headers.get("X-Linch-Blueprint-Digest"),
      };
    },

    listProposals: async (id: string): Promise<ProposalResponse[]> =>
      (await request<{ proposals: ProposalResponse[] }>(`${project(id)}/proposals`)).proposals,

    createProposal: (id: string, instruction: string) =>
      request<ProposalResponse>(`${project(id)}/proposals`, {
        method: "POST",
        body: JSON.stringify({ instruction }),
      }),

    /** One conversational turn; the client re-sends the whole transcript each time. */
    converse: (id: string, messages: TurnMessage[], stage: "chat" | "build" = "chat") =>
      request<TurnResponse>(`${project(id)}/authoring/turns`, {
        method: "POST",
        body: JSON.stringify({ messages, stage }),
      }),

    /**
     * The same turn over server-sent events: `thinking` deltas stream through
     * `onThinking` while the model reasons, `onToolCallStart`/`onToolCallEnd`
     * fire around each knowledge-tool call, and the promise settles with the
     * final turn (or a StudioApiError carrying the event's value-safe envelope).
     */
    converseStream: async (
      id: string,
      messages: TurnMessage[],
      stage: "chat" | "build" = "chat",
      handlers: {
        onThinking?: (text: string) => void;
        onToolCallStart?: (update: ToolCallStreamUpdate) => void;
        onToolCallEnd?: (update: ToolCallStreamUpdate) => void;
      } = {},
    ): Promise<TurnResponse> => {
      const response = await fetchImpl(`${baseUrl}${project(id)}/authoring/turns/stream`, {
        method: "POST",
        headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
        body: JSON.stringify({ messages, stage }),
      });
      if (!response.ok) {
        await raise(response);
      }
      const reader = response.body?.getReader();
      if (!reader) {
        throw new StudioApiError(502, FALLBACK_ERROR);
      }
      const decoder = new TextDecoder();
      let buffer = "";
      let final: TurnResponse | null = null;

      const handleBlock = (block: string) => {
        let event = "";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice("event: ".length);
          else if (line.startsWith("data: ")) data = line.slice("data: ".length);
        }
        if (!event || !data) return;
        let payload: unknown;
        try {
          payload = JSON.parse(data) as unknown;
        } catch {
          throw new StudioApiError(502, FALLBACK_ERROR);
        }
        if (event === "thinking") {
          const text = (payload as { text?: unknown }).text;
          if (typeof text === "string") handlers.onThinking?.(text);
          return;
        }
        if (event === "tool_call_start") {
          handlers.onToolCallStart?.(payload as ToolCallStreamUpdate);
          return;
        }
        if (event === "tool_call_end") {
          handlers.onToolCallEnd?.(payload as ToolCallStreamUpdate);
          return;
        }
        if (event === "turn") {
          final = payload as TurnResponse;
          return;
        }
        if (event === "error") {
          const body = payload as { status?: number; error?: ErrorBody };
          throw new StudioApiError(body.status ?? 502, body.error ?? FALLBACK_ERROR);
        }
      };

      try {
        while (final === null) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            handleBlock(block);
            boundary = buffer.indexOf("\n\n");
          }
        }
      } finally {
        // Releasing the stream aborts the server-side run on early exits.
        void reader.cancel().catch(() => undefined);
      }
      if (final === null) {
        throw new StudioApiError(502, FALLBACK_ERROR);
      }
      return final;
    },

    acceptProposal: (id: string, proposalId: string) =>
      request<ProjectDocument>(
        `${project(id)}/proposals/${encodeURIComponent(proposalId)}/accept`,
        { method: "POST" },
      ),

    rejectProposal: (id: string, proposalId: string) =>
      request<void>(`${project(id)}/proposals/${encodeURIComponent(proposalId)}/reject`, {
        method: "POST",
      }),

    blueprintSchema: () => request<Record<string, unknown>>("/api/v1/schema"),
  };
}

export type StudioApi = ReturnType<typeof createStudioApi>;
export type { Blueprint };
export const studioApi = createStudioApi();
