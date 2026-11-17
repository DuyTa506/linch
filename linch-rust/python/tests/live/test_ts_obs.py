"""Live toolsec: OpenTelemetry spans of a real run, exported as OTLP/JSON to a local receiver.

Spans are built by the Python client (`linch_rust.otel.Telemetry`), not by the server: see
`toolsec/expectations.md` B1 and section 8.1. The OTLP exporter packages are not installed, so the
exporter here serializes finished spans as OTLP/JSON and POSTs them to a receiver started by the test.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from ts_support import (
    FakeModel,
    ends,
    fake_config,
    finish,
    needs_server,
    pytestmark,
    record,
    server_env,
)

from linch_rust import Server, UsageEvent, tool
from linch_rust.otel import Telemetry

__all__ = ["pytestmark"]

AREA = "otel"


def _any(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


class OtlpJsonExporter(SpanExporter):
    """POSTs finished spans to `url` as OTLP/JSON (`/v1/traces`)."""

    def __init__(self, url: str) -> None:
        self.url = url

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        encoded = []
        for span in spans:
            ctx = span.get_span_context()
            encoded.append(
                {
                    "traceId": f"{ctx.trace_id:032x}",
                    "spanId": f"{ctx.span_id:016x}",
                    "parentSpanId": f"{span.parent.span_id:016x}" if span.parent else "",
                    "name": span.name,
                    "kind": int(span.kind.value) + 1,
                    "startTimeUnixNano": str(span.start_time),
                    "endTimeUnixNano": str(span.end_time),
                    "attributes": [
                        {"key": k, "value": _any(v)} for k, v in (span.attributes or {}).items()
                    ],
                    "status": {"code": int(span.status.status_code.value)},
                }
            )
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": _any("toolsec")}]},
                    "scopeSpans": [{"scope": {"name": "linch"}, "spans": encoded}],
                }
            ]
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as answer:
            return SpanExportResult.SUCCESS if answer.status == 200 else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass


class Receiver:
    """A tiny OTLP/HTTP receiver: stores every `POST /v1/traces` body."""

    def __init__(self) -> None:
        self.bodies: list[tuple[str, dict[str, str], bytes]] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> Receiver:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {
                k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.bodies.append((path, headers, body))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
            )
            await writer.drain()
        finally:
            writer.close()

    def spans(self) -> list[dict[str, Any]]:
        out = []
        for path, _, body in self.bodies:
            if path != "/v1/traces":
                continue
            for resource in json.loads(body)["resourceSpans"]:
                for scope in resource["scopeSpans"]:
                    out.extend(scope["spans"])
        return out


def attrs(span: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for item in span["attributes"]:
        ((kind, value),) = item["value"].items()
        out[item["key"]] = int(value) if kind == "intValue" else value
    return out


@needs_server
async def test_genai_spans_with_usage_reach_an_otlp_receiver() -> None:
    @tool(scope="read")
    async def lookup(email: str) -> str:
        """Find a customer."""
        return "Ada Lovelace"

    def handler(seen: Any) -> Any:
        if seen.body["messages"][-1]["role"] == "tool":
            return ("text", "Ada has been a customer.")
        return ("tools", [("lookup", {"email": "ada@example.com"})])

    model = await FakeModel(handler).start()
    receiver = await Receiver().start()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OtlpJsonExporter(f"http://127.0.0.1:{receiver.port}/v1/traces"))
    )
    reader = InMemoryMetricReader()
    telemetry = Telemetry(
        tracer_provider=tracer_provider, meter_provider=MeterProvider(metric_readers=[reader])
    )
    try:
        async with Server(env=server_env()) as server:
            agent = await server.open_agent(fake_config(model), tools=[lookup], telemetry=telemetry)
            tracer = tracer_provider.get_tracer("toolsec")
            with tracer.start_as_current_span("POST /ask"):
                run = await agent.run("s1", "Who is ada?", run_id="otel-1")
            envelopes = await finish(agent, run)
        await asyncio.get_running_loop().run_in_executor(None, tracer_provider.force_flush)
        await asyncio.sleep(0.3)
    finally:
        await receiver.stop()
        await model.stop()

    spans = receiver.spans()
    names = sorted({s["name"] for s in spans})
    usage = [e.event for e in envelopes if isinstance(e.event, UsageEvent)]
    in_total = sum(u.usage["input_tokens"] for u in usage)
    out_total = sum(u.usage["output_tokens"] for u in usage)
    by_name = {s["name"]: attrs(s) for s in spans}
    chats = [attrs(s) for s in spans if s["name"].startswith("chat ")]
    tool_span = next(s for s in spans if s["name"].startswith("execute_tool"))
    invoke = next(s for s in spans if s["name"] == "invoke_agent")
    record(
        AREA,
        "8.1",
        posts=len(receiver.bodies),
        paths=sorted({p for p, _, _ in receiver.bodies}),
        span_names=names,
        invoke_agent=by_name["invoke_agent"],
        chat_spans=chats,
        execute_tool=attrs(tool_span),
        usage_events=[(u.usage["input_tokens"], u.usage["output_tokens"]) for u in usage],
        expected_in=in_total,
        expected_out=out_total,
        parent_ok={
            "step_parent_is_invoke": any(
                s["name"] == "step" and s["parentSpanId"] == invoke["spanId"] for s in spans
            ),
            "invoke_has_request_parent": invoke["parentSpanId"] != "",
        },
        metrics=sorted(
            m.name
            for rm in reader.get_metrics_data().resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        ),
        tool_calls=len(ends(envelopes, "lookup")),
    )
    assert receiver.bodies and all(p == "/v1/traces" for p, _, _ in receiver.bodies)
    assert {"invoke_agent", "step", "chat fake", "execute_tool lookup"} <= set(names)
    inv = by_name["invoke_agent"]
    assert inv["gen_ai.operation.name"] == "invoke_agent"
    assert (
        inv["gen_ai.usage.input_tokens"] == in_total
        and inv["gen_ai.usage.output_tokens"] == out_total
    )
    assert len(chats) == 2 and all(c["gen_ai.operation.name"] == "chat" for c in chats)
    assert sum(c["gen_ai.usage.input_tokens"] for c in chats) == in_total
    assert sum(c["gen_ai.usage.output_tokens"] for c in chats) == out_total
    tool_attrs = attrs(tool_span)
    assert tool_attrs["gen_ai.operation.name"] == "execute_tool"
    assert tool_attrs["gen_ai.tool.name"] == "lookup"
