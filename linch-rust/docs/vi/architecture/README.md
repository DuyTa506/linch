# Kiến trúc linch-rust

[Bản gốc tiếng Anh](../../architecture/README.md)

Tài liệu tham chiếu dành cho nhà phát triển linch-rust, bản port Rust của kernel agent Linch.
Tài liệu mô tả hợp đồng, luồng dữ liệu và các bất biến của từng crate, cùng cách
mở rộng chúng bằng Rust. Tài liệu dành cho người bảo trì linch-rust hoặc thêm
provider, tool và store vào đó. Ứng dụng dùng client Python, vốn có tài liệu
sử dụng riêng.

---

## Quy ước

- Các tham chiếu `file:line` tính từ thư mục `linch-rust/` (ví dụ
  `crates/linch-core/src/tool.rs:226`) và khớp với revision mà trang được viết.
  Hãy ưu tiên chúng thay vì tự suy ra lại hành vi.
- Mọi đoạn mã Rust đều biên dịch được với các crate trong workspace ở revision đó;
  những đoạn có `main` không cần mạng hay cơ sở dữ liệu cũng chạy được.
- Tài liệu kiến trúc của Linch bản Python nằm ở thư mục gốc của repository, trong
  [`docs/architecture/`](../../../../docs/architecture/README.md). Một trang chỉ liên kết
  tới đó khi khái niệm được dùng chung, và nêu rõ điểm khác biệt.

## Các trang

| Trang | Nội dung |
|---|---|
| [Providers](./providers.md) | Hợp đồng provider trong `linch-core`, tập event và quy tắc stream, lỗi và retry, các adapter trong `linch-providers` (DeepSeek, OpenAI Chat, OpenAI Responses, vLLM, SGLang, llama.cpp, Anthropic, Gemini), vòng lặp SSE duy nhất, bảng giá, cách thêm provider |
| [Tools](./tools.md) | Trait `Tool` và các scope, registry và việc đăng ký có thể hoàn tác, cách một batch chạy và phần nào chạy song song, các tool workspace và giới hạn truy cập, hệ thống tệp ảo và chuyển kết quả ra ngoài, các tool task của session, cách viết một tool |
| [MCP client](./mcp.md) | `linch-mcp`: transport stdio và HTTP, ánh xạ tool, relist, kết nối lại, hủy, kill cả process group, đóng có giới hạn thời gian, các giới hạn và che dữ liệu nhạy cảm |
| [Stores](./stores.md) | Hợp đồng của store run, session, task và file cùng các bộ kiểm tra tuân thủ; store trong bộ nhớ; store Postgres: schema, migration, một run đang chạy cho mỗi session, fencing, ghi bằng một câu lệnh, phân loại từ chối, kích thước pool; số lần ghi của group commit |
| [Worker](./worker.md) | `linch-worker`: hàng đợi job Postgres, vòng lặp worker, lease và fencing, back-off và dead letter, một job đang hoạt động cho mỗi run, lịch cron |
| [Extensions](./ext.md) | `linch-ext`: bộ chạy eval và scorer, báo cáo run, workflow có journal trên journal bộ nhớ và Postgres |
| [OpenTelemetry](./otel.md) | `linch-otel`: span và metric dựng từ luồng event, ánh xạ sang tên của Linch bản Python, ghi lại nội dung, những gì host cần nối vào |
| [Events](./events.md) | `Event` và các loại của nó, `Envelope`, các lớp lưu trữ bền vững, wire format và phiên bản của nó, việc bọc subagent, cách client Python giải mã và test parity, cách thêm một loại event |
| [Skill và subagent](./skills-subagents.md) | Tool `Skill`, danh sách và overlay của nó; tool `Subagent`, worker foreground và background, `SubagentContinue` và `TaskStop`; họ run; hợp đồng của run con; resume sau sự cố; reload; danh sách deep agent |
| [Giao thức RPC và server](./rpc.md) | `linch-rpc` và `linch-server`: method và notification, mã lỗi, bên trong một kết nối, thứ tự và backpressure, transport stdio, socket và HTTP, tenant và allowlist, các giới hạn, cách thêm một method |
| [Kernel](./kernel.md) | `Agent` là scope của từng agent, `AgentBuilder` và run contract digest, `Registration`, bảy chuỗi hook và cách mỗi chuỗi kết hợp các câu trả lời, đường plan và execute của một batch tool, không có biến toàn cục |
| [Vòng đời của lượt](./turn-lifecycle.md) | Một run: các bước của lượt, vì sao vòng lặp tiếp tục, barrier và group commit, batch tool và việc chạy song song chỉ đọc, suspend, resume và reconcile, các terminal |
| [Thu gọn ngữ cảnh](./compaction.md) | Điều kiện kích hoạt, lời gọi tóm tắt, phép chiếu có ghi log trên session log, khôi phục bản tóm tắt đã trả phí, những gì model nhìn thấy |

Các trang Python chỉ mô tả các khái niệm dùng chung. Mỗi trang Rust nêu rõ chỗ
Rust khác biệt. Điểm khác biệt chính của kernel nằm ở phần
[Các bất biến dùng chung](#các-bất-biến-dùng-chung) và trên trang [kernel](./kernel.md).

## Các crate

| Crate | Vai trò | Trang |
|---|---|---|
| `linch-core` | Lõi không phụ thuộc Python: kiểu dữ liệu, codec wire, hợp đồng provider và tool, registry, scheduler, runner, journal và các trait store | [Kernel](./kernel.md), [Vòng đời của lượt](./turn-lifecycle.md), [Thu gọn ngữ cảnh](./compaction.md), Providers, Tools, Stores, Events, Skill và subagent |
| `linch-providers` | Adapter HTTP cho model provider và bảng giá | [Providers](./providers.md) |
| `linch-tools` | Tool workspace, Bash, hệ thống tệp ảo, chuyển kết quả ra ngoài, các tool task của session | [Tools](./tools.md) |
| `linch-mcp` | MCP client: tool của server MCP trở thành tool trong registry | [MCP client](./mcp.md) |
| `linch-postgres` | Store run, session, task và file trên Postgres | [Stores](./stores.md) |
| `linch-worker` | Hàng đợi job Postgres, nhóm worker và lịch cron | [Worker](./worker.md) |
| `linch-ext` | Eval, báo cáo run, workflow | [Extensions](./ext.md) |
| `linch-otel` | Trace và metric OpenTelemetry dựng từ luồng event | [OpenTelemetry](./otel.md) |
| `linch` | Facade Rust: API công khai cùng một cấu hình serde để mở một agent | sau |
| `linch-rpc`, `linch-server` | Giao thức JSON-RPC và sidecar | [Giao thức RPC và server](./rpc.md) |
| `linch-python` | Phần mở rộng PyO3 nằm sau một số thành phần nội bộ của gói Python | sau |

## Các bất biến dùng chung

Các điều này đúng trên mọi trang ở trên, giống như
[các bất biến chính](../../../../docs/architecture/invariants.md) của Linch bản Python:

- **Chỉ async trên đường nóng.** Công việc tệp chặn chạy trên blocking pool của
  Tokio (`crates/linch-tools/src/fs/local.rs:228`,
  `crates/linch-tools/src/vfs/disk.rs:57`); một run chỉ tiến lên khi bên gọi
  poll stream của nó (`crates/linch-core/src/runner.rs:37-39`). Lõi chỉ spawn một
  loại task, trên Tokio runtime của bên gọi: task điều khiển các subagent chạy nền
  của một run đang suspend để chờ phê duyệt, cho đến khi một lần approve hoặc resume
  tiếp quản chúng (`crates/linch-core/src/runner/family.rs`).
- **Mọi đăng ký đều có thể hoàn tác.** Mỗi đăng ký registry và hook trả về một
  `Registration` mà `dispose` của nó hoàn tác đúng đăng ký đó và là
  idempotent ([tools](./tools.md#đăng-ký-có-thể-hoàn-tác)).
- **Chỉ công việc đọc mới chạy song song.** Các lời gọi write và exec chạy riêng
  ([tools](./tools.md#chỉ-công-việc-đọc-mới-chạy-song-song)).
- **Bền vững trước các tác dụng phụ.** Trạng thái run được commit tại các barrier: trước
  mỗi lần gửi tới provider, mỗi lần tool bắt đầu, mỗi lần ghi session và lúc kết thúc
  ([stores](./stores.md#số-lần-ghi-của-group-commit)).
- **Trạng thái bền vững là Postgres; store trong bộ nhớ dành cho test và phát triển.**
  Cả hai cùng đạt các bộ kiểm tra tuân thủ ([stores](./stores.md#các-bộ-kiểm-tra-tuân-thủ)).
- **Đa tenant theo namespace.** Bản ghi được khóa bằng một namespace do host chọn cộng với
  một id, nên các tenant không bao giờ dùng chung không gian khóa
  ([stores](./stores.md#khóa-và-đa-tenant)).
- **Khả năng quan sát là OpenTelemetry**, dựng từ luồng event với tên của Linch
  bản Python ([OpenTelemetry](./otel.md)).

Điểm khác với Python ([kernel](./kernel.md)): không có `Context` IoC, event bus hay
pipeline tool; các điểm mở rộng là trait (`Provider`, `Tool`, `RunStore`,
`SessionStore`, `TaskStore`, `FileBackend`, `JournalStore`) và các chuỗi hook
(`crates/linch-core/src/hooks.rs`) được đưa vào `AgentBuilder::hooks`
(`crates/linch-core/src/agent/builder.rs:85-86`).
