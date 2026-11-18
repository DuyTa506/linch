# Nhúng linch-rust vào ứng dụng của bạn

[Bản gốc tiếng Anh](../../usage/embedding.md)

Dùng trang này để xem linch-rust làm gì cho ứng dụng của bạn và ứng dụng của bạn phải cung cấp gì xung quanh nó.

[<- Hướng dẫn sử dụng](./README.md)

linch-rust là một SDK, không phải một dịch vụ được lưu trữ sẵn. Ứng dụng Python của bạn import
`linch_rust`, thư viện này khởi động `linch-server` như một sidecar (hoặc kết nối tới một
server bạn tự chạy) và điều khiển các agent qua nó. Sidecar không có endpoint health, không có
endpoint metrics, không có TLS và không có giao diện quản trị: những thứ đó thuộc về ứng dụng
của bạn và nền tảng của nó.

---

## SDK làm gì cho bạn

- **Run bền vững.** Với kho Postgres, mọi event và checkpoint đều bền vững trước side effect
  kế tiếp (một lệnh gọi mô hình, lúc một tool bắt đầu, lúc kết thúc), nên một sự cố làm mất
  nhiều nhất là lệnh gọi đang chạy dở ([Run](./runs.md), [Kho lưu trữ](./stores.md)).
- **Lease và tiếp quản.** Mỗi lúc chỉ một server điều khiển một run; run của một server bị
  crash được giải phóng sau độ dài lease (mặc định 30 s), và một server dừng bình thường giải
  phóng các run của nó ngay lập tức ([Run leases](./stores.md#run-leases)). Lease được gia hạn
  bởi một task riêng trên một kết nối chuyên dụng mà store của mỗi agent đang mở giữ ngoài pool
  của nó (hãy tính thêm một cho mỗi agent trong [ngân sách kết nối](./stores.md#ngân-sách-kết-nối)),
  nên một client ngừng đọc stream của nó, hoặc một pool có mọi kết nối đều bận, không làm mất run
  vào tay server khác.
- **Group commit.** Các lần ghi được commit cùng nhau tại các barrier ở trên, không phải mỗi
  event một lần.
- **Kiểm duyệt tenant.** Một server có tenant kiểm tra mọi cấu hình agent với allowlist và
  giới hạn của tenant trước khi bất cứ thứ gì chạy ([Server](./server.md)).
- **Redaction.** Các rule regex xóa thông tin trong response, kết quả tool và prompt trước khi
  bất cứ thứ gì ghi log, stream hoặc lưu chúng ([Bảo mật](./security.md#redaction)).
- **Trace và metric.** Client Python phát ra span và metric OpenTelemetry cho run, step, lệnh
  gọi mô hình và tool ([Khả năng quan sát](./observability.md)).
- **Kết quả dài và hội thoại dài.** Kết quả tool lớn được ghi vào một tệp kèm bản xem trước,
  và compaction giữ một hội thoại trong context window ([Tool](./tools.md#kết-quả-tool-dài),
  [Memory](./memory.md)).
- **Retention, nếu bạn bật.** Một worker có thể xóa các run đã kết thúc và session nhàn rỗi
  sau một khoảng thời gian bạn chọn ([Retention](./stores.md#retention)). Mặc định không có gì
  bị xóa.
- **Lịch và job nền.** Lịch cron và interval cùng các run trong hàng đợi, do một worker chạy
  ([Scheduler](./scheduler.md)).

## Ứng dụng của bạn phải cung cấp gì

| Mối quan tâm | Việc cần làm |
|---|---|
| **Health và readiness** | Sidecar không có endpoint cho chúng. Hãy báo cáo health của chính tiến trình của bạn: `Server()` đã khởi động chưa, và, với cơ sở dữ liệu cùng schema của nó, `await linch_rust.schema_status(dsn)` (chỉ đọc; nó khởi động server binary một lần, nên hãy gọi lúc khởi động hoặc ở một probe chậm, không gọi cho mỗi request). |
| **Metrics** | Dùng OpenTelemetry. Client phát ra span và hai metric (`gen_ai.client.operation.duration`, `gen_ai.client.token.usage`); server không có metric. Với số run, bên giữ lease hay độ sâu hàng đợi, hãy truy vấn cơ sở dữ liệu (`linch_runs` có `status`, `owner` và `lease_until`; các bảng hàng đợi nằm trong [kiến trúc worker](../architecture/worker.md)) hoặc tự đếm trong ứng dụng. Kích thước và mức sử dụng pool không được xuất ra: hãy định cỡ từ [ngân sách kết nối](./stores.md#ngân-sách-kết-nối). |
| **TLS và xác thực** | Một server lắng nghe nói HTTP thuần và Unix socket: hãy đặt TLS phía trước hoặc lắng nghe trên loopback. Token là bearer token tĩnh theo từng tenant, được đọc lúc khởi động, không hết hạn và không thu hồi được. Xoay một token nghĩa là khởi động lại server, việc này bỏ mọi run đang chạy trên nó (một run Postgres vẫn resume được; client kết nối lại và resume). Hãy giữ một `Server()` nhúng trên stdio ở nơi bạn có thể: nó không cần cả hai. |
| **Backup** | Mọi trạng thái bền vững nằm trong một schema Postgres (run, event, session, tệp, hàng đợi, lịch, memory). Hãy backup nó như bạn backup cơ sở dữ liệu đó. Một lần restore đưa mọi run về trạng thái của snapshot, và không có gì phát lại những gì xảy ra sau đó; hãy dừng các server trước khi restore, và thử một lần restore trước khi bạn cần đến nó. |
| **Lưu giữ log** | Server ghi log ra stderr (`LINCH_LOG`). Log không được xóa thông tin nhạy cảm: một provider, một tool hoặc cơ sở dữ liệu có thể đã phản hồi một credential vào một thông báo lỗi. Hãy giữ log riêng tư như cơ sở dữ liệu, và đừng đẩy chúng sang một hệ thống dùng chung nếu chưa tự xóa thông tin ([Server: Log](./server.md#log)). |
| **Nâng cấp** | Hãy chạy [`migrate`](./stores.md#migration-như-một-bước-deploy) trước, rồi khởi động các server của bản phát hành mới. Một bản phát hành có thể thay đổi [hợp đồng của run](./agent.md#hợp-đồng-của-run) (mô tả của một tool dựng sẵn, danh sách tool của một MCP server, phiên bản chính sách của một hook), và khi đó một run đã lưu chưa kết thúc không thể resume. Hãy đọc `CHANGELOG.md` trước mỗi lần nâng cấp và để các run đang chạy kết thúc trước. Một server cũ trên schema mới hơn từ chối với `SchemaNewer` (-32016). |
| **Signal** | SIGTERM và SIGINT dừng một server một cách sạch sẽ: các run bị bỏ ở trạng thái bền vững cuối cùng của chúng, lease của chúng được giải phóng (chờ tối đa 5 s), và một worker cho các job của nó `worker.drain_timeout_ms` (30 s) trước khi trả chúng lại ([Scheduler: Dừng một worker](./scheduler.md#dừng-một-worker)). SIGKILL hoặc một sự cố không giải phóng gì: server khác chờ một lease (mặc định 30 s; `stores.lease_ttl_ms`) trước khi tiếp quản các run. Hãy cho supervisor của bạn một grace period dài hơn drain timeout cộng khoảng 15 s. |
| **Định cỡ** | Hãy đếm kết nối bằng [ngân sách kết nối](./stores.md#ngân-sách-kết-nối). Mọi giới hạn đều tính theo từng tiến trình. Về throughput và bộ nhớ, xem bên dưới. |
| **Cô lập lệnh** | `Bash` chạy với user của server theo mặc định. Hãy chọn một backend phù hợp với những gì agent của bạn chạy ([Sandbox](./sandbox.md), gồm cả [cô lập mạnh hơn](./sandbox.md#các-mức-cô-lập)). |

## Định cỡ: những gì đã được đo

Các con số này đến từ một lần chạy các live check trong
`python/tests/live/measurements/durable.md`, trên một host 64 core dùng chung, với provider
scripted (không có độ trễ của mô hình) và một Postgres. Chúng cho thấy bậc độ lớn của chi phí
riêng của SDK, không phải những gì agent của bạn sẽ làm: một lệnh gọi mô hình thật mất vài
giây. Hãy đo workload của bạn.

| Cái gì | Đã đo |
|---|---|
| Run bền vững trên Postgres, 20 run scripted tầm thường, 1 / 4 / 16 đồng thời | 56 / 180 / 258 run mỗi giây (kho memory: 4568 / 6713 / 7516) |
| Resume sau một sự cố, từ lúc server khởi động đến event đầu tiên được resume (lease đã hết hạn) | p50 59 ms |
| Tiếp quản sau `kill -9` | trung bình 2,7 s với lease 3 s; 28,6 s với mặc định 30 s (bị chặn bởi lease) |
| Kho memory | khoảng 110 KB bộ nhớ server cho mỗi run, giữ trong suốt thời gian agent còn tồn tại |
| 1000 session chạy cùng lúc trên một agent (Postgres, 4 lượt mỗi session), bộ nhớ đỉnh của process | 268 MB (khoảng 0,25 MB mỗi session; 207 MB với `MALLOC_ARENA_MAX=2`) |

Schema của tool được giữ một bản cho mỗi agent và dùng chung cho mọi session, nên mỗi session
chỉ tốn transcript và trạng thái run của nó, không còn một bản sao của cả danh mục tool. Trên
glibc, bộ nhớ đã giải phóng vẫn nằm lại trong các arena theo từng thread: với process phục vụ
nhiều session cùng lúc, hãy đặt `MALLOC_ARENA_MAX=2` trong môi trường của server (đỉnh thấp hơn
23 % trong cùng cell, đo 3 lần mỗi bên). Một global allocator như jemalloc là lựa chọn khác;
nó tiết kiệm khoảng 9 % khi còn các bản sao cũ và đã không được dùng.

Hãy dùng kho Postgres cho bất cứ thứ gì sống lâu; kho memory giữ lịch sử của mọi run trong
server trong suốt thời gian agent còn tồn tại.

## Một lần deploy tối thiểu

1. Tạo schema và hai role: một role có quyền DDL cho `migrate`, một role không có cho các
   server.
2. `await linch_rust.migrate(admin_dsn)` như một bước deploy, một lần cho mỗi bản phát hành.
3. Khởi động ứng dụng của bạn, nơi khởi động `Server()` (hoặc kết nối tới một server bạn chạy
   bằng `linch-server --config`).
4. Dừng bằng SIGTERM và cho nó grace period ở trên.

## Các trang liên quan

- [Kho lưu trữ](./stores.md): DSN, migration, ngân sách kết nối, lease, retention.
- [Server](./server.md): chạy `linch-server` cho các tenant từ xa.
- [Khả năng quan sát](./observability.md): OpenTelemetry từ client.
- [Khắc phục sự cố](./troubleshooting.md): các lỗi và nguyên nhân của chúng.
