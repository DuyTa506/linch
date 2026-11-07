# Các hệ thống con
[Bản gốc tiếng Anh](../../architecture/subsystems.md)

> Dùng trang này khi bạn cần hợp đồng của một hệ thống con, ranh giới thực thi hoặc
> bất biến xuyên suốt nhiều thành phần.

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Trang này mô tả runtime Linch 3.1 hiện tại. Kernel không có giao diện và
không phụ thuộc provider; host cung cấp vòng đời tiến trình, lớp truyền tải
HTTP hoặc RPC, các dịch vụ tenant và OpenTelemetry SDK.

## 3.1 Kernel và các dịch vụ theo phạm vi

`linch.kernel` là một kernel nhỏ cho dịch vụ và hiệu ứng, không có dependency.
Mỗi `Agent` sở hữu một phạm vi `Context` riêng, chứa registry dịch vụ,
bus event và các hiệu ứng có thể hoàn tác. Khi được cung cấp một `Context` của host,
phạm vi đó là phạm vi con, còn phạm vi cha của host vẫn thuộc quyền sở hữu của host.
Các handle `Disposable` hoàn tác một đăng ký và có tính lũy đẳng. Các thành phần
cơ sở này cũng được dùng để đăng ký tool, hook, listener dừng lượt và adapter tùy chọn.

Kernel không có registry khả biến dùng chung toàn tiến trình. Host có thể tạo nhiều
agent trong một tiến trình; mỗi agent có các tool, hook, session, bộ máy phân quyền,
cấu hình run và cơ chế phân giải telemetry riêng. `Context` cha được cung cấp chia sẻ
các dịch vụ của host thông qua phạm vi con, nên việc đóng agent không giải phóng
phạm vi của host.

Xem [kernel.md](./kernel.md) để biết hợp đồng của `Context`, `Disposable`, phạm vi
hiệu ứng và bus event.

## 3.2 Session, các phép chiếu và giai đoạn của lượt

`Session` giữ trạng thái cho từng cuộc hội thoại. `SessionLog` của nó là nguồn
duy nhất, chỉ cho phép ghi nối tiếp, cho hai phép chiếu đọc:

- `full_history` là phép chiếu phục vụ kiểm toán;
- `provider_view` là phép chiếu mà mô hình nhìn thấy sau quá trình compaction được ghi log.

Ngữ cảnh cho từng yêu cầu từ `ContextInjectionHook` và các bộ dựng RAG chỉ được
hợp nhất vào `ProviderRequest`. Ngữ cảnh này không được nối vào phép chiếu nào.
Mở rộng lịch sử bằng `await session.append([...])`; phương thức nhận một
`list[Message]` có thứ tự. Gọi trực tiếp
`SessionLog.append(...)` chỉ thay đổi dữ liệu trong bộ nhớ và sẽ mất khi tải lại.
Compaction dùng `session.session_log.record_projection(...)`, lưu thay đổi của
phép chiếu vào log trong khi vẫn giữ nguyên lịch sử kiểm toán.

Vòng lặp được chia theo trách nhiệm:

1. `loop/startup.py` nối prompt và khôi phục trạng thái có thể lưu checkpoint.
2. `loop/provider_turn.py` dựng ngữ cảnh và yêu cầu gửi provider, streaming phản hồi
   provider đã chuẩn hóa và nối thông điệp assistant được chấp nhận.
3. `loop/tool_batch.py` kiểm tra tính hợp lệ và thực thi một lô tool của assistant,
   nối các kết quả tool và đánh giá `LoopGuard`.
4. `loop/finalize.py` xử lý văn bản cuối cùng và đầu ra của tool kết thúc.
5. `loop/turn_stopping.py` chạy điểm quyết định kết thúc tuần tự trước khi kết quả
   kết thúc hoặc xác nhận của tool cuối cùng được commit.
6. `loop/checkpoint.py` và `loop/runtime.py` lưu bền vững các giai đoạn, event,
   biên nhận gọi mô hình, trạng thái extension và con trỏ resume.

```mermaid
sequenceDiagram
    participant Caller as Host caller
    participant S as Session
    participant R as RunStore
    participant P as Provider
    participant T as Scheduler

    Caller->>S: run(prompt) or resume(run_id)
    S->>R: create/load run + initial checkpoint
    loop each turn
        S->>R: checkpoint provider_pending
        S->>P: stream ProviderRequest
        P-->>S: normalized text/tool events
        alt tool calls
            Note over S,R: tool_batch_pending is held, but exact-input runs write it at once
            S->>R: commit tool_executing + pending events
            S->>T: resolve, authorize, execute tools
            T-->>S: ToolCallStart/End events
            S->>R: append tool results + checkpoint
        else terminal candidate
            S->>S: dispatch TurnStopping
            S->>R: append result + completed checkpoint
        end
        S-->>Caller: typed event stream
    end
```

Khi run được lưu bền vững, checkpoint được ghi trước thao tác có kết quả cần
khôi phục. Vì vậy, tích hợp tool nhận một `ToolContext.idempotency_key` ổn định
(`run_id:tool_use_id`) và phải đối soát tác động phụ bên ngoài nếu tiến trình gặp
sự cố sau khi tác động phụ xảy ra nhưng trước event hoàn tất. Linch cung cấp khả
năng khôi phục ít nhất một lần, không bảo đảm tác động phụ bên ngoài xảy ra đúng một lần.

`Agent(max_idle_sessions=N)` là giới hạn LRU tùy chọn cho các session nhàn rỗi.
Các run đang hoạt động, thông báo đang chờ và công việc nền còn chạy bảo vệ cây
sở hữu của chúng khỏi bị loại bỏ.

## 3.3 Run bền vững, lease và checkpoint

Protocol `RunStore` bắt buộc nằm trong `run_store.py`. Nó lưu bền vững
`RunRecord`, `RunCheckpoint`, trạng thái và luồng event có thứ tự cho từng run.
Các khả năng tùy chọn được phát hiện khi chạy:

- `RunLeaseStore`: `claim(run_id, owner, ttl_s)`, `renew(lease)` và
  `release(lease)`;
- `RunEventBatchStore`: nối nhiều event một cách nguyên tử; và
- `ModelInputSnapshotStore`: snapshot chính xác của yêu cầu provider để phát lại
  yêu cầu provider đang chờ.

`InMemoryRunStore` triển khai cùng ngữ nghĩa định dạng truyền và fencing hiện tại
cho kiểm thử và phát triển. `PostgresRunStore` nằm trong `run_stores/postgres.py`
và dùng extra Postgres. Nó quản lý ba bảng liên quan:
`runs` (trạng thái, checkpoint, các cột lease và số thứ tự event cuối cùng),
`run_events` (duy nhất theo `(run_id, seq)`) và `model_input_snapshots`.

Unique index có điều kiện trên `runs(session_id)` chỉ nhận một run chưa kết thúc:
`pending`, `running`, `waiting_permission` và `suspended` đều chặn run mới bằng
`SessionBusyError`. Store trong bộ nhớ áp dụng cùng quy tắc. Khởi tạo cài index
trên schema phiên bản 3 tương thích và từ chối các hàng chưa kết thúc trùng lặp;
host đối soát chúng bằng bản triển khai cũ trước khi nâng cấp. Mức đồng thời task
hàng đợi worker mặc định là 32 mỗi process (tối thiểu hai), độc lập với giới hạn
hàng đợi toàn cụm. Hủy task giữ hàng chưa kết thúc để resume; `session.abort()`
rồi tiêu thụ kết quả cuối ghi nhận run aborted và giải phóng quyền tiếp nhận.
Thực thi trong process không có bộ thu hồi run bị gián đoạn.

Nhóm tiến trình và file spill của run được cleanup trước khi ghi trạng thái kết
thúc bền vững (`completed`, `aborted` hoặc `failed`). Lỗi cleanup được báo ra,
giữ admission chưa kết thúc và giữ `_active` để thử lại. `waiting_permission`
và `suspended` vẫn có thể resume.

Định danh resume loại phiên bản package và mô tả cấp cao nhất của tool, nhưng
wire generation, schema, scope/mức đồng thời và chính sách thực thi vẫn quan
trọng. Phiên bản chính sách backend `2` và thay đổi schema Bash trong 3.1.0 yêu
cầu chờ mọi run đang hoạt động kết thúc trước triển khai, kể cả run suspended.

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: create/adopt
    running --> waiting_permission: permission request checkpoint
    waiting_permission --> running: resume
    running --> suspended: workflow extension
    suspended --> running: resume
    running --> completed: terminal result
    running --> failed: ErrorEvent + failed checkpoint
    running --> aborted: cooperative abort
```

### Fencing bằng lease

`RunLease` chứa `run_id`, `owner`, `epoch`, `expires_at` và `ttl_s`. Thao tác
claim trong Postgres là một lệnh `UPDATE ... RETURNING` có điều kiện: lease chưa
hết hạn thuộc về worker khác sẽ trả về `RunLeaseConflictError`; việc tiếp quản
làm tăng epoch. Gia hạn giữ nguyên epoch và dùng TTL của token, còn giải phóng
xóa quyền sở hữu và tăng epoch. Mọi thay đổi checkpoint, trạng thái, event và
snapshot đầu vào mô hình đều nhận token `lease=` tường minh và đưa điều kiện
owner cùng epoch vào truy vấn. Bên giữ lease đã lỗi thời nhận `LeaseLostError`
và không thể nối event hoặc thay đổi trạng thái. Giải phóng token sẽ chặn token
đó vĩnh viễn; các thay đổi không có lease vẫn được hỗ trợ cho run không thuộc
quyền sở hữu của worker.

Store không có khả năng lease tùy chọn, hoặc run bắt đầu mà không giữ lease,
vẫn giữ hành vi đơn tiến trình của protocol `RunStore`.

### Thứ tự checkpoint và event

`RunEventBuffer` (`loop/checkpoint.py`) group-commit trạng thái run.
Checkpoint đang chạy và event nằm trong bộ nhớ cho đến khi một barrier commit
chúng trong một lần ghi `save_checkpoint_with_events()`:

- `flush()` trước khi gửi provider, trước khi yield mỗi `ToolCallStartEvent`
  (cùng checkpoint `tool_executing`, nên tool không thể chạy trước), sau mỗi
  `ToolCallEndEvent`, và khi kết thúc run;
- `flush_checkpoint()` trước mỗi lần ghi session store (các op `durable_call`
  tên `session.*`); nó chỉ commit checkpoint đang giữ và các event mà
  checkpoint đó bao phủ.

Checkpoint đang giữ là snapshot tại lúc lưu, kèm số event đang chờ khi đó, nên
mọi trạng thái đã commit đều là trạng thái mà thứ tự write-through cũng tạo ra.
Dispatch marker, receipt đã trả tiền và projection được ghi ngay. Giai đoạn
chuẩn bị (`started`), run exact-input, alignment đang chờ của host và các
trạng thái không phải `running` vẫn ghi xuyên. Các phase trung gian
(`assistant_appended`, `tool_batch_pending`, `tool_results_appended`,
`turn_complete`) thường chỉ tới store bên trong lần ghi của barrier kế tiếp,
nên crash giữa hai barrier sẽ resume từ barrier gần nhất. Tool đã bắt đầu mà
chưa có kết thúc bền vững sẽ resume với kết quả "bị gián đoạn" và không chạy lại.

`RunCheckpoint.tool_batch_event_after_seq` là con trỏ khôi phục cho kết quả tool.
`load_events(run_id, after_seq=...)` trả về các hàng `StoredRunEvent` theo thứ tự
tăng dần. Bên đọc luồng lưu số thứ tự cuối cùng đã chuyển giao và kết nối lại từ
số đó; thiếu một thông báo không bao giờ có nghĩa là thiếu một event.

Các envelope được lưu hiện tại dùng wire version 3. Dữ liệu run bị thiếu,
trước 3.0, không xác định hoặc không có hợp đồng đều bị từ chối. `storage/_pg.py`
điều phối việc xác minh schema Postgres dưới advisory lock.

## 3.4 Phân phối công việc cho worker và khôi phục PgQueuer

`linch.worker` là runtime tùy chọn, được cài bằng `linch[worker]` và import tường
minh. Phần lõi `linch` không import PgQueuer. Host cung cấp một pool asyncpg
và gọi `install(pool)` một lần mỗi khi tiến trình khởi động.

`enqueue_run()` và `enqueue_runs()` tạo các hàng run đang chờ và job PgQueuer
trong một giao dịch. ID run do bên gọi cung cấp là biên nhận chống trùng lặp
vĩnh viễn. Tên hàng đợi ánh xạ tới các entrypoint PgQueuer như
`linch.run:default`; cơ sở dữ liệu áp dụng giới hạn đồng thời trên toàn bộ
cụm worker. `create_schedule()` dùng `linch.schedule` job trì hoãn, kiểm tra
croniter theo UTC, ID run xác định cho mỗi lần kích hoạt và khóa chống trùng
lặp cho từng lần. Các tool lịch được gắn tường minh bằng
`register_schedule_tools()`.

`Worker` nhận một callable `AgentFactory` có đối số là `WorkerJob`.
Factory phải trả về một agent mới cho job và tái sử dụng `PostgresRunStore`
cùng `PostgresSessionStore` của job; các store đó mượn pool của host từ worker.
Worker:

1. xác minh schema và cài PgQueuer trước khi lấy job khỏi hàng đợi;
2. claim lease run Linch và bắt đầu heartbeat với chu kỳ bằng TTL của lease / 3;
3. chạy prompt mới hoặc resume checkpoint đã lưu;
4. hủy task run khi mất lease hoặc lease hết hạn mà không ghi trạng thái kết thúc
   dưới epoch lỗi thời;
5. đưa session về trạng thái ngừng hoạt động, giải phóng lease và kiểm tra run đã
   kết thúc trước khi cho phép PgQueuer xác nhận job.

Lỗi tạm thời của Postgres, listener và trình quản lý hàng đợi sẽ dựng lại trình
quản lý trên kết nối mới, với backoff có độ lệch ngẫu nhiên và giới hạn. Cơ chế
khôi phục job lỗi thời của PgQueuer lấy lại các job có heartbeat hàng đợi đã
ngừng; worker tiếp theo sẽ claim lease run đã hết hạn và resume từ checkpoint.
Lỗi thiết lập không phải lỗi hạ tầng được commit thành run thất bại với metadata
lỗi, một `ErrorEvent` và một `ResultEvent` cuối cùng. Lỗi hạ tầng vẫn có thể thử
lại thông qua `DatabaseRetryEntrypointExecutor`.

`Worker.stop()` ngăn thao tác lấy job mới khỏi hàng đợi và chờ các job đang hoạt
động hoàn tất. Host giữ pool mở cho đến khi `Worker.run()` trả về.
`PostgresRunStore` cũng có thể được dùng trực tiếp bởi bên đọc luồng API; thăm dò
`run_events` theo số thứ tự khi kênh thông báo không khả dụng hoặc bị tranh chấp quá nhiều.

## 3.5 Bộ lập lịch tool và bảo mật thực thi

`scheduler.py` là điểm kiểm soát duy nhất cho việc thực thi tool. Với mỗi khối
sử dụng tool từ provider, nó thực hiện pipeline theo thứ tự sau:

```mermaid
flowchart LR
    Resolve["resolve + validate"] --> Offered["offered-tool boundary"]
    Offered --> Pre["PreToolUse / middleware"]
    Pre --> Revalidate["validate transformed input"]
    Revalidate --> Permit["PermissionEngine"]
    Permit --> Checkpoint["tool_executing checkpoint"]
    Checkpoint --> Batch["resource-aware batches"]
    Batch --> Execute["tool.execute(ctx)"]
    Execute --> Post["PostToolUse"]
    Post --> Result["ToolCallEndEvent"]
```

Ranh giới tool được cung cấp từ chối tool đã đăng ký nhưng không có trong yêu
cầu provider hiện tại. `PreToolUse` là điểm duy nhất cho phép thay đổi đầu vào;
đầu vào sau biến đổi được kiểm tra lại và trở thành đầu vào chuẩn cho khóa
phân quyền, bản tóm tắt, khai báo tài nguyên, callback phê duyệt và thực thi.
Yêu cầu phân quyền được tổng hợp cho cả lô, và các quyết định đã phân giải được
lưu checkpoint trước khi bất kỳ tool được phép nào bắt đầu.

Các tool `scope="read"` có cờ `parallel` là true có thể dùng chung một lô.
Tool `scope="write"` hoặc `scope="exec"` với cờ boolean vẫn chạy tuần tự.
Callable xét đầu vào có thể chứng minh một lời gọi cụ thể an toàn để chạy song
song trong bất kỳ scope nào; nếu hàm điều kiện gặp lỗi, lời gọi mặc định chạy
tuần tự. `ResourceAccess(resource, mode)` ngăn xung đột ghi/đọc hoặc ghi/ghi.
`max_tool_concurrency` và `tool_batching_strategy` giới hạn và định hình các lô,
còn các khối kết quả được ghép lại theo thứ tự của provider.

Timeout và chính sách thử lại của tool được áp dụng tại điểm kiểm soát này.
Lời gọi đọc có thể thử lại với các exception đã cấu hình; lời gọi ghi/thực thi
chỉ thử lại khi tool khai báo `retryable = True`. Lời gọi nền trả về xác nhận
trong khi task vẫn thuộc quyền sở hữu của session, và việc hoàn tất được hiển
thị qua các đường thông báo/event của session.

## 3.6 Provider, quyết định kết thúc và độ tin cậy

Provider triển khai `BaseProvider` và trả về các dictionary streaming đã chuẩn
hóa; vòng lặp không tiêu thụ đối tượng SDK của nhà cung cấp. Các adapter tích
hợp sẵn gồm OpenAI Chat Completions, OpenAI Responses, Anthropic, DeepSeek,
Gemini, llama.cpp, vLLM và SGLang. Gemini dùng gói tùy chọn `google-genai`
(`linch[gemini]`). Các khả năng của provider được dùng để hạ cấp những tùy chọn
yêu cầu không được hỗ trợ trước khi gửi đi.

`loop/turn_stopping.py` chứa điểm quyết định tuần tự được dùng cho các nhánh
văn bản cuối cùng, final-tool, forced-final và phản hồi không hợp lệ. Listener
trả về `TurnStoppingDecision("accept")`, `"continue"` kèm phản hồi hoặc `"fail"`
kèm lỗi. Phản hồi để tiếp tục được nối qua log session trước lượt tiếp theo.
Xác nhận final-tool chỉ được phát sau quyết định này, nên listener có thể phủ
quyết việc thực thi hoặc yêu cầu sửa lại.

Cấu hình `Agent` mặc định cài `ReliabilityPolicy()`:

```python
from linch import Agent, ReliabilityPolicy

agent = Agent(model="your-model")
assert agent.reliability == ReliabilityPolicy()
agent_without_reliability = Agent(model="your-model", reliability=None)
```

Chính sách mặc định thử lại các phản hồi provider không hợp lệ trong tối đa hai
lượt sửa lỗi. Nó không thực thi tool nào từ phản hồi native bị từ chối.
Các quy tắc `ToolPrerequisite` được cài dưới dạng hook `PreToolUse`, còn các quy
tắc `CompletionGate` kiểm tra kết quả thành công của tool chạy ở tiền cảnh.
Cả trạng thái độ tin cậy lẫn cấu hình chính sách đều thuộc trạng thái resume
bền vững.

Vòng lặp còn có `LoopGuard` trên `Agent`, được bật mặc định và có thể tắt: nó
theo dõi chữ ký lời gọi tool lặp lại và các lô thất bại liên tiếp. Giới hạn run
`max_turns` riêng biệt được vòng lặp thực thi. Nhánh force-final của guard chèn
lời nhắc và loại bỏ tool khỏi một yêu cầu cuối cùng.

## 3.7 Đánh giá phân quyền và `Principal`

`RunOptions.principal` mang một `Principal` bất biến từ host vào một run và các
lời gọi tool của run đó:

```python
from linch import Agent, Principal, RunOptions, ToolRule

principal = Principal(
    tenant="acme",
    user="user-42",
    roles={"editor"},
)
agent = Agent(
    model="your-model",
    permissions={"rules": [ToolRule("write_file", "allow", role="editor")]},
)
session = await agent.session()
events = session.run("update the report", RunOptions(principal=principal))
```

`Principal` được định nghĩa trong `permissions/principal.py`; tenant, user và
các role dạng chuỗi không rỗng của nó được kiểm tra và đóng băng. `ToolRule`,
`PathRule` và `BashRule` có thể chọn `role=` hoặc loại trừ bằng `unless_role=`.
`PermissionEngine` truyền principal cho `CanUseToolRequest`, và
`ToolContext.principal` cho tool truy cập principal. Hợp đồng run lưu principal
để resume không thể âm thầm đổi định danh tenant, user hoặc role.

Đây là chính sách RBAC do host cấu hình, không phải dịch vụ định danh trên toàn
tiến trình. Principal bị thiếu có tập role rỗng; host cần cô lập tenant phải
cung cấp principal cho mọi run mới và dùng quy tắc yêu cầu role phù hợp.

## 3.8 Ngữ cảnh, bộ nhớ tệp và RAG

`ContextInjectionHook` nhận snapshot của `session.provider_view` và trả về
`ContextBuildResult`. Các khối system, thông điệp bổ sung, tool được chọn,
metadata và `ContextBudget` được hợp nhất vào yêu cầu và phát dưới dạng
`ContextBuildEvent`; log session không thay đổi. Các bộ dựng thực hiện I/O
bất đồng bộ và có thể nhận đối tượng `deps` của từng run.

`FileMemoryContextBuilder` là cơ chế bộ nhớ tệp hiện tại. Khi được gắn bằng
`ContextInjectionHook`, nó đọc `/memories/MEMORY.md` một lần mỗi run, giữ 200
dòng nội dung đầu tiên và chèn một chỉ dẫn ổn định cùng chỉ mục. Mô hình dùng
các tool hệ thống tệp ảo hiện có để đọc, ghi và sửa một thông tin trong mỗi
tệp. Backend cũng cung cấp thao tác xóa cho mã host, nhưng
`filesystem_tools()` không đăng ký tool xóa.

RAG là một protocol nhỏ có phạm vi theo yêu cầu trong `rag/__init__.py`:

```python
from linch import Agent, Principal, RetrievedChunk, RunOptions, register_rag_connector

class TenantRag:
    async def retrieve(self, query: str, *, k: int, principal=None):
        """Return an example sourced chunk for this request."""
        return [RetrievedChunk("doc-1", "handbook", "...", 0.9, {})]

agent = Agent(model="your-model")
run_principal = Principal("acme", "user-42", {"reader"})
undo = register_rag_connector(
    agent,
    TenantRag(),
    k=5,
    principal=run_principal,
)
session = await agent.session()
events = session.run("question", RunOptions(principal=run_principal))
```

`RagConnector.retrieve()` trả về các giá trị `RetrievedChunk` có nguồn. Hook
thêm các khối system `[doc:<id>]` và ghi các ID vào trạng thái cục bộ của run có
thể lưu checkpoint. `register_grounding_check(required=True)` thêm listener
`TurnStopping` chỉ chấp nhận trích dẫn đến các ID được truy xuất gần nhất;
thiếu trích dẫn sẽ nhận phản hồi sửa lỗi với số lần giới hạn, rồi thất bại.
`RunOptions.principal` đang hoạt động được truyền vào thao tác truy xuất.
`principal` khi đăng ký là cơ chế bảo vệ tùy chọn để kiểm tra định danh mong
đợi, nên principal của run bị thiếu hoặc khác sẽ khiến thao tác bị từ chối
trước I/O của connector.
Các connector RAG và client của chúng thuộc quyền sở hữu của host; Linch không
thêm dependency embedding hoặc cơ sở dữ liệu vector.

## 3.9 Hệ thống tệp ảo và chuyển kết quả ra ngoài

Hệ thống tệp ảo bị tắt mặc định thông qua
`FeatureFlags(filesystem=False)`. Khi bật, session nhận một
`StateFileBackend` trừ khi host cung cấp backend. `filesystem_tools()` trả về
các tool `ls`, `read_file`, `write_file` và `edit_file`. `DiskFileBackend` và
`PostgresFileBackend` là các triển khai backend tùy chọn; backend sau được
import từ `linch.filesystem.postgres`.

`OffloadConfig` kiểm soát việc chuyển kết quả lớn ra ngoài. Khi được bật và kết
quả vượt ngưỡng đã phân giải, bộ lập lịch ghi toàn bộ nội dung và đặt bản xem
trước cùng gợi ý đường dẫn vào kết quả dành cho mô hình. Bất kể backend hệ
thống tệp nào được cấu hình, nội dung được đưa vào session store qua
`SessionOffloadBackend` (`src/linch/filesystem/offload.py`), thuộc session gốc
của cây agent, nên nó không bao giờ đi vào workspace nào và một run được resume
ở tiến trình khác vẫn đọc lại được. `ToolResult` đầy đủ vẫn nằm trên
`ToolCallEndEvent.tool_result`. Cấu hình offload mặc định suy ra ngưỡng bằng
10% cửa sổ ngữ cảnh của provider; `preview_lines` mặc định là 10. Nếu việc ghi
thất bại, kể cả khi session store không có khả năng offload, kết quả gốc được
giữ nguyên.

Dùng hệ thống tệp tổng hợp dựa trên Postgres khi các tệp bộ nhớ phải tồn tại
qua lần khởi động lại tiến trình:

```python
from linch import Agent, CompositeFileBackend, FeatureFlags, StateFileBackend
from linch.filesystem.postgres import PostgresFileBackend

dsn = "postgresql://user:password@localhost/linch"
filesystem = CompositeFileBackend(
    default=StateFileBackend(),
    routes={"/memories/": PostgresFileBackend(dsn)},
)
agent = Agent(
    model="your-model",
    features=FeatureFlags(filesystem=True),
    filesystem=filesystem,
)
```

## 3.10 OpenTelemetry

Phần lõi phụ thuộc `opentelemetry-api`; host chọn SDK và exporter. Adapter
private trong `_telemetry.py` phân giải tracer và meter thông qua `Context`
của agent (hoặc các provider API toàn cục khi không có provider theo phạm vi
được đăng ký). Khi không có SDK, các lời gọi không thực hiện thao tác nào.

Kernel tạo các span sau tại ranh giới I/O:

| Span | Ranh giới |
|---|---|
| `invoke_agent` | Một run, gồm dấu resume và ID run. |
| `step` | Một lượt. |
| `chat <model>` | Một luồng provider, gồm mức sử dụng và lý do kết thúc. |
| `execute_tool <name>` | Một lần thực thi tool. |
| `compaction` | Một thao tác compaction. |

Lời gọi store không phải span. Chúng ghi `linch.store.duration` với thuộc tính
`op`. Meter còn ghi `gen_ai.client.token.usage` và
`gen_ai.client.operation.duration`. Các thuộc tính span và metric dùng tên
quy ước ngữ nghĩa `gen_ai.*` cùng các thuộc tính `linch.*` cho run, lượt, cuộc
hội thoại, resume, dừng và lỗi.

Prompt, đối số tool, kết quả tool và nội dung cuối cùng chỉ được thu thập khi
host đặt
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Nếu không, adapter
ghi metadata định danh, thời gian, mức sử dụng và lỗi mà không có nội dung
thông điệp.

Để kiểm thử hoặc đo đạc riêng từng agent, đăng ký `tracer_provider` và
`meter_provider` trên một `Context` trước khi tạo agent. Runtime không cài
protocol observer hoặc bộ thu thập span tùy chỉnh.

## 3.11 Extension và các ranh giới thuộc host

Bề mặt public của phần lõi gồm kernel agent, vòng lặp, provider, tool, phân
quyền, session, run store, hook, hệ thống tệp và adapter MCP. Các phần sau là
extension tường minh dưới `linch.ext.*`:

- `linch.ext.deep_agent` cho các preset coordinator/worker;
- `linch.ext.evals` cho đánh giá theo kịch bản;
- `linch.ext.workflow` cho workflow xác định có ghi nhật ký;
- `linch.ext.reports` cho các tiện ích báo cáo;
- `linch.ext.loop_runner` cho các vòng lặp định kỳ do host điều khiển; và
- `linch.ext.scaffold` cho việc dựng khung dự án.

`coordination/` giữ cơ chế nhắn tin mailbox trong tiến trình. Phân phối hàng
đợi bền vững và lịch nằm trong `linch.worker`. Host chịu trách nhiệm về HTTP,
thông tin xác thực provider, đối tượng dependency của tenant, client RAG/vector,
tính lũy đẳng của tác động phụ bên ngoài và vòng đời OpenTelemetry SDK.

Quay lại [mục lục kiến trúc](./README.md).
