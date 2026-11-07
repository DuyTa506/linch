# Dùng linch-rust từ Python

[Bản gốc tiếng Anh](../../usage/README.md)

Dùng hướng dẫn này để xây dựng agent bằng Python với client `linch_rust`, và để
tìm trang cho từng tính năng.

linch-rust có cùng các khái niệm với Python Linch (agent, session, run, event,
tool, permission, run bền vững) nhưng kernel nhanh hơn, được viết bằng Rust.
Kernel chạy trong `linch-server`: đó là một sidecar mà client `linch_rust` tự khởi
động và điều khiển qua stdio, hoặc một server đang chạy phục vụ nhiều client mà
client kết nối tới qua Unix socket hoặc WebSocket ([Server](./server.md)). Bạn chỉ
viết Python; phần Rust được mô tả trong
[các trang kiến trúc](../architecture/README.md).

Tài liệu được đóng gói cùng package: `linch-rust docs path` in ra nơi chúng được
cài đặt, để bạn đọc hoặc tìm kiếm ngoại tuyến. `linch-rust skill install` đặt vào
dự án của bạn một skill trỏ Claude Code hoặc Codex tới các tài liệu này
([CLI](./cli.md#linch-rust-skill-install)); `linch-rust new` thêm skill này cho bạn.

---

## Cài đặt

```bash
pip install linch-rust              # the client and the linch-server binary
pip install 'linch-rust[otel]'      # with OpenTelemetry spans and metrics
pip install 'linch-rust[websocket]' # to reach a remote server over WebSocket
```

Cần Python 3.11 trở lên. Dependency runtime duy nhất còn lại là `orjson`. Nếu cài
từ checkout, xem [Bắt đầu nhanh](./quickstart.md#1-cài-đặt).

## Khái niệm cốt lõi

```text
Server ─────── one linch-server child process, or a connection to a running one; many agents and runs
  └── Agent ── opened from an AgentConfig (plain data) plus host tools and callbacks
        └── session id ── one conversation; at most one live run
              └── Run ── one prompt: an async stream of Envelopes
```

- **Agent là một config**: một typed dict nêu model, provider, kho lưu trữ,
  workspace và các công tắc. Hãy mở nó một lần cho mỗi server và dùng lại cho mọi
  session.
- **Host tool và callback** là các hàm Python bất đồng bộ; server gọi ngược vào
  process của bạn để chạy chúng.
- Một **run** stream các event có kiểu. Nó có thể tạm dừng để chờ phê duyệt và
  được resume sau đó, từ một process khác khi các kho lưu trữ là Postgres.

## Các trang

| Trang | Nội dung |
|---|---|
| [Bắt đầu nhanh](./quickstart.md) | Cài đặt, một run ngoại tuyến, provider trực tuyến, một host tool, một lần phê duyệt, Postgres |
| [Config của agent](./agent.md) | Mọi trường của `AgentConfig` cùng giá trị mặc định, `spec`, hợp đồng của run, intercept callback, session |
| [Provider](./providers.md) | Từng provider id, key lấy từ môi trường của server, profile cho server tự triển khai, provider scripted |
| [Tool](./tools.md) | Host tool, workspace tool, scope, permission mode và rule, phê duyệt, hệ thống tệp ảo, MCP tool |
| [Run](./runs.md) | Bắt đầu, stream, resume, phê duyệt, hủy; lỗi ở cuối stream; cái gì được lưu bền vững và khi nào |
| [Event](./events.md) | Mọi loại event mà client giải mã, cùng các trường của chúng; số thứ tự và replay; mức sử dụng và chi phí |
| [Kho lưu trữ](./stores.md) | Kho memory và Postgres, DSN, schema, migration, namespace, pool |
| [CLI](./cli.md) | `linch-rust new`, `linch-rust add tool`, `linch-rust docs path`, `linch-rust skill install` |
| [Khắc phục sự cố](./troubleshooting.md) | Các lỗi từ server binary đến run và tool, cùng nguyên nhân và cách khắc phục |
| [Skill](./skills.md) | Tool `Skill`, skill `verify` tích hợp sẵn, các skill trong `config_dir/skills` |
| [Subagent](./subagents.md) | Tool `Subagent`: worker foreground và background, continue, stop, fork, mailbox |
| [Task](./tasks.md) | Các tool task của session |
| [Deep agent](./deep-agent.md) | Preset `deep_agent` và các profile của nó |
| [Profile](./profiles.md) | Domain profile có phiên bản cho các run của một agent |
| [Observability](./observability.md) | Span và metric OpenTelemetry từ client |
| [Server](./server.md) | Chạy `linch-server` cho các tenant từ xa, và `Server.connect`: token, allowlist, giới hạn |

Sẽ có sau:

| Chủ đề | Nội dung |
|---|---|
| Thay đổi catalog và tool search | Đổi tool và skill của agent giữa chừng một session mà vẫn giữ prompt cache, và nạp trì hoãn schema tool cho catalog lớn |

## Khác biệt so với Python Linch

| Python Linch | linch_rust |
|---|---|
| `Agent(model=..., tools=..., ...)` trong process của bạn | dict `AgentConfig` được mở trên một sidecar `Server` |
| `session = await agent.session()`; `session.run(prompt)` | `agent.run(session_id, prompt)` với session id do bạn chọn |
| `ToolRegistry`, `@tool`, tool dạng class | host tool `@tool(scope=...)`; `scope` không có giá trị mặc định |
| `permissions={"mode": "acceptEdits"}`, `can_use_tool` | `{"mode": "accept_edits", "project_root": ...}`; tạm dừng rồi `agent.approve`, hoặc một callback `permission_request` |
| Hook chạy trong process | `@callback` ở bảy điểm hook, khai báo khi mở agent |
| `InMemorySessionStore`, kho Postgres | `{"type": "memory"}` hoặc `{"type": "postgres", "dsn": ...}` |
| `ScriptedProvider` từ `linch.ext.evals` | `{"id": "scripted", "turns": [...]}` với `linch_rust.scripted` |
| CLI `linch` | CLI `linch-rust` |

## Liên quan

- [Kiến trúc](../architecture/README.md): các crate Rust, dành cho người bảo trì.
