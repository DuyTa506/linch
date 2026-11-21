# Run

[Bản gốc tiếng Anh](../../usage/runs.md)

Dùng trang này để bắt đầu, stream, resume, phê duyệt, trả lời và hủy run, và để biết điều
gì còn lại sau một lần crash.

[<- Hướng dẫn sử dụng](./README.md)

Một run là một prompt trong một session. `agent.run`, `agent.resume`,
`agent.approve` và `agent.answer` đều trả về một `Run` ngay khi server chấp nhận lời gọi; các event
của run sau đó được stream về dưới dạng `Envelope`.

| Lời gọi | Làm gì |
|---|---|
| `await agent.run(session_id, prompt, *, run_id=None, principal=None, trace_id=None, output_schema=None, tool_choice=None)` | Bắt đầu một run; session được tạo nếu chưa có. `trace_id` là của bạn, được lặp lại trên các event `init` và `result` của run ([Observability](./observability.md#correlate-logs-and-traces)). `output_schema` và `tool_choice` thay thế của agent cho run này ([Structured output](./structured-output.md#cho-một-run-schema-riêng)) |
| `await agent.resume(run_id, *, principal=None)` | Tiếp tục một run sau crash hoặc sau khi stream bị rớt |
| `await agent.approve(run_id, decisions, *, principal=None)` | Trả lời các lời gọi đang chờ của một run bị tạm dừng và tiếp tục nó |
| `await agent.answer(run_id, answers, *, principal=None)` | Trả lời các câu hỏi `AskUser` của một run bị tạm dừng và tiếp tục nó |
| `async for envelope in run` | Mọi envelope của lời gọi này, theo thứ tự, kết thúc sau `result` hoặc `suspended` |
| `await run.result()` | Đọc phần còn lại và trả về `ResultEvent` hoặc `SuspendedEvent` |
| `await run.cancel()` | Hủy run, đang chạy hay bị tạm dừng; `True` khi nó đã bị hủy |
| `await agent.steer(run_id, text, *, steer_id=None, principal=None)` | Gửi văn bản cho một run đang chạy; nó đọc văn bản ở ranh giới lượt kế tiếp ([bên dưới](#steer)) |
| `await run.steer(text, *, steer_id=None)` | Tương tự, cho run này |
| `await agent.cancel(run_id)` | Hủy một run theo id, kể cả run bị tạm dừng, từ bất kỳ tiến trình nào ([bên dưới](#cancel-a-suspended-run)) |
| `await agent.events(run_id, after_seq=0)` | Các envelope bền vững của run sau `after_seq`, lấy từ các kho lưu trữ; run dài được đọc từng trang ([bên dưới](#run-dài-được-đọc-từng-trang)) |
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
  một lần phê duyệt (status `awaiting_approval`) hoặc một câu trả lời (status
  `awaiting_input`). `result.subtype` là `success`, `error`, `aborted` hoặc
  `interrupted`.
- Bạn có thể lặp qua một phần của run rồi gọi `result()` cho phần còn lại. Ngừng
  lặp không dừng run: server vẫn tiếp tục điều khiển nó và các envelope của nó chờ
  trong bộ nhớ. Hãy hủy nó nếu bạn không còn cần.
- **Hãy chọn run id** khi bạn cần tìm lại run ngay cả khi `run()` chưa bao giờ trả
  về (timeout, process của bạn bị crash). Server tự chọn một UUID khi bạn không
  chọn. Một id đã được lưu sẽ kết thúc run bằng `RunExists` (code -32023) và không
  ghi gì.
- **Một run id đã được xác nhận là bền vững.** `run()` chỉ trả về khi run đã được
  lưu (với Postgres: khi lệnh create của nó đã commit), nên id nó đưa cho bạn vẫn
  tồn tại sau mọi crash xảy ra tiếp theo: `resume` tìm thấy nó và `agent.events`
  không bao giờ báo `RunNotFound` cho nó. Cho đến khi `run()` trả về, run có thể
  tồn tại hoặc không (create có thể commit trong lúc câu trả lời đang trên đường,
  hoặc sau khi process của bạn đã ngừng chờ): hãy `resume` id bạn đã chọn, thử lại
  `RunNotFound` trong giây lát, và chỉ khởi động lại với cùng id nếu nó vẫn thiếu
  (`RunExists` khi đó nghĩa là nó đã được ghi trong lúc ấy). `run_or_resume`
  ([Workflows](./workflows.md#run_or_resume)) làm nửa sau cho một job được thử lại:
  nó start với id rồi resume khi gặp `RunExists`. Một lần start bị từ chối
  (session bận, prompt bị chặn) vẫn trả về, và sự từ chối đến ở cuối stream.
  `resume`, `approve` và `answer` trả về ngay.
- Nhiều run của một server stream cùng lúc; mỗi `Run` chỉ nhận envelope của chính
  nó.

<a id="one-live-run-per-session"></a>

### Một run đang chạy cho mỗi session

Một session có nhiều nhất một run chưa kết thúc: một run đang chạy hoặc đang chờ
phê duyệt hay câu trả lời. Bắt đầu một run khác sẽ kết thúc run mới bằng `SessionBusy` (code
-32030) trước khi bất kỳ thứ gì được ghi; `busy.session_id` và `busy.run_id` nêu
tên run đang chạy. Hãy resume, phê duyệt, trả lời hoặc hủy run đó trước, hoặc xếp hàng
prompt. Với Postgres, quy tắc này đúng trên mọi server dùng chung database.

## Lỗi đến ở cuối stream

`run()` trả về khi run đã được lưu hoặc bị từ chối; `resume()`, `approve()` và `answer()` trả về trước khi
server làm gì ngoài việc chấp nhận lời gọi. Một sự từ chối (session đang bận, run không xác định, contract khác
nhau) và một lỗi khi tiếp tục bền vững sẽ đến dưới dạng phần cuối của stream: lặp
hoặc gọi `result()` sẽ raise một subclass của `RunError`.

| Exception | Code | Khi nào |
|---|---|---|
| `RunNotFound` | -32021 | Không có run đó trong các kho lưu trữ của agent (với kho memory: không có trong handle này) |
| `EventTooLarge` | -32051 | `agent.events` gặp một envelope quá dài cho một message; `error.events` giữ những gì đã đọc trước nó và `error.seq` nêu tên nó ([bên dưới](#run-dài-được-đọc-từng-trang)). `EventsIncomplete` là lớp cơ sở của nó: nó cũng kết thúc một lần đọc có con trỏ trang không tiến lên |
| `RunPruned` | -32050 | Run từng tồn tại và [retention](./stores.md#retention) của server đã xóa nó; lớp con của `RunNotFound`, `run_id` nêu tên nó. Hết thời hạn tombstone thì nó đọc ra là `RunNotFound` |
| `RunExists` | -32023 | `run_id` đã được lưu; `run_or_resume` xử lý nó ([Workflows](./workflows.md#run_or_resume)) |
| `ContractMismatch` | -32025 | Run đã bắt đầu dưới một contract khác ([Config của agent](./agent.md#hợp-đồng-của-run)); `data["expected"]` là contract của run và `data["found"]` là của agent này, như thông báo nêu |
| `RunTerminal` | -32026 | Run đã kết thúc; `data.status` là trạng thái của nó |
| `NotSuspended` | -32027 | `approve` trên một run không đang chờ phê duyệt, hoặc `answer` trên một run không đang chờ câu trả lời ([bên dưới](#approve)) |
| `PromptBlocked` | -32029 | Một callback `user_prompt_submit` đã từ chối prompt; `.reason` cho biết lý do. Không có run hay session nào được tạo |
| `SessionBusy` | -32030 | Session có một run đang chạy |
| `InvalidOutputSchema` | -32031 | `output_schema` của run không compile được hoặc root của nó không phải object; `.reason` nêu lý do. Không có run hay session nào được tạo |
| `InvalidToolChoice` | -32034 | `tool_choice` của run nêu một tool mà run không đưa ra, hoặc provider từ chối nó; `.reason` nêu lý do. Không có run hay session nào được tạo |
| `InvalidAnswer` | -32032 | Các câu trả lời không khớp với các câu hỏi `AskUser` đang chờ; `.reason` nêu lý do. Không có gì được ghi: run vẫn chờ ([bên dưới](#answer)) |
| `InvalidSteer` | -32038 | Văn bản của steer để trống hoặc dài hơn 16 KiB, hoặc id của nó không dùng được; `.reason` nêu lý do. Không có gì được ghi |
| `RunLeased` | -32033 | Một server khác đang điều khiển run (Postgres); `.owner` và `.expires_in` (giây) cho biết lease của ai và còn bao lâu. Không có gì được ghi ([Kho lưu trữ](./stores.md#run-leases)) |
| `LeaseLost` | -32022 | Một server khác đã tiếp quản run, hoặc đã fail nó, trong khi server này điều khiển; run dừng ở đây mà không ghi gì. Một lần ghi thấy session dài hơn lúc run này để lại (các entry của bên điều khiển mới) sau một lần tiếp quản cũng báo mã này, không phải `RunExists` |
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
thấy nó. Nó trả về `False`, không phải lỗi, khi không còn gì để hủy: run đã kết
thúc. Một run bị abort là đã kết thúc: session của nó nhận được run mới. Chỉ server
đang điều khiển một run đang chạy mới hủy được nó; một run bị tạm dừng có thể được
hủy từ bất kỳ đâu ([bên dưới](#cancel-a-suspended-run)).

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

<a id="cancel-a-suspended-run"></a>

### Hủy một run bị tạm dừng

Một run đang chờ một approval hoặc một câu trả lời không có stream: server không
điều khiển nó. `agent.cancel(run_id)`, hoặc `cancel()` trên `Run` có stream kết thúc
bằng `suspended`, kết thúc nó thông qua các kho lưu trữ của agent thay vì approve,
deny hay answer nó:

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, RunTerminal, Server, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="write")
async def deploy(service: str) -> str:
    """Deploy a service."""
    return "deployed"


config: AgentConfig = {
    "namespace": "runs-demo",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("c1", "deploy", {"service": "api"}), text_turn("Nothing deployed.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[deploy])
        run = await agent.run("s1", "Deploy api")
        assert isinstance(await run.result(), SuspendedEvent)

        print("cancelled:", await agent.cancel(run.id))
        events = [e.event for e in await agent.events(run.id)]
        print(events[-3].result["content"][0]["text"], "/", events[-1].subtype)
        try:
            await (await agent.approve(run.id, {})).result()
        except RunTerminal as late:
            print("late approve:", late.data["status"])
        print("cancel again:", await agent.cancel(run.id))

        # The session goes on: the next run sees the call answered.
        result = await (await agent.run("s1", "What happened?")).result()
        assert isinstance(result, ResultEvent)
        print(result.final_text)


asyncio.run(main())
```

```text
cancelled: True
Tool call aborted / aborted
late approve: aborted
cancel again: False
Nothing deployed.
```

- Mọi lời gọi của response chưa có kết quả đều nhận một kết quả lỗi,
  `Tool call aborted` (cả một câu hỏi `AskUser` cũng vậy; một lời gọi đã bắt đầu
  trước một lần crash giữ placeholder bị gián đoạn của nó), rồi run kết thúc bằng
  một `result` có subtype `aborted` và trạng thái `aborted`. Các kết quả được đưa
  vào session, nên model của một run sau đó trong session thấy từng lời gọi đã được
  trả lời.
- Một `approve` hoặc `answer` về sau kết thúc bằng `RunTerminal`; một lần cancel khác
  trả về `False`.
- **Postgres**: cancel có thể đến từ một tiến trình khác, hoặc sau khi server đã
  suspend run khởi động lại. Một run mà server khác đang điều khiển ngay lúc đó (một
  approve đến trước) không bị lấy khỏi nó: cancel trả về `False` khi approve đó đã
  commit các quyết định của nó, hoặc raise `RunLeased` khi approve mới chỉ claim run.
- Một cancel và một approve (hoặc answer) chạy đua để lại một bên thắng: bên claim
  run sau cùng loại bên kia ra. Một cancel thua trả về `False` hoặc raise
  `LeaseLost`; một approve thua kết thúc bằng `RunTerminal` hoặc `LeaseLost`.
- Cancel bền vững từ lần ghi đầu tiên: nếu server chết giữa chừng, `resume` hoặc một
  `cancel` khác sẽ hoàn tất nó, còn một `approve` nhận `NotSuspended`.
- Contract của run không được kiểm tra, nên một run của một config cũ hơn vẫn có thể
  bị hủy.

## Resume

`resume(run_id)` tiếp tục một run từ trạng thái bền vững cuối cùng của nó. Dùng nó
sau khi process của bạn hoặc server bị crash, sau khi stream bị rớt, hoặc để nhận
một run mà process khác đã bắt đầu. Một run đang chờ phê duyệt hoặc câu trả lời sẽ tạm dừng lại
ngay. Chỉ resume sau khi stream trước đó đã kết thúc (việc lặp của nó đã xong hoặc
đã raise); trong khi một server còn đang điều khiển run, `resume` sẽ là `RunBusy`.

Handle nào resume được phụ thuộc vào các kho lưu trữ:

- **Kho memory**: run nằm trong agent handle đã bắt đầu nó. Hãy resume và phê duyệt
  hoặc trả lời nó qua chính `Agent` đó; nó mất khi server dừng.
- **Kho Postgres**: bất kỳ agent nào được mở với cùng config và tool (nên cùng
  contract), trên bất kỳ server nào, đều resume được, nhưng không phải khi một
  server khác đang điều khiển nó: lần resume đó bị từ chối với `RunLeased`. Một
  server đã crash giữ các run của nó cho đến khi lease hết hạn (mặc định 30 s, xem
  [Run leases](./stores.md#run-leases)); hãy thử lại sau `.expires_in` giây:

```python
from linch_rust import RunLeased


async def resume_when_free(agent, run_id):
    while True:
        run = await agent.resume(run_id)
        try:
            return await run.result()
        except RunLeased as leased:
            await asyncio.sleep(leased.expires_in or 1)
```

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
Tool execution was interrupted before a result was recorded; its effect is unknown, and it is not re-run automatically on resume.
```

Một lời gọi tool đã được ghi điểm bắt đầu nhưng chưa ghi điểm kết thúc sẽ không bao
giờ được chạy lại: model nhận một kết quả lỗi nói rằng nó đã bị gián đoạn, và tự
quyết định phải làm gì. Dù vậy, hãy làm cho các host tool có thay đổi trạng thái an
toàn khi gọi hai lần, hoặc kiểm tra trước khi hành động, vì crash có thể xảy ra
sau tác động và trước khi ghi bản ghi.

<a id="tools-run-at-most-once-never-exactly-once"></a>

### Tool chạy tối đa một lần, không bao giờ đúng một lần

Một run được resume cho mỗi lời gọi tool một trong ba kết cục, tùy điều gì đã bền
vững khi process chết:

| Bền vững lúc crash | Một lần resume làm gì | Tác động của tool |
|---|---|---|
| Không có start lẫn end | Lập kế hoạch lại lời gọi và chạy nó (quyền được hỏi lại trừ khi một quyết định đã ghi bao phủ nó) | Xảy ra một lần, bây giờ |
| Có start, chưa có end | Không bao giờ chạy nó. Nó ghi `tool_call_end` của lời gọi như một lỗi với `error_code` là `tool_interrupted`, và model đọc kết quả lỗi `Tool execution was interrupted before a result was recorded; its effect is unknown, and it is not re-run automatically on resume.` cho `tool_use_id` đó, nên transcript vẫn khớp cặp với các provider đòi một kết quả cho mỗi tool use | Đã xảy ra (crash đến sau nó, trước khi end được ghi) hoặc chưa (crash đến trước nó): không biết |
| Cả hai | Dùng lại kết quả đã ghi | Đã xảy ra một lần |

Vì vậy một tool chạy tối đa một lần cho mỗi lời gọi, và linch không bao giờ tuyên
bố đúng một lần: khoảng giữa tác động và lần ghi end của nó không thể đóng lại từ
bên ngoài tool. Host đóng nó trong các tool của chính mình:

- Dùng `ctx.idempotency_key` (`run_id:tool_use_id`, trường của `ToolContext` trong
  [Tools](./tools.md#tool-đang-phục-vụ-lời-gọi-nào)) làm khóa idempotency của tác
  động. `tool_use_id` là của provider và giữ nguyên sau resume, nên một side effect
  được khóa bằng nó chỉ xảy ra một lần dù lời gọi được thử bao nhiêu lần. Điều này
  đúng với các host tool bạn viết. Execution host từ xa nhận cùng khóa đó trong
  `exec.start` (`ExecStart.idempotency_key`, xem
  [Thực thi từ xa](./remote-execution.md#sau-crash-khóa-cho-phép-host-khử-trùng-lặp)) và có
  thể khử trùng lặp theo nó. Các backend khác không có host để khử trùng lặp (trang đó
  cũng nêu mỗi backend đảm bảo gì sau crash), nên một lệnh `local`, `sandbox` hay
  `docker` có side effect chỉ chạy tối đa một lần mà không có khử trùng lặp: hãy làm
  cho chính lệnh đó idempotent, hoặc kiểm tra tác động của nó trước khi chạy lại.
- Tìm các lời gọi cần đối soát bằng các event `tool_call_end` có `error_code` là
  `tool_interrupted`, và hỏi hệ thống mà tool nói chuyện cùng xem khóa đã được áp
  dụng chưa.
- Yêu cầu model kiểm tra trước khi hành động lại: kết quả lỗi của nó đã nói tác
  động là không rõ.
- Sau khi mất kết nối, hãy `resume` run trước (thử lại `RunLeased` như trên); chỉ
  gửi lại `run` với cùng `run_id` khi run không tồn tại (`RunNotFound` vẫn còn),
  không bao giờ để lặp lại một run đã tồn tại.

```python
import asyncio

from linch_rust import Agent, ToolCallEndEvent, ToolContext, tool

charged: dict[str, str] = {}  # your payment system's idempotency records


@tool(scope="write")
async def charge(order: str, ctx: ToolContext) -> str:
    """Charge an order once, however often the call is attempted."""
    key = ctx.idempotency_key
    if key not in charged:  # the real check is the payment API's own key
        charged[key] = f"receipt-{order}"
    return charged[key]


async def interrupted_calls(agent: Agent, run_id: str) -> list[str]:
    """The tool_use_ids whose effect a crash left unknown, to reconcile."""
    ends = [e.event for e in await agent.events(run_id)]
    return [
        end.tool_use_id
        for end in ends
        if isinstance(end, ToolCallEndEvent) and end.error_code == "tool_interrupted"
    ]
```

`charged.get(f"{run_id}:{tool_use_id}")` khi đó cho biết tác động đã xảy ra chưa.

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

## Answer

`answer(run_id, answers)` nhận một `Answer(...)` hoặc `Answer.declined(note)`
cho mỗi `request_id` của các event `input_request` (`pending_request_ids` của event
`suspended`); một request bị bỏ sót sẽ được hỏi lại. Cách hỏi và những gì model đọc
nằm ở [Tool](./tools.md#hỏi-người-dùng).

Nó theo các quy tắc của approve: chỉ một run đang chờ câu trả lời mới nhận một câu
trả lời; câu trả lời được ghi thành kết quả của lời gọi trước khi run tiếp tục, nên
cùng câu trả lời đó gửi lại sau một crash sẽ kết thúc bằng `NotSuspended` và không
ghi gì, và `resume` tiếp tục run với câu trả lời đã commit. Một câu trả lời cho run
đã kết thúc là `RunTerminal`. Với kho Postgres, câu trả lời có thể đến từ một
process khác hoặc một ngày sau.

## Steer

`agent.steer(run_id, text)` (hoặc `run.steer(text)`) gửi văn bản cho một run trong
lúc nó làm việc, giống như một người gõ vào cuộc hội thoại. Nó trả về id của steer.

```python
steer_id = await agent.steer(run.id, "Use tabs, not spaces.")
```

- **Khi nào model đọc nó.** Văn bản là một message user riêng. Nó được ghi vào log
  sau các kết quả tool của batch mà run đang ở trong và trước lời gọi model kế
  tiếp, nên thứ tự assistant rồi đến kết quả mà các provider cần không bao giờ bị
  phá vỡ. Khi model trả lời mà không gọi tool, một steer đang chờ được ghi vào log
  trước khi run kết thúc, và run sẽ chạy thêm một lượt thay vì kết thúc. Một event
  `user` có `subtype` là `alignment` và `steer_id` bằng id được trả về báo việc đã
  giao; nó cũng có trong `agent.events`.
- **Cái gì được chấp nhận.** Một run đang chạy, hoặc bị tạm dừng chờ phê duyệt hay
  chờ câu trả lời. Một run bị tạm dừng giữ steer và đọc nó khi bạn approve hoặc
  answer; nó vẫn ở trạng thái tạm dừng. Một run đã kết thúc bị từ chối bằng
  `RunTerminal`, một run không xác định bằng `RunNotFound`. Một run nhàn rỗi
  (đang xếp hàng, hoặc `running` mà không server nào lái, sau một crash hay một
  stream bị bỏ) bị từ chối bằng `InvalidSteer`: hãy resume nó trước, rồi mới
  steer.
- **Bền vững trước khi trả lời.** Steer là một event `steer_received` trong
  journal riêng của run (chỉ có trong journal: `agent.events` không bao giờ liệt kê
  nó). Lời gọi trả về sau khi nó được ghi. Một run do server điều khiển sẽ ghi nó ở
  bước kế tiếp, nên câu trả lời chờ lời gọi model hoặc batch tool đang chạy, có thể
  là vài phút sau một tool chạy lâu; hãy đặt timeout phía bạn như với mọi request.
  Với kho Postgres, một lần crash không làm mất steer đã được trả lời, và một run
  được resume đọc lại từ journal những gì còn đang chờ: một steer chỉ được ghi vào
  log một lần, dù lần crash cắt ngang lần ghi nào. Với kho memory không có gì sống
  sót qua process, nên steer cũng vậy.
- **Một steer thất bại sau khi claim.** Một run bị tạm dừng được claim để ghi rồi trả lại
  trạng thái cũ. Nếu lần ghi trả lại đó thất bại, run có thể hiển thị `running` dù nó vẫn
  đang tạm dừng và không ai điều khiển nó. Nó không bị kẹt: `approve`, `answer`, `cancel`
  và `resume` mỗi cái vẫn kết thúc hoặc tiếp tục được nó, và một steer gửi lại với cùng id
  chỉ được nhận một lần.
- **Một lời gọi được gửi lại.** `steer_id` đặt tên cho steer (chữ cái, chữ số và
  `-_.:`, tối đa 128 byte). Gửi lại với cùng id sau khi câu trả lời bị rớt, steer
  chỉ được nhận một lần. Một lời gọi thất bại vì run đã dừng trước đó (`RunExists`,
  -32023) có thể đã hoặc chưa được ghi; hãy gửi lại với cùng id.
- **Các server khác.** Một run do server khác điều khiển không được định tuyến tới:
  lời gọi nhận `RunLeased` (kèm `expires_in`), như `approve`, và run có server đã
  crash mà lease chưa hết hạn cũng vậy.
- **Giới hạn.** Tối đa 16 KiB văn bản, không để trống (`InvalidSteer`, -32038,
  `reason`), và tối đa 16 steer mà run chưa đọc, tính cả những steer còn đang chờ
  bước kế tiếp của nó (`LimitReached`, `kind` là `steers`): quá 16 thì lời gọi bị từ
  chối ngay, nên các steer đang chờ không bao giờ chiếm chỗ request của kết nối và
  một `cancel` trên nó luôn được đọc. Một tenant chỉ steer các run của agent của chính nó: run id của
  namespace khác là `RunNotFound`.
- **Một cancel khi có steer đang chờ.** Một steer là bền vững trước khi lời gọi của nó
  được trả lời, nên một run bị cancel khi còn steer đang chờ vẫn giữ chúng: các steer
  đang chờ được ghi vào journal và vẫn trả lời bằng id của chúng (đã đo: 16 steer đang chờ,
  16 hàng `steer_received`, 16 câu trả lời), và mỗi steer xuất hiện như một event
  `alignment` trong run đã aborted. Run kế tiếp của session thấy chúng như một phần của
  cuộc hội thoại, nên một steer gửi trước cancel không bị mất, và steer bạn không muốn
  giữ phải được sửa bằng prompt kế tiếp.
- **Kiểm tra prompt.** Việc redact prompt và các callback `user_prompt_submit` xem
  một steer như một prompt, với `source` là `"steer"` trong request của callback.
  Một steer bị callback chặn sẽ bị bỏ; các bản ghi `hook` của nó được stream.

Khác với `session.align` của Python Linch ở chỗ: lời gọi trả lời khi steer đã bền
vững, không phải khi nó đã được ghi vào log; văn bản không bao giờ bị tách khỏi một
message kết quả tool vì steer chờ cả batch; một steer đến trong lúc model đang trả
lời không bị run làm mất; và `images` không được nhận.

## Hành động thay cho một principal

`principal` cho biết một run hành động thay cho ai: `{"tenant", "user", "roles"?}`
(typed dict `Principal`). Các permission rule có `role` hoặc `unless_role` so khớp
với các role của nó ([Tool](./tools.md#permission-rule)), và request của một
callback `permission_request` mang theo nó. Hãy truyền nó cho `run`: run giữ lại
nó, và mỗi `resume`, `approve` và `answer` không nêu principal nào sẽ hành động
thay cho nó, kể cả sau một sự cố hoặc từ một process khác. Lời gọi nêu một
principal thì hành động thay cho principal đó. Trên một server từ xa, `tenant`
phải là tenant của kết nối ([Server](./server.md#tenants-and-namespaces)).

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
| Sau mỗi phản hồi của model, trước các callback của nó (chỉ khi có một callback `after_provider_call` được gắn) | Usage và chi phí của phản hồi, không kèm message |
| Khi `run()` trả về | Chính run (lệnh create của nó), nên run id bạn đang giữ tồn tại sau mọi crash |
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
- Khi có một callback `after_provider_call` được gắn, một lần crash trong lúc một callback
  trên phản hồi chạy (kể cả `provider_call_stop`) vẫn giữ usage và chi phí của lời gọi đã trả phí: run được
  resume tính chúng vào các tổng của `result` và budget, hiện chúng trong một event
  `usage`, và gọi lại model (một `model_call_receipt` mới của cùng turn, không phải một
  lần thử lại). Bản thân message chỉ được ghi sau các callback, nên một callback redact
  nó không bao giờ bị bỏ qua.
- Một tool đã được ghi điểm bắt đầu thì không bao giờ được chạy lại khi resume:
  tối đa một lần, không bao giờ đúng một lần
  ([ở trên](#tools-run-at-most-once-never-exactly-once)).
- Một steer được ghi ở ranh giới bước, trước khi lời gọi của nó trả lời; một run được resume đọc lại những steer còn đang chờ ([Steer](#steer)).
- Đóng khối `Server`, hoặc server chết, để mỗi run ở trạng thái bền vững cuối cùng
  của nó; run trên Postgres resume được, run trên memory thì mất.

Với kho memory, các điểm này vẫn áp dụng, nhưng các kho nằm bên trong process của
server. Một lượt tốn bao nhiêu lần ghi và chúng được gom nhóm ra sao nằm ở
[trang stores của kiến trúc](../architecture/stores.md#group-commit-write-counts).

### Run dài được đọc từng trang

`agent.events` trả về cả run: nó xin theo trang (`max_bytes` 8 MiB) và tự đi theo các trang đó, khi server có capability `run_events_paging` (`server.info.has("run_events_paging")`). Server không có nó không biết `max_bytes` và từ chối trường đó, nên với server ấy `agent.events` không xin trang nào và đọc cả run trong một lần gọi, đúng như server đó trả lời. Trên wire, `run.events` trả lời
từng message, và một message chứa tối đa `limits.max_message_bytes` của `server.info` (16 MiB)
tính cả khung. Một request nhận được:

- **Không có `limit` lẫn `max_bytes`** (những gì một client cũ gửi): mọi envelope sau `after_seq`
  trong một câu trả lời khi chúng vừa một message (16 MiB trừ 128 byte khung), như trước đây; khi
  không vừa thì là một lỗi, `-32052`, và không có gì được trả lời: không bao giờ là một trang đầu
  cùng một cursor. Vì vậy một client không biết `next_after_seq` nhận cả run hoặc một lỗi, và
  không có event nào mất đi mà không có dấu hiệu. `data.seq` là envelope đầu tiên không vừa, và
  thông báo nói hãy hỏi lại với `max_bytes` hoặc `limit`, thứ chia trang kèm cursor.
- **`limit`, `max_bytes` hoặc cả hai**: tối đa `limit` envelope (1000 khi không có) và
  `max_bytes` byte đã mã hóa (8 MiB khi không có). Giá trị vượt mức tối đa (10000 envelope,
  8 MiB) bị hạ xuống mức đó, dù lớn đến đâu; 0 là `-32602`. Envelope đầu của một trang luôn được
  trả về, nên người đọc luôn tiến lên được kể cả khi một envelope dài hơn `max_bytes`.

Khi còn envelope phía sau, câu trả lời có `next_after_seq`, seq của envelope cuối cùng của nó:
hỏi lại với giá trị đó làm `after_seq`. Trang cuối không có `next_after_seq`. Client Python dừng
bằng `EventsIncomplete` nếu `next_after_seq` của một trang không tiến lên.

Một envelope không vừa một message cùng khung của nó (16 MiB trừ khoảng 130 byte) bị từ chối với
`-32051` (`EventTooLarge`, một `RunError`); `data.seq` nêu tên nó, và thông báo nói cách làm:
hỏi lại với `after_seq` bằng seq đó để đi tiếp sau nó. `agent.events` raise `EventTooLarge` kèm
các envelope đã đọc trong `error.events` và seq trong `error.seq`:

```python
try:
    events = await agent.events(run_id)
except EventTooLarge as error:
    events = error.events                       # mọi thứ trước envelope đó
    events += await agent.events(run_id, after_seq=error.seq)   # và sau nó
```

Server vẫn đọc từ các kho lưu trữ phần event còn lại của run cho mỗi trang; trang giới hạn những
gì được gửi và mã hóa, không giới hạn lần đọc đó.

## Các trang liên quan

- [Event](./events.md): mọi event trong stream.
- [Tool](./tools.md#phê-duyệt): vòng lặp phê duyệt và permission rule.
- [Tool](./tools.md#hỏi-người-dùng): `AskUser` và các câu trả lời của nó.
- [Kho lưu trữ](./stores.md): memory và Postgres.
- [Khắc phục sự cố](./troubleshooting.md#runs): lỗi của run và cách khắc phục.
