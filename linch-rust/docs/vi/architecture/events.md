# Events

[Bản gốc tiếng Anh](../../architecture/events.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Một run báo cáo mọi việc nó làm dưới dạng một stream các `Event`, mỗi event nằm trong
một `Envelope` có phiên bản. Chính các envelope đó là event log bền vững của run trong
run store, là các notification `run.event` của giao thức RPC, và là thứ mà lớp
OpenTelemetry cùng các báo cáo run đọc. Bộ từ vựng nằm trong `linch-core`
(`crates/linch-core/src/events/`); client Python giải mã nó trong
`python/src/linch_rust/events.py`. Dùng trang này khi bạn thêm hoặc đổi một loại event,
giải mã envelope bên ngoài lõi, hoặc đổi nơi lưu một loại event.

Linch bản Python có cùng ý tưởng ([phân loại event](../../../../docs/architecture/events.md)).
Điểm khác trong Rust:

- `Event` là một enum đóng duy nhất với codec viết tay, và mọi payload đều từ chối các
  trường không biết khi giải mã. Một wire version `v` khác sẽ bị từ chối.
- `SystemEvent` của Python (`type: system`, `subtype: init`) ở đây là `init`.
  `BackgroundWorkerEvent` của Python không có loại riêng: các thay đổi trạng thái của
  một worker là các event `child_run_status`.
- Các bản ghi ledger lời gọi model của Python (`intent`, `dispatch_started`,
  `abandoned`, `projection`) là ba loại chỉ dành cho journal:
  `model_call_delivery`, `model_call_receipt` và
  `model_call_usage_settlement` (`crates/linch-core/src/events/model_call.rs:1-9`).
- Không có lifecycle bus để subscribe. Các observer đọc envelope từ stream của run hoặc
  từ store; hook là một cơ chế riêng (`crates/linch-core/src/hooks.rs`).

## Enum event

`Event` (`crates/linch-core/src/events/event/mod.rs:48`) có một variant cho mỗi loại.
Mỗi payload là một struct thuần trong `events/kinds/*.rs` hoặc
`events/model_call.rs`, derive `Serialize` và `Deserialize` với
`deny_unknown_fields` và không có trường `type` riêng; `Event` thêm và
bỏ tag bằng tay (`events/kinds/mod.rs:1-9`). `Event::type_name`
(`crates/linch-core/src/events/event/name.rs:12`) cho ra tag mà không cần mã hóa.

| `type` | Payload, trong `events/` | Stored | Delivered | Ghi chú |
|---|---|---|---|---|
| `init` | `InitEvent`, `kinds/lifecycle.rs:19` | yes | yes | Một event cho mỗi lần gọi của run: lúc bắt đầu, và mỗi lần resume hoặc approve, với `resumed` và `turn` mà nó tiếp tục |
| `user` | `UserEvent`, `kinds/lifecycle.rs:62` | yes | yes | `subtype`: `prompt`, `tool_result`, `alignment`, `notification` |
| `assistant` | `AssistantEvent`, `kinds/lifecycle.rs:72` | yes | yes | |
| `partial_assistant` | `PartialAssistantEvent`, `kinds/lifecycle.rs:82` | no | yes | |
| `usage` | `UsageEvent`, `kinds/control.rs:13` | yes | yes | |
| `tool_call_start` | `ToolCallStartEvent`, `kinds/tooling.rs:11` | yes | yes | |
| `tool_call_end` | `ToolCallEndEvent`, `kinds/tooling.rs:29` | yes | yes | `tool_use_id` và `is_error` nằm trong `result` |
| `tool_progress` | `ToolProgressEvent`, `kinds/tooling.rs:98` | no | yes | |
| `permission_request` | `PermissionRequestEvent`, `kinds/tooling.rs:127` | yes | yes | |
| `input_request` | `InputRequestEvent`, `kinds/suspend.rs:22` | yes | yes | Chưa được phát ở revision này |
| `suspended` | `SuspendedEvent`, `kinds/suspend.rs:53` | yes | yes | Kết thúc stream của một run đã suspend thay cho `result` |
| `context_build` | `ContextBuildEvent`, `kinds/control.rs:44` | yes | yes | |
| `compaction` | `CompactionEvent`, `kinds/control.rs:64` | yes | yes | |
| `loop_guard` | `LoopGuardEvent`, `kinds/control.rs:105` | yes | yes | |
| `budget` | `BudgetEvent`, `kinds/control.rs:127` | yes | yes | Một `budget` của spec (`crates/linch-core/src/budget.rs`): `warning` ngay sau event `usage` vượt tỷ lệ, một lần cho mỗi cây run; `exceeded` trong lần ghi terminal, trước `error` của nó |
| `prompt_cache_advisory` | `PromptCacheAdvisoryEvent`, `kinds/control.rs:168` | yes | yes | Chưa được phát ở revision này |
| `skills_loaded` | `SkillsLoadedEvent`, `kinds/skills.rs:10` | yes | yes | |
| `skill_invoked` | `SkillInvokedEvent`, `kinds/skills.rs:18` | yes | yes | |
| `skill_completed` | `SkillCompletedEvent`, `kinds/skills.rs:35` | yes | yes | |
| `subagent` | `SubagentEvent`, `event/mod.rs:118` | như event được bọc | như event được bọc | Xem [bọc subagent](#bọc-subagent) |
| `child_run_status` | `ChildRunStatusEvent`, `kinds/suspend.rs:66` | yes | yes | |
| `workflow` | `WorkflowEvent`, `kinds/skills.rs:76` | yes | yes | Không bao giờ nằm trong log của run: các workflow của `linch-ext` chuyển nó cho event sink của chúng ([extensions](./ext.md)) |
| `verification` | `VerificationEvent`, `kinds/control.rs:191` | yes | yes | Chưa được phát ở revision này |
| `hook` | `HookEvent`, `kinds/hook.rs:14` | no | yes | |
| `error` | `ErrorEvent`, `kinds/lifecycle.rs:90` | yes | yes | |
| `result` | `ResultEvent`, `kinds/lifecycle.rs:115` | yes | yes | Kết quả terminal |
| `model_call_delivery` | `ModelCallDeliveryEvent`, `model_call.rs:33` | yes | no | |
| `model_call_receipt` | `ModelCallReceiptEvent`, `model_call.rs:72` | yes | no | |
| `model_call_usage_settlement` | `ModelCallUsageSettlementEvent`, `model_call.rs:110` | yes | no | Chưa được phát ở revision này |
| loại khác | `IgnorableEvent`, `event/ignorable.rs:52` | yes | yes | Chỉ nằm trong một envelope `ignorable` |

"Stored" nghĩa là được ghi vào event log của run; "Delivered" nghĩa là được stream của
run yield ra, và do đó gửi tới client. Hai cột này là
[lớp lưu trữ bền vững](#các-lớp-lưu-trữ-bền-vững).

## Envelope

`Envelope` (`crates/linch-core/src/events/envelope.rs:163`):

| Trường | Ý nghĩa |
|---|---|
| `v` | Wire version; chỉ `WIRE_VERSION`, tức 1, được đọc hoặc ghi (`envelope.rs:15`) |
| `seq` | Số thứ tự của run. Các event bền vững nhận seq liên tiếp từ 1; một event chỉ có trong stream mang seq bền vững gần nhất, nên các seq trên stream lặp lại và có khoảng trống |
| `run_id` | Run mà event thuộc về |
| `ts_ms` | Đồng hồ thực của bên tạo ra, tính bằng mili giây Unix, lúc nó tạo envelope (`envelope.rs:170-174`). Hãy sắp xếp theo `seq`, không bao giờ theo `ts_ms`: một run gặp sự cố và lần resume của nó có thể chạy trên các đồng hồ không khớp nhau |
| `ignorable` | Cam kết của bên tạo rằng một `event.type` không biết có thể bị bỏ qua. Không xuất hiện trên wire khi là false |
| `event` | Event |

Journal của run tạo mọi envelope (`crates/linch-core/src/journal.rs:297`):
`v` là `WIRE_VERSION`, `ts_ms` là thời điểm event được stage, và
`ignorable` là false. Thời điểm một envelope trở nên bền vững theo quy tắc
[group commit](./stores.md#số-lần-ghi-của-group-commit): một event bền vững
được giữ lại đến lần ghi barrier kế tiếp, lần ghi đó mang nó đi.

## Các lớp lưu trữ bền vững

`Persistence` (`crates/linch-core/src/events/event/mod.rs:33`) có ba lớp, và
`Event::persistence` (`event/mod.rs:282`) ánh xạ mỗi loại vào một lớp. Câu lệnh match đó
không có nhánh wildcard, nên một loại mới không biên dịch được cho đến khi nó được
phân loại.

| Lớp | Các loại | Đi đến đâu |
|---|---|---|
| `StreamOnly` | `partial_assistant`, `tool_progress`, `hook` | `Journal::stage` trả nó ngay lập tức với seq bền vững gần nhất và không bao giờ lưu nó (`crates/linch-core/src/journal.rs:123-130`). Tiến độ của tool đi thẳng vào outbox của stream (`crates/linch-core/src/runner/sink.rs:184-189`) |
| `Both` | mọi loại đã biết khác, và `IgnorableEvent` | Được stage với seq kế tiếp, giữ đến lần ghi barrier kế tiếp, rồi được delivery theo thứ tự seq (`journal.rs:310-316`) |
| `JournalOnly` | ba loại `model_call_*` | Được stage với seq kế tiếp và lưu như `Both`, nhưng không bao giờ được delivery. `Agent::events` cũng bỏ chúng ra (`crates/linch-core/src/agent.rs:380-396`), và method RPC `run.events`, vốn gọi nó, cũng vậy |

Các bản ghi chỉ dành cho journal là thứ mà resume đọc để kết thúc một lời gọi model mà
nó có thể đã trả phí rồi (`crates/linch-core/src/journal/reconcile.rs`).
Hãy đọc chúng trực tiếp bằng `RunStore::events`. Chúng chiếm seq, đó là lý do một client
thấy khoảng trống trong các seq bền vững.

## Wire format và phiên bản

Một envelope là một đối tượng JSON:

```json
{"v":1,"seq":7,"run_id":"r1","ts_ms":1762400000123,"event":{"type":"subagent","child_run_id":"r1:c1","tool_use_id":"c1","ts_ms":1762400000000,"event":{"tool_use_id":"c9","tool_name":"Read","message":"reading","type":"tool_progress"}}}
```

Đó là đầu ra của ví dụ bên dưới. Các quy tắc:

- **Mã hóa** là một lượt duy nhất, không có cây JSON ở giữa, nên bộ ghi RPC
  và các store đi thẳng từ một `Envelope` sang văn bản của nó
  (`envelope.rs:249-270`). Các trường của payload đến trước, rồi đến `type`
  (`crates/linch-core/src/events/event/mod.rs:131-148`); một event `subagent`
  ghi `type` trước (`event/mod.rs:362-370`). Bộ giải mã không được
  phụ thuộc vào thứ tự khóa.
- **Lược bỏ:** một `Option` vắng mặt, một cờ false (`resumed`, `is_error`), một
  bộ đếm bằng không (`turn`, `attempt`, `occurrence`) và một chuỗi rỗng của
  `WorkflowEvent` được bỏ khỏi wire và đọc lại thành giá trị mặc định của chúng
  (`crates/linch-core/src/events/kinds/mod.rs:25-30`).
- Các trường **tiền** chỉ mã hóa số hữu hạn; NaN hoặc vô cực là một
  `EventError::EncodePayload`, không bao giờ là một `null` âm thầm
  (`crates/linch-core/src/events/wire.rs:81-122`).
- **`canonical_output: null`** ở phần kết thúc của tool được giữ phân biệt với một
  trường vắng mặt (`crates/linch-core/src/events/kinds/tooling.rs:36-50`).
- **Giải mã nghiêm ngặt.** `Envelope::from_value` (`envelope.rs:204`) từ chối
  một trường bị thiếu, một thành viên mà nó không biết, và mọi `v` khác
  `WIRE_VERSION` (`EventError::UnsupportedVersion`). Một `type` đã biết luôn
  giải mã payload của nó một cách nghiêm ngặt. Một `type` mà bản build không biết là
  `EventError::UnknownType`, trừ khi envelope là `ignorable`, khi đó nó
  trở thành một `IgnorableEvent` (`event/mod.rs:245-258`), giữ mọi
  khóa và giá trị của đối tượng event nhưng không giữ thứ tự của chúng. Ignorable không
  bao giờ cứu một loại đã biết bị sai định dạng.
- **Mã hóa lại** một envelope có event chứa một `IgnorableEvent`, ở bất kỳ độ sâu nào,
  sẽ ghi `ignorable: true` bất kể trường đó nói gì, để mọi envelope mã hóa được
  cũng giải mã được (`envelope.rs:156-161`, `envelope.rs:258`).

`EventError` (`envelope.rs:72`) là kiểu lỗi duy nhất cho mọi lỗi mã hóa và
giải mã. `Envelope` implement `FromStr` cho văn bản và
`Deserialize`, cả hai đều nghiêm ngặt.

Vì các payload từ chối trường không biết, một bản build không biết một trường
không thể đọc một envelope có trường đó. Một trường tùy chọn mới với
`#[serde(default)]` giữ cho các envelope đã lưu vẫn đọc được bởi bản build mới;
chiều ngược lại thì không. Client và server được phát hành trong cùng một
wheel, nên chúng khớp nhau. Không có gì trong cây mã đọc hai phiên bản, và
`WIRE_VERSION` chưa rời khỏi 1.

## Bọc subagent

Run con của một subagent là một run riêng, với journal và log riêng của nó.
Mỗi event của nó cũng đến log của run cha bên trong một event `subagent`
(`crates/linch-core/src/events/event/mod.rs:118-129`):

| Trường | Ý nghĩa |
|---|---|
| `child_run_id` | Run con, là `<parent run id>:<tool_use_id>` của lời gọi đã sinh ra nó |
| `tool_use_id` | Lời gọi của run cha chạy run con: lời gọi `Subagent`, hoặc một lời gọi `SubagentContinue` |
| `ts_ms` | `ts_ms` của envelope con. Các event của một run con chạy nền đến log của run cha ở lượt kế tiếp của run cha, nên `ts_ms` bên ngoài muộn hơn |
| `event` | Event của run con |

`seq` của run con không được giữ lại; hãy đọc log riêng của run con để lấy nó.

- **Lưu trữ bền vững đệ quy** (`event/mod.rs:274-281`): một `tool_progress` được bọc là
  chỉ có trong stream, một `model_call_receipt` được bọc là chỉ dành cho journal.
- **Một run con foreground** được điều khiển bên trong lời gọi. Mọi envelope mà stream
  của nó yield ra, kể cả các envelope chỉ có trong stream, đều được bọc và giao cho run
  cha qua context của lời gọi (`crates/linch-core/src/subagents/run.rs:147-164`).
  Một event bền vững được bọc đi cùng lần ghi kế tiếp của run cha, muộn nhất là
  phần kết thúc của lời gọi.
- **Một run con background** chỉ giữ các envelope bền vững của nó
  (`crates/linch-core/src/subagents/background.rs:279-292`); run cha stage
  chúng ở lượt kế tiếp (`crates/linch-core/src/runner/inbox.rs:113-121`).
- **Độ sâu** bị giới hạn ở 8 khi giải mã (`MAX_SUBAGENT_DEPTH`,
  `envelope.rs:17-23`) để phòng một stream độc hại. Một run con không thể
  sinh thêm, nên log thực tế lồng một cấp.
- Event lồng bên trong kế thừa cờ `ignorable` của envelope ngoài.

[Skill và subagent](./skills-subagents.md) nói về thời điểm các run con chạy.

## Client Python

`python/src/linch_rust/events.py` có một dataclass frozen cho mỗi loại được delivery,
với tên wire là biến lớp `type`, được gom trong union `Event` (`events.py:349`) và bảng
`_KINDS` (`events.py:379`).

- `decode_event` (`events.py:436`) dựng lớp ứng với `type`, chỉ truyền
  các thành viên mà lớp có, nên một thành viên mà client không biết sẽ bị
  bỏ. Một event `subagent` giải mã event con của nó theo cách tương tự. Mọi
  `type` khác trở thành `UnknownEvent(type, raw)` (`events.py:342`).
- `decode_envelope` (`events.py:454`) đọc `seq`, `run_id`, `ts_ms` và
  `event`, và raise `LinchError` với `v` khác `WIRE_VERSION` của nó
  (1, `events.py:19`), kể cả khi thiếu `v`. Nó không đọc
  `ignorable`.
- Các giá trị lồng nhau (message, usage, các block kết quả tool) vẫn là
  các dict JSON mà server đã gửi.
- Các loại chỉ dành cho journal không có lớp; server không bao giờ gửi chúng.

Vậy hai phía khác nhau có chủ ý: Rust từ chối những gì nó không biết, còn
client chấp nhận. Một client cũ hơn server của nó vẫn hoạt động nhờ
`UnknownEvent`; một bộ đọc Rust thì không.

### Test parity

`test_every_rust_event_kind_has_a_class_with_its_fields`
(`python/tests/test_events.py:133`) cố định các lớp Python theo các payload Rust.
`rust_kinds` (`test_events.py:114-130`) đọc mã nguồn Rust dưới dạng văn bản:

- các variant của `pub enum Event {` trong `events/event/mod.rs`, được viết dưới dạng
  `Name(Payload),` mỗi dòng một variant;
- các nhánh `Serialize` được viết dưới dạng `Event::Name(e) => tagged("name", e)`;
- phần thân `pub struct Payload {` của mỗi payload trong `events/event/mod.rs` hoặc
  `events/kinds/*.rs`, và các dòng `pub field:` của nó;
- `subagent` bằng tay, và không có loại nào trong `JOURNAL_ONLY`
  (`test_events.py:26`).

Sau đó nó khẳng định rằng tập các loại và tên trường của mỗi loại bằng với
tập của các lớp Python. Nó chỉ so sánh tên, không so sánh kiểu hay tính tùy chọn. Một loại
được delivery mà payload nằm trong `model_call.rs`, hoặc một nhánh hay struct được viết
theo cách khác, sẽ làm nó thất bại. Nó không cần server:

```sh
cd linch-rust/python && PYTHONPATH=src uv run pytest tests/test_events.py
```

## Ví dụ

Đoạn này biên dịch và chạy được với `linch-core` ở revision này, và in ra
envelope được trình bày trong phần [wire format](#wire-format-và-phiên-bản):

```rust
use linch_core::events::kinds::ToolProgressEvent;
use linch_core::{
    Envelope, Event, EventError, IgnorableEvent, JsonObject, Persistence, RunId, SubagentEvent,
    WIRE_VERSION,
};

fn main() {
    // A child's stream-only progress, wrapped for its parent.
    let progress = Event::ToolProgress(ToolProgressEvent {
        tool_use_id: "c9".into(),
        tool_name: "Read".into(),
        message: "reading".into(),
        data: None,
    });
    let wrapped = Event::Subagent(SubagentEvent {
        child_run_id: RunId::new("r1:c1"),
        tool_use_id: "c1".into(),
        ts_ms: 1_762_400_000_000,
        event: Box::new(progress),
    });
    assert_eq!(wrapped.type_name(), "subagent");
    assert_eq!(wrapped.persistence(), Persistence::StreamOnly);

    // One envelope, encoded in one pass and decoded back.
    let envelope = Envelope {
        v: WIRE_VERSION,
        seq: 7,
        run_id: RunId::new("r1"),
        ts_ms: 1_762_400_000_123,
        ignorable: false,
        event: wrapped,
    };
    let text = serde_json::to_string(&envelope).unwrap();
    println!("{text}");
    assert_eq!(text.parse::<Envelope>().unwrap(), envelope);

    // Another wire version is refused, not guessed at.
    let newer = text.replacen("\"v\":1", "\"v\":2", 1);
    let refused = newer.parse::<Envelope>().unwrap_err();
    assert!(matches!(refused, EventError::UnsupportedVersion { found: 2, .. }));

    // An unknown kind decodes only when the producer marked it ignorable.
    let unknown = r#"{"v":1,"seq":8,"run_id":"r1","ts_ms":1,"event":{"type":"brand_new","x":1}}"#;
    assert!(matches!(
        unknown.parse::<Envelope>(),
        Err(EventError::UnknownType(_))
    ));
    let marked = unknown.replacen("\"event\"", "\"ignorable\":true,\"event\"", 1);
    let kept = marked.parse::<Envelope>().unwrap();
    let Event::Ignorable(sentinel) = &kept.event else {
        panic!("an ignorable envelope keeps the unknown kind");
    };
    assert_eq!(sentinel.type_name(), "brand_new");
    // It encodes back to the same fields (serde_json sorts them by key).
    let again: serde_json::Value = serde_json::to_value(&kept).unwrap();
    assert_eq!(again, serde_json::from_str::<serde_json::Value>(&marked).unwrap());

    // Ignorable never rescues a malformed known kind.
    let bad = r#"{"v":1,"seq":9,"run_id":"r1","ts_ms":1,"ignorable":true,
                  "event":{"type":"tool_progress"}}"#;
    assert!(matches!(
        bad.parse::<Envelope>(),
        Err(EventError::InvalidPayload { .. })
    ));

    // Encoding sets the flag whenever the event holds a sentinel.
    let sentinel = IgnorableEvent::new("brand_new", JsonObject::new()).unwrap();
    let carried = Envelope {
        ignorable: false,
        event: Event::Ignorable(sentinel),
        ..envelope
    };
    let text = serde_json::to_string(&carried).unwrap();
    assert!(text.contains("\"ignorable\":true"));
}
```

Client đọc cùng dòng đó, và chấp nhận ở những chỗ Rust nghiêm ngặt. Đoạn này
chạy bằng `cd linch-rust/python && PYTHONPATH=src uv run python <file>`:

```python
import json

from linch_rust import SubagentEvent, ToolProgressEvent, UnknownEvent
from linch_rust.events import decode_envelope

line = (
    '{"v":1,"seq":7,"run_id":"r1","ts_ms":1762400000123,"event":{"type":"subagent",'
    '"child_run_id":"r1:c1","tool_use_id":"c1","ts_ms":1762400000000,"event":'
    '{"tool_use_id":"c9","tool_name":"Read","message":"reading","type":"tool_progress"}}}'
)
envelope = decode_envelope(json.loads(line))
assert isinstance(envelope.event, SubagentEvent)
assert isinstance(envelope.event.event, ToolProgressEvent)
assert (envelope.seq, envelope.event.child_run_id) == (7, "r1:c1")

# The client is tolerant: it reads neither `v` nor `ignorable`, drops
# members it does not know, and keeps an unknown kind as UnknownEvent.
newer = json.loads(line.replace('"v":1', '"v":2', 1))
newer["event"]["event"]["extra"] = True
assert isinstance(decode_envelope(newer).event.event, ToolProgressEvent)
unknown = {"v": 1, "seq": 8, "run_id": "r1", "ts_ms": 1, "event": {"type": "brand_new", "x": 1}}
assert decode_envelope(unknown).event == UnknownEvent("brand_new", {"x": 1})
print("ok")
```

## Thêm một loại event

Nhiều danh sách nêu tên mọi loại event. Trình biên dịch kiểm tra một số, test kiểm tra
những cái khác, và có một danh sách không có gì kiểm tra:

| Bước | Ở đâu | Cái gì bắt lỗi nếu bỏ sót |
|---|---|---|
| 1. Struct payload: `Serialize`, `Deserialize`, `deny_unknown_fields`, không có trường `type`; `default` và `skip_serializing_if` trên các trường tùy chọn; `serialize_finite_f64` hoặc `_option` trên trường tiền | `events/kinds/<topic>.rs` (được `kinds/mod.rs` re-export) | Review |
| 2. Variant, viết dưới dạng `Name(NameEvent),` trên một dòng | `Event`, `event/mod.rs:48` | Test parity đọc dòng này |
| 3. Nhánh giải mã | `Event::decode`, `event/mod.rs:169` | Không phải trình biên dịch: match kết thúc bằng `other =>`. `known_event_types_matches_decode_exhaustively` (`event/tests.rs:29`) |
| 4. Nhánh mã hóa, viết dưới dạng `tagged("name", e)` | `impl Serialize for Event`, `event/mod.rs:333` | Trình biên dịch; test parity đọc nhánh này |
| 5. Tên wire | `Event::type_name`, `event/name.rs:12` | Trình biên dịch; `type_name_is_the_wire_type_tag` (`event/tests.rs:263`) |
| 6. Lớp lưu trữ bền vững | `Event::persistence`, `event/mod.rs:282`; với `StreamOnly` hoặc `JournalOnly`, cũng cần các tập trong `persistence_classification_covers_every_kind` (`event/tests.rs:195`) | Trình biên dịch, rồi đến test đó |
| 7. Tag trong danh sách đã biết | `KNOWN_EVENT_TYPES`, `event/ignorable.rs:8` | `known_event_types_matches_decode_exhaustively`; nếu thiếu, `IgnorableEvent::new` sẽ chấp nhận tag đó |
| 8. Một mẫu | `one_of_every_kind`, `event/tests/samples.rs:14` | `persistence_classification_covers_every_kind` đếm các mẫu |
| 9. Một builder proptest, thêm vào `Union` | `arbitrary/builders.rs` hoặc `arbitrary/accounting.rs`, và `arbitrary::event`, `arbitrary/mod.rs:185` | **Không có gì.** Bỏ sót builder chỉ làm giảm độ phủ round-trip của `every_kind_round_trips_through_decode` |
| 10. Lớp Python, trong union `Event` và `_KINDS`; hoặc tag trong `JOURNAL_ONLY` cho loại chỉ dành cho journal | `python/src/linch_rust/events.py`, `python/tests/test_events.py:26` | Test parity |
| 11. Điểm phát event | runner (`crates/linch-core/src/runner/`) hoặc một tool thông qua context của nó | Review: một loại bền vững đi cùng barrier kế tiếp, không bao giờ qua nó |
| 12. Các bên tiêu thụ match trên `Event` | `crates/linch-otel/src/observer.rs`, `crates/linch-ext/src/reports.rs`, `crates/linch-ext/src/reports/summary.rs` | Không phải trình biên dịch: chúng kết thúc bằng các nhánh wildcard |
| 13. Tài liệu | bảng của trang này; `docs/usage/events.md` cho một loại được delivery | Review |

Sau đó chạy `cargo test -p linch-core events` và test parity ở trên.
Thêm một variant là một thay đổi wire đối với các bộ đọc cũ hơn: một bản build Rust cũ
hơn đọc loại mới thành `UnknownType` trừ khi bên tạo đánh dấu envelope là
`ignorable`, và lõi không bao giờ làm vậy.

## Test

| Test | Cố định điều gì |
|---|---|
| `events/event/tests.rs` | Lớp lưu trữ bền vững của mọi loại, danh sách loại đã biết so với giải mã, round trip của một mẫu cho mỗi loại và của 256 event ngẫu nhiên có subagent lồng nhau, tên wire, kích thước phần kết thúc của tool (`the_tool_end_is_no_larger_than_the_result`, `event/tests.rs:284`) |
| `events/envelope/tests.rs` | Độ nghiêm ngặt của envelope, các phiên bản, quy tắc `ignorable`, mã hóa một lượt |
| Unit test `kinds/*.rs` | Quy tắc ở mức trường: các trường bắt buộc của result, giá trị mặc định bị lược bỏ, `canonical_output: null`, tiền hữu hạn |
| `python/tests/test_events.py` | Test parity, và việc giải mã các lớp skill, subagent, trạng thái run con và phần kết thúc của tool |
| `python/tests/test_protocol.py` | Giải mã chấp nhận và `UnknownEvent` |

---

Quay lại [mục lục kiến trúc](./README.md).
