# Giám sát

[Bản gốc tiếng Anh](../../usage/monitoring.md)

Theo dõi agent của bạn làm gì và tốn bao nhiêu: trace, chi phí, eval và báo cáo run.

[← Hướng dẫn sử dụng](./README.md)

Có bốn công cụ. Log và trace đi đến collector của bạn qua OpenTelemetry. Chi phí
có trên mỗi event. Một eval chạy một danh sách case và chấm điểm chúng. Một báo
cáo run gộp một run đã lưu thành các lần gọi tool, thời gian và tổng số.

---

## Những gì được trace

Client biến event stream của mỗi run thành span và metric OpenTelemetry với các
tên mà Python Linch dùng: một span cho mỗi run, mỗi lần gọi model và mỗi lần gọi
tool, cùng metric cho token, chi phí và thời lượng. Ứng dụng của bạn sở hữu SDK
và exporter (Langfuse, Tempo, Datadog, bất kỳ OTLP collector nào). Không có gì
được dựng trừ khi bạn cài extra `otel` và đặt một provider, nên một agent không
được trace không tốn thêm gì.

Cách cài đặt, tên span và metric, và cách đối chiếu log nằm trong
[Khả năng quan sát](./observability.md).

---

## Chi phí được tính thế nào

Mỗi lần gọi model có báo usage được tính giá từ bốn bucket token của lần gọi đó,
mỗi bucket theo đơn giá riêng trên một triệu token:

```text
cost = input * input_rate + output * output_rate
     + cache_read * cache_read_rate + cache_creation * cache_creation_rate
```

`input_tokens` chỉ là đầu vào không qua cache: adapter trừ đi các lượt đọc và
ghi cache mà nhà cung cấp của nó đếm bên trong prompt. Kết quả nằm trên event
`usage` (`cost_usd`, `cumulative_cost_usd`) và trên event `result`
(`total_cost_usd`). Việc tính giá không bao giờ thay đổi một request hay bất kỳ
trường event nào khác.

**Bảng giá.** Bảng giá tích hợp là của Python Linch và có sáu id Anthropic, theo
mức giá công bố ngày 2026-06-08 (https://www.anthropic.com/pricing), với cache
write ở bậc 5 phút:

| Model id | Input | Output | Cache read | Cache write |
|---|---:|---:|---:|---:|
| `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` | 5.00 | 25.00 | 0.50 | 6.25 |
| `claude-sonnet-4-6` | 3.00 | 15.00 | 0.30 | 3.75 |
| `claude-haiku-4-5`, `claude-haiku-4-5-20251001` | 1.00 | 5.00 | 0.10 | 1.25 |

USD trên một triệu token. Một model được khớp theo id chính xác.

**Theo model và theo subagent.** Event `result` chia usage theo hai cách, và mỗi
cách cộng lại đúng bằng phần nó chia:

- `usage_by_model` (id model sang bốn bucket usage) cộng lại bằng
  `total_usage`. Khóa là model đã phục vụ lần gọi (`actual_model`; model hiện
  tại của run khi provider không nêu), nên một fallback hoặc một bản tóm tắt
  compaction chạy trên model khác hiện thành một mục riêng. Event `usage` mang
  phần chia đang chạy dưới tên `cumulative_by_model`.
- `subagent_usage` (id child run sang `{subagent_type, total_usage,
  usage_complete, usage_by_model, total_cost_usd}`) giữ những gì mỗi child run
  subagent mà run đã khởi động đã tiêu, đúng như `result` của chính child báo.
  `total_usage` chỉ đếm các lần gọi của chính run, nên cả cây là `total_usage`
  cộng `total_usage` của từng mục, và cả cây theo model là `usage_by_model` cộng
  `usage_by_model` của từng mục. Child chưa từng được khởi động hoặc chưa kết
  thúc thì bị bỏ.

Cả hai bị bỏ khỏi event khi rỗng, nên một run một model và không subagent báo
đúng như trước. Một run bị `Agent.fail` kết thúc báo `usage_by_model` từ
checkpoint cuối và không có `subagent_usage`. Python: `ResultEvent.usage_by_model`,
`ResultEvent.subagent_usage` (`SubagentUsage`) và `UsageEvent.cumulative_by_model`.

**Mọi model khác có chi phí `None`**, không bao giờ là số 0 âm thầm và không bao
giờ là phỏng đoán: các model OpenAI, Gemini và DeepSeek, và mọi model mà một
server cục bộ (vLLM, SGLang, llama.cpp) phục vụ. Python Linch cũng không có đơn
giá cho chúng. Một run dùng nhiều model chỉ cộng các lần gọi đã có giá; một run
không có lần gọi nào có giá thì `total_cost_usd: None`. Một agent có `budget` tính bằng
USD mà model không có giá thì không mở được (`-32011`, nêu tên model): hãy đặt giá
cho model bằng `pricing`, hoặc dùng giới hạn token.

**Tự đặt giá cho một model** bằng `pricing` trong agent config. Nó áp dụng theo
từng agent, theo id model chính xác, và thay thế bảng giá cho model đó hoặc thêm
một model mà bảng chưa có:

```python
config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": "Qwen/Qwen3-32B"},
    "provider": {"id": "vllm", "base_url": "http://localhost:8000/v1"},
    "pricing": {
        # USD per million tokens. A local server you own can cost 0.
        "Qwen/Qwen3-32B": {"input": 0.0, "output": 0.0},
        # Or an amortised rate; cache rates default to `input`.
        "my-finetune": {"input": 0.2, "output": 0.6, "cache_read": 0.05},
    },
}
```

**DeepSeek** không có hàng trong bảng: giá API của nó đổi theo giờ (giờ thấp điểm
bằng nửa giá cao điểm; cao điểm là 01:00 đến 04:00 và 06:00 đến 10:00 UTC các ngày
trong tuần, trừ ngày lễ của Trung Quốc), nên một mức giá dựng sẵn sẽ sai một nửa
thời gian. Hãy đặt mức giá bạn trả, với giá cache hit là `cache_read`. Các mức dưới
đây là giá cao điểm đọc ngày 2026-11-17 từ
https://api-docs.deepseek.com/quick_start/pricing, USD trên một triệu token; hãy
kiểm tra lại trang đó trước khi dựa vào chúng:

```python
"pricing": {
    "deepseek-flash": {"input": 0.3, "output": 1.2, "cache_read": 0.006},
    "deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cache_read": 0.044},
},
```

`input` và `output` là bắt buộc; `cache_read` và `cache_creation` mặc định bằng
`input`, nên một token được cache không bao giờ rẻ hơn mức bạn đã nêu. Đơn giá
phải hữu hạn và không âm; mọi giá trị khác bị từ chối khi agent được mở.

**Cache usage từ server cục bộ.** `cache_read_tokens` lấy từ các trường mà mỗi
server báo cáo: `prompt_tokens_details.cached_tokens` trên vLLM (và
`created_cache_tokens` làm cache write), SGLang và llama.cpp; `cached_tokens` ở
cấp cao nhất của SGLang khi nó không gửi details; và với llama.cpp là các tên
khác mà Python đọc (`cache_read_tokens`, `prompt_cache_hit_tokens`) cùng
`timings.cache_n`. vLLM và SGLang không báo gì trừ khi được khởi động với
`--enable-prompt-tokens-details` hoặc `--enable-cache-report`: khi đó
`cache_read_tokens` là 0 vì chưa biết, không phải vì không có gì được cache. Trên SGLang 0.5.23, server khởi động không có `--enable-cache-report` không gửi `prompt_tokens_details` và linch báo `cache_read_tokens` 0 ở mọi run, không có lỗi hay cảnh báo
([chạy linch với server SGLang](providers.md#running-linch-against-an-sglang-server)).

---

## Chạy một eval

Một eval chạy các case trên một agent đang mở, lần lượt từng case, mỗi case trên
một session mới, và chấm điểm mỗi run.

```python
import asyncio

from linch_rust import Server
from linch_rust.scripted import MODEL, text_turn

CONFIG = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Paris."), text_turn("Munich.")]},
    "pricing": {MODEL: {"input": 3.0, "output": 15.0}},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(CONFIG)
        report = await agent.eval(
            [
                {"prompt": "Capital of France?", "expected": "Paris"},
                {"prompt": "Capital of Germany?", "expected": "Berlin"},
            ],
            [
                {"type": "text_contains"},
                {"type": "cost_under", "budget_usd": 0.01},
                {"type": "run_completed"},
            ],
        )
        print(report["passed"], "of", report["total"], report["total_cost_usd"])
        print(report["markdown"])


asyncio.run(main())
```

Một case là `{prompt, expected?, metadata?}`; prompt không được rỗng. `eval.run` của
một tenant nhận tối đa 256 case (`-32602` nếu vượt, trước khi có run nào chạy); kết nối
cục bộ không có giới hạn. Các run được server nhận bằng id của nó như mọi run nó
điều khiển. Một case
đạt khi run của nó không ghi lỗi nào và không scorer nào trả về `False`. Một
scorer trả về `None` (không xác định) khi run thiếu dữ liệu mà nó đọc, và không
xác định thì không làm case thất bại.

**Scorer tích hợp** chạy trong server và là các dict có `type`:

| `type` | Trường | Đạt khi |
|---|---|---|
| `text_contains` | `substring` (mặc định `{expected}`) | đầu ra chứa nó, không phân biệt hoa thường; `{expected}` là đáp án mong đợi của case |
| `tool_called` | `tool` | run đã bắt đầu một lần gọi tool đó |
| `schema_valid` | `schema` | đầu ra là JSON mà schema chấp nhận |
| `cost_under` | `budget_usd` | `total_cost_usd` của run thấp hơn hẳn nó; không xác định nếu không có giá |
| `run_completed` | không có | run kết thúc với một kết quả thành công |
| `recovery_succeeded` | `tool?` | một lần gọi tool thất bại được theo sau bởi một lần thành công |
| `context_selected_tool` | `tool` | một event `context_build` đã đưa tool đó vào danh sách |
| `context_not_trimmed` | không có | không event `context_build` nào báo có cắt bớt |
| `context_metadata_contains` | `key`, `expected?` | metadata của một event `context_build` chứa key (và giá trị) |

Một type hoặc trường không xác định, hoặc một chuỗi rỗng ở nơi bắt buộc có giá
trị, gây ra `ProtocolError` và không có gì được chạy.

**Scorer tùy chỉnh** là các callable Python, đồng bộ hoặc bất đồng bộ, nhận kết
quả của case và trả về `True`, `False` hoặc `None`. Chúng chạy trong process của
bạn sau khi các case xong, nên không cần callback và không thấy được run đang
chạy; hãy đọc event bằng `agent.events(result["run_id"])` hoặc yêu cầu chúng
bằng `include_events=True`. Client thêm kết quả chấm của chúng dưới `__name__`
của hàm, tính lại `passed` và `pass_rate`, và bỏ `markdown`, vốn được server
viết khi chưa có chúng.

```python
def short(result):
    return len(result["output"]) < 200

report = await agent.eval(cases, [{"type": "run_completed"}, short])
```

**Báo cáo** (`EvalReport`) gồm `total`, `passed`, `pass_rate`, `total_cost_usd`
(tổng các case đã có giá, `None` khi không case nào có giá), `usage` (bốn bucket
token được cộng lại), `duration_ms` và `cases`. Kết quả của mỗi case có `case`,
`output`, `passed`, `scores` (`[{name, verdict}]`), `error`, `tool_calls`,
`total_cost_usd`, `usage`, `duration_ms`, `event_count` và `run_id`. Trường
`markdown` là một bảng với một dòng cho mỗi case. Event bị bỏ ra trừ khi
`include_events=True`.

**Hành vi cần biết.**

- `eval` trả về khi mọi case đã chạy xong; không có progress stream và client
  không có cách hủy nào ngoài việc đóng kết nối, thao tác này dừng nó ngay tại
  chỗ đang chạy.
- Mỗi case là một run đã lưu, nên `agent.report(case["run_id"])` và
  `agent.events(...)` dùng được trên nó.
- Một eval chiếm một chỗ chạy run của một remote tenant trong lúc chạy
  (`LimitReached`), và `agent.close` bị từ chối (`RunBusy`) cho đến khi nó kết
  thúc.
- Một case bị treo để chờ phê duyệt hoặc đầu vào sẽ được để nguyên trạng thái
  treo và được báo cáo không có kết quả: chi phí và thời lượng của nó là `None`.
  Hãy đánh giá các agent có permission mode không hỏi, hoặc có tool được duyệt
  bằng callback.
- Với các turn dựng sẵn (scripted), các case tiêu thụ turn theo thứ tự.

---

## Đọc báo cáo run

`agent.report(run_id)` gộp một run đã lưu, dù đã kết thúc hay chưa, thành một
báo cáo.

```python
one = await agent.report(run_id)
report = one["report"]
print(report["status"], report["final"]["total_cost_usd"])
for call in report["tool_calls"]:
    print(call["tool_name"], call["duration_ms"], "failed" if call["is_error"] else "ok")
print(one["markdown"])
```

Các trường của `RunReport`:

| Trường | Nội dung |
|---|---|
| `run_id`, `session_id`, `status`, `phase` | Run và trạng thái hiện tại của nó: `running`, `awaiting_approval`, `awaiting_input`, `completed`, `error`, `aborted` hoặc `interrupted` |
| `event_count` | Số event đã đọc |
| `tool_calls` | Mọi lần gọi đã kết thúc theo thứ tự kết thúc: `tool_name`, `input`, `summary`, `result`, `is_error`, `duration_ms` |
| `permission_requests` | Các lần gọi đã xin quyền |
| `errors` | Payload của mọi event `error` |
| `usage` | Event `usage` cuối cùng |
| `final` | Event `result`: `subtype`, `total_usage`, `total_cost_usd`, `duration_ms`, `final_text` |
| `summary` | `event_counts`; `usage` với `total_tokens` và `cache_read_ratio`; `tools` (thời lượng tổng, trung bình và lớn nhất, các lần gọi chậm nhất và bị lỗi); các số liệu `context`, `recovery`, `prompt_cache` và `risk` |
| `long_run` | Tín hiệu về tăng trưởng context, chất lượng và resume |
| `timeline` | Mọi event; rỗng trừ khi `timeline=True` |

Một report được dựng để vừa 8 MiB, nằm gọn trong một message. Ba bước, theo thứ tự này, khi nó còn dài hơn:

1. Một chuỗi dài hơn 64 KiB (một câu trả lời hay kết quả tool dài) bị cắt và kết thúc bằng `... [truncated: N more bytes]`.
2. Vượt 8 MiB thì timeline mất các event cuối.
3. Vẫn vượt 8 MiB thì văn bản của mỗi tool call (input và kết quả) bị cắt còn 8 KiB, rồi còn 1 KiB.

Khi có gì bị cắt, câu trả lời có `truncated`: `{"strings": n, "timeline_omitted": m, "tool_call_text": k}`; `timeline_omitted` chỉ có khi có event bị bỏ, và `tool_call_text` (số tool call có văn bản bị rút ngắn) chỉ có khi bước 3 đã chạy. Đọc toàn văn bằng `agent.events`. Một report vẫn vượt 8 MiB khi mọi tool call chỉ còn 1 KiB (hàng nghìn tool call) bị từ chối với `-32603`; hãy đọc run đó bằng `agent.events`.

Hình dạng của stream không quan trọng: một run không có event `result`
thì có `final: None` và giữ trạng thái nó đang có. Một run không tồn tại gây ra
`RunNotFound`.

Để tìm tool chậm hoặc bị lỗi của một run, hãy đọc `summary["tools"]`. Để so sánh
chi phí giữa các run, hãy đọc `final["total_cost_usd"]` và `final["total_usage"]`,
hoặc để một eval làm việc đó trên một tập case cố định.

---

## Các trang liên quan

- [Khả năng quan sát](./observability.md): span, metric và đối chiếu log
- [Event](./events.md): các trường usage và result nơi có chi phí
- [Provider](./providers.md): các provider id và server cục bộ
- [Run](./runs.md): các run đã lưu mà một báo cáo đọc
