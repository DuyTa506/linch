# OpenTelemetry

[Bản gốc tiếng Anh](../../architecture/otel.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

`linch-otel` (`crates/linch-otel/src/`) biến luồng event của một run thành các span và
metric OpenTelemetry với đúng những tên mà Linch bản Python phát ra. Nó chỉ phụ thuộc vào
crate API `opentelemetry`; host sở hữu SDK và các exporter của nó (Langfuse, Tempo,
Datadog, ...). Dùng trang này khi bạn đổi một span, một attribute hoặc một metric, hoặc
nối tracing vào một host.

Linch bản Python phát span từ bên trong vòng lặp
([OpenTelemetry](../../../../docs/architecture/subsystems.md#310-opentelemetry)).
Trong Rust, lõi không có lời gọi tracing nào. Một `RunObserver` đọc các `Envelope` của
run khi chúng được stream và dựng các span giống hệt sau đó, mỗi span bắt đầu tại
`ts_ms` của event mở nó và kết thúc tại `ts_ms` của event đóng nó
(`crates/linch-otel/src/lib.rs:4-7`). Vì vậy các span mang thời gian event của chính run,
và một host không dùng tracing thì không tốn gì trong vòng lặp.

## Cây span

```text
invoke_agent                      init .. result (or suspended)
└─ step                           one per turn, linch.turn_index
   ├─ compaction                  .. compaction
   │  └─ chat {model}             the summary call, .. its usage
   ├─ chat {model}                context_build .. assistant / error
   └─ execute_tool {tool}         tool_call_start .. tool_call_end
```

(`lib.rs:9-16`.) Một step kết thúc khi kết quả tool của lượt (hoặc phản hồi dừng) đến
dưới dạng một event `user` (`crates/linch-otel/src/observer.rs:243-259`). Span của run
kết thúc ở `result` hoặc `suspended`. Các span `chat` là `SpanKind::Client`;
các span khác là `Internal`. Một span không bao giờ kết thúc trước khi bắt đầu, kể cả khi
đồng hồ thực bị lùi (`crates/linch-otel/src/span.rs:73-77`).

## Tên và attribute

Mỗi khóa là một hằng số trong `crates/linch-otel/src/keys.rs`, viết giống như trong
`src/linch/_telemetry.py` của Python. Các khóa `gen_ai.*` theo GenAI semantic
conventions, vốn vẫn còn là thử nghiệm; giữ chúng trong một tệp
giúp một lần cập nhật convention chỉ ảnh hưởng cục bộ.

| Span | Attribute |
|---|---|
| `invoke_agent` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.conversation.id` (session), `linch.run_id`, `linch.resumed`; lúc kết thúc có bốn số đếm token `gen_ai.usage.*`, `linch.stop_reason` và `linch.cost_usd` |
| `step` | Các attribute định danh giống trên cộng với `linch.turn_index` |
| `chat {model}` | Các attribute định danh và lượt, với `gen_ai.request.model` và `{model}` trong tên lấy từ `model` của event `context_build` (của request, như `chat {request.model}` của Python; một lời gọi tóm tắt dùng model của run); `gen_ai.response.finish_reasons`, `gen_ai.response.model` (`actual_model` của event `usage`), bốn số đếm token, `linch.cost_usd` |
| `compaction` | Các attribute định danh và lượt, `linch.compaction.strategy`; nó bắt đầu `duration_ms` của event `compaction` trước event đó, và giữ `chat` của lời gọi tóm tắt từ đó đến `usage` của nó |
| `execute_tool {tool}` | `gen_ai.operation.name`, `gen_ai.conversation.id`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `linch.run_id`, `linch.turn_index` |

Một attribute mà observer không biết giá trị sẽ bị bỏ qua thay vì được điền một
placeholder.

Các attribute token là `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens` và
`gen_ai.usage.cache_creation.input_tokens` (`span.rs:99-110`), lấy từ bốn bucket của
`Usage`. Như GenAI convention nói, `input_tokens` bao gồm các token đã cache:
nó là `Usage::total_input_tokens()`, tức input chưa cache cộng với cả hai bucket cache.

Điểm khác với Python:

| Tên | Rust |
|---|---|
| `linch.cost_usd` | Chỉ có ở Rust: chi phí của lời gọi hoặc của run khi agent định giá model của nó (`keys.rs:59-61`) |
| `gen_ai.input.messages` trên `chat` | Không được đặt: stream không mang các message của request |
| `linch.store.duration` | Không được dựng: stream không mang lời gọi store nào |
| `gen_ai.provider.name` | Lấy từ `provider` của event `init`, ánh xạ bởi `provider_name`; `RunObserver::provider` đặt nó cho một stream không có |
| `gen_ai.usage.input_tokens`, bucket token `input` | Đã bao gồm token cache, trong khi Python chỉ đếm input chưa cache |

Các event subagent đến stream của run cha ở dạng được bọc, với `ts_ms` riêng của run con
và `tool_use_id` của run cha, nhưng observer chưa trace chúng
(`lib.rs:104-115`).

`provider_name(id)` (`lib.rs:220`) ánh xạ một provider id của Linch như Python làm:
`openai-chat` và `openai-responses` là `openai`, `gemini` là `gcp.gemini`,
mọi id khác là chính nó.

### Lỗi

`error.type` chỉ giữ tên của lỗi, không bao giờ giữ message của nó, vì message có thể
chứa nội dung riêng tư; trạng thái của span là lỗi với mô tả rỗng
(`span.rs:61-67`).

| Nguồn | `error.type` |
|---|---|
| Một event `error` | `name` của payload, nếu không thì `run_error`; đánh dấu chat đang mở, step và run (`observer.rs:261-271`) |
| Một lời gọi tool kết thúc bằng lỗi | `error_code` của `tool_call_end` của nó, nếu không thì `tool_error`, như Python dùng loại lỗi của outcome (`crates/linch-otel/src/observer/tools.rs`) |
| Một phần kết thúc của tool mà phần bắt đầu nằm trong một stream trước đó (placeholder của một lời gọi bị sự cố làm gián đoạn) | `interrupted`, trên một span bắt đầu `duration_ms` trước phần kết thúc (`crates/linch-otel/src/observer/tools.rs:43-65`) |
| Một `result` không thành công mà không có event `error` nào trước đó | Tên wire của subtype result (`observer.rs:273-293`) |
| `RunObserver::fail(type)` | Type mà host truyền vào |

## Metric

Cho mỗi lời gọi model (`crates/linch-otel/src/observer/calls.rs:142-176`):

| Metric | Đơn vị | Attribute |
|---|---|---|
| `gen_ai.client.operation.duration` | `s` | `gen_ai.operation.name` (`chat`), `gen_ai.provider.name`, `gen_ai.request.model` (của lời gọi), `error.type` khi thất bại |
| `gen_ai.client.token.usage` | `{token}` | Giống trên, cộng với `gen_ai.token.type` (`input` hoặc `output`) |

Cả hai là histogram với các bucket mặc định của SDK, được tạo một lần trên meter mà
host truyền vào (`lib.rs:152-172`). Token usage ghi hai bucket: `input`,
kèm các token cache, và `output`. Một lời gọi có usage không xác định ghi thời lượng của
nó và không ghi token nào.

## Ghi lại nội dung

Nội dung (prompt, câu trả lời, input và kết quả của tool) bị bỏ qua trừ khi việc ghi lại
được bật. `Telemetry::observer()` bắt đầu với
`capture_content_from_env()` (`lib.rs:209`), giá trị này là true khi
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` là `true` ở bất kỳ kiểu chữ nào,
cùng công tắc với Python. `RunObserver::capture_content(bool)` ghi đè nó cho từng run.

| Được ghi lại | Attribute | Span |
|---|---|---|
| Văn bản prompt của user | `gen_ai.input.messages` | `invoke_agent` |
| Văn bản cuối cùng | `gen_ai.output.messages` | `invoke_agent` |
| Mỗi message của assistant | `gen_ai.output.messages` | `chat` |
| Input của tool | `gen_ai.tool.call.arguments` | `execute_tool` |
| Văn bản kết quả của tool (JSON của nội dung khi nó chứa ảnh) | `gen_ai.tool.call.result` | `execute_tool` |

Giá trị là văn bản JSON. Nội dung chỉ được serialize khi span đang ghi, nên một span bị
loại khỏi mẫu không tốn thêm gì (`span.rs:52-55`).

## Những gì host cần nối vào

1. Dựng các provider và exporter của SDK, và tạo một tracer và một meter cho
   scope `linch_otel::SCOPE` (`"linch"`, tên mà Python đặt,
   `lib.rs:130`).
2. Tạo một `Telemetry::new(tracer, &meter)` (`lib.rs:152`) và dùng chung nó; nó
   rẻ để clone. `Telemetry::global()` (`lib.rs:176`) dùng các provider toàn cục của
   process thay vào đó; không có gì khác trong crate đọc hay cài đặt trạng thái toàn cục.
3. Với mỗi run, resume hoặc approve, tạo một observer bằng
   `telemetry.observer()` và đặt những gì stream không mang:
   `.provider(id)` trước khi event `init` đặt tên cho nó, và, với một stream
   không bắt đầu bằng `init`, `.session_id(..)`, `.model(..)` và `.turn(..)`.
   Mọi stream run, resume và approve đều bắt đầu bằng `init` (của resume là
   `resumed`, với `turn` của checkpoint); một stream không có nó được trace
   như đã resume với các attribute đó lấy từ host (`observer.rs:86-112`).
   `.parent(cx)` lồng span của run vào dưới span request của host; không có nó
   thì span của run là gốc của trace.
4. Đưa mọi envelope, theo thứ tự, vào `observe` (`observer.rs:137`). Khi
   stream kết thúc bằng lỗi, gọi `fail` với tên type của lỗi, hoặc drop
   observer: cả hai đều kết thúc mọi span đang mở tại đồng hồ thực
   (`observer.rs:172`, `observer.rs:329-335`).
5. Đọc `trace_id()` (`observer.rs:132`) để liên kết log hoặc một response với
   trace. `trace_id` của chính các event (trên `init`, `result` và mỗi
   `ToolContext`) là giá trị mà host đã đưa vào `RunOptions::trace_id`, được giữ
   cùng run; hãy truyền trace id của span vào đó để cả hai khớp nhau.

Observer không bao giờ panic trước một thứ tự mà không run nào tạo ra: nó trace đến chừng
nào các event còn có nghĩa, và bỏ qua các envelope của run khác hoặc bất cứ thứ gì sau
phần kết thúc của run (`observer.rs:30-37`). `Telemetry` và `RunObserver` là `Send`,
nên một observer có thể di chuyển giữa các task cùng stream của nó (`lib.rs:260-265`).

## Ví dụ

Đoạn này biên dịch và chạy được ở revision này. Nó trace một run được script vào
exporter trong bộ nhớ của SDK:

```rust
use std::sync::Arc;

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider,
};
use linch_otel::{SCOPE, Telemetry};
use opentelemetry::metrics::MeterProvider as _;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider};

/// Run `prompt` and trace it: one observer per run, fed every envelope.
async fn traced_run(agent: &Agent, telemetry: &Telemetry, prompt: &str) -> Option<String> {
    let mut observer = telemetry.observer().provider("anthropic").capture_content(false);
    let mut stream = agent.run(prompt, RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        match item {
            Ok(envelope) => observer.observe(&envelope),
            // A type name only: an error message may hold private content.
            Err(_) => observer.fail("RunError"),
        }
    }
    observer.trace_id()
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // The host owns the SDK and its exporters.
    let spans = InMemorySpanExporter::default();
    let tracer_provider = SdkTracerProvider::builder().with_simple_exporter(spans.clone()).build();
    let meter_provider = SdkMeterProvider::builder().build();
    let telemetry = Telemetry::new(tracer_provider.tracer(SCOPE), &meter_provider.meter(SCOPE));

    let provider = ScriptedProvider::new([TurnBuilder::new("m").text("hello").build()]);
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(Arc::new(provider))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .build()
        .unwrap();

    let trace_id = traced_run(&agent, &telemetry, "hi").await;
    assert_eq!(trace_id.map(|id| id.len()), Some(32));
    let mut names: Vec<_> = spans
        .get_finished_spans()
        .unwrap()
        .into_iter()
        .map(|span| span.name.into_owned())
        .collect();
    names.sort();
    assert_eq!(names, ["chat m", "invoke_agent", "step"]);
}
```

## Test

`crates/linch-otel/tests/` kiểm tra cây run (`run_tree.rs`), thời gian của span
(`timeline.rs`), việc ghi lại nội dung (`content.rs`) và việc đánh dấu lỗi
(`failures.rs`) so với các exporter trong bộ nhớ của SDK.

---

Quay lại [mục lục kiến trúc](./README.md).
