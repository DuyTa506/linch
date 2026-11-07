# Run

[Bản gốc tiếng Anh](../../usage/runs.md)

Dùng trang này để bắt đầu, stream, resume, phê duyệt và hủy run, và để biết điều
gì còn lại sau một lần crash.

[<- Hướng dẫn sử dụng](./README.md)

Một run là một prompt trong một session. `agent.run`, `agent.resume` và
`agent.approve` đều trả về một `Run` ngay khi server chấp nhận lời gọi; các event
của run sau đó được stream về dưới dạng `Envelope`.

| Lời gọi | Làm gì |
|---|---|
| `await agent.run(session_id, prompt, *, run_id=None, principal=None, trace_id=None)` | Bắt đầu một run; session được tạo nếu chưa có. `trace_id` là của bạn, được lặp lại trên các event `init` và `result` của run ([Observability](./observability.md#correlate-logs-and-traces)) |
| `await agent.resume(run_id, *, principal=None)` | Tiếp tục một run sau crash hoặc sau khi stream bị rớt |
| `await agent.approve(run_id, decisions, *, principal=None)` | Trả lời các lời gọi đang chờ của một run bị tạm dừng và tiếp tục nó |
| `async for envelope in run` | Mọi envelope của lời gọi này, theo thứ tự, kết thúc sau `result` hoặc `suspended` |
| `await run.result()` | Đọc phần còn lại và trả về `ResultEvent` hoặc `SuspendedEvent` |
| `await run.cancel()` | Hủy run; `True` khi server đang điều khiển nó |
| `await agent.events(run_id, after_seq=0)` | Các envelope bền vững của run sau `after_seq`, lấy từ các kho lưu trữ |
| `run.id` | Run id, dùng cho `resume`, `approve` và `events` |
| `run.trace_id` | Trace id của các span của lời gọi khi nó được trace ([Observability](./observability.md#correlate-logs-and-traces)) |

---

## Bắt đầu và stream

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server, SessionBusy, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def save(text: str) -> str:
    """Save a note."""
    return "saved"


config: AgentConfig = {
    "namespace": "runs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [text_turn("First answer."), tool_turn("c1", "save", {"text": "x"})],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save])

        # Iterate for every envelope, or await result() for the end only.
        run = await agent.run("s1", "Hello", run_id="order-42-reply")
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(run.id, result.subtype, result.final_text)

        # Replay what was durable, from the start or after a seq you saw.
        replayed = await agent.events(run.id)
        print([e.event.type for e in await agent.events(run.id, after_seq=replayed[2].seq)])

        # A second live run on a session is refused, naming the live one.
        waiting = await agent.run("s1", "Save x")
        assert isinstance(await waiting.result(), SuspendedEvent)
        try:
            await (await agent.run("s1", "Something else")).result()
        except SessionBusy as busy:
            print("busy with", busy.run_id == waiting.id)


asyncio.run(main())
```

```text
order-42-reply success First answer.
['assistant', 'usage', 'result']
busy with True
```

- Một stream kết thúc bằng `result` khi run đã xong, hoặc `suspended` khi nó chờ
  một lần phê duyệt. `result.subtype` là `success`, `error`, `aborted` hoặc
  `interrupted`.
- Bạn có thể lặp qua một phần của run rồi gọi `result()` cho phần còn lại. Ngừng
  lặp không dừng run: server vẫn tiếp tục điều khiển nó và các envelope của nó chờ
  trong bộ nhớ. Hãy hủy nó nếu bạn không còn cần.
- **Hãy chọn run id** khi bạn cần tìm lại run ngay cả khi `run()` chưa bao giờ trả
  về (timeout, process của bạn bị crash). Server tự chọn một UUID khi bạn không
  chọn. Một id đã được lưu sẽ kết thúc run bằng `RunError` (code -32023) và không
  ghi gì.
- Nhiều run của một server stream cùng lúc; mỗi `Run` chỉ nhận envelope của chính
  nó.

<a id="one-live-run-per-session"></a>

### Một run đang chạy cho mỗi session

Một session có nhiều nhất một run chưa kết thúc: một run đang chạy hoặc đang chờ
phê duyệt. Bắt đầu một run khác sẽ kết thúc run mới bằng `SessionBusy` (code
-32030) trước khi bất kỳ thứ gì được ghi; `busy.session_id` và `busy.run_id` nêu
tên run đang chạy. Hãy resume, phê duyệt hoặc hủy run đó trước, hoặc xếp hàng
prompt. Với Postgres, quy tắc này đúng trên mọi server dùng chung database.

## Lỗi đến ở cuối stream

`run()`, `resume()` và `approve()` trả về trước khi server làm gì ngoài việc chấp
nhận lời gọi. Một sự từ chối (session đang bận, run không xác định, contract khác
nhau) và một lỗi khi tiếp tục bền vững sẽ đến dưới dạng phần cuối của stream: lặp
hoặc gọi `result()` sẽ raise một subclass của `RunError`.

| Exception | Code | Khi nào |
|---|---|---|
| `RunNotFound` | -32021 | Không có run đó trong các kho lưu trữ của agent (với kho memory: không có trong handle này) |
| `RunError` | -32023 | `run_id` đã được lưu |
| `ContractMismatch` | -32025 | Run đã bắt đầu dưới một contract khác ([Config của agent](./agent.md#hợp-đồng-của-run)); `data["expected"]` là contract của run và `data["found"]` là của agent này, như thông báo nêu |
| `RunTerminal` | -32026 | Run đã kết thúc; `data.status` là trạng thái của nó |
| `NotSuspended` | -32027 | `approve` trên một run không đang chờ phê duyệt ([bên dưới](#approve)) |
| `PromptBlocked` | -32029 | Một callback `user_prompt_submit` đã từ chối prompt; `.reason` cho biết lý do. Không có run hay session nào được tạo |
| `SessionBusy` | -32030 | Session có một run đang chạy |
| `RunError` | -32020 đến -32028 | Việc ghi vào kho hoặc journal thất bại, hoặc trạng thái đã lưu không khớp với nhau; run giữ nguyên ở trạng thái bền vững cuối cùng của nó |
| `ConnectionClosed` | không có | Server biến mất giữa chừng stream |

Có một sự từ chối đến từ chính lời gọi: `RunBusy` (code -32002) khi server này đã
điều khiển run id đó, ví dụ một `resume` cho run mà bạn vẫn đang stream.
`agent.events` cũng raise lỗi của nó trực tiếp. Lỗi của provider hoặc tool không
phải là exception: đó là một event `error` và một `result` có subtype `error`.
Mọi thông báo nằm ở trang [Khắc phục sự cố](./troubleshooting.md).

## Cancel

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, RunBusy, Server, ToolCallStartEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def crawl(url: str) -> str:
    """Fetch a page, slowly."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("crawl cancelled")
        raise
    return "page"


config: AgentConfig = {
    "namespace": "runs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("c1", "crawl", {"url": "https://example.com"}), text_turn("unused")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[crawl])
        run = await agent.run("s1", "Crawl example.com")
        async for envelope in run:
            if isinstance(envelope.event, ToolCallStartEvent):
                try:
                    await agent.resume(run.id)
                except RunBusy:
                    print("this server already drives", run.id == envelope.run_id)
                print("cancelled:", await run.cancel())
            elif isinstance(envelope.event, ResultEvent):
                print(envelope.event.subtype)
        print("cancel again:", await run.cancel())


asyncio.run(main())
```

```text
this server already drives True
cancelled: True
aborted
crawl cancelled
cancel again: False
```

`cancel()` hủy lời gọi model hoặc tool đang chạy (task của một host tool bị hủy) và
kết thúc run bằng một `result` có subtype `aborted`; hãy tiếp tục đọc stream để
thấy nó. Nó trả về `False`, không phải lỗi, khi server không còn điều khiển run.
Một run bị abort là đã kết thúc: session của nó nhận được run mới. Chỉ server đang
điều khiển một run mới hủy được nó.

Hủy chính lời gọi `run`, `resume` hoặc `approve` (chẳng hạn bằng
`asyncio.wait_for`) trước khi nó trả về sẽ raise `CancelledError` như thường lệ.
Run mà server có thể đã bắt đầu cho lời gọi đó bị hủy ngay khi câu trả lời của nó
đến, và `agent.close()` chờ run đó kết thúc trước khi giải phóng agent.

Hãy đọc mọi run bạn bắt đầu đến hết. Một kết nối giữ tối đa 1024 batch event chưa
đọc trên tất cả các run của nó; vượt quá mức đó, client ngừng đọc, nên server tạm
dừng các run của nó thay vì để bộ nhớ của client tăng lên. Các câu trả lời cũng
phải chờ sau các batch đó: một run bị bỏ không đọc trong khi nó vẫn sinh event sẽ
làm các lời gọi khác trên kết nối của nó bị kẹt. `cancel()` vẫn đọc run của nó
trong lúc chờ, nên nó không bao giờ tự chờ chính mình.

## Resume

`resume(run_id)` tiếp tục một run từ trạng thái bền vững cuối cùng của nó. Dùng nó
sau khi process của bạn hoặc server bị crash, sau khi stream bị rớt, hoặc để nhận
một run mà process khác đã bắt đầu. Một run đang chờ phê duyệt sẽ tạm dừng lại
ngay. Chỉ resume sau khi stream trước đó đã kết thúc (việc lặp của nó đã xong hoặc
đã raise); trong khi một server còn đang điều khiển run, `resume` sẽ là `RunBusy`.

Handle nào resume được phụ thuộc vào các kho lưu trữ:

- **Kho memory**: run nằm trong agent handle đã bắt đầu nó. Hãy resume và phê duyệt
  nó qua chính `Agent` đó; nó mất khi server dừng.
- **Kho Postgres**: bất kỳ agent nào được mở với cùng config và tool (nên cùng
  contract), trên bất kỳ server nào, đều resume được.

Trên Postgres, sau một lần crash (ví dụ này đọc DSN từ `DATABASE_URL`):

```python
import asyncio
import os
import signal

from linch_rust import (
    AgentConfig,
    ConnectionClosed,
    ResultEvent,
    Server,
    ToolCallStartEvent,
    UserEvent,
    tool,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def lookup(q: str) -> str:
    """Look something up, slowly."""
    await asyncio.sleep(3600)
    return q


def config(turns: list) -> AgentConfig:
    return {
        "namespace": "runs-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": os.environ["DATABASE_URL"]},
    }


async def main() -> None:
    # First process: the server dies while a tool call is in flight.
    async with Server() as server:
        agent = await server.open_agent(config([tool_turn("c1", "lookup", {"q": "x"})]), tools=[lookup])
        run = await agent.run("s1", "Look up x", run_id="job-7")
        try:
            async for envelope in run:
                if isinstance(envelope.event, ToolCallStartEvent):
                    os.kill(server.pid, signal.SIGKILL)
        except ConnectionClosed as closed:
            print("lost the server:", closed)

    # Later, another process: same config and tools, so the same contract.
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Recovered.")]), tools=[lookup])
        resumed = await agent.resume("job-7")
        result = await resumed.result()
        assert isinstance(result, ResultEvent)
        print(result.subtype, result.final_text)
        for envelope in await agent.events("job-7"):
            if isinstance(envelope.event, UserEvent) and envelope.event.subtype == "tool_result":
                print(envelope.event.message["content"][0]["content"][0]["text"])


asyncio.run(main())
```

```text
lost the server: the server closed its output
success Recovered.
Tool execution was interrupted before a result was recorded; not re-running automatically on resume.
```

Một lời gọi tool đã được ghi điểm bắt đầu nhưng chưa ghi điểm kết thúc sẽ không bao
giờ được chạy lại: model nhận một kết quả lỗi nói rằng nó đã bị gián đoạn, và tự
quyết định phải làm gì. Dù vậy, hãy làm cho các host tool có thay đổi trạng thái an
toàn khi gọi hai lần, hoặc kiểm tra trước khi hành động, vì crash có thể xảy ra
sau tác động và trước khi ghi bản ghi.

## Approve

`approve(run_id, decisions)` nhận một `Allow()` hoặc `Deny(reason)` cho mỗi
`tool_use_id` của event `suspended`; lời gọi bị bỏ sót sẽ được hỏi lại. Vòng lặp
trả lời các phê duyệt nằm ở trang [Tool](./tools.md#phê-duyệt).

Chỉ run đang chờ phê duyệt mới nhận được một lần approve. Khi một approve đã commit
các câu trả lời của nó, chính approve đó được gửi lại (vì process của bạn bị crash
hoặc kết nối rớt trước khi bạn thấy kết quả) sẽ kết thúc bằng `NotSuspended` và
không ghi gì; hãy tiếp tục run đó bằng `resume`, vốn giữ các quyết định đã commit.
Một approve trên run đã kết thúc là `RunTerminal`.

```python
import asyncio
import os
import signal

from linch_rust import (
    Agent,
    AgentConfig,
    Allow,
    ConnectionClosed,
    Decision,
    NotSuspended,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallStartEvent,
    tool,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def deploy(service: str) -> str:
    """Deploy a service."""
    await asyncio.sleep(3600)
    return "deployed"


def config(turns: list) -> AgentConfig:
    return {
        "namespace": "runs-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": os.environ["DATABASE_URL"]},
    }


async def approve_once(
    agent: Agent, run_id: str, decisions: dict[str, Decision]
) -> ResultEvent | SuspendedEvent:
    """Approve, or continue a run whose approval was already committed."""
    try:
        return await (await agent.approve(run_id, decisions)).result()
    except NotSuspended:
        return await (await agent.resume(run_id)).result()


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([tool_turn("c1", "deploy", {"service": "api"})]), tools=[deploy])
        run = await agent.run("s1", "Deploy api", run_id="deploy-1")
        assert isinstance(await run.result(), SuspendedEvent)
        approved = await agent.approve("deploy-1", {"c1": Allow()})
        try:
            async for envelope in approved:
                if isinstance(envelope.event, ToolCallStartEvent):
                    os.kill(server.pid, signal.SIGKILL)  # a crash mid-deploy
        except ConnectionClosed:
            print("lost the server; did the approve land?")

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Deploy was interrupted.")]), tools=[deploy])
        result = await approve_once(agent, "deploy-1", {"c1": Allow()})
        assert isinstance(result, ResultEvent)
        print(result.final_text)


asyncio.run(main())
```

```text
lost the server; did the approve land?
Deploy was interrupted.
```

## Hành động thay cho một principal

`principal` cho biết một run hành động thay cho ai: `{"tenant", "user", "roles"?}`
(typed dict `Principal`). Các permission rule có `role` hoặc `unless_role` so khớp
với các role của nó ([Tool](./tools.md#permission-rule)), và request của một
callback `permission_request` mang theo nó. Hãy truyền nó cho `run`, và truyền lại
cho từng `resume` và `approve`: mỗi lời gọi hành động thay cho principal mà nó
được đưa, và lời gọi không có thì không hành động thay cho ai. Trên một server từ
xa, `tenant` phải là tenant của kết nối
([Server](./server.md#tenants-and-namespaces)).

```python
import asyncio

from linch_rust import AgentConfig, Principal, Server, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def refund(order: str) -> str:
    """Refund an order."""
    return f"refunded {order}"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [
        tool_turn("c1", "refund", {"order": "A-1"}),  # the clerk's run
        tool_turn("c1", "refund", {"order": "A-1"}),  # the manager's run
        text_turn("Refunded A-1."),
    ]},
    "permissions": {
        "mode": "default",
        "project_root": "/",
        "layers": [{"name": "app", "rules": [
            {"type": "tool", "tool": "refund", "decision": "allow", "role": "manager"},
        ]}],
    },
}
clerk: Principal = {"tenant": "acme", "user": "bob"}
manager: Principal = {"tenant": "acme", "user": "ada", "roles": ["manager"]}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[refund])
        for session, who in (("s1", clerk), ("s2", manager)):
            stopped = await (await agent.run(session, "Refund A-1", principal=who)).result()
            print(who["user"], "->", type(stopped).__name__)


asyncio.run(main())
```

```text
bob -> SuspendedEvent
ada -> ResultEvent
```

<a id="what-is-durable-and-when"></a>

## Cái gì bền vững, và khi nào

Với kho Postgres, run được ghi tại các điểm cố định, và không có gì gây tác động
ngoài run xảy ra trước khi trạng thái dẫn tới nó được ghi:

| Điểm | Cái gì đã bền vững khi qua điểm đó |
|---|---|
| Trước mỗi lời gọi model | Run cho đến lúc này và một bản ghi rằng lời gọi đã được gửi |
| Sau mỗi phản hồi của model | Message của assistant, usage và chi phí của nó |
| Trước khi mỗi tool chạy | `tool_call_start` của nó, để một lần resume không bao giờ chạy nó lần thứ hai |
| Sau khi mỗi tool trả về | `tool_call_end` của nó |
| Trước khi cuộc hội thoại được nối thêm | Checkpoint khớp với nó |
| Ở cuối | `result` (hoặc `suspended`) và trạng thái cuối cùng |

Điều đó có nghĩa gì với bạn:

- Một event bền vững chỉ đến stream của bạn sau khi nó đã được ghi. Nếu bạn đã thấy
  seq `n`, `agent.events(run_id, after_seq=n)` trả về chính xác những gì đến sau,
  kể cả từ một process khác.
- Các event chỉ có trong stream (`partial_assistant`, `tool_progress`, `hook`) đến
  ngay và không bao giờ được lưu hay replay.
- Một lời gọi model đã được gửi nhưng phản hồi chưa được ghi sẽ được gửi lại khi
  resume, nên một lần crash vào đúng lúc đó có thể khiến một lời gọi tốn hai lần.
- Một tool đã được ghi điểm bắt đầu thì không bao giờ được chạy lại khi resume
  ([ở trên](#resume)).
- Đóng khối `Server`, hoặc server chết, để mỗi run ở trạng thái bền vững cuối cùng
  của nó; run trên Postgres resume được, run trên memory thì mất.

Với kho memory, các điểm này vẫn áp dụng, nhưng các kho nằm bên trong process của
server. Một lượt tốn bao nhiêu lần ghi và chúng được gom nhóm ra sao nằm ở
[trang stores của kiến trúc](../architecture/stores.md#group-commit-write-counts).

## Các trang liên quan

- [Event](./events.md): mọi event trong stream.
- [Tool](./tools.md#phê-duyệt): vòng lặp phê duyệt và permission rule.
- [Kho lưu trữ](./stores.md): memory và Postgres.
- [Khắc phục sự cố](./troubleshooting.md#runs): lỗi của run và cách khắc phục.
