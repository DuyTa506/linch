# Tổng quan hệ thống

[Bản gốc tiếng Anh](../../architecture/overview.md)

> Dùng trang này khi bạn cần nắm cấu trúc tổng thể của runtime, ranh giới với ứng dụng chủ
> và luồng dữ liệu lưu trữ bền vững.

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Linch 3.0 là một lõi agent bất đồng bộ, không có giao diện. Nó được nhúng vào một dịch vụ chủ;
ứng dụng chủ quản lý HTTP, xác thực, định tuyến tenant và SDK OpenTelemetry.
Linch quản lý vòng lặp run độc lập với provider, thực thi tool, phân quyền,
các phép chiếu dữ liệu session, trạng thái run bền vững và các điểm mở rộng. Lõi không có UI.

Bề mặt Python được hỗ trợ là `linch.__all__`. Các package `linch.ext.*` là
những phần mở rộng tường minh và không được package lõi import. `linch.worker`
cũng cần được import tường minh vì nó nạp runtime PgQueuer tùy chọn.

## Cấu trúc runtime

Khi nhúng trong một tiến trình, ứng dụng chủ tạo một `Agent`, lấy một
`Session` và tiếp nhận luồng event có kiểu từ `Session.run()` hoặc
`Session.resume()`. Với một cụm worker, các tiến trình API đưa run vào hàng đợi và các tiến trình
worker thực thi chúng. Postgres là nguồn dữ liệu chuẩn, được lưu trữ bền vững cho session,
run, checkpoint, lease và các event của run có số thứ tự.

```mermaid
flowchart LR
    subgraph Host["Host service"]
        API["HTTP/API process"]
        OTel["OpenTelemetry SDK/exporter"]
        Deps["RunOptions(deps=...)\ntenant services"]
    end

    subgraph Dispatch["Optional linch.worker"]
        Queue["PgQueuer"]
        Worker["Worker\nclaim / renew / release"]
    end

    subgraph Kernel["Linch headless kernel"]
        Agent["Agent\nContext + registries"]
        Session["Session\nappend-only SessionLog"]
        Loop["loop/runner.py\nturn phases"]
        Guard["turn_stopping\nreliability listeners"]
        Scheduler["scheduler.py\npermission + tool execution"]
        Provider["providers/*\nnormalized provider stream"]
    end

    subgraph Durable["Postgres source of truth"]
        Sessions["PostgresSessionStore"]
        Runs["PostgresRunStore\nruns + leases + checkpoints"]
        Events["run_events\n(run_id, seq)"]
    end

    API -->|"enqueue_run"| Queue
    Queue --> Worker
    Worker -->|"RunLease(epoch)"| Runs
    Worker --> Agent
    API -->|"embedded run"| Agent
    Agent --> Session --> Loop
    Loop --> Provider
    Loop --> Scheduler
    Loop --> Guard
    Scheduler --> Deps
    Session <-->|"messages / snapshots"| Sessions
    Loop <-->|"checkpoint before side effects"| Runs
    Runs --> Events
    Events -->|"load_events(after_seq=...)"| API
    Agent -.->|"tracer + meter resolved from Context"| OTel
```

## Ranh giới và các bất biến

- `Agent` chứa cấu hình và các dịch vụ theo phạm vi. Nó quản lý `Context`
  riêng của từng agent; khi được cung cấp `Context` của ứng dụng chủ, phạm vi
  của agent là phạm vi con và phạm vi cha vẫn do ứng dụng chủ quản lý. `Session`
  chứa trạng thái hội thoại và run. `SessionLog` là nguồn chỉ ghi thêm cho các
  phép chiếu `provider_view` và `full_history` thuộc session; context theo phạm vi
  request được lắp ghép vào `ProviderRequest` và không được ghi thêm vào nhật ký.
- Vòng lặp run là bất đồng bộ. Các adapter provider và kho lưu trữ là protocol
  bất đồng bộ, còn các hàm tool đồng bộ gây chặn được adapter `FunctionTool`
  chuyển ra khỏi event loop.
- Một run có lease chỉ có tối đa một worker đang hoạt động nắm quyền sở hữu.
  `RunLease.epoch` là một fencing token. `claim()` tăng epoch khi quyền sở hữu
  thay đổi; `renew()` giữ nguyên epoch; thao tác ghi với epoch cũ sẽ phát sinh `LeaseLostError`.
- Vòng lặp ghi checkpoint trước một thao tác bên ngoài mà kết quả của nó phải
  có thể khôi phục được. `provider_pending` được lưu trước một luồng provider,
  còn `tool_batch_pending`/`tool_executing` được lưu trước khi thực thi tool. Các kho
  lưu trữ có thể commit các event của run đang chờ cùng với checkpoint thông qua
  `save_checkpoint_with_events()`.
- Các event của run được lưu bền vững có số thứ tự riêng cho từng run. Bên đọc kết nối lại
  bằng `PostgresRunStore.load_events(run_id, after_seq=...)`; thông báo chỉ dùng để
  đánh thức bên đọc, không phải nguồn dữ liệu chuẩn.
- Trạng thái cơ sở dữ liệu bền vững nằm trong Postgres. `InMemorySessionStore` và
  `InMemoryRunStore` là các triển khai cục bộ trong tiến trình dành cho kiểm thử và
  phát triển.
- OpenTelemetry là API cho khả năng quan sát. Khi ứng dụng chủ chưa cấu hình SDK,
  API không thực hiện thao tác nào. Tracer và meter được phân giải qua `Context`
  của agent, nên ứng dụng chủ và kiểm thử có thể cung cấp provider theo phạm vi mà
  không cần trạng thái Linch toàn tiến trình có thể thay đổi.
- `Agent` mặc định dùng `ReliabilityPolicy()`; listener của nó chạy trên bộ điều phối
  `loop/turn_stopping.py` và có thể yêu cầu các lượt sửa lỗi có giới hạn đối với
  phản hồi provider không hợp lệ. Truyền `reliability=None` để tắt cơ chế này.

## Kết nối với ứng dụng chủ

Các kho lưu trữ bền vững có đường dẫn import riêng vì Postgres là tùy chọn:

```python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

dsn = "postgresql://user:password@localhost/linch"
agent = Agent(
    model="your-model",
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
)
```

Cài extra Postgres trước khi khởi tạo các kho lưu trữ đó. Với một cụm worker,
cài `linch[worker]` và import runtime worker một cách tường minh:

```python
from linch.worker import Worker, enqueue_run, install

await install(pool)
run_id = await enqueue_run(pool, session_id, prompt, queue="default")
worker = Worker(pool, agent_factory, queues={"default": 8})
await worker.run()
```

`agent_factory` nhận một `WorkerJob`; nó phải tái sử dụng `job.run_store` và
`job.session_store`. `Worker` nhận lease của run, gia hạn lease trong lúc run
hoạt động, resume từ checkpoint sau khi tiếp quản và chỉ xác nhận job trong hàng đợi
sau khi run đã kết thúc. Nó kết nối lại sau các lỗi Postgres hoặc listener tạm thời
và giữ cho các job chưa kết thúc có thể được thử lại.

## Phần mở rộng

Lõi được giữ nhỏ gọn. Workflow, đánh giá, các cấu hình sẵn cho deep agent, báo cáo,
trình chạy vòng lặp định kỳ và console khởi tạo dự án nằm trong các module
`linch.ext.*` tường minh:

- `linch.ext.workflow` — workflow xác định có ghi nhật ký;
- `linch.ext.evals` — provider theo kịch bản, ca kiểm thử và bộ chấm điểm;
- `linch.ext.deep_agent` — cấu hình sẵn cho bộ điều phối/worker;
- `linch.ext.reports` — tiện ích hỗ trợ báo cáo run;
- `linch.ext.loop_runner` — các vòng lặp định kỳ do ứng dụng chủ điều khiển; và
- `linch.ext.scaffold` — điểm vào console `linch` để khởi tạo khung dự án.

Các module này có thể dùng lõi, nhưng import `linch` không import chúng.

Quay lại [mục lục kiến trúc](./README.md).
