# Hook

[Bản gốc tiếng Anh](../../usage/hooks.md)

Dùng hook để quan sát hoặc thay đổi hành vi của agent tại các chokepoint vòng đời đã định kiểu.

[← Hướng dẫn sử dụng](./README.md)

Hook là **cơ chế mở rộng chuẩn tắc** cho runtime Linch. Hook là một đối tượng Python thuần với một hoặc nhiều phương thức `on_*`; bạn đăng ký hook bằng `Agent(hooks=[...])`, rồi loop gọi chúng tại các chokepoint đã định kiểu. Thông qua hook, bạn có thể quan sát event, biến đổi dữ liệu đang đi qua (viết lại prompt, input/output của tool, câu trả lời cuối), hoặc điều khiển loop (chặn tool, dừng sớm, buộc thêm một turn). Trace runtime và metric đến từ OpenTelemetry tại nguồn I/O; không cần telemetry hook.

Mỗi chokepoint của hook là một điểm `hook` trong [catalog vòng đời](../../architecture/events.md#typed-lifecycle-catalog) được tạo tự động; `HookDispatcher` await các hook theo thứ tự đăng ký. Các điểm `tools/pre-execute`, `tools/execute` và `tools/post-execute` riêng biệt là các waterfall cho wrapper của tool; hook của user vẫn nằm trên adapter để việc ghép biến đổi, cô lập lỗi và thứ tự `PreToolUse` nhạy cảm với bảo mật được thể hiện rõ ràng.

```python
from linch import Agent, HookResult, workspace_tools

class GuardTools:
    def on_pre_tool_use(self, ctx):
        if ctx.tool_name == "Bash" and "rm -rf" in str(ctx.input.get("command", "")):
            return HookResult.block("blocked dangerous command")
        return None

agent = Agent(
    model="gpt-5",
    tools=workspace_tools(),
    hooks=[GuardTools()],
    permissions={"mode": "skip-dangerous"},
)
```

Phương thức hook nhận một *context* đã định kiểu và trả về `None` (không làm gì) hoặc một `HookResult`. Phương thức có thể là **sync hoặc async**. Exception phát sinh bên trong hook được dispatcher bắt và ghi log; dispatcher ghi nhận một lỗi cho lần dispatch đó rồi tiếp tục tới hook kế tiếp. Một số điểm vòng đời không chuyển các bản ghi này vào stream. Trả về `HookResult.block(...)` để từ chối một tool; việc phát sinh exception không chặn tool.

---

## Các chokepoint

Mỗi chokepoint có một tên event, một phương thức `on_*` và một context object. Hook chỉ cần định nghĩa những phương thức mà hook quan tâm.

| Phương thức | Kích hoạt | Trường context | Có thể điều khiển |
|---|---|---|---|
| `on_agent_start` / `on_agent_stop` | run bắt đầu / kết thúc | `model`, `prompt`, `tools` / `result` | chỉ quan sát |
| `on_user_prompt_submit` | trước khi prompt được thêm vào | `prompt`, `images` | biến đổi prompt/images, block/stop |
| `on_turn_start` / `on_turn_stop` | quanh mỗi turn | `turn_index` | chỉ quan sát |
| `on_before_provider_call` | trước mỗi lệnh gọi provider | `request`, `context_result` | biến đổi request, block/stop, force_continue |
| `on_provider_call_start` / `on_provider_call_stop` | quanh lệnh gọi provider | `model`, `stop_reason`, `usage`, `duration_ms` | chỉ quan sát |
| `on_after_provider_call` | sau khi turn của assistant được lắp ráp | `assembly` | biến đổi assembly, block/stop, retry/force_continue |
| `on_pre_tool_use` | sau khâu kiểm tra của provider, trước đánh giá permission cuối cùng | `tool_name`, `input`, `tool` | biến đổi input, block/stop |
| `on_tool_use_start` / `on_tool_use_stop` | quanh mỗi tool | `tool_name`, `result`, `is_error`, `duration_ms` | chỉ quan sát |
| `on_post_tool_use` | sau result của tool, kể cả khi thất bại | `tool_name`, `input`, `result`, `tool_output` | biến đổi result, block/stop |
| `on_post_tool_use_failure` | sau một result thất bại | `tool_name`, `input`, `result`, `tool_output` | chỉ quan sát |
| `on_pre_compact` / `on_post_compact` | quanh compaction | số lượng message và token, `strategy` | chỉ quan sát |
| `on_before_final_answer` | trước câu trả lời cuối dạng text/structured | `final_text`, `structured_output`, `structured_error`, `stop_reason` | biến đổi câu trả lời, block/stop, retry/force_continue |
| `on_stop` | trước khi trả về một `ResultEvent` ứng viên cuối | `result_event` | biến đổi result, force_continue, block/stop |
| `on_subagent_start` / `on_subagent_stop` | quanh một subagent run | `subagent_type`, `display_name`, `prompt`, `result` | chỉ quan sát |
| `on_event_emit` | đối với các event trong stream | `event` | chỉ quan sát |

Một object có thể triển khai bất kỳ tập con nào trong số này. Để xử lý *mọi* event tại một nơi, hãy định nghĩa `on_hook(event_value, ctx)` thay cho các phương thức theo từng event — nếu có `on_hook`, phương thức này được dùng cho mọi event và các phương thức theo từng event sẽ bị bỏ qua.

Với các run durable, một policy hook tùy chỉnh phải cung cấp `resume_policy_id` do host sở hữu và có thể cung cấp `resume_policy_version` cùng `resume_policy_config` an toàn với JSON. Quy tắc tương tự áp dụng cho các adapter mang policy như `ToolMiddlewareHook`, `ContextInjectionHook`, `FinalAnswerVerifierHook` và `StopPredicateHook`, vì callback được bọc có thể thay đổi execution dù class adapter là built-in. `checkpoint_key` của một hook hỗ trợ checkpoint chỉ xác định tên vị trí lưu trạng thái bền vững; nó không chứng minh các policy là tương đương.

---

## `HookResult`

Trả về `None` để không làm gì, hoặc một `HookResult` để thực hiện hành động. Dùng các factory method:

```python
HookResult.continue_()                       # explicit no-op
HookResult.mutate(prompt="...", input={...}) # replace one or more fields
HookResult.block("reason")                   # reject (tool/prompt) → error result
HookResult.retry("feedback")                 # bounce the answer back for another turn
HookResult.force_continue("feedback")        # run another turn even past a stop
HookResult.stop("reason")                    # end the run with an error result
HookResult.resolve(tool_result=tr)           # PreToolUse: serve tr, skip execution
```

Những hành động nào được áp dụng phụ thuộc vào chokepoint:

- **`mutate`** — chỉ các trường có ý nghĩa với chokepoint đó được đọc: `prompt`/`images` (UserPromptSubmit), `request` (BeforeProviderCall), `assembly` (AfterProviderCall), `input` (PreToolUse), `tool_result` hoặc `tool_output` (PostToolUse), `final_text`/`structured_output`/`structured_error` (BeforeFinalAnswer), `result_event` (Stop). Khi nhiều hook biến đổi cùng một context, các thay đổi được truyền qua theo thứ tự — hook tiếp theo thấy giá trị từ hook trước.
- **`block` / `stop`** — tại `PreToolUse`/`PostToolUse`, result của tool trở thành error; tại `UserPromptSubmit`/`BeforeProviderCall`/`AfterProviderCall`/`BeforeFinalAnswer`/`Stop`, run kết thúc với một `ResultEvent` lỗi (vòng đời `on_agent_stop` vẫn được gọi).
- **`retry` / `force_continue`** — tại `BeforeFinalAnswer`, chúng đưa câu trả lời sắp được trả về trở lại loop cùng feedback dưới dạng user message tiếp theo. `AfterProviderCall` cũng áp dụng cả hai; `Stop` chỉ áp dụng `force_continue`. Các turn tiếp theo bị giới hạn bởi `max_turns` và [budget của run](./agent.md#runbudget). `force_continue` tại `BeforeProviderCall` chạy lại turn mà không gọi provider.
- Tại `BeforeProviderCall`, có thể dừng sớm **thành công** một cách êm: trả về `HookResult.stop("stop_when", metadata={"subtype": "success"})` (đây là cách `StopPredicateHook` hoạt động).
- **`resolve`** — chỉ tại `PreToolUse`: tool **không được thực thi**; `tool_result` được cung cấp trở thành kết quả (thành công hoặc lỗi tùy `is_error`). Đây là cách cache phục vụ một cache hit — xem [`ToolCacheHook`](./tool-cache.md).

`PreToolUse` là điểm để biến đổi input. Sau tất cả các biến đổi đó, Linch kiểm tra lại input chuẩn rồi đánh giá rule và `can_use_tool` trên giá trị cuối cùng. Callback phê duyệt quyết định có cho phép input cuối cùng hay không; để thay đổi input, hãy dùng `PreToolUse`.

Các chokepoint chỉ dành cho vòng đời (`agent_start/stop`, `turn_*`, `provider_call_*`, `tool_use_*`, `post_tool_use_failure`, `pre_compact`, `post_compact`, `subagent_*`, `event_emit`) bỏ qua giá trị trả về — hãy dùng chúng cho telemetry.

### Lưu ý về ghép cặp tool

Trước request tiếp theo tới provider, runtime ghép mọi `tool_use` đang chờ với một `tool_result` tổng hợp để hội thoại vẫn đúng định dạng. Feedback sửa chữa được mang trong result đã ghép hoặc trong một user message tiếp theo, tùy đường đi. Bạn không cần làm gì cho việc này.

---

## Adapter tích hợp sẵn

Các pattern mở rộng phổ biến được cung cấp dưới dạng adapter. Import chúng từ `linch`.

| Adapter | Bọc |
|---|---|
| `ContextInjectionHook(builder)` | một hoặc nhiều `ContextBuilder` |
| `ToolMiddlewareHook(middleware)` | tool middleware (`before_tool_call` / `after_tool_result`) |
| `FinalAnswerVerifierHook(verifiers, max_retries=2)` | output verifier |
| `StopPredicateHook(predicate)` | một stop predicate dạng `(session) -> bool` |
| `ToolCacheHook(config)` | memoization theo từng run cho các lệnh gọi tool thuộc read-scope — xem [tool-cache.md](./tool-cache.md) |
| `ReadBeforeWriteHook(config)` | gate read-before-edit cho virtual filesystem (opt-in) |
| `RedactionHook(config)` | regex scrubbing do host cung cấp trên tool result, câu trả lời cuối và prompt (opt-in) |

```python
from linch import (
    ContextInjectionHook,
    FinalAnswerVerifierHook,
    StopPredicateHook,
    ToolMiddlewareHook,
)

agent = Agent(
    model="gpt-5",
    hooks=[
        ContextInjectionHook(MyRagBuilder()),          # per-turn RAG → context-and-memory.md
        ToolMiddlewareHook(MyGovernanceMiddleware()),  # transform/redact tool I/O
        FinalAnswerVerifierHook(MyVerifier(), max_retries=2),
        StopPredicateHook(lambda s: saw_enough(s)),
    ],
)
```

Lưu ý:

- **`ContextInjectionHook`** cung cấp `build_context()` và đưa dữ liệu vào context pipeline theo từng turn. Khi có nhiều context hook riêng biệt, output của chúng được gộp rồi phân bổ lại budget như một tổng thể. Xem [context-and-memory.md](./context-and-memory.md).
- **`ToolMiddlewareHook`** là **fail-closed**: nếu middleware của bạn phát sinh lỗi trong `before_tool_call`, tool sẽ bị *block* (error result), còn nếu phát sinh lỗi trong `after_tool_result`, result trở thành error — cơ chế kiểm soát phát sinh lỗi sẽ không bao giờ âm thầm cho tool đi qua. Các giá trị `tool_output` chỉ canonical sẽ bỏ qua result adapter này.
- **`ReadBeforeWriteHook`** là opt-in (`Agent(read_before_write=True)`) và chỉ có ý nghĩa khi các tool virtual filesystem được bật một cách tường minh. Nó block một `edit_file` tại chỗ trên virtual filesystem cho tới khi file đó đã được read hoặc write trong session này — một `read_file`/`write_file` thành công sẽ đánh dấu file. `Edit` của workspace *không* nằm trong phạm vi này: tool built-in `Edit` tự enforce gate read-before-edit (nguồn kiểm soát duy nhất, vẫn giữ thông báo "You must Read…"), vì vậy flag này chỉ điều khiển virtual gate. Ghi đè toàn bộ file (`Write`/`write_file`) được cho phép mặc định — đó là mục đích của chúng — nhưng host có thể opt in vào việc gate ghi đè các file *đã tồn tại* thông qua `ReadBeforeWriteConfig(overwrite_tools=...)`. Một lần read theo window (`offset`/`limit`) không mở khóa việc edit các phần chưa thấy của file. `Agent` Linch 3.0 thuần không có filesystem tool, offload backend hay read-before-write hook trừ khi embedder opt in.
- **Tiến trình của tool chỉ được phát trực tiếp.** `ToolContext.report_progress(...)` được emit dưới dạng `ToolProgressEvent` và chuyển tới `on_event_emit` khi stream tiến triển, nhưng không được thêm vào provider history hoặc durable run event log.
- **`RedactionHook`** là **điểm mở rộng cho quản trị, không kèm policy**: nó áp dụng các regex `RedactionRule(pattern, replacement)` do bên gọi cung cấp lên text của tool result (`PostToolUse`), câu trả lời cuối (`BeforeFinalAnswer`) và — opt-in qua `RedactionConfig(redact_user_prompt=True)` — user prompt. Linch **không** cung cấp pattern mặc định, vì vậy policy (điều gì được xem là nhạy cảm) thuộc về embedder và tập rule rỗng là no-op. Giá trị `tool_output` chỉ canonical bỏ qua tool-result redactor. Regex sai sẽ được phát hiện lúc khởi tạo, không phải giữa run. Xem [`examples/core/governance_redaction.py`](../../../examples/core/governance_redaction.py). Với phép biến đổi có state phong phú hơn, hãy dùng `ToolMiddlewareHook`.

### OpenTelemetry

Linch dùng trực tiếp `opentelemetry-api`. Khi chưa cấu hình SDK, các lệnh gọi tracer và meter của nó là no-op. Host có thể đưa SDK provider vào `Context` của agent để giữ các tenant tách biệt khi nhiều agent chạy trong cùng một process:

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from linch import Agent, Context

host_context = Context()
host_context.register("tracer_provider", TracerProvider())
host_context.register("meter_provider", MeterProvider())
agent = Agent(model="gpt-5", provider=my_provider, context=host_context)
```

Host sở hữu các SDK provider và vòng đời exporter của chúng. Cũng hỗ trợ đăng ký trực tiếp `tracer` hoặc `meter` trong context khi host đã có các đối tượng instrumentation. Instrumentation nguồn tích hợp sẵn phát ra cây span sau: `invoke_agent` cho mỗi run, `step` cho mỗi turn, `chat` cho mỗi lệnh gọi provider, `execute_tool` cho mỗi tool và một span cho mỗi compaction. Nó ghi các GenAI attribute như `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, lý do kết thúc, token usage/cache, conversation và tool identifier, cùng `linch.run_id` và `linch.turn_index`. Các metric là `gen_ai.client.token.usage`, `gen_ai.client.operation.duration` và `linch.store.duration` (với attribute `op` quanh các lệnh gọi loop store).

Mặc định content của prompt, assistant và tool không được ghi nhận. Host có policy content có chủ đích có thể opt in bằng switch môi trường chuẩn:

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
```

API hoạt động với mọi OTLP exporter. Cấu hình endpoint exporter của host cho Langfuse, Grafana Tempo, Datadog hoặc collector OTLP khác; agent và cấu hình hook của Linch không đổi. Xem [`examples/observability/observability_agent.py`](../../../examples/observability/observability_agent.py) để có thiết lập console-exporter tự chứa.

Để liên kết log của host với một run, gắn `TraceIdFilter` vào một handler. Nó ưu tiên `extra={"trace_id": ...}` tường minh, rồi tới OTel span đang hoạt động, `record.run_id` và một fallback tùy chọn. `ToolContext.trace_id` cung cấp cùng giá trị cho implementation của tool dưới dạng read-only property; tool có thể truyền nó một cách tường minh bằng `logger.info("...", extra={"trace_id": ctx.trace_id})`.

---

## Telemetry: `HookEventRecord`

Dispatcher ghi lại mọi `HookResult` được trả về, bao gồm cả `HookResult.continue_()` tường minh, dưới dạng một `HookEventRecord`. Trả về `None` thì không phát ra bản ghi. Bản ghi chứa chokepoint, tên hook, hành động và lý do. Exception và giá trị trả về không hợp lệ tạo ra bản ghi `error`; exception cũng được ghi log. Chỉ một số chokepoint chuyển bản ghi vào stream, vì vậy các bản ghi này không phải audit log đầy đủ của mọi lần gọi hook.

```python
from linch import is_hook_event

async for event in session.run("..."):
    if is_hook_event(event):
        print(event.hook, event.event, event.action, event.reason)
```

---

## Dọn dẹp

Hook giữ resource có thể cung cấp `close()` hoặc `aclose()`; `Agent.close()` gọi chúng sau khi session, store và filesystem ở trạng thái idle. Lỗi close khiến agent tiếp tục ở trạng thái quiescing; gọi lại `close()` sẽ chỉ retry các resource chưa hoàn tất. SDK provider của OpenTelemetry do host sở hữu và host nên shutdown chúng sau khi các agent đã close.

---

## Trang liên quan

- [Context và memory](./context-and-memory.md) — `ContextInjectionHook` chuyên sâu.
- [Tools](./tools.md) — `PreToolUse`/`PostToolUse` nhìn thấy gì và permission.
- [Structured output](./structured-output.md) — sửa schema và capture final-tool.
- [Events](./events.md) — event stream mà `on_event_emit` mirror.
