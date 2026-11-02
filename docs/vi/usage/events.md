# Event

[Bản gốc tiếng Anh](../../usage/events.md)

Dùng event để truyền tiến trình của agent theo thời gian thực, kiểm tra kết quả và thống kê mức sử dụng mô hình.

[← Hướng dẫn sử dụng](./README.md)

Mọi thông tin mà vòng lặp agent gửi đến bạn đều ở dạng **event** có kiểu.
`session.run(prompt)` trả về một trình lặp bất đồng bộ gồm các event; bạn truyền chúng đến
UI, nhật ký hoặc hệ thống số liệu ngay khi chúng xuất hiện. Trang này trình bày cách phân loại
event, các trường chi phí mà event mang theo và những công cụ ngoại tuyến (báo cáo run và
bộ chấm điểm eval) giúp bạn gỡ lỗi hoặc đánh giá một run sau khi thực hiện.

---

## Các loại event

Đối chiếu `event.type` để xử lý từng loại. Vòng lặp phát ra event theo thời gian thực
khi lượt xử lý diễn ra:

```python
async for event in session.run("hello"):
    match event.type:
        case "system":    # run started — model, tools, cwd
            pass
        case "user":      # user message appended
            pass
        case "assistant": # full assistant turn (final)
            pass
        case "partial_assistant":  # streaming text/thinking delta
            pass
        case "tool_call_start":   # tool about to run
            pass
        case "tool_call_end":     # tool finished, has .result
            pass
        case "tool_progress":     # transient progress from an in-flight tool
            pass
        case "permission_request": # user approval needed (mode="default")
            pass
        case "usage":     # primary or auxiliary token counts
            pass
        case "budget":    # RunBudget warning or exhaustion
            pass
        case "result":    # .subtype: "success", "error", "aborted", "interrupted"
            pass
        case "error":     # provider/tool error details
            pass
        case "compaction": # context was summarised (or micro-compacted)
            pass
        case "workflow":  # workflow engine progress / journal records
            pass
```

Một số quy tắc về vòng đời cần nắm rõ:

- **Một run mới được đọc bình thường bắt đầu bằng `system` và kết thúc bằng `result`.**
  Đọc kết quả từ `ResultEvent` kết thúc. Một luồng được resume có thể bắt đầu
  ở thời điểm muộn hơn trong run, và đóng luồng sớm sẽ dừng việc đọc.
- **`partial_assistant` chỉ xuất hiện khi `include_partial_messages=True`.**
  Đây là các phần thay đổi của văn bản/suy luận khi streaming (xem
  [Provider → Đọc event suy luận](./providers.md#đọc-các-event-thinking)).
  Khi không bật cờ này, bạn vẫn nhận được event `assistant` đầy đủ ở cuối mỗi
  lượt.
- **`tool_call_end` mang văn bản kết quả đã được kết xuất** trong `.result`; các trường
  phong phú của `ToolResult` (tóm tắt, siêu dữ liệu, trích dẫn) nằm trong `.tool_result`,
  ngay cả khi mô hình chỉ thấy bản xem trước đã bị cắt ngắn hoặc chuyển ra lưu trữ ngoài.
- **`tool_call_end.tool_output` mang Canonical Tool Output V2** cho các tool
  khai báo `output_schema`: giá trị JSON đã được xác thực hoặc lỗi có cấu trúc cùng
  các tệp đính kèm tham chiếu JSON vẫn có sẵn bên cạnh các dạng biểu diễn kết quả.
- **`tool_progress` chỉ phục vụ quan sát.** Một tool có thể gọi
  `ctx.report_progress("scanning", {"completed": 3})`; Linch phát ra một
  `ToolProgressEvent` cho bên đọc, nhưng không bao giờ thêm nó vào lịch sử của provider,
  nhật ký event bền vững của run hay thay đổi `ToolResult` cuối cùng. Tiến trình được
  cung cấp theo khả năng và bên đọc có thể bỏ qua.
- **`permission_request` xác định những lời gọi cần quyết định cấp quyền.**
  Sau khi phát ra event này, Linch xử lý cả nhóm thông qua callback
  `can_use_tool` đã cấu hình. Nếu không có callback, yêu cầu xin phép sẽ bị từ chối.
- **`budget` được phát ra theo `RunBudget`** — một lần để cảnh báo tại `warn_ratio` (mặc định 90%), rồi khi
  cạn budget. Xem [Agent](./agent.md) để biết cách cấu hình `RunBudget`.

Kiểm tra `event.subtype` trên `ResultEvent` kết thúc và đọc `event.final_text`
(hoặc `event.structured_output` khi dùng `OutputSchema`):

```python
async for event in session.run("hello"):
    ...
# In a normally consumed stream, the terminal ResultEvent is last. Check
# `event.subtype` and
# `event.final_text` (or `event.structured_output` when using an OutputSchema).
```

`event.subtype` là một trong các giá trị `"success"`, `"error"`, `"aborted"` hoặc
`"interrupted"` — rẽ nhánh theo giá trị này để quyết định hiển thị câu trả lời,
lỗi hay trạng thái hủy/ngắt.

Các event trong luồng được chuyển đến hook `on_event_emit`. Với các event không phải event kết thúc,
hook chạy khi bên đọc yêu cầu event tiếp theo; event kết thúc được
phân phối trước khi được phát ra. Đóng luồng sớm có thể khiến event không phải event kết thúc
vừa được phát ra gần nhất chưa được phân phối. Xem [Hook](../../usage/hooks.md).

<a id="model-call-accounting"></a>

## `ModelCallEvent` — thống kê lời gọi mô hình

Trong một run thông thường, Linch ghi nhận mọi lần chuyển giao thực tế đến provider đạt tới
`message_end` đã chuẩn hóa với `Usage` hợp lệ, bao gồm lần chuyển giao phụ đến provider
được dùng cho quá trình nén ngữ cảnh bằng mô hình. Một lần thử lại hoặc chuyển sang phương án dự phòng nhận một ID
chuyển giao thực tế mới và được thống kê riêng ngay cả khi thuộc cùng một
thao tác logic. Mức sử dụng hợp lệ độc lập với việc đầu ra có dùng được hay không:
kết quả nén ngữ cảnh rỗng, sử dụng tool, không phải văn bản hoặc quá lớn sẽ bị từ chối nhưng
mức sử dụng được báo cáo vẫn được tính một lần.

`ModelCallEvent` là lớp bao công khai, bất biến cho nhật ký thống kê
nội bộ. Nó không được phát ra bởi `session.run()`. Khi đã cấu hình `RunStore`,
dùng `load_events()` và hàm kiểm tra kiểu để kiểm tra các bản ghi thống kê đã lưu:

```python
from linch import ModelCallEvent, is_model_call_event

# run_store is the configured RunStore; run_id comes from SystemEvent.run_id.
cursor = 0
for row in await run_store.load_events(run_id, after_seq=cursor):
    event = row.event
    if is_model_call_event(event):
        assert isinstance(event, ModelCallEvent)
        print(row.seq, event.record_kind, event.run_id)
    cursor = row.seq
```

Lớp bao mang ID của run, một trong các giá trị `intent`, `dispatch_started`, `receipt`,
`abandoned` hoặc `projection` và một bản ghi độc lập, an toàn để biểu diễn bằng JSON. Biên nhận,
ý định và chi tiết gateway là các bản ghi nhật ký nội bộ; chúng không bổ sung
tên vào `linch.__all__`. Các bản ghi lời gọi mô hình chỉ nằm trong nhật ký và không
được phát ra trong luồng `session.run()` thông thường. Một
biên nhận nén ngữ cảnh được đưa ra thông qua `UsageEvent` cộng thêm:

```python
if event.type == "usage" and event.source == "compaction":
    print(event.usage, event.call_id, event.logical_call_id, event.model)
```

`UsageEvent.usage` là mức sử dụng của lần chuyển giao thực tế, còn `cumulative` là mức sử dụng đã biết tích lũy của run
trên các lần chuyển giao chính và phụ. `ResultEvent.total_usage` và
`RunReport` dùng cùng tổng tích lũy mang tính cận dưới này. Khi một lời gọi đã gửi
bị hủy, thất bại hoặc kết thúc trước khi có mức sử dụng cuối cùng hợp lệ, tổng không
được tự coi là bằng không: kết quả kết thúc đặt `usage_complete=False`.

---

## Các trường chi phí

Event về mức sử dụng và kết quả bao gồm các trường chi phí USD tùy chọn cho những mô hình có giá:

```python
async for event in session.run("Summarize this thread."):
    if event.type == "usage":
        print(event.cost_usd, event.cumulative_cost_usd)
    elif event.type == "result":
        print(event.total_cost_usd)
```

Event `usage` báo cáo chi phí của lần chuyển giao thực tế (`cost_usd`) và
tổng đã biết tích lũy đến thời điểm hiện tại (`cumulative_cost_usd`); event `result` kết thúc
báo cáo tổng của run (`total_cost_usd`). Số token tích lũy bao gồm
lần chuyển giao phục vụ nén ngữ cảnh trước lượt chính tiếp theo.

Một lần chuyển giao chưa có giá có `cost_usd=None`. Chi phí tích lũy và kết thúc có thể là
tổng chưa đầy đủ khi chỉ một số lần chuyển giao có giá đã biết. Mức sử dụng token đã biết được
giữ lại, nhưng giới hạn USD không thể áp dụng với giá chưa biết.

Điều này cũng ảnh hưởng đến budget: với mô hình chưa có giá, chỉ giới hạn token của
`RunBudget` mới có thể ràng buộc lần chuyển giao đó (giới hạn chi phí không thể bị kích hoạt khi
giá chưa biết). Xem
[Agent](./agent.md) để biết cách cấu hình budget.

### Rào chắn budget và nén ngữ cảnh

Lần chuyển giao phục vụ nén ngữ cảnh được quyết toán trước khi bản tóm tắt được áp dụng, các hook tiếp tục
hoặc yêu cầu tiếp theo đến provider được tạo. Vòng lặp kiểm tra lại điều kiện cho phép sau
lần quyết toán đó, nên một bản tóm tắt có thể làm cạn giới hạn token/USD và ngăn lượt chính
được gửi đi. Nếu một lần chuyển giao đã gửi có mức sử dụng chưa biết, một run có giới hạn
sẽ dừng theo hướng từ chối trước lời gọi mô hình tiếp theo, thay vì khẳng định rằng giới hạn
đang được thực thi. Một run không có giới hạn có thể tiếp tục, nhưng kết quả vẫn được ghi rõ là
cận dưới (`usage_complete=False`).

Sổ biên nhận nội bộ dùng chung một đối tượng quản lý `RunBudget` đang hoạt động giữa các session và
subagent trong cùng cây chạy trong một tiến trình, nên một nhánh ngang hàng không thể tiếp tục vượt qua
rào chắn do mức sử dụng chưa biết hoặc phát lại một lần chuyển giao đã được tính phí. Đây không phải
bảo đảm cho cây phân tán và không cam kết việc tính phí đúng một lần tại
provider bên ngoài hay đặt trước budget cho các yêu cầu được chấp nhận đồng thời.

## Trace và số liệu OpenTelemetry

Luồng event vẫn là cách để ứng dụng hiển thị tiến trình và
kết quả. Trace và số liệu vận hành được phát ra tại nguồn I/O thông qua
`opentelemetry-api`; nếu không có provider SDK, chúng không thực hiện thao tác nào. Truyền các
provider tracer và meter của ứng dụng chủ qua `Context` của agent (hoặc truyền trực tiếp
các đối tượng tracer/meter) để các agent khác nhau có thể dùng các exporter khác nhau:

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from linch import Agent, Context

host_context = Context()
host_context.register("tracer_provider", TracerProvider())
host_context.register("meter_provider", MeterProvider())
agent = Agent(model="gpt-5", provider=my_provider, context=host_context)
```

Mã đo đạc tại nguồn tạo các span `invoke_agent`, `step`, `chat`,
`execute_tool` và nén ngữ cảnh. Thuộc tính GenAI bao gồm thao tác,
provider, mô hình, lý do kết thúc, mức sử dụng token/cache, ID hội thoại, tên tool
và ID lời gọi; Linch bổ sung định danh run và lượt. Nó ghi lại
các số liệu `gen_ai.client.token.usage`, `gen_ai.client.operation.duration` và
`linch.store.duration` (gắn thẻ `op`). Các trường nội dung được bỏ qua
trừ khi đặt `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Dùng
exporter OTLP cho Langfuse, Tempo, Datadog hoặc bộ thu thập khác; không cần
hook telemetry tùy chỉnh.

Các event ở ranh giới run cung cấp cùng giá trị tương quan dưới tên `trace_id`. Khi có
ngữ cảnh OpenTelemetry đang hoạt động hợp lệ, đây là ID trace dài 32 ký tự; nếu không có,
Linch dùng lại `run_id` bền vững nên phương án dự phòng không tốn thêm công sức
thiết lập trace. ID lúc tạo/khởi nguồn được lưu trong `RunRecord.meta["trace_id"]` và
chỉ đọc trên `ToolContext.trace_id` của mỗi tool. Một lần thực thi được resume theo
trace đang hoạt động của bên gọi, hoặc cùng ID run bền vững khi không có trace đang hoạt động.
Ứng dụng chủ có thể thêm giá trị này vào các bản ghi nhật ký thông thường mà không cần cài đặt trạng thái ghi nhật ký
toàn cục:

```python
import logging

from linch import TraceIdFilter

handler = logging.StreamHandler()
handler.addFilter(TraceIdFilter())
handler.setFormatter(logging.Formatter("%(trace_id)s %(message)s"))
logging.getLogger("my-service").addHandler(handler)
```

Giá trị `extra={"trace_id": ...}` tường minh được ưu tiên, tiếp theo là span OTel đang hoạt động,
`record.run_id` và giá trị dự phòng tùy chọn của bộ lọc. Một tool có thể truyền tường minh
giá trị đã ghi nhận: `logger.info("search complete", extra={"trace_id": ctx.trace_id})`.
Xem danh mục vòng đời được tạo tự động
trong [hướng dẫn kiến trúc](../architecture/events.md#danh-mục-vòng-đời-có-kiểu)
để biết dữ liệu và chế độ phân phối của mỗi điểm. Các điểm hook do
`HookDispatcher` có thứ tự quản lý; các điểm trong pipeline tool chạy nối tiếp theo dạng waterfall, còn
`turn-stopping` là điểm cho phép phủ quyết tuần tự.

---

## Áp lực ngược

`session.run(prompt)` trả về `AsyncIterator[Event]`. Việc lấy event điều khiển
bộ sinh tiền cảnh, bộ sinh này tạm dừng tại mỗi lần yield. Một tác vụ tool đã bắt đầu
có thể tiếp tục trong khi bên đọc tạm dừng; hàng đợi tiến trình của nó giữ tối đa
một cập nhật đang chờ cho mỗi lời gọi và gộp các cập nhật mới hơn. Các tác vụ nền
cũng chạy độc lập. Hãy đọc event kịp thời khi cần theo dõi tiến trình nhanh chóng:

```python
async for event in session.run(prompt):
    await slow_websocket.send(serialize(event))
```

Các hệ quả cần tính đến khi thiết kế:

- **Luôn đọc hết trình lặp.** Bỏ dở trước `ResultEvent` kết thúc
  khiến run bị treo tại chỗ. Hãy đọc đến kết quả kết thúc hoặc chủ động
  đóng trình lặp bằng `aclosing`; có thể gọi `session.abort()` cùng lúc đóng
  sớm để báo hủy công việc đang thực hiện. Khi
  `ResultEvent` kết thúc đã sẵn sàng, session hoàn tất trước khi phát ra nó, nên
  có thể an toàn thoát vòng lặp tại event đó.
- **Mỗi session chỉ có một run đang hoạt động.** Lời gọi `session.run()` thứ hai khi một run đang hoạt động
  sẽ phát sinh `ConfigError` — hãy phân nhánh bằng các session riêng (hoặc subagent).
- **Công việc nền là ngoại lệ tường minh.** Một tool hoặc
  subagent có `run_in_background=True` tách ra thành tác vụ riêng và gửi thông báo hoàn tất dưới dạng
  `<task-notification>` được đọc hết ở lượt sau mà không chặn luồng tiền cảnh.
  Tác vụ đang hoạt động chỉ tồn tại trong tiến trình và không được tái tạo sau khi khởi động lại.
  Mặc định, thông báo nằm trong bộ nhớ; chế độ hộp thư đến bền vững có thể giữ lại
  thông báo hoàn tất đã được đưa vào hàng đợi trong kho session bền vững qua lần khởi động lại. Khi có
  `RunStore`, trạng thái là dữ liệu telemetry kiểm toán theo khả năng trên run khởi nguồn. Xem
  [Tool](../../usage/tools.md) và [deep-agent](./deep-agent.md).

---

## Báo cáo run

Dùng báo cáo run khi bạn cần bản xuất gọn để gỡ lỗi một run đã hoàn tất hoặc
đang thực hiện. Báo cáo được xây dựng từ các event có kiểu và, khi có,
các checkpoint của `RunStore`. Chúng thuần túy là **mô hình đọc** — việc tạo báo cáo không bao giờ
thay đổi trạng thái session.

Tải báo cáo từ kho run bền vững theo `run_id`:

```python
from linch.ext.reports import load_run_report

report = await load_run_report(agent.run_store, run_id)
print(report.to_markdown())

payload = report.to_dict()
print(payload["summary"])
print(payload["context_builds"])
print(payload["permission_requests"])
print(payload["tool_calls"])
print(payload["loop_guards"])
print(payload["long_run"])
```

Với các run tạm thời mà bạn đã thu thập event, hãy tạo báo cáo trực tiếp
từ danh sách đã tích lũy — không cần kho lưu trữ:

```python
from linch.ext.reports import build_run_report

events = []
async for event in session.run("Summarize the policy change."):
    events.append(event)

report = build_run_report(events)
```

Dùng `to_markdown()` để xuất nội dung dễ đọc cho con người (nhật ký, bình luận PR) và
`to_dict()` khi bạn muốn kiểm tra hoặc tuần tự hóa các khía cạnh cụ thể.

Để nhanh chóng tạo báo cáo cục bộ từ kho run Postgres bền vững, dùng script đi kèm:

```bash
PYTHONPATH=src python scripts/run_report.py --dsn "$LINCH_POSTGRES_DSN" <run_id>
PYTHONPATH=src python scripts/run_report.py --dsn "$LINCH_POSTGRES_DSN" --format markdown <run_id>
PYTHONPATH=src python scripts/run_report.py --dsn "$LINCH_POSTGRES_DSN" --format json <run_id>
```

Script nhận `--dsn` hoặc mặc định dùng `LINCH_POSTGRES_DSN`; cần thiết lập
một trong hai. Xem [thiết lập Postgres cục bộ](../../usage/quickstart.md#5-keep-state-when-the-process-restarts).

`payload["summary"]` là phần quan sát tổng hợp gọn: số lượng event,
thời lượng, tổng token/chi phí, tỷ lệ đọc cache, độ trễ và tỷ lệ lỗi của tool,
mức sử dụng ngữ cảnh, các bộ đếm khôi phục cho nén ngữ cảnh, chuyển mô hình dự phòng,
thử lại verifier/hook, chuyển kết quả ra lưu trữ ngoài và các bộ đếm rủi ro như yêu cầu
cấp quyền, cơ chế bảo vệ vòng lặp, lỗi và gợi ý khôi phục.

`payload["long_run"]` tóm tắt các tín hiệu quan trọng trong các session kéo dài:
số lần xây dựng ngữ cảnh, số lần xây dựng ngữ cảnh bị cắt bớt, số token ngữ cảnh đã dùng tối đa, số lượng
tool được chọn, lời gọi tool thất bại, gợi ý khôi phục, trạng thái hoàn tất, tổng chi phí
và giai đoạn checkpoint.

---

## Bộ chấm điểm eval cho run dài

Báo cáo run cho bạn biết *một* run đã làm gì; **hệ thống eval** chấm điểm nhiều
run theo cách xác định khi chạy ngoại tuyến. Các bộ chấm điểm bao quát hành vi ngữ cảnh và bộ nhớ
bên cạnh các kiểm tra thông thường về văn bản, schema, tool và chi phí — mỗi bộ trả về
`True`/`False`/`None`:

```python
from linch.ext.evals import (
    context_metadata_contains,
    context_not_trimmed,
    context_selected_tool,
    cost_under,
    recovery_succeeded,
    run_completed,
    run_eval,
)

result = await run_eval(
    agent,
    cases,
    scorers=[
        context_selected_tool("SearchPolicy"),
        context_not_trimmed(),
        recovery_succeeded(),
        cost_under(0.05),
        run_completed(),
    ],
)
```

`run_eval` chạy agent trên một danh sách `EvalCase` và trả về kết quả
đạt/không đạt/`None` cho từng bộ chấm điểm. `None` nghĩa là "không áp dụng / không quan sát được" —
khác với thất bại rõ ràng — nên một bộ chấm điểm không thấy tín hiệu liên quan
sẽ không âm thầm được tính là đạt. Kết hợp với `ScriptedProvider` để chấm điểm
hành vi mà không cần thực hiện lời gọi mô hình thật. Về tệp bộ kiểm thử, so sánh các đối tượng đánh giá
và mã thoát CI, xem [Eval và benchmark](./evals.md).

---

<a id="trace-context-across-host-jobs"></a>

## `TraceIdFilter` — ngữ cảnh trace xuyên suốt các công việc của ứng dụng chủ

`invoke_agent` dùng span hiện tại của bên gọi làm span cha. Các
subagent tiền cảnh và nền kế thừa ngữ cảnh đó qua các tác vụ tool. Định danh
trace của một run là ID trace OTel đang hoạt động nếu có, nếu không là ID run; `linch.run_id` vẫn là ID run bền vững của nó.
Khi ứng dụng chủ gửi công việc sang tiến trình khác, hãy mang `traceparent` W3C trong
dữ liệu công việc bằng cơ chế lan truyền OTel:

```python
from opentelemetry import context, propagate

# Producer, inside the host request span:
carrier = {}
propagate.inject(carrier)
job = {"run_id": run_id, "trace_context": carrier}

# Consumer, after loading the session for this job:
token = context.attach(propagate.extract(job["trace_context"]))
try:
    async for event in session.resume(job["run_id"]):
        ...
finally:
    context.detach(token)
```

Cài `opentelemetry-sdk` và `opentelemetry-exporter-otlp` trong ứng dụng chủ để
xuất OTLP. Cấu hình bộ xử lý trace và bộ đọc số liệu của SDK với các exporter OTLP,
sau đó đăng ký các provider của chúng trong `Context` như trình bày trong
[hướng dẫn hook](../../usage/hooks.md#opentelemetry). Gửi đến một OTel Collector có chức năng
định tuyến trace/số liệu đến Langfuse, Tempo hoặc Datadog theo các tín hiệu được
backend hỗ trợ. SDK/exporter quản lý cấu hình endpoint và xác thực;
Linch không yêu cầu bộ điều hợp theo nhà cung cấp.

---

## Các trang liên quan

- [Hook](../../usage/hooks.md) — quan sát luồng này qua `on_event_emit` và phản ứng ngay trong vòng lặp.
- [Eval và benchmark](./evals.md) — chạy các bộ eval trên những đối tượng đánh giá có hành vi xác định.
- [Provider](./providers.md) — nội dung của event `partial_assistant`/`usage` theo từng backend.
- [Agent](./agent.md) — `RunBudget` và event `budget`.
