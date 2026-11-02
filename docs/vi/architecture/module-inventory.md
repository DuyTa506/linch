# Danh mục mô-đun

[Bản gốc tiếng Anh](../../architecture/module-inventory.md)

> Dùng trang này khi bạn cần tìm một mô-đun mã nguồn hoặc phần mở rộng và kiểm tra
> trách nhiệm của nó.

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Danh mục này bám theo cây thư mục `src/linch` hiện tại. Các tên trong package
cấp cao nhất chỉ là API công khai khi có trong `linch.__all__`. Các phần mở rộng
`linch.ext.*` tường minh và các mô-đun tích hợp tùy chọn được liệt kê trong
`docs/versioning.md` được hỗ trợ tại những đường dẫn đó; các đường dẫn mô-đun con
khác và tên bắt đầu bằng dấu gạch dưới là chi tiết triển khai nội bộ. Bộ nhớ bền vững
được lưu bằng tệp thông qua `context/file_memory.py`, còn phần đo lường OpenTelemetry
được giữ trong adapter nội bộ `_telemetry.py`.

## Kernel và vòng lặp run

| Đường dẫn | Trách nhiệm |
|---|---|
| `__init__.py` | API cấp cao nhất nạp trì hoãn. Phân giải các tên trong `linch.__all__` mà không nạp sớm các tích hợp provider, MCP, Postgres hoặc worker tùy chọn. |
| `agent.py` | Cấu hình `Agent`, `Context` cho từng agent, các registry, vòng đời session, chuẩn bị provider, quyền hạn, hook, thiết lập hệ thống tệp và `ReliabilityPolicy()` mặc định. |
| `session.py` | `Session`, `RunOptions`, vòng đời session, kiểm soát tiếp nhận run/resume, truy cập lịch sử chỉ nối thêm, các phụ thuộc cho từng run, chủ thể và công việc nền. |
| `session_log.py` | `SessionLog` chỉ nối thêm; các phép chiếu `provider_view` và `full_history`; các phép chiếu thu gọn ngữ cảnh đã ghi vào log và dấu vân tay lịch sử. |
| `catalog.py` | Các bản chụp tiền tố session nội bộ, các mã băm bất biến của danh mục cơ sở và danh mục đã thông báo, lời nhắc thay đổi bền vững, và đặt lại thông báo khi thu gọn ngữ cảnh/gặp sự cố. |
| `profiles.py` | Kết hợp `Profile` bất biến có phiên bản, các hàng plugin, hạn chế tool, schema đầu ra, budget và mã băm nội dung resume. |
| `lifecycle.py` | Các bộ mô tả điểm vòng đời có kiểu và chế độ điều phối mà danh mục event và bus kernel sử dụng. |
| `kernel/` | `Context` không có phụ thuộc, các hiệu ứng `Disposable` có thể hoàn tác, `EffectScope` và `EventBus`. Xem [kernel.md](./kernel.md). |
| `loop/runner.py` | Các điểm vào `run_loop()` và `resume_loop()`, cùng việc phối hợp lượt và dọn dẹp khi kết thúc. |
| `loop/runtime.py` | Trạng thái checkpoint, sổ ghi nhận, vòng đời, hook, telemetry và mất lease trong phạm vi run mà runner sử dụng. |
| `loop/request.py` | Tạo thông điệp người dùng và `ProviderRequest`, hợp nhất đầu ra bộ dựng ngữ cảnh và hạ cấp khả năng của provider. |
| `loop/provider_turn.py` | Chuẩn bị lượt provider, chuyển giao streaming, lưu bền vững thông điệp assistant đã chấp nhận và điều phối hook phản hồi. |
| `loop/streaming.py` | Phân tích luồng provider đã chuẩn hóa và khôi phục khi vượt độ dài ngữ cảnh/chuyển sang mô hình dự phòng. |
| `loop/tool_batch.py` | Checkpoint cho lô tool, phép chiếu event, nối thêm thông điệp kết quả, khôi phục và đánh giá bộ bảo vệ vòng lặp. |
| `loop/checkpoint.py` | Tuần tự hóa checkpoint/event theo định dạng truyền hiện tại, đệm event, con trỏ khôi phục lời gọi mô hình và trạng thái phần mở rộng. |
| `loop/terminals.py` | Phần đuôi kết quả/event kết thúc, xác thực đầu ra có cấu trúc và các cổng kiểm soát hoàn tất. |
| `loop/finalize.py` | Hoàn tất văn bản cuối/tool cuối và điều phối `TurnStopping` trước khi xác nhận tool kết thúc. |
| `loop/turn_stopping.py` | `TurnStopping`, `TurnStoppingDecision`, `TurnStoppingDispatcher` và điểm nối tuần tự để phủ quyết/tiếp tục/báo lỗi. |
| `loop/feedback.py` | Thông điệp phản hồi bền vững cho đầu ra provider bị từ chối, kiểm tra câu trả lời cuối và tiếp tục lượt. |
| `loop/reliability.py` | Trạng thái độ tin cậy trong phạm vi run và triển khai hook điều kiện tiên quyết. |
| `loop/contracts.py` | Tạo hợp đồng run an toàn khi resume, bao gồm cấu hình chủ thể và độ tin cậy. |
| `loop/resume.py` | Khôi phục checkpoint và phát lại công việc provider/tool đang chờ. |
| `loop/startup.py` | Giai đoạn khởi động bền vững và xử lý nối thêm prompt người dùng. |
| `loop/dispatch.py` | Các adapter điều phối vòng đời hook. |
| `loop/__init__.py` | Tái xuất các thành phần vòng lặp công khai: `run_loop`, `resume_loop`, hàm hỗ trợ yêu cầu và áp dụng khả năng provider. |

## Các mô-đun hỗ trợ cốt lõi

| Đường dẫn | Trách nhiệm |
|---|---|
| `_blocking.py` | Chuyển I/O đồng bộ của host sang các luồng daemon có giới hạn. |
| `_client_lifecycle.py` | Các hàm hỗ trợ đóng/quyền sở hữu client bất đồng bộ mà adapter provider sử dụng. |
| `_http_errors.py` | Chuẩn hóa lỗi HTTP dùng chung cho các client provider. |
| `_json.py` | Các hàm hỗ trợ duyệt và chuẩn hóa nghiêm ngặt để bảo đảm an toàn với JSON. |
| `_prompt_cache.py` | Các hàm hỗ trợ gợi ý bộ nhớ đệm prompt và định danh tiền tố. |
| `_telemetry.py` | Đo lường OpenTelemetry nội bộ tại mã nguồn cho run, lượt, lời gọi provider, tool, thu gọn ngữ cảnh và các chỉ số kho lưu trữ. |
| `_token_estimation.py` | Các chính sách ước lượng token độc lập với provider. |
| `_tracing.py` | Các hàm hỗ trợ lan truyền ngữ cảnh trace và `trace_id`. |
| `_version.py` | Siêu dữ liệu phiên bản package. |
| `_wire.py` | Buộc tuân thủ thế hệ định dạng truyền đang dùng cho dữ liệu lưu bền vững. |
| `abort.py` | Tín hiệu hủy phối hợp và các lỗi hủy. |
| `config.py` | `FeatureFlags` và cấu hình prompt hệ thống. |
| `durability.py` | Tùy chọn hộp thư đến bền vững và phong bì chuyển giao thông báo. |
| `errors.py` | Phân cấp lỗi Linch công khai và các lỗi run/provider/tool có kiểu. |
| `events.py` | Các event luồng có kiểu và tuần tự hóa nghiêm ngặt theo định dạng truyền hiện tại. |
| `middleware.py` | Ngữ cảnh middleware của tool và các bản ghi đầu vào/kết quả. |
| `recovery.py` | Chính sách tùy chọn để khôi phục khi đầu ra bị cắt ngắn. |
| `reliability.py` | Chính sách độ tin cậy phản hồi công khai, điều kiện tiên quyết và các cổng kiểm soát hoàn tất. |
| `scheduler.py` | Phân giải tool chuẩn tắc, thứ tự cấp quyền, các lô có xét tài nguyên, thử lại, thời gian chờ và chuyển kết quả ra ngoài. |
| `types.py` | Các thông điệp, khối, yêu cầu provider, mức sử dụng, schema và lý do dừng dùng chung. |
| `verification.py` | Các protocol kiểm chứng câu trả lời cuối và đánh giá kết luận. |

## Provider và hạch toán mô hình

| Đường dẫn | Trách nhiệm |
|---|---|
| `providers/base.py` | `BaseProvider`, `ProviderCapabilities`, cấu hình suy nghĩ và protocol provider đã chuẩn hóa. |
| `providers/openai_chat.py` | Ngữ nghĩa OpenAI Chat Completions cùng transport, tuần tự hóa và cơ chế streaming chat dùng chung nội bộ; từng adapter chịu trách nhiệm về khả năng, ngữ cảnh, suy luận và usage. |
| `providers/_context_probe.py` | Thăm dò `/v1/models` nội bộ dùng chung khi chuẩn bị vLLM/SGLang; đọc `max_model_len` dương theo đúng ID mô hình. |
| `providers/openai_responses.py` | OpenAI Responses API, bao gồm id phản hồi có trạng thái và các tùy chọn suy luận gốc. |
| `providers/anthropic.py` | Streaming, bộ nhớ đệm prompt và các tùy chọn suy nghĩ của Anthropic. |
| `providers/gemini.py` | Adapter Gemini/Vertex AI dùng SDK `google-genai` tùy chọn; các đối tượng SDK nằm trong adapter. |
| `providers/deepseek.py` | Các tùy chọn gốc của DeepSeek và xử lý nội dung suy luận. |
| `providers/llamacpp.py` | Adapter máy chủ llama.cpp tương thích OpenAI và chuẩn bị cửa sổ ngữ cảnh. |
| `providers/vllm.py` | Adapter máy chủ vLLM tương thích OpenAI. |
| `providers/sglang.py` | Adapter máy chủ SGLang tương thích OpenAI. |
| `providers/catalog.py` | Siêu dữ liệu mô hình của provider và các hàm hỗ trợ liệt kê mô hình. |
| `providers/limiter.py` | Cổng giới hạn đồng thời tùy chọn cho provider. |
| `providers/retry.py` | Các tùy chọn thử lại provider/tool và lớp bao thử lại. |
| `model_calls.py` | Ý định gọi mô hình bền vững, biên nhận, các phép chiếu và hạch toán mức sử dụng tích lũy. |
| `openai_responses.py` | `OpenAIOptions`, `OpenAIReasoning`, chuyển đổi định dạng truyền Responses và các hàm hỗ trợ streaming cấp thấp mà adapter provider sử dụng. |
| `compaction.py` | Các chiến lược thu gọn ngữ cảnh, hạch toán thu gọn và tóm tắt do provider thực hiện. |
| `budget.py` | Budget token/chi phí dùng chung cho từng run và subagent. |
| `pricing.py` | Giá mô hình đã biết và tính toán chi phí. |

## Trạng thái bền vững và mở rộng theo chiều ngang

| Đường dẫn | Trách nhiệm |
|---|---|
| `run_store.py` | `RunStore`, `RunLeaseStore` tùy chọn, các protocol cho lô event và bản chụp đầu vào mô hình; `RunCheckpoint`, `RunRecord`, `RunLease`, hợp đồng run, bộ mã hóa/giải mã định dạng truyền và `InMemoryRunStore`. |
| `run_stores/__init__.py` | Không gian tên tường minh cho kho run bền vững và ranh giới xuất Postgres. |
| `run_stores/postgres.py` | `PostgresRunStore` với ghi có hàng rào bảo vệ, epoch của lease, checkpoint, bản chụp đầu vào mô hình và các hàng event `(run_id, seq)`. |
| `sessions/store.py` | `SessionStore`, `ProviderViewSnapshotStore`, `SessionInboxStore` và các kiểu bản ghi/thông điệp session. |
| `sessions/memory.py` | `InMemorySessionStore` cho kiểm thử và phát triển trong phạm vi tiến trình. |
| `sessions/postgres.py` | `PostgresSessionStore` cho thông điệp session bền vững, các hàng tác vụ, bản chụp góc nhìn provider và chuyển giao hộp thư đến. Nhập từ `linch.sessions.postgres`. |
| `storage/` | Vòng đời pool/schema asyncpg nội bộ dùng chung trong `storage/_pg.py`; package này không thuộc API công khai. |
| `worker/__init__.py` | Không gian tên worker tùy chọn tường minh; `import linch` ở lõi không nạp PgQueuer. |
| `worker/dispatch.py` | Tạo run đang chờ và đưa công việc vào hàng đợi PgQueuer theo cách nguyên tử. |
| `worker/runtime.py` | Bộ thực thi đội `Worker`, heartbeat lease của run, tiếp quản, chờ xử lý hết công việc và xử lý thử lại. |
| `worker/schedules.py` | Định nghĩa lịch cron/khoảng thời gian theo UTC và các công việc lịch được trì hoãn. |
| `worker/tools.py` | Các tool lập lịch gắn với session do host gắn vào. |

Worker dùng `PostgresRunStore.claim()`/`renew()`/`release()` với một epoch
hàng rào bảo vệ tăng đơn điệu. Worker tạo lại các trình quản lý PgQueuer sau các lỗi
cơ sở dữ liệu/bộ lắng nghe tạm thời, thử lại các lỗi hạ tầng bằng
`DatabaseRetryEntrypointExecutor`, và để PgQueuer thu hồi các công việc hàng đợi
đã cũ. Worker chỉ xác nhận công việc sau khi trạng thái kết thúc của run
đã được lưu bền vững.

## Tool, chính sách và ngữ cảnh

| Đường dẫn | Trách nhiệm |
|---|---|
| `tools/base.py` | Protocol tool theo duck typing, `ToolContext`, `ToolResult`, hợp đồng đầu ra, phạm vi, truy cập tài nguyên, trích dẫn và khóa lũy đẳng ổn định. |
| `tools/function.py` | `FunctionTool` và adapter `tool()`; các hàm đồng bộ chạy trong các worker xử lý tác vụ chặn có giới hạn. |
| `tools/execution.py` | Các backend shell cục bộ và Docker (`LocalBackend`, `DockerBackend`) cùng `ExecResult`. |
| `tools/_limits.py` | Giới hạn đầu ra dùng chung cho shell, Grep và MCP. |
| `tools/_output.py` | Thu đầu/cuối có giới hạn, spill tối đa 64 MiB và cursor UTF-8. |
| `tools/processes.py` | Registry tiến trình theo run, thời gian chạy tối đa, dọn dẹp và `BashProcess`. |
| `tools/file_tracker.py` | Bằng chứng đọc toàn bộ tệp và phạm vi dòng của `FileReadTracker` mà tool và hook sử dụng. |
| `tools/registry.py` | `ToolRegistry`, đăng ký có thể hoàn tác, `empty_tools()` và `workspace_tools()`. |
| `tools/search.py` | Trì hoãn danh mục yêu cầu của `ToolSearchPlugin`, tìm kiếm theo từ khóa/lựa chọn, lưu bền vững tool đã nạp và bộ bảo vệ lời gọi trì hoãn. |
| `tools/pipeline.py` | Pipeline mở `tools/pre-execute` → `execute` → `post-execute`. |
| `tools/builtin.py` | Các tool đọc/ghi/shell trong workspace. |
| `tools/tasks.py` | Các tool tác vụ session. |
| `tools/ask_user.py` | Tool hỏi người dùng do host cung cấp. |
| `tools/skill.py` | Tool gọi skill. |
| `tools/subagent.py` | Khởi tạo subagent và xác định điều kiện chạy song song có xét đầu vào. |
| `tools/subagent_continue.py` | Tiếp tục một session con đã giữ lại theo id worker hoặc tên hiển thị. |
| `tools/subagent_stop.py` | Dừng một worker nền và giữ lại handle để tiếp tục sau này. |
| `tools/_worker_utils.py` | Phân giải id/tên hiển thị worker dùng chung. |
| `tools/isolation.py` | Backend cô lập cho từng subagent và hỗ trợ thư mục tạm. |
| `tools/wrappers/` | Các lớp bao thực thi tùy chọn cho thời gian chờ và chỉ số. |
| `permissions/engine.py` | Quyết định quyền dựa trên đầu vào chuẩn tắc và các callback phê duyệt. |
| `permissions/rules.py` | `ToolRule`, `PathRule` và `BashRule`, bao gồm các điều kiện vai trò. |
| `permissions/ruleset.py` | Các bộ quy tắc quyền phân lớp. |
| `permissions/principal.py` | Danh tính `Principal(tenant, user, roles)` bất biến được một run và các lời gọi tool của run đó sử dụng. |
| `context/builder.py` | Protocol bộ dựng ngữ cảnh bất đồng bộ, budget ngữ cảnh, chuỗi bộ dựng và ngữ cảnh yêu cầu tạm thời. |
| `context/file_memory.py` | `FileMemoryContextBuilder`, lưu đệm `/memories/MEMORY.md` cho một run và giới hạn chỉ mục bộ nhớ ở 200 dòng nội dung đầu tiên. |
| `rag/__init__.py` | Protocol truy xuất trong phạm vi yêu cầu (`RagConnector`, `RetrievedChunk`), chèn ngữ cảnh có nguồn, trạng thái truy xuất được lưu checkpoint và đối chiếu trích dẫn với nguồn tùy chọn tại `TurnStopping`. |
| `filesystem/backend.py` | `FileBackend` ảo, chuẩn hóa đường dẫn, backend có phạm vi, trạng thái trong bộ nhớ và định tuyến kết hợp. |
| `filesystem/disk.py` | Các tệp ảo lưu trên đĩa. |
| `filesystem/postgres.py` | `PostgresFileBackend` cho các tệp ảo bền vững; nhập từ `linch.filesystem.postgres`. |
| `filesystem/offload.py` | Chuyển kết quả lớn sang hệ thống tệp ảo dựa trên ngưỡng. |
| `filesystem/tools.py` | Các tool hệ thống tệp ảo `ls`, `read_file`, `write_file` và `edit_file`. |
| `hooks/` | Ngữ cảnh hook có kiểu, bộ điều phối vòng đời, chèn ngữ cảnh, middleware tool, che dữ liệu, đọc trước khi ghi, bộ nhớ đệm, kiểm chứng và hook dừng. |
| `skills/` | Nạp `SKILL.md`, liệt kê, thay thế, các lớp phủ, skill tích hợp sẵn và lời nhắc hệ thống. |
| `subagents/` | Định nghĩa agent có tên, tạo agent con, handle worker trong tiến trình và các runner subagent. |
| `coordination/__init__.py` | Các thành phần phối hợp trong tiến trình được xuất; `MailboxPlugin` được định nghĩa trong `coordination/mailbox/__init__.py`. |
| `coordination/mailbox/__init__.py` | Xuất protocol hộp thư và triển khai trong bộ nhớ. |
| `coordination/mailbox/core.py` | Thông điệp hộp thư, quyền nhận xử lý, lease và chuyển giao hộp thư trong bộ nhớ. |
| `coordination/mailbox/correlation.py` | Các bước bắt tay của bộ tương quan cho thông điệp yêu cầu/phản hồi giữa các bên ngang hàng. |
| `coordination/_claim_validation.py` | Xác thực token nhận quyền xử lý dùng chung. |
| `coordination/send_message.py` | Triển khai tool gửi thông điệp ngang hàng. Lập lịch bền vững và điều phối PgQueuer nằm trong `worker/`. |
| `mcp/` | Cấu hình máy chủ MCP tùy chọn, kết nối client, đặt tên, cầu nối quyền và các adapter tool. |

Các đường dẫn cấp package bên dưới là những mô-đun protocol và adapter nhỏ hơn
hỗ trợ các hàng trên:

| Đường dẫn | Trách nhiệm |
|---|---|
| `context/__init__.py` | Xuất các thành phần bộ dựng ngữ cảnh và bộ nhớ tệp. |
| `execution/__init__.py` | Xuất khả năng thực thi thống nhất. |
| `execution/backend.py` | `ExecutionBackend`, `ShellBackend`, `ShellStarter`/`ShellProcess` tùy chọn và metadata bảo mật. |
| `execution/local.py` | Backend thực thi shell/hệ thống tệp cục bộ trên host. |
| `execution/remote.py` | Backend thực thi từ xa với định danh chính sách hỗ trợ resume. |
| `filesystem/__init__.py` | Xuất protocol, backend và tool hệ thống tệp. |
| `hooks/__init__.py` | Xuất bộ điều phối hook, ngữ cảnh, adapter và kiểu hook. |
| `hooks/adapters.py` | Các adapter tích hợp sẵn cho các ranh giới ngữ cảnh, middleware, kiểm chứng và dừng. |
| `hooks/contexts.py` | Dữ liệu ngữ cảnh hook có kiểu. |
| `hooks/dispatcher.py` | Điều phối hook có thứ tự và cô lập lỗi. |
| `hooks/read_before_write.py` | Bộ bảo vệ đọc trước khi ghi tùy chọn. |
| `hooks/redaction.py` | Hook che dữ liệu tool/event. |
| `hooks/tool_cache.py` | Hook tùy chọn lưu đệm kết quả tool đọc. |
| `hooks/types.py` | Các protocol hook và `HookResult`. |
| `kernel/__init__.py` | Xuất các thành phần kernel công khai. |
| `kernel/context.py` | Registry dịch vụ có phạm vi và ngữ cảnh kích hoạt plugin. |
| `kernel/effects.py` | `Disposable` có thể hoàn tác và các phạm vi hiệu ứng. |
| `kernel/errors.py` | Lỗi cấu hình và vòng đời kernel. |
| `kernel/events.py` | Bus event có kiểu và các chế độ điều phối vòng đời. |
| `kernel/plugins.py` | Protocol plugin, kích hoạt phụ thuộc, kiểm tra phạm vi và giải phóng phần gắn vào. |
| `loop_guard/__init__.py` | Xuất protocol và quyết định của bộ bảo vệ vòng lặp. |
| `loop_guard/guard.py` | Phát hiện lời gọi lặp lại và chuỗi lỗi liên tiếp. Xử lý giới hạn số lượt nằm trong nhánh kết thúc vòng lặp. |
| `mcp/__init__.py` | Xuất các adapter MCP tùy chọn. |
| `mcp/client.py` | Session client MCP và vòng đời truyền tải. |
| `mcp/config.py` | Cấu hình máy chủ MCP. |
| `mcp/naming.py` | Đặt tên ổn định cho tool MCP. |
| `mcp/permission_bridge.py` | Ánh xạ siêu dữ liệu MCP sang quyền Linch. |
| `mcp/result.py` | Chuẩn hóa kết quả MCP. |
| `mcp/tool.py` | Adapter tool MCP. |
| `permissions/__init__.py` | Xuất bộ máy quyền, quy tắc, bộ quy tắc và chủ thể. |
| `permissions/keys.py` | Tạo khóa quyền chuẩn tắc và bộ mã hóa/giải mã quyết định theo định dạng truyền hiện tại. |
| `sessions/__init__.py` | Xuất kho session và các hàm hỗ trợ session. |
| `sessions/fork.py` | Các nhánh session di chuyển được đã qua xác thực. |
| `sessions/tasks.py` | Các hàng tác vụ session và cấp phát id tác vụ. |
| `skills/__init__.py` | Xuất các thành phần nạp, liệt kê và kiểu skill. |
| `subagents/__init__.py` | Xuất agent con và worker. |
| `testing/__init__.py` | Xuất các kiểm thử hợp đồng tái sử dụng được. |
| `testing/contracts.py` | Các hàm hỗ trợ hợp đồng provider, tool, hộp thư, hệ thống tệp và cô lập. |
| `tools/__init__.py` | Xuất protocol tool, registry, tool tích hợp sẵn và plugin. |

## Bộ bảo vệ vòng lặp và hỗ trợ hợp đồng

| Đường dẫn | Trách nhiệm |
|---|---|
| `loop_guard/` | Phát hiện lời gọi lặp lại/lỗi có giới hạn và trạng thái bộ bảo vệ có thể lưu checkpoint. |
| `execution/` | Điểm nối khả năng thực thi shell/hệ thống tệp thống nhất và các triển khai cục bộ/từ xa. |
| `testing/` | Các phép kiểm tra hợp đồng tái sử dụng được cho provider, tool, hộp thư, hệ thống tệp và cô lập. |

## Các phần mở rộng tường minh

| Đường dẫn | Trách nhiệm |
|---|---|
| `ext/deep_agent/` | Profile, prompt, hàm tạo deep-agent và định nghĩa subagent chuyên trách. |
| `ext/evals/` | Provider theo kịch bản, các ca/kết quả đánh giá, bộ khung benchmark và các bộ chấm điểm. |
| `ext/workflow/` | Ngữ cảnh, nhật ký, bộ máy workflow xác định và cơ chế tạm ngưng/lỗi. |
| `ext/reports.py` | Tạo báo cáo run và nạp báo cáo đã lưu bền vững. |
| `ext/loop_runner.py` | Runner vòng lặp định kỳ do host điều khiển và các sản phẩm đầu ra trên hệ thống tệp. |
| `ext/scaffold/` | Triển khai dòng lệnh tạo khung dự án chỉ dùng thư viện chuẩn. |
| `ext/__init__.py` | Mô tả không gian tên phần mở rộng tường minh mà không xuất tên phần mở rộng vào `linch.__all__`. |

Bộ nhớ tệp dùng `context/file_memory.py` và `FileBackend`; điều phối hàng đợi và
lịch dùng `linch.worker`. CLI tạo khung dự án được cung cấp qua
`ext/scaffold/__init__.py`, với `ext/scaffold/__main__.py` làm lớp chuyển tiếp cho `python -m`,
và được kiểm thử trong `tests/ext/scaffold/test_scaffold_cli.py`.

Quay lại [mục lục kiến trúc](./README.md).
