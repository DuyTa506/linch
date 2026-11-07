# Task của session

[Bản gốc tiếng Anh](../../usage/tasks.md)

Dùng task của session khi mô hình cần giữ một danh sách việc cần làm cho công việc của chính nó, kéo dài qua các run của một cuộc hội thoại.

[← Hướng dẫn sử dụng](./README.md)

`tasks: True` trong cấu hình agent cung cấp cho mô hình bốn tool: `TaskCreate`,
`TaskList`, `TaskGet` và `TaskUpdate`. Task là các hàng trong kho session, khóa theo
namespace và id của session: run kế tiếp trong cùng session tìm thấy chúng, còn một
session khác bắt đầu mà không có task nào. Run con của một subagent dùng chung task của
session đã khởi chạy nó. Chỉ các lệnh gọi tool và kết quả của chúng đi vào lịch sử hội thoại.

---

## Bật task

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

# The scripted provider replays these turns: the model plans two tasks,
# links them, finishes the first, and lists what is left.
turns = [
    tool_turn("c1", "TaskCreate", {"subject": "Draft the release notes", "description": "One page"}),
    tool_turn("c2", "TaskCreate", {"subject": "Publish the release", "description": "Tag and push"}),
    tool_turn("c3", "TaskUpdate", {"id": "2", "add_blocked_by": ["1"]}),
    tool_turn("c4", "TaskUpdate", {"id": "1", "status": "completed"}),
    tool_turn("c5", "TaskList", {}),
    tool_turn("c6", "TaskGet", {"id": "2"}),
    text_turn("Release notes are done; publishing is next."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "tasks": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("release-42", "Plan the release.")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                text = "".join(part["text"] for part in event.result["content"])
                print(f"{event.tool_name}: {text}")
        result = await run.result()
        print(result.final_text)


asyncio.run(main())
```

```text
TaskCreate: Task #1 created: Draft the release notes
TaskCreate: Task #2 created: Publish the release
TaskUpdate: Task #2 updated: [pending] Publish the release
TaskUpdate: Task #1 updated: [completed] Draft the release notes
TaskList: #1 [completed] Draft the release notes
#2 [pending] Publish the release
TaskGet: #2 [pending] Publish the release
Tag and push
Release notes are done; publishing is next.
```

Cấu hình không đặt `permissions`, nên chế độ là `default`, nơi các tool write sẽ hỏi.
Hai tool write vẫn chạy; xem [Không cần phê duyệt](#không-cần-phê-duyệt) bên dưới.

---

## Bốn tool

| Tool | Scope | Đầu vào | Mô hình đọc được gì |
|---|---|---|---|
| `TaskCreate` | write | `subject`, `description`; tùy chọn `active_form`, `metadata` | `Task #1 created: <subject>` |
| `TaskList` | read | không có | Một dòng `#<id> [<status>] <subject>` cho mỗi task, hoặc `No tasks found.` |
| `TaskGet` | read | `id` | Dòng đó, rồi đến mô tả; lỗi `Task not found.` |
| `TaskUpdate` | write | `id`; tùy chọn `subject`, `description`, `active_form`, `status`, `owner`, `add_blocks`, `add_blocked_by`, `remove_blocks`, `remove_blocked_by`, `metadata` | `Task #<id> updated: [<status>] <subject>`, hoặc `Task #<id> deleted.` |

- Id là chuỗi, `"1"`, `"2"`, ..., được đánh số theo thứ tự trong session.
- Trạng thái là `pending` (task mới), `in_progress` hoặc `completed`.
  `"status": "deleted"` xóa task thay vì đặt trạng thái. Mọi giá trị khác không qua
  được schema đầu vào, và mô hình đọc được lý do.
- `add_blocked_by` và ba trường cạnh còn lại ghi nhận task nào chờ task nào. Các tool
  lưu các cạnh này; cả `TaskList` lẫn `TaskGet` đều không in chúng ra.
- Tên, schema đầu vào và văn bản kết quả là của Python Linch.

Hai lệnh gọi write trong cùng một lượt chạy lần lượt; các lệnh gọi read có thể chạy
cùng lúc.

---

## Không cần phê duyệt

Bốn tool chạy mà không hỏi ở mọi chế độ quyền: khi mở agent, tên của chúng được thêm
vào `auto_allow` của chính sách. Chúng chỉ chạm vào các hàng task của chính session.
Một quy tắc từ chối hoặc hỏi cho một trong số chúng vẫn có hiệu lực, vì các quy tắc
được kiểm tra trước `auto_allow`.

Trên một [server từ xa](./server.md), quyền cấp đó là lý do một tenant cần
`skip_dangerous` trong `permission_modes` của nó để bật `tasks`.

---

## Task được lưu ở đâu

| Kho lưu trữ | Task |
|---|---|
| Bộ nhớ (mặc định) | Bên trong handle của agent đã mở; mất khi server thoát |
| Postgres | Các bảng `linch_tasks` và `linch_task_edges`; chúng tồn tại sau khi khởi động lại, và mọi server dùng cùng DSN và namespace đều thấy chúng |

Run con của một subagent chạy trong session riêng, nhưng các tool task của nó dùng
session của run gốc trong cây, nên một worker liệt kê và cập nhật task của coordinator.
Task của các session không liên quan vẫn tách biệt. Khi bật thêm `subagents`, lần chạy
này cho thấy điều đó (scripted provider đưa lượt thứ ba cho run con):

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, Server, SubagentEvent, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    tool_turn("c1", "TaskCreate", {"subject": "Parent task", "description": "x"}),
    tool_turn("c2", "Subagent", {"description": "List", "prompt": "List the tasks"}),
    tool_turn("k1", "TaskList", {}),  # the child's first turn
    text_turn("The child saw the parent's task."),  # the child's answer
    text_turn("done"),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    "tasks": True,
    "subagents": True,
    "config_dir": tempfile.mkdtemp(),  # no project subagents
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "go"):
            event = envelope.event
            if isinstance(event, SubagentEvent) and isinstance(event.event, ToolCallEndEvent):
                print("child:", event.event.result["content"][0]["text"])
            elif isinstance(event, ToolCallEndEvent):
                print("parent:", event.result["content"][0]["text"])


asyncio.run(main())
```

```text
parent: Task #1 created: Parent task
child: #1 [pending] Parent task
parent: The child saw the parent's task.
```

Điều tương tự áp dụng cho các worker của [deep agent](./deep-agent.md): chúng đọc và
cập nhật id task của coordinator.

---

## Các trang liên quan

- [Subagent](./subagents.md): các run con và session của chúng
- [Deep agent](./deep-agent.md): preset bật task
- [Kho lưu trữ](./stores.md): kho trong bộ nhớ và Postgres
- Rust: [các tool task của session](../architecture/tools.md#session-task-tools) và
  [kho task](../architecture/stores.md#task-store)
