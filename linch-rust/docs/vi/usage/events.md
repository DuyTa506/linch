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

Các khoảng trống là có chủ đích: một seq mà client không bao giờ thấy không phải
là event bị mất, nên **chỉ nhìn khoảng trống thì không biết có event nào bị mất**.
Để kiểm tra, replay từ seq cuối cùng bạn có (bên dưới) và so sánh; kho lưu trữ là
bản ghi, stream chỉ là góc nhìn trực tiếp vào nó.

Replay bằng `await agent.events(run_id, after_seq=n)`: nó trả về các envelope bền
vững của run có seq lớn hơn `n`, theo thứ tự, từ các kho lưu trữ. Nó không bao giờ
trả về các event chỉ có trong stream. Vì vậy một client đã thấy seq `n` và mất
stream sẽ đọc đúng những gì nó đã bỏ lỡ.

## Bền vững và chỉ có trong stream

| Loại | Được lưu và replay |
|---|---|
| `partial_assistant`, `tool_progress`, `hook`, `cache_diagnostic` | Không: gửi ngay, không bao giờ replay |
| Mọi loại khác | Có: gửi khi nó đã bền vững |

Stream của một run kết thúc bằng `result` (đã xong) hoặc `suspended` (đang chờ
`approve` hoặc `answer`).

## Các loại event

### `InitEvent` (`init`)

Run đã bắt đầu.

| Trường | Ý nghĩa |
|---|---|
| `session_id`, `run_id` | Session và run |
| `model` | `spec.model` |
| `provider` | Id của provider: `anthropic`, `openai-chat`, `scripted`, ... |
| `tools` | Tên các tool mà model được cấp, theo thứ tự: của agent, rồi [final tool](./structured-output.md#the-final-tool) khi run tạo nó từ output schema (một `final_tool_name` đã đăng ký thì đã nằm trong danh sách của agent) |
| `parent_run_id` | Run cha, với run của một subagent; ngược lại là `None` |
| `trace_id` | `trace_id` được truyền cho `agent.run`, giữ trong cả run và các run của subagent; `None` khi không truyền ([Observability](./observability.md#correlate-logs-and-traces)) |
| `resumed` | `True` ở `init` mở đầu một lần resume hoặc approve của run, kèm các tool hiện tại của run; `False` ở `init` đầu tiên của run |
| `turn` | Lượt mà lời gọi tiếp tục: lượt của checkpoint run, với một `init` được resume; `0` ở lần đầu của run |
| `output_schema` | [Output schema](./structured-output.md#cho-một-run-schema-riêng) riêng của run, trên `init` đầu tiên của một run được bắt đầu với nó; `None` nếu không |
| `tool_choice` | [Tool choice](./structured-output.md#choose-which-tool-the-model-calls) riêng của run, trên `init` đầu tiên của một run được bắt đầu với nó; `None` nếu không |
| `final_tool` | Tên [final tool](./structured-output.md#the-final-tool) của run, trên `init` đầu tiên của một run có nó; `None` nếu không |

Mọi stream `run`, `resume` và `approve` đều bắt đầu bằng `init`. Một `init` được
resume là bền vững, nên các event được replay cho thấy điểm bắt đầu của mỗi lời
gọi: một run đã được resume một lần có hai event `init`, event đầu với
`resumed=False` và event của lần resume với `resumed=True` (cùng `turn` mà nó
tiếp tục). Một tool call mà resume thấy đang bị gián đoạn được đóng bằng một
`tool_call_end` có result mang `is_error`, nên replay của run đọc được một điểm
bắt đầu và một điểm kết thúc cho mỗi lời gọi.

### `UserEvent` (`user`)

Một message vai trò user đã vào cuộc hội thoại.

| Trường | Ý nghĩa |
|---|---|
| `message` | `{"role": "user", "content": [blocks]}` |
| `subtype` | `prompt` (prompt của bạn), `tool_result` (kết quả của một batch tool), `notification` (thông báo của runtime, chẳng hạn một worker nền vừa xong, hoặc phản hồi của một hook đòi thêm một lượt) hoặc `alignment` (một [steer](./runs.md#steer): văn bản bạn gửi cho run) |
| `steer_id` | Trên `alignment`: id mà `steer` đã trả về. Vắng mặt ở các subtype khác |

### `AssistantEvent` (`assistant`)

Một phản hồi hoàn chỉnh của model.

| Trường | Ý nghĩa |
|---|---|
| `message` | `{"role": "assistant", "content": [blocks]}`: các block `text`, `thinking`, `tool_use`. Một block `tool_use` là `{"type", "id", "name", "input"}`, và chỉ mang `raw_input` (các đối số đúng như model viết) khi chúng không phải một đối tượng JSON: `input` khi đó là `{}` và lời gọi được trả lời bằng lỗi, không bao giờ chạy ([Khắc phục sự cố](./troubleshooting.md)) |
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
| `budget` | Quyết định của [context budget](./memory.md#context-budget) cho request này: `{}` khi không có `spec.context_budget`; khi có thì gồm `max_tokens`, `used_tokens`, `remaining_tokens`, `trimmed`, `dropped` (một danh sách `{source, index, tokens}`, sắp theo callback và `index`, không theo thứ tự budget đã loại) và `over_by` khi các block pinned vượt mức trần. `ContextBuildEvent.dropped` trong Python đọc danh sách đó |
| `metadata` | Những gì các callback `context_build` trả về trong `metadata`, được gộp lại, key của callback đến sau thắng; `{}` nếu không có. Ở lượt mà `tool_choice` có tên không được request đưa ra, hoặc lựa chọn `required` bị một hook để lại không còn tool nào, `tool_choice_dropped` chứa tên tool (`required` cho lựa chọn đó) |

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
| `metadata` | Chi tiết chỉ dành cho host, không bao giờ hiển thị cho model: những gì một host tool trả về trong [`ToolOutput.metadata`](./tools.md#metadata-cho-host), hoặc nơi một kết quả được offload đã đi tới; với lời gọi thất bại, là object `details` của lỗi (`status` và `exit_code` của một lệnh `Bash`, xem [Workspace tool](./tools.md#workspace-tool)); `None` với tool không đặt gì |
| `error_code` | Mã máy đọc được của một lời gọi thất bại (`invalid_input`, `not_found`, ...), khi tool của nó cung cấp; ngược lại là `None`. `tool_interrupted` đánh dấu phần end thay thế của một lời gọi mà start đã bền vững khi crash đến: tác động của nó không rõ và nó không bao giờ được chạy lại ([Runs: Tool chạy tối đa một lần](./runs.md#tools-run-at-most-once-never-exactly-once)) |
| `cache_hit` | Việc [`spec.tool_cache`](./tools.md#tool_cache) có phục vụ kết quả hay không: tool không chạy, và `tool_call_start` của lời gọi được ghi cùng với event này, không phải trước nó. Ngược lại là `False` |
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
| `status` | `awaiting_approval`, hoặc `awaiting_input` với các câu hỏi `AskUser` |
| `pending_request_ids` | Các `tool_use_id` cần trả lời bằng `agent.approve`, hoặc bằng `agent.answer` khi là `awaiting_input` |

### `InputRequestEvent` (`input_request`)

Một lời gọi `AskUser` đang chờ người dùng: một event cho mỗi lời gọi, trước event
`suspended` có status `awaiting_input`. Xem [Tool](./tools.md#hỏi-người-dùng).

| Trường | Ý nghĩa |
|---|---|
| `request_id` | `tool_use_id` của lời gọi, là khóa của `Answer` của nó trong `agent.answer` |
| `kind` | `ask_user` |
| `prompt` | Nội dung các câu hỏi, mỗi câu một dòng |
| `questions` | Các câu hỏi, mỗi câu là `{"id", "header", "question", "options", "multi_select"}`, một option là `{"label", "description"}` |

### `CompactionEvent` (`compaction`)

View mà model nhìn thấy đã được rút ngắn
([`spec.compaction`](./agent.md#compaction)): `messages_before`,
`messages_after`, `tokens_before`, `tokens_after` (là giá trị ước tính), `measured_tokens`
(số đo mà ngưỡng đã dùng cho view, khi nó lớn hơn `tokens_before`: số token prompt
provider báo gần nhất cộng với ước lượng phần phát sinh sau đó; vắng mặt cho tới khi
provider báo usage), `strategy`
(`summary`, `micro` khi các kết quả tool cũ bị xóa mà không có lời gọi model, hoặc `clip`
khi kết quả tool của lượt cuối trong một prompt bị từ chối bị cắt theo một ngân
sách), `clipped_bytes` (số byte bị cắt khỏi văn bản để prompt vừa, tính cả dấu cắt: phần
cũ mà request tóm tắt bắt buộc đọc ít đi, hoặc phần view mất đi vì một `clip`; vắng mặt
khi không cắt gì), `duration_ms`, là thời gian từ lúc quyết định nén đến event này (lời gọi tóm tắt và
phép chiếu), và `forced`, là true khi nguyên nhân là provider từ chối một prompt vì quá
dài ([Memory](./memory.md#when-the-prompt-does-not-fit)). `rejected` được đặt khi bản
tóm tắt không được áp dụng và view không đổi (các số đếm bằng nhau). Nó bắt đầu bằng một
mã, `tool_call` (câu trả lời gọi một tool), `empty`, `context_length` (một request tóm tắt
bắt buộc bị từ chối vì dài hơn cửa sổ: run hỏi lại với một nửa phần cũ, hoặc kết thúc khi
đã gửi bốn request), hoặc `not_smaller` (view nó để lại được ước lượng không nhỏ hơn
view hiện tại), rồi nói điều xảy ra tiếp theo: ở đầu một lượt run tiếp tục và lượt đến hạn
kế tiếp hỏi lại (từ lần từ chối thứ hai liên tiếp: khi view đã lớn thêm một phần mười
context window hoặc provider từ chối prompt); một bản tóm tắt bắt buộc thì kết thúc run.
Một bản tóm tắt được áp dụng không có `rejected`, kể cả khi `tokens_after` của nó vẫn bằng hoặc trên
ngưỡng: hai bản như vậy liên tiếp giữ các lần hỏi sau như các lần từ chối ([Memory](./memory.md#compaction)). `not_smaller` được xét trên toàn bộ view có checkpoint trong đó (chính `tokens_before` và `tokens_after` mà event báo), không phải văn bản của model, nên một lần nén được chấp nhận luôn có `tokens_after < tokens_before`.

### `LoopGuardEvent` (`loop_guard`)

Run chạm giới hạn lượt của nó hoặc [loop guard](./agent.md#loop_guard) kích hoạt:
`reason` (`max_turns`, `repeated_tool_call`, `repeated_failures` hoặc
`idle_turns`), `detail` (ví dụ `Maximum turns (20) reached.` hoặc
`Tool 'Read' called with identical inputs and the same result 3 time(s)
(limit: 3).`) và
`action` (`stop`). Một event `error` (`TurnLimitError` hoặc `LoopGuardError`)
và một `result` có subtype `error` theo sau.

### `ToolRetryEvent` (`tool_retry`)

Một lần thử tool thất bại với một lỗi mà [`spec.tool_retry`](./tools.md#tool_retry)
retry, và lời gọi chạy lại sau một khoảng chờ: `tool_use_id`, `tool_name`,
`attempt` (lần sắp chạy, bắt đầu từ 2), `max_attempts`, `error_class` (`code` của
lỗi, hoặc `timeout`), `error` (thông điệp của lần thử thất bại, đã qua
`spec.redaction` khi nó bao phủ kết quả tool rồi cắt còn 512 byte) và
`delay_ms`. Nó được ghi bền trước khi chờ.

### `ToolInterruptedEvent` (`tool_interrupted`)

Một lời gọi đã bắt đầu mà phần end của nó bị một crash làm mất: `tool_use_id` và
`tool_name`. Một lần resume ghi nó cùng với `tool_call_end` thay thế cho phần đã
mất (một lỗi có `error_code` là `tool_interrupted`, và văn bản của nó cho model
biết tác động là không rõ), rồi không chạy lại lời gọi. `tool_use_id` của nó giữ
nguyên sau resume: khóa để hỏi hệ thống mà tool nói chuyện cùng xem tác động đã xảy
ra chưa ([Runs: Tool chạy tối đa một lần](./runs.md#tools-run-at-most-once-never-exactly-once)).
Một client cũ đọc nó như một event không xác định.
### `PolicyEvent` (`policy`)

[Reliability policy](./agent.md#policy) đã quyết định. `rule` là `prerequisite` hoặc
`completion`. Một lần từ chối prerequisite (`action` là `refuse`) nêu `tool` và
`tool_use_id` của lời gọi đã được trả lời bằng một lỗi và không chạy, cùng các tool
trong `missing` chưa thành công. Một quyết định completion nêu các `gates` mà câu trả lời
cuối cùng không đạt và các tool chúng còn cần (`missing`): `correct` gửi câu trả lời
lại cho model cho một lượt nữa, với `attempt` là số lần sửa đã dùng cho tới nay, và
`stop` kết thúc run `failed` (một `error` tên `PolicyGateError` và một `result` có
subtype `error` theo sau). `detail` là điều model đã được nói.

### `ProviderRetryEvent` (`provider_retry`)

Một lời gọi provider thất bại với lỗi có thể thử lại và run gọi lại cùng model
([`spec.provider_retry`](./providers.md#lỗi-và-thử-lại)): `model`, `retry` (lần
thử lại của model này trong turn, từ 1), `delay_ms` (thời gian chờ trước lời gọi
kế tiếp) và `error` (lỗi, có hình dạng như `error` của `ErrorEvent`). Event đến
trước lúc chờ. Hãy bỏ phần text dang dở của lời gọi lỗi: lời gọi kế tiếp trả lời
lại từ đầu.

### `ModelFallbackEvent` (`model_fallback`)

Run đã bỏ một model và gọi model kế tiếp trong
[`spec.fallback_models`](./providers.md#lỗi-và-thử-lại) cho đến hết run:
`from_model`, `to_model` và `error` (lỗi đã kết thúc các lời gọi của `from_model`).
Event của Python Linch có chuỗi `reason` thay cho `error`. Như với `provider_retry`,
hãy bỏ phần văn bản dở dang của lời gọi lỗi: model fallback trả lời lại từ đầu.

### `VerificationEvent` (`verification`)

Một lần kiểm tra câu trả lời cuối cùng thấy nó chưa đạt, hoặc đã hết lượt
retry: `verifier`, `action` (`retry`, `stop` hoặc `exhausted`), `feedback` và
`attempt`. Việc kiểm tra [output schema](./structured-output.md#run-kiểm-tra-gì)
phát nó với `verifier` là `output_schema`; một [verifier](./security.md#verifiers)
với `name` của nó; một lần retry của callback `stop` dưới `spec.verification`
với `policy_id` của nó.

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

### `CacheDiagnosticEvent` (`cache_diagnostic`), chỉ có trong stream

Vì sao prompt cache của provider đọc được đúng chừng đó ở một lời gọi, so với lời gọi
trước của run. Event chỉ được gửi khi
[`monitoring.cache_diagnostic`](./providers.md#tìm-hiểu-vì-sao-cache-bị-trượt) của agent
bật; nó không được lưu, không được replay và không nằm trong run contract. Khi tắt thì
không có gì được băm, và stream, log cùng các lần ghi y hệt như trước.

| Trường | Ý nghĩa |
|---|---|
| `reason` | Kiểm tra đầu tiên đúng, theo thứ tự: `model_changed`, `provider_changed`, `ttl_expired` (lâu hơn thời gian provider giữ một entry: 5 phút của Anthropic, hoặc 1 giờ khi request yêu cầu; adapter khác không khai báo), `system_changed`, `tools_changed`, `compaction` (hội thoại ngắn đi), `prefix_changed` (một message phía trước khác đi), `low_read` (prefix giữ nguyên nhưng provider chỉ đọc dưới một nửa số input token của lời gọi trước) hoặc `none` |
| `previous_cached` | Số token lời gọi trước đọc từ cache |
| `cached` | Số token lời gọi này đọc từ cache |
| `prompt_tokens` | Input token của lời gọi này, kể cả phần cached |
| `first_changed_message` | Chỉ số của message đầu tiên khác với request trước, hoặc `None` |

Một event theo sau mỗi lời gọi provider có báo input token, từ lời gọi thứ hai của một
lần chạy run trở đi (run được resume không còn gì trong bộ nhớ để so, nên lời gọi đầu
của nó không nói gì). Lời gọi dưới 1024 input token (2048 với OpenAI) không nói gì, vì
không provider nào cache ít hơn. Event chỉ chứa số đếm và một chỉ số, không bao giờ có
văn bản hay mã băm.

### `ErrorEvent` (`error`)

Một lỗi mà run đã ghi lại. `error` là một dict có `name`, `message` và
`retryable`; lỗi của provider còn có `kind` (`connect`, `transport`,
`status`, `context_length`, `invalid_request`, `stream`) và, với lỗi HTTP, cả `status`:

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
| `structured_output`, `structured_error` | Với một [output schema](./structured-output.md): câu trả lời đã parse, hoặc lý do câu trả lời cuối không thỏa schema (run khi đó kết thúc `error`); `None` khi không có |
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

### `ScheduleFireEvent` (`schedule_fire`)

Một worker đã bắt đầu run này cho một lần chạy của [lịch](./scheduler.md): có
`schedule_id` và `fire_at_ms`, thời điểm lần chạy đến hạn tính bằng mili giây
Unix. Event nằm ngay sau `init` đầu tiên của run, trong cùng một lần ghi; run
không do lịch bắt đầu, job xếp hàng và run được resume đều không có nó. Envelope của nó
được ghi là `ignorable`, nên một bản build Rust có trước loại này bỏ qua nó khi đọc
các event của run thay vì lỗi vì loại không biết.

### Các loại chỉ có trong journal

`ModelCallDeliveryEvent` (`model_call_delivery`), `ModelCallReceiptEvent`
(`model_call_receipt`) và `ModelCallUsageSettlementEvent`
(`model_call_usage_settlement`) là bản ghi của journal kế toán. Server ghi
chúng vào journal, không stream và cũng không trả về từ `Agent.events`, nên
client chỉ giải mã chúng từ các envelope đọc ra từ journal.
`ModelCallDeliveryEvent` có `logical_call_id`, `delivery_attempt_id`, `kind`
(`primary` hoặc `compaction`), `requested_model` và `turn_index`;
`ModelCallReceiptEvent` có cùng các id và `kind` cùng `disposition`
(`accepted`, `rejected`, `cancelled`, `failed` hoặc `not_dispatched`),
`duration_ms`, `actual_model`, `usage` và `cost_usd`;
`ModelCallUsageSettlementEvent` có `logical_call_id`, `usage`,
`budget_owner_id` và `cost_usd`.

`SteerReceivedEvent` (`steer_received`) là một [steer](./runs.md#steer) mà run
đã chấp nhận, với `steer_id` và `text` (sau khi redact prompt). Nó được ghi
trước khi `steer` trả lời và không bao giờ được stream; event `user` có subtype
`alignment` cùng `steer_id` đánh dấu nó đã được giao cho model.

### Các loại mà server chưa phát

Phần lõi khai báo các loại này và client giải mã mỗi loại thành class của nó,
nhưng hiện chưa có run nào phát chúng:

| Class (`type`) | Trường |
|---|---|
| `PromptCacheAdvisoryEvent` (`prompt_cache_advisory`) | `reason` (`tool_set_changed` hoặc `model_changed`), `detail` |
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
