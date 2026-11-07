# Stores

[Bản gốc tiếng Anh](../../architecture/stores.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Trạng thái bền vững nằm sau bốn trait: `RunStore`, `SessionStore`,
`TaskStore` (đều trong `crates/linch-core/src/store.rs` và `store/tasks.rs`) và
`FileBackend` (`crates/linch-core/src/vfs.rs`). Mỗi trait có một bản triển khai trong
bộ nhớ cho test và phát triển, một bản Postgres trong `linch-postgres`, và một bộ kiểm
tra tuân thủ (conformance suite) mà mọi bản triển khai đều phải đạt. Dùng trang này khi
bạn viết một store, đổi một đường ghi hoặc một migration, hoặc định cỡ một pool.

Linch bản Python mô tả cùng mô hình ở
[run bền vững, lease và checkpoint](../../../../docs/architecture/subsystems.md#33-durable-runs-leases-and-checkpoints).
Điểm khác trong Rust: một lời gọi `commit` là cách ghi duy nhất vào một run đang chạy,
Postgres là backend bền vững duy nhất, và các lần ghi Postgres trên đường nóng là các
câu lệnh đơn.

## Khóa và đa tenant

Mỗi bản ghi được khóa bằng một namespace do host chọn cộng với một id, nên các tenant
không bao giờ dùng chung không gian khóa (`store.rs:3-4`). Namespace của một agent
(`AgentBuilder::namespace`) là namespace của các run và session của nó;
`task_tools` nhận namespace của các session mà nó phục vụ.

## Run store

`RunStore` (`crates/linch-core/src/store.rs:163`):

| Method | Hợp đồng |
|---|---|
| `create(NewRun)` | Một lần ghi: hàng run ở trạng thái `pending`, epoch 0, cùng checkpoint đầu tiên và các event của nó (seq bắt đầu từ 1). `Conflict` khi id đã tồn tại; `SessionBusy` khi namespace đã có một run chưa terminal của session đó. Việc kiểm tra và việc chèn là một bước nguyên tử (`store.rs:164-171`) |
| `claim(namespace, run_id, owner)` | Nhận quyền sở hữu: epoch + 1, trạng thái `running`, một `Lease`. Là cách duy nhất để sang `running`, kể cả từ `awaiting_*`. Bị từ chối với một run đã terminal |
| `load` | `RunRecord` hiện tại |
| `commit(&Lease, Commit)` | Một lần ghi nguyên tử gồm các event (seq nối tiếp `last_seq` không có khoảng trống), một checkpoint tùy chọn và một trạng thái tùy chọn. Chỉ được chấp nhận trên một run `running` dưới epoch hiện tại. Một trạng thái terminal là cuối cùng |
| `events(namespace, run_id, after_seq)` | Các event đã commit sau `after_seq`, theo thứ tự |

`StoreError` (`store.rs:104`) đặt tên cho từng lần từ chối: `NotFound`, `LeaseLost`,
`Conflict`, `SessionBusy`, `SeqGap`, `InvalidTransition` và `Backend`. Một lần ghi bị
từ chối không thay đổi gì.

### Một run đang chạy cho mỗi session

Một session có tối đa một run chưa terminal, trên mỗi namespace. Store từ chối lần
`create` thứ hai trước khi bất cứ thứ gì của nó được ghi, nên hai run không bao giờ xen
kẽ trong một session log (`crates/linch-core/src/runner.rs:30-34`).
`SessionBusy` mang theo id của run đang chạy: hãy resume, approve hoặc fail nó trước.
Các trạng thái terminal là `completed`, `failed` và `aborted`
(`crates/linch-core/src/run.rs:56`).

### Fencing

`claim` luôn thắng và cộng 1 vào epoch; mọi `commit` sau đó dưới một lease cũ hơn đều
thất bại với `LeaseLost`. Không có hết hạn và không có heartbeat trong store: một worker
đang giữ một run bị fence bởi lần claim kế tiếp, không bao giờ bởi một timeout. Worker
thêm các visibility timeout lên trên
([worker](./worker.md#lease-và-fencing)).

## Session store

`SessionStore` (`store.rs:199`) giữ session log chỉ nối thêm:

| Method | Hợp đồng |
|---|---|
| `create(namespace, session_id)` | Một log rỗng; `Conflict` nếu nó đã tồn tại |
| `load` | Mọi entry, theo thứ tự |
| `append(namespace, session_id, expected_len, entries)` | Chỉ nối thêm nếu log có đúng `expected_len` entry, nếu không thì `Conflict`; trả về độ dài mới |

`expected_len` là khóa lạc quan: trong hai lần append ở cùng một độ dài,
một lần thành công và lần kia nhận `Conflict`.

## Task store

`TaskStore` (`crates/linch-core/src/store/tasks.rs:186`) giữ các task của từng session,
một đồ thị phụ thuộc nhỏ nằm sau các task tool. Các session store triển khai nó. Id là
các số thập phân được cấp theo từng session bắt đầu từ 1 và không bao giờ được dùng lại.
Một cạnh "`a` chặn `b`" có thể nêu tên một task chưa tồn tại; nó hiện ra khi task đó được
tạo, và xóa một task sẽ gỡ mọi cạnh nêu tên nó. `claim_task` có đúng một bên thắng trong
các lần claim đồng thời (`tasks.rs:236`); `ready_tasks` liệt kê các task đang pending,
chưa có chủ, mà mọi task chặn nó đều tồn tại và đã completed (`tasks.rs:255`). Task là
dữ liệu của session: chỉ các lời gọi tool và kết quả của chúng đi vào session log.

## Các bộ kiểm tra tuân thủ

| Suite | Cho | Ở đâu |
|---|---|---|
| `run_store_suite` | `RunStore` | `crates/linch-core/src/store/conformance.rs:33` |
| `run_admission_suite` | `RunStore` (một run đang chạy cho mỗi session) | `store/conformance/admission.rs:12` |
| `session_store_suite` | `SessionStore` | `store/conformance.rs:42` |
| `task_store_suite` | `SessionStore + TaskStore` | `store/conformance/tasks.rs:14` |
| `backend_suite` | `FileBackend` | `crates/linch-core/src/vfs/conformance.rs:11` |

Một quy tắc thất bại sẽ panic với một thông báo nêu tên nó, nên hãy gọi các suite từ một
test. Mỗi kiểm tra dùng namespace riêng của nó dưới `conformance/`; hãy chạy chúng trên
một store không chứa gì ở đó. Vòng đời của run mà chúng cố định
(`store/conformance.rs:10-16`): `create` lưu một run `pending` ở epoch 0;
`claim` là cách duy nhất để sang `running` và cộng 1 vào epoch; `commit` chỉ được chấp
nhận trên một run `running` dưới epoch hiện tại; một trạng thái terminal là cuối cùng,
và một commit bị từ chối không thay đổi gì.

## Store trong bộ nhớ

`MemoryRunStore` và `MemorySessionStore`
(`crates/linch-core/src/store/memory.rs:45`, `memory.rs:227`) theo cùng vòng đời và đạt
cùng các suite. Chúng đếm mọi lần ghi thành công, theo từng method (`writes()`,
`memory.rs:68`, `memory.rs:246`), để một test hoặc bench có thể khẳng định số lần ghi
store cho mỗi lượt; các lần ghi bị từ chối và các lần ghi task không được đếm.
`store::faulty::Faulty` (`crates/linch-core/src/store/faulty.rs:43`)
bọc một trong hai và làm lần ghi thứ `n` thất bại, trước hoặc sau khi nó được ghi, cho
các test sự cố.

<a id="postgres-stores"></a>

## Store Postgres

`linch-postgres` triển khai `PgRunStore`, `PgSessionStore` (cũng là
`TaskStore`) và `PgFileBackend`. Tất cả đều nhận một `PgPool`.

### Schema và migration

`migrate(&pool)` (`crates/linch-postgres/src/lib.rs:227`) tạo hoặc nâng cấp
các bảng. Nó an toàn ở mỗi lần khởi động: các migration đã áp dụng bị bỏ qua và các bên
gọi đồng thời được tuần tự hóa bằng một advisory lock. Các phiên bản đã áp dụng được ghi
trong `linch_migrations` (`lib.rs:62`), không phải bảng mặc định của sqlx, nên các
migration sqlx riêng của host trong cùng schema không va chạm. Các bảng không có tiền tố
schema và bắt đầu bằng `linch_`, nên `search_path` của kết nối chọn schema. Các truy vấn
được dựng lúc chạy: build crate không cần cơ sở dữ liệu.

| Migration | Thêm |
|---|---|
| `0001_stores.sql` | `linch_runs`, `linch_run_events`, `linch_sessions`, `linch_session_entries` |
| `0002_one_live_run_per_session.sql` | Chỉ mục unique một phần `linch_runs_one_live_per_session` trên `(namespace, session_id)` khi trạng thái chưa terminal |
| `0003_files.sql` | `linch_files`, với `path` ở dạng `COLLATE "C"` để khóa chính sắp xếp theo byte và phục vụ quét theo tiền tố |
| `0004_tasks.sql` | `linch_sessions.next_task_id`, `linch_tasks`, `linch_task_edges` |
| `0005_mailbox.sql` | `linch_mailbox` (`PgMailbox`: message giữa các peer theo namespace và người nhận, được một lần drain giữ lease, bị xóa bởi ack của nó) |

Payload JSON (checkpoint, envelope, log entry, metadata của task) là `TEXT`, không phải
`JSONB`: JSONB sắp xếp lại khóa và viết lại số, mà các store phải trả về đúng những gì đã
ghi, vì byte của message đi vào prompt cache và digest
(`migrations/0001_stores.sql:1-3`). Các hàng được giải mã nghiêm ngặt; một hàng không giải
mã được là một lỗi (`crates/linch-postgres/src/codec.rs:1-4`).
Migration chỉ được nối thêm: thêm một tệp mới và một mục `Migration::new`
(`lib.rs:169-211`), không bao giờ sửa một migration đã áp dụng, vì `migrate` thất bại khi
một migration đã áp dụng khác với bản nhúng.

### Đường ghi

| Ghi | Hình dạng |
|---|---|
| `commit` của run | Một câu lệnh autocommit (`crates/linch-postgres/src/run.rs:72-82`): một `UPDATE` chỉ di chuyển hàng run dưới epoch của lease, khi đang `running`, và khi `last_seq` đúng là giá trị mà các event nối tiếp; các event được chèn từ hàng đã di chuyển, nên chúng được ghi cùng với nó hoặc không được ghi |
| `append` của session | Một câu lệnh autocommit (`crates/linch-postgres/src/session.rs:17-24`): độ dài chỉ di chuyển từ `expected_len`, và các entry được chèn từ hàng đã di chuyển tại các vị trí `expected_len + n - 1` |
| `create` của run | Một transaction: chèn hàng với `ON CONFLICT (namespace, run_id) DO NOTHING`, rồi đến các event đầu tiên (`run.rs:182`) |
| `claim` của run | Một transaction khóa hàng bằng `FOR UPDATE`, rồi đặt trạng thái, epoch và owner (`run.rs:259`) |
| Ghi task | Một transaction khóa hàng session trước, nên các lần ghi task của một session được tuần tự hóa: id ra theo thứ tự không có khoảng trống và một lần claim có một bên thắng (`crates/linch-postgres/src/tasks.rs:1-8`) |
| `edit` của file | Một transaction khóa hàng file bằng `FOR UPDATE` (`crates/linch-postgres/src/files.rs:1-6`) |

Các lần ghi nhiều câu lệnh bắt đầu ở `READ COMMITTED` bất kể mặc định của server hay
role, vì chúng phải đọc lại một hàng sau khi chờ một bên ghi khác (`lib.rs:148`). Các lần
ghi một câu lệnh chạy ở autocommit với mức cô lập mặc định của pool; trên
`READ COMMITTED`, một lỗi serialization (`40001`) không ghi gì và được thử lại, tối đa ba
lần thử (`lib.rs:153-166`).

Việc nhận run (admission) được thực thi bởi chỉ mục unique một phần, không phải bởi một
lần đọc. Một vi phạm `linch_runs_one_live_per_session` đọc id của run đang chạy và trả về
`SessionBusy`; nếu run đó đã kết thúc trong khoảng giữa, việc chèn được thử lại, tối đa ba
lần (`run.rs:128-152`, `run.rs:222-244`).

### Phân loại từ chối

Khi câu lệnh của một commit không di chuyển hàng nào, store đọc hàng thêm một lần và đặt
tên cho lần từ chối theo cùng thứ tự với store trong bộ nhớ (`run.rs:86-102`,
`codec.rs:120`):

1. không có hàng: `NotFound`;
2. epoch khác: `LeaseLost`;
3. trạng thái không phải `running`, hoặc một commit sang `pending`: `InvalidTransition`;
4. theo từng event, theo thứ tự: một event của run khác là `Conflict`, một seq không
   nối tiếp là `SeqGap`.

Nếu lần đọc không thấy gì sai, hàng đã di chuyển sau lần ghi bị từ chối và commit được
thử lại. Sau ba lần thử thì là `Conflict` ("kept changing during a commit")
(`run.rs:320-354`). Một commit mà không hàng nào có thể nhận (trạng thái `pending`, một
event của run khác, hoặc các seq không nối tiếp nhau) bỏ qua câu lệnh và đi thẳng tới lần
đọc. Một lần append của session đọc độ dài theo cách tương tự: `NotFound`, hoặc `Conflict`
nêu cả hai độ dài (`session.rs:146-168`).

### Kết nối và định cỡ pool

`linch_postgres::connect(dsn, timeout, max_connections)` (`lib.rs:104`) giới hạn thời gian
của kết nối đầu tiên (mặc định `CONNECT_TIMEOUT`, 5 s); một mình sqlx sẽ thử lại một kết
nối bị từ chối trong suốt acquire timeout 30 s của nó. Quá giới hạn đó, lỗi nêu tên host
và port (hoặc socket) mà nó đã thử. Không thông báo lỗi nào chứa DSN.

Pool chỉ ping một kết nối trước khi giao nó ra khi kết nối đó đã nhàn rỗi từ
`IDLE_PING_AFTER` (1 s) trở lên, và thay nó khi ping thất bại
(`before_acquire`, `lib.rs:119-128`). sqlx không theo dõi một socket đang nhàn rỗi, nên
nếu không có ping, một kết nối mà server đã đóng khi nhàn rỗi (khởi động lại, failover,
`idle_session_timeout`, một proxy cắt kết nối nhàn rỗi) sẽ làm lần ghi kế tiếp trên nó
thất bại. Sau một lời gọi model, lần ghi đó là receipt: journal sẽ bị nhiễm lỗi, và lần
resume sẽ không thấy receipt nào được chấp nhận và trả phí cho lời gọi lần nữa. Các lần
ghi nối tiếp nhau sát sao, chẳng hạn các lần ghi barrier của một run, bỏ qua ping, nên một
commit của run hoặc một append của session vẫn là một round trip; ping của chính sqlx
trước mỗi lần acquire (`test_before_acquire`, ở đây đang tắt) sẽ biến mỗi lần thành hai.
Một lần ghi sau một khoảng nghỉ từ 1 s trở lên, như receipt sau một lời gọi model, trả
thêm một round trip. Một kết nối mà server đóng trong vòng một giây sau lần dùng cuối vẫn
làm lần ghi kế tiếp thất bại, và run dừng ở trạng thái bền vững cuối cùng của nó. Idle
timeout và vòng đời của sqlx vẫn tái chế các kết nối.

`connect` giữ các mặc định khác của sqlx. Kích thước mặc định của nó,
`default_max_connections()` (`crates/linch-postgres/src/pool_size.rs:26`), là số core của
máy được kẹp giữa `MIN_DEFAULT_CONNECTIONS` (10, mặc định của chính sqlx) và
`MAX_DEFAULT_CONNECTIONS` (32), tính một lần cho mỗi process. Mức sàn giữ một máy nhỏ ở
khoảng năm run commit cùng lúc. Mức trần dành cho cơ sở dữ liệu: `max_connections` của
chính Postgres (100 nếu không đặt) được dùng chung bởi mọi pool của mọi process trên nó,
nên ba process ở mặc định vẫn vừa. Trong bench case 14 (2000 session, store Postgres),
pool 10 là nút thắt: 3355 ms overhead cho mỗi lượt, so với 382 ms với 40 kết nối; với
mặc định 32 trên máy 64 core là 646 ms (3914 ms với 10, trong cùng một lần chạy liên
tiếp). Một cấu hình facade đặt kích thước riêng bằng `stores.max_connections`; trên
`linch-server`, một tenant được yêu cầu tối đa `max_pool_connections` của nó
([rpc](./rpc.md)). sqlx
cũng ping một kết nối trong nền khi nó được trả lại (sqlx-core
0.9.0, `src/pool/connection.rs:314`), nên một run commit liên tiếp sẽ luân phiên giữa
hai kết nối. Hãy định cỡ một pool vào khoảng hai kết nối cho mỗi run commit đồng thời.
Một `Worker` cần thêm một kết nối cho kết nối `LISTEN` của nó
(`crates/linch-worker/src/worker.rs:87-90`). Ví dụ bên dưới định cỡ pool của nó theo cách
đó bằng `connect`.

<a id="group-commit-write-counts"></a>

## Số lần ghi của group commit

Run journal (`crates/linch-core/src/journal.rs`) giữ các event và checkpoint trong bộ nhớ
và ghi chúng trong một `commit` tại mỗi barrier: trước khi gửi tới provider, quanh mỗi
tool thực sự chạy, trước mỗi lần ghi session, và lúc kết thúc. Một checkpoint được giữ lại
chỉ bao phủ các event đang chờ khi nó được lưu, nên mọi trạng thái đã commit đều là trạng
thái mà việc ghi write-through từng bước cũng sẽ tạo ra (`journal.rs:1-31`).

Một lượt tool với `k` tool đã bắt đầu tốn `3 + 2k` lần ghi run và 2 lần ghi session
(`crates/linch-core/src/runner.rs:36-37`). Con số này được cố định bởi
`a_tool_turn_costs_three_plus_two_k_run_writes_and_two_session_writes`
(`crates/linch-core/src/journal/tests/turn.rs:116`):

| Bước | Ghi run | Ghi session |
|---|---|---|
| `ProviderPending` được giữ lại; event delivery và checkpoint đó được flush trước khi dispatch | 1 | |
| `AssistantReceived` ghi write-through cùng receipt, các event `Assistant` và `Usage` | 1 | |
| Response được nối vào session (không có gì đang giữ, nên không có lần ghi barrier) | | 1 |
| `ToolBatch` được giữ lại; mỗi tool đã bắt đầu flush phần bắt đầu của nó trước khi chạy (tool đầu tiên cũng mang `ToolBatch`) và phần kết thúc sau đó | 2k | |
| `ToolResults` được giữ lại; `flush_checkpoint` ghi nó trước khi các kết quả được nối | 1 | |
| Các kết quả được nối vào session | | 1 |

Một lời gọi không chạy gì (bị từ chối, được hook phục vụ, bị abort trước khi bắt đầu)
không thêm barrier: phần bắt đầu và kết thúc của nó đi cùng lần ghi kế tiếp. Một lượt cuối
chỉ có văn bản kết thúc bằng một lần ghi terminal duy nhất mang theo các event đang chờ,
`Result` và trạng thái cùng nhau (`journal.rs:233-238`).

## Ví dụ

Đoạn này biên dịch được ở revision này; `main` chạy mọi suite trên các store trong bộ
nhớ, còn hai hàm kia cần một cơ sở dữ liệu:

```rust
use std::error::Error;
use std::num::NonZeroU32;

use linch_core::store::conformance::{
    run_admission_suite, run_store_suite, session_store_suite, task_store_suite,
};
use linch_core::vfs::conformance::backend_suite;
use linch_core::{MemoryRunStore, MemorySessionStore};
use linch_postgres::{PgFileBackend, PgPool, PgRunStore, PgSessionStore};

/// A pool for `runs` runs committing at once: about two connections each.
async fn pool(dsn: &str, runs: NonZeroU32) -> Result<PgPool, sqlx::Error> {
    let max = runs.saturating_mul(NonZeroU32::new(2).unwrap());
    let pool = linch_postgres::connect(dsn, linch_postgres::CONNECT_TIMEOUT, max).await?;
    linch_postgres::migrate(&pool).await?;
    Ok(pool)
}

/// Every suite a store backend must pass, here run on Postgres.
async fn check_postgres(dsn: &str) -> Result<(), Box<dyn Error>> {
    let (timeout, max) = (linch_postgres::CONNECT_TIMEOUT, linch_postgres::default_max_connections());
    let pool = linch_postgres::connect(dsn, timeout, max).await?;
    linch_postgres::migrate(&pool).await?;
    let runs = PgRunStore::new(pool.clone());
    run_store_suite(&runs).await;
    run_admission_suite(&runs).await;
    let sessions = PgSessionStore::new(pool.clone());
    session_store_suite(&sessions).await;
    task_store_suite(&sessions).await;
    backend_suite(&PgFileBackend::new(pool, "conformance/files")).await;
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let runs = MemoryRunStore::new();
    run_store_suite(&runs).await;
    run_admission_suite(&runs).await;
    let sessions = MemorySessionStore::new();
    session_store_suite(&sessions).await;
    task_store_suite(&sessions).await;
    backend_suite(&linch_tools::vfs::MemoryBackend::new()).await;
    let _ = (pool, check_postgres); // need a database
}
```

Các test Postgres (`crates/linch-postgres/tests/pg/`) chạy các suite này cùng các
trường hợp từ chối, admission, sự cố và cô lập, mỗi test trong schema riêng của nó.
Mỗi test bị bỏ qua khi `LINCH_TEST_PG_DSN` chưa được đặt.
Mỗi test mở khoảng sáu kết nối, nên trên máy có từ 16 core trở lên hãy chạy với
`RUST_TEST_THREADS=8` để không vượt `max_connections` 100 của Postgres.

---

Quay lại [mục lục kiến trúc](./README.md).
