# Cấu hình sẵn cho deep agent

[Bản gốc tiếng Anh](../../usage/deep-agent.md)

Dùng cấu hình sẵn deep agent khi bạn muốn một mô hình biết lập kế hoạch, giao việc cho subagent và ghi chú, mà không phải tự lắp ghép từng thành phần.

[← Hướng dẫn sử dụng](./README.md)

`deep_agent` trong cấu hình agent biến một agent thành deep agent: các tool task của
session, một danh sách subagent, các worker được giữ lại và chạy nền, hệ thống tệp ảo
cùng bộ nhớ tệp, và các chỉ dẫn vận hành của Python Linch. Nó là một agent thông
thường với các thiết lập đó; vòng lặp không thay đổi. Một cấu hình trơn giữ tất cả
những thứ này ở trạng thái tắt.

Deep agent do mô hình dẫn dắt: mô hình quyết định cần khởi động subagent nào, khi nào
chạy chúng ở chế độ nền và khi nào tiếp tục một subagent. Việc điều phối không có tính
xác định, và không có gì ghi nhật ký nó ngoài các bản ghi run và session thông thường.

---

## Những gì nó bật

Mở một cấu hình có `deep_agent` sẽ bật các thành phần sau, thêm vào những gì cấu hình
đã đặt:

| Thành phần | Bạn nhận được |
|---|---|
| [`tasks`](./tasks.md) | `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, chạy mà không hỏi |
| [Skill](./skills.md) | Tool `Skill` với `verify` tích hợp sẵn; các skill trong thư mục cấu hình chỉ khi `skills` bật |
| [Subagent](./subagents.md) | Tool `Subagent` với danh sách bên dưới; cả các subagent trong thư mục cấu hình khi `subagents` bật |
| Worker | Của profile: chạy nền và được giữ lại, nên có `SubagentContinue` và `TaskStop`; giá trị `workers` trong cấu hình được ưu tiên |
| Hệ thống tệp ảo | `ls`, `read_file`, `write_file`, `edit_file`, không chuyển nội dung ra ngoài trừ khi cấu hình đặt `filesystem` |
| Bộ nhớ tệp | `/memories/MEMORY.md` được hiển thị cho mô hình trong mọi request (xem [Bộ nhớ tệp](#bộ-nhớ-tệp)) |
| Chỉ dẫn | Của preset, đặt trước `spec.system_prompt`; `max_turns` của profile trừ khi spec đã đặt giá trị |

Nó không thêm workspace. Không có workspace thì agent không có `Read`, `Write`, `Edit`,
`Grep`, `Glob` hay `Bash`, và các định nghĩa trong danh sách nêu tên những tool mà
agent không có (agent con không nhận được chúng).

---

## Deep agent bền vững

Các profile tích hợp sẵn đều bền vững: agent phải có kho lưu trữ Postgres, nếu không
`open_agent` raise `OpenError` (`a durable deep agent needs durable stores`). Đoạn mã
này đọc DSN từ `LINCH_POSTGRES_DSN`; bản thân client không đọc biến môi trường nào.
Sau đó, một tiến trình server thứ hai đọc ghi chú mà tiến trình đầu đã giữ lại.

```python
import asyncio
import os

from linch_rust import AgentConfig, InitEvent, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

dsn = os.environ["LINCH_POSTGRES_DSN"]
memory_index = "- /memories/stack.md: the service runs on Postgres 17\n"


def config(turns: list) -> AgentConfig:
    return {
        "namespace": "research",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": dsn},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
        "deep_agent": {"profile": "balanced"},
    }


async def show(run) -> None:
    async for envelope in run:
        event = envelope.event
        if isinstance(event, InitEvent):
            print("tools:", sorted(event.tools))
        elif isinstance(event, ToolCallEndEvent):
            print(f"{event.tool_name}: {event.result['content'][0]['text'].strip()}")


async def main() -> None:
    # Monday: plan, and keep a note in file memory.
    async with Server() as server:
        agent = await server.open_agent(config([
            tool_turn("c1", "TaskCreate", {"subject": "Survey the stack", "description": "List services"}),
            tool_turn("c2", "write_file", {"path": "/memories/MEMORY.md", "content": memory_index}),
            text_turn("Noted the stack."),
        ]))
        await show(await agent.run("monday", "Survey the stack and remember it."))
    # Tuesday: a new server process and a new session read the same memory.
    async with Server() as server:
        agent = await server.open_agent(config([
            tool_turn("c1", "read_file", {"path": "/memories/MEMORY.md"}),
            text_turn("The service runs on Postgres 17."),
        ]))
        await show(await agent.run("tuesday", "What database do we run?"))


asyncio.run(main())
```

```text
tools: ['Skill', 'Subagent', 'SubagentContinue', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskStop', 'TaskUpdate', 'edit_file', 'ls', 'read_file', 'write_file']
TaskCreate: Task #1 created: Survey the stack
write_file: Written 1 line to '/memories/MEMORY.md'.
tools: ['Skill', 'Subagent', 'SubagentContinue', 'TaskCreate', 'TaskGet', 'TaskList', 'TaskStop', 'TaskUpdate', 'edit_file', 'ls', 'read_file', 'write_file']
read_file: - /memories/stack.md: the service runs on Postgres 17
```

Với bản demo không có Postgres, đặt `"deep_agent": {"durable": False}`: khi đó kho lưu
trữ trong bộ nhớ hoạt động, và mọi thứ mất khi server thoát.

### Quyền

Preset không đặt chính sách quyền nào. Ở chế độ `default`, `write_file` và `edit_file`
sẽ hỏi; `accept_edits`, như ở trên, cho phép chúng ghi. `Subagent`, `SubagentContinue`
và `TaskStop` có scope exec và hỏi ở cả hai chế độ, nên một deep agent cần giao việc
mà không dừng chờ phê duyệt thì cần các quy tắc cho phép chúng, hoặc `skip_dangerous`.

### Với một mô hình thật

Cách này cần `DEEPSEEK_API_KEY` trong môi trường và tạo các lệnh gọi tính phí; nó chưa
được chạy cho trang này. Nó làm việc trên repository ở thư mục hiện tại, không có
`Bash`, và cho phép ba tool điều phối.

```python
import asyncio
import os

from linch_rust import AgentConfig, Server

orchestration = ["Subagent", "SubagentContinue", "TaskStop"]
config: AgentConfig = {
    "namespace": "research",
    "spec": {"model": "deepseek-flash", "system_prompt": "You maintain the billing service."},
    "provider": {"id": "deepseek"},
    "stores": {"type": "postgres", "dsn": os.environ["LINCH_POSTGRES_DSN"]},
    "workspace": ".",
    "bash": False,
    "permissions": {
        "mode": "accept_edits",
        "project_root": os.getcwd(),
        "layers": [{"name": "host", "rules": [
            {"type": "tool", "tool": name, "decision": "allow"} for name in orchestration
        ]}],
    },
    "deep_agent": {"profile": "coordinator"},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("billing-audit", "Find where invoices are rounded and plan a fix.")
        result = await run.result()
        print(getattr(result, "final_text", result))


asyncio.run(main())
```

`run.result()` trả về `SuspendedEvent` khi một lệnh gọi vẫn cần phê duyệt; xem
[Run](./runs.md).

---

## Profile

`profile` nêu tên một profile tích hợp sẵn hoặc viết ra một profile đầy đủ. `durable`
và `coordinator` ghi đè các giá trị của profile.

| Profile | `durable` | `coordinator` | `max_turns` | `max_tokens` | Worker |
|---|---|---|---|---|---|
| `balanced` (mặc định) | có | không | 64 | 1,000,000 | được giữ lại và chạy nền |
| `coordinator` | có | có | 64 | 1,000,000 | được giữ lại và chạy nền |
| `unbounded` | có | không | không có | không có | được giữ lại và chạy nền |

Chỉ chọn `unbounded` khi ứng dụng chủ của bạn tự áp đặt giới hạn chi phí và thời gian.
`max_tokens` được giữ lại để các profile giống như trong Python Linch, nhưng hiện chưa
có gì thực thi nó. Một tên khác làm `open_agent` thất bại với `unknown deep-agent
profile`.

Một profile viết ra đầy đủ là `DeepAgentProfile`; mọi khóa đều bắt buộc:

```python
from linch_rust import DeepAgent, DeepAgentProfile

review: DeepAgentProfile = {
    "name": "review",
    "durable": False,
    "coordinator": False,
    "max_turns": 24,
    "max_tokens": None,
    "retain_subagents": True,
    "enable_background_subagents": False,
}
preset: DeepAgent = {"profile": review}
```

Một coordinator nhận các chỉ dẫn coordinator: lập kế hoạch, giao việc, tổng hợp, và
để việc nặng cho worker. Khác với Python Linch, nó vẫn giữ các tool workspace; chỉ có
chỉ dẫn của nó bảo không dùng chúng.

---

## Danh sách subagent

| Subagent | Tool có thể dùng | Vai trò |
|---|---|---|
| `researcher` | `Read`, `Glob`, `Grep`, `TaskList`, `TaskGet`, `ls`, `read_file` | Khám phá chỉ đọc và thu thập bằng chứng |
| `planner` | `Read`, `Glob`, `Grep`, `TaskList`, `TaskGet`, `ls`, `read_file` | Một kế hoạch có thứ tự kèm đường dẫn tệp |
| `implementer` | `Read`, `Edit`, `Write`, `Glob`, `Grep`, `Bash`, `TaskList`, `TaskGet`, `TaskUpdate`, `ls`, `read_file`, `write_file`, `edit_file` | Các thay đổi có phạm vi rõ ràng |
| `verification` | `Read`, `Glob`, `Grep`, `TaskList`, `TaskGet`, `ls`, `read_file` | Một bước kiểm tra đối kháng chỉ đọc, kết thúc bằng verdict |

Mô tả và chỉ dẫn là của Python Linch. `verification` này thay thế bản tích hợp sẵn của
lõi, vốn có `Bash`. Với `subagents: True`, một tệp cùng tên trong
`<config_dir>/agents/` sẽ thay thế một mục trong danh sách.

Mỗi worker chạy trong session riêng, nhưng các tool task của nó dùng danh sách task của
coordinator (xem [Task của session](./tasks.md#task-được-lưu-ở-đâu)). Các
lệnh gọi `researcher`, `planner` và `verification` có thể chạy song song với nhau khi
mọi tool chúng liệt kê đều đã đăng ký và có scope read, điều này cần có workspace.

---

## Bộ nhớ tệp

Trước mỗi lệnh gọi mô hình, agent thêm hai khối văn bản vào cuối request: một chỉ dẫn
ngắn (bộ nhớ nằm dưới `/memories/`, mỗi sự kiện một tệp, với `MEMORY.md` là chỉ mục có
một dòng cho mỗi tệp), và 200 dòng đầu của `/memories/MEMORY.md` khi tệp tồn tại và
không rỗng. Chỉ mục được đọc lại cho mỗi request, nên thay đổi mà mô hình thực hiện
sẽ hiện ra ở lượt kế tiếp.

Các khối này không được ghi vào nhật ký và không xuất hiện trong event stream; mô hình
duy trì các tệp bằng các tool của hệ thống tệp ảo.

Nơi các tệp nằm phụ thuộc vào kho lưu trữ:

| Kho lưu trữ | Tệp |
|---|---|
| Bộ nhớ | Theo từng session. Worker có session riêng, nên không thấy tệp của agent cha |
| Postgres | Dùng chung cho mọi session của namespace, kể cả worker, và được giữ qua các lần khởi động lại |

---

## Điểm khác so với Python Linch

| Python Linch | linch-rust |
|---|---|
| `create_deep_agent(...)` | `deep_agent` trong cấu hình |
| Coordinator mất `Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Read` | Nó vẫn giữ chúng; chỉ chỉ dẫn khác |
| `max_tokens` giới hạn cả cây subagent | Không được thực thi |
| `durable=True` cần kho lưu trữ và hệ thống tệp tường minh | Cần kho Postgres; hệ thống tệp được bật cho bạn |
| `cwd="."` cung cấp workspace | Không có workspace trừ khi cấu hình đặt |
| Skill và subagent của dự án được nạp theo mặc định | Chỉ khi `skills` hoặc `subagents` bật |
| `features=` có thể tắt từng phần | Mọi phần đều bật |

Trên một [server từ xa](./server.md), tenant cần `skip_dangerous` trong
`permission_modes` của nó để mở một deep agent, vì preset bật các tool task, vốn chạy
mà không hỏi.

---

## Các trang liên quan

- [Subagent](./subagents.md): worker chạy nền, `SubagentContinue`, `TaskStop`
- [Task của session](./tasks.md)
- [Profile theo miền nghiệp vụ](./profiles.md): một vai trò được thêm sau chỉ dẫn của
  preset
- [Kho lưu trữ](./stores.md): thiết lập Postgres
