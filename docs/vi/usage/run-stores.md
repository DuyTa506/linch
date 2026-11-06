# Kho lưu trữ run bền vững

[Bản gốc tiếng Anh](../../usage/run-stores.md)

Sử dụng kho lưu trữ run khi checkpoint và các event run có thứ tự cần tồn tại sau khi khởi động lại.

`RunStore` quản lý trạng thái run có thể resume: bản ghi run, checkpoint mới nhất của run và
nhật ký event có thứ tự. `SessionStore` quản lý metadata của cuộc hội thoại và các
message chỉ được ghi nối tiếp. Cấu hình cả hai trên `Agent` khi lịch sử hội thoại và
các run có thể resume cần tồn tại sau khi tiến trình khởi động lại.

## Các backend tích hợp sẵn

Đối với trạng thái run và session, SDK cung cấp hai kho lưu trữ cục bộ trong tiến trình và hai
adapter lưu trữ bền vững:

| Kho lưu trữ | Thời gian tồn tại của trạng thái | Mục đích sử dụng |
|---|---|---|
| `InMemoryRunStore` | Chỉ trong tiến trình hiện tại | Kiểm thử và phát triển cục bộ |
| `InMemorySessionStore` | Chỉ trong tiến trình hiện tại | Kiểm thử và phát triển cục bộ |
| `PostgresRunStore` | PostgreSQL | Run bền vững và các worker dùng chung |
| `PostgresSessionStore` | PostgreSQL | Session bền vững và các worker dùng chung |

Các kho lưu trữ bền vững tích hợp sẵn sử dụng PostgreSQL. Cài đặt dependency tùy chọn
bằng `pip install 'linch[postgres]'`.

~~~python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

runs = PostgresRunStore("postgresql://user:password@localhost/linch")
sessions = PostgresSessionStore("postgresql://user:password@localhost/linch")
agent = Agent(model="gpt-5", session_store=sessions, run_store=runs)
~~~

Các hàm khởi tạo có chữ ký chính xác như sau:

~~~text
PostgresRunStore(
    dsn: str,
    *,
    pool: Any = None,
    min_size: int = 1,
    max_size: int = 10,
)

PostgresSessionStore(
    dsn: str,
    *,
    pool: Any = None,
    min_size: int = 1,
    max_size: int = 10,
)
~~~

Việc khởi tạo một trong hai adapter sẽ kiểm tra xem `asyncpg` đã được cài đặt hay chưa. Pool và schema
được khởi tạo theo cơ chế trì hoãn, vào lần thao tác đầu tiên với kho lưu trữ. Nếu được cung cấp pool,
ứng dụng chủ là bên sở hữu pool đó và kho lưu trữ không bao giờ đóng nó; adapter session bỏ qua `dsn`
khi được cung cấp pool. Pool do kho lưu trữ tạo sẽ được đóng bằng `await store.close()`. Ứng dụng chủ
phải giữ pool được mượn ở trạng thái mở cho đến khi tất cả kho lưu trữ sử dụng pool đó đã dừng.
Nếu việc khởi tạo pool thuộc sở hữu của kho lưu trữ hoặc schema thất bại, adapter sẽ đóng và đặt lại
pool thuộc sở hữu đó để lần thử sau có thể khởi tạo lại; pool được mượn vẫn thuộc trách nhiệm của ứng dụng chủ.

Cơ sở dữ liệu PostgreSQL mới nhận schema phiên bản 3 hiện tại. Các cơ sở dữ liệu
hiện có mà metadata schema Linch bị thiếu, cũ, không xác định hoặc không nhất quán
với cấu trúc bắt buộc sẽ bị từ chối mà không bị thay đổi; schema không tương thích
không được nâng cấp. Với schema phiên bản 3 tương thích, quá trình khởi tạo cài
unique index có điều kiện để giới hạn tiếp nhận run. Nhiều run chưa kết thúc của
cùng session khiến khởi tạo thất bại với `ConfigError`; dùng bản triển khai cũ để
resume hoặc đối soát và đánh dấu thất bại các run đó trước khi nâng cấp store/worker. Việc cài index
này không yêu cầu cơ sở dữ liệu mới. Checkpoint/hợp đồng/event của run, biên nhận inbox,
ảnh chụp khung nhìn provider và ảnh chụp đầu vào mô hình sử dụng phiên bản định dạng truyền 3.

## Quy ước của kho lưu trữ

Các protocol dưới đây là quy ước triển khai. Những thao tác thay đổi thành công
được lưu bền vững trước khi trả về. Giá trị do các kho lưu trữ tích hợp sẵn trả về đều là
bản sao tách biệt: việc thay đổi bản ghi, message, tác vụ, checkpoint, event
hoặc metadata lồng nhau được trả về không được làm thay đổi trạng thái đã lưu. Các triển khai kho lưu trữ
cũng phải sao chép tách biệt các đồ thị đối tượng đầu vào có thể thay đổi trước khi giữ lại chúng.

Cả hai run store tích hợp sẵn chỉ nhận một run chưa kết thúc cho mỗi session.
Run thứ hai phát sinh `SessionBusyError` có `session_id` và `run_id` hiện có.
`pending`, `running`, `waiting_permission` và `suspended` đều chặn tiếp nhận;
trạng thái kết thúc giải phóng session. Host xếp hàng prompt mới hoặc báo bận.
Hủy task trong process giữ hàng bền vững chưa kết thúc: hãy resume hoặc đối soát
và đánh dấu thất bại. Dùng `session.abort()` rồi tiêu thụ kết quả cuối khi muốn
abort kết thúc run. Xem [cấu hình production](./production.md).

Nếu run xung đột trong admission Postgres biến mất trước khi đọc được, admission
thử tối đa ba lần ngoài transaction do caller quản lý, hoặc một lần trong đó.
Xung đột chưa giải quyết gây lỗi kèm nguyên nhân; thử lại transaction hoặc đối
soát run cạnh tranh.

### Các giá trị của run

`RunRecord` chứa:

- `id: str`
- `session_id: str`
- `status`, là một trong các giá trị `pending`, `running`, `waiting_permission`, `suspended`,
  `completed`, `failed` hoặc `aborted`
- Các dấu thời gian `created_at: str` và `updated_at: str`
- `checkpoint: RunCheckpoint | None`
- `meta: dict[str, object]` an toàn cho JSON

`StoredRunEvent` chứa `seq` bắt đầu từ 1, riêng cho từng run, dấu thời gian `appended_at`
và một `event` có kiểu. `RunCheckpoint` lưu giữ giai đoạn thực thi, prompt,
chỉ số lượt, mức sử dụng tích lũy, message assistant đã được chấp nhận, các khối và kết quả tool
đang chờ, trạng thái buộc kết thúc, trạng thái bảo vệ vòng lặp, lớp phủ skill đang chờ, các tool
được phép, lý do assistant dừng, các quyết định về quyền, trạng thái worker nền,
trạng thái cắt ngắn, mốc event của lô tool, trạng thái căn chỉnh session,
trạng thái phần mở rộng, lần thử provider và ảnh chụp đầu vào mô hình được tham chiếu.
Giai đoạn của nó là một trong các giá trị `started`, `user_appended`,
`provider_pending`, `assistant_appended`, `permission_pending`,
`tool_batch_pending`, `tool_executing`, `tool_results_appended`,
`turn_complete`, `workflow_suspended`, `completed`, `failed` hoặc `aborted`.
Kho lưu trữ phải lưu rồi đọc lại nguyên vẹn mọi trường của checkpoint,
bao gồm cả trạng thái do phần mở rộng quản lý mà kho lưu trữ không diễn giải.

Các trường của checkpoint và giá trị mặc định là:

~~~text
phase: RunPhase
prompt: str
turn_index: int
total_usage: Usage
assistant_message: Message | None = None
pending_tool_blocks: list[ToolUseBlock] = []
completed_tool_results: dict[str, ToolResultBlock] = {}
force_final_pending: bool = False
loop_guard_state: dict[str, object] | None = None
pending_skill_overlay: dict[str, object] | None = None
current_turn_allowed_tools: list[str] | None = None
assistant_stop_reason: str | None = None
permission_decisions: dict[str, dict] = {}
background_workers: dict[str, dict[str, object]] = {}
truncation_attempts: int = 0
truncation_prefix: str = ""
pending_truncation_feedback: str | None = None
tool_batch_event_after_seq: int | None = None
pending_alignment: list[dict[str, Any]] = []
extension_state: dict[str, Any] = {}
provider_attempt: int = 0
model_input_snapshot_id: str | None = None
~~~

### Các phương thức RunStore bắt buộc

Protocol bắt buộc có chữ ký chính xác như sau:

~~~text
async def create_run(
    session_id: str,
    *,
    id: str | None = None,
    meta: dict[str, object] | None = None,
    initial_checkpoint: RunCheckpoint | None = None,
    lease: RunLease | None = None,
) -> RunRecord

async def load_run(run_id: str) -> RunRecord | None

async def save_checkpoint(
    run_id: str,
    checkpoint: RunCheckpoint,
    *,
    status: str = "running",
    lease: RunLease | None = None,
) -> None

async def save_checkpoint_with_events(
    run_id: str,
    checkpoint: RunCheckpoint,
    events: Sequence[Event],
    *,
    status: str = "running",
    lease: RunLease | None = None,
) -> list[int]

async def append_event(
    run_id: str,
    event: Event,
    *,
    lease: RunLease | None = None,
) -> int

async def load_events(
    run_id: str,
    *,
    after_seq: int = 0,
) -> list[StoredRunEvent]

async def mark_completed(
    run_id: str,
    checkpoint: RunCheckpoint,
    *,
    lease: RunLease | None = None,
) -> None

async def mark_failed(
    run_id: str,
    checkpoint: RunCheckpoint | None = None,
    error: dict[str, object] | None = None,
    *,
    events: Sequence[Event] = (),
    lease: RunLease | None = None,
) -> list[int]
~~~

Triển khai từng phương thức như sau:

- `create_run` tạo UUID khi bỏ qua `id` và tạo bản ghi `running`.
  `initial_checkpoint`, nếu được cung cấp, được commit cùng hàng đó. ID được chỉ định
  chỉ có thể tiếp quản bản ghi đang chờ thuộc cùng session; việc tiếp quản
  chuyển bản ghi sang trạng thái running, hợp nhất metadata đầu vào và thay thế
  checkpoint của nó. Bản ghi không ở trạng thái pending hoặc thuộc session khác sẽ gây ra
  `ValueError`. Nếu cả metadata đã lưu và metadata được yêu cầu đều chứa hợp đồng run,
  hợp đồng không tương thích sẽ gây ra `RunContractMismatchError`. Việc tạo và
  tiếp quản sao chép tách biệt, đồng thời xác thực tất cả đầu vào trước khi thay đổi trạng thái. Lease trên
  hàng mới là không hợp lệ; lease khi tiếp quản thực hiện fencing đối với hàng pending hiện có.
- `load_run` trả về bản ghi tách biệt hoặc `None` nếu ID không tồn tại.
- `save_checkpoint` thay thế checkpoint, lưu `status` được cung cấp
  (mặc định là `"running"`) và cập nhật `updated_at`. Các phương thức ghi
  checkpoint (`save_checkpoint`, `save_checkpoint_with_events`, `mark_completed`,
  `mark_failed`) không trả về bản ghi; đọc trạng thái đã lưu bằng `load_run`.
  Run không tồn tại gây ra `KeyError`.
- `save_checkpoint_with_events` commit checkpoint/trạng thái và tất cả
  event được cung cấp thành một ranh giới giai đoạn. Các event giữ nguyên thứ tự đầu vào. Danh sách
  số thứ tự trả về tương ứng với đầu vào. Lỗi mã hóa checkpoint hoặc event,
  lỗi cơ sở dữ liệu hoặc lỗi fencing đều giữ nguyên checkpoint cũ và
  nhật ký event cũ.
- `append_event` ghi nối tiếp một event và trả về số thứ tự mới của nó, bắt đầu từ 1.
- `load_events` trả về các hàng tách biệt theo số thứ tự tăng dần. `after_seq`
  là mốc loại trừ; `0` đọc từ đầu.
- `mark_completed` lưu checkpoint được cung cấp với giai đoạn `"completed"` và
  trạng thái `"completed"`. Bản thân phương thức này không ghi nối tiếp event.
- `mark_failed` lưu trạng thái `"failed"`; khi được cung cấp checkpoint,
  giai đoạn của nó được lưu thành `"failed"`; nếu không thì giữ nguyên checkpoint hiện có.
  Lỗi được cung cấp sẽ được thêm vào lịch sử `meta["errors"]` của run; lịch sử
  lỗi sai định dạng được thay bằng danh sách mới. Các event kết thúc được cung cấp sẽ
  được ghi nối tiếp trong cùng thao tác nguyên tử. Lỗi mã hóa, cơ sở dữ liệu hoặc fencing
  đều giữ nguyên trạng thái, checkpoint, metadata và các event.

Mọi thao tác thay đổi bắt buộc có nhận `lease` đều xác thực token trong
cùng lần thay đổi trạng thái. Nếu bỏ qua tham số này, hành vi không có lease được giữ nguyên. Các kho lưu trữ tích hợp sẵn
sử dụng bộ đếm riêng cho từng run để cấp số thứ tự event; PostgreSQL sử dụng
`last_event_seq` của hàng run, vì vậy các bên ghi đồng thời không cấp số thứ tự bằng cách đọc `MAX(seq)`.
Protocol `RunStore` không yêu cầu `close`, nhưng cả hai kho lưu trữ run tích hợp sẵn
đều cung cấp `async def close() -> None`; phương thức này không làm gì với `InMemoryRunStore` và
chỉ đóng pool do kho lưu trữ tạo với `PostgresRunStore`.

### Các khả năng tùy chọn của kho lưu trữ run

#### Ghi nối tiếp event theo lô

Kho lưu trữ có thể triển khai thêm khả năng có chữ ký chính xác sau:

~~~text
async def append_events(
    run_id: str,
    events: list[Event],
    *,
    lease: RunLease | None = None,
) -> list[int]
~~~

Khi triển khai khả năng tùy chọn này, lô được mã hóa và
commit theo thứ tự đầu vào trong một thao tác hoặc thành công toàn bộ, hoặc không thay đổi gì.
Các số thứ tự trả về liên tiếp và tương ứng với các event. Lô rỗng
trả về `[]`; nếu được cung cấp lease, lease vẫn được xác thực. Lô không rỗng
cho run không tồn tại gây ra `KeyError`, token cũ gây ra `LeaseLostError`,
và lỗi mã hóa giữ nguyên nhật ký event. Vòng lặp phát hiện
khả năng này; nếu không có, vòng lặp có thể sử dụng append_event.

#### Ảnh chụp chính xác đầu vào mô hình

`ModelInputSnapshotStore` là khả năng tùy chọn trên kho lưu trữ run. Chữ ký chính xác
của nó là:

~~~text
async def save(
    run_id: str,
    provider_attempt: int,
    request: ProviderRequest,
    *,
    lease: RunLease | None = None,
) -> ModelInputSnapshot

async def load(snapshot_id: str) -> ModelInputSnapshot | None

async def delete(
    snapshot_id: str,
    *,
    run_id: str,
    lease: RunLease | None = None,
) -> None

async def prune(
    run_id: str,
    keep_ids: Sequence[str] = (),
    *,
    lease: RunLease | None = None,
) -> int
~~~

`ModelInputSnapshot` là bất biến và chứa `id`, `run_id`,
`provider_attempt`, `codec_version`, `request_json` ở dạng chuẩn,
`integrity_hash` và `created_at`.
Các triển khai phải tuân theo những quy tắc sau:

- `save` yêu cầu run đã tồn tại và `provider_attempt` là số nguyên dương,
  không phải boolean. Phương thức này xác thực toàn bộ `ProviderRequest` trước
  khi lưu bất cứ thứ gì. Ảnh chụp sử dụng phiên bản codec `3` và loại trừ
  `signal` đang hoạt động. JSON chuẩn của ảnh chụp sử dụng UTF-8, khóa được sắp xếp, không có
  khoảng trắng không mang ý nghĩa và không cho phép số không hữu hạn. Payload bao gồm
  mô hình của provider, các khối văn bản hệ thống, tool, message, các tùy chọn điều khiển sinh nội dung,
  thiết lập reasoning/thinking, schema đầu ra, lựa chọn tool, thiết lập streaming
  một phần và trường `_native_catalog_changes` tùy chọn; trường cuối này được
  bỏ qua khi có giá trị `None`.
- Nguồn chuẩn cho codec là các hàm hỗ trợ công khai
  `create_model_input_snapshot(run_id, provider_attempt, request)` và
  `decode_model_input_snapshot(snapshot, signal=...)`. Kho lưu trữ lưu
  ảnh chụp do hàm thứ nhất trả về và sử dụng hàm thứ hai để xác minh/giải mã ảnh chụp;
  không được tự tạo một cách mã hóa request thứ hai.
- Giá trị kiểm tra tính toàn vẹn là `sha256:` theo sau bởi SHA-256 của
  `f"{codec_version}\n{request_json}"` được mã hóa bằng UTF-8. Codec request chính xác
  từ chối đối tượng không được hỗ trợ, khóa ánh xạ không phải chuỗi, chu trình và
  số không hữu hạn trước khi ảnh chụp có thể được checkpoint tham chiếu.
- `load` trả về `None` nếu ảnh chụp không tồn tại. Phương thức này phải xác minh phiên bản
  codec, mã băm toàn vẹn, cấu trúc JSON và các trường request có kiểu. Dữ liệu hỏng,
  lỗi toàn vẹn hoặc cấu trúc request không hợp lệ gây ra
  `ModelInputSnapshotError`; phiên bản định dạng truyền không được hỗ trợ gây ra
  `UnsupportedWireVersionError`.
- `delete` yêu cầu đối số từ khóa `run_id` của run sở hữu và chỉ xóa ảnh chụp có
  `run_id` đã lưu khớp với nó. Ảnh chụp không tồn tại hoặc thuộc run khác sẽ không bị
  xóa. PostgreSQL báo run không tồn tại bằng `KeyError`; triển khai trong bộ nhớ
  không có hàng để xóa và trả về.
- `prune` xóa mọi ảnh chụp thuộc `run_id` ngoại trừ các ID trong `keep_ids` và
  trả về số lượng đã xóa. Mặc định rỗng không giữ lại ảnh chụp nào. PostgreSQL báo
  run không tồn tại bằng KeyError; triển khai trong bộ nhớ trả về không khi
  không có ảnh chụp nào khớp.

`save`, `delete` và `prune` thực hiện fencing cho thay đổi run và thay đổi ảnh chụp bằng
cùng một lần kiểm tra lease. Lease cũ không thay đổi bảng ảnh chụp hay
run. Các kho lưu trữ run tích hợp sẵn trong bộ nhớ và PostgreSQL đều triển khai khả năng này.
Với kho lưu trữ tùy chỉnh, việc gọi phương thức không có lease sẽ bỏ qua đối số từ khóa lease để
chữ ký không có lease hiện tại có thể tiếp tục hoạt động; việc cung cấp lease cho thao tác thay đổi
không nhận đối số từ khóa này sẽ gây ra TypeError trước khi phương thức đó được gọi.

Khi `DurabilityOptions.exact_model_input` là true, cả bốn phương thức ảnh chụp
đều bắt buộc; thiếu khả năng `save`, `load`, `delete` hoặc `prune`
sẽ gây ra `ConfigError` trước khi run thực thi. Tùy chọn này mặc định là `false`.
Các giá trị mặc định của `DurabilityOptions` là `durable_inbox=False`,
`exact_model_input=False` và `delivery_lease_s=30.0`; cấu hình `strict_v1()`
của nó bật cả hai khả năng.

## Quy ước của SessionStore

### Các giá trị của session

`SessionRecord` có `id`, `created_at`, `updated_at`, metadata ứng dụng
`meta` và danh sách các dictionary `invoked_skills` đã được lưu. `StoredMessage`
có `seq` bắt đầu từ 1, riêng cho từng session, dấu thời gian `appended_at` và một
`message` có kiểu. Các số thứ tự message được sắp xếp trong từng session.

### Các phương thức bắt buộc

Protocol SessionStore có chữ ký chính xác như sau:

~~~text
async def create(
    *,
    id: str | None = None,
    meta: dict[str, object] | None = None,
) -> SessionRecord

async def load(id: str) -> SessionRecord | None

async def load_messages(id: str) -> list[StoredMessage]

async def append_messages(
    id: str,
    messages: list[Message],
) -> list[StoredMessage]

async def update_meta(
    id: str,
    meta: dict[str, object],
) -> SessionRecord

async def set_invoked_skills(
    id: str,
    skills: list[dict[str, object]],
) -> None

async def list(
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[SessionRecord]

async def delete(id: str) -> None

async def create_task(
    session_id: str,
    input: CreateTaskInput,
) -> Task

async def get_task(
    session_id: str,
    task_id: str,
) -> Task | None

async def list_tasks(session_id: str) -> list[Task]

async def update_task(
    session_id: str,
    task_id: str,
    patch: TaskPatch,
) -> Task | None

async def delete_task(
    session_id: str,
    task_id: str,
) -> bool

async def claim_task(
    session_id: str,
    task_id: str,
    owner: str,
) -> Task | None

async def ready_tasks(session_id: str) -> list[Task]

async def release_task(
    session_id: str,
    task_id: str,
) -> Task | None

async def close() -> None
~~~

Các thao tác session bắt buộc có ngữ nghĩa như sau:

- `create` tạo UUID khi bỏ qua `id`. ID được chỉ định có tính lũy đẳng:
  bản ghi hiện có được trả về nguyên vẹn và `meta` mới bị bỏ qua. Bản ghi
  mới bắt đầu với `invoked_skills` rỗng và trạng thái message, tác vụ, inbox
  rỗng.
- `load` trả về bản ghi tách biệt hoặc `None`. `load_messages` trả về các hàng tách biệt
  theo số thứ tự tăng dần.
- `append_messages` gán các số thứ tự liên tiếp riêng cho từng session theo thứ tự đầu vào và
  trả về các hàng tách biệt theo thứ tự đó. Tất cả message được mã hóa trước khi
  thay đổi trạng thái; lô không rỗng cho session không tồn tại gây ra KeyError, và
  lỗi mã hóa hoặc lưu trữ sẽ không ghi nối tiếp bất kỳ message nào trong lô. Lô rỗng trả về
  [] mà không cấp số thứ tự; adapter PostgreSQL trả về trước khi
  kiểm tra sự tồn tại của session, trong khi adapter trong bộ nhớ vẫn kiểm tra
  session.
- `update_meta` hợp nhất các khóa được cung cấp vào ánh xạ metadata cấp cao nhất
  hiện có và trả về bản ghi tách biệt. Phương thức này không thay thế các khóa không được chỉ định.
  Session không tồn tại gây ra KeyError.
- `set_invoked_skills` thay thế toàn bộ danh sách bằng bản sao tách biệt. Session không tồn tại
  gây ra KeyError.
- `list` sắp xếp theo `updated_at` giảm dần, nên session được cập nhật gần nhất đứng đầu.
  `offset` bỏ qua số hàng mới nhất tương ứng; `limit=None` nghĩa là không có giới hạn tối đa. Các bản ghi
  trả về đều tách biệt.
- `delete` có tính lũy đẳng và không làm gì khi ID không tồn tại. Phương thức này xóa session,
  message, ảnh chụp provider, đồ thị tác vụ và bộ đếm, cùng trạng thái inbox trong
  cùng thao tác backend.
- `close` giải phóng tài nguyên thuộc sở hữu của kho lưu trữ. Triển khai trong bộ nhớ không có
  tài nguyên cần giải phóng.

Việc tạo session, thay đổi metadata/skill, thay đổi message và thay đổi tác vụ
đều cập nhật updated_at của session. Ảnh chụp provider và việc thêm vào inbox có
ngữ nghĩa lưu trữ riêng. Kho lưu trữ tùy chỉnh phải bảo toàn thứ tự message
trong từng session và thuộc tính tách biệt của đầu vào/giá trị trả về.

### Các giá trị và phương thức của tác vụ

Giá trị đầu vào của tác vụ có cấu trúc sau:

~~~text
CreateTaskInput(
    subject: str,
    description: str,
    active_form: str | None = None,
    metadata: dict[str, object] | None = None,
)
~~~

Task trả về có:

~~~text
id: str
session_id: str
subject: str
description: str
active_form: str | None
status: "pending" | "in_progress" | "completed"
owner: str | None
blocks: list[str]
blocked_by: list[str]
metadata: dict[str, object]
created_at: str
updated_at: str
~~~

Tác vụ mới có trạng thái `pending`, chưa có chủ sở hữu và nhận ID dạng số tăng đơn điệu
riêng trong session của nó. `create_task` yêu cầu session tồn tại và gây ra
`KeyError` nếu không có session. PostgreSQL cấp ID bằng bộ đếm
riêng cho từng session trong giao dịch; triển khai trong bộ nhớ có tính nguyên tử trong event loop.
`list_tasks` sắp xếp
theo ID tác vụ dạng số. Tất cả bản ghi tác vụ cùng các danh sách lồng nhau và metadata do
kho lưu trữ trả về đều tách biệt.

`TaskPatch` có các trường sau:

~~~text
subject: str | None = None
description: str | None = None
active_form: str | None = None
status: "pending" | "in_progress" | "completed" | "deleted" | None = None
owner: str | None = None
add_blocks: list[str] | None = None
add_blocked_by: list[str] | None = None
remove_blocks: list[str] | None = None
remove_blocked_by: list[str] | None = None
metadata: dict[str, object] | None = None
~~~

`None` trong trường patch vô hướng có nghĩa là giữ nguyên trường đó. Metadata được
hợp nhất theo khóa; giá trị metadata `None` xóa khóa đó. `owner=None` cũng
có nghĩa là giữ nguyên chủ sở hữu; sử dụng `release_task` để xóa quyền sở hữu.
`status="deleted"` xóa tác vụ và khiến `update_task` trả về `None`. Tác vụ
không tồn tại khiến `get_task` và `update_task` trả về `None`.

`blocks` và `blocked_by` là các cạnh phụ thuộc đối ứng. Việc thêm hoặc xóa một
cạnh cập nhật danh sách ở cả hai đầu khi đầu kia tồn tại; các tham chiếu tới
ID tác vụ sẽ được tạo trong tương lai vẫn được giữ lại. Thao tác thêm và xóa lặp lại có tính lũy đẳng.
Các thao tác xóa được áp dụng trước thao tác thêm, nên ID có trong cả hai danh sách sẽ tồn tại
sau khi áp dụng patch. `delete_task` xóa tác vụ và mọi tham chiếu tới nó,
chỉ trả về `True` khi một tác vụ đã bị xóa.

`claim_task` chỉ thành công một cách nguyên tử với tác vụ pending chưa có chủ sở hữu. Phương thức này ghi lại
chủ sở hữu được cung cấp và trả về tác vụ tách biệt; bản thân nó không đổi
trạng thái thành `in_progress`. Nếu chủ sở hữu khác giành được tác vụ, hoặc tác vụ không ở trạng thái pending hay
đã có chủ sở hữu, phương thức trả về `None`.

`ready_tasks` trả về các tác vụ pending chưa có chủ sở hữu theo thứ tự ID dạng số khi mọi ID trong
`blocked_by` đều tồn tại và có trạng thái `completed`. Thiếu một phụ thuộc sẽ khiến
tác vụ chưa sẵn sàng. `release_task` trả về `None` nếu tác vụ không tồn tại, trả về nguyên vẹn tác vụ đã hoàn thành,
và trong các trường hợp khác sẽ xóa chủ sở hữu, đồng thời đặt lại trạng thái thành
`pending`. Các lần nhận tác vụ đồng thời trong PostgreSQL chỉ có một bên thành công.

### Ảnh chụp khung nhìn provider tùy chọn

Kho lưu trữ có thể triển khai khả năng tùy chọn sau:

~~~text
async def save_provider_snapshot(
    id: str,
    snapshot: ProviderViewSnapshot,
) -> None

async def load_provider_snapshot(
    id: str,
) -> ProviderViewSnapshot | None
~~~

`ProviderViewSnapshot` chứa `provider_view: list[Message]`,
`covers_seq: int` và `projection_id: str | None = None` tùy chọn.
`covers_seq` là số thứ tự message cao nhất được khung nhìn biểu diễn, không phải
số lượng message. Schema định dạng truyền của nó là phiên bản `3`.
Việc lưu thực hiện upsert một khung nhìn provider đã được nén gọn và mốc message của nó; không bao giờ
thay đổi hoặc xóa các message chỉ được ghi nối tiếp. Việc tải trả về None khi không có ảnh chụp.

Việc tải lại session coi đây là cache. Nếu không có hàm tải, toàn bộ
lịch sử message được sử dụng. Nếu ảnh chụp được tải thành công và `covers_seq` của nó
nằm trong khoảng từ không đến số thứ tự message đã lưu cao nhất, các message có số thứ tự lớn hơn
sẽ được nối sau khung nhìn ảnh chụp. Mốc ngoài phạm vi hoặc
dãy số thứ tự message được tải không đơn điệu sẽ khiến hệ thống dùng lại toàn bộ lịch sử. Lỗi
thông thường của hàm tải cũng khiến hệ thống dùng lại toàn bộ lịch sử; lỗi do phiên bản định dạng truyền không được hỗ trợ thì được truyền ra ngoài.
Hàm lưu hoạt động theo cơ chế cố gắng tối đa đối với các lỗi thông thường. Hàm tải và hàm lưu được phát hiện
độc lập: hàm tải có thể khôi phục ảnh chụp hiện có mà không cần hàm lưu, và
hàm lưu có thể lưu các ảnh chụp trong tương lai mà không cần hàm tải.

### Inbox bền vững tùy chọn

`SessionInboxStore` bổ sung khả năng chèn đúng một lần cho các message dành cho
lượt sau:

~~~text
async def enqueue_inbox(
    id: str,
    delivery: InboxDelivery,
) -> None

async def commit_inbox(
    id: str,
    *,
    after_seq: int,
) -> list[StoredMessage]
~~~

`InboxDelivery` chứa `delivery_id` không rỗng, message có vai trò user,
`source` không rỗng mặc định là `"host"`, `metadata` tương thích JSON và giá trị băm
được tính từ những giá trị đó. `enqueue_inbox` yêu cầu session đã tồn tại.
Lặp lại cùng ID chuyển giao với cùng giá trị băm là thao tác không làm gì có tính lũy đẳng;
sử dụng ID đó với giá trị băm khác gây ra ConfigError.

`commit_inbox` yêu cầu `after_seq` là số nguyên không âm; `bool` không phải
số nguyên hợp lệ ở đây. Phương thức này xét các lần chuyển giao đang chờ theo thứ tự FIFO khi thêm vào hàng đợi, giải mã
tất cả message đang chờ trước khi chèn bất kỳ message nào và ghi nối tiếp toàn bộ chúng một cách nguyên tử
vào chuỗi message của session. Lỗi giải mã hoặc lưu trữ khiến toàn bộ
lô đang chờ chưa được commit. Kết quả chứa mọi message đã lưu có
`seq > after_seq`, bao gồm các message được commit bởi lần gọi trước và các message
được ghi nối tiếp trực tiếp, nên có thể thử lại an toàn nếu phản hồi bị mất. Kết quả đều
tách biệt. Sau khi commit thành công, biên nhận và giá trị băm vẫn được giữ lại, còn
các kho lưu trữ tích hợp sẵn xóa nội dung của các trường payload/source/metadata được lưu bền vững.

Session không tồn tại gây ra KeyError với cả hai phương thức inbox. Payload đã lưu bị hỏng
gây ra ConfigError, json.JSONDecodeError, KeyError, TypeError hoặc
ValueError từ bộ giải mã tùy trường hợp.

Khi `DurabilityOptions.durable_inbox` là true, cả `enqueue_inbox` và
`commit_inbox` đều bắt buộc; thiếu một phương thức sẽ gây ra `ConfigError` trước
khi thực thi chuyển giao. Tùy chọn này mặc định là `false`.

### Đặt trước session tường minh tùy chọn

Cả hai kho lưu trữ session tích hợp sẵn cũng cung cấp phương thức tiện ích sau, vốn không
bắt buộc theo protocol `SessionStore`:

~~~text
async def create_if_absent(
    *,
    id: str,
    meta: dict[str, object] | None = None,
) -> SessionRecord | None
~~~

Phương thức trả về bản ghi tách biệt mới được tạo hoặc `None` khi ID đã
tồn tại. PostgreSQL sử dụng một quyết định `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`
duy nhất; thao tác trong bộ nhớ không có điểm tạm dừng. Khả năng này được
sử dụng khi bên gọi phải đặt trước đích fork trước khi sao chép lịch sử.

## Ghi giai đoạn một cách nguyên tử

Lưu bền vững checkpoint mô tả một giai đoạn trước khi thực hiện thao tác
bên ngoài mà giai đoạn đó biểu diễn. Vòng lặp bền vững ghi checkpoint
`started` ban đầu bằng `create_run(initial_checkpoint=...)` trước khi bắt đầu
công việc với provider hoặc tool. Lệnh gọi provider, quyết định về quyền hoặc lô tool
đang chờ phải có checkpoint trước khi tạo ra tác động bên ngoài.

Sử dụng `save_checkpoint_with_events` khi một giai đoạn cũng tạo ra các event cần lưu. Phương thức này
commit checkpoint và phần đầu nhật ký event cùng nhau, gán số thứ tự event từ
cùng bộ đếm riêng cho từng run và thực hiện fencing cho cả hai phần bằng một lần kiểm tra lease.
`mark_failed` cung cấp thao tác nguyên tử tương ứng cho trạng thái thất bại, checkpoint
tùy chọn, metadata lỗi và lô event kết thúc. `mark_completed` lưu
checkpoint và trạng thái hoàn thành cuối cùng. Nếu việc tuần tự hóa hoặc lưu trữ thất bại,
thao tác giữ nguyên ranh giới bền vững trước đó.

`append_messages` của session và `commit_inbox` của inbox có cùng yêu cầu
hoặc thành công toàn bộ, hoặc không thay đổi gì. Khả năng `append_events` tùy chọn của run cũng có tính chất này khi
được triển khai; nếu không có, phương án dự phòng gọi `append_event` lặp lại
có thể ghi nối tiếp một phần trước khi lần gọi sau thất bại. Xác thực và mã hóa mọi đầu vào
trước khi thay đổi trạng thái; trong PostgreSQL, cấp bộ đếm số thứ tự và chèn
các hàng trong một giao dịch hoặc câu lệnh thay đổi dữ liệu.

## Lease và fencing tùy chọn

Cả hai kho lưu trữ run tích hợp sẵn đều triển khai khả năng lease tùy chọn. Các phương thức
chính xác của khả năng này là:

~~~text
async def claim(
    run_id: str,
    owner: str,
    ttl_s: float,
) -> RunLease

async def renew(lease: RunLease) -> RunLease

async def release(lease: RunLease) -> None
~~~

`RunLease` là bất biến và chứa `run_id`, `owner`, `epoch`, `expires_at`
(dấu thời gian Unix) và `ttl_s`. `ttl_s` phải dương và hữu hạn. `claim`
yêu cầu run đã tồn tại; ID không tồn tại gây ra `KeyError`. Nếu chủ sở hữu khác
đang giữ lease còn hiệu lực, phương thức gây ra `RunLeaseConflictError`. Lần gọi claim
của cùng chủ sở hữu khi lease còn hiệu lực sẽ làm mới thời hạn mà không đổi epoch. Claim sau khi hết hạn hoặc sau
khi release sẽ tăng epoch.

`renew` yêu cầu đúng chủ sở hữu và epoch hiện tại, cùng lease chưa
hết hạn. Phương thức giữ nguyên epoch và sử dụng ttl_s của token; token cũ, hết hạn hoặc
không khớp theo cách khác gây ra `LeaseLostError`. Chỉ việc hết hạn không ngăn chặn
lệnh ghi hoặc release có thông tin khớp: cho đến khi claim khác thay đổi epoch, chủ sở hữu
và epoch vẫn khớp. `release` xóa quyền sở hữu và tăng epoch, nên
token đã release không bao giờ dùng lại được. Release bằng token cũ không thay đổi gì.

Mọi thao tác thay đổi run có nhận `lease`, bao gồm ghi checkpoint, các lô
event, ghi kết thúc, tiếp quản run pending và thay đổi ảnh chụp
đầu vào mô hình, đều xác thực chủ sở hữu và epoch trong chính thao tác đó. Khi
chủ sở hữu khác claim run hoặc lease khớp được release, mọi lệnh ghi
sử dụng token cũ đều gây ra `LeaseLostError` và giữ nguyên trạng thái. Thao tác thay đổi
không có token vẫn là thao tác không có lease và có thể tiếp tục ngay cả khi token khác
còn hiệu lực; kho lưu trữ không thể suy ra quyền sở hữu từ lease của một thực thể kho lưu trữ khác.
Ứng dụng chủ phải truyền từng token đang hoạt động trong mọi thao tác thay đổi khi cần
bảo vệ quyền sở hữu.

~~~python
from linch import RunLeaseConflictError

async def handle(checkpoint):
    run = await runs.create_run("session-123", id="run-456")
    try:
        lease = await runs.claim(run.id, owner="worker-unique-id", ttl_s=30.0)
    except RunLeaseConflictError:
        return
    try:
        lease = await runs.renew(lease)
        await runs.save_checkpoint(run.id, checkpoint, lease=lease)
    finally:
        await runs.release(lease)
~~~

Ứng dụng chủ lên lịch heartbeat, dừng công việc sau khi mất lease và chỉ release
lease sau khi run đã ngừng hoạt động. Vòng lặp agent không khởi động
heartbeat. Các tool bên ngoài vẫn cần cơ chế lũy đẳng hoặc fencing riêng vì
lease của kho lưu trữ run không thể hoàn tác tác động bên ngoài đã hoàn thành.

## Ngữ nghĩa khi xảy ra lỗi

Ngoài lần thử lại admission có giới hạn đã mô tả ở trên, kho lưu trữ không âm
thầm thử lại các lệnh ghi của run và session.
Lỗi mã hóa, lỗi xác thực và lỗi lease cũ đều
tuân theo nguyên tắc fail-closed: trạng thái bền vững đích giữ nguyên. Lỗi cơ sở dữ liệu tạm thời
trong run bền vững đang hoạt động làm gián đoạn lần thử trước khi lưu trạng thái kết thúc,
giữ lại checkpoint bền vững gần nhất để thử lại hoặc tiếp quản. Nếu
`create_run` thất bại, ngoại lệ từ kho lưu trữ được truyền ra ngoài trước khi vòng lặp bắt đầu; không có
event run kết thúc nào được phát ra cho run chưa từng được tạo.

Resume run đã kết thúc hoặc run có trạng thái không có checkpoint có thể resume
sẽ gây ra `RunNotResumableError` kèm ID và trạng thái run, đồng thời không thực hiện
lệnh ghi nào với provider, tool, session hay thao tác ghi kết thúc nào vào kho lưu trữ. Run không tồn tại, chủ sở hữu
session khác, phiên bản định dạng truyền không tương thích hoặc hợp đồng run không tương thích
đều bị từ chối trước khi thực thi provider/tool.

Ảnh chụp khung nhìn provider là cache: lỗi tải thông thường sẽ dựng lại khung nhìn từ
lịch sử session chỉ ghi nối tiếp, còn lỗi lưu thông thường được ghi log mà không
thay đổi cuộc hội thoại. Việc ghi ảnh chụp chính xác đầu vào mô hình là thao tác lưu trữ
bền vững; request không hợp lệ và lỗi toàn vẹn không tạo ra ảnh chụp.
Việc xóa và cắt tỉa các ảnh chụp đầu vào mô hình đã hoàn thành là dọn dẹp theo cơ chế cố gắng tối đa:
lỗi thông thường được ghi log, còn `LeaseLostError` được truyền ra ngoài. Việc dọn dẹp
được bỏ qua sau khi mất lease.

## Kết nối lại bằng số thứ tự event đã lưu

Mỗi event run đã lưu có số thứ tự tăng đơn điệu, bắt đầu từ 1, riêng
cho run của nó. Ghi nối tiếp từng event, ghi nối tiếp theo lô, ghi checkpoint kèm event
và ghi kết thúc thất bại đều dùng chung bộ đếm. Đọc sau con trỏ
loại trừ và chỉ tăng con trỏ sau khi client đã chấp nhận hàng:

~~~python
cursor = last_delivered_seq  # 0 for a new connection
for row in await runs.load_events(run_id, after_seq=cursor):
    await publish({"seq": row.seq, "event": row.event})
    cursor = row.seq
~~~

Các hàng được trả về theo số thứ tự tăng dần. Lưu con trỏ sau khi
chuyển giao; khi chưa chắc đã nhận được xác nhận, khử trùng lặp bằng (run_id, seq).
Thông báo hoặc tín hiệu đánh thức hàng đợi chỉ là gợi ý—nhật ký event có thứ tự và
con trỏ của nó là nguồn để khôi phục.

## Tuân thủ quy ước backend

Các kiểm thử lưu trữ kiểm tra giá trị tách biệt, tính nguyên tử của checkpoint/event,
việc cấp số thứ tự, ghi kết thúc, con trỏ kết nối lại, kiểm tra schema,
ảnh chụp provider, hành vi inbox bền vững, phụ thuộc tác vụ và fencing
bằng lease. Các kiểm thử Postgres yêu cầu asyncpg và AGENT_KIT_TEST_PG_DSN; nếu không có
biến môi trường đó, chúng bỏ qua phần kiểm thử với cơ sở dữ liệu thật.

~~~bash
AGENT_KIT_TEST_PG_DSN=postgresql://user:password@localhost/linch_test \
  PYTHONPATH=src \
  python -m pytest \
  tests/storage/test_run_store_conformance.py \
  tests/storage/test_run_store_leases.py \
  tests/storage/test_sessions.py \
  tests/storage/test_model_input_snapshots.py \
  tests/storage/test_durable_inbox.py \
  tests/storage/test_provider_snapshot.py -q
~~~
