# Subagent

[Bản gốc tiếng Anh](../../usage/subagents.md)

Dùng subagent khi mô hình cần giao một tác vụ tập trung cho một run con và dùng câu trả lời của nó.

[← Hướng dẫn sử dụng](./README.md)

`subagents: True` cung cấp cho mô hình tool `Subagent`. Mỗi lệnh gọi khởi động một run
con trên cùng provider, model và kho lưu trữ, được ghi lại với id run của agent cha làm
`parent_run_id`; văn bản cuối cùng của run con là kết quả của lệnh gọi. `workers` giữ
các run con lại: chạy nền, hoặc để tiếp tục tiếp bằng `SubagentContinue`. `mailbox` cho
phép các session gửi message cho nhau.

Mọi đoạn mã ở đây đều dùng provider `scripted`, trả lời mọi lệnh gọi mô hình theo thứ
tự, kể cả của các run con. Vì vậy mỗi danh sách lượt trộn các lượt của agent cha với
các lượt của run con.

---

## Định nghĩa subagent

Một subagent của dự án là một tệp markdown, `<config_dir>/agents/<name>.md`, với cùng
quy tắc `config_dir` như [skill](./skills.md#bật-skill) (`.linch`, tương đối theo
workspace hoặc thư mục làm việc của server).

```markdown
---
name: reviewer
description: Reviews a change and lists concrete problems.
tools: [Read, Grep, Glob]
---
You are a code reviewer. Read the files you are pointed at and list each
problem with its file and line. Do not edit anything.
```

| Trường | Bắt buộc | Ý nghĩa |
|---|---|---|
| `description` | có | Được liệt kê trong mô tả của tool `Subagent` |
| `name` | không | Giá trị mà mô hình truyền làm `subagent_type`; là tên tệp khi không có |
| `tools` | không | Các tool mà run con được dùng, dưới dạng danh sách hoặc chuỗi phân tách bằng dấu phẩy; `Read(...)` được tính là `Read`; khi không có hoặc là `*` nghĩa là tất cả tool của agent cha |

Phần thân là chỉ dẫn của run con. Có sẵn hai định nghĩa: `verification`, cố gắng phá
vỡ công việc đã hoàn thành bằng `Read`, `Glob`, `Grep` và `Bash` (những tool mà agent
cha có) và kết thúc bằng một dòng `VERDICT:`, và một agent đa dụng với mọi tool của
agent cha, dùng khi lệnh gọi không nêu `subagent_type`. Một tệp tên `verification`
(không phân biệt hoa thường) thay thế bản tích hợp sẵn. Một tệp không phân tích được,
một symlink, hoặc một tệp thứ hai cùng tên sẽ bị bỏ qua; `Agent.skipped` liệt kê từng
tệp kèm lý do ([Cấu hình agent](./agent.md#skipped-definitions)).

Những gì run con nhận được:

| | Run con |
|---|---|
| Session | Riêng của nó, được đặt tên theo id worker của nó, và rỗng lúc đầu |
| System prompt | Của agent cha, rồi đến danh sách skill nếu nó có `Skill`, rồi `User-provided instructions:` và phần thân |
| Tool | Của agent cha, trừ `Subagent`, `SubagentContinue` và `TaskStop`; chỉ những tool trong `tools` khi nó liệt kê bất kỳ tool nào |
| Provider, model, kho lưu trữ, hook | Của agent cha |
| Phê duyệt | Nó không thể tạm dừng: một lệnh gọi cần phê duyệt sẽ bị từ chối, trừ khi một quy tắc quyền hoặc callback `permission_request` cho phép |

---

## Chạy một subagent

Chạy ví dụ này từ một thư mục dự án có tệp `reviewer` ở trên trong `.linch/agents/`.
Workspace cung cấp cho reviewer các tool read. Lần chạy thứ hai tiếp tục chính reviewer
đó (xem [Giữ lại và tiếp tục worker](#giữ-lại-và-tiếp-tục-worker)).

```python
import asyncio
import os

from linch_rust import AgentConfig, InitEvent, Server, SubagentEvent, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

# Every model call takes the next turn, the child's included.
turns = [
    # Run 1: the parent delegates, the reviewer answers, the parent sums up.
    tool_turn("c1", "Subagent", {
        "description": "Review the parser",
        "prompt": "Review src/parser.py for error handling.",
        "subagent_type": "reviewer",
    }),
    text_turn("parser.py:12 swallows ValueError."),
    text_turn("The reviewer found one problem."),
    # Run 2: the parent asks the same reviewer for a fix.
    tool_turn("c1", "SubagentContinue", {"to": "review-1:c1", "message": "Suggest a fix."}),
    text_turn("Re-raise it as ParseError."),
    text_turn("The fix is to re-raise it as ParseError."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "workspace": ".",
    "bash": False,
    "permissions": {"mode": "skip_dangerous", "project_root": os.getcwd()},
    "subagents": True,
    "workers": {"retain": True},
}


async def show(run) -> None:
    async for envelope in run:
        event = envelope.event
        if isinstance(event, InitEvent):
            print("parent tools:", event.tools)
        elif isinstance(event, SubagentEvent) and isinstance(event.event, InitEvent):
            print("child run:", event.event.run_id, event.event.tools)
        elif isinstance(event, ToolCallEndEvent):
            print("tool result:", event.result["content"][0]["text"])


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        await show(await agent.run("s1", "Get the parser reviewed.", run_id="review-1"))
        await show(await agent.run("s1", "How do we fix it?", run_id="review-2"))


asyncio.run(main())
```

```text
parent tools: ['Read', 'Write', 'Edit', 'Grep', 'Glob', 'Subagent', 'SubagentContinue']
child run: review-1:c1 ['Read', 'Grep', 'Glob']
tool result: parser.py:12 swallows ValueError.

[Worker ID: review-1:c1]
parent tools: ['Read', 'Write', 'Edit', 'Grep', 'Glob', 'Subagent', 'SubagentContinue']
child run: review-2:c1 ['Read', 'Grep', 'Glob']
tool result: Re-raise it as ParseError.
```

- Đầu vào của `Subagent` gồm `description` và `prompt` (cả hai bắt buộc),
  `subagent_type` và `run_in_background`. Một type không xác định thất bại kèm danh
  sách các type đã biết.
- Tool này có scope exec, nên ở chế độ `default` lệnh gọi sẽ hỏi trước; hãy phê duyệt
  như trong [Run](./runs.md). Nó chỉ chạy cùng các lệnh gọi read khác khi type của nó
  liệt kê tool và mỗi tool đó là tool read đã đăng ký.
- Mỗi event của run con đến stream của agent cha được bọc trong một `SubagentEvent`
  trước khi lệnh gọi kết thúc: `child_run_id`, `tool_use_id` (lệnh gọi của agent cha),
  `ts_ms` (thời gian riêng của run con) và `event`, event của run con được giải mã
  thành lớp riêng của nó. Các event bền vững được `Agent.events` phát lại trên run
  của agent cha.
- Run của run con là một run thông thường trong cùng kho lưu trữ:
  `agent.events("review-1:c1")` đọc journal riêng của nó, và event `init` của nó nêu
  agent cha trong `parent_run_id`.
- `ResultEvent.total_usage` của agent cha chỉ đếm các lệnh gọi mô hình của agent cha.
  Hãy cộng thêm các event `usage` được bọc để có toàn bộ cây.

---

## Worker chạy nền

Với `workers: {"background": True}`, một lệnh gọi có thể truyền `run_in_background:
True`. Lệnh gọi trả về ngay với một xác nhận, và run con tiếp tục bên cạnh agent cha.
Agent cũng nhận được `SubagentContinue` và `TaskStop`.

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ChildRunStatusEvent, Server, ToolCallEndEvent, UserEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    tool_turn("c1", "Subagent", {
        "description": "Find the flaky test",
        "prompt": "Run the suite three times and name the flaky test.",
        "run_in_background": True,
    }),
    text_turn("test_upload fails one run in three."),  # the worker
    text_turn("The worker says test_upload is flaky."),  # the parent
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "subagents": True,
    "workers": {"background": True},
    "config_dir": tempfile.mkdtemp(),  # no project subagents
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Find the flaky test.", run_id="ci-1")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print("ack:", event.result["content"][0]["text"])
            elif isinstance(event, ChildRunStatusEvent):
                print("status:", event.status)
            elif isinstance(event, UserEvent) and event.subtype != "prompt":
                for block in event.message["content"]:
                    if block["type"] == "text":
                        print("model reads:", block["text"])
        print("final:", (await run.result()).final_text)
        replayed = {e.event.type for e in await agent.events(run.id)}
        print("replayed:", sorted(replayed))


asyncio.run(main())
```

```text
status: running
ack: Worker 'ci-1:c1' started in background. You will receive a <task-notification> when it finishes.
status: completed
model reads: <task-notification><task-id>ci-1:c1</task-id><status>completed</status><summary>Worker 'Agent #1' finished.</summary><result>test_upload fails one run in three.</result></task-notification>
final: The worker says test_upload is flaky.
replayed: ['assistant', 'child_run_status', 'context_build', 'init', 'result', 'subagent', 'tool_call_end', 'tool_call_start', 'usage', 'user']
```

- Một `ChildRunStatusEvent` (`running`, rồi `completed`, `failed` hoặc `aborted`)
  đánh dấu worker trong nhật ký của agent cha.
- Việc worker kết thúc trở thành một `<task-notification>` (văn bản của Python Linch),
  được thêm vào các kết quả tool kế tiếp mà agent cha ghi log, hoặc được ghi log dưới
  dạng một message của người dùng.
- Khi agent cha trả lời bằng văn bản trong lúc một worker vẫn chạy, run không kết
  thúc: nó chờ worker và cho mô hình thêm một lượt cùng với thông báo. Một worker mà
  `TaskStop` đã dừng thì không có thông báo, nên việc chờ tiếp tục với các worker
  khác. Một agent cha vẫn kết thúc (bị hủy, thất bại, hết lượt) sẽ hủy các worker của
  nó, chờ chúng, và đánh dấu việc kết thúc của từng worker bằng một
  `ChildRunStatusEvent` trong lần ghi cuối của nó.
- Stream của agent cha điều khiển các worker của nó: hãy tiếp tục đọc run đến hết,
  hoặc gọi `Run.result()`.
- Một agent cha tạm dừng để chờ phê duyệt vẫn giữ các worker chạy trong lúc chờ, như
  Python Linch: mỗi worker ghi nhật ký riêng, và approve (hoặc resume) trong cùng
  server sẽ tiếp quản chúng và nghe được việc chúng kết thúc. Một worker đang ở giữa
  một lệnh gọi mô hình hoặc một tool tại thời điểm tạm dừng sẽ không bị gọi hay chạy
  lại.
- Đóng session của agent cha (`Agent.close_session`) hoặc đóng agent trong khi agent
  cha đang chờ sẽ hủy các worker của nó: mỗi worker kết thúc `aborted`, và một lần
  approve hoặc resume sau đó của agent cha sẽ nghe được điều đó.
- Một run có tối đa `workers.max_background` worker chạy cùng lúc (4 nếu không đặt).
  Một lệnh gọi `run_in_background` vượt quá giới hạn nhận một lỗi tool mà mô hình đọc,
  và không khởi động gì; một worker kết thúc sẽ trả lại chỗ của nó. Python Linch không
  có giới hạn này.
- Các event bền vững của worker đến nhật ký của agent cha dưới dạng được bọc, ở lượt
  kế tiếp của agent cha; mỗi event giữ `ts_ms` riêng của run con và `tool_use_id` của
  lệnh gọi đã sinh ra nó.

---

## Giữ lại và tiếp tục worker

Một worker nền luôn được giữ lại. Với `workers: {"retain": True}`, một worker
foreground cũng được giữ lại: kết quả của nó kết thúc bằng `[Worker ID: <id>]`, và
agent nhận được `SubagentContinue`. Một worker được giữ lại được định địa chỉ bằng id
của nó hoặc bằng tên hiển thị khi chỉ một worker có tên đó.

| | Giá trị |
|---|---|
| Id worker | `<parent run id>:<tool_use_id>`; truyền `run_id=` cho `Agent.run` để biết trước. Một lệnh gọi sau đó với `tool_use_id` mà run đã dùng (provider lặp lại id) nhận một lỗi tool và không khởi động gì |
| Tên hiển thị | Tên của định nghĩa, hoặc `Agent #N` cho worker đa dụng thứ N của session |
| Đầu vào của `SubagentContinue` | `{"to": <id or display name>, "message": ...}` |

`SubagentContinue` chạy lại worker trên session riêng của nó, với lịch sử còn nguyên,
như một run con foreground mới của run đang gọi (`review-2:c1` ở trên). Lệnh bị từ chối
khi worker đó vẫn đang chạy nền. Worker được giữ theo từng session của agent cha;
`Agent.close_session` đóng session của chúng cùng với session của agent cha.

---

## Dừng một worker nền

`TaskStop` nhận `{"task_id": <id or display name>, "reason": ...}`. Run của worker kết
thúc `aborted`, không có thông báo, và vẫn có thể được định địa chỉ cho
`SubagentContinue`. Một worker đã kết thúc thì được giữ nguyên: câu trả lời nói rằng nó
đã kết thúc và kết thúc ra sao (`completed`, `failed` hoặc `killed`), chứ không nói là
đã dừng.

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ChildRunStatusEvent, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def wait_for_ci(build: str) -> str:
    """Wait until a CI build finishes."""
    await asyncio.Event().wait()  # never finishes in this example
    return "green"


turns = [
    tool_turn("c1", "Subagent", {
        "description": "Watch CI",
        "prompt": "Wait for build 812 and report.",
        "run_in_background": True,
    }),
    tool_turn("w1", "wait_for_ci", {"build": "812"}),  # the worker
    tool_turn("c2", "TaskStop", {"task_id": "Agent #1", "reason": "build cancelled"}),
    text_turn("Stopped the CI watcher."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "subagents": True,
    "workers": {"background": True},
    "config_dir": tempfile.mkdtemp(),
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[wait_for_ci])
        run = await agent.run("s1", "Watch build 812.", run_id="ci-2")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name + ":", event.result["content"][0]["text"])
            elif isinstance(event, ChildRunStatusEvent):
                print("status:", event.status)
        print("final:", (await run.result()).final_text)


asyncio.run(main())
```

```text
status: running
Subagent: Worker 'ci-2:c1' started in background. You will receive a <task-notification> when it finishes.
TaskStop: Worker 'ci-2:c1' (Agent #1) stopped. Reason: build cancelled. Use SubagentContinue to re-engage if needed.
status: aborted
final: Stopped the CI watcher.
```

---

## Mailbox

`mailbox: True` cung cấp cho agent một mailbox và tool `send_message`, để gửi message
giữa các session của nó. Địa chỉ là id của một session. Với kho lưu trữ trong bộ nhớ,
mailbox nằm trong tiến trình; với kho Postgres, nó là bảng `linch_mailbox`, khóa theo
namespace và id session, nên một message nằm chờ ở đó qua lần khởi động lại server.

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent, UserEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    # The lead's run: it messages alice, then ends.
    tool_turn("c1", "send_message", {
        "to": "alice",
        "content": "Please own the migration.",
        "request_id": "req-1",
    }),
    text_turn("Asked alice."),
    # Alice's run: her prompt carries the message; she replies.
    tool_turn("c1", "send_message", {"to": "lead", "content": "Yes.", "in_reply_to": "req-1"}),
    text_turn("Replied."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "mailbox": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("lead", "Find an owner for the migration."):
            if isinstance(envelope.event, ToolCallEndEvent):
                print("lead:", envelope.event.result["content"][0]["text"])
        async for envelope in await agent.run("alice", "Check your messages."):
            event = envelope.event
            if isinstance(event, UserEvent) and event.subtype == "prompt":
                for block in event.message["content"]:
                    print("alice reads:", block["text"])


asyncio.run(main())
```

```text
lead: Message delivered to 'alice'.
alice reads: Check your messages.
alice reads: <peer-message><from>lead</from><type>message</type><request-id>req-1</request-id><content>Please own the migration.</content></peer-message>
```

- `send_message` nhận `to` và `content`, cùng `type` (mặc định `message`), `request_id`
  và `in_reply_to` tùy chọn. Người gửi là session đang gọi. Nó có scope exec.
- Một run rút hết hộp thư đến của session khi bắt đầu và sau mỗi lô tool. Mỗi message
  gia nhập prompt hoặc message kết quả tool đã được ghi log dưới dạng một khối
  `<peer-message>`, nên mô hình thấy nó ở request kế tiếp.
- Mỗi lần rút sẽ cho thuê (lease) các message nó lấy. Run xóa chúng khi event mang
  chúng đã bền vững, và trả lại chúng khi run kết thúc trước (prompt của nó bị từ chối,
  hoặc nó thất bại). Trên Postgres, một lease từ server đã dừng giữa chừng hết hạn sau
  60 giây và run kế tiếp lấy lại message: việc chuyển giao là ít nhất một lần.
- Id session của một worker là id worker của nó, nên một message gửi tới `review-1:c1`
  đến được worker đó ở lần chạy kế tiếp của nó, như một `SubagentContinue`. Tên hiển
  thị không phải là địa chỉ.
- Ý nghĩa của một message do prompt của bạn quyết định; kernel chỉ chuyển giao nó.
  `Correlator` của Python Linch ở đây là một API Rust
  (`linch_core::coordination::Correlator`); từ Python, mô hình tự khớp `request_id` và
  `in_reply_to`.

---

## Fork một session

Fork chưa có trong client Python. Ứng dụng chủ viết bằng Rust gọi
`Agent::fork_session(source, dest, before)`
(`crates/linch-core/src/agent/fork.rs`), sao chép `before` mục đầu tiên của nhật ký
một session (tất cả khi là `None`) sang một session mới, và từ chối một điểm cắt để
lại các lệnh gọi tool không có kết quả.

---

## Cái gì được lưu bền vững

| | Kho trong bộ nhớ | Kho Postgres |
|---|---|---|
| Một run con và journal của nó | Trong handle của agent | Bền vững, như mọi run |
| Event của run con được bọc trong nhật ký của agent cha | Có, các loại bền vững | Có |
| Một lệnh gọi foreground bị crash cắt ngang | Mất cùng server | Resume agent cha chạy lại lệnh gọi, tức resume run con; một lệnh gọi mô hình đã trả tiền sẽ không bị gọi hai lần |
| Một worker nền | Mất cùng server | Resume agent cha sẽ resume từng worker mà nhật ký của nó đánh dấu `running`, hoặc đọc lại worker đã kết thúc |
| Id worker và tên hiển thị | Bộ nhớ tiến trình | Bộ nhớ tiến trình; một run được resume nhận biết lại các worker của chính nó từ nhật ký |
| Message trong mailbox | Bộ nhớ tiến trình | Bền vững trong `linch_mailbox` cho đến khi một run ghi log chúng; một message được rút ngay trước khi crash sẽ đến lại khi lease của nó hết hạn |

Một agent cha tạm dừng để chờ phê duyệt giữ các worker chạy trong server cho đến khi
approve hoặc resume của nó tiếp quản chúng ở đó. Chỉ một agent cha được resume sau
crash, hoặc ở server khác, mới nhận lại các worker của nó từ nhật ký của chúng.

Hợp đồng của một run con là hợp đồng của agent cha, như agent được mở, cộng với định
nghĩa của run con: một tool được đăng ký sau đó không làm nó thay đổi. Một run con mà
định nghĩa đã thay đổi kể từ khi nó bắt đầu (ứng dụng chủ đã nạp lại subagent trước khi
resume agent cha) không thể tiếp tục: resume agent cha sẽ kết thúc nó ở trạng thái
`failed`, giải phóng session của nó, và agent cha nhận lỗi đó.

---

## Điểm khác so với Python Linch

| Python Linch | linch-rust |
|---|---|
| Id worker trông như `agent-a1b2` | `<parent run id>:<tool_use_id>`, như mô tả tool nêu |
| Một worker nền có thể sống lâu hơn run và thông báo cho một run sau | Run chờ các worker của nó trước khi kết thúc |
| Một worker đang chạy được đánh dấu `killed` sau khi khởi động lại | Nó được resume cùng run của agent cha (Postgres) |
| `Correlator`, `MailboxPlugin`, hộp thư đến bền vững | Mỗi lần rút đều có lease và được ack khi đã ghi log; mailbox bền vững với kho Postgres; `Correlator` chỉ có trong Rust |
| `generate_subagent_definition()` | Không có trong client; hãy tự viết tệp |
| Các handle `session.workers` | Không được cung cấp; hãy theo dõi `ChildRunStatusEvent` và `SubagentEvent` |
| `agent.fork_session(...)` | Chỉ Rust, `Agent::fork_session` |
| Nạp lại subagent | Không có trong client này hay trên RPC; hãy mở một agent mới. Ứng dụng chủ viết bằng Rust có `OpenedAgent::reload_subagents` và `Agent::reload_subagents` |

---

## Các trang liên quan

- [Skill](./skills.md): các quy trình prompt mà mô hình tự nạp
- [Task của session](./tasks.md): một danh sách việc cần làm cho mỗi session, không
  chia sẻ với worker
- [Deep agent](./deep-agent.md): một preset có danh sách subagent
- [Run](./runs.md): approve, resume và cancel
- [Event](./events.md): `SubagentEvent`, `ChildRunStatusEvent` và phát lại
