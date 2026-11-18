# Kho lưu trữ

[Bản gốc tiếng Anh](../../usage/stores.md)

Dùng trang này để chọn nơi một agent lưu run và session: bộ nhớ cho kiểm thử và phát triển, Postgres cho mọi thứ phải tồn tại sau khi khởi động lại.

[<- Hướng dẫn sử dụng](./README.md)

Trường `stores` của [cấu hình agent](./agent.md) chọn backend. Server sở hữu các kho
lưu trữ; tiến trình của bạn không bao giờ kết nối trực tiếp tới chúng.

```python
{"type": "memory"}                                            # the default
{"type": "postgres", "dsn": "postgres://user:pass@db:5432/linch"}
{"type": "postgres", "dsn": "postgres://user:pass@db:5432/linch", "max_connections": 32}
```

---

## Bộ nhớ hay Postgres

| | Bộ nhớ | Postgres |
|---|---|---|
| Run và session nằm ở | Trong handle của agent, bên trong tiến trình server | Trong cơ sở dữ liệu |
| Sau khi server dừng | Mất | Được giữ; một server mới tiếp tục chúng |
| Ai có thể resume hoặc phê duyệt một run | `Agent` đã bắt đầu nó | Bất kỳ agent nào có cùng cấu hình và tool (cùng [hợp đồng](./agent.md#the-run-contract)) trên bất kỳ server nào |
| Một run đang chạy cho mỗi session | Theo từng handle của agent | Trên mọi server dùng cơ sở dữ liệu, theo từng namespace |
| Một bên điều khiển cho mỗi run | Không: mỗi tiến trình có các run riêng | Có: một run mà server khác đang điều khiển bị từ chối với `RunLeased` ([Run leases](#run-leases)) |
| Tệp của hệ thống tệp ảo | Theo từng session, bị bỏ khi `close_session` | Dùng chung cho mọi session của namespace |
| Task của session | Trong bộ nhớ | Trong cơ sở dữ liệu |
| Dùng cho | Kiểm thử, notebook, phát triển | Dịch vụ, run dài hoặc có chờ phê duyệt, nhiều hơn một tiến trình |

Cả hai tuân theo cùng quy tắc và vượt qua cùng các bài kiểm thử tuân thủ, nên mã
chạy được trên cái này cũng chạy được trên cái kia. Kho trong bộ nhớ không phải là
cache đặt trước Postgres; hãy chọn một loại cho mỗi agent.

## Postgres

```python
import asyncio
import os

from linch_rust import AgentConfig, ResultEvent, RunNotFound, Server
from linch_rust.scripted import MODEL, text_turn

DSN = os.environ["DATABASE_URL"]


def config(namespace: str, reply: str = "unused") -> AgentConfig:
    return {
        "namespace": namespace,
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": [text_turn(reply)]},
        "stores": {"type": "postgres", "dsn": DSN},
    }


async def main() -> None:
    async with Server() as server:
        # Two agents on one DSN share one pool; the first open runs the migrations.
        support = await server.open_agent(config("acme/support", "Ticket noted."))
        billing = await server.open_agent(config("acme/billing"))
        run = await support.run("ticket-9", "My invoice is wrong", run_id="ticket-9-1")
        assert isinstance(await run.result(), ResultEvent)
        try:
            await billing.events("ticket-9-1")
        except RunNotFound:
            print("billing cannot see support's runs")

    # Another server process reads the same run back.
    async with Server() as server:
        support = await server.open_agent(config("acme/support"))
        for envelope in await support.events("ticket-9-1"):
            print(envelope.seq, envelope.event.type)


asyncio.run(main())
```

```text
billing cannot see support's runs
1 init
2 user
3 context_build
6 assistant
7 usage
8 result
```

### DSN

`dsn` là một URL kiểu libpq: `postgres://user:password@host:port/database`, kèm các
tham số truy vấn như `sslmode=require` hoặc `options=...`. Server là bên kết nối;
hãy cấp cho máy chạy server đường mạng và thông tin xác thực.

- **Schema.** Các bảng được tạo không kèm tên schema, nên `search_path` của kết nối
  quyết định schema. Để giữ linch-rust trong schema riêng, hãy tạo schema và đưa nó
  vào DSN:
  `postgres://user:pass@db/app?options=-c%20search_path%3Dlinch`.
- **Bí mật.** Không thông báo lỗi, dòng log hay cấu hình được phản hồi lại nào chứa
  DSN hoặc mật khẩu của nó. DSN không thuộc hợp đồng của run, nên bạn có thể đổi
  mật khẩu mà vẫn tiếp tục resume các run.
- **Namespace.** Mọi hàng được khóa theo `namespace` của agent. Các agent có
  namespace khác nhau trên cùng một cơ sở dữ liệu không bao giờ thấy run hay
  session của nhau (`RunNotFound`, như trên); các agent có cùng namespace dùng
  chung chúng. Hãy dùng một namespace cho mỗi tenant hoặc mảng sản phẩm, như
  `acme/support`.

### Bảng và migration

Hãy chạy các migration như một bước deploy ([bên dưới](#migration-như-một-bước-deploy)).
Một agent mở trên một DSN cũng áp dụng những migration còn thiếu, nên một môi trường phát
triển không cần gì khác; việc này an toàn ở mọi lần khởi động: các migration đã áp dụng được
bỏ qua, và các lần mở đồng thời chờ trên một khóa cơ sở dữ liệu. Một schema đã được migrate
đủ chỉ bị đọc, trong một truy vấn, và không lấy khóa: một agent mở trên nó không phải chờ lần
mở khác, hay một migration còn đang chạy. Các phiên bản đã áp dụng được ghi trong
`linch_migrations` (của worker trong `linch_worker_migrations`), nên bảng của công cụ migration
riêng của bạn trong cùng schema sẽ không xung đột.

| Bảng | Chứa |
|---|---|
| `linch_runs`, `linch_run_events` | Trạng thái và checkpoint của từng run, cùng các event bền vững của nó (thứ mà `agent.events` trả về) |
| `linch_sessions`, `linch_session_entries` | Nhật ký hội thoại của từng session |
| `linch_files` | Hệ thống tệp ảo, theo từng namespace |
| `linch_tasks`, `linch_task_edges` | Task của session và các phụ thuộc của chúng |

Migration 6 thêm hai cột nullable vào `linch_runs` (`lease_holder`, `lease_until`) cho
[run leases](#run-leases). Nó không viết lại hàng nào và không giữ khóa lâu.

- Role trong DSN cần quyền tạo bảng và index trong schema của nó ở lần đầu và ở mỗi lần
  nâng cấp có thêm một migration; ngoài ra nó chỉ đọc và ghi các hàng. Hãy chạy
  [`migrate`](#migration-như-một-bước-deploy) bằng một role có các quyền đó và đưa cho các
  server một role không có chúng.
- Một migration không hoàn tất trong 30 giây làm việc mở thất bại với `OpenError`
  (mã -32014); nguyên nhân thường gặp là một tiến trình khác đang giữ khóa migration.
- Migration chỉ được thêm vào, không bao giờ sửa. Đừng sửa hay xóa các bảng
  `linch_` bằng tay khi các run còn đang chạy.
- Payload được lưu dưới dạng `TEXT`, không phải `JSONB`, nên các byte mà mô hình đã
  thấy chính là các byte được đọc lại.
- Client không có lệnh gọi nào xóa run hay session. Chỉ [retention](#retention)
  của server làm việc đó, khi bạn bật nó.

Chi tiết schema, đường ghi và quy tắc từ chối nằm ở
[trang kiến trúc về kho lưu trữ](../architecture/stores.md#postgres-stores).

### Migration như một bước deploy

Áp dụng schema một lần cho mỗi bản phát hành, trước khi các server của bản đó khởi động:

```python
import linch_rust

status = await linch_rust.migrate("postgres://admin:...@db:5432/linch?options=-c%20search_path%3Dlinch")
assert status.is_current                      # store and worker tables, both at the latest
status = await linch_rust.schema_status(dsn)  # read only; status.store.applied / .latest
```

Tương tự từ shell hoặc một script deploy, với DSN nằm trong một biến môi trường (không bao
giờ trên dòng lệnh):

```bash
LINCH_DSN=postgres://admin:...@db/linch linch-server --migrate --dsn-env LINCH_DSN
LINCH_DSN=... linch-server --check --dsn-env LINCH_DSN    # writes nothing
linch-server --check --config /etc/linch/linch-server.json  # the DSN of its worker section
```

Mỗi lệnh in một dòng JSON, `{"current": true, "store": {"applied": 10,
"latest": 10, "current": true}, "worker": {"applied": 7, "latest": 7,
"current": true}}`, và thoát với một mã trạng thái mà
script có thể rẽ nhánh theo:

| Mã thoát | Ý nghĩa |
|---|---|
| 0 | Schema đã hiện hành |
| 1 | Không đến được cơ sở dữ liệu, hoặc một migration thất bại |
| 2 | Dòng lệnh hoặc cấu hình sai, hoặc một biến chưa được đặt |
| 3 | Cơ sở dữ liệu có một migration mà binary này không biết: một bản phát hành mới hơn đã migrate nó |
| 4 | Chỉ với `--check`: schema đứng sau binary này, hoặc khác với nó (thiếu hoặc đổi một migration); hãy chạy `--migrate` |

`migrate` bao gồm cả hai bảng migration, của các store và của worker. Chạy lặp lại an toàn và
chạy từ nhiều host cùng lúc cũng an toàn. Trong Rust, nó là `linch::migrate(dsn)` và
`linch::check(dsn)`.

**Cổng kiểm tra phiên bản.** Trước khi `agent.open` chạm vào một run, và trước khi một worker
khởi động, server đọc các migration mà cơ sở dữ liệu đã áp dụng. Nếu nó có một migration mà
binary này không nhúng, việc mở thất bại với `OpenError` mã -32016 (`SchemaNewer` trong
Python, với `.table`, `.applied` và `.latest`) và không có gì được đọc hay ghi; một worker từ
chối khởi động với cùng nội dung trên stderr (thoát 2). Một lần mở bình thường trên schema
hiện hành hoặc cũ hơn không thay đổi: nó áp dụng những gì còn thiếu, như trên.

**Rolling deploy.** Migration chỉ được thêm vào, và các bảng và cột của mỗi bản phát hành
đều mang tính cộng thêm, nên một server cũ vẫn chạy được trên một cơ sở dữ liệu mà bản mới đã
migrate, miễn là nó không mở một pool mới: mọi đích kết nối mà nó đã mở vẫn tiếp tục chạy.
Một server cũ mở pool của một tenant mới, hoặc khởi động lại, sẽ thấy schema mới hơn và từ
chối với -32016. Vì vậy:

1. Chạy `migrate` bằng binary mới (bước deploy ở trên).
2. Khởi động các server mới; drain và dừng các server cũ. Client của một server đã dừng
   resume các run của họ trên một server mới ([Server nào điều khiển một
   run](./server.md#server-nào-điều-khiển-một-run)).
3. Để rollback một bản phát hành đã thêm một migration, hãy khôi phục cơ sở dữ liệu từ trước
   bước 1: một binary cũ hơn từ chối một schema mới hơn. Không có gì xóa một migration.

Một bản phát hành thay đổi mô tả của một tool dựng sẵn, danh sách tool của một MCP server,
hoặc phiên bản chính sách của một hook sẽ thay đổi [hợp đồng của
run](./agent.md#hợp-đồng-của-run), và một run đã lưu chưa kết thúc (run chờ một phê duyệt
hoặc một câu trả lời, hoặc run mà một lần dừng để lại giữa chừng) không thể resume dưới nó
(`ContractMismatch`). CHANGELOG nêu tên các bản phát hành như vậy: hãy đọc các mục
"Unreleased" và các mục phiên bản của nó trước khi nâng cấp, và để các run đang tạm dừng kết
thúc trước.

### Kết nối và pool

- Một tiến trình server giữ một pool cho mỗi đích kết nối, dùng chung cho mọi agent
  được mở trên đó. Đích kết nối gồm host hoặc socket, cổng, cơ sở dữ liệu, người
  dùng, mật khẩu, `options` khi khởi động, `sslmode` và các tệp TLS, theo cách kho
  lưu trữ đọc DSN. Các DSN chỉ khác nhau về thứ tự tham số, hoặc ở các tham số khác
  như `application_name`, dùng chung một pool, và các kết nối của pool giữ
  `application_name` của lần mở đã tạo kết nối.
- Việc mở sẽ bỏ cuộc sau 5 giây khi không đến được cơ sở dữ liệu (`OpenError`, mã
  -32014, nêu host và cổng đã thử), thay vì thử lại lâu hơn.
- Một pool giữ tối đa `max_connections` kết nối (tối thiểu 1). Khi không có, mặc định
  theo máy: số core của nó, nhưng không dưới 10 và không quá 32 (32 trên máy 64 core).
  Hai con số đặt ra các giới hạn này:
  - Một run commit liên tiếp dùng khoảng hai kết nối, nên hãy dự trù khoảng hai kết
    nối cho mỗi run đang chạy cùng lúc, trên mỗi pool. Pool quá nhỏ làm các run phải
    chờ kết nối: trong bench 2000 session, pool 10 thêm 3 đến 4 s cho mỗi lượt, mặc
    định 32 (trên 64 core) khoảng 0,6 s, và pool 40 khoảng 0,4 s.
  - `max_connections` của chính Postgres (mặc định 100) được dùng chung bởi mọi pool
    của mọi tiến trình trên cơ sở dữ liệu, kể cả worker và công cụ quản trị. Đó là lý
    do mặc định dừng ở 32: ba tiến trình server ở mặc định vẫn vừa. Bốn thì không: khi đó
    run lỗi với `pool timed out` (xem [Ngân sách kết nối](#ngân-sách-kết-nối)). Với N tiến
    trình (sidecar) trên một cơ sở dữ liệu, đặt `stores.max_connections` của mỗi tiến trình
    khoảng (giới hạn của cơ sở dữ liệu trừ phần dự phòng) / N, ví dụ 20 cho bốn sidecar với
    giới hạn 100. Chỉ tăng `stores.max_connections` quá 32 sau khi đã tăng giới hạn của cơ sở
    dữ liệu, hoặc đặt một pooler như PgBouncer phía trước.
- Một lệnh gọi thấy mọi kết nối của pool đều bận sẽ chờ 30 giây, rồi lỗi với `pool timed out
  after 30s: raise max_pool_connections or lower concurrent runs`. Một run gặp lỗi này dừng ở
  trạng thái bền vững cuối cùng của nó; hãy resume nó. Server cũng ghi log một dòng `warn`
  cho mỗi run gặp lỗi này, kèm tenant, mức tối đa của pool, số kết nối đang mở và số đang
  dùng (`LINCH_LOG=warn`, mức mặc định, sẽ hiển thị nó).
- Lần mở đã kết nối một pool đặt kích thước của nó; các agent dùng chung pool dùng
  chung kích thước đó. Nhiều tiến trình server trên cùng một cơ sở dữ liệu mỗi tiến
  trình có pool riêng.
- Trên một `linch-server` có tenant, mỗi tenant có các pool riêng: hai tenant trên cùng
  một đích không bao giờ dùng chung pool, nên bên nào cũng không đặt được kích thước
  của bên kia. `stores.max_connections` của một tenant được phép tối đa bằng
  `max_pool_connections` của nó (kích thước pool mặc định khi không đặt); vượt quá,
  `open_agent` bị từ chối (`NotAllowed`, `kind == "pool_connections"`). Một cấu hình
  không có `max_connections` nhận kích thước mặc định hoặc mức trần, tùy giá trị nào
  nhỏ hơn ([Server](./server.md#config)).
- Mỗi store Postgres (một store cho mỗi agent đã mở) còn giữ **một kết nối riêng** nằm
  ngoài pool, dùng để gia hạn và release lease của run ([Run leases](#run-leases)), nên
  heartbeat của một run không bao giờ phải xếp hàng sau việc của run. Nó được mở ở lần
  gia hạn đầu tiên và được tính vào `max_connections` của chính Postgres: hãy dự trù
  `max_connections` cộng 1 cho mỗi agent mở trên một pool, và thêm một cho heartbeat job
  của worker. (Một workflow đang được giữ trên server chiếm thêm một backend, nằm ngoài
  pool: N workflow đang chạy là N kết nối thêm.)
- Một kết nối nhàn rỗi từ một giây trở lên được kiểm tra trước lần ghi kế tiếp và bị
  thay nếu cơ sở dữ liệu đã đóng nó (do khởi động lại, failover, hoặc idle timeout),
  nên một run đã chờ một lệnh gọi mô hình dài vẫn ghi được câu trả lời của nó. Một
  kết nối lỗi giữa chừng một lần ghi sẽ dừng run ở trạng thái bền vững cuối cùng;
  hãy resume nó.

### Ngân sách kết nối

Postgres có một giới hạn cho mọi thứ kết nối vào nó: `max_connections` của chính nó (mặc
định 100, trừ `superuser_reserved_connections`). Hãy đếm những gì fleet của bạn có thể mở
cùng lúc:

```text
connections = servers x (tenants x (max_pool_connections + agents per tenant)
                         + worker pool + worker lease connection)
              + extras
```

- **servers** là số tiến trình `linch-server` (hoặc sidecar nhúng) trên cơ sở dữ liệu.
- **tenants** là số tenant có một agent đang mở trên một server. Một tenant có một pool cho
  mỗi đích kết nối (host, cơ sở dữ liệu, người dùng và `search_path`), và hai tenant không
  bao giờ dùng chung một pool. Một server không có tenant (stdio) có một pool cho các agent
  cục bộ của nó.
- **max_pool_connections** là kích thước pool: `stores.max_connections` của tenant, tối đa
  bằng `max_pool_connections` của nó, và giá trị đó mặc định là số core của máy của server,
  giới hạn trong khoảng 10 đến 32. Một pool mở các kết nối của nó khi các run cần, nên một
  tenant yên tĩnh giữ ít kết nối, nhưng một tenant bận có thể đạt mức tối đa.
- **agents per tenant** là số agent tenant đang mở: store của mỗi agent giữ một [kết nối
  lease](#run-leases) riêng ngoài pool (+1 cho mỗi `PgRunStore`).
- **worker pool** là `worker.max_connections` (cùng mặc định) cho một server có phần `worker`,
  và 0 khi không có. Worker cũng giữ một kết nối lease cho heartbeat job (+1 cho mỗi worker;
  0 khi không có phần `worker`).
- **extras**: khóa migration chiếm một kết nối ngoài pool trong lúc một pool kết nối và
  migrate (các lần mở đồng thời của một schema mỗi lần cần một kết nối trong khoảnh khắc đó);
  một [workflow](./workflows.md) đang chạy giữ thêm một kết nối, ngoài mức tối đa của pool,
  trong suốt lần chạy của nó (một advisory lock cấp session); `LISTEN` của worker giữ một kết
  nối trong suốt thời gian nó chạy (hãy chừa chỗ cho nó trong `worker.max_connections`).

Ví dụ cụ thể: 3 server, mỗi server có 5 tenant (mỗi tenant một agent) với pool 10 kết nối mặc
định và một worker pool 10 kết nối, cần tối đa `3 x (5 x (10 + 1) + 10 + 1) = 198` kết nối, so
với 100 của Postgres. Đặt `max_pool_connections` là 4 cho mỗi tenant và `worker.max_connections`
là 4, thì con số là `3 x (5 x (4 + 1) + 4 + 1) = 90`, vừa khít: với 4 workflow đang chạy, một session quản trị và `migrate` của một lần deploy
là 96 trên 97 kết nối mà một Postgres mặc định còn lại sau `superuser_reserved_connections`.
Dùng 3 cho mỗi giá trị (`3 x (5 x (3 + 1) + 3 + 1) = 72`) để có thêm chỗ dư. Hãy dự trù khoảng hai kết nối cho mỗi run đang chạy cùng lúc,
trên mỗi pool; một pool nhỏ hơn mức đó làm các run phải chờ, và quá 30 s thì lỗi với `pool
timed out` (ở trên).

Mọi giới hạn của một tenant đều tính theo từng tiến trình, và không có giới hạn trên toàn
server:

- `max_runs`, `max_agents` và `max_connections` của một tenant được đếm riêng trong từng
  server, nên N server cho phép gấp N lần. Tương tự với `max_provider_concurrency`: một tiến
  trình worker hay một server thứ hai tự đếm riêng.
- Không có gì chặn tổng số trên các tenant trong một tiến trình, nên năm mươi tenant ở mặc
  định có thể mở năm mươi pool. Hãy chọn kích thước `max_pool_connections` theo số tenant mà
  bạn lưu trữ, không phải theo một tenant.

**Một pooler.** PgBouncer ở chế độ transaction chia sẻ một vài kết nối Postgres cho nhiều
client, phù hợp với nhiều tenant nhàn rỗi, nhưng bốn thứ trong linch-rust cần một kết nối cấp
session hoặc một tính năng mà một transaction pooler có thể không có:

- Khóa migration (`pg_advisory_lock`, giữ trên một kết nối riêng trong lúc migration chạy),
  khóa của một workflow đang chạy và khóa của đợt retention đều là advisory lock cấp session;
  qua một transaction pooler chúng có thể rơi vào các backend khác nhau hoặc sống lâu hơn
  bên giữ chúng.
- `LISTEN` của worker cần một session; một worker qua một transaction pooler mất các lần đánh
  thức và quay lại poll mỗi `poll_interval_ms`.
- sqlx cache các prepared statement theo từng kết nối (mặc định 100); một transaction pooler
  cần hỗ trợ prepared statement (PgBouncer 1.21 trở lên với `max_prepared_statements`) hoặc
  `statement-cache-capacity=0` trong DSN.
- `options=-c search_path=...` của DSN là một tham số khởi động; pooler phải chuyển nó đi (hoặc
  đặt `search_path` của role trong cơ sở dữ liệu thay vào đó).

Cách đơn giản là kết nối trực tiếp cho các worker, `migrate` và các workflow, và pooler cho
lưu lượng run của các server. Hãy thử pooler với cấu hình của chính bạn trước khi dựa vào nó;
trang này không khẳng định một cách thiết lập mà các test không bao phủ.

### Run leases

Trên Postgres, mỗi lúc chỉ một server điều khiển một run. Agent bắt đầu, resume,
approve hoặc answer một run giữ lease của nó và gia hạn mỗi một phần ba độ dài lease
trong khi điều khiển run, nên một lần gọi mô hình hay tool dài vẫn giữ được nó. Cho đến
khi run đó dừng, kết thúc, hoặc chờ một phê duyệt hay một câu trả lời, `resume`,
`approve` hoặc `answer` của agent khác bị từ chối với `RunLeased` (mã -32033) và không
ghi gì; `.owner` là id của server đang giữ nó (mờ đục: 8 ký tự hex ngẫu nhiên, hoặc `server_id` của server;
[Server nào điều khiển một run](./server.md#server-nào-điều-khiển-một-run)) và `.expires_in` là số giây lease của nó còn lại.

- Một run có stream kết thúc với lỗi, hoặc có kết nối bị đứt, được release ngay: server
  khác có thể resume nó liền. Các run của một server thoát bình thường (đầu vào của nó
  đóng, rời khỏi `async with Server()`) cũng vậy: nó chờ tối đa 5 s cho các lần release
  đó trước khi thoát.
- Một server crash hoặc treo không release gì. Các run của nó bị từ chối trong tối đa
  độ dài lease (mặc định 30 s), rồi server nào cũng resume được.
- Nếu một server bị đứng lâu hơn lease của nó (một tiến trình bị đóng băng, mất mạng
  tới cơ sở dữ liệu), server khác có thể tiếp quản các run của nó. Khi nó tỉnh lại, lần
  ghi kế tiếp của nó bị từ chối và run kết thúc với `LeaseLost` (mã -32022) mà không ghi
  gì: bên điều khiển mới của run sở hữu nó.
- Lease được gia hạn chừng nào run còn sống trên server của nó, mỗi một phần ba lease,
  bởi một task riêng: nó không chờ run, không chờ stream được đọc, không chờ pool. Ba
  việc từng làm một run mất lease khi server của nó vẫn khỏe, nay không còn nữa:
  - Một pool mà mọi kết nối đều bận (khóa hàng, đĩa chậm). Việc gia hạn dùng kết nối
    riêng của store, và bỏ cuộc sau một phần ba lease (cũng đặt làm `statement_timeout`
    của câu lệnh), nên một lần gia hạn kẹt chỉ tốn một nhịp; nhịp kế tiếp đến hạn sau
    một phần ba lease kể từ lúc nhịp trước bắt đầu. Một lần gia hạn thất bại vì lý do
    khác (cơ sở dữ liệu không với tới được) được thử lại ở nhịp sau, trên kết nối mới
    nếu kết nối cũ đã hỏng.
  - Một client ngừng đọc event của một run đủ lâu để hàng đợi của server đầy (xem
    [backpressure](../architecture/rpc.md#backpressure)). Run tạm dừng chờ client đó
    nhưng vẫn thuộc server của nó: không server nào khác resume được nó khi kết nối còn
    sống, và nó chạy tiếp khi client đọc lại. Một kết nối HTTP không đọc gì bị ping của
    [liveness](./server.md#liveness) kết thúc, và các run của nó bị drop (resume được từ
    trạng thái bền vững cuối cùng); một client Unix socket hoặc stdio không bao giờ đọc
    lại giữ run của nó cho đến khi đóng.
  - Một stream không ai poll (một host thư viện đã ngừng đọc): run không tiến lên, nhưng
    giữ lease cho đến khi stream bị drop.
- `stores.lease_ttl_ms` đặt độ dài lease, tối thiểu 1000. Ngắn hơn nghĩa là các run của
  một server đã crash được giải phóng sớm hơn, đổi lại thêm một lần ghi nhỏ cho mỗi run
  đang chạy mỗi một phần ba của nó.
- Cùng một agent handle không bao giờ chờ lease của chính nó: một run bạn đã drop
  stream resume ngay qua nó.

```python
{"type": "postgres", "dsn": "postgres://user:pass@db:5432/linch", "lease_ttl_ms": 10000}
```

Store memory không có lease: mỗi tiến trình có các run riêng, và agent giữ chúng là
agent duy nhất điều khiển được chúng.

Store memory giữ lịch sử của mọi run chừng nào agent còn sống, nên bộ nhớ của server tăng
theo số run: khoảng 110 KB mỗi run trong một lần đo trên server release (100 run, mỗi run một
lời gọi `Bash`, 24 MB sau 25 run và 32 MB sau 100; đóng agent không làm process nhỏ lại). Hãy
dùng store Postgres cho một agent sống lâu hoặc phục vụ nhiều run.

<a id="the-virtual-filesystem"></a>

## Retention

Postgres giữ mọi run, event và session cho đến khi có thứ gì đó xóa chúng. Một
schedule bắn mỗi phút thêm một run và một session mỗi phút (1440 mỗi ngày), và
mỗi lượt tool thêm khoảng 8 hàng event. Retention là cách riêng, tùy chọn của
server để chặn mức tăng đó. Nó tắt trừ khi phần `worker` của config server có
đối tượng `retention`:

```json
{"worker": {"dsn": {"env": "LINCH_DSN"},
            "retention": {"terminal_runs_days": 30, "idle_sessions_days": 90}}}
```

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `terminal_runs_days` | không có | Xóa run (bản ghi và mọi event của nó) đã kết thúc quá chừng này ngày |
| `idle_sessions_days` | không có | Xóa session (log, task và các cạnh task của nó) mà không có gì ghi vào trong chừng này ngày |
| `tombstone_days` | 30 | Một run đã xóa được nhớ bao lâu (xem [client thấy gì](#client-thấy-gì)) |
| `batch_size` | 500 | Số run hoặc session nhiều nhất mà một transaction xóa (1 đến 10000) |
| `interval_secs` | 3600 | Khoảng cách tối thiểu giữa hai đợt (ít nhất 60) |

Cần ít nhất một trong hai cửa sổ, cửa sổ là số ngày nguyên (không phải 0), và
một giá trị sai làm server dừng ngay lúc khởi động. Worker chạy một đợt trong
task riêng, nên một đợt dài không bao giờ làm chậm một lượt poll, và cả fleet
chỉ chạy một đợt tại một thời điểm: đợt thấy đợt của server khác đang chạy sẽ
dừng và thử lại ở interval kế tiếp. Cần có phần `worker`; server không có nó
thì không xóa gì.

### Cái gì bị xóa và cái gì được giữ

Hai cửa sổ là hai quyết định riêng. Một session có thể được người dùng tiếp tục
rất lâu sau khi các run của nó kết thúc, nên xóa run của một session cũ không
xóa session, và ngược lại.

| Luôn được giữ, bất kể cửa sổ | Vì sao |
|---|---|
| Run chưa terminal (`pending`, `running`, `awaiting_approval`, `awaiting_input`) | Một run bị tạm dừng chờ người dùng bao lâu cũng được; xóa nó sẽ kết thúc nó mà không có câu trả lời. Chính lệnh xóa cũng kiểm tra lại trạng thái |
| Run con (của subagent) khi run cha chưa terminal | Run cha đọc event của các con khi resume; thiếu chúng nó sẽ khởi động lại subagent |
| Run và session agent của workflow (id `wf-`) | Workflow được resume đọc run này khi journal chưa có bản ghi của lời gọi; thiếu nó agent sẽ chạy lại |
| Run mà một job đang xếp hàng hoặc đang chạy còn gọi tên | Job start được thử lại mà không thấy run sẽ bắt đầu một run khác |
| Session có run chưa terminal | Một run trong đó vẫn đọc log |

Các hàng bị xóa theo lô, mỗi lô một transaction riêng: một run cùng event của nó
(và một tombstone), một session cùng log, task và các cạnh task. Một lần crash
giữa các lô để mọi run và session còn nguyên hoặc đã mất hẳn. Đợt đi qua mỗi
bảng đúng một lần theo thứ tự khóa, nên hàng nó không xóa được không bị đọc lại
cho đến đợt sau.

Không có gì khác bị đụng đến. Lịch sử job và job thất bại có
`history_retention_ms` riêng; schedule, definition, file, bộ nhớ dài hạn,
mailbox, tool cache và journal workflow không thuộc retention. Hàng lịch sử của
job còn sống lâu hơn run nó gọi tên, nên `job.list` giống hệt trước và sau một
đợt, và lần bắn kế tiếp của schedule không bị dời.

Retention xóa dữ liệu kiểm toán: hết cửa sổ thì không ai đọc được event hay
report của run. Hãy chọn cửa sổ đủ dài cho những gì bạn cần nhìn lại, hoặc sao
chép thứ phải giữ trước khi hết hạn.

Session bị xóa vì nhàn rỗi là mất hẳn: bắt đầu một run trong id session đó sau
đó sẽ bắt đầu một session trống. Hoạt động của session là của riêng nó: session
cha còn dùng không giữ cho session của các subagent của nó sống, nên hãy để
`idle_sessions_days` dài hơn khoảng nghỉ dài nhất mà sau đó vẫn có người quay
lại.

### Client thấy gì

Đọc, resume, approve, answer, steer, cancel hay report một run đã xóa là
`RunPruned` (mã -32050, lớp con của `RunNotFound`; `data.run_id`), chứ không phải
"not found" thường, trong `tombstone_days`. Sau đó server đã quên nó và run đọc
ra là `RunNotFound`. Một id sai luôn là `RunNotFound`.

### Nâng cấp

Migration 10 thêm `linch_runs.ended_at` (đặt bởi commit làm run thành terminal),
`linch_sessions.updated_at` (đặt khi tạo session và mỗi lần append) và
`linch_pruned_runs`. Nó không ghi lại hàng nào, nên mất xấp xỉ cùng thời gian
trên database hàng triệu run như trên database trống (thời gian dựng hai index
ở [Vận hành](#vận-hành)). Các run đã terminal trước nó không có thời điểm kết
thúc: đợt đầu tiên có `terminal_runs_days` gán cho mỗi run thời điểm của đợt đó,
nên cửa sổ của nó bắt đầu từ đó, và đồng hồ của mọi session hiện có bắt đầu từ
migration. Những hàng đầu tiên bị xóa sau một cửa sổ kể từ lúc bạn bật
retention, không phải lúc nâng cấp.

### Vận hành

Đo trên một máy dùng chung 64 lõi, Postgres 16.15 cấu hình mặc định (`shared_buffers` 128 MB) ở cùng máy,
trên database có 520k run (4,2M event), 516k session (1,6M mục log) và 300k
hàng history, trong lúc đợt retention xóa và 4 client bắt đầu các run scripted
trên cùng schema:

- Trước khi xóa, các bảng có 2,0 GB event (4,0M hàng, 500 byte một hàng kèm
  index), 0,3 GB run (505k hàng, 600 byte), 0,09 GB session (500k hàng, 180
  byte) và 0,47 GB mục log (1,5M hàng, 310 byte). Một run scripted chỉ có văn
  bản (8 event, 1 session, 3 mục) để lại khoảng 5,7 KB: một schedule bắn mỗi
  phút thêm 8 MB mỗi ngày.
- Một đợt xóa 67k run cùng 536k event của chúng và 66k session cùng 214k mục
  log (khoảng 0,9M hàng) trong 187 giây, 4,7k hàng/giây, với lô 500 hàng và
  quãng nghỉ bên dưới. Một đợt trên 263k run và 259k session (3,4M hàng) mất
  24 phút với quãng nghỉ, trên máy bận hơn (một mẫu duy nhất; một job của
  người dùng khác đang chạy).
- Sau mỗi lô là một quãng nghỉ bằng thời gian lô đó chạy, nên một đợt chỉ làm
  database bận tối đa một nửa. Thông lượng run của 4 client (lô 100 run) trung
  bình 58 run/giây trong đợt và 67 run/giây ở một cửa sổ đối chứng không có
  đợt (mỗi bên một mẫu, trên database nhỏ hơn bốn lần, giữa các lô dao động từ
  3 đến 123 run/giây: chỉ mang tính tham khảo, không phải một cận), và mọi run
  đều thành công. Gán thời điểm kết thúc cho 500k run mà không nghỉ làm cùng
  các client đó giảm xuống trung bình 24 run/giây, vì vậy đợt có quãng nghỉ.
- Chọn một lô run đọc 500 mục index trong khoảng 5 ms ở mọi độ sâu của đợt; một
  đợt không có gì để xóa tốn 0,05 ms. Chọn một lô session mất 7 ms; quét session
  khi không có gì để xóa là quét đủ theo thứ tự khóa, 0,45 giây ở 516k session,
  mỗi đợt một lần.
- Migration không ghi lại hàng nào (mở một database 518k run, migrate hết 1,3
  giây). Index một phần trên run terminal tốn khoảng 77 byte mỗi hàng (39 MB
  cho 500k run) và được ghi một lần mỗi run, khi nó kết thúc.
- Xóa không làm file nhỏ lại: Postgres dùng lại các trang đã giải phóng sau khi
  autovacuum đã thấy chúng, còn `VACUUM FULL` hoặc `pg_repack` trả dung lượng
  cho hệ điều hành. Một đợt tạo hàng chết với tốc độ lô của nó, nên với bảng
  hàng triệu hàng hãy tăng ngưỡng autovacuum cho đợt đầu hoặc chạy
  `VACUUM (ANALYZE)` sau nó.
- `batch_size` lớn hơn xóa nhanh hơn và giữ khóa của các hàng nó xóa lâu hơn;
  mặc định là 500.

Các hàng terminal không làm chậm một run theo cách đo được: với 520k run và
4,2M event này trong schema và chưa xóa gì, thông lượng run scripted ở 1, 4 và
16 client (trung vị của ba lô 600 run, hai chuỗi mỗi bên) là 43, 159 đến 221 và
539 đến 728 run/giây so với 44 đến 46, 187 đến 204 và 583 đến 649 run/giây trên
schema trống: các chuỗi chồng lên nhau và nhiễu của máy lớn hơn mọi chênh lệch.
(Chuỗi đầu tiên gồm các lô 200 run đọc được 481 run/giây ở 16 client so với 709
trên schema trống; các chuỗi 600 run không lặp lại điều đó.) Các truy vấn
claim, lease, hết hạn và due-fire giữ nguyên kế hoạch (0,02 đến 1,4 ms) vì chúng
đọc các index một phần trên hàng còn sống; một lần resume run, từ lúc spawn
server đến kết quả, mất 80 ms ở cả hai. Cái giá của việc giữ mọi run là đĩa
(các kích thước ở trên) và các lượt quét đọc cả một namespace như `job.list`
(205 ms ở 180k hàng history), không phải đường đi của run.

## Hệ thống tệp ảo

Khi `filesystem` bật, nơi các tệp nằm phụ thuộc vào kho lưu trữ:

```python
import asyncio
import os

from linch_rust import AgentConfig, Server, ToolCallEndEvent
from linch_rust.scripted import MODEL, text_turn, tool_turn

TURNS = [
    tool_turn("c1", "write_file", {"path": "/plan.md", "content": "1. ship"}),
    text_turn("Wrote the plan."),
    tool_turn("c2", "read_file", {"path": "/plan.md"}),
    text_turn("Read the plan."),
]


def config(stores: dict) -> AgentConfig:
    return {
        "namespace": "files-demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": TURNS},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "filesystem": {},
        "stores": stores,
    }


async def main() -> None:
    for stores in ({"type": "memory"}, {"type": "postgres", "dsn": os.environ["DATABASE_URL"]}):
        async with Server() as server:
            agent = await server.open_agent(config(stores))
            await (await agent.run("writer", "Write the plan")).result()
            async for envelope in await agent.run("reader", "Read the plan"):
                if isinstance(envelope.event, ToolCallEndEvent):
                    print(stores["type"], "|", envelope.event.result["content"][0]["text"])


asyncio.run(main())
```

```text
memory | File not found: '/plan.md'. Use ls() to see available files.
postgres | 1. ship
```

Với kho trong bộ nhớ, mỗi session có tệp riêng, mất theo server; với Postgres, mọi
session của namespace dùng chung chúng (`linch_files`), và một run được resume trong
tiến trình khác vẫn tìm thấy kết quả đã được chuyển ra ngoài.

## Các trang liên quan

- [Run](./runs.md#what-is-durable-and-when): cái gì được ghi và khi nào.
- [Cấu hình agent](./agent.md): `namespace`, `stores`, `filesystem`.
- [Khắc phục sự cố](./troubleshooting.md#postgres): lỗi kết nối và migration.
- [Kiến trúc: kho lưu trữ](../architecture/stores.md): hợp đồng, schema, đường ghi
  và cách định cỡ pool.
