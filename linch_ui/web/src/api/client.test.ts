import { describe, expect, it, vi } from "vitest";

import { StudioApiError, createStudioApi } from "./client";

/** A minimal Response standing in for a server-sent-event stream. */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const queue = [...chunks];
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "text/event-stream" }),
    body: {
      getReader: () => ({
        read: async () =>
          queue.length > 0
            ? { done: false, value: encoder.encode(queue.shift() ?? "") }
            : { done: true, value: undefined },
        cancel: async () => undefined,
      }),
    },
  } as unknown as Response;
}

function jsonError(status: number, code: string): Response {
  return {
    ok: false,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => ({ error: { code, message: "nope", diagnostics: [] } }),
  } as unknown as Response;
}

describe("converseStream", () => {
  it("delivers thinking deltas as they arrive and resolves with the final turn", async () => {
    const turn =
      'event: turn\ndata: {"kind":"questions","questions":[{"question":"Which?","options":["a","b"]}],' +
      '"plan":null,"planNote":"note","proposal":null,"thinking":"first second"}\n\n';
    const api = createStudioApi({
      fetchImpl: async () =>
        sseResponse([
          'event: thinking\ndata: {"text":"first "}\n\n',
          // A delta split across transport chunks must reassemble.
          'event: thinking\ndata: {"tex',
           't":"second"}\n\n' + turn,
        ]),
    });
    const seen: string[] = [];

    const result = await api.converseStream(
      "demo",
      [{ role: "user", content: "go" }],
      "chat",
      { onThinking: (text) => seen.push(text) },
    );

    expect(seen).toEqual(["first ", "second"]);
    expect(result.kind).toBe("questions");
    expect(result.thinking).toBe("first second");
  });

  it("rejects with the value-safe error event, diagnostics included", async () => {
    const api = createStudioApi({
      fetchImpl: async () =>
        sseResponse([
          'event: thinking\ndata: {"text":"half a thought"}\n\n',
          'event: error\ndata: {"status":502,"error":{"code":"authoring.failed",' +
            '"message":"The authoring provider did not return a valid proposal.",' +
            '"diagnostics":[{"code":"authoring.malformed_proposal","severity":"error",' +
            '"path":"/","message":"m","remediation":"r"}]}}\n\n',
        ]),
    });

    const failure = await api
      .converseStream("demo", [{ role: "user", content: "go" }], "build", {})
      .then(
        () => null,
        (error: unknown) => error,
      );

    expect(failure).toBeInstanceOf(StudioApiError);
    const typed = failure as StudioApiError;
    expect(typed.status).toBe(502);
    expect(typed.code).toBe("authoring.failed");
    expect(typed.diagnostics).toHaveLength(1);
  });

  it("maps a pre-stream HTTP failure through the normal error path", async () => {
    const api = createStudioApi({ fetchImpl: async () => jsonError(501, "authoring.unavailable") });

    const failure = await api
      .converseStream("demo", [{ role: "user", content: "go" }], "chat", {})
      .then(
        () => null,
        (error: unknown) => error,
      );

    expect(failure).toBeInstanceOf(StudioApiError);
    expect((failure as StudioApiError).is("authoring.unavailable")).toBe(true);
  });

  it("delivers tool-call start/end notices as they arrive, before the final turn", async () => {
    const turn =
      'event: turn\ndata: {"kind":"questions","questions":[],"plan":null,"planNote":null,' +
      '"proposal":null,"thinking":null,' +
      '"toolCalls":[{"toolUseId":"call_1","toolName":"search_docs","summary":"3 results",' +
      '"resultSummary":"3 results","detail":"full text","isError":false,"durationMs":42}]}\n\n';
    const api = createStudioApi({
      fetchImpl: async () =>
        sseResponse([
          'event: tool_call_start\ndata: {"toolUseId":"call_1","toolName":"search_docs",' +
            '"summary":"search_docs: fan-out","detail":null,"isError":false,"durationMs":0}\n\n',
          'event: tool_call_end\ndata: {"toolUseId":"call_1","toolName":"search_docs",' +
            '"summary":"3 results","detail":"full text","isError":false,"durationMs":42}\n\n' +
            turn,
        ]),
    });
    const starts: string[] = [];
    const ends: string[] = [];

    const result = await api.converseStream(
      "demo",
      [{ role: "user", content: "go" }],
      "chat",
      {
        onToolCallStart: (update) => starts.push(update.summary),
        onToolCallEnd: (update) => ends.push(update.summary),
      },
    );

    expect(starts).toEqual(["search_docs: fan-out"]);
    expect(ends).toEqual(["3 results"]);
    expect(result.toolCalls).toHaveLength(1);
    expect(result.toolCalls?.[0]).toMatchObject({ toolUseId: "call_1", resultSummary: "3 results" });
  });
});

describe("supportTurnStream", () => {
  it("delivers response deltas and tool notices before resolving the final support turn", async () => {
    const turn =
      'event: turn\ndata: {"kind":"answer","mode":"documentation","answer":"Use the documented workflow API.",' +
      '"recipe":null,"pipelineIntent":null,"evidence":[],"coverage":"documented","followUp":null,' +
      '"questions":[],"plan":null,"planDigest":null,"proposal":null,' +
      '"toolCalls":[{"toolUseId":"call_1","toolName":"search_docs","summary":"1 result",' +
      '"resultSummary":"1 result","detail":"workflow section","isError":false,"durationMs":42}]}\n\n';
    const fetchImpl = vi.fn(async () =>
      sseResponse([
        'event: response_delta\ndata: {"text":"Use the "}\n\n' +
          'event: tool_call_start\ndata: {"toolUseId":"call_1","toolName":"search_docs",' +
          '"summary":"Searching docs","detail":null,"isError":false,"durationMs":0}\n\n',
        // The response delta is deliberately split across transport chunks.
        'event: response_delta\ndata: {"text":"documented work',
        'flow API."}\n\n' +
          'event: tool_call_end\ndata: {"toolUseId":"call_1","toolName":"search_docs",' +
          '"summary":"1 result","detail":"workflow section","isError":false,"durationMs":42}\n\n' +
          turn,
      ]),
    );
    const api = createStudioApi({ fetchImpl });
    const timeline: string[] = [];

    const result = await api.supportTurnStream(
      {
        messages: [{ role: "user", content: "How do workflows work?" }],
        requestedMode: "documentation",
      },
      {
        onResponseDelta: (text) => timeline.push(`text:${text}`),
        onToolCallStart: (update) => timeline.push(`start:${update.summary}`),
        onToolCallEnd: (update) => timeline.push(`end:${update.summary}`),
      },
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/support/turns/stream",
      expect.objectContaining({
        method: "POST",
        headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: [{ role: "user", content: "How do workflows work?" }],
          requestedMode: "documentation",
        }),
      }),
    );
    expect(timeline).toEqual([
      "text:Use the ",
      "start:Searching docs",
      "text:documented workflow API.",
      "end:1 result",
    ]);
    expect(result).toMatchObject({
      kind: "answer",
      answer: "Use the documented workflow API.",
      toolCalls: [{ toolUseId: "call_1", resultSummary: "1 result" }],
    });
  });
});
