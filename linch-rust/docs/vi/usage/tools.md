# Tool

[Bản gốc tiếng Anh](../../usage/tools.md)

Dùng trang này để cung cấp tool cho agent: các hàm Python trong process của bạn,
các workspace tool tích hợp sẵn, hệ thống tệp ảo và MCP server, đồng thời quyết
định lời gọi nào chạy, hỏi hay bị từ chối.

[<- Hướng dẫn sử dụng](./README.md)

| Tool | Chạy ở đâu | Được bật bởi |
|---|---|---|
| Host tool | Trong process Python của bạn, được server gọi ngược | `open_agent(config, tools=[...])` |
| Workspace tool: `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash`, `BashOutput`, `BashKill` | Trong server, trên một thư mục | `workspace` (và `bash`) |
| `AskUser` | Tạm dừng run cho đến khi bạn [trả lời](#hỏi-người-dùng) | `ask_user` |
| Hệ thống tệp ảo: `ls`, `read_file`, `write_file`, `edit_file` | Trong server, trên các tệp văn bản lưu trong kho | `filesystem` |
| MCP tool: `mcp__<server>__<tool>` | Trong MCP server | `mcp_servers` |
| Task của session: `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate` | Trong server | `tasks` ([Task](./tasks.md)) |
| Lịch: `CreateSchedule`, `ListSchedules`, `CancelSchedule` | Trong server; một worker chạy các lần kích hoạt | `schedule_tools` ([bên dưới](#tool-lập-lịch)) |
| `Skill`, `Subagent`, `SubagentContinue`, `TaskStop`, `send_message` | Trong server | `skills`, `subagents`, `workers`, `mailbox` ([Skill](./skills.md), [Subagent](./subagents.md)) |
| `tool_search` | Trong server | `tool_search` ([Config của agent](./agent.md#trường-cấp-cao-nhất)) |

Event `init` của mỗi run liệt kê tên các tool được cấp cho model, theo thứ tự:
host tool trước, rồi các tool còn lại.

---

## Host tool

Host tool là một hàm được gắn decorator `@tool`. Server xác thực input
của model theo schema của tool, rồi gọi hàm trong process của bạn với input đó
dưới dạng keyword argument.

```python
from typing import Any

from linch_rust import tool


@tool(scope="read")
async def search_orders(customer: str, limit: int = 10, status: str | None = None) -> list[str]:
    """Search a customer's orders, newest first."""
    return []


@tool(
    scope="exec",
    name="run_report",
    description="Run a named report.",
    input_schema={
        "type": "object",
        "properties": {"report": {"type": "string", "enum": ["daily", "weekly"]}},
        "required": ["report"],
        "additionalProperties": False,
    },
    timeout=30,
)
async def report(**input: Any) -> dict[str, Any]:
    return {"report": input["report"], "rows": 0}


print(search_orders.spec())
print(report.spec())
```

```text
{'name': 'search_orders', 'description': "Search a customer's orders, newest first.", 'input_schema': {'type': 'object', 'properties': {'customer': {'type': 'string'}, 'limit': {'type': 'integer'}, 'status': {'type': 'string'}}, 'additionalProperties': False, 'required': ['customer']}, 'scope': 'read'}
{'name': 'run_report', 'description': 'Run a named report.', 'input_schema': {'type': 'object', 'properties': {'report': {'type': 'string', 'enum': ['daily', 'weekly']}}, 'required': ['report'], 'additionalProperties': False}, 'scope': 'exec', 'timeout_ms': 30000}
```

Một tool hỏi về một thứ đang chạy (trạng thái một job) khai báo `polls=True`: [loop
guard](./agent.md#loop_guard) khi đó chờ các câu trả lời lặp lại của nó thay vì kết
thúc run ở câu giống nhau thứ ba. Trả về `ToolOutput(value, metadata={"poll_done":
True})` khi job đã xong, để đọc lại nó là một lần lặp.

`@tool` trả về một `HostTool`; `spec()` là thứ server nhận được và `fn` là hàm của
bạn, nên một unit test có thể `await search_orders.fn(customer="ada")`.

| Đối số | Mặc định | Điều khiển gì |
|---|---|---|
| `scope` | bắt buộc, không có mặc định | `read`, `write` hoặc `exec`. Nó quyết định việc phê duyệt và chạy song song ([Scope](#scope)) |
| `name` | tên của hàm | Tên tool: 1 đến 64 chữ cái ASCII, chữ số, `_` hoặc `-`, duy nhất trong agent |
| `description` | docstring | Điều được nói với model |
| `input_schema` | dựng từ type hint | Một JSON Schema object cho input |
| `timeout` | `300` giây | Server chờ câu trả lời trong bao lâu |

`@tool` raise `ValueError` với scope không xác định hoặc timeout không dương, và
`TypeError` với hàm không phải `async` hoặc tham số mà nó không thể gán kiểu.

### Schema suy ra từ type hint

| Hint | Kiểu JSON |
|---|---|
| `str` | `string` |
| `int` | `integer` |
| `float` | `number` |
| `bool` | `boolean` |
| `list`, `list[...]` | `array` (kiểu phần tử không được giữ lại) |
| `dict`, `dict[...]` | `object` |
| `X \| None`, `Optional[X]` | `X`, không bắt buộc |

Tham số không có giá trị mặc định và không phải optional là bắt buộc, và schema
đặt `additionalProperties: false`. Mọi hint khác, tham số không có hint, hoặc
`*args` đều raise `TypeError`; hãy truyền `input_schema` thay thế, với một hàm
nhận `**input` như ở trên. Server biên dịch mọi schema lúc mở và từ chối schema
không hợp lệ, tên trùng hoặc tên sai bằng một `ProtocolError` (code -32602).

### Những gì model đọc

- Giá trị trả về có thể là bất kỳ giá trị JSON nào. Một chuỗi đến với model
  nguyên trạng; mọi thứ khác đến dưới dạng JSON gọn (`{"q":"x"}`).
- Một exception là lỗi tool mà model đọc được, dưới dạng
  `"<ExceptionType>: <message>"`, để nó sửa input hoặc giải thích. Run vẫn tiếp
  tục.
- Khi hết timeout, server ngừng chờ, model nhận lỗi timeout, và task của hàm bị
  hủy (`asyncio.CancelledError`).
- Hủy run (`run.cancel()`) sẽ hủy task của lời gọi đang chạy.

### Metadata cho host

Trả về `ToolOutput(value, metadata=...)` để đính kèm các chi tiết mà model không
bao giờ thấy: model đọc `value`, còn `metadata` (một dict an toàn với JSON) đến
trong `ToolCallEndEvent.metadata` của lời gọi.

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent, ToolOutput, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def lookup(email: str) -> ToolOutput:
    """Find a customer by email."""
    return ToolOutput({"name": "Ada"}, metadata={"source": "crm", "cache": "hit"})


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [
        tool_turn("c1", "lookup", {"email": "ada@example.com"}),
        text_turn("Found Ada."),
    ]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup])
        async for envelope in await agent.run("s1", "Who is ada@example.com?"):
            if isinstance(envelope.event, ToolCallEndEvent):
                print("model reads:", envelope.event.result["content"][0]["text"])
                print("host reads:", envelope.event.metadata)


asyncio.run(main())
```

```text
model reads: {"name":"Ada"}
host reads: {'source': 'crm', 'cache': 'hit'}
```

Lỗi tool không có `metadata` riêng, chỉ có `details`, và một object `details` tới event
như `metadata` (`status` và `exit_code` của một lệnh `Bash` thất bại nằm ở đó). Một callback `post_tool_use` thay thế kết quả sẽ
khiến event mang metadata của kết quả thay thế; một callback
trả lời `mutate` giữ nguyên metadata và mã lỗi của kết quả và chỉ đổi
giá trị hoặc cờ lỗi của nó.

### Tool đang phục vụ lời gọi nào

Một host tool khai báo tham số tên `ctx`, hoặc được chú thích `ToolContext`, sẽ
nhận `ToolContext` của lời gọi ở đó. Tham số đó không bao giờ nằm trong input
schema, và tool không khai báo nó chỉ được gọi với input của nó. Các tên này là
của Python Linch.

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolContext, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def refund(order: str, ctx: ToolContext) -> str:
    """Refund an order."""
    print("run:", ctx.run_id, "session:", ctx.session_id, "call:", ctx.tool_use_id)
    print("acting for:", ctx.principal)
    print("key:", ctx.idempotency_key)
    return f"refunded {order}"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [
        tool_turn("c1", "refund", {"order": "A-17"}),
        text_turn("Refunded."),
    ]},
    "permissions": {"mode": "skip_dangerous", "project_root": "/"},
}


async def main() -> None:
    print(refund.spec()["input_schema"]["properties"])
    async with Server() as server:
        agent = await server.open_agent(config, tools=[refund])
        principal = {"tenant": "acme", "user": "ada"}
        run = await agent.run("s1", "Refund A-17", run_id="r1", principal=principal)
        print((await run.result()).final_text)


asyncio.run(main())
```

```text
{'order': {'type': 'string'}}
run: r1 session: s1 call: c1
acting for: {'tenant': 'acme', 'user': 'ada', 'roles': []}
key: r1:c1
Refunded.
```

| Trường | Ý nghĩa |
|---|---|
| `session_id` | Session sở hữu lời gọi |
| `run_id` | Run đang thực thi lời gọi |
| `tool_use_id` | Id của provider cho lần dùng tool này; một run được resume mà gọi lại nó sẽ dùng cùng id |
| `principal` | Run đang hành động thay cho ai (`tenant`, `user`, `roles`), hoặc `None` |
| `idempotency_key` | `"<run_id>:<tool_use_id>"`, để khử trùng lặp một side effect đã chạy trước khi crash |

<a id="scopes"></a>

## Scope

| Scope | Mode `default` | Mode `accept_edits` | Mode `skip_dangerous` | Chạy song song |
|---|---|---|---|---|
| `read` | chạy | chạy | chạy | có, cùng các lời gọi read lân cận, tối đa `spec.max_tool_concurrency` (4) |
| `write` | hỏi | chạy trong project root, hỏi ở ngoài | chạy | không |
| `exec` | hỏi | hỏi | chạy | không |

Scope của một tool là một phần của [hợp đồng của run](./agent.md#hợp-đồng-của-run).
Chỉ cấp `read` cho tool không thay đổi gì: nó chạy mà không hỏi và chạy cạnh các
lời gọi khác.

## Workspace tool

`workspace` nêu tên một thư mục đã tồn tại. Khi đó agent có các tool này trên nó
(`bash: False` loại `Bash`, `BashOutput` và `BashKill` ra):

| Tool | Scope | Làm gì |
|---|---|---|
| `Read` | read | Văn bản UTF-8 với các dòng được đánh số; `offset` và `limit`, mặc định 2000 dòng; tệp trên 10 MiB bị từ chối |
| `Write` | write | Thay thế cả tệp theo kiểu nguyên tử, tạo các thư mục cha |
| `Edit` | write | Thay thế đoạn văn bản khớp chính xác, một lần trừ khi có `replace_all` |
| `Grep` | read | Engine của ripgrep; các mode `files_with_matches`, `count`, `content` |
| `Glob` | read | Quy tắc glob của ripgrep, mới nhất trước, tối đa 1000 đường dẫn |
| `Bash` | exec | `/bin/sh -c` tại gốc của workspace; với `run_in_background` nó trả về ngay ([bên dưới](#lệnh-chạy-nền)) |
| `BashOutput` | read | Những gì một lệnh chạy nền đã in kể từ lần đọc trước, và nó đã kết thúc chưa |
| `BashKill` | exec | Kill process group của một lệnh chạy nền |

```python
import asyncio
import tempfile
from pathlib import Path

from linch_rust import AgentConfig, InitEvent, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

root = Path(tempfile.mkdtemp())
(root / "notes.txt").write_text("status: draft\n")

config: AgentConfig = {
    "namespace": "workspace-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "Edit", {"file_path": "notes.txt", "old_string": "draft", "new_string": "final"}),
            tool_turn("c2", "Read", {"file_path": "notes.txt"}),
            tool_turn("c3", "Edit", {"file_path": "notes.txt", "old_string": "draft", "new_string": "final"}),
            text_turn("Updated."),
        ],
    },
    "workspace": str(root),
    "bash": False,
    "permissions": {"mode": "accept_edits", "project_root": str(root)},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Mark the notes final"):
            event = envelope.event
            if isinstance(event, InitEvent):
                print("tools:", event.tools)
            elif isinstance(event, ToolCallEndEvent):
                text = event.result["content"][0]["text"]
                print(event.tool_name, "error" if event.is_error else "ok", "|", text.splitlines()[0])
    print((root / "notes.txt").read_text(), end="")


asyncio.run(main())
```

```text
tools: ['Read', 'Write', 'Edit', 'Grep', 'Glob']
Edit error | Read notes.txt before changing it.
Read ok | 1	status: draft
Edit ok | Edited notes.txt
status: final
```

- **Đọc trước khi ghi.** `Write` lên một tệp đã tồn tại và `Edit` cần một lần
  `Read` tệp đó trong cùng session, tại đúng thời điểm sửa đổi và kích thước hiện
  tại của nó; bất kỳ thay đổi nào của ai đó đều làm mất hiệu lực. Bản ghi nằm
  trong bộ nhớ của server: sau khi khởi động lại, hoặc sau
  `agent.close_session(session_id)`, model phải đọc lại trước khi ghi.
- **Tệp đã thay đổi.** Nếu tệp thay đổi sau lần model đọc, `Write` và `Edit` trả lời `Read this file again before writing it.` hoặc `... before editing it.`; `Read` thấy tệp đổi giữa chừng trả lời `File changed while reading; Read this file again.`
- **`old_string` rỗng.** `Edit` chấp nhận nó, như `Edit` của Python. Nó khớp ở
  mọi vị trí, nên với tệp rỗng thì văn bản mới được chèn vào, với tệp không rỗng
  thì bị từ chối vì mơ hồ trừ khi đặt `replace_all` (chèn ở mọi vị trí và cần
  đã đọc toàn bộ tệp), và dòng đầu tiên phải đã được đọc.
- **Giới hạn phạm vi.** Đường dẫn là tương đối so với workspace. `..`, đường dẫn
  tuyệt đối nằm ngoài nó và symlink dẫn ra ngoài đều bị từ chối.
- **`Bash` mặc định không bị giới hạn phạm vi.** Với backend `local` mặc định, nó
  bắt đầu trong workspace nhưng có thể chạm tới mọi thứ mà user của server chạm
  được; `execution` chạy nó trong một sandbox hoặc container thay vào đó
  ([Sandbox](./sandbox.md)). Nó hỏi ở các mode `default` và
  `accept_edits`; chỉ chín biến trong môi trường của server (`PATH`, `HOME`,
  `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`) đến được lệnh,
  nên key của provider thì không. Timeout của nó mặc định là 120 giây.
- **Trạng thái trong event kết thúc.** `Bash`, `BashOutput` và `BashKill` đặt
  trạng thái của lệnh vào `metadata` của event `tool_call_end`, cả khi thất bại
  lẫn khi thành công, nên client hoặc hook đọc được mà không phải phân tích văn
  bản (văn bản model nhìn thấy không đổi): `status` là `running`, `exited`,
  `timed_out`, `killed`, `output_limit` hoặc `interrupted`; `exit_code` (hoặc
  `signal`) có mặt khi lệnh tự thoát; `truncated` cho biết đầu ra có bị cắt
  không; `process_id` của lệnh chạy nền cũng có mặt. Một lời gọi thất bại vẫn
  giữ `error_code` của nó (`exit_status`, `timeout`, `output_limit`, ...).

Các giới hạn, việc kill cả process group và mức trần của đầu ra nằm ở
[trang tool của kiến trúc](../architecture/tools.md#workspace-tools).

### Lệnh chạy nền

`Bash` với `"run_in_background": true` khởi động lệnh và trả về ngay với
`process_id` của nó (cũng nằm trong `metadata` của lời gọi, cùng với `pid`):

```text
[started background process bash_add697562a64; read its output with BashOutput, stop it with BashKill; it is killed after 1800000 ms, when the session closes, or when the server stops]
```

Sau đó model gọi `BashOutput` với `process_id` đó, và nhận những gì lệnh đã in
kể từ lần đọc trước. Với `wait_ms` (tối đa 600000), nó chờ chừng đó thời gian cho
lệnh kết thúc trước. Một lệnh còn đang chạy được đọc như một kết quả thành công
kết thúc bằng `[bash_... still running; ...]`; một lệnh đã kết thúc được đọc như
kết quả của `Bash` chạy ở foreground (`[exit code 0]`, hoặc một lỗi nếu thoát với
mã khác 0 hoặc hết timeout). `BashKill` kill process group của nó và trả về phần
đầu ra còn lại.

- **Vòng đời.** Một lệnh chạy nền thuộc về session của nó trong agent handle này
  và sống lâu hơn run đã khởi động nó. Nó bị kill sau `timeout_ms` (mặc định 30
  phút, tối đa 30 phút), bởi `BashKill`, bởi `agent.close_session(session_id)`,
  bởi `agent.close()`, và khi server dừng, kể cả bằng SIGKILL: một watchdog
  trong process group của nó kill cả group khi đầu pipe phía server đóng lại.
- **Không sống sót qua restart.** Sau khi server khởi động lại, hoặc từ một agent
  handle khác, id của nó không chỉ tới gì: `BashOutput` và `BashKill` thất bại
  với `no_such_process` và nói điều đó cho model. Một run được resume mà đã khởi
  động một lệnh thì phải khởi động lại nó.
- **Tối đa 4 lệnh cùng lúc** cho mỗi session; lệnh thứ năm thất bại với
  `too_many_processes`. Mỗi lần đọc trả về tối đa 4000 byte đầu và 26000 byte
  cuối của đầu ra chưa đọc của mỗi stream.
- **Permission.** `BashOutput` có scope read, nên chạy ở mọi mode và chạy song
  song. `BashKill` có scope exec như `Bash`, nên nó hỏi ở `default` và
  `accept_edits`; hãy cho phép nó bằng `auto_allow: ["BashKill"]` hoặc một rule
  `tool`. Linch Python thay vào đó có một tool `BashProcess` scope exec (`wait`,
  `status`, `kill`), nên ở đó ngay cả việc đọc output cũng phải hỏi.

<a id="permissions"></a>

## Permission

Mỗi lời gọi được quyết định trước khi chạy: cho phép, hỏi hoặc từ chối. Lời gọi bị
từ chối không bao giờ chạy; model nhận một lỗi nêu tên rule. Lời gọi cần hỏi sẽ
tạm dừng run cho tới khi bạn [phê duyệt](#phê-duyệt) nó.

| Mode | Khi không có rule nào khớp |
|---|---|
| `default` | `read` chạy; `write` và `exec` hỏi |
| `accept_edits` | `read` chạy; `write` chạy khi đường dẫn của nó nằm trong project root, nếu không thì hỏi; `exec` hỏi |
| `skip_dangerous` | Mọi lời gọi đều chạy |

Khi không có `permissions`, mode là `default` và project root là workspace (hoặc
`/`). Khi có `permissions`, `project_root` là bắt buộc và phải là đường dẫn tuyệt
đối. Cách viết là `accept_edits` và `skip_dangerous`, không phải `acceptEdits` và
`skip-dangerous` của Python Linch; cách viết khác sẽ là một `ProtocolError` lúc
mở.

<a id="permission-rules"></a>

### Permission rule

Rule nằm trong các layer có tên, từ ít cụ thể nhất đến cụ thể nhất:

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, Server, SuspendedEvent, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn

root = tempfile.mkdtemp()


@tool(scope="write")
async def save(text: str) -> str:
    """Save a note."""
    return "saved"


config: AgentConfig = {
    "namespace": "rules-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "save", {"text": "x"}),
            tool_turn("c2", "Bash", {"command": "rm -rf build"}),
            tool_turn("c3", "Write", {"file_path": "secrets/key.txt", "content": "x"}),
            tool_turn("c4", "Write", {"file_path": "notes.txt", "content": "x"}),
            text_turn("Done."),
        ],
    },
    "workspace": root,
    "permissions": {
        "mode": "default",
        "project_root": root,
        "layers": [
            {
                "name": "app",
                "rules": [
                    {"type": "tool", "tool": "save", "decision": "allow"},
                    {"type": "bash", "patterns": ["rm *"], "decision": "deny"},
                    {"type": "path", "paths": ["secrets/**"], "decision": "deny"},
                ],
            }
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save])
        async for envelope in await agent.run("s1", "go"):
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name, "|", event.result["content"][0]["text"])
            elif isinstance(event, SuspendedEvent):
                print("asks:", event.pending_request_ids)


asyncio.run(main())
```

```text
save | saved
Bash | Tool call denied: Bash denied by bash rule in layer "app"
Write | Tool call denied: Write denied by path rule in layer "app"
asks: ['c4']
```

| `type` của rule | Trường | Khớp với |
|---|---|---|
| `tool` | `tool`, `decision`, `skill?` | Tên tool, `*` cho mọi tool, hoặc `mcp__<server>__*`; `skill` thu hẹp một rule `Skill` về một skill |
| `path` | `paths`, `decision`, `tools?` | Glob tương đối so với project root (tuyệt đối khi bắt đầu bằng `/`); `**` trải qua nhiều thư mục, `!` ở đầu thì đảo ngược. Khi không có `tools`, là mọi tool scope write có trường đường dẫn |
| `bash` | `patterns`, `decision` | Từng đoạn lệnh, dưới dạng glob hoặc theo các từ đầu của nó (`npm install` khớp `npm install lodash`) |

`decision` là `allow`, `deny` hoặc `ask`. Mọi rule còn nhận `role` và
`unless_role`, được so khớp với các role của principal của run: hãy truyền
`principal={"tenant": ..., "user": ..., "roles": [...]}` cho `agent.run`, `resume`
hoặc `approve` ([Run](./runs.md#hành-động-thay-cho-một-principal)). Khi không có
principal, rule có `role` không bao giờ khớp và rule có `unless_role` luôn được
áp dụng.

Cách các rule kết hợp:

- Trong một layer, rule khớp đầu tiên thắng (các bash rule được gộp lại: bất kỳ
  đoạn nào bị deny thì deny, bất kỳ đoạn nào hỏi thì hỏi).
- Giữa các layer, một deny từ bất kỳ layer nào là quyết định cuối; nếu không, layer
  cuối cùng đã quyết định sẽ thắng. Khi không layer nào quyết định, mode quyết
  định.
- Một lệnh shell mà bộ tách lệnh không mô hình hóa được có thể bị rule deny
  hoặc ask nhưng không bao giờ được rule allow. Gồm `$(...)` và dấu backtick, dấu
  `&` chạy nền hoặc `&>`, `>&file`, chú thích `#`, here-document, `>|`, `${...}`
  trong dấu nháy kép mà phần thân có dấu nháy, dấu gạch chéo ngược, `${` lồng hay `$[`, `env -S`,
  và vị trí lệnh là phép khai triển `$`, khai triển ngoặc nhọn (`{rm,-rf}`),
  `function`, `coproc`, gán phần tử mảng (`a[0]=1`) hay chuyển hướng `{fd}>file`.
- Một rule allow hẹp (pattern không phải `*` trần) không bao phủ lệnh chuyển
  hướng đầu ra vào một tệp (`echo x > file`, `>>`, `2>file`, `<>`): lệnh đó sẽ
  hỏi. Vì vậy rule allow cho `echo` hay `git *` không cho `echo x > file` hay
  `git log > file` đi qua, còn rule allow-all `*` cho phép `pytest -q > test.log`
  như mọi lệnh khác. `>&` theo sau đúng bởi chữ số hoặc `-` (`2>&1`, `>&2`, `>&-`) nhân bản descriptor
  và không phải tệp, `/dev/null`,
  `/dev/stdout`, `/dev/stderr` không phải tệp. Rule khớp đầu tiên của một đoạn
  quyết định đoạn đó có phải allow-all hay không. Cú pháp mà bộ tách lệnh không
  mô hình hóa được (`&>`, `>|`) không bao giờ được cho phép, kể cả bởi `*`. Rule
  deny và ask không đổi.
- Các bash rule dựa trên tên lệnh chỉ là nỗ lực tối đa và sandbox mới là ranh giới.
  Một rule deny hoặc ask khớp từng đoạn như đã viết, rồi khớp lại sau khi từ lệnh
  từng từ được bỏ dấu nháy (`\rm`, `r""m`, `git 'push'`; đối số trong dấu nháy vẫn là
  một từ nên `* --force` không khớp bên trong thông điệp commit) và các từ đứng trước
  lệnh bị gỡ: phép gán
  `NAME=value`, chuyển hướng (`>f`, `2>&1`), các từ khóa dành riêng `if`, `then`,
  `else`, `elif`, `do`, `while`, `until`, `!` và `{`, cùng `time`, `exec`,
  `command`, `builtin` và `env` (có hoặc không có dấu nháy) với các tùy chọn của
  chúng, đọc sau khi bỏ dấu nháy: tùy chọn ngắn gộp (`env -iu NAME`, `exec -la NAME`),
  tiền tố duy nhất của tùy chọn dài (`env --uns NAME`) và đối số của
  `env -u`/`-C`/`-a`, `time -f`/`-o` và `exec -a`.
  `command -v NAME` không phải lệnh tên `NAME`. Rule allow không bao giờ khớp dạng
  đã gỡ: `X=1 echo` không phải `echo`. Các cách viết và wrapper khác, như
  `/bin/rm`, `sudo rm`, `nice`, `nohup`, `setsid`, `timeout`, `xargs`, `eval rm x`
  và `sh -c 'rm x'`, không được bao phủ: chúng là những lệnh khác đối với rule.
- Đường dẫn nằm ngoài project root không bao giờ khớp một rule allow.

`auto_allow` (danh sách tên tool) cho phép các tool đó ở mọi mode trừ khi một rule
nói khác. `tasks` và `tool_deny` của một [profile](./profiles.md) tự thêm vào
policy cho bạn.

## Phê duyệt

Lời gọi cần hỏi sẽ kết thúc stream của run bằng một event `permission_request`
(các lời gọi, mỗi lời gọi có một `summary` một dòng) rồi một event `suspended`
(các `tool_use_id` của chúng). Không có gì chờ trong bộ nhớ: hãy trả lời khi bạn
sẵn sàng bằng `agent.approve(run_id, decisions)`, lệnh này tiếp tục run và trả về
stream mới của nó. Vòng lặp này trả lời mọi phê duyệt cho tới khi có kết quả:

```python
import asyncio

from linch_rust import (
    Agent,
    AgentConfig,
    Allow,
    Decision,
    Deny,
    PermissionRequestEvent,
    ResultEvent,
    Run,
    Server,
    tool,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def refund(order: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} on {order}"


def decide(request: dict) -> Decision:
    # Your policy, or a human's answer from a UI.
    if request["tool_name"] == "refund" and request["input"]["amount"] <= 50:
        return Allow()
    return Deny("refunds over 50 need a manager")


async def drive(agent: Agent, run: Run) -> ResultEvent:
    """Run until the result, answering every approval on the way."""
    while True:
        requests: list[dict] = []
        async for envelope in run:
            if isinstance(envelope.event, PermissionRequestEvent):
                requests.extend(envelope.event.requests)
            elif isinstance(envelope.event, ResultEvent):
                return envelope.event
        # The stream ended with `suspended`: answer what it asked.
        for request in requests:
            print("asked:", request["summary"], request["input"])
        decisions = {request["tool_use_id"]: decide(request) for request in requests}
        run = await agent.approve(run.id, decisions)


config: AgentConfig = {
    "namespace": "approvals",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "refund", {"order": "A-1", "amount": 20.0}),
            tool_turn("c2", "refund", {"order": "B-7", "amount": 500.0}),
            text_turn("One refund done, one needs a manager."),
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[refund])
        result = await drive(agent, await agent.run("s1", "Refund A-1 and B-7"))
        print(result.final_text)


asyncio.run(main())
```

```text
asked: refund {'order': 'A-1', 'amount': 20.0}
asked: refund {'order': 'B-7', 'amount': 500.0}
One refund done, one needs a manager.
```

- `Allow()` chạy lời gọi. `Deny(reason)` từ chối nó, và model đọc lý do trong kết
  quả lỗi của lời gọi.
- Lời gọi đang chờ mà chưa có quyết định sẽ được hỏi lại: run tạm dừng thêm một
  lần nữa.
- `agent.resume(run_id)` trên một run đang chờ phê duyệt sẽ tạm dừng nó lại với
  cùng các lời gọi đang chờ; không có gì chạy.
- Với kho Postgres, phê duyệt có thể đến từ một process khác hoặc một ngày sau; xem
  [Run](./runs.md#approve).

Để quyết định trong code mà không tạm dừng, hãy khai báo một
[callback](./agent.md#callback) `permission_request`: nó trả lời `allow` hoặc
`deny` trước khi run tạm dừng, và khi nó không đưa ra câu trả lời, lời gọi hỏi như
thường lệ.

## Hỏi người dùng

`ask_user: True` cho model `AskUser`: từ một đến bốn câu hỏi, mỗi câu có một
`header` ngắn (tối đa 12 ký tự), hai đến bốn lựa chọn (`label`, `description`) và
`multi_select`. Một lời gọi không chạy: nó kết thúc stream bằng một event
`input_request` (`kind` là `ask_user`, `request_id` là `tool_use_id` của lời gọi,
cùng các `questions`) và một event `suspended` với status `awaiting_input`. Không
có gì chờ trong bộ nhớ: hãy trả lời khi bạn sẵn sàng bằng
`agent.answer(run_id, answers)`, lệnh này tiếp tục run và trả về stream mới của nó.

```python
import asyncio

from linch_rust import AgentConfig, Answer, InputRequestEvent, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn, tool_turn

QUESTIONS = {
    "questions": [
        {
            "id": "db",
            "header": "Database",
            "question": "Which database should the service use?",
            "options": [
                {"label": "Postgres", "description": "Durable, shared by every server"},
                {"label": "Memory", "description": "For tests; gone on restart"},
            ],
        }
    ]
}

config: AgentConfig = {
    "namespace": "ask-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("ask_1", "AskUser", QUESTIONS), text_turn("Postgres it is.")],
    },
    "ask_user": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Set up storage")
        answers: dict[str, Answer] = {}
        async for envelope in run:
            event = envelope.event
            if isinstance(event, InputRequestEvent):
                for question in event.questions:
                    labels = [option["label"] for option in question["options"]]
                    print(question["question"], labels)
                # A UI would show the options and wait for the user.
                answers[event.request_id] = Answer({"db": "Postgres"})
        # The stream ended with `suspended` (status `awaiting_input`).
        result = await (await agent.answer(run.id, answers)).result()
        assert isinstance(result, ResultEvent)
        print(result.final_text)


asyncio.run(main())
```

```text
Which database should the service use? ['Postgres', 'Memory']
Postgres it is.
```

- `Answer({question_id: choice})` trả lời mọi câu hỏi: một chuỗi, hoặc một danh
  sách chuỗi với câu hỏi `multi_select`. Một lựa chọn thường là label của một
  option, nhưng người dùng có thể tự viết. `note=` thêm một ghi chú tự do.
- `Answer.declined(note)` từ chối: model được báo là tiếp tục với các giả định đã
  nêu.
- Model đọc câu trả lời như kết quả của lời gọi, dưới dạng JSON:
  `{"answers": {"db": "Postgres"}}`, kèm `"note"` khi có.
- Một request đang chờ mà chưa có câu trả lời sẽ được hỏi lại: run tạm dừng thêm
  một lần nữa. Một câu trả lời nêu tên request không đang chờ, bỏ sót một câu hỏi
  hoặc đưa danh sách cho câu hỏi chọn một sẽ gây ra `InvalidAnswer` (code -32032)
  và không ghi gì; run vẫn chờ.
- Câu trả lời được ghi trước khi run tiếp tục, nên sau một crash
  `agent.resume(run_id)` tiếp tục với nó, và cùng câu trả lời đó gửi lại sẽ là
  `NotSuspended` ([Run](./runs.md#answer)).
- Một [callback](./agent.md#callback) `pre_tool_use` xử lý lời gọi sẽ trả lời thay
  người dùng, và run không tạm dừng. Một subagent không thể hỏi: lời gọi của nó
  nhận một lỗi nói rằng run không thể chờ người dùng.
- Một response vừa hỏi vừa cần phê duyệt sẽ tạm dừng để chờ phê duyệt trước, rồi
  mới chờ câu trả lời.

## Tool lập lịch

`schedule_tools: True` cho model `CreateSchedule` (scope `exec`), `ListSchedules` (`read`)
và `CancelSchedule` (`write`), tức `register_schedule_tools` của Python. Một lịch do nó tạo
chạy chính agent này, trong session của run đang gọi, cho principal của run đang gọi, dưới
các mức trần lập lịch của tenant; nó chỉ liệt kê và hủy các lịch của chính agent này
trong session của run đang gọi. Nó
cần các store Postgres, không có host tool hay callback, và một tenant có allowlist cấp cho
nó `max_schedules`, và mặc định nó tắt: khi tắt, danh sách tool và hợp đồng của run vẫn như
trước. Mọi thứ còn lại nằm ở [Scheduler](./scheduler.md#để-model-lập-lịch).

## Hệ thống tệp ảo

`filesystem: {}` thêm `ls`, `read_file`, `write_file` và `edit_file` trên các tệp
văn bản nằm trong kho, không phải trên đĩa: ghi chú nháp, kế hoạch, và cả các kết
quả tool quá lớn cho cuộc hội thoại. Với `offload_threshold_tokens`, một kết quả
(chuỗi, giá trị có cấu trúc hoặc lỗi) được ước tính trên số token đó sẽ được ghi vào
`/offload/<tool>_<tool_use_id>.txt` và model nhận vài dòng đầu cùng đường dẫn:

```python
import asyncio

from linch_rust import AgentConfig, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def export_logs(service: str) -> str:
    """Return a service's recent log lines."""
    return "\n".join(f"{service} line {n}" for n in range(1, 2001))


config: AgentConfig = {
    "namespace": "vfs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("c1", "export_logs", {"service": "api"}),
            tool_turn("c2", "read_file", {"path": "/offload/export_logs_c1.txt", "offset": 1999}),
            text_turn("Read the tail."),
        ],
    },
    "permissions": {"mode": "default", "project_root": "/"},
    "filesystem": {"offload_threshold_tokens": 1000},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[export_logs])
        async for envelope in await agent.run("s1", "What is in the api logs?"):
            if isinstance(envelope.event, ToolCallEndEvent):
                text = envelope.event.result["content"][0]["text"]
                print(f"{envelope.event.tool_name}: {len(text)} chars, ends {text[-60:]!r}")


asyncio.run(main())
```

```text
export_logs: 371 chars, ends 'read the rest, or ls("/offload") to list offloaded results.]'
read_file: 27 chars, ends 'api line 1999\napi line 2000'
```

`ls` và `read_file` có scope read; `write_file` và `edit_file` có scope write, nên
chúng hỏi ở mode `default`. Một tệp ảo chứa tối đa 10 MiB: một `write_file` hoặc
`edit_file` (kể cả `replace_all`) làm nó lớn hơn sẽ nhận lỗi `too_large` và không
thay đổi gì. Chính kết quả của các tool hệ thống tệp thì không bao giờ bị offload.
Nơi các tệp nằm tuân theo kho lưu trữ: theo từng session trong memory, hoặc dùng
chung cho mọi session của namespace trong Postgres
([Kho lưu trữ](./stores.md#the-virtual-filesystem)).

<a id="long-tool-results"></a>

## Kết quả tool dài

Một kết quả đi vào view của model nguyên vẹn trừ khi có một giới hạn áp dụng, mà view là
thứ compaction và context window phải trả giá. Một số tool tự giới hạn đầu ra của mình:

| Tool | Giới hạn |
|---|---|
| `Bash`, `BashOutput` | 4000 byte đầu và 26000 byte cuối của mỗi luồng |
| `Grep` | mặc định 250 mục, đầu ra tối đa 30000 byte |
| `Glob` | 1000 đường dẫn |
| Một tool MCP | 30000 byte văn bản kết quả |
| `memory_recall` | 8 KiB ngữ cảnh được gợi lại mỗi request |
| `Read` | mặc định 2000 dòng và tệp tối đa 10 MiB; một dòng không bị giới hạn độ rộng, nên một dòng minify có thể trả về hàng megabyte (một khoảng trống đã biết: offload bên dưới phủ cả kết quả của `Read` và `read_file` phân trang một dòng dài, nhưng chính `Read` không cắt dòng) |

Một host tool không có giới hạn riêng. Với các tool đó, và với mọi kết quả mà một giới hạn ở
trên cho qua, cơ chế duy nhất là offload sang hệ thống tệp:
`filesystem: {"offload_threshold_tokens": 8000}` (hoặc `offload_threshold_fraction`) ghi một
kết quả có ước lượng trên ngưỡng vào một tệp và để lại cho model mười dòng đầu (tối đa 2000
byte; con trỏ ghi "cut at 2000 bytes" khi số byte cắt ngắn phần xem trước) cùng đường dẫn
([Hệ thống tệp ảo](#hệ-thống-tệp-ảo)). 8000 là giá trị khuyến nghị: một kết quả dưới
ngưỡng đó, khoảng 32 KB, đủ rẻ để giữ nguyên vẹn. Offload phủ:

- **Một chuỗi**, được lưu nguyên.
- **Một giá trị có cấu trúc** (đối tượng hoặc mảng JSON của một host tool hay tool MCP), được ước
  lượng theo JSON gọn của nó và lưu dưới dạng JSON định dạng đẹp, mỗi giá trị một dòng, nên
  `read_file` phân trang được theo dòng. Model thấy con trỏ, là một chuỗi; `metadata` của lời gọi
  chỉ giữ các kích thước (`original_tokens`, `original_bytes`, `offloaded_to`,
  `original_kind: "json"`), không bao giờ giữ chính giá trị.
- **Một lỗi**: một thông báo trên ngưỡng được lưu nguyên, và model thấy vài dòng đầu cùng đường
  dẫn làm thông báo của chính lỗi đó. `code` của lỗi và `details` của nó được giữ (`details` có
  thêm `offloaded_to`, `original_tokens` và `original_bytes` khi nó là một đối tượng hoặc vắng
  mặt), nên việc thử lại và loop guard phân loại nó như trước.
- **Một dòng rất dài** (JSON hoặc XML minify, base64, protobuf gửi dưới dạng văn bản): nó được
  offload như một chuỗi, và `read_file` trả về tối đa 16384 byte của một dòng mỗi lần gọi, cắt tại
  ranh giới ký tự và kết thúc bằng lời gọi để đọc tiếp, ví dụ
  `[line 1 continues at byte 16384; read_file(path="/offload/x.txt", offset=1, limit=1, byte_offset=16384)]`.
  `byte_offset` là byte của dòng để bắt đầu và đi cùng `limit=1`; một tệp mà mọi dòng đều ngắn hơn
  được đọc đúng như khi lưu.

Trong một lần chạy kịch bản 30 lượt, mỗi lượt có một kết quả tool 2, 12, 60 hoặc 200 KB và
không có compaction, request cuối nặng 484956 token ước lượng khi không có giới hạn và 141961
khi mọi kết quả bị giữ ở khoảng 8000 token, còn tổng 30 request là 7384454 và 2190541, ít hơn
khoảng 70%. Các số này được đo bằng bộ test kịch bản với một cách cắt giữ đầu và đuôi mỗi kết
quả trong view, không phải với model thật và không phải với chính offload; offload chỉ giữ mười
dòng của một kết quả dài, nên mức tiết kiệm ít nhất cũng bằng, trừ phần model chọn đọc lại.

Offload không phủ mọi kết quả. Kết quả dưới ngưỡng được giữ nguyên vẹn, và nhiều kết quả như
vậy cộng dồn lại; compaction mới là thứ cắt bớt chúng. Chính kết quả của bốn tool hệ thống tệp đi
qua nguyên vẹn. Một tool không thể trả về ảnh hay dữ liệu nhị phân dưới dạng JSON, nên không có gì
loại đó để offload. Các dòng của chính `Read` không bị cắt (xem bảng). Một `read_file` không có `offset` hay `limit` trả về nguyên một tệp gồm các dòng ngắn, dài bao nhiêu cũng vậy (một giá trị có cấu trúc dài hơn khi định dạng đẹp so với ban đầu), vì thế con trỏ yêu cầu `offset` và `limit`. `details` của một lỗi dành
cho host và không bao giờ tới model, nên không được offload. [Run contract](./agent.md#the-run-contract)
của agent chứa phiên bản policy của hook và schema của `read_file`, nên một run đã lưu trước khi
một trong hai thay đổi không thể được resume (`ContractMismatch`). Với store trong bộ nhớ, các tệp chỉ sống bằng tiến trình, nên một
run được resume ở nơi khác thấy con trỏ nhưng không thấy tệp
([Kho lưu trữ](./stores.md#the-virtual-filesystem)).

## MCP tool

Mỗi phần tử của `mcp_servers` được kết nối khi agent được mở; một server không kết
nối được sẽ làm việc mở thất bại với `OpenError` (code -32015) nêu tên nó. Tool
của nó được cấp dưới tên `mcp__<name>__<tool>`, với scope `exec` trừ khi bật
`trust_annotations` và server đánh dấu tool là chỉ đọc. Ví dụ này cần một MCP
server (nó đã được kiểm tra với một test server có tool `echo` chỉ đọc):

```python
import asyncio

from linch_rust import AgentConfig, InitEvent, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

COMMAND = "/path/to/your-mcp-server"

config: AgentConfig = {
    "namespace": "mcp-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("c1", "mcp__demo__echo", {"text": "hi"}), text_turn("Done.")],
    },
    "mcp_servers": [
        {
            "name": "demo",
            "transport": {"type": "stdio", "command": COMMAND, "args": []},
            "trust_annotations": True,
            "call_timeout_ms": 30_000,
        }
    ],
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Echo hi"):
            event = envelope.event
            if isinstance(event, InitEvent):
                print("tools:", event.tools)
            elif isinstance(event, ToolCallEndEvent):
                print(event.tool_name, "|", event.result["content"][0]["text"])


asyncio.run(main())
```

```text
tools: ['mcp__demo__echo', 'mcp__demo__fail', 'mcp__demo__image', 'mcp__demo__pid', 'mcp__demo__slow']
mcp__demo__echo | hi
```

Một HTTP server có dạng `{"type": "http", "url": "http://127.0.0.1:8000/mcp",
"headers": {"Authorization": "Bearer ..."}}`. Các tool mà server có khi agent được
mở đi vào hợp đồng của run; tool mà nó thêm sau đó thì không. Mọi trường và giá
trị mặc định nằm ở [Config của agent](./agent.md#mcp_servers); việc kết nối lại,
hủy và các giới hạn nằm ở [trang MCP của kiến trúc](../architecture/mcp.md).

## Retry và cache

Hai trường của spec giúp công việc tool lặp lại rẻ hơn hoặc an toàn hơn. Cả hai đều tắt
khi không được đặt, và một agent không đặt cái nào thì giữ nguyên hợp đồng của run.

### `tool_retry`

Một host tool thất bại với lỗi tạm thời có thể chạy lại, nhưng chỉ khi việc đó an toàn.
Một lời gọi được retry khi tất cả các điều sau đều đúng:

- `spec.tool_retry` được đặt;
- tool tự khai báo là **idempotent** (`@tool(idempotent=True)`; các tool tích hợp sẵn
  `Read`, `Grep` và `Glob` thì có): chạy hai lần không có tác dụng nào ngoài kết quả của
  nó. Một tool không khai báo thì không bao giờ chạy hai lần, dù policy nói gì;
- thất bại là một kết quả lỗi có `code` nằm trong `retry_on`. Một host tool
  đặt nó bằng `raise ToolError("busy", code="transient")`; một mục `timeout`
  cũng retry một lời gọi chạy quá `call_timeout_ms`. Một panic, một
  output vi phạm schema của tool và một lỗi không có mã thì không được retry;
- còn lần thử.

```python
config["spec"] = {
    "model": "deepseek-flash",
    "tool_retry": {"max_attempts": 3, "base_delay_ms": 200, "retry_on": ["transient"]},
}
```

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `max_attempts` | 3 | Số lần thử của một lời gọi, kể cả lần đầu; tối thiểu 2 |
| `base_delay_ms` | 200 | Thời gian chờ trước lần thử thứ hai; mỗi lần retry chờ gấp đôi, đến `max_delay_ms` |
| `max_delay_ms` | 5000 | Thời gian chờ dài nhất; tối thiểu `base_delay_ms` |
| `retry_on` | `["transient"]` | Các lớp lỗi được retry: `code` của một lỗi tool, hoặc `timeout` |
| `tools` | mọi tool idempotent | Thu hẹp các tool có thể được retry |

Mỗi lần retry ghi một [event `tool_retry`](./events.md#toolretryevent-tool_retry)
và flush nó trước khi chờ và trước lần thử kế tiếp. Một lần crash sau đó để lại
lời gọi có một start và các lần thử nhưng không có end, và một run được resume báo nó là
bị gián đoạn và không chạy lại, nên một lần retry không bao giờ nhân đôi một tác dụng
qua một lần khởi động lại. Kiểm tra permission và các callback `pre_tool_use` chạy
một lần cho mỗi lời gọi; một callback `tool_execute` chạy một lần cho mỗi lần thử.

Có ba giới hạn. `error` của event là thông điệp của lần thử thất bại, đã qua
`spec.redaction` (khi nó bao phủ kết quả tool) rồi cắt còn 512 byte, vì event được lưu và
stream trước khi bất kỳ callback `post_tool_use` nào thấy lỗi. Các lần retry của một lời gọi
dừng cùng nhau khi đã qua năm phút kể từ lúc lần thử đầu bắt đầu (mỗi lần thử vẫn có
`call_timeout_ms` riêng), và một khoảng chờ bị cắt còn đúng phần thời gian còn lại. Cuối
cùng, một host tool hết thời gian bị hủy bằng một notification mà client không bao giờ
xác nhận, nên lần thử kế tiếp có thể bắt đầu khi handler cũ vẫn đang kết thúc: một host
tool bạn khai báo `idempotent` phải chịu được hai lời gọi chồng lên nhau.

### `tool_cache`

`spec.tool_cache` dùng lại kết quả của một tool read cho một lời gọi giống hệt.
Bắt buộc phải nêu tên các tool: Linch không thể phân biệt một tool tất định với một
tool hay thay đổi, và việc nêu tên một tool là lời hứa của bạn rằng nó trả lời như nhau
với cùng một input.

```python
config["spec"] = {
    "model": "deepseek-flash",
    "tool_cache": {"tools": ["lookup"], "ttl_ms": 60_000},
}
```

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `tools` | bắt buộc | Các tool có thể cache. Một tool không phải scope `read`, có polling hoặc hỏi người dùng thì không bao giờ được cache, kể cả khi được liệt kê |
| `ttl_ms` | 300000 | Một mục sống bao lâu |
| `max_entries` | 256 | Số mục giữ cho namespace; mục cũ nhất đi trước |
| `max_value_bytes` | 100000 | Một kết quả có JSON lớn hơn thì không được lưu |
| `invalidate_on_write` | `True` | Có hay không việc một lời gọi tool `write` hoặc `exec` xóa các mục của namespace, trước khi nó bắt đầu và sau khi nó kết thúc |
| `scope` | `"session"` | `"session"`: một mục chỉ phục vụ session đã lưu nó. `"namespace"`: mọi session của namespace dùng chung các mục |

**Khóa là gì.** `namespace` của agent, session (với `scope` mặc định), principal của run
(tenant và user, khi nó có), tên tool và input chuẩn hóa. Một tool read có thể trả lời theo
từng session (file riêng của session, home của shell) hoặc theo từng principal (`MemorySearch`
đọc bộ nhớ riêng của principal), nên mặc định một session không bao giờ nhận kết quả của
session khác và không `scope` nào đưa kết quả của principal này cho principal khác;
`scope: "namespace"` chia sẻ các mục giữa các session của một principal và là lời hứa của
bạn rằng tool trả lời như nhau cho chúng. Các mục không bao giờ lan sang namespace khác. Một lời gọi write hoặc exec xóa
cả namespace, mục của mọi session, và `max_entries` đếm cả namespace. Registry tool không
có phiên bản hay digest của tool, nên khóa cũng không có: khi hành vi của một tool thay
đổi, các mục cũ sống đến hết `ttl_ms`. Với store Postgres, các mục nằm trong một bảng
(dùng chung cho mọi server của namespace, sống qua các lần khởi động lại); với store
memory chúng nằm trong agent.

**Điều gì không bao giờ được cache.** Một tool không phải scope `read`, có polling hoặc hỏi
người dùng, và một tool mà lần chạy giữ trạng thái cho session: `Read` có sẵn ghi lại điều
session đã thấy để `Edit` và `Write` có thể theo sau, và một lần hit sẽ bỏ qua việc đó;
`Skill` và `tool_search` thay đổi những gì session được cung cấp, nên một lần hit cũng bỏ qua điều đó.
Nêu một tool như vậy trong `tools` bị từ chối khi agent mở. Hãy cache `Grep`, `Glob` và
các host tool của bạn.

**Một lần hit không bao giờ bỏ qua một kiểm tra, và chỉ bỏ qua lần chạy của tool.** Lời gọi
đã qua xác thực, các callback `pre_tool_use` và permission, nên một lời gọi bị từ chối
hoặc bị chặn không bao giờ chạm tới cache. Một lần hit sau đó chỉ thay thế lần chạy của
chính tool: `post_tool_use`, redaction và output schema chạy trên nó như trên một kết quả
mới. Các layer `tool_execute` bao quanh lần chạy của tool, nên chúng **không** chạy khi
hit: một layer phải thấy mọi kết quả, như một bộ lọc nội dung fail-closed, thì thuộc về
`post_tool_use`. `tool_call_end` của nó có `cache_hit: true`, và `tool_call_start` của nó
được ghi cùng với end đó, không phải trước nó, vì không có gì chạy.

**Thứ được lưu.** Kết quả thành công do chính tool trả về, sau khi kiểm tra output schema và
`spec.redaction` trên kết quả tool, trước `post_tool_use`, không bao giờ là một lỗi. Nếu có
layer `tool_execute`, một kết quả chỉ được lưu khi các layer trả nó về nguyên vẹn: một veto
hay một thay thế không bao giờ được cache, nên một lần hit không bao giờ phục vụ đầu ra mà
một layer đã che. **Không có `spec.redaction` bao phủ kết quả tool thì kết quả được lưu đúng
như tool trả về**, kể cả bí mật, trong memory của agent hoặc bảng Postgres; hãy cấu hình
redaction trước khi cache một tool có thể trả về bí mật.

**Ghi, race và việc chạy nền.** Một lời gọi `write` hoặc `exec` xóa namespace trước khi nó
bắt đầu và sau khi nó kết thúc. Mỗi namespace có một epoch mà mỗi lần xóa tăng lên, và một
lần lookup nhớ nó: một lần đọc bắt đầu trước một lần ghi không thể lưu câu trả lời trước
khi ghi của nó sau lần xóa của lần ghi. Lời gọi đầu tiên của một tool khởi động công việc
sống lâu hơn lời gọi (`Bash` với `run_in_background`, một subagent chạy nền) chấm dứt việc
cache cho agent cho đến khi nó đóng, vì không có gì cho biết khi nào công việc đó đổi câu
trả lời của một tool read. Các server khác dùng chung bảng Postgres không thấy lần khởi
động đó, nên hãy để `tool_cache` tắt trên agent chạy việc nền trong một workspace mà các
server khác cache.

**Lỗi store.** Mỗi lời gọi store bị cắt sau 2 giây, và một lỗi hay một lần hết thời gian là
một miss: model không bao giờ thấy nó. Việc hủy run cũng cắt ngắn một lần lookup. Một lần
xóa thất bại hoặc hết thời gian có thể để lại mục cũ, nên việc cache chấm dứt cho agent.
Lần xóa sau một lần ghi không bị hủy cắt, vì lần ghi có thể đã đổi trạng thái; nó vẫn bị
giới hạn bởi 2 giây. Một lần xóa thất bại được thử thêm một lần trước khi việc cache chấm
dứt, và server ghi một cảnh báo `tracing` khi đó (`the tool cache stopped for an agent`, kèm
lý do; xem [Server](./server.md#log)).

**Khoảng hở sau một lần xóa thất bại.** Việc ngừng cache chỉ bảo vệ agent đã xóa thất bại: các
mục vẫn nằm trong bảng, nên một agent khác của namespace, và một server khác dùng chung cache
Postgres, vẫn phục vụ chúng đến hết ttl (mặc định một giờ với một tenant), dù một lần ghi đã
đổi thứ chúng giữ. Hãy giữ `ttl_ms` ngắn nơi một câu trả lời cũ gây tốn kém, và để `tool_cache`
tắt trên một workspace mà nhiều server cùng ghi.

**Giới hạn.** Các mục bị giới hạn theo namespace (`max_entries`, `max_value_bytes`). Một
lần put Postgres cũng xóa một lô có giới hạn các hàng hết hạn của bất kỳ namespace nào, nên
một namespace không ai dùng lại không giữ hàng của nó mãi. Một tenant từ xa còn bị giới hạn
thêm: xem [Server](./server.md#những-gì-admission-từ-chối-và-lý-do).

## Các trang liên quan

- [Config của agent](./agent.md): `workspace`, `permissions`, `mcp_servers`,
  `filesystem`, callback.
- [Run](./runs.md): phê duyệt, resume và hủy.
- [Event](./events.md): `tool_call_start`, `tool_call_end`,
  `permission_request`, `suspended`.
- [Kiến trúc: tool](../architecture/tools.md): phần Rust.
