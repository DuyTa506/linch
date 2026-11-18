# Khắc phục sự cố

[Bản gốc tiếng Anh](../../usage/troubleshooting.md)

Dùng trang này để đối chiếu một lỗi từ `linch_rust` với nguyên nhân và cách khắc phục.

[<- Hướng dẫn sử dụng](./README.md)

Các thông báo được trích đúng như client hiển thị. Phần trong `<...>` thay đổi; dòng
ghi "fragment" chỉ trích phần cố định của một thông báo dài hơn. Mọi lỗi từ server
đều là `RpcError` với `code`, `message` và `data`; lớp exception cho biết nhóm của lỗi:

| Lớp | Mã | Do đâu |
|---|---|---|
| `ProtocolError` | -32700 đến -32600 | Một cấu hình hoặc khai báo mà server không đọc được |
| `OpenError` | -32010 đến -32015 | `open_agent`: cấu hình, provider, kho lưu trữ hoặc một MCP server bị lỗi |
| `RunError` và các lớp con | -32020 đến -32035 | Cuối stream của một run, hoặc `agent.events` |
| `AgentNotFound`, `RunBusy` | -32001, -32002 | Chính lệnh gọi |
| `ConnectionClosed` | không có | Mọi lệnh gọi hoặc stream, khi server đã biến mất |
| `LinchError` | không có | Lớp cơ sở của tất cả các lớp trên, và việc dùng sai `Server` |

---

<a id="the-server-binary"></a>

## Binary của server

`Server()` tìm `linch-server` theo thứ tự này:

1. đường dẫn trong `LINCH_SERVER_BIN`, để bản build của riêng bạn thay thế bản đi kèm;
2. binary được cài cùng wheel `linch-rust`;
3. trong một bản checkout, bản mới hơn trong `target/debug/linch-server` và
   `target/release/linch-server` dưới `CARGO_TARGET_DIR` hoặc `target/` của cargo
   workspace.

`Server(binary=...)` bỏ qua bước tìm kiếm.

| Thông báo | Nguyên nhân | Cách khắc phục |
|---|---|---|
| ``no linch-server binary: install the linch-rust wheel, set LINCH_SERVER_BIN, or run `cargo build -p linch-server` in <workspace> (looked in <target dir>)`` (`FileNotFoundError`) | Chưa cài wheel, biến chưa được đặt và không có bản build cargo | Cài wheel, hoặc build server trong bản checkout, hoặc đặt `LINCH_SERVER_BIN` |
| `[Errno 2] No such file or directory: <path>` (`FileNotFoundError`) | `LINCH_SERVER_BIN` hoặc `binary=` trỏ tới một tệp không tồn tại | Trỏ nó tới binary đã build |
| Một trường được ghi trong tài liệu này bị từ chối vì không xác định (`ProtocolError`, -32602) | Binary của server cũ hơn client: một wheel cũ, hoặc một bản build cargo lỗi thời mà bước tìm kiếm vẫn ưu tiên | Cài lại wheel, hoặc build lại bằng `cargo build -p linch-server` |

Server ghi log của chính nó ra stderr: cảnh báo và lỗi theo mặc định, nhiều hơn với
`LINCH_LOG=info` (hoặc `debug`). Một job được lập lịch thất bại, một lần retry, một lỗi dọn
dẹp và một lỗi journal workflow chỉ hiện ở đó và không ở đâu khác ([Server](./server.md#log)).
Một server do `Server()` khởi động ghi nó ra stderr của process của bạn.

## Đối tượng `Server`

| Thông báo | Nguyên nhân | Cách khắc phục |
|---|---|---|
| ``the server is not started; use `async with Server()` `` (`LinchError`) | Gọi trên một `Server` chưa bao giờ được khởi động hoặc đã đóng | Dùng `async with Server() as server:` và gọi bên trong khối đó |
| `the server is already started` (`LinchError`) | `start()` được gọi hai lần | Khởi động mỗi `Server` một lần |
| `the server closed its output` (`ConnectionClosed`) | Tiến trình server đã thoát: bị kill, bị crash, hoặc khối `Server` kết thúc khi một run đang stream | Khởi động `Server` mới. Với kho Postgres, resume run ([Run](./runs.md#resume)); run trong bộ nhớ đã mất |
| fragment: `reading the server failed:` (`ConnectionClosed`) | Không đọc hoặc phân tích được một dòng của server | Báo cáo kèm stderr của server |

### Server từ xa (`Server.connect`)

| Thông báo | Nguyên nhân | Cách khắc phục |
|---|---|---|
| `a server address is unix:PATH, ws://HOST:PORT/rpc or wss://...` (`ValueError`) | Dùng scheme khác, như `http://`, hoặc một đường dẫn trần | Sao chép địa chỉ mà server in ra khi khởi động |
| `[Errno 2] No such file or directory` hoặc `[Errno 111] Connect call failed` (`OSError`) | Không có gì lắng nghe ở đó | Khởi động `linch-server --config`, hoặc sửa đường dẫn hay cổng |
| `authentication required: the first message must be connection.auth with a valid token (code -32003)` trên socket, `the server refused the token (HTTP 401) (code -32003)` trên WebSocket (`Unauthenticated`) | Token không có trong cấu hình của server | Dùng token có trong cấu hình; server đọc các biến của nó khi khởi động |
| `at most <n> connections at once; try again once one ends` (`LimitReached`, `kind == "connections"`) | Tenant đang giữ `max_connections` kết nối, trong đó có thể có một client đã biến mất mà không đóng, mà server kết thúc sau khoảng một phút ([liveness](./server.md#liveness)) | Đóng một kết nối, hoặc tăng `max_connections` của tenant; thử lại sau một phút nếu một client đã chết |
| `at most <n> logins at once; try again once one ends` (`LimitReached`, `kind == "logins"`) | 32 kết nối socket cùng chờ đăng nhập | Thử lại với backoff; server đang bận, không phải đã biến mất |
| `at most <n> agents at once; try again once one ends` (`LimitReached`, `kind == "agents"`) từ `open_agent` | Tenant đang giữ mở `max_agents` agent, trên tất cả các kết nối của nó | Đóng các agent không còn dùng, hoặc tăng `max_agents` của tenant |
| `stores.max_connections <n> is above the tenant's max_pool_connections <max>` (`NotAllowed`, `kind == "pool_connections"`) từ `open_agent` | Cấu hình yêu cầu pool Postgres lớn hơn mức tenant được phép có | Yêu cầu tối đa `<max>`, bỏ `max_connections` đi, hoặc tăng `max_pool_connections` của tenant (xem [Kho lưu trữ](./stores.md#kết-nối-và-pool) trước khi vượt quá 32) |
| `Server.connect over WebSocket needs the websocket extra` (`ImportError`) | Chưa cài `websockets` | `pip install 'linch-rust[websocket]'`, hoặc dùng socket |
| `a server reached with Server.connect has no process here` (`LinchError`) | Dùng `server.pid` trên một server từ xa | Chỉ server do chính mình khởi chạy mới có tiến trình ở đây |

Việc từ chối `open_agent` và `run` trên server từ xa là `NotAllowed` (-32004), có
`kind` nêu tên bước kiểm tra, và `LimitReached` (-32005) với `max_agents` và
`max_runs`; [Server](./server.md#những-gì-admission-từ-chối-và-lý-do) liệt kê chúng.

<a id="opening-an-agent"></a>

## Mở một agent

`open_agent` kiểm tra toàn bộ cấu hình trước khi bất cứ thứ gì chạy.

| Thông báo | Mã | Nguyên nhân | Cách khắc phục |
|---|---|---|---|
| ``unknown field `<name>`, expected one of `namespace`, `spec`, ...`` | -32602 | Một trường sai chính tả hoặc không được hỗ trợ, ở bất kỳ cấp nào (cả bên trong `spec`, `provider`, `permissions`, ...) | Dùng trường có trong [Cấu hình agent](./agent.md); khóa API không bao giờ nằm trong cấu hình |
| ``missing field `project_root` `` | -32602 | Có `permissions` mà thiếu `project_root` | Thêm `project_root` là đường dẫn tuyệt đối, hoặc bỏ `permissions` để dùng chính sách mặc định |
| ``unknown variant `skip-dangerous`, expected one of `default`, `accept_edits`, `skip_dangerous` `` | -32602 | Cách viết chế độ của Python Linch | Dùng `accept_edits` hoặc `skip_dangerous` |
| `permission policy rejected: project_root must be an absolute path, got "<root>"` | -32011 | `project_root` là đường dẫn tương đối | Cho một đường dẫn tuyệt đối |
| ``unknown variant `<id>`, expected one of `deepseek`, `openai-chat`, ...`` | -32602 | `id` của provider không xác định | Chọn một id trong [Provider](./providers.md) |
| ``missing field `base_url` `` | -32602 | `vllm`, `sglang` hoặc `llamacpp` thiếu `base_url` | Thêm URL của server, bao gồm `/v1` |
| `provider <id> needs its key in the environment variable <VAR>` | -32013 | Khóa chưa được đặt hoặc để trống trong môi trường của server; `data["env"]` nêu tên biến | Export nó trước khi `Server()` khởi động, hoặc truyền qua `Server(env=...)` |
| `workspace unusable: <path>: no such file` | -32011 | `workspace` không tồn tại hoặc không phải thư mục (đường dẫn tương đối được phân giải theo thư mục làm việc của server) | Tạo thư mục hoặc cho một đường dẫn tuyệt đối |
| `tools: tool "<name>" is already registered` | -32602 | Hai tool của ứng dụng chủ trùng tên | Đổi tên một tool bằng `@tool(name=...)` |
| `tools: tool name "<name>" must be 1-64 ASCII letters, digits, '_' or '-'` | -32602 | Tên tool không hợp lệ | Đổi tên |
| `tool registration failed: tool "<name>" is already registered` | -32011 | Một tool của ứng dụng chủ trùng tên với tool tích hợp sẵn mà cấu hình bật (`Read`, `ls`, `TaskCreate`, ...) | Đổi tên tool của ứng dụng chủ |
| `invalid agent: the model must not be blank` | -32011 | `spec.model` để trống | Đặt một model |
| `invalid agent: the namespace must not be blank` | -32011 | `namespace` để trống | Đặt một namespace |
| `invalid agent: workers need subagents` | -32011 | Có `workers` mà không có `subagents: True` | Bật `subagents` |
| `profile unusable: profile id must not be blank` | -32011 | `profile.id` để trống (hoặc `version`, hoặc `max_turns` nhỏ hơn 1) | Sửa [profile](./profiles.md) |
| `deep agent unusable: a durable deep agent needs durable stores; use the Postgres stores, or set durable to false for ephemeral use` | -32011 | Một profile deep agent tích hợp sẵn chạy trên kho lưu trữ trong bộ nhớ | Dùng kho Postgres, hoặc `"deep_agent": {"durable": False}` |
| `deep agent unusable: unknown deep-agent profile "<name>"; choose one of: balanced, coordinator, unbounded` | -32011 | Tên profile sai chính tả | Dùng tên tích hợp sẵn hoặc một dict profile đầy đủ |
| `MCP server unusable: could not connect to MCP server "<name>": could not start the MCP server process: No such file or directory (os error 2)` | -32015 | `command` stdio không có trong `PATH` của server; `data["server"]` nêu tên mục cấu hình | Dùng đường dẫn tuyệt đối, hoặc đưa `PATH` vào `Server(env=...)` |

Khóa sai không làm việc mở thất bại: không có yêu cầu nào được gửi cho đến khi một
run gọi mô hình. Lỗi này hiện ra thành một event `error` trong run (`status` HTTP 401
hoặc 403).

## Postgres

Tất cả các lỗi này là `OpenError` với mã -32014, từ agent đầu tiên mở trên một DSN.
Thông báo không bao giờ chứa DSN.

| Thông báo (fragment) | Nguyên nhân | Cách khắc phục |
|---|---|---|
| `Postgres stores could not be opened: error communicating with database: cannot reach Postgres at <host>:<port> within 5s` | Không có phản hồi trong 5 giây: sai host hoặc cổng, cơ sở dữ liệu ngừng hoạt động, hoặc bị tường lửa chặn | Kiểm tra máy chạy server có đến được host và cổng đó không |
| `password authentication failed for user "<user>"` | Sai người dùng hoặc mật khẩu | Sửa thông tin xác thực trong DSN |
| `database "<name>" does not exist` | Cơ sở dữ liệu trong DSN không tồn tại | Tạo nó; linch-rust tạo bảng, không tạo cơ sở dữ liệu |
| `error with configuration: relative URL without a base` | DSN không phải là URL | Dùng `postgres://user:pass@host:port/db` |
| `failed to lookup address information` | Không phân giải được tên host | Sửa host |
| `the migrations did not finish within 30s` | Một tiến trình khác đang giữ khóa migration | Chờ nó, hoặc tìm migration bị kẹt |

Nếu kết nối bị đứt giữa chừng một run, stream của run đó kết thúc với `RunError`
(mã -32020); run giữ nguyên ở trạng thái bền vững cuối cùng và có thể được resume.

<a id="runs"></a>

## Run

Các lỗi này kết thúc stream của một run: duyệt `Run`, hoặc gọi `run.result()`, sẽ
raise chúng.

| Exception | Thông báo | Nguyên nhân | Cách khắc phục |
|---|---|---|---|
| `SessionBusy` (-32030) | `session <id> is busy with run <run id>` | Session có một run đang chạy hoặc đang chờ phê duyệt hay câu trả lời | Resume, phê duyệt, trả lời hoặc hủy `busy.run_id` trước, hoặc xếp prompt vào hàng đợi ([Run](./runs.md#one-live-run-per-session)) |
| `RunError` (-32023) | `conflict: run <id> already exists in <namespace>` | `agent.run(..., run_id=...)` dùng lại một id | Dùng id mới, hoặc `resume` run hiện có |
| `RunNotFound` (-32021) | `run <id> not found in namespace <namespace>` | Không có run đó trong kho lưu trữ của agent; với kho trong bộ nhớ, một handle `Agent` khác đã bắt đầu nó, hoặc server đã khởi động lại | Với kho trong bộ nhớ, dùng handle đã bắt đầu run; với Postgres, kiểm tra namespace và DSN |
| `RunTerminal` (-32026) | `run <id> is Completed and cannot continue` | `resume`, `approve` hoặc `answer` một run đã kết thúc, kể cả run đã bị hủy (`data["status"]` là `aborted`); `data["status"]` là trạng thái của nó | Bắt đầu một run mới trong session |
| `ContractMismatch` (-32025) | `run <id> has contract <expected>, this agent has <found>` (tên các khóa của `data`) | Spec, tool, callback hoặc (khi không phải mặc định) permission policy của agent khác với những gì run đã bắt đầu; sau một lần nâng cấp, một run do binary cũ hơn bắt đầu dưới một policy không mặc định | Mở lại với cấu hình gốc, hoặc để run kết thúc trước; một run bị tạm dừng vẫn có thể được kết thúc bằng `agent.cancel` ([Hợp đồng của run](./agent.md#the-run-contract)) |
| `NotSuspended` (-32027) | `run <id> is not awaiting this approval or answer; if an approve or answer already committed, continue the run with resume` | Một approve hoặc answer được gửi lại sau khi nó đã được commit (tiến trình hoặc kết nối của bạn đã chết), hoặc một câu trả lời cho run đang chờ phê duyệt | Gọi `agent.resume(run_id)` ([Run](./runs.md#approve)) |
| `PromptBlocked` (-32029) | lý do của callback, trong `.reason` | Một callback `user_prompt_submit` đã chặn prompt | Không có gì được ghi; đổi prompt hoặc callback |
| `InvalidOutputSchema` (-32031) | `run <id> was not started: its output schema is unusable: <reason>` | `agent.run(..., output_schema=...)` nhận một schema không compile được, hoặc root của nó không phải `{"type": "object"}` | Không có gì được ghi; sửa schema ([Structured output](./structured-output.md)) |
| `RunLeased` (-32033) | `run <id> is held by "<owner>" for up to <duration> more` | Một server khác (một agent handle khác) đang điều khiển run trên Postgres, hoặc đã crash chưa đầy một lease trước; từ `agent.cancel`, một approve ở nơi khác vừa claim run bị tạm dừng | Chờ run đó dừng, hoặc thử lại sau `.expires_in` giây ([Kho lưu trữ](./stores.md#run-leases)) |
| `LeaseLost` (-32022) | `lease on run <id> lost (epoch <n>)` | Một server khác đã tiếp quản run (server này đứng quá lease của nó) hoặc đã fail nó; từ `agent.cancel`, một approve hoặc answer đã claim run sau cancel, và nó tiếp tục | Ở đây không có gì để sửa: bên điều khiển mới của run đang giữ nó. Nếu server này bị tạm dừng, hãy kiểm tra các lần dừng GC hay mạng kéo dài, hoặc tăng `stores.lease_ttl_ms` |
| `InvalidAnswer` (-32032) | `run <id> cannot take this answer: <reason>` (lý do nằm trong `.reason`) | `agent.answer` nêu tên một request không đang chờ, bỏ sót một câu hỏi chưa trả lời, hoặc đưa danh sách cho câu hỏi chọn một | Không có gì được ghi và run vẫn chờ; hãy trả lời lại ([Tool](./tools.md#hỏi-người-dùng)) |

Do chính lệnh gọi raise:

| Exception | Nguyên nhân | Cách khắc phục |
|---|---|---|
| `RunBusy` (-32002) | Server này đã đang điều khiển run đó, ví dụ `resume` trong khi bạn vẫn đang stream nó | Chờ stream kết thúc, hoặc `cancel()` nó |
| `ConnectionClosed` | `Agent` thuộc về một server đã thoát; một handle chỉ hoạt động trên server đã mở nó | Mở lại agent trên một `Server` mới |

`agent.cancel` và `run.cancel()` của một run bị tạm dừng tự raise `RunNotFound`,
`RunLeased` và `LeaseLost`, với các nguyên nhân ở trên. `False` từ chúng không phải
lỗi: run đã kết thúc, hoặc nó đang chạy và kết nối này không điều khiển nó
([Run](./runs.md#cancel-a-suspended-run)).

Một run thất bại bên trong, không có exception, kết thúc bằng một event `error` và
một `result` có subtype `error`:

| `error["message"]` | Nguyên nhân | Cách khắc phục |
|---|---|---|
| `max turns exceeded` (sau một event `loop_guard`) | Run đã đạt `spec.max_turns` | Tăng giới hạn, hoặc bắt đầu một run mới để tiếp tục |
| fragment: `provider returned HTTP <status>:` | Provider từ chối lệnh gọi; `error["retryable"]` cho biết thử lại có thể giúp hay không | Kiểm tra khóa và model với 401, 403 hoặc 404; với 408, 429 và 5xx thì run đã tự thử lại tới `max_attempts` của [`spec.provider_retry`](./providers.md#lỗi-và-thử-lại); hãy thêm `fallback_models` hoặc thử lại sau |
| fragment: `prompt exceeds the model's context window:` (`kind` `context_length`) | Provider từ chối prompt vì dài hơn context window của model. Trước khi lỗi này được nhận diện, nó hiện là `provider returned HTTP 400:` | Đặt [`spec.compaction`](./memory.md#when-the-prompt-does-not-fit): run khi đó rút gọn view và gọi lại |
| `the prompt exceeds the model's context window, and the compaction it forced could not shorten it` | Có `spec.compaction` nhưng không còn gì để gộp, `max_forced` đã dùng hết, hoặc bản tóm tắt bắt buộc thất bại | Giảm `keep_recent_turns`, bật `micro`, tăng `max_forced`, hoặc giữ tool result và block `context_build` nhỏ hơn |
| fragment: `provider connect error:` (`kind` `connect`) | Kết nối chưa bao giờ được thiết lập (bị từ chối, DNS, TLS, hết thời gian kết nối), nên request chưa được gửi đi | Kiểm tra `base_url` của provider và mạng; run đã tự thử lại rồi |
| fragment: `provider transport error:` (`kind` `transport`) | Request đã gửi đi nhưng không có câu trả lời nào về, hoặc stream bị đứt | Run đã tự thử lại rồi; dưới một `budget` có giới hạn thì không thể, vì lời gọi có thể đã bị tính tiền |
| `invalid provider request: scripted provider ran out of turns after <n>` | Một bài kiểm thử scripted cần nhiều lượt hơn số lượt đã có | Thêm các lượt còn thiếu |

## Tool

Đây là các kết quả tool mà mô hình đọc (`ToolCallEndEvent.is_error` là true); run
vẫn tiếp tục.

| Văn bản kết quả | Nguyên nhân | Cách khắc phục |
|---|---|---|
| `<ExceptionType>: <message>` | Tool của ứng dụng chủ raise exception | Sửa tool, hoặc raise một thông báo mà mô hình có thể hành động theo |
| `Host tool "<name>" timed out after <n>ms` | Tool của ứng dụng chủ không trả lời trong `timeout` của nó (mặc định 300 s); task của nó đã bị hủy | Tăng `@tool(timeout=...)`, hoặc làm tool nhanh hơn |
| `Input for tool "<name>" does not match its schema:` theo sau là các vấn đề | Đầu vào của mô hình không khớp schema | Thường mô hình tự sửa; kiểm tra schema và mô tả |
| `Tool "<name>" is not registered` | Mô hình gọi một tool mà agent không có | Kiểm tra danh sách tool trong event `init` |
| `Tool call denied: <reason>` | `Deny(reason)` khi phê duyệt | Đúng như dự kiến |
| `Tool call denied: <tool> denied by <kind> rule in layer "<layer>"` | Một quy tắc quyền đã từ chối lệnh gọi | Đổi quy tắc nếu lệnh gọi nên chạy ([Tool](./tools.md#permission-rules)) |
| `Read <path> before changing it.` | `Write` lên một tệp có sẵn hoặc `Edit` mà chưa có `Read` trong session này, hoặc sau `close_session` hay khi khởi động lại | Đúng như dự kiến; mô hình đọc, rồi mới ghi |
| `Read this file again before editing it.` / `... before writing it.` | Tệp thay đổi trên đĩa sau khi model đọc | Đúng như dự kiến; mô hình đọc lại rồi thử lại |
| `Tool execution was interrupted before a result was recorded; not re-running automatically on resume.` | Tiến trình đã chết trong khi lệnh gọi đang chạy | Kiểm tra xem tác động đã xảy ra chưa trước khi hành động tiếp |
| `Background process <id> no longer exists in this session: ...` (code `no_such_process`) | `BashOutput` hoặc `BashKill` nêu tên một id mà agent handle này không biết: session đã đóng, server đã khởi động lại, hoặc một handle khác đã khởi động nó | Đúng như dự kiến sau khi khởi động lại; model khởi động lại lệnh ([Tool](./tools.md#lệnh-chạy-nền)) |
| `4 background processes already run in this session; ...` (code `too_many_processes`) | Một lệnh `Bash` chạy nền thứ năm trong một session | Model đọc hoặc kill một lệnh trước |
| `AskUser is not available here: this run cannot wait for the user...` | Một subagent gọi `AskUser` | Đúng như dự kiến; chỉ run cấp cao nhất mới hỏi được |

Bản thân `@tool` raise khi import: `ValueError` với scope không phải `read`,
`write` hoặc `exec` hoặc timeout không dương, và `TypeError` với hàm không phải
`async` hoặc một tham số mà nó không thể gán kiểu (`pass input_schema`).

Các lệnh gọi liên tục hỏi quyền: ở chế độ `default`, mọi lệnh gọi `write` và `exec`
đều hỏi, và ở `accept_edits`, một lệnh ghi ngoài thư mục gốc của dự án sẽ hỏi. Hãy
dùng quy tắc cho phép `tool`, `auto_allow`, callback `permission_request`, hoặc một
chế độ phù hợp ([Tool](./tools.md#permissions)).

## Các trang liên quan

- [Run](./runs.md): bảng lỗi và quy tắc resume.
- [Cấu hình agent](./agent.md): mọi trường.
- [Provider](./providers.md): khóa và endpoint.
