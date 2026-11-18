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
| `0005_scheduler.sql` | `linch_worker_agents` (định nghĩa agent đã lưu, theo digest); các cột interval, múi giờ và session cố định trên lịch, cùng `fires`, `skips` và `last_fire`; `agent`, `schedule_id`, `fire_at` và `created_at` trên job; `linch_worker_history` |
| `0006_background_principal.sql` | `principal` (một `Principal` dạng JSON) trên lịch và job; các chỉ mục một phần trên cột `agent` của chúng |
| `0007_history_agent.sql` | `agent` trên `linch_worker_history`, cùng các chỉ mục một phần trên `(namespace, run_id)` và `(namespace, agent)` |

Một job bắt đầu một run (`session_id` và `prompt`), tiếp tục một run (`run_id`), hoặc
approve một run (`run_id` và `approvals`); các check constraint giữ những hình dạng đó
(`migrations/0001_worker.sql:13-31`). Một job đã xong được chuyển sang
`linch_worker_history` ([trạng thái job](#trạng-thái-và-lịch-sử-của-job)); một job thất bại
giữ hàng và lỗi của nó. `fire_due_schedules` đọc `linch_runs` (run đang sống của một
heartbeat), nên hãy chạy `linch_postgres::migrate` trên cùng schema.

## Đưa job vào hàng đợi

| Lời gọi | Job | Ghi chú |
|---|---|---|
| `enqueue_run(pool, queue, namespace, session_id, prompt)` | start | Worker ghi run id vào job trước lần ghi đầu tiên của run, nên một lần retry sau sự cố tiếp tục run đó (`crates/linch-worker/src/job.rs:129-163`) |
| `enqueue_resume(pool, queue, namespace, run_id)` | continue | Khi run đã có một job đang hoạt động, id của job đó được trả về thay vì một job mới. Job gọi tên định nghĩa đã lưu mà job mới nhất của run đã chạy cùng (từ `linch_worker_jobs` hoặc `linch_worker_history`), nên worker đã phục vụ run sẽ phục vụ lại nó; `WorkerError::Invalid` khi định nghĩa đó đã bị dọn. Nó không gọi tên principal nào: core tiếp tục run cho principal lúc start của nó (`job.rs`, `enqueue_continuation`) |
| `enqueue_approve(pool, queue, namespace, run_id, approvals)` | approve | Các approval được lưu ở dạng wire `ApprovalResponse` của lõi. Cùng định nghĩa và principal như `enqueue_resume`. `WorkerError::RunBusy` khi run có một job đang hoạt động, job này có thể kết thúc mà không đọc chúng; hãy đưa vào hàng đợi lại khi nó đã kết thúc (`job.rs`) |
| `enqueue(pool, &RunRequest, cap)` | start | `enqueue_run` kèm một định nghĩa agent đã lưu (tùy chọn) và một [cap](#cap) tùy chọn cho số job chưa xong của root (`crates/linch-worker/src/job.rs`) |

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

Nó cũng dọn lịch sử của các job đã kết thúc, và các job thất bại, cũ hơn `history_retention` (mặc định 7 ngày),
tối đa mỗi 10 phút một lần.

`Worker::definitions(roots, factory)` phục vụ các job mang một định nghĩa agent đã lưu: một
job có định nghĩa chỉ được nhận bởi worker có `roots` chứa đoạn đầu tiên (phân tách bằng
`/`) của namespace, còn job không có chỉ được nhận bởi worker có một agent cho namespace
của nó (`crates/linch-worker/src/lease.rs`, `dequeue`). Xem
[định nghĩa agent](#định-nghĩa-agent).

Khi `shutdown`, nó chờ tối đa `WorkerConfig::drain_timeout` (30 s) cho các job đang chạy
(`worker.rs`, `drain`). Một job vẫn còn chạy lúc đó sẽ được trả lại: `execute` drop lần thử
(một run stream bị drop sẽ giải phóng lease của run của nó qua `Held::drop`), chờ tối đa 5 s
cho `agent.released()`, rồi giải phóng job với độ trễ bằng 0 và không tính thất bại
(`Outcome::HandedBack`), nên worker kế tiếp không thấy hold nào. Một job không thể ổn định
trong 10 giây nữa sẽ bị abort và dựa vào visibility timeout. Thay vào đó, drop future sẽ bỏ
rơi chúng như một sự cố: các run của chúng vẫn resume được và các job hiện lại sau
visibility timeout.
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
  ([stores](./stores.md#fencing)). `PgRunStore` còn cho agent của worker thuê lease
  run ([leases](./stores.md#leases)): khi một process khác đang điều khiển run và
  gia hạn lease của nó, một claim bị từ chối với `RunLeased`, và job chờ (`Busy`).
  Hãy giữ lease của run không dài hơn visibility timeout (cả hai mặc định 30 s), để
  một job bị tiếp quản sau khi worker của nó im lặng thấy run đã rảnh.

Trong khi run stream, `keep_alive` gia hạn visibility của job mỗi một phần ba
timeout (`crates/linch-worker/src/execute.rs:151-172`). Job bị coi là mất
khi một heartbeat thấy một bên giữ khác, hoặc khi không có lần gia hạn nào thành công
trong cả một timeout kể từ lần được xác nhận gần nhất. Mất job làm run stream bị drop,
việc này là một sự cố đối với run: nó vẫn resume được, lease run của nó được release,
và lần claim của worker kế tiếp fence worker này ra theo epoch. Worker không bao giờ hủy token của run vì lý do
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

Không có job answer: một run suspend để chờ câu trả lời `AskUser` kết thúc job của nó,
một job resume trên nó chỉ suspend nó lại, và câu trả lời đến từ một host qua
`Agent::answer` (hoặc `run.answer`).

## Kết quả, back-off và dead letter

Mỗi lần thử kết thúc bằng một `Outcome` (`crates/linch-worker/src/outcome.rs:9-32`),
được `settle` (`settle.rs:32`) áp dụng:

| Outcome | Đến từ | Job |
|---|---|---|
| `Done` | Stream kết thúc ở `Result` hoặc `Suspended`; run đã terminal | Được chuyển sang lịch sử với trạng thái `done` |
| `Retry` | Một lỗi store hoặc journal, kể cả xung đột append của session; không có agent cho namespace | Được release với back-off, tính một lần thất bại |
| `Busy` | Một job start mà session của nó có một run đang chạy (`SessionBusy`), hoặc một run mà process khác đang giữ lease còn sống (`RunLeased`) | Được release với back-off, không tính thất bại |
| `Fail` | Một approve muộn (`NotSuspended`), một prompt bị chặn, approval không giải mã được, một run bị thiếu | Bị đánh dấu thất bại vĩnh viễn |
| `FailRun` | Trạng thái của run không reconcile được, hoặc contract của nó khác với của agent | Run bị đánh dấu thất bại qua `Agent::fail`, rồi đến job |
| `Fenced` | `LeaseLost`: một owner khác đã claim run | Được chuyển sang lịch sử với trạng thái `superseded`, không retry, vì retry sẽ lấy lại run |
| `Lost` | Một worker khác giữ job | Để nguyên |

`classify` (`outcome.rs:44`) ánh xạ một `RunError` vào các giá trị này. Độ trễ sau
lần thử `n` là `retry_delay * 2^(n-1)`, tối đa 5 phút
(`settle.rs:24-29`). Sau năm lần thử thất bại, job bị đánh dấu thất bại vĩnh viễn,
và run của nó cũng vậy khi job nêu tên một run (`settle.rs:77-102`). Chờ một
session đang bận không bao giờ được tính, nên một job start không bao giờ bị bỏ cuộc vì lý do đó.

Một job thất bại là dead letter: `failed_at` và `error` được đặt, nó không bao giờ được
dequeue lại, và nó không còn được tính vào chỉ mục một-job-cho-mỗi-run hay
giới hạn của hàng đợi (`lease.rs:288-302`). Nó nằm lại trong `linch_worker_jobs` đến khi
`prune_history` xóa nó, `history_retention` sau khi nó thất bại. `error` giữ cả chuỗi lỗi
(`crates/linch-worker/src/error.rs:40-49`). Khi một run bị đánh dấu thất bại cùng job của
nó, run đi trước: một sự cố ở giữa để lại một run terminal, mà lần thử kế tiếp của job
sẽ ack (`settle.rs:46-56`).

Một factory từ chối định nghĩa của job (`FactoryError::Refused`) làm job thất bại; một
factory trả `Busy` (tenant của nó không còn chỗ chạy run nào) release job như `Busy` ở trên;
một factory trả `Unavailable` retry job như `Retry`
(`crates/linch-worker/src/execute.rs`, `agent`).

## Trạng thái và lịch sử của job

Một job kết thúc sẽ rời `linch_worker_jobs` và vào `linch_worker_history` trong một câu
lệnh duy nhất, một `DELETE ... RETURNING` cấp dữ liệu cho một `INSERT`, bị fence theo lần
thử của nó như mọi lần ghi khác (`lease.rs`, `ack`), nên một job luôn nằm trong đúng một
trong hai bảng. `job(pool, namespace, id)` và `jobs(pool, namespace, schedule_id, limit)`
(`crates/linch-worker/src/status.rs`) đọc cả hai, giới hạn theo namespace chính xác (id job
là toàn cục), và nêu một `JobState`: `queued`, `running` (có chủ và vẫn đang ẩn),
`retrying` (được release kèm lỗi), `failed`, `done` hoặc `superseded`.
`prune_history(pool, older_than)` xóa các hàng lịch sử đã kết thúc, và các job thất bại đã thất bại,
quá `older_than` trước.

## Lịch chạy

Một `Schedule` (`crates/linch-worker/src/schedule.rs`) kích hoạt theo một `Timing`
(`crates/linch-worker/src/timing.rs`): một mẫu cron (năm trường, hoặc sáu trường với giây ở
đầu, cú pháp croner) được đọc trong một múi giờ IANA qua `chrono-tz`, hoặc một interval cố
định. Một lần kích hoạt cron rơi vào khoảng trống của giờ mùa hè được dời tới thời điểm hợp
lệ đầu tiên sau nó; một lần rơi vào khoảng chồng lặp chỉ kích hoạt một lần, ở nửa đầu (quy
tắc của croner, được test trong `timing.rs`). `Timing::shortest_gap` là khoảng cách nhỏ nhất
trong 512 lần kích hoạt kế tiếp, mà tầng RPC so với interval tối thiểu của tenant.

`create_schedule(pool, &Schedule, cap)` lưu nó cùng lần kích hoạt đầu tiên, tức tick đầu tiên
sau thời điểm hiện tại của cơ sở dữ liệu; `ScheduleExists` cho một id đã dùng.
`list_schedules`, `get_schedule`, `pause_schedule`, `resume_schedule` (lần kích hoạt kế tiếp
tính từ bây giờ: các lần bị lỡ khi đang tạm dừng không được thực hiện) và `delete_schedule`
(các job đang chờ và đang chạy được để nguyên) hoàn tất bộ này.

`fire_due_schedules(pool, caps)` khóa tối đa 64 hàng đến hạn bằng `FOR UPDATE SKIP LOCKED` và, với
mỗi hàng, đưa một job start vào hàng đợi rồi dời `next_fire` tới tick đầu tiên sau lần
kích hoạt này, hoặc sau bây giờ khi tick đó cũng đã qua, tất cả trong một transaction:

- **Đúng một lần.** Một worker sập trước khi commit không để lại job cũng không dời gì, nên
  lần poll kế tiếp thực hiện lần kích hoạt; các poller cùng lúc bỏ qua các hàng đã bị khóa.
  Dedupe key của job, `schedule:{id}:{fire time}`, là lớp bảo vệ thứ hai.
  `tests/pg/scheduler.rs` hủy một lần kích hoạt ngay trong lúc insert, kill backend đang
  kích hoạt bằng `pg_terminate_backend` giữa transaction, và cho tám bộ kích hoạt đua nhau
  trên hai mươi lịch.
- **Bù lần lỡ có gộp.** Một lịch lỡ nhiều tick (mọi worker đều ngừng hoạt động) kích hoạt
  một lần, ở `next_fire` đã lưu của nó, cộng các tick bị lỡ sau đó đến bây giờ vào `skips`,
  và dời `next_fire` tới tick đầu tiên sau bây giờ (`after_missed` trong `schedule.rs`; một
  interval vẫn nằm trên lưới của nó). Các tick bị lỡ của một interval được đếm bằng phép
  chia; của một mẫu cron được duyệt từng tick, tối đa 10.000 (`MISSED_COUNTED`). Python
  Linch kích hoạt từng tick bị lỡ, theo thứ tự; việc gộp giữ cho một thời gian ngừng hoạt
  động dài không xếp hàng một run cho mỗi tick bị lỡ.
- **Bỏ qua.** Một lần kích hoạt bị bỏ qua (`skips` + 1, `next_fire` được dời như khi đã
  thực hiện) khi một job của lịch chưa thất bại và chưa kết thúc, với mọi lịch; có
  `session_id` thì cũng khi session có một run chưa terminal trong `linch_runs` (kể cả run
  đang suspend chờ approval hoặc input); và khi root của nó đang ở `Cap` về job chưa xong mà
  caller truyền vào (`fire_due_schedules(pool, caps)`, `Worker::job_caps` của worker, mà
  `linch-server` điền bằng `max_queued_jobs` của từng tenant). Các root bị giới hạn của batch
  được khóa trước theo thứ tự đã sắp xếp, nên hai bộ kích hoạt không bao giờ chờ nhau. Các
  lần kích hoạt không bao giờ chồng chất sau một hàng đợi tồn đọng hay một run đang chờ một
  người, và không bao giờ thất bại ở mức trần.
- **Session.** Không có `session_id`, mỗi lần kích hoạt bắt đầu session
  `schedule:{id}:{fire time}`; có `session_id`, mỗi lần kích hoạt là một run mới trong đó.
- Một timing đã lưu mà không còn parse được sẽ tạm dừng lịch của nó (có ghi log) thay vì
  chặn các lịch khác.

### Cap

`Cap { root, max }` (`crates/linch-worker/src/definition.rs`) giới hạn những gì các
namespace dưới một root (đoạn đầu tiên phân tách bằng `/` của một namespace, tên của một
tenant) được giữ: số lịch cho `create_schedule`, số job chưa xong cho `enqueue` và cho lần kích hoạt của một lịch. Việc đếm
chạy trong transaction của lần insert sau một advisory lock phạm vi transaction trên root,
nên các writer trên mọi server đếm các hàng của nhau; vượt quá thì `LimitReached`, hoặc với
một lần kích hoạt thì bỏ qua.

## Định nghĩa agent

Một job hoặc lịch có thể mang một định nghĩa agent: văn bản mà hàng đợi lưu một lần cho mỗi
`(namespace, digest SHA-256)` trong `linch_worker_agents` và gọi theo digest, nên các lần
kích hoạt của một lịch dùng chung một hàng và một định nghĩa không bao giờ thay đổi dưới
chân một lịch. Một worker được cấp `Worker::definitions(roots, factory)` gọi `AgentFactory`
(`crates/linch-worker/src/factory.rs`) mỗi lần thử; `JobAgent` nó trả về được đóng sau khi
job được settle, như worker của Python đóng agent mà factory của nó trả về. `BackgroundAgents`
của `linch-rpc` là factory mà `linch-server` dùng: định nghĩa là một cấu hình agent, được
admit lại theo tenant của nó ở mỗi lần thử ([RPC](./rpc.md#run-nền)).

Một định nghĩa giữ các secret của cấu hình dưới dạng văn bản thuần, nên
`prune_definitions(pool)` (`crates/linch-worker/src/definition.rs`) xóa những định nghĩa mà
không lịch nào, không job chưa xong nào và không hàng history nào của một run chưa terminal
gọi tên, sau khi đặt `agent` thành NULL trên các job đã thất bại (không bao giờ chạy lại)
mà run của chúng đã terminal. Vì vậy một run suspend chờ approval hoặc câu trả lời giữ định
nghĩa của nó cho job tiếp tục nó: `prune_history` giữ một hàng history hoặc job thất bại gọi
tên một định nghĩa khi run của nó chưa terminal, và xóa nó ở lần dọn đầu tiên sau khi run
kết thúc. `enqueue_resume` và `enqueue_approve` từ chối một run mà định nghĩa được gọi tên
đã mất (`WorkerError::Invalid`). Một worker chạy nó cùng việc dọn history, từ lần poll
đầu tiên và cứ 10 phút một lần sau đó, dù có giữ history hay không. Một writer lưu một định
nghĩa khóa hàng của nó (`ON CONFLICT ... DO UPDATE`) đến khi commit, và prune khóa các ứng cử
viên của nó bằng `FOR UPDATE SKIP LOCKED`, nên một định nghĩa sắp được gọi tên sẽ bị bỏ qua;
một định nghĩa được gọi tên bởi một writer đã commit sau khi prune đọc các tham chiếu sẽ làm
foreign key của lệnh xóa thất bại, và lần prune đó không xóa gì (nó chạy lại sau).

## Retention

`prune_retained(pool, &Retention)` (`crates/linch-worker/src/retention.rs`) là
phần xóa tùy chọn những gì run đã kết thúc và session nhàn rỗi để lại; worker
chạy nó trong một task riêng mỗi `Retention::interval` khi
`WorkerConfig::retention` được đặt (`worker.retention` trong config server).
Mỗi lô là một transaction: một lượt chọn keyset theo thứ tự index sau con trỏ
của đợt (`FOR UPDATE ... SKIP LOCKED`, `batch_size` hàng), rồi một câu lệnh
xóa run (hoặc session), event của nó (hoặc log, task và cạnh) và ghi tombstone,
trong đó trạng thái terminal (hoặc đồng hồ nhàn rỗi và việc không có run còn
sống) được kiểm tra lại ngay trong lệnh xóa. Một đợt giữ một try-lock
mức session, khóa theo database và schema, trên một connection của pool từ lô
đầu đến lô cuối, nên cả fleet chỉ có một pruner chạy tại một thời điểm, đợt thứ
hai không thể xen vào các quãng nghỉ giữa các lô (sau mỗi lô là một quãng nghỉ
bằng thời gian lô đó chạy, giữ cho đợt bận tối đa một nửa so với database), và
connection của pruner bị crash nhả khóa. Đợt thấy khóa đã bị giữ không xóa gì
và báo `contended`.

Những gì không bao giờ bị xóa suy ra từ việc code khác đọc gì của một run về
sau: run chưa terminal; run con khi run cha chưa terminal (resume của cha đọc
`<run>:<tool_use_id>` và khởi động lại subagent nếu nó mất); run và session
`wf-` (workflow được resume đọc run khi journal chưa có bản ghi); run mà một job
còn sống gọi tên (job start được thử lại sẽ bắt đầu run khác nếu run mất). Không
có code nào đọc event của một run trước đó trong cùng session, nên việc tiếp
tục session không phụ thuộc event của run, và hai cửa sổ độc lập nhau. Các run
đã terminal trước migration 10 được gán thời điểm kết thúc ở đợt đầu tiên có
cửa sổ cho run. `job.list`, history và schedule không bị đụng đến.

## Principal

Một lịch và một job có thể mang một principal (`Schedule::principal`,
`RunRequest::principal`), được lưu dưới dạng JSON; một lần kích hoạt sao chép principal của
lịch của nó sang job của nó. Worker giải mã nó ở mỗi lần thử và đặt nó vào `RunOptions` của
lệnh start, resume hoặc approve (`execute.rs`). Core lưu principal lúc start cùng run
(`RunRecord::principal`), và một resume hoặc approve có options không nêu principal nào sẽ
hành động thay cho nó, nên một job tiếp tục (`enqueue_resume`, `enqueue_approve`) không mang
principal riêng. Một hàng có principal không giải mã được làm job của nó thất
bại. Worker của Python khôi phục `RunRequest.principal` theo cách tương tự
(`linch/worker/runtime.py`).

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
    Schedule, Timing, Worker, WorkerConfig, create_schedule, enqueue_run, set_queue_limit,
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
        // A heartbeat: every fire is a new run in this one session.
        session_id: Some(SessionId::new("digest")),
        prompt: "Write the daily digest.".into(),
        timing: Timing::Cron {
            pattern: "0 7 * * *".into(),
            timezone: "Europe/Berlin".into(),
        },
        // No stored definition: the worker's agent for the namespace runs it.
        agent: None,
        // Fires act for no principal: no role-gated rule applies.
        principal: None,
    };
    let stored = create_schedule(&pool, &digest, None).await?;
    println!("first digest at {}", stored.next_fire);

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
tiếp quản sau khi mất heartbeat (các agent test thuê lease run trong 1,5 s, dưới
visibility timeout 3 s của một worker mà test định làm mất), kích hoạt lịch (bù lần lỡ, heartbeat bỏ qua, tạm dừng và tiếp tục, cap, một lần kích hoạt bị hủy hoặc bị kill giữa transaction, các bộ kích hoạt đua nhau), các job do một `AgentFactory` chạy cùng lịch sử của chúng, và việc cố định
`READ COMMITTED`; `crates/linch-worker/src/lease/tests.rs` chạy từng lần ghi bị fence sau
một bản cập nhật khác của hàng job của nó dưới `REPEATABLE READ`. Mỗi test bị bỏ qua khi
`LINCH_TEST_PG_DSN` chưa được đặt.

---

Quay lại [mục lục kiến trúc](./README.md).
