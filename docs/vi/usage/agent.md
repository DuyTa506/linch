# Agent và Session

[Bản gốc tiếng Anh](../../usage/agent.md)

Dùng trang này để cấu hình một `Agent` và quản lý vòng đời của từng `Session`.

[← Hướng dẫn sử dụng](./README.md)

`Agent` giữ cấu hình dài hạn; `Session` giữ trạng thái của một cuộc hội thoại.
Trang này trình bày các tùy chọn cấu hình định hình vòng đời của agent: nơi lưu
cuộc hội thoại, các subsystem nào đang hoạt động, system prompt, cách giữ
context trong giới hạn cửa sổ context (compaction), và cách giới hạn chi tiêu (budget).

- Việc chọn model và provider được trình bày trong [providers.md](./providers.md).
- Tool, timeout, retry, backend Bash, dependency và permission được trình bày
  trong [tools.md](./tools.md).
- Phần mở rộng dùng chung (middleware, RAG, verifier) được hợp nhất
  trong [hooks.md](./hooks.md).
- Vai trò và cấu hình theo từng run, có version, dùng `RunOptions(profile=...)`;
  xem [domain profiles](./profiles.md) để biết cách xếp lớp và resume bền vững.

<a id="tool-and-response-reliability"></a>

## `ReliabilityPolicy` — Độ tin cậy của tool và phản hồi

`Agent` dùng `ReliabilityPolicy()` theo mặc định cho mọi provider. Khi gặp
`end_turn` rỗng, envelope `<tool_call>...</tool_call>` bao trọn văn bản,
hoặc tham số không hợp lệ của tool native, Linch ghi log thông báo chẩn đoán
và yêu cầu model gửi lại. Không tool nào trong batch native bị từ chối
được chạy. Mặc định cho phép hai turn sửa lỗi trong toàn bộ run; khi hết lượt,
run kết thúc bằng kết quả lỗi. Lỗi transport và lỗi cấu trúc stream vẫn
giữ cách xử lý provider-error. Câu trả lời thông thường, lời từ chối và output
bị cắt ngắn vẫn giữ hành vi hiện có. Envelope văn bản không bao giờ được thực
thi; phần diễn giải và ví dụ trong khối code vẫn là văn bản thuần.

Khai báo thứ tự tool và yêu cầu hoàn tất khi ứng dụng cần các điều kiện đó:

```python
from linch import Agent, CompletionGate, ReliabilityPolicy, ToolPrerequisite, empty_tools

agent = Agent(
    model="gpt-5",
    tools=empty_tools(lookup_customer, update_customer),
    reliability=ReliabilityPolicy(
        prerequisites=(ToolPrerequisite("update_customer", ("lookup_customer",)),),
        completion=CompletionGate(("update_customer",)),
    ),
)
```

Dùng tên canonical đã đăng ký của tool. Các yêu cầu được kiểm tra sau khi tool
được nạp, trước lần gọi provider đầu tiên. Một prerequisite phải đã thành công
trong batch trước đó của cùng run. Các lần gọi trong một batch không thể mở khóa
lẫn nhau. Chỉ result tiền cảnh thành công sau hook mới được tính; error,
denial, interruption và xác nhận chạy nền không được tính. Final-answer
tool không thể làm bằng chứng hoàn tất. Completion gate kiểm tra mọi route cuối,
bao gồm cả câu trả lời bị loop-guard ép buộc, và dùng chung correction budget.
Các lần dừng sớm thành công rõ ràng của host thông qua `StopPredicateHook` được
ưu tiên.

Đặt `retry_invalid_responses=False` để tắt việc sửa response nhưng vẫn giữ
prerequisite và completion đã cấu hình. Đặt `max_correction_turns=0` để fail
ngay khi cần sửa, hoặc đặt `reliability=None` để tắt hoàn toàn subsystem. Một
run sạch không thêm reliability event, feedback hay provider call nào. Việc can
thiệp phát ra `ReliabilityEvent`; số correction và các tool thành công được
tham chiếu được giữ lại qua resume, còn thay đổi policy sẽ khiến resume bị từ
chối. State thuộc về từng run và không được kế thừa bởi subagent.

---

## Vòng đời Agent và Session

Tạo `Agent` một lần và tái sử dụng. Mở một `Session` mới cho mỗi cuộc hội thoại:

```python
agent = Agent(model="gpt-5", session_store=InMemorySessionStore())

# one conversation
session = await agent.session()                  # new id
async for event in session.run("first turn"):
    ...
async for event in session.run("second turn"):   # same session — full history
    ...

# a different conversation, same agent
other = await agent.session(id="user-42")        # resume/attach by id
```

`session.run(prompt)` nối thêm user message, điều khiển [turn
loop](../../architecture.md), và yield [event](./events.md). Gọi lại trên cùng
`Session` để tiếp tục thread — history vẫn tồn tại trong session store. Khi xong,
`await agent.close()` drain các session đang hoạt động và công việc nền, rồi
đóng store, provider và các hook có expose `close`/`aclose`.

Mỗi Agent cũng sở hữu một scope `Context` riêng. Scope này chứa các
service registration, event bus, tool pipeline và execution world hiệu lực của
agent; nó không phải state toàn process. Truyền `context=` để cung cấp scope
parent/host; Agent tạo và sở hữu một child scope, chỉ dispose child đó khi đóng.
Parent được cung cấp vẫn khả dụng cho host. Việc đóng agent dispose child scope
sau khi chờ công việc đang hoạt động lắng xuống. Disposal có thể retry: nếu
teardown tùy chỉnh thất bại, lần `await agent.close()` sau chỉ retry phần cleanup
chưa hoàn tất.

### Giải phóng một session

`agent.close()` dọn dẹp toàn bộ agent. Để chỉ giải phóng một cuộc hội thoại dài
hạn — giải phóng registration trong memory và drain công việc nền do nó sở hữu —
mà không đóng agent hoặc xóa history bền vững, hãy dùng API vòng đời theo session:

```python
# Release by instance or id (idempotent; unknown/already-released is a no-op).
await agent.release_session(session)
await agent.release_session("user-42")

# Or from the session itself.
await session.aclose()

# Session is also an async context manager (aclose(force=True) on exit).
async with await agent.session(id="user-42") as session:
    async for event in session.run("hello"):
        ...
```

Giải phóng một session khi có **run đang hoạt động** sẽ phát sinh lỗi, trừ khi truyền
`force=True`. Forced release hủy run, xả công việc nền do session sở hữu để
finalizer chạy, và đệ quy giải phóng các child session được giữ lại. History bền
vững trong session store luôn được bảo toàn — session đã giải phóng có thể được
gắn lại sau đó bằng id.

### Giới hạn session idle được cache

Host chạy trong thời gian dài có thể chọn tự động release các cuộc hội thoại idle:

```python
agent = Agent(model="gpt-5", session_store=store, max_idle_sessions=100)
session = await agent.session(id="user-42")
async for event in session.run("hello"):
    ...
```

`max_idle_sessions=None` (mặc định) giữ mọi session còn tồn tại cho đến khi được
release tường minh. Một số nguyên không âm giới hạn số session idle được cache;
`0` release từng session ngay khi run iterator kết thúc. Hoàn tất run và gắn lại
bằng id sẽ cập nhật thời điểm sử dụng gần nhất. Khi vượt giới hạn, Linch release các session idle
ít được dùng gần đây nhất thông qua `release_session()`, đồng thời giữ history
đã lưu. Việc mở session không tự kích hoạt eviction; giới hạn được kiểm tra sau
khi run và công việc nền được bảo vệ hoàn tất.

Run đang hoạt động, tác vụ tool tách rời, worker chạy nền, cùng notification nội
bộ chưa được chuyển đến hoặc event của child được bảo vệ và không tính vào giới hạn
idle. Parent có child đang được giữ và bận cũng được bảo vệ. Khi công việc nền
được bảo vệ hoàn tất, giới hạn được áp dụng lại; delivery đang xếp hàng vẫn còn
chờ xử lý cho đến khi run tiếp theo xả chúng. Child idle được giữ có thể release độc
lập hoặc cùng parent. Việc tiếp tục worker cần một child được giữ còn sống, vì vậy
hãy chọn giới hạn phù hợp với vòng đời worker. Gắn lại cuộc hội thoại bị evict
bằng `await agent.session(id=...)`; một instance `Session` đã release không thể
run lại.

### Điều hướng một run đang thực thi

`session.align(prompt, images=..., timeout_s=...)` đưa một user message vào
session **đang chạy** mà không hủy nó. Message được xếp hàng và xử lý tại ranh
giới turn kế tiếp — sau tool result của turn hiện tại, trước provider call kế
tiếp — phát ra `UserEvent(subtype="alignment")` và resolve lệnh gọi `align()` đang
await ngay khi đưa message vào xong. Gọi `align()` trên session idle sẽ phát sinh
`ConfigError`.

```python
run_task = asyncio.create_task(consume(session.run("start the migration")))
# ... user types while the agent works:
await session.align("skip the staging environment, go straight to prod checks")
```

**Lưu trữ bền vững.** Khi đã cấu hình `RunStore`, các alignment entry chưa được xử lý sẽ
được snapshot vào mọi run checkpoint (`RunCheckpoint.pending_alignment`) và khôi phục
bằng `resume()`, nên ý định điều hướng vẫn tồn tại qua crash. Delivery theo đúng
thứ tự và **at-least-once**: entry được xếp hàng sau lần checkpoint save cuối cùng
sẽ mất khi crash, còn entry được xử lý ngay trước crash có thể được đưa vào thêm
một lần khi resume. Resume rơi vào *giữa turn* (pending tool batch) sẽ trì hoãn
việc xử lý đến sau khi thực thi lại tool để message được đưa vào không phá vỡ thứ tự
assistant→tool-results của provider; turn được resume mà kết thúc không có thêm
provider call sẽ âm thầm bỏ các entry đã khôi phục.

---

<a id="session-store"></a>

## `SessionStore`

Session store lưu các message snapshot cần cho `SessionLog` chỉ nối thêm:
`provider_view` (hiển thị cho model) và `full_history` (audit). Cả hai là
projection deep-copy đã tách rời; sửa chúng không thay đổi session. Mở rộng
history bằng `await session.append([message])`, truyền vào một list `Message`
theo đúng thứ tự. Lệnh này ghi vào session store *và* log, nên message vẫn tồn
tại sau khi nạp lại. `session.session_log.append(...)` chỉ cập nhật log trong
bộ nhớ, không phải mutation bền vững của session. Ghi nhận compaction bằng
`session.session_log.record_projection(...)`. Output của context-builder theo
từng request là ephemeral và không bao giờ nằm trong log. Chọn ephemeral cho
worker stateless, Postgres để tồn tại qua restart.

Vì mỗi lần đọc `provider_view`/`full_history` đều dựng một deep copy tách rời,
hãy dùng accessor rẻ hơn khi chỉ cần đếm hoặc lấy turn mới nhất —
`session.message_count`, `session.last_provider_message()`, hoặc trên log là
`visible_count` / `history_count` / `last_visible()`. Với session dài, khác biệt
là copy toàn bộ cuộc hội thoại so với không copy gì.

`MessageEntry` và `ProjectionEntry` là log entry trong bộ nhớ. Ranh giới bền vững
là session snapshot, không phải journal của projection-entry được lưu lại. Khi
nạp, Linch khởi tạo một log mới từ snapshot history và visible đã lưu.

```python
import os
from linch import InMemorySessionStore
from linch.sessions.postgres import PostgresSessionStore

# Ephemeral (tests, single-request workers)
store = InMemorySessionStore()

# Persistent (keep history across restarts)
store = PostgresSessionStore(os.environ["LINCH_POSTGRES_DSN"])

agent = Agent(model="gpt-5", session_store=store)
```

`SessionStore` là một protocol, vì vậy host app có thể tự triển khai backend của
mình bằng cách đáp ứng cùng interface. Adapter bền vững tích hợp sẵn là Postgres;
adapter in-memory dành cho test và phát triển. Đối với run checkpoint bền vững
(resume qua process restart, HITL approval bền vững), cũng truyền
`run_store=PostgresRunStore(dsn)` — xem [workflows.md](./workflows.md) và
[deep-agent.md](./deep-agent.md).

### Tăng dần mức độ lưu trữ bền vững

`DurabilityOptions` bật khả năng lưu trữ bền vững tùy chọn mà không thay đổi hành
vi agent mặc định:

```python
import os
from linch import Agent, DurabilityOptions, Message, PostgresRunStore, TextBlock
from linch.sessions.postgres import PostgresSessionStore

dsn = os.environ["LINCH_POSTGRES_DSN"]

agent = Agent(
    model="gpt-5",
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
    durability=DurabilityOptions.strict_v1(),
)

session = await agent.session(id="customer-42")
message = Message(role="user", content=[TextBlock(text="New webhook received")])
await session.notify(message, delivery_id="webhook:123")
```

`durable_inbox` cung cấp việc chèn chính xác một lần vào session history theo
delivery ID (không phải effect bên ngoài chính xác một lần). `exact_model_input`
đóng băng mọi provider request đang chờ và replay chính xác sau crash. Capability
được bật sẽ fail closed nếu custom store không triển khai optional protocol bắt
buộc. In-memory store cung cấp cùng semantics trong process nhưng không sống qua
restart. Snapshot có thể chứa prompt, context và tool schema; SDK xác minh
integrity nhưng encryption và access control thuộc về host.

---

<a id="feature-flags"></a>

## `FeatureFlags`

`FeatureFlags` tắt các subsystem bạn không dùng để chúng không bao giờ kết nối
trong `session()`. Việc này giảm công việc khởi động và system prompt.

Trong Linch 3.0, các mặc định an toàn cho SDK đều là `False`: một `Agent` trống
không tìm skill/subagent của project, không kết nối tới MCP sẵn có,
và không bật virtual filesystem. Đây là cô lập resource trust có chủ ý, không
phải cấu hình coding-agent. Chỉ bật cho project đáng tin cậy hoặc dùng preset
tường minh:

```python
from linch import Agent, FeatureFlags, workspace_tools

agent = Agent(
    model="gpt-5",
    tools=workspace_tools(),
    features=FeatureFlags(
        skills=True,
        subagents=True,
        filesystem=True,
        mcp=False,  # turn on only with configured, trusted servers
    ),
)
```

```python
from linch import FeatureFlags

agent = Agent(
    model="gpt-5",
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    # also: filesystem=False to disable the virtual filesystem subsystem
)
```

Mỗi flag kiểm soát một subsystem: `skills` (tool `Skill` + nạp skill),
`subagents` (tool `Subagent`/`SubagentContinue` + registry), `mcp` (kết nối MCP
server), và `filesystem` (virtual filesystem và việc chuyển kết quả lớn từ
[filesystem.md](./filesystem.md)). `filesystem=False` tắt offload ngay
cả khi bạn truyền một backend.

---

<a id="system-prompt-control"></a>

## `SystemPromptConfig`

Theo mặc định, Linch cung cấp identity prompt không phụ thuộc domain. Prompt này
chỉ nhắc đến các tool thực sự đã đăng ký trên agent. Bạn có thể nối thêm
instruction, bỏ identity và protocol tích hợp sẵn, hoặc kết hợp các section có
thể tái sử dụng. Quy tắc coding nên nằm trong workspace preset tường minh hoặc
prompt do application sở hữu.

```python
from linch import SystemPromptConfig, SystemPromptSection

# 1. Append instructions to the built-in Linch prompt
agent = Agent(model="gpt-5", system_prompt="Always reply in formal English.")

# 2. Replace the built-in identity and protocol with your own
agent = Agent(
    model="gpt-5",
    system_prompt_config=SystemPromptConfig(
        replace_defaults=True,
        append="You are a financial analyst. Only discuss stocks and bonds.",
    ),
)

# 3. Add reusable prompt sections without replacing the defaults
agent = Agent(
    model="gpt-5",
    system_prompt_config=SystemPromptConfig(
        sections=[
            SystemPromptSection(
                name="domain-policy",
                text="When handling invoices, preserve source document IDs in every answer.",
                placement="after_defaults",
            )
        ]
    ),
)
```

Dùng chuỗi `system_prompt=` đơn giản cho trường hợp phổ biến “thêm một hoặc
hai dòng”. Dùng `SystemPromptConfig` khi cần bỏ identity và protocol tích hợp
(`replace_defaults=True`) hoặc gắn các section có tên, được kiểm soát vị trí.
Các section do tool đóng góp, metadata môi trường và phần bổ sung của caller
vẫn được kết xuất khi thay defaults.

---

<a id="compaction"></a>

## `DefaultCompaction` và `DetailedCompaction`

Khi context sau projection tiến gần window của provider, strategy compaction sẽ
tóm tắt các message cũ để giải phóng chỗ. Việc này trong suốt với caller; một
`CompactionEvent` được phát ra khi chạy.

```python
from linch import Agent, DetailedCompaction

# DefaultCompaction is the default. DetailedCompaction is opt-in and uses a
# continuation-safe summary structure for long-running sessions.
agent = Agent(model="gpt-5", compaction=DetailedCompaction())
```

### Hạch toán compaction

Trong run thông thường, lần gửi request tới provider của strategy đã cấu hình
được hạch toán như một lệnh gọi phụ. `Usage` hợp lệ của nó được cộng vào cùng
tổng tích lũy với turn chính, và biên nhận được quyết toán trước khi áp dụng bản
tóm tắt hoặc bắt đầu lệnh gọi provider khác. Nếu việc tạo bản tóm tắt chạm giới
hạn token hoặc USD, run sẽ dừng trước khi gửi turn chính. Bản tóm tắt không dùng
được (rỗng, không phải văn bản, dùng tool hoặc quá lớn) bị từ chối nhưng vẫn bị
tính phí khi đã nhận được thông tin mức sử dụng cuối cùng hợp lệ.

`DefaultCompaction` và `DetailedCompaction` có cơ chế phát lại xác định cho kết quả
đã tính phí trong run bền vững. Linch lưu bản tóm tắt đã chấp nhận với kích thước
giới hạn, định danh biên nhận và bản mô tả nguồn chỉ chứa hash theo đúng thứ tự;
sau crash, nếu nguồn đã lưu bền vững khớp, Linch có thể áp dụng bản tóm tắt đó mà
không cần gọi provider lần nữa hoặc tính phí trùng. Nội dung skill bắt buộc phải
được ghi qua session store hiện có trước khi bản mô tả đó được ghi. Nếu nguồn,
cấu hình strategy, wrapper liệt kê hoặc run contract thay đổi, hệ thống sẽ fail
closed thay vì áp dụng bản tóm tắt cũ.

Strategy tùy chỉnh nhận một provider facade có hạch toán cho các lệnh gọi thực
hiện qua Linch, bao gồm cơ chế hủy run và các bảo đảm provider-slot/limiter.
Các lệnh gọi mạng nằm ngoài facade đó không thể được quan sát; logic gồm nhiều
lệnh gọi hoặc các phép biến đổi tùy ý không tự động được bảo đảm phát lại xác định
kết quả đã tính phí.

<a id="compaction-ladder"></a>

### `CompactionLadder`

`CompactionLadder` thêm các bậc phục hồi rẻ, không dùng LLM trước và xung quanh
việc tóm tắt. Đây là opt-in — mặc định (`compaction_ladder=None`) giữ một lần
retry compaction mỗi turn. Cả hai policy dùng chung trình điều khiển phục hồi.

```python
from linch import Agent, CompactionLadder

agent = Agent(
    model="gpt-5",
    compaction_ladder=CompactionLadder(
        micro=True,                 # rung 1: LLM-free tool-result elision
        keep_recent_turns=10,       # keep the last N assistant messages and all that follow
        max_forced_compactions=3,   # per-run circuit breaker on LLM compaction
    ),
)
```

- **Micro-compact (bậc 1)** — nội dung `ToolResultBlock` cũ được thay bằng
  placeholder ngắn qua copy-on-write: không gọi LLM, giữ cặp `tool_use_id`, không
  đụng tới `full_history`. Chạy chủ động khi projection cộng output được dành trước
  đạt 80% window, và chạy phản ứng (một lần mỗi turn) khi provider phát sinh
  `ContextLengthError`. Phát `CompactionEvent(strategy="micro")`.
- **Forced compaction (bậc 2)** — strategy đã cấu hình tóm tắt như thường,
  nhưng bị giới hạn ở `max_forced_compactions` mỗi run; sau đó
  `ContextLengthError` nổi lên thay vì lặp vô hạn.
- Kiểm tra chủ động “đã giải phóng đủ chưa?” dùng `token_estimator` của agent.
  Heuristic mặc định đếm text, tool input và result, metadata, cùng ước tính cố
  định cho image. Cung cấp estimator theo model nếu cần ước tính khác. Bậc phản
  ứng thu nhỏ payload thực.

---

<a id="truncation-recovery"></a>

## `TruncationRecovery`

Khi response dạng text của model bị cắt vì chạm giới hạn output-token
(chuẩn hóa thành `stop_reason == "max_tokens"`), loop mặc định trả text bị cắt
đó làm final answer. Linch không bao giờ âm thầm tăng output cap — điều đó thay
đổi chi phí và độ trễ, và đó là quyết định policy của bạn.

`TruncationRecovery` là nút opt-in tường minh: một text turn bị cắt sẽ không
kết thúc khi vẫn còn attempt — loop đưa continuation nudge vào như một user turn
và chạy lại để model hoàn tất. Với `truncation_recovery=None` (mặc định), hành vi
là byte-identical.

```python
from linch import Agent, TruncationRecovery

agent = Agent(
    model="gpt-5",
    truncation_recovery=TruncationRecovery(
        max_attempts=2,          # continuation turns to spend per run
        # feedback="Continue ...",  # the nudge sent to the model (has a default)
    ),
)
```

Khi đã hết `max_attempts`, câu trả lời bị cắt được trả nguyên trạng, với
`ResultEvent.stop_reason == "max_tokens"` để host vẫn biết output đã bị cắt. Mỗi
continuation là một turn thông thường, vì vậy được tính vào `max_turns` và
`RunBudget` của run.

---

<a id="run-budgets"></a>

## `RunBudget`

`RunBudget` giới hạn chi tiêu — token, USD hoặc cả hai — cho một run **và các
subagent của nó** bằng cách chặn các lần gọi tiếp theo khi đã đạt giới hạn. Child
session kế thừa budget object của parent theo reference, vì vậy một cap bao phủ
toàn bộ cây agent.

```python
from linch import Agent, RunBudget
from linch import RunOptions

budget = RunBudget(max_tokens=500_000)          # and/or max_cost_usd=2.0

session = await agent.session()
async for event in session.run("do the task", RunOptions(budget=budget)):
    if event.type == "budget" and event.kind == "warning":
        print(f"90% spent: {event.spent_tokens} tokens")
    elif event.type == "budget" and event.kind == "exceeded":
        print("budget exhausted — another provider call was blocked")

print(budget.spent_tokens, budget.remaining_tokens, budget.exceeded)
```

Hành vi:

- Loop quyết toán từng lần gửi request thực tế tới provider trước khi tiếp tục.
  Việc này bao gồm cả request phụ cho compaction, nên mức sử dụng của nó được tính
  trước khi áp dụng bản tóm tắt và quyền gọi tiếp được kiểm tra lại trước turn
  chính kế tiếp. Khi budget cạn khiến lệnh gọi tiếp bị chặn, loop phát
  `BudgetEvent(kind="exceeded")`, một `ErrorEvent` tên `BudgetExceededError`, và
  `ResultEvent(subtype="error")`. Câu trả lời cuối từ lệnh gọi đã được cho phép
  vẫn có thể thành công ngay cả khi mức sử dụng của nó làm cạn budget; lệnh gọi
  sau đó bị chặn. Lịch sử session vẫn nguyên vẹn và session vẫn dùng được.
- `BudgetEvent(kind="warning")` phát một lần cho mỗi budget object khi chi tiêu
  lần đầu đạt `warn_ratio` (mặc định 0.9) của bất kỳ limit nào. Budget dùng chung
  chỉ phát cảnh báo một lần trên toàn cây, không phải một lần mỗi run.
- Thứ tự ưu tiên: `RunOptions(budget=...)` > budget kế thừa từ parent session
  (subagent) > `Agent(budget=...)`.
- Số token là tổng của mọi nhóm `Usage` từ các request chính và phụ. Chi phí USD
  lấy từ `linch.pricing`; model chưa có thông tin giá sẽ không được tự gán giá
  suy đoán, nên số token đã biết vẫn được cộng vào tổng, còn chi phí chỉ được tính
  một phần và chỉ giới hạn token mới có thể ràng buộc request đó.
- Nếu request đã gửi bị hủy hoặc thất bại trước khi có thông tin mức sử dụng
  cuối cùng hợp lệ, mức sử dụng của nó là `unknown`, không bao giờ bằng không.
  Run có giới hạn sẽ fail closed trước lệnh gọi provider khác cho đến khi host
  xử lý rào chắn hạch toán đó; run không có giới hạn có thể kết thúc với tổng
  chỉ là giới hạn dưới và `ResultEvent.usage_complete=False`.
- Đối tượng `RunBudget` đang tồn tại mang theo chủ sở hữu biên nhận và ID các request
  đã tính phí trên cây agent/subagent trong cùng tiến trình. Resume chỉ có thể
  tiếp nhận checkpoint với budget mới chưa chi tiêu; budget mới đã có chi tiêu,
  hoặc budget có giới hạn nhưng không khớp chủ sở hữu, sẽ fail closed thay vì âm
  thầm tính lại phí.

Các rào chắn về quyền sở hữu này được chủ ý giới hạn trong cùng tiến trình.
Chúng không bảo đảm khôi phục cây phân tán, tính phí provider bên ngoài chính xác
một lần, hay dành trước budget cho các request được cho phép đồng thời.

Verification retry (schema repair, [verifier hook](./hooks.md)) và subagent của
workflow đều dùng cùng budget, vì vậy gate nghiêm ngặt không thể lặp vô hạn.

---

## Giới hạn số provider call đồng thời

`RunBudget` giới hạn một run được chi bao nhiêu; `Agent(max_provider_concurrency=N)`
giới hạn số provider call đang hoạt động đồng thời của agent đó.

```python
agent = Agent(model="gpt-5.5", provider=provider, max_provider_concurrency=8)
```

Điều này giới hạn **provider call, không phải call `run()`** — phân biệt này quan
trọng vì một `run()` tách thành nhiều call: mỗi turn của loop, mỗi retry cùng
model, mỗi lần đổi model fallback, mỗi lần tóm tắt compaction và mỗi subagent
mà nó spawn. Một semaphore của host bọc quanh session run sẽ giới hạn run đồng
thời, còn cấu hình này giới hạn provider call bên trong chúng.

Giới hạn đồng thời tính theo từng `Agent`, vì vậy hai agent có gate riêng. Gate
được giữ quanh chính provider call và giải phóng trong thời gian retry backoff, nên
call đang chờ retry sẽ xếp hàng lại thay vì chiếm slot. Khi không thiết lập,
hoàn toàn không có gate và không phát sinh cost.

Để dùng chung budget giữa nhiều agent, quota theo model hoặc token bucket, hãy
truyền [`Limiter`](./extending.md#limiter--kiểm-soát-mọi-lệnh-gọi-provider-thực-tế) của
riêng bạn. Truyền đồng thời `limiter=` và `max_provider_concurrency=` sẽ phát sinh
`ConfigError`.

Phía tool có cap riêng `Agent(max_tool_concurrency=...)` — xem
[Tools](./tools.md#scheduler).

---

## Các trang liên quan

- [Providers](./providers.md) — lựa chọn model và capability.
- [Hooks](./hooks.md) — middleware, RAG, verifier, stop predicate và thiết lập OTel.
- [Workflows](./workflows.md) / [Deep agent](./deep-agent.md) —
  run bền vững và cây subagent mà budget bao phủ.
