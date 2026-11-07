# Tool

[Bản gốc tiếng Anh](../../usage/tools.md)

Dùng trang này để cung cấp tool cho agent: các hàm Python trong process của bạn,
các workspace tool tích hợp sẵn, hệ thống tệp ảo và MCP server, đồng thời quyết
định lời gọi nào chạy, hỏi hay bị từ chối.

[<- Hướng dẫn sử dụng](./README.md)

| Tool | Chạy ở đâu | Được bật bởi |
|---|---|---|
| Host tool | Trong process Python của bạn, được server gọi ngược | `open_agent(config, tools=[...])` |
| Workspace tool: `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash` | Trong server, trên một thư mục | `workspace` (và `bash`) |
| Hệ thống tệp ảo: `ls`, `read_file`, `write_file`, `edit_file` | Trong server, trên các tệp văn bản lưu trong kho | `filesystem` |
| MCP tool: `mcp__<server>__<tool>` | Trong MCP server | `mcp_servers` |
| Task của session: `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate` | Trong server | `tasks` ([Task](./tasks.md)) |
| `Skill`, `Subagent`, `SubagentContinue`, `TaskStop`, `send_message` | Trong server | `skills`, `subagents`, `workers`, `mailbox` ([Skill](./skills.md), [Subagent](./subagents.md)) |
| `tool_search` | Trong server | `tool_search` ([Config của agent](./agent.md#trường-cấp-cao-nhất)) |

Event `init` của mỗi run liệt kê tên các tool được cấp cho model, theo thứ tự:
host tool trước, rồi các tool còn lại.

---

## Host tool

Host tool là một hàm bất đồng bộ được gắn decorator `@tool`. Server xác thực input
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

Lỗi tool không mang metadata. Một callback `post_tool_use` thay thế kết quả sẽ
khiến event mang metadata của kết quả thay thế.

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
(`bash: False` loại `Bash` ra):

| Tool | Scope | Làm gì |
|---|---|---|
| `Read` | read | Văn bản UTF-8 với các dòng được đánh số; `offset` và `limit`, mặc định 2000 dòng; tệp trên 10 MiB bị từ chối |
| `Write` | write | Thay thế cả tệp theo kiểu nguyên tử, tạo các thư mục cha |
| `Edit` | write | Thay thế đoạn văn bản khớp chính xác, một lần trừ khi có `replace_all` |
| `Grep` | read | Engine của ripgrep; các mode `files_with_matches`, `count`, `content` |
| `Glob` | read | Quy tắc glob của ripgrep, mới nhất trước, tối đa 1000 đường dẫn |
| `Bash` | exec | `/bin/sh -c` tại gốc của workspace |

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
- **Giới hạn phạm vi.** Đường dẫn là tương đối so với workspace. `..`, đường dẫn
  tuyệt đối nằm ngoài nó và symlink dẫn ra ngoài đều bị từ chối.
- **`Bash` không bị giới hạn phạm vi.** Nó bắt đầu trong workspace nhưng có thể
  chạm tới mọi thứ mà user của server chạm được. Nó hỏi ở các mode `default` và
  `accept_edits`; chỉ chín biến trong môi trường của server (`PATH`, `HOME`,
  `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`) đến được lệnh,
  nên key của provider thì không. Timeout của nó mặc định là 120 giây.

Các giới hạn, việc kill cả process group và mức trần của đầu ra nằm ở
[trang tool của kiến trúc](../architecture/tools.md#workspace-tools).

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
- Một lệnh shell mà bộ tách lệnh không mô hình hóa được (chẳng hạn có `$(...)`) có
  thể bị rule deny hoặc ask nhưng không bao giờ được rule allow.
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

## Hệ thống tệp ảo

`filesystem: {}` thêm `ls`, `read_file`, `write_file` và `edit_file` trên các tệp
văn bản nằm trong kho, không phải trên đĩa: ghi chú nháp, kế hoạch, và cả các kết
quả tool quá lớn cho cuộc hội thoại. Với `offload_threshold_tokens`, một kết quả
văn bản được ước tính trên số token đó sẽ được ghi vào
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
thay đổi gì. Lỗi, kết quả có giá trị không phải chuỗi (chẳng hạn dict của một host
tool) và chính kết quả của các tool hệ thống tệp thì không bao giờ bị offload.
Nơi các tệp nằm tuân theo kho lưu trữ: theo từng session trong memory, hoặc dùng
chung cho mọi session của namespace trong Postgres
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

## Các trang liên quan

- [Config của agent](./agent.md): `workspace`, `permissions`, `mcp_servers`,
  `filesystem`, callback.
- [Run](./runs.md): phê duyệt, resume và hủy.
- [Event](./events.md): `tool_call_start`, `tool_call_end`,
  `permission_request`, `suspended`.
- [Kiến trúc: tool](../architecture/tools.md): phần Rust.
