# Scheduler

[Bản gốc tiếng Anh](../../usage/scheduler.md)

Dùng trang này để chạy một agent ở chế độ nền: theo một mẫu cron hoặc một interval, như
một heartbeat liên tục kiểm tra một session, hoặc chạy một lần, được đưa vào hàng đợi cho
một worker.

[<- Hướng dẫn sử dụng](./README.md)

Một lịch và một run trong hàng đợi là các hàng trong cơ sở dữ liệu Postgres của agent.
Chúng không làm gì cho đến khi một **worker** chạy: một `linch-server --config FILE` có
cấu hình chứa phần `worker` trên cùng cơ sở dữ liệu và schema. Kết nối client đã tạo ra
chúng không bao giờ chạy chúng, và một server stdio (`Server()`) không chạy worker nào.

```text
client ── schedule.create / job.enqueue ──> Postgres (schedules, jobs)
                                                │
linch-server --config (worker section) ─────────┘  fires schedules, takes jobs,
                                                    opens the stored agent, runs it
```

## Chạy một worker

Thêm phần `worker` vào [cấu hình server](./server.md#cấu-hình). Một server có thể chỉ chạy
worker (không có `socket` hay `http`); khi đó các tenant của nó không cần token.

```json
{
  "socket": "/run/linch/linch.sock",
  "worker": {"dsn": {"env": "LINCH_WORKER_DSN"}},
  "tenants": {
    "acme": {
      "tokens": [{"env": "LINCH_TOKEN_ACME"}],
      "allow": {
        "postgres_dsns": ["postgres://linch_acme@db.internal:5432/linch"],
        "max_schedules": 20,
        "max_queued_jobs": 200,
        "min_schedule_interval_ms": 60000
      }
    }
  }
}
```

| Trường `worker` | Khi không có | Ý nghĩa |
|---|---|---|
| `dsn` | bắt buộc | `{"env": "VAR"}` hoặc `{"value": "..."}`: cơ sở dữ liệu và schema có hàng đợi mà nó phục vụ. Các run nền lưu qua nó |
| `owner` | `linch-server-<pid>-<start time>` | Tên mà các job và claim run của nó mang; duy nhất cho mỗi worker |
| `queues` | `["default"]` | Các hàng đợi mà nó nhận job, lần lượt một job từ mỗi hàng đợi |
| `max_jobs` | 32 | Số job nó chạy cùng lúc |
| `max_connections` | kích thước pool mặc định | Pool riêng của nó cho hàng đợi; mỗi run dùng pool của tenant của nó |
| `poll_interval_ms` | 1000 | Thời gian chờ dài nhất giữa các lần poll; các lịch đến hạn được kiểm tra mỗi interval một lần |
| `visibility_timeout_ms` | 30000 | Một job đã được nhận vẫn bị ẩn bao lâu khi không có heartbeat. Hãy giữ `stores.lease_ttl_ms` của các agent không dài hơn mức này |
| `retry_delay_ms` | 5000 | Thời gian chờ trước lần thử kế tiếp của một job thất bại; nhân đôi mỗi lần, tối đa 5 phút |
| `history_retention_ms` | 7 ngày | Các job đã kết thúc còn đọc được bao lâu bằng `job` và `jobs` |
| `local_namespaces` | không có | Các namespace root nằm ngoài mọi tenant mà nó cũng chạy job của, không kiểm tra (các lịch do một client stdio tạo); `dev` bao gồm `dev` và `dev/...` |

Bạn có thể chạy bao nhiêu server như vậy trên một cơ sở dữ liệu tùy ý: mỗi lần kích hoạt
được đưa vào hàng đợi một lần và mỗi job chạy trên một worker tại một thời điểm. Một server
không có phần `worker` không chạy vòng lặp nào và không gửi truy vấn nào cho nó.

Một lịch hoặc run trong hàng đợi được lưu trong cơ sở dữ liệu và schema của agent đã tạo
ra nó, và một worker chỉ phục vụ những cái thuộc `dsn` của chính nó, nơi nó cũng chạy chúng.
Một agent có DSN store trỏ tới cơ sở dữ liệu (hoặc schema) khác cần một worker ở đó: không
worker nào khác từng kích hoạt các lịch của nó. Hãy trỏ `worker.dsn` tới cùng cơ sở dữ liệu
và schema với DSN store của các agent. Khi chúng chỉ khác nhau ở user, các run nền kết nối
bằng user của worker.

Worker chạy các job của mọi tenant trong cấu hình. Agent của mỗi job được kiểm tra lại với
các allowlist của tenant của nó ở mỗi lần thử, như `open_agent` sẽ làm, ngoại trừ DSN của
store, vốn là của worker. Vì vậy một tenant bị siết allowlist sẽ ngừng chạy ở lần kích hoạt
kế tiếp: job đó thất bại kèm lý do. Mỗi run nền chiếm một trong các chỗ `max_runs` của
tenant trong lúc chạy; khi không còn chỗ, job chờ.

## Tenant được lập lịch gì

| Trường `allow` | Khi không có | Tác dụng |
|---|---|---|
| `max_schedules` | không có lịch | Số lịch mà tenant được giữ trong mỗi cơ sở dữ liệu, trên các namespace của nó. Không có: `create_schedule` ném `NotAllowed` (`kind == "schedules"`); chạm mức trần thì `LimitReached` (`kind == "schedules"`) |
| `min_schedule_interval_ms` | 60000 | Khoảng thời gian ngắn nhất giữa hai lần kích hoạt: một interval, hoặc các lần kích hoạt gần nhau nhất của một mẫu cron (trong 512 lần kích hoạt kế tiếp). Nhanh hơn: `NotAllowed` (`kind == "schedule_interval"`) |
| `max_queued_jobs` | không có run nền | Số job chưa xong (đang chờ, đang chạy hoặc đang retry) mà tenant được có khi gọi `enqueue`. Không có: `NotAllowed` (`kind == "background_runs"`); chạm mức trần thì `LimitReached` (`kind == "jobs"`). Các lần kích hoạt không bị từ chối, chỉ được đếm |

Một client stdio không có tenant và không có giới hạn.

## Agent nào chạy nền được

Agent bạn lập lịch cần các store Postgres và không có host tool hay callback: một run nền
không có client nào để gọi ngược lại. Nếu không, lời gọi ném `NotBackground` với `reason` là
`memory_store`, `host_tools` hoặc `callbacks`. Hãy cho agent các tool workspace hoặc MCP
thay vào đó.

Một lịch luôn chạy cấu hình mà nó được tạo cùng; muốn đổi thì mở lại agent và tạo một lịch
mới.

### Những gì được lưu ở trạng thái nghỉ

Cấu hình của agent, như lúc nó được mở (sau các kiểm tra của tenant), được lưu dưới dạng văn
bản thuần trong bảng `linch_worker_agents` của cơ sở dữ liệu của agent, một lần cho mỗi cấu
hình khác nhau, vì worker phải mở lại đúng agent đó. Mọi thứ trong nó đều được lưu **trừ DSN
store** (worker kết nối bằng DSN của chính nó):

- các giá trị `env` và đối số của MCP stdio, cùng URL và giá trị header của MCP HTTP, đúng
  như được cung cấp;
- các cài đặt provider, kể cả mọi header request và trường body bổ sung mà cấu hình đặt;
- phần còn lại của cấu hình: `spec`, permission, workspace, profile.

Các hàng job, history và lịch gọi cấu hình bằng digest SHA-256 của nó, và không câu trả
lời `schedule.*` hay `job.*` nào, không lỗi nào mang các giá trị của nó, kể cả lỗi của một
run nền mà worker không mở được. Lỗi của chính một run thì khác: nó chứa những gì provider,
một tool hay một MCP server đã nói, và một endpoint lặp lại một credential nó nhận được sẽ
đưa credential đó vào đó, như với một run được khởi động trực tiếp. Một cấu hình đã lưu bị xóa khi không lịch nào và không
job chưa xong nào gọi tên nó: một worker kiểm tra từ lần poll đầu tiên và cứ 10 phút một lần
sau đó (`prune_definitions`). Hãy giữ secret ra khỏi cấu hình được lập lịch khi có thể (một
key mà server đọc từ môi trường của nó thì không bao giờ được lưu), và bảo vệ cơ sở dữ liệu
như bạn bảo vệ các secret đó.

## Lập lịch một run

```python
from linch_rust import Server

async with Server.connect("unix:/run/linch/linch.sock", token=token) as server:
    agent = await server.open_agent(config)  # Postgres stores, no host tools

    # Every day at 07:00 Berlin time, each fire in a new session.
    digest = await agent.create_schedule(
        "Write the daily digest.", cron="0 7 * * *", timezone="Europe/Berlin", id="digest"
    )
    print(digest["next_fire"])  # RFC 3339, UTC

    # Every 15 minutes.
    await agent.create_schedule("Check the queue.", interval_ms=15 * 60 * 1000)
```

| Lời gọi | Làm gì |
|---|---|
| `await agent.create_schedule(prompt, *, cron=None, timezone=None, interval_ms=None, session_id=None, id=None, queue=None, principal=None)` | Lưu một lịch; đưa `cron` (năm trường, hoặc sáu trường với giây ở đầu) hoặc `interval_ms`. `timezone` là một tên IANA, mặc định UTC. `id` mặc định là một uuid |
| `await agent.heartbeat(session_id, *, interval_ms=None, cron=None, timezone=None, prompt=CHECK_IN, id=None, queue=None, principal=None)` | Một lịch mà các lần kích hoạt tiếp tục một session (bên dưới) |
| `await agent.schedules()` | Các lịch của namespace của agent, theo id |
| `await agent.pause_schedule(id)` | Dừng các lần kích hoạt của nó; các run đã trong hàng đợi vẫn ở đó |
| `await agent.resume_schedule(id)` | Kích hoạt lại từ tick kế tiếp sau bây giờ |
| `await agent.delete_schedule(id)` | Xóa nó; `False` khi không có lịch nào. Các run đã trong hàng đợi hoặc đang chạy vẫn hoàn tất |

Một `Schedule` là một dict: `id`, `namespace`, `queue`, `prompt`,
`session_id` (chỉ với session cố định), `cron` và `timezone` hoặc
`interval_ms`, `paused`, `next_fire`, `last_fire`, `fires` (các lần kích hoạt đã đưa một run
vào hàng đợi), `skips` (các lần kích hoạt bị bỏ qua) và `created_at`.

### Múi giờ

Một mẫu cron được đọc trong múi giờ của nó, kể cả giờ mùa hè. Một thời điểm kích hoạt không
tồn tại (02:30 vào đêm đồng hồ chỉnh tiến) được dời tới thời điểm hợp lệ đầu tiên sau khoảng
trống; một thời điểm xảy ra hai lần (01:30 vào đêm đồng hồ chỉnh lùi) chỉ kích hoạt một lần,
vào lần đầu. Một interval bỏ qua múi giờ.

### Session

Không có `session_id`, mỗi lần kích hoạt bắt đầu session riêng của nó,
`schedule:<id>:<fire time>`, nên các lần kích hoạt không bao giờ chia sẻ một cuộc hội thoại
và có thể chạy cùng lúc.

Có `session_id`, mỗi lần kích hoạt là một run mới trong session đó: một heartbeat.

### Một run nền hành động thay cho ai

Truyền cùng `principal` mà bạn sẽ truyền cho `agent.run`
([permission rule](./tools.md#permission-rules)). Nó được lưu cùng lịch hoặc run trong hàng
đợi, và run của mọi lần kích hoạt (cùng việc worker resume hoặc approve nó) hành động thay
cho nó, nên một permission rule có `role` hoặc `unless_role` có hiệu lực ở chế độ nền như với
một run bắt đầu trực tiếp. Không có principal, run không giữ role nào: một rule có `role`
không bao giờ khớp với nó còn rule có `unless_role` thì luôn khớp. Principal của một tenant
phải nêu tên tenant (`NotAllowed`, `kind == "principal"`). Hãy lập lịch cho một caller bị hạn
chế bằng principal của caller đó, đừng bao giờ không có principal.

## Heartbeat: một session, được kiểm tra liên tục

```python
beat = await agent.heartbeat("ops-room", interval_ms=30 * 60 * 1000)
```

Mỗi lần kích hoạt là một run mới trong `ops-room` với prompt `CHECK_IN` (hoặc `prompt` của
bạn), nên agent thấy toàn bộ cuộc hội thoại cho đến lúc đó. Hai lần kích hoạt không bao giờ
chồng lên nhau: **một lần kích hoạt bị bỏ qua** (được đếm trong `skips`, không đưa vào hàng
đợi) khi

- job của lần kích hoạt trước chưa xong (đang chờ, đang chạy hoặc đang retry), hoặc
- session có một run chưa terminal: một run đang tiến hành (kể cả run của bạn từ một
  client), hoặc một run đang suspend chờ một approval hay một câu trả lời `AskUser`.

Bỏ qua, thay vì xếp hàng phía sau, là có chủ ý: một lần check-in phải chờ sau một run dài
chỉ lặp lại lần kế tiếp, và một run chờ một người sẽ khiến mỗi lần kích hoạt chồng thêm một
job. Để tiếp tục một run heartbeat đang suspend, hãy đọc `run_id` của nó từ
`agent.jobs(schedule_id=...)` và trả lời nó bằng `agent.approve` hoặc `agent.answer` trên
một agent có cùng cấu hình, hoặc kết thúc nó bằng `agent.cancel(run_id)`; lần kích hoạt kế tiếp sau khi nó kết thúc sẽ chạy lại.

Một run không người trông coi cần approval sẽ suspend như bất kỳ run nào khác: job là
`done`, run đang chờ. Hãy dùng một permission mode hoặc rule cho phép những gì một run nền
phải làm.

## Đưa một run một lần vào hàng đợi

```python
job = await agent.enqueue("inbox", "Summarize the inbox.")
done = await agent.wait_job(job["job_id"], timeout=300)
if done["state"] == "done":
    events = await agent.events(done["run_id"])
```

| Lời gọi | Làm gì |
|---|---|
| `await agent.enqueue(session_id, prompt, *, queue=None, principal=None)` | Đưa một run vào hàng đợi; một worker bắt đầu nó. Một session có run đang sống khiến nó chờ (retrying) đến khi run đó kết thúc |
| `await agent.job(job_id)` | Một job của namespace của agent, còn sống hoặc đã kết thúc |
| `await agent.jobs(*, schedule_id=None, limit=None)` | Các job, mới nhất trước (mặc định 50, tối đa 500); chỉ của một lịch khi có `schedule_id` |
| `await agent.wait_job(job_id, *, poll=0.25, timeout=None)` | Poll cho đến khi job là `done`, `failed` hoặc `superseded` |

Một `Job` là một dict: `job_id`, `queue`, `state`, `session_id`, `run_id`
(khi một worker đã bắt đầu nó), `schedule_id` và `fire_at` (cho một lần kích hoạt),
`attempts`, `failures`, `error`, `created_at`, `finished_at`.

| `state` | Nghĩa là |
|---|---|
| `queued` | Đang chờ một worker |
| `running` | Một worker giữ nó và điều khiển run của nó |
| `retrying` | Một lần thử kết thúc mà chưa hoàn tất nó; `error` nói lý do (một lỗi, hoặc một session bận). Nó sẽ được thử lại sau |
| `failed` | Nó không bao giờ có thể thành công, hoặc đã thất bại 5 lần; `error` nói lý do. Run của nó, nếu có, cũng bị đánh dấu thất bại khi không thể tiếp tục |
| `done` | Run của nó đã kết thúc, hoặc suspend chờ một approval hay một câu trả lời |
| `superseded` | Một owner khác đã claim run của nó, run tiếp tục ở đó |

Các event của run là bền vững: đọc chúng bằng `agent.events(run_id)` trên một agent của
namespace. Các run nền không gửi stream trực tiếp tới client nào.

## Độ bền vững

- **Mỗi lần kích hoạt được đưa vào hàng đợi đúng một lần.** Hàng của một lịch đến hạn bị
  khóa, job của nó được chèn và lần kích hoạt kế tiếp của nó được dời trong một transaction,
  nên một worker sập trước khi commit không để lại gì và lần kích hoạt được thực hiện ở lần
  poll kế tiếp; các worker poll cùng lúc bỏ qua hàng đã bị khóa. Mỗi job cũng có khóa duy
  nhất `schedule:<id>:<fire time>`.
- **Mỗi job chạy trên một worker tại một thời điểm.** Một job đã được nhận bị ẩn trong
  visibility timeout, được heartbeat của worker nó gia hạn. Job của một worker đã sập hiện
  lại và một worker khác nhận nó; lease và epoch của run chặn chủ cũ ra ngoài, và run
  resume từ trạng thái bền vững cuối cùng của nó ([Run](./runs.md)).
- **Các lần kích hoạt bị lỡ được bù.** Sau khi mọi worker ngừng hoạt động, một lịch kích
  hoạt từng tick mà nó đã lỡ, theo thứ tự, mỗi lần poll một lần, như Python Linch. Một
  heartbeat bỏ qua chúng trong lúc lần kích hoạt bù đầu tiên của nó chạy, nên nó chỉ chạy
  một lần. Các lần kích hoạt bị lỡ khi đang tạm dừng không được thực hiện.
- **Xóa một lịch chỉ dừng các lần kích hoạt trong tương lai.** Các job đang chờ và đang
  chạy của nó vẫn hoàn tất.

## Lỗi

| Được ném | Khi |
|---|---|
| `NotBackground` (-32040) | Agent có kho lưu trữ trong bộ nhớ, host tool hoặc callback; `.reason` |
| `ScheduleNotFound` (-32041) | `pause_schedule` hoặc `resume_schedule` với một id mà namespace không có |
| `ScheduleExists` (-32042) | `create_schedule` với một id mà namespace đã có |
| `JobNotFound` (-32043) | `job` với một id mà namespace không có, hoặc lịch sử của nó đã bị dọn |
| `InvalidSchedule` (-32044) | Một mẫu cron không parse được hoặc không bao giờ kích hoạt, một múi giờ không biết, một interval ngoài phạm vi, hoặc có cả hai hay không có cái nào trong `cron` và `interval_ms`; `.reason` |
| `NotAllowed` (-32004) | Một tenant không có `max_schedules` hoặc `max_queued_jobs`, hoặc một lịch nhanh hơn `min_schedule_interval_ms`, hoặc một principal của tenant khác; `.kind` |
| `LimitReached` (-32005) | Tenant đang giữ đủ `max_schedules` (`kind == "schedules"`) hoặc `max_queued_jobs` (`kind == "jobs"`) |

Tất cả đều là `SchedulerError` trừ hai cái cuối.

## Khác Python Linch ở đâu

| Python Linch (`linch.worker`) | linch-rust |
|---|---|
| `create_schedule(pool, session_id, payload, cron=..., interval_s=...)`; cron theo UTC, năm trường | `agent.create_schedule(prompt, cron=..., timezone=..., interval_ms=...)`: mọi múi giờ IANA, năm hoặc sáu trường |
| Một lịch luôn nhắm vào một session | Không có `session_id`, mỗi lần kích hoạt bắt đầu session riêng của nó |
| Các lần kích hoạt của một session xếp hàng sau nhau | Một heartbeat bỏ qua một lần kích hoạt khi session của nó đang bận |
| `cancel_schedule` vô hiệu hóa các lần kích hoạt trong tương lai | `pause_schedule`, `resume_schedule` và `delete_schedule` |
| Host truyền một `agent_factory` cho `Worker` của nó | Cấu hình của agent được lưu cùng lịch; worker của server mở nó |
| `RunRequest.principal` được worker khôi phục | `principal=` trên `create_schedule` và `enqueue`, được đưa cho run của mọi lần kích hoạt |
| `register_schedule_tools` cho phép model lập lịch | Chưa có |
| Các lần kích hoạt bị lỡ được bù theo thứ tự | Giống nhau |

## Các trang liên quan

- [Server](./server.md): cấu hình, tenant và allowlist.
- [Run](./runs.md): resume, approve, answer, và những gì bền vững.
- [Kho lưu trữ](./stores.md): DSN Postgres, schema và lease.
- [Kiến trúc worker](../architecture/worker.md): hàng đợi, lease và lịch bên trong.
