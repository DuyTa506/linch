# Event

[Bản gốc tiếng Anh](../../usage/events.md)

Dùng trang này để đọc stream event của một run: mọi loại event mà client giải mã,
các trường của chúng, và cách số thứ tự và replay hoạt động.

[<- Hướng dẫn sử dụng](./README.md)

Lặp qua một `Run` cho ra các `Envelope`. Mỗi envelope chứa một event, được giải mã
thành một dataclass frozen của `linch_rust`. Các giá trị lồng bên trong (message,
usage, kết quả tool) vẫn là các dict JSON mà server gửi; event con mà một event
`subagent` bao bọc được giải mã như mọi event khác. Việc giải mã có tính khoan
dung: trường mà client này không biết sẽ bị bỏ, và một type mà nó không có class
sẽ trở thành `UnknownEvent`.

---

## Hiển thị một stream

Dùng `match` trên các class event:

```python
import asyncio

from linch_rust import (
    AgentConfig,
    AssistantEvent,
    ContextBuildEvent,
    Envelope,
    ErrorEvent,
    InitEvent,
    PartialAssistantEvent,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UnknownEvent,
    UsageEvent,
    UserEvent,
    tool,
)
from linch_rust.scripted import text_turn, tool_turn


@tool(scope="read")
async def weather(city: str) -> str:
    """Current weather for a city."""
    return f"{city}: 18C, light rain"


def render(envelope: Envelope) -> str | None:
    match envelope.event:
        case InitEvent(model=model, tools=tools):
            return f"start {model} with {tools}"
        case UserEvent(subtype="prompt", message=message):
            return f"user: {message['content'][0]['text']}"
        case ContextBuildEvent(messages=messages):
            return f"calling the model with {messages} messages"
        case PartialAssistantEvent(delta={"kind": "text", "text": text}):
            return f"  ...{text}"
        case ToolCallStartEvent(tool_name=name, summary=summary):
            return f"tool {name}: {summary}"
        case ToolCallEndEvent() as end:
            state = "failed" if end.is_error else "ok"
            return f"tool {end.tool_name} {state} in {end.duration_ms} ms"
        case AssistantEvent(stop_reason=reason):
            return f"assistant turn ({reason})"
        case UsageEvent(cumulative=total, cumulative_cost_usd=cost):
            return f"tokens so far {total['input_tokens']} in / {total['output_tokens']} out, ${cost}"
        case ErrorEvent(error=error):
            return f"error: {error['message']}"
        case SuspendedEvent(pending_request_ids=pending):
            return f"waiting for approval of {pending}"
        case ResultEvent(subtype=subtype, final_text=text):
            return f"{subtype}: {text}"
        case UnknownEvent(type=kind):
            return f"({kind})"
        case _:
            return None


config: AgentConfig = {
    "namespace": "events-demo",
    "spec": {"model": "claude-sonnet-4-6"},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "weather", {"city": "Hanoi"}, model="claude-sonnet-4-6"),
            text_turn("Rainy, 18C.", model="claude-sonnet-4-6"),
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[weather])
        async for envelope in await agent.run("s1", "Weather in Hanoi?"):
            line = render(envelope)
            if line is not None:
                print(f"{envelope.seq:>3} {line}")


asyncio.run(main())
```

```text
  1 start claude-sonnet-4-6 with ['weather']
  2 user: Weather in Hanoi?
  3 calling the model with 1 messages
  6 assistant turn (tool_use)
  7 tokens so far 3 in / 1 out, $2.4e-05
  8 tool weather: weather
  9 tool weather ok in 0 ms
 11 calling the model with 3 messages
 12   ...Rainy, 18C.
 14 assistant turn (end_turn)
 15 tokens so far 6 in / 2 out, $4.8e-05
 16 success: Rainy, 18C.
```

Mỗi class cũng có một thuộc tính class `type` (`"init"`, `"tool_call_end"`, ...)
nếu bạn muốn so sánh chuỗi.

## Envelope

| Trường | Ý nghĩa |
|---|---|
| `seq` | Số thứ tự bền vững của run. Một event chỉ có trong stream mang seq của bản ghi bền vững cuối cùng trước nó |
| `run_id` | Run mà event thuộc về |
| `ts_ms` | Thời điểm server tạo envelope, tính bằng mili giây Unix |
| `event` | Event đã giải mã |

### Số thứ tự

Seq tăng dần trong suốt run nhưng không liên tục theo góc nhìn của bạn: run cũng
ghi các bản ghi kế toán cho mỗi lời gọi model (lần dispatch và biên nhận của nó),
chúng chiếm seq và không bao giờ được gửi cho client. Trong stream ở trên, 4 và 5
là các bản ghi của lời gọi đầu tiên, và `partial_assistant` ở dòng 12 mang seq 12,
là bản ghi dispatch của lời gọi thứ hai.

Replay bằng `await agent.events(run_id, after_seq=n)`: nó trả về các envelope bền
vững của run có seq lớn hơn `n`, theo thứ tự, từ các kho lưu trữ. Nó không bao giờ
trả về các event chỉ có trong stream. Vì vậy một client đã thấy seq `n` và mất
stream sẽ đọc đúng những gì nó đã bỏ lỡ.

## Bền vững và chỉ có trong stream

| Loại | Được lưu và replay |
|---|---|
| `partial_assistant`, `tool_progress`, `hook` | Không: gửi ngay, không bao giờ replay |
| Mọi loại khác | Có: gửi khi nó đã bền vững |

Stream của một run kết thúc bằng `result` (đã xong) hoặc `suspended` (đang chờ
`approve`).

## Các loại event

### `InitEvent` (`init`)

Run đã bắt đầu.

| Trường | Ý nghĩa |
|---|---|
| `session_id`, `run_id` | Session và run |
| `model` | `spec.model` |
| `provider` | Id của provider: `anthropic`, `openai-chat`, `scripted`, ... |
| `tools` | Tên các tool được cấp, theo thứ tự |
| `parent_run_id` | Run cha, với run của một subagent; ngược lại là `None` |
| `trace_id` | `trace_id` được truyền cho `agent.run`, giữ trong cả run và các run của subagent; `None` khi không truyền ([Observability](./observability.md#correlate-logs-and-traces)) |
| `resumed` | `True` ở `init` mở đầu một lần resume hoặc approve của run, kèm các tool hiện tại của run; `False` ở `init` đầu tiên của run |
| `turn` | Lượt mà lời gọi tiếp tục: lượt của checkpoint run, với một `init` được resume; `0` ở lần đầu của run |

Mọi stream `run`, `resume` và `approve` đều bắt đầu bằng `init`. Một `init` được
resume là bền vững, nên các event được replay cho thấy điểm bắt đầu của mỗi lời
gọi.

### `UserEvent` (`user`)

Một message vai trò user đã vào cuộc hội thoại.

| Trường | Ý nghĩa |
|---|---|
| `message` | `{"role": "user", "content": [blocks]}` |
| `subtype` | `prompt` (prompt của bạn), `tool_result` (kết quả của một batch tool), `notification` (thông báo của runtime, chẳng hạn một worker nền vừa xong) hoặc `alignment` |

### `AssistantEvent` (`assistant`)

Một phản hồi hoàn chỉnh của model.

| Trường | Ý nghĩa |
|---|---|
| `message` | `{"role": "assistant", "content": [blocks]}`: các block `text`, `thinking`, `tool_use` |
| `stop_reason` | `end_turn`, `tool_use`, `max_tokens`, `stop_sequence`, `refusal`, `error`, `lease_lost` hoặc `interrupted` |

### `PartialAssistantEvent` (`partial_assistant`), chỉ có trong stream

Một phần của phản hồi khi nó đang stream. `delta` là một trong:

| `delta["kind"]` | Các key khác |
|---|---|
| `text` | `text` |
| `thinking` | `text` |
| `tool_use_input` | `tool_use_id`, `json_delta` (một phần của JSON input của lời gọi) |

Phản hồi hoàn chỉnh theo sau dưới dạng `AssistantEvent`, nên bạn có thể bỏ qua các
delta nếu không stream văn bản tới người dùng.

### `ContextBuildEvent` (`context_build`)

Request cho lời gọi model kế tiếp đã được lắp ráp; lời gọi bắt đầu.

| Trường | Ý nghĩa |
|---|---|
| `model` | Model mà lời gọi yêu cầu: `spec.model`, hoặc `model` của một skill đã nạp cho lượt mà skill đó thu hẹp |
| `system_blocks` | Số system block |
| `messages` | Số message hội thoại được gửi |
| `selected_tools` | Tên các tool được cấp trong request này |
| `budget` | Kế toán context; `{}`, vì server không cắt bớt context |
| `metadata` | Những gì các callback `context_build` trả về trong `metadata`, được gộp lại, key của callback đến sau thắng; `{}` nếu không có |

### `UsageEvent` (`usage`)

Mức sử dụng token của một lời gọi model. Xem [Mức sử dụng và chi phí](#mức-sử-dụng-và-chi-phí).

| Trường | Ý nghĩa |
|---|---|
| `usage` | Lời gọi này: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` |
| `cumulative` | Run tính đến hiện tại, cùng các key |
| `cost_usd` | Chi phí lời gọi này bằng USD; `None` khi model không có giá |
| `cumulative_cost_usd` | Các lời gọi đã có giá của run tính đến hiện tại; `None` cho tới khi có một lời gọi có giá |
| `actual_model` | Model mà provider báo cáo đã phục vụ lời gọi này (có thể khác với model được yêu cầu, ví dụ một id có ngày); `None` khi nó không nêu model nào |

### `ToolCallStartEvent` (`tool_call_start`)

Một lời gọi tool sắp chạy. Với kho Postgres, nó bền vững trước khi tool bắt đầu.

| Trường | Ý nghĩa |
|---|---|
| `tool_use_id` | Id của lời gọi, từ model |
| `tool_name` | Tool |
| `input` | Input, sau khi xác thực và sau mọi lần rewrite của `pre_tool_use` |
| `summary` | Một dòng cho log và lời nhắc phê duyệt |

### `ToolCallEndEvent` (`tool_call_end`)

Một lời gọi tool đã kết thúc.

| Trường hoặc property | Ý nghĩa |
|---|---|
| `tool_name` | Tool |
| `result` | Block kết quả tool mà model đọc: `{"tool_use_id", "content": [blocks], "is_error"?}` |
| `duration_ms` | Lời gọi mất bao lâu |
| `canonical_output` | Giá trị có cấu trúc, với các tool khai báo output schema; ngược lại là `None` |
| `metadata` | Chi tiết chỉ dành cho host, không bao giờ hiển thị cho model: những gì một host tool trả về trong [`ToolOutput.metadata`](./tools.md#metadata-cho-host), hoặc nơi một kết quả được offload đã đi tới; `None` với lỗi hoặc với tool không đặt gì |
| `error_code` | Mã máy đọc được của một lời gọi thất bại (`invalid_input`, `not_found`, ...), khi tool của nó cung cấp; ngược lại là `None` |
| `tool_use_id` | Property: `result["tool_use_id"]` |
| `is_error` | Property: kết quả có phải là lỗi tool hay không |

Một lời gọi bị từ chối, bị deny hoặc bị chặn cũng kết thúc bằng một
`tool_call_end` có `is_error` là true. Một lời gọi bị gián đoạn bởi crash sẽ không
có `tool_call_end`; kết quả giữ chỗ của nó nằm trong event `user` kế tiếp có
subtype `tool_result`.

### `ToolProgressEvent` (`tool_progress`), chỉ có trong stream

Tiến trình từ một tool đang chạy: `tool_use_id`, `tool_name`, `message` và `data`
(một dict hoặc `None`). Hiện chưa có tool tích hợp sẵn nào báo cáo tiến trình, và
host tool cũng không có cách nào để làm vậy.

### `PermissionRequestEvent` (`permission_request`)

Các lời gọi cần bạn phê duyệt. `requests` là danh sách các
`{"tool_use_id", "tool_name", "input", "summary"}`. Một event `suspended` theo
sau. Xem [Tool](./tools.md#phê-duyệt).

### `SuspendedEvent` (`suspended`)

Run dừng lại để chờ; stream của nó kết thúc ở đây.

| Trường | Ý nghĩa |
|---|---|
| `status` | `awaiting_approval` |
| `pending_request_ids` | Các `tool_use_id` cần trả lời bằng `agent.approve` |

### `CompactionEvent` (`compaction`)

Phần cũ của cuộc hội thoại đã được tóm tắt
([`spec.compaction`](./agent.md#compaction)): `messages_before`,
`messages_after`, `tokens_before`, `tokens_after` (là giá trị ước tính),
`strategy`, và `duration_ms`, là thời gian từ lúc quyết định nén đến event này
(lời gọi tóm tắt và phép chiếu).

### `LoopGuardEvent` (`loop_guard`)

Run chạm giới hạn lượt của nó: `reason` (`max_turns`), `detail` (ví dụ
`Maximum turns (20) reached.`) và `action` (`stop`). Một event `error` và một
`result` có subtype `error` theo sau.

### `BudgetEvent` (`budget`)

[Budget](./agent.md#budget) của run đã vượt tỷ lệ cảnh báo (`kind` là `warning`,
một lần cho mỗi run và các subagent của nó) hoặc đã dừng run (`exceeded`, theo
sau là một event `error` và một `result` có subtype `error`). Nó mang
`spent_tokens` và `spent_usd` trên cả cây, cùng các giới hạn, `max_tokens` và
`max_cost_usd` (`None` khi không đặt).

### `HookEvent` (`hook`), chỉ có trong stream

Một lời gọi [callback](./agent.md#callback) và cách nó kết thúc.

| Trường | Ý nghĩa |
|---|---|
| `point`, `policy_id`, `policy_version` | Callback nào |
| `tool_use_id` | Lời gọi mà nó nhắm tới, ở các điểm tool; ngược lại là `None` |
| `outcome` | `{"status": "answered", "verdict"}`, `{"status": "failed", "error", "fail_mode"}` hoặc `{"status": "timed_out", "timeout_ms", "fail_mode"}` |

### `ErrorEvent` (`error`)

Một lỗi mà run đã ghi lại. `error` là một dict có `name`, `message` và
`retryable`; lỗi của provider còn có `kind` (`transport`, `status`,
`invalid_request`, `stream`) và, với lỗi HTTP, cả `status`:

```text
{'name': 'ProviderError', 'kind': 'status', 'message': 'provider returned HTTP 503: busy', 'retryable': True, 'status': 503}
{'name': 'TurnLimitError', 'message': 'max turns exceeded', 'retryable': False}
{'name': 'BudgetExceededError', 'message': 'run budget exhausted (30 tokens, $0.0400 spent)', 'retryable': False}
```

Một `result` có subtype `error` theo sau.

### `ResultEvent` (`result`)

Run đã kết thúc; stream của nó kết thúc ở đây.

| Trường | Ý nghĩa |
|---|---|
| `subtype` | `success`, `error`, `aborted` (bị hủy) hoặc `interrupted` |
| `stop_reason` | Vì sao vòng lặp dừng, như trong `AssistantEvent` |
| `final_text` | Văn bản của câu trả lời cuối; `None` khi không có |
| `total_usage` | Usage cộng trên mọi lời gọi có usage đã biết |
| `usage_complete` | `False` khi usage của một lời gọi nào đó không xác định và không có trong tổng |
| `duration_ms` | Thời gian thực của lời gọi `run`, `resume` hoặc `approve` đã kết thúc run. Khoảng thời gian của cả run là `ts_ms` của envelope này trừ `ts_ms` của envelope `init` đầu tiên (cái không phải `resumed`) |
| `total_cost_usd` | Chi phí của các lời gọi đã có giá của run; `None` khi không lời gọi nào có giá |
| `structured_output`, `structured_error` | Cho các run có output schema; `None` với client này |
| `trace_id` | `trace_id` của run, như trên `InitEvent` |

### `SkillsLoadedEvent` (`skills_loaded`)

Các skill của agent, ở run đầu tiên của một session thuộc agent có skill, nằm giữa
`init` và event `user` của prompt. Một run không cấp tool `Skill` (một subagent mà
định nghĩa của nó bỏ tool này) thì không gửi event này. `skills` chứa một dict cho
mỗi skill, sắp xếp theo tên: gồm cả `verify` tích hợp sẵn và, như trong Python,
một skill có `disable_model_invocation` mà model không được nạp và danh sách trong
prompt bỏ qua. Mỗi dict có `name`, `description`, và `when_to_use` cùng
`argument_hint` khi skill đặt chúng. Python Linch gửi nó một lần cho mỗi session
object, nên gửi lại sau khi khởi động lại; ở đây, các run sau của một session thì
không bao giờ gửi.

### `SkillInvokedEvent` (`skill_invoked`)

Một lời gọi `Skill` sắp bắt đầu, ngay trước `tool_call_start` của nó. `name` là
skill và `args` là văn bản đối số nó nhận được, hoặc `None`. `model_override` và
`allowed_tools` hiện luôn là `None`. Xem [Skill](./skills.md).

### `SkillCompletedEvent` (`skill_completed`)

Lời gọi skill đó đã kết thúc, ngay sau `tool_call_end` của nó: `name`, và
`is_error` (`True` khi lời gọi thất bại: không nạp được skill, hoặc lời gọi bị
deny).

### `SubagentEvent` (`subagent`)

Một event của run con của một subagent.

| Trường | Ý nghĩa |
|---|---|
| `child_run_id` | Run con: `<parent run id>:<tool_use_id>` của lời gọi chạy nó |
| `tool_use_id` | Lời gọi của run cha chạy run con: lời gọi `Subagent`, hoặc lời gọi `SubagentContinue` tiếp tục một worker |
| `ts_ms` | Thời điểm run con phát event, lấy từ envelope của chính run con. Event của run con chạy nền đến log của run cha ở lượt kế tiếp của nó, nên `ts_ms` của envelope ngoài muộn hơn |
| `event` | Event của run con, được giải mã thành class riêng của nó (một `InitEvent`, một `ToolCallEndEvent`, ...). Không bao giờ là một `SubagentEvent` khác: run con không thể sinh thêm |

Các event của subagent foreground, kể cả những event chỉ có trong stream, đến
stream của run cha theo cách này; với worker nền thì chỉ các event bền vững đến.
Xem [Subagent](./subagents.md).

### `ChildRunStatusEvent` (`child_run_status`)

Run con của một worker nền đổi trạng thái: `child_run_id` và `status` (`running`,
rồi `completed`, `failed` hoặc `aborted`).

### Các loại mà server chưa phát

Phần lõi khai báo các loại này và client giải mã mỗi loại thành class của nó,
nhưng hiện chưa có run nào phát chúng:

| Class (`type`) | Trường |
|---|---|
| `InputRequestEvent` (`input_request`) | `request_id`, `kind` (`ask_user` hoặc `workflow_interrupt`), `prompt` |
| `PromptCacheAdvisoryEvent` (`prompt_cache_advisory`) | `reason` (`tool_set_changed` hoặc `model_changed`), `detail` |
| `VerificationEvent` (`verification`) | `verifier`, `action` (`retry`, `stop` hoặc `exhausted`), `feedback`, `attempt` |
| `WorkflowEvent` (`workflow`) | `kind`, `title`, `call_key`, `occurrence`, `subagent_type`, `result_text`, `structured_output`, `structured_error` |

### `UnknownEvent`

Một type mà client này không có class, đến từ server mới hơn. `type` là type trên
wire và `raw` là payload không có nó.

## Mức sử dụng và chi phí

`usage` đếm bốn nhóm riêng biệt: `input_tokens` chỉ là input không được cache,
tách khỏi `cache_read_tokens` và `cache_creation_tokens`. Mỗi adapter trừ đi phần
mà nhà cung cấp của nó gộp vào số đếm prompt.

Các trường chi phí được điền từ một bảng giá tích hợp sẵn, khớp theo model id
chính xác của lời gọi. Bảng chỉ chứa các model của Anthropic (ví dụ
`claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5`); mọi model khác báo
`None`. Khi một run dùng nhiều model, tổng chỉ cộng các lời gọi có giá. Giá không
thuộc hợp đồng của run. Các mức giá nằm ở
[trang providers của kiến trúc](../architecture/providers.md#pricing).

Về span và metric dựng từ các event này, xem
[Observability](./observability.md).

## Các trang liên quan

- [Run](./runs.md): resume, approve, hủy, độ bền vững.
- [Tool](./tools.md): thứ tạo ra `tool_call_*` và `permission_request`.
- [Kiến trúc](../architecture/README.md): phần Rust.
