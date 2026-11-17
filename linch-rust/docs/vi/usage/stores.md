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

Agent đầu tiên mở trên một DSN sẽ kết nối và áp dụng các migration. Việc này an toàn
ở mọi lần khởi động: các migration đã áp dụng được bỏ qua, và các lần mở đồng thời
chờ trên một khóa cơ sở dữ liệu. Các phiên bản đã áp dụng được ghi trong
`linch_migrations`, nên bảng của công cụ migration riêng của bạn trong cùng schema
sẽ không xung đột.

| Bảng | Chứa |
|---|---|
| `linch_runs`, `linch_run_events` | Trạng thái và checkpoint của từng run, cùng các event bền vững của nó (thứ mà `agent.events` trả về) |
| `linch_sessions`, `linch_session_entries` | Nhật ký hội thoại của từng session |
| `linch_files` | Hệ thống tệp ảo, theo từng namespace |
| `linch_tasks`, `linch_task_edges` | Task của session và các phụ thuộc của chúng |

Migration 6 thêm hai cột nullable vào `linch_runs` (`lease_holder`, `lease_until`) cho
[run leases](#run-leases). Nó không viết lại hàng nào và không giữ khóa lâu. Một server
của phiên bản cũ hơn từ chối khởi động trên một cơ sở dữ liệu đã migrate vượt qua nó,
như với mọi migration: hãy nâng cấp mọi server trước khi một trong số chúng khởi động
lại trên phiên bản cũ.

- Role trong DSN cần quyền tạo bảng và index trong schema của nó ở lần đầu; sau đó
  nó chỉ đọc và ghi các hàng.
- Một migration không hoàn tất trong 30 giây làm việc mở thất bại với `OpenError`
  (mã -32014); nguyên nhân thường gặp là một tiến trình khác đang giữ khóa migration.
- Migration chỉ được thêm vào, không bao giờ sửa. Đừng sửa hay xóa các bảng
  `linch_` bằng tay khi các run còn đang chạy.
- Payload được lưu dưới dạng `TEXT`, không phải `JSONB`, nên các byte mà mô hình đã
  thấy chính là các byte được đọc lại.
- Client không có lệnh gọi nào xóa run hay session.

Chi tiết schema, đường ghi và quy tắc từ chối nằm ở
[trang kiến trúc về kho lưu trữ](../architecture/stores.md#postgres-stores).

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
    run lỗi với `pool timed out`. Với N tiến trình (sidecar) trên một cơ sở dữ liệu, đặt
    `stores.max_connections` của mỗi tiến trình khoảng (giới hạn của cơ sở dữ liệu trừ
    phần dự phòng) / N, ví dụ 20 cho bốn sidecar với giới hạn 100. Chỉ tăng
    `stores.max_connections` quá 32 sau khi đã tăng giới hạn của cơ sở dữ liệu, hoặc đặt
    một pooler như PgBouncer phía trước.
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
- Một kết nối nhàn rỗi từ một giây trở lên được kiểm tra trước lần ghi kế tiếp và bị
  thay nếu cơ sở dữ liệu đã đóng nó (do khởi động lại, failover, hoặc idle timeout),
  nên một run đã chờ một lệnh gọi mô hình dài vẫn ghi được câu trả lời của nó. Một
  kết nối lỗi giữa chừng một lần ghi sẽ dừng run ở trạng thái bền vững cuối cùng;
  hãy resume nó.

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
- Nếu một server bị đứng lâu hơn lease của nó (một lần tạm dừng dài, mất mạng), server
  khác có thể tiếp quản các run của nó. Khi nó tỉnh lại, lần ghi kế tiếp của nó bị từ
  chối và run kết thúc với `LeaseLost` (mã -32022) mà không ghi gì: bên điều khiển mới
  của run sở hữu nó.
- Lease chỉ được gia hạn khi run còn tiến triển trên server của nó. Một client ngừng đọc
  event của một run đủ lâu để hàng đợi của server đầy (xem
  [backpressure](../architecture/rpc.md#backpressure)) sẽ làm run tạm dừng, và sau trọn một lease, server khác có thể
  tiếp quản nó; run bị tạm dừng sau đó kết thúc với `LeaseLost` khi nó chạy tiếp.
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
