# Worker

[Bản gốc tiếng Anh](../../architecture/worker.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

`linch-worker` (`crates/linch-worker/src/`) là hàng đợi job Postgres, nhóm worker xử lý
hàng đợi đó, và các lịch cron cấp việc cho nó. Một host đưa việc vào hàng đợi; số lượng
`Worker` tùy ý, trong số lượng process tùy ý, nhận các job và điều khiển các run qua API
công khai `Agent` (`run`, `resume`, `approve`). Dùng trang này khi bạn đổi cách các job
được nhận, giữ, thử lại hoặc bị đánh dấu thất bại.

Linch bản Python chạy nhóm worker của nó trên PgQueuer
([phân phối worker](../../../../docs/architecture/subsystems.md#34-worker-dispatch-and-pgqueuer-recovery)).
Hàng đợi Rust là một bảng thường được dequeue bằng `FOR UPDATE SKIP LOCKED` và
được đánh thức bằng `LISTEN/NOTIFY`. Nó giữ các giá trị mặc định của PgQueuer cho ngưỡng
thất bại và độ trễ retry dài nhất (`crates/linch-worker/src/settle.rs:14-20`).

## Bảng

`linch_worker::migrate(&pool)` (`crates/linch-worker/src/lib.rs:135`) tạo
các bảng, ghi các phiên bản trong `linch_worker_migrations` để chúng không bao giờ
va chạm với của `linch_postgres`. Hãy chạy `migrate` của cả hai crate khi khởi động. Mọi
thời điểm đều là đồng hồ của cơ sở dữ liệu (`now()`), không bao giờ là của một worker.

| Migration | Thêm |
|---|---|
| `0001_worker.sql` | `linch_worker_jobs` (một hàng cho mỗi job) và `linch_worker_schedules` |
| `0002_one_job_per_run.sql` | Chỉ mục unique một phần `linch_worker_jobs_one_per_run` trên `(namespace, run_id)` khi `failed_at IS NULL` |
| `0003_job_failures.sql` | `failures`, được đếm tách biệt với `attempts` |
| `0004_queue_limits.sql` | `linch_worker_queues` (`name`, `max_held`) |

Một job bắt đầu một run (`session_id` và `prompt`), tiếp tục một run (`run_id`), hoặc
approve một run (`run_id` và `approvals`); các check constraint giữ những hình dạng đó
(`migrations/0001_worker.sql:13-31`). Một job đã xong bị xóa; một job thất bại giữ hàng và
lỗi của nó.

## Đưa job vào hàng đợi

| Lời gọi | Job | Ghi chú |
|---|---|---|
| `enqueue_run(pool, queue, namespace, session_id, prompt)` | start | Worker ghi run id vào job trước lần ghi đầu tiên của run, nên một lần retry sau sự cố tiếp tục run đó (`crates/linch-worker/src/job.rs:129-163`) |
| `enqueue_resume(pool, queue, namespace, run_id)` | continue | Khi run đã có một job đang hoạt động, id của job đó được trả về thay vì một job mới (`job.rs:164-196`) |
| `enqueue_approve(pool, queue, namespace, run_id, approvals)` | approve | Các approval được lưu ở dạng wire `ApprovalResponse` của lõi. `WorkerError::RunBusy` khi run có một job đang hoạt động, job này có thể kết thúc mà không đọc chúng; hãy đưa vào hàng đợi lại khi nó đã kết thúc (`job.rs:197-239`) |

Mỗi lần chèn và `pg_notify` của nó trên kênh `linch_worker` (`lib.rs:76`) chạy trong một
transaction, nên tín hiệu đánh thức được gửi khi job được commit
(`job.rs:49-109`). Một job nêu tên một run phải nhường cho job đang hoạt động của run đó;
một job start phải nhường cho một job trước đó có cùng dedupe key.

## Một job đang hoạt động cho mỗi run

Hai job trên cùng một run sẽ giành claim run từ nhau. Chỉ mục unique cho phép tối đa một
job cho mỗi run chưa thất bại. Một job start chỉ nêu tên run của nó khi nó ghi lại run
đó, nên trước đó nó không bao giờ xung đột
(`migrations/0002_one_job_per_run.sql`).

## Vòng lặp worker

`WorkerConfig::new(owner)` (`crates/linch-worker/src/worker.rs:53`):

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `owner` | bắt buộc | Được ghi cùng mỗi lần claim run và giữ job; duy nhất cho mỗi worker |
| `queues` | `["default"]` | Lần lượt phục vụ một job từ mỗi hàng đợi |
| `max_jobs` | 32 | Số job tối đa mà worker này chạy cùng lúc |
| `visibility_timeout` | 30 s | Thời gian một job đã dequeue vẫn bị ẩn khi không có heartbeat |
| `poll_interval` | 1 s | Thời gian chờ tối đa giữa các lần poll khi không có thông báo |
| `retry_delay` | 5 s | Thời gian chờ trước lần thử thứ hai của một job; tăng gấp đôi ở mỗi lần thử |

`Worker::new(pool, config).agent(agent)` phục vụ các job của
`agent.namespace()`; thêm một agent cho mỗi namespace. Mọi worker phục vụ một
namespace cần một agent có cùng contract, và mọi agent phải lưu các run của nó qua
`PgRunStore` trong cơ sở dữ liệu và schema của pool worker, vì
worker đọc các bản ghi run ở đó (`worker.rs:84-90`). Pool cần chỗ cho
một kết nối `LISTEN` cộng với các truy vấn job.

`Worker::run(shutdown)` (`worker.rs:128`):

1. kiểm tra cấu hình và mở kết nối `LISTEN`; một lần nhận thất bại
   sẽ quay về polling và thử lại sau 1 s (`worker.rs:224-234`);
2. kích hoạt các lịch đến hạn, tối đa một lần cho mỗi `poll_interval` dù có bao nhiêu
   thông báo đánh thức nó (`worker.rs:146-152`);
3. lần lượt nhận một job từ mỗi hàng đợi cho đến khi nó giữ `max_jobs` hoặc mọi
   hàng đợi đều rỗng, và chạy mỗi job trong task riêng của nó (`worker.rs:173-212`);
4. chờ một thông báo, một job vừa xong, khoảng poll hoặc `shutdown`.

Khi `shutdown`, nó chờ các job đang chạy. Thay vào đó, drop future sẽ bỏ rơi chúng như
một sự cố: các run của chúng vẫn resume được và các job hiện lại sau visibility timeout.
Lỗi cơ sở dữ liệu bên trong vòng lặp được ghi log và thử lại ở lần poll kế tiếp.

### Giới hạn đồng thời

`set_queue_limit(pool, queue, limit)` (`crates/linch-worker/src/lease.rs:107`)
lưu giới hạn của một hàng đợi cùng với hàng đợi đó, nên mọi worker áp dụng cùng một giới
hạn; 0 nghĩa là không có. Một dequeue trên hàng đợi có giới hạn lấy một advisory lock phạm
vi transaction cho hàng đợi đó, đếm các job đang được giữ bởi các worker còn sống ở bất kỳ
đâu (có chủ, vẫn đang ẩn, chưa thất bại), và không lấy gì khi đã tới giới hạn
(`lease.rs:134-157`). Các job đang được giữ khi giới hạn bị hạ xuống vẫn tiếp tục chạy.

## Lease và fencing

Hai lớp fence bảo vệ một run, mỗi lớp một tầng:

- **Job.** Một dequeue (`lease.rs:125`) lấy job hiển thị lâu nhất của hàng đợi trong các
  namespace của worker bằng `FOR UPDATE SKIP LOCKED`, ẩn nó trong
  visibility timeout, đặt `owner` và cộng 1 vào `attempts`. Giá trị
  `attempts` đó là fencing token: heartbeat, record-run, ack, release
  và fail đều khớp trên nó, nên một worker đã mất job không thay đổi gì
  (`lease.rs:1-6`). Mỗi thao tác là một câu lệnh autocommit ở mức cô lập mặc định của
  pool. Trên `READ COMMITTED`, một thao tác đã chờ một bản cập nhật khác của hàng job
  (heartbeat của chính lần thử đó vẫn đang bay khi run kết thúc, hoặc dequeue của một
  worker khác) sẽ thất bại với lỗi serialization
  (`40001`) khi bản cập nhật đó commit; nó không ghi gì và chạy lại, tối đa
  ba lần thử (`lease.rs:57-87`).
- **Run.** Điều khiển run sẽ claim nó trong run store, việc này cộng 1 vào epoch của nó và
  fence mọi bên giữ trước đó
  ([stores](./stores.md#fencing)).

Trong khi run stream, `keep_alive` gia hạn visibility của job mỗi một phần ba
timeout (`crates/linch-worker/src/execute.rs:151-172`). Job bị coi là mất
khi một heartbeat thấy một bên giữ khác, hoặc khi không có lần gia hạn nào thành công
trong cả một timeout kể từ lần được xác nhận gần nhất. Mất job làm run stream bị drop,
việc này là một sự cố đối với run: nó vẫn resume được, và lần claim của worker kế tiếp
fence worker này ra theo epoch. Worker không bao giờ hủy token của run vì lý do
này, vì một lần hủy kết thúc run với trạng thái `aborted` (`execute.rs:5-9`).

### Chọn run, resume hay approve

`open` (`execute.rs:74-104`) quyết định từ job và bản ghi bền vững của run:

| Job và run | Hành động |
|---|---|
| Job start, chưa có run (hoặc lần create của nó chưa bao giờ được ghi) | `Agent::run`; run id được ghi vào job trước |
| Run đã terminal | Không làm gì; job xong |
| Job approve, run đang suspend để chờ phê duyệt, hoặc lần thử đầu tiên | `Agent::approve`; trên một run không suspend, lõi từ chối nó (`NotSuspended`) và job thất bại |
| Job approve, lần thử sau, run không còn suspend | `Agent::resume`: một lần thử trước đã commit các câu trả lời (được ghi log dưới dạng cảnh báo) |
| Job resume, run đang suspend để chờ phê duyệt | Không làm gì; run chờ một job approve |
| Job resume, run không suspend | `Agent::resume` |

"Suspended" là phase của checkpoint, không phải trạng thái: một approve bị sự cố giữa
claim và commit của nó để lại trạng thái `running`
(`execute.rs:116-118`). Một run suspend để chờ phê duyệt kết thúc job của nó;
`enqueue_approve` đưa phần tiếp tục vào hàng đợi.

## Kết quả, back-off và dead letter

Mỗi lần thử kết thúc bằng một `Outcome` (`crates/linch-worker/src/outcome.rs:9-32`),
được `settle` (`settle.rs:32`) áp dụng:

| Outcome | Đến từ | Job |
|---|---|---|
| `Done` | Stream kết thúc ở `Result` hoặc `Suspended`; run đã terminal | Bị xóa |
| `Retry` | Một lỗi store hoặc journal, kể cả xung đột append của session; không có agent cho namespace | Được release với back-off, tính một lần thất bại |
| `Busy` | Một job start mà session của nó có một run đang chạy (`SessionBusy`) | Được release với back-off, không tính thất bại |
| `Fail` | Một approve muộn (`NotSuspended`), một prompt bị chặn, approval không giải mã được, một run bị thiếu | Bị đánh dấu thất bại vĩnh viễn |
| `FailRun` | Trạng thái của run không reconcile được, hoặc contract của nó khác với của agent | Run bị đánh dấu thất bại qua `Agent::fail`, rồi đến job |
| `Fenced` | `LeaseLost`: một owner khác đã claim run | Bị xóa không retry, vì retry sẽ lấy lại run |
| `Lost` | Một worker khác giữ job | Để nguyên |

`classify` (`outcome.rs:44`) ánh xạ một `RunError` vào các giá trị này. Độ trễ sau
lần thử `n` là `retry_delay * 2^(n-1)`, tối đa 5 phút
(`settle.rs:24-29`). Sau năm lần thử thất bại, job bị đánh dấu thất bại vĩnh viễn,
và run của nó cũng vậy khi job nêu tên một run (`settle.rs:77-102`). Chờ một
session đang bận không bao giờ được tính, nên một job start không bao giờ bị bỏ cuộc vì lý do đó.

Một job thất bại là dead letter: `failed_at` và `error` được đặt, nó không bao giờ được
dequeue lại, và nó không còn được tính vào chỉ mục một-job-cho-mỗi-run hay
giới hạn của hàng đợi (`lease.rs:288-302`). `error` giữ cả chuỗi lỗi
(`crates/linch-worker/src/error.rs:40-49`). Khi một run bị đánh dấu thất bại cùng job của
nó, run đi trước: một sự cố ở giữa để lại một run terminal, mà lần thử kế tiếp của job
sẽ ack (`settle.rs:46-56`).

## Lịch chạy

`create_schedule(pool, &Schedule)` (`crates/linch-worker/src/schedule.rs:80`)
lưu một mẫu cron theo UTC (năm trường, hoặc sáu trường với giây ở đầu, cú pháp croner) và
trả về lần kích hoạt đầu tiên của nó, tức tick đầu tiên sau thời điểm hiện tại của cơ sở
dữ liệu. `cancel_schedule` vô hiệu hóa các lần kích hoạt trong tương lai; các run đã được
đưa vào hàng đợi vẫn nằm trong hàng đợi.

`fire_due_schedules(pool)` (`schedule.rs:165`) khóa tối đa 64 hàng đến hạn bằng
`FOR UPDATE SKIP LOCKED`, đưa một job start vào hàng đợi cho mỗi hàng và dời `next_fire`
về phía trước, tất cả trong một transaction. Vì vậy một lần kích hoạt chỉ xảy ra một lần
dù có bao nhiêu worker poll. Mỗi job cũng mang dedupe key `schedule:{id}:{fire time}`
như một lớp bảo vệ thứ hai (`schedule.rs:1-12`). Các lần kích hoạt bị lỡ không được phát
lại: một lịch trễ nhiều tick chỉ kích hoạt một lần, và lần kích hoạt kế tiếp của nó là
tick đầu tiên sau thời điểm hiện tại. Một mẫu đã lưu mà không còn parse được sẽ vô hiệu
hóa lịch của nó thay vì chặn các lịch khác.

## Ví dụ

Đoạn này biên dịch được ở revision này; để chạy nó cần một cơ sở dữ liệu:

```rust
use std::error::Error;
use std::sync::Arc;

use linch_core::{
    AgentBuilder, AgentSpec, PermissionEngine, PermissionMode, PermissionPolicy, Provider,
    SessionId,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use linch_worker::{
    Schedule, Worker, WorkerConfig, create_schedule, enqueue_run, set_queue_limit,
};
use tokio_util::sync::CancellationToken;

/// Migrate, queue work, and serve it until `shutdown` is cancelled.
async fn serve(
    dsn: &str,
    provider: Arc<dyn Provider>,
    shutdown: CancellationToken,
) -> Result<(), Box<dyn Error>> {
    let (timeout, max) = (linch_postgres::CONNECT_TIMEOUT, linch_postgres::default_max_connections());
    let pool = linch_postgres::connect(dsn, timeout, max).await?;
    linch_postgres::migrate(&pool).await?;
    linch_worker::migrate(&pool).await?;

    // The worker reads run records through PgRunStore on its own pool, so
    // the agent's runs must live in the same database and schema.
    let permissions = PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/"))?;
    let agent = AgentBuilder::new(AgentSpec::new("my-model"))
        .provider(provider)
        .permissions(permissions)
        .run_store(Arc::new(PgRunStore::new(pool.clone())))
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace("tenant-a")
        .build()?;

    set_queue_limit(&pool, "default", 8).await?;
    let session = SessionId::new("inbox");
    enqueue_run(&pool, "default", "tenant-a", &session, "Summarize the inbox.").await?;
    let digest = Schedule {
        namespace: "tenant-a".into(),
        id: "daily-digest".into(),
        queue: "default".into(),
        session_id: SessionId::new("digest"),
        prompt: "Write the daily digest.".into(),
        cron: "0 7 * * *".into(),
    };
    let first_fire = create_schedule(&pool, &digest).await?;
    println!("first digest at {first_fire}");

    let mut config = WorkerConfig::new("worker-1");
    config.max_jobs = 4;
    Worker::new(pool, config).agent(agent).run(shutdown).await?;
    Ok(())
}

fn main() {
    let _ = serve; // needs a database
}
```

Pool trong ví dụ giữ `default_max_connections()`, tức số core của máy, kẹp trong khoảng 10 đến 32. Một worker có `max_jobs` run commit cùng lúc
cần khoảng hai kết nối cho mỗi run cộng với listener; xem [kết nối và định cỡ pool](./stores.md#kết-nối-và-định-cỡ-pool).

## Test

`crates/linch-worker/tests/pg/` bao phủ một job cho mỗi run, việc giữ độc quyền và
giới hạn hàng đợi, retry và back-off, session bận, suspend và approve,
tiếp quản sau khi mất heartbeat, kích hoạt lịch, và việc cố định
`READ COMMITTED`; `crates/linch-worker/src/lease/tests.rs` chạy từng lần ghi bị fence sau
một bản cập nhật khác của hàng job của nó dưới `REPEATABLE READ`. Mỗi test bị bỏ qua khi
`LINCH_TEST_PG_DSN` chưa được đặt.

---

Quay lại [mục lục kiến trúc](./README.md).
