# Khả năng quan sát

[Bản gốc tiếng Anh](../../usage/observability.md)

Dùng OpenTelemetry khi bạn muốn trace và metric của các run trong agent được đưa vào Langfuse, Tempo, Datadog hoặc một collector khác.

[← Hướng dẫn sử dụng](./README.md)

Event stream vẫn là cách ứng dụng của bạn hiển thị tiến trình và kết quả (xem
[Event](./events.md)). Với vận hành, client biến stream của mỗi run thành span và
metric OpenTelemetry với các tên mà Python Linch dùng. Nó chỉ phụ thuộc vào
`opentelemetry-api`: ứng dụng chủ sở hữu SDK và các exporter.

---

## Cài đặt

```bash
pip install 'linch-rust[otel]'          # adds opentelemetry-api
pip install opentelemetry-sdk           # the host's SDK
pip install opentelemetry-exporter-otlp # or the exporter your collector needs
```

Khi đã cài extra, mọi agent mà `Server.open_agent` mở đều được trace. Nếu không cài,
`linch_rust.otel` không bao giờ được import và các run không tốn thêm chi phí nào.
Nếu đã cài extra nhưng chưa đặt SDK provider, cũng không có gì được dựng: các provider
mặc định của API được coi là tắt.

---

## Trace một agent

Truyền các provider của bạn bằng `telemetry=`, để các agent khác nhau có thể xuất đến
những nơi khác nhau. Đoạn mã này giữ span và metric trong bộ nhớ để in ra; hãy cài một
exporter khi dùng thật.

```python
import asyncio

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from linch_rust import AgentConfig, Server, tool
from linch_rust.otel import Telemetry
from linch_rust.scripted import MODEL, text_turn, tool_turn

exporter = InMemorySpanExporter()
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
reader = InMemoryMetricReader()
meter_provider = MeterProvider(metric_readers=[reader])
telemetry = Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


@tool(scope="read")
async def lookup(email: str) -> str:
    """Find a customer by email."""
    return "Ada Lovelace, customer since 2019"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [
        tool_turn("c1", "lookup", {"email": "ada@example.com"}),
        text_turn("Ada has been a customer since 2019."),
    ]},
}


async def main() -> None:
    tracer = tracer_provider.get_tracer("my-service")
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup], telemetry=telemetry)
        with tracer.start_as_current_span("POST /ask") as request:
            run = await agent.run("s1", "Who is ada@example.com?", run_id="ask-1")
        await run.result()  # read the run to its end, or its spans stay open

    spans = sorted(exporter.get_finished_spans(), key=lambda span: (span.start_time, span.end_time))

    def show(parent, indent: str) -> None:
        for span in spans:
            if (span.parent.span_id if span.parent else None) == parent:
                print(indent + span.name)
                show(span.context.span_id, indent + "  ")

    show(None, "")
    run_span = next(span for span in spans if span.name == "invoke_agent")
    print("attributes:", {k: v for k, v in run_span.attributes.items() if k.startswith(("gen_ai.", "linch."))})
    same = run_span.context.trace_id == request.get_span_context().trace_id
    print("same trace as the request:", same)
    metrics = reader.get_metrics_data().resource_metrics[0].scope_metrics[0]
    print("metrics:", metrics.scope.name, sorted(metric.name for metric in metrics.metrics))


asyncio.run(main())
```

```text
POST /ask
  invoke_agent
    step
      chat scripted-model
      execute_tool lookup
    step
      chat scripted-model
attributes: {'gen_ai.operation.name': 'invoke_agent', 'gen_ai.provider.name': 'scripted', 'gen_ai.request.model': 'scripted-model', 'gen_ai.conversation.id': 's1', 'linch.run_id': 'ask-1', 'linch.resumed': False, 'gen_ai.usage.input_tokens': 6, 'gen_ai.usage.output_tokens': 2, 'gen_ai.usage.cache_read.input_tokens': 0, 'gen_ai.usage.cache_creation.input_tokens': 0, 'linch.stop_reason': 'end_turn'}
same trace as the request: True
metrics: linch ['gen_ai.client.operation.duration', 'gen_ai.client.token.usage']
```

Hai quy tắc suy ra từ cách các span được dựng:

- **Span cha là context hiện hành tại thời điểm gọi.** `Agent.run`, `resume` và
  `approve` lấy context hiện hành khi chúng được await, không phải khi bạn duyệt
  stream. Hãy bắt đầu span request của bạn quanh lệnh gọi đó, như trên, để đặt run
  bên dưới nó.
- **Hãy đọc mọi run đến hết.** Các span kết thúc ở event `result` hoặc `suspended`
  của run, hoặc khi stream lỗi. Một run mà bạn bỏ dở iterator sẽ để các span của nó
  mở, và chúng không bao giờ được xuất. Hãy duyệt đến hết hoặc gọi `Run.result()`.

---

## Dùng các provider toàn cục

Khi không có `telemetry=`, agent dùng các tracer provider và meter provider toàn cục,
được đọc lại ở mỗi run, nên các provider bạn đặt sau khi mở agent vẫn có hiệu lực.

```python
import asyncio

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from linch_rust import AgentConfig, Server
from linch_rust.scripted import MODEL, text_turn

# Set once at startup; swap the console exporters for OTLP ones in production.
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(tracer_provider)
meter_provider = MeterProvider(metric_readers=[PeriodicExportingMetricReader(ConsoleMetricExporter())])
metrics.set_meter_provider(meter_provider)

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)  # no telemetry=: the global providers
        await (await agent.run("s1", "Say hello.")).result()
    tracer_provider.shutdown()  # flush before exit
    meter_provider.shutdown()


asyncio.run(main())
```

Đoạn mã này in các span `invoke_agent`, `step` và `chat` cùng hai metric dưới dạng
JSON. Với một collector, hãy dùng `OTLPSpanExporter` và `OTLPMetricExporter` từ
`opentelemetry-exporter-otlp` thay cho các console exporter.

---

## Span và metric

```text
invoke_agent                      init .. result (or suspended)
└─ step                           one per turn, linch.turn_index
   ├─ compaction                  .. compaction
   │  └─ chat {model}             the summary call, .. its usage
   ├─ chat {model}                context_build .. assistant / error
   └─ execute_tool {tool}         tool_call_start .. tool_call_end
```

Mỗi span bắt đầu vào thời gian server của event mở nó (`Envelope.ts_ms`) và kết thúc
vào thời gian của event đóng nó. Instrumentation scope là `linch`.

| Span | Thuộc tính |
|---|---|
| `invoke_agent` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.conversation.id` (session), `linch.run_id`, `linch.resumed`; ở cuối là bốn số đếm token `gen_ai.usage.*`, `linch.stop_reason`, `linch.cost_usd` |
| `step` | Các thuộc tính định danh tương tự và `linch.turn_index` |
| `chat {model}` | Các thuộc tính định danh và lượt, với `gen_ai.request.model` (và `{model}` trong tên) là model riêng của lệnh gọi lấy từ event `context_build` của nó; `gen_ai.response.finish_reasons`, `gen_ai.response.model` (`actual_model` của event `usage`), bốn số đếm token, `linch.cost_usd` |
| `compaction` | Các thuộc tính định danh và lượt, `linch.compaction.strategy` |
| `execute_tool {tool}` | `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.conversation.id`, `linch.run_id`, `linch.turn_index` |

`gen_ai.provider.name` lấy từ id provider của cấu hình (`openai-chat` và
`openai-responses` báo `openai`, `gemini` báo `gcp.gemini`), và `gen_ai.request.model`
lấy từ `spec.model` cho đến khi event `init` của run nêu tên model. Một chat, cùng hai
metric của nó, mang model mà lệnh gọi của nó yêu cầu, model này có thể bị một skill
đã nạp thay đổi cho một lượt. `linch.cost_usd` được đặt khi model có giá.

`gen_ai.usage.input_tokens` đếm mọi token đầu vào, kể cả token từ cache, theo đúng
quy ước ngữ nghĩa GenAI: đầu vào không qua cache cộng
`gen_ai.usage.cache_read.input_tokens` cộng
`gen_ai.usage.cache_creation.input_tokens`. Để có số đếm không qua cache, hãy lấy
`input_tokens` trừ đi hai số đếm cache.

Mỗi lệnh gọi mô hình ghi hai histogram với các bucket mặc định của SDK:

| Metric | Đơn vị | Thuộc tính |
|---|---|---|
| `gen_ai.client.operation.duration` | `s` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `error.type` khi thất bại |
| `gen_ai.client.token.usage` | `{token}` | Tương tự, và `gen_ai.token.type` (`input` hoặc `output`); bucket `input` bao gồm các token từ cache, như `gen_ai.usage.input_tokens` |

`error.type` chứa một tên, không bao giờ là thông báo:

| Nguyên nhân | `error.type` |
|---|---|
| Một event `error`, như một lệnh gọi provider thất bại | `name` của nó; nó đánh dấu chat, step và run |
| Một lệnh gọi tool kết thúc bằng lỗi | `error_code` của `tool_call_end` của nó, nếu không thì `tool_error` |
| Một tool end mà phần start của nó nằm trong stream trước đó (một lệnh gọi bị crash làm gián đoạn) | `interrupted` |
| Một `result` không phải `success`, không có event `error` | Subtype của nó, như `aborted` |
| Một stream bị lỗi | Tên lớp của exception, như `RunError` hoặc `ConnectionClosed` |

Một lệnh gọi resume hoặc approve bắt đầu bằng event `init` kiểu `resumed`: các span của
nó có `linch.resumed: true`, và id session cùng chỉ số lượt của nó lấy từ event đó.
Run con của một subagent đến stream của agent cha dưới dạng các event được bao bọc
(với thời gian riêng của run con và call id của agent cha) và hiện chưa có span riêng.

---

## Ghi lại nội dung

Prompt, các câu trả lời, cùng đầu vào và kết quả của tool bị bỏ ra ngoài trừ khi
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` là `true` (không phân biệt hoa
thường). Client đọc biến này khi bắt đầu mỗi run.

| Được ghi lại | Thuộc tính | Span |
|---|---|---|
| Văn bản của prompt | `gen_ai.input.messages` | `invoke_agent` |
| Văn bản cuối cùng | `gen_ai.output.messages` | `invoke_agent` |
| Mỗi message của assistant | `gen_ai.output.messages` | `chat` |
| Đầu vào của tool | `gen_ai.tool.call.arguments` | `execute_tool` |
| Văn bản kết quả của tool | `gen_ai.tool.call.result` | `execute_tool` |

Giá trị là văn bản JSON, và chỉ được dựng cho các span đang ghi.

---

## Liên kết log và trace

`run.trace_id` là id trace của các span của một run được trace, dưới dạng 32 chữ số
hex, để đưa vào dòng log hoặc header phản hồi. Span của run bắt đầu cùng envelope đầu
tiên, nên hãy đọc nó sau thời điểm đó; trước đó, `run.trace_id` là trace của span hiện
hành tại lúc gọi, hoặc `None` nếu ngoài mọi span. Nó là `None` với một run không được
trace.

`agent.run(..., trace_id=...)` cho một run một trace id do bạn chọn, chẳng hạn id của
request của bạn: run lưu nó, và mọi `InitEvent` và `ResultEvent` của run đều mang nó,
kể cả resume hay approve, cũng như các run của subagent và context của các lệnh gọi
tool của nó. Khi không có, chúng mang `None`; server không bao giờ tự bịa ra một id.

```python
import asyncio
import logging

from opentelemetry.sdk.trace import TracerProvider

from linch_rust import AgentConfig, Server
from linch_rust.otel import Telemetry
from linch_rust.scripted import MODEL, text_turn

logging.basicConfig(level=logging.INFO, format="%(message)s")
telemetry = Telemetry(tracer_provider=TracerProvider())
config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, telemetry=telemetry)
        run = await agent.run("s1", "Say hello.")
        result = await run.result()
        logging.info("run %s trace %s: %s", run.id, run.trace_id, result.final_text)


asyncio.run(main())
```

```text
run 21c62618-fb4c-4160-bf07-742f4487880f trace 4f6060a55bd5884c81cbd82583b32a9c: Hello.
```

Hãy log cả `run.id`: nó là `linch.run_id` trên mọi span của run. Một resume hoặc
approve là một `Run` riêng, với trace riêng trừ khi bạn bắt đầu chúng dưới cùng một
span của bạn.

---

## Điểm khác so với Python Linch

| Python Linch | Client linch-rust |
|---|---|
| Span được tạo bên trong vòng lặp | Client dựng chúng từ event stream, vào thời gian của các event |
| Provider lấy từ `Context` của agent | `open_agent(..., telemetry=Telemetry(...))` cho mỗi agent |
| `OTEL_PYTHON_TRACER_PROVIDER` được đọc một lần khi import | Các provider toàn cục được đọc ở mỗi run |
| `linch.store.duration` | Không được dựng: stream không mang các lệnh gọi store |
| `gen_ai.response.model`, `gen_ai.input.messages` trên `chat` | Không được đặt: stream không mang chúng |
| Không có thuộc tính chi phí | `linch.cost_usd` trên `chat` và `invoke_agent` |
| `gen_ai.usage.input_tokens` và bucket token `input` chỉ đếm đầu vào không qua cache | Cả hai bao gồm các token từ cache, như quy ước GenAI nêu |
| `trace_id` trên event của run và `ToolContext.trace_id` | `None` trên event, và `ToolContext` của client không có; hãy đọc `Run.trace_id` |
| `TraceIdFilter` cho log record | Không có |
| Run con có span riêng | Run con không có span |

Phía Rust của cùng phép ánh xạ này là `linch-otel`; xem
[OpenTelemetry](../architecture/otel.md).

---

## Các trang liên quan

- [Event](./events.md): stream mà các span được dựng từ đó
- [Run](./runs.md): resume và approve, có span được đánh dấu resumed
- [Server](./server.md): tiến trình sidecar mà các event đến từ đó
