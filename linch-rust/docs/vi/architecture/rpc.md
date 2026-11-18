# Giao thức RPC và server

[Bản gốc tiếng Anh](../../architecture/rpc.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

`linch-rpc` chứa giao thức JSON-RPC 2.0 của Linch: các kiểu message, một
server cho một kết nối và một client Rust. `linch-server` là binary
sidecar phục vụ nó trên stdio, một Unix socket, hoặc HTTP với
WebSocket, kèm tenant và allowlist từ một tệp cấu hình. Một client mở
agent từ cấu hình, start, approve, hủy và phát lại các run, và nhận
mọi envelope của một run trong một notification. Mọi thứ bên trong một lượt chạy trong
server; nó chỉ gọi ngược lại client cho các host tool và cho các
callback mà client đã khai báo.

Chính giao thức được đặc tả trong crate docs của `linch-rpc`
(`crates/linch-rpc/src/lib.rs:1-573`): framing, mọi method, request
và notification của server, và bảng lỗi. Trang này giải thích cách mã
triển khai nó, các giới hạn và kiểm tra nằm ở đâu, và cách thay đổi
nó. [Trang hướng dẫn dùng server](../usage/server.md) chỉ cách chạy nó và
kết nối từ Python.

Linch bản Python không có phần tương ứng: nó là một thư viện mà host nhúng vào
process của chính mình, nên không có wire protocol, tenant hay allowlist. Gói Python
`linch_rust` (`python/src/linch_rust/`) là một client của server này.

## Bố cục crate

| Tệp | Chứa |
|---|---|
| `crates/linch-rpc/src/lib.rs` | Tài liệu giao thức (crate docs) và các export công khai |
| `crates/linch-rpc/src/protocol.rs`, `protocol/{open,call,hook}.rs` | Params và kết quả của mọi method, tên `method::`, `tool_code` |
| `crates/linch-rpc/src/message.rs` | Đọc nghiêm ngặt một request, response hoặc notification JSON-RPC |
| `crates/linch-rpc/src/lines.rs` | Codec dòng, giữ phần đầu của một dòng quá khổ để tìm `id` của nó |
| `crates/linch-rpc/src/error.rs` | Mã lỗi và các `ErrorObject` từ lỗi của lõi và facade |
| `crates/linch-rpc/src/access.rs`, `access/{admit,allowlists}.rs` | `Access`, `Tenant`, `Tokens`, `Allowlists` và các kiểm tra admission |
| `crates/linch-rpc/src/server.rs` | Trạng thái của một kết nối, vòng lặp đọc và việc dispatch method |
| `crates/linch-rpc/src/server/connection.rs` | Các điểm vào transport và task writer |
| `crates/linch-rpc/src/server/outgoing.rs` | Cách các envelope của một run được gộp thành một `run.event` |
| `crates/linch-rpc/src/server/login.rs` | `connection.auth` của socket |
| `crates/linch-rpc/src/server/agents.rs`, `server/run.rs` | Các handler của method |
| `crates/linch-rpc/src/protocol/schedule.rs`, `crates/linch-rpc/src/server/schedule.rs` | Các method của scheduler: params, kết quả và handler |
| `crates/linch-rpc/src/background.rs` | `BackgroundAgents`, `AgentFactory` của worker |
| `crates/linch-rpc/src/server/peer.rs`, `server/host.rs`, `server/callback.rs` | Các request từ server: host tool (`tool.call`) và callback (`hook.call`) |
| `crates/linch-rpc/src/client.rs` | `Client` Rust |
| `crates/linch-server/src/main.rs` | Binary: stdio, hoặc các listener với `--config FILE` |
| `crates/linch-server/src/{config,listen,http}.rs` | Cấu hình, Unix socket và transport HTTP |
| `crates/linch-server/src/worker.rs` | `BackgroundWorker`: phần `worker` của cấu hình chạy trong tiến trình |
| `crates/linch/src/pools.rs` | `PgPools`: các pool Postgres theo đích; mỗi tenant có một bộ riêng, và một kết nối cục bộ mang một bộ trong `Access::Local` |

## Method và message

Mỗi tên method là một hằng số `method::` (`crates/linch-rpc/src/protocol.rs:28-66`),
và `Server::dispatch` (`crates/linch-rpc/src/server.rs:304-322`) ánh xạ từng tên
tới handler của nó. Các kiểu params từ chối trường không biết; `OpenParams` tách
`tools` và `callbacks` ra và đọc phần còn lại thành một `AgentConfig` nghiêm ngặt
(`crates/linch-rpc/src/protocol/open.rs:13-28`).

| Method | Params | Handler | Kết quả |
|---|---|---|---|
| `connection.auth` | `AuthParams` (`protocol.rs:73`) | `login` (`crates/linch-rpc/src/server/login.rs:24`), chỉ là message đầu tiên trên socket; ở nơi khác là `-32600` | `Authenticated` `{tenant}` |
| `agent.open` | `OpenParams` | `Server::open` (`crates/linch-rpc/src/server/agents.rs:20-49`) | `AgentOpened` `{agent, contract, skipped?}` |
| `agent.close` | `CloseParams` | `agents.rs:52-80`; `-32002` khi kết nối đang điều khiển một run của nó | `{}` sau khi các MCP server của nó đã tắt |
| `session.close` | `SessionCloseParams` | `agents.rs:81-92`: `Agent::close_session` (quên các lần đọc của session, kill các lệnh `Bash` chạy nền của nó) | `{}` |
| `run.start` | `StartParams` (`protocol.rs:191`) | `Server::start` (`crates/linch-rpc/src/server/run.rs:20-43`) | `RunAccepted` `{run_id}`, rồi các notification |
| `run.resume` | `ResumeParams` (`protocol.rs:215`) | `run.rs:45-50`, qua `claim` | như trên |
| `run.approve` | `ApproveParams` (`protocol.rs:228`) | `run.rs:52-58`, qua `claim` | như trên |
| `run.answer` | `AnswerParams` (`protocol.rs:256`) `{agent, run_id, answers, principal?}`, `answers` được khóa theo `request_id` | `run.rs:61-67`, qua `claim` | như trên |
| `run.steer` | `SteerParams` `{agent, run_id, text, id?, principal?}` | `Server::steer_run` (`run.rs`): `Agent::steer`. Một run mà process này điều khiển nhận nó qua inbox và trả lời sau lần flush của bước kế tiếp; một run bị tạm dừng được claim, ghi vào rồi đặt lại trong trạng thái tạm dừng; một run mà process khác giữ lease là `-32033` | `Steered` `{queued: true, id}` |
| `run.cancel` | `CancelParams` `{run_id, agent?}` | `Server::cancel` (`run.rs`): hủy một run mà kết nối này điều khiển bằng token của nó; nếu không, khi có `agent`, kết thúc một run bị tạm dừng bằng `Agent::cancel`, run được tính là đang được điều khiển trong lúc đó nên một approve ở đây nhận `-32002` | `{cancelled}` |
| `run.events` | `EventsParams` | `agents.rs:93-100`: `Agent::events` | `{events}`, các envelope bền vững sau `after_seq` |
| `provider.capabilities` | `ProviderModelParams` `{provider, model}`: một `provider` như trong cấu hình agent (`protocol.rs`) | `server/provider.rs`: `ProviderConfig::build_keyless`, `Provider::prepare`, `Provider::capabilities`; không cần key | `ProviderCapabilities` |
| `provider.models` | `ProviderParams` `{provider}` | `server/provider.rs`: `ProviderConfig::build`, `Provider::list_models`; cần key | `{models}`, một `ModelInfo` cho mỗi model |
| `provider.model` | `ProviderModelParams` | `server/provider.rs`: `Provider::model_info` | `ModelInfo`; `-32036` khi danh sách không có nó |
| `eval.run` | `EvalRunParams` `{agent, cases, scorers?, include_events?}` (`protocol/monitor.rs`) | `server/monitor.rs`: `run_eval` trên agent; chiếm một chỗ run của tenant và đánh dấu agent bận (`agent.close` trả `-32002`) cho đến khi xong; dừng cùng kết nối | kết quả eval dạng JSON (`total`, `passed`, `pass_rate`, `total_cost_usd`, `usage`, `duration_ms`, `cases`) và `markdown`; case hoặc scorer sai là `-32602` |
| `run.report` | `ReportParams` `{agent, run_id, timeline?}` | `server/monitor.rs`: `build_run_report` trên `Agent::run_record` và `Agent::events` | `{report, markdown}`; run không tồn tại là `-32021` |
| `workflow.start` | `WorkflowStartParams` `{agent, run_id, budget?, resume?, principal?}` (`protocol/workflow.rs`) | `Server::start_workflow` (`server/workflow.rs`): `admit_principal`, các mức trần của tenant, rồi `WorkflowSession::open_held`; `-32045` khi một client khác đang drive nó | `WorkflowStarted` `{records, resumed, previous?, limits?}` |
| `workflow.agent` | `WorkflowAgentParams` `{run_id, prompt, name?, subagent?, label?, key?, output_schema?, fork?, timeout_ms?, retry?, branch?, call_id?}` | `workflow_agent`: `WorkflowContext::agent_with` trên nhánh; bị bỏ cùng kết nối như một run | `WorkflowAgentResult` `{text, structured?, session_id, tokens, cost_usd, replayed}` |
| `workflow.step.begin`, `workflow.step.complete` | `{run_id, name, key?, branch?}`, `{run_id, ticket, value}` | `step_begin`, `step_complete`; ticket do server giữ | `{replayed, value?, ticket?}`, `{}` |
| `workflow.interrupt` | `{run_id, key, payload?, branch?}` | `WorkflowContext::interrupt` | `{suspended, value?}` |
| `workflow.cancel` | `{run_id, call_id?}` | hủy một lời gọi agent đang chạy, hoặc tất cả | `{cancelled}` |
| `workflow.finish` | `{run_id, ok}` | kết thúc các lời gọi đang chạy, ghi trạng thái (`awaiting_input` khi một interrupt đã suspend nó), giải phóng hold | `{status}` |
| `workflow.status` | `{agent, run_id}` | đọc workflow đã lưu | `{status, records, interrupt?}` |
| `schedule.create` | `ScheduleCreateParams` (`protocol/schedule.rs`) | `Server::create_schedule` (`server/schedule.rs`) | `ScheduleInfo` |
| `schedule.list` | `ScheduleListParams` | `server/schedule.rs` | `{schedules}` |
| `schedule.pause`, `schedule.resume` | `ScheduleIdParams` | `server/schedule.rs`; `-32041` cho một id không biết | `ScheduleInfo` |
| `schedule.delete` | `ScheduleIdParams` | `server/schedule.rs` | `{deleted}` |
| `job.enqueue` | `JobEnqueueParams` | `server/schedule.rs` | `JobInfo` |
| `job.get` | `JobGetParams` | `server/schedule.rs`; `-32043` cho một id không biết | `JobInfo` |
| `job.list` | `JobListParams` | `server/schedule.rs` | `{jobs}` |

Server gửi ba notification. `workflow.event` `{run_id, event}` mang từng `WorkflowEvent` của một workflow mà kết nối đang drive, theo thứ tự, trước câu trả lời của lời gọi tạo ra nó. Hai cái còn lại: `run.event` `{run_id, envelopes}`
(`RunEvent`, `protocol.rs:293`) mang một hoặc nhiều envelope liên tiếp
của một run; `run.end` `{run_id, error?}` (`RunEnd`, `protocol.rs:303`) là
message cuối cùng của một run. Nó cũng gửi các request riêng: `tool.call`
cho một host tool (`crates/linch-rpc/src/protocol/call.rs:12`,
`crates/linch-rpc/src/server/host.rs:70`) và `hook.call` cho một callback
(`crates/linch-rpc/src/protocol/hook.rs:46`,
`crates/linch-rpc/src/server/callback.rs:128`). Id của chúng là `srv-<n>`, nên
không bao giờ trùng với id số của client; một lời gọi thôi không chờ nữa sẽ gửi
`$/cancelRequest` `{id}` và bỏ một câu trả lời đến muộn
(`crates/linch-rpc/src/server/peer.rs:1-7`).

### Lỗi

`ErrorObject` là `{code, message, data?}`; bảng đầy đủ nằm trong crate
docs (`lib.rs:518-569`) và các mã nằm trong `crates/linch-rpc/src/error.rs:24-183`.

| Mã | Hằng | Python | Ý nghĩa |
|---|---|---|---|
| -32700 | `PARSE_ERROR` | `ProtocolError` | Dòng không phải JSON |
| -32600 | `INVALID_REQUEST` | `ProtocolError` | Không phải request object hợp lệ, hoặc là một batch |
| -32601 | `METHOD_NOT_FOUND` | `ProtocolError` | Không có method như vậy |
| -32602 | `INVALID_PARAMS` | `ProtocolError` | Params không khớp method: field lạ hoặc thiếu, sai kiểu |
| -32603 | `INTERNAL_ERROR` | `ProtocolError` | Lỗi của server, gồm cả panic |
| -32001 | `AGENT_NOT_FOUND` | `AgentNotFound` | Không agent nào đang mở có handle đó |
| -32002 | `RUN_BUSY` | `RunBusy` | Connection đã điều khiển run đó, hoặc `agent.close` nêu một agent đang có run |
| -32003 | `UNAUTHENTICATED` | `Unauthenticated` | Message đầu tiên của socket không phải `connection.auth` với token đã biết; server đóng connection |
| -32004 | `NOT_ALLOWED` | `NotAllowed` | Một kiểm tra tenant từ chối; `data.kind` nêu tên nó (bảng trong [Server](../usage/server.md#những-gì-admission-từ-chối-và-lý-do)), trong đó có `run_bound` và `workflow_principal` |
| -32005 | `LIMIT_REACHED` | `LimitReached` | Đã chạm một giới hạn và chưa có gì bắt đầu; `data: {kind, max}`, `kind` là một trong `runs`, `agents`, `connections`, `logins`, `schedules`, `jobs`, `workflows`, `agent_calls_in_flight`, `journal_records` hoặc `steers` (một run có 16 steer đang chờ), hoặc `provider_calls`, một tốc độ: tối đa `max` lời gọi `provider.models` và `provider.model` mỗi `per_seconds` (60) cho tenant |
| -32010 | `BUILD_MISSING` | `OpenError` | `agent.open`: thiếu một binding bắt buộc |
| -32011 | `BUILD_INVALID` | `OpenError` | `agent.open`: config không hợp lệ, hoặc lỗi workspace, policy hay tool. Trong đó có: `budget.max_cost_usd` trên một model không có giá (model chính hoặc một mục `fallback_models`; thông báo nêu tên model), `spec.call_timeout_ms` dưới 100, và `workflow.start` có `max_cost_usd` cho agent có model chưa có giá |
| -32012 | `BUILD_CONTRACT` | `OpenError` | `agent.open`: không tính được digest của run contract |
| -32013 | `PROVIDER_UNAVAILABLE` | `OpenError` | Key của provider chưa đặt (`data.env` nêu biến của nó), hoặc không dựng được client của provider |
| -32014 | `STORE_UNAVAILABLE` | `OpenError` | Không kết nối hay migrate được Postgres |
| -32015 | `MCP_UNAVAILABLE` | `OpenError` | Không kết nối được một MCP server trong config; `data.server` nêu tên nó |
| -32020 | `STORE` | `RunError` | Một lỗi store không nằm trong danh sách dưới (cả một connection database rớt giữa run) |
| -32021 | `NOT_FOUND` | `RunNotFound` | Không có run hay session như vậy; cũng là `run.report` và `workflow.status` của một id mà namespace của agent không có |
| -32022 | `LEASE_LOST` | `LeaseLost` | Từ một claim hoặc một lần ghi journal bị fence: một process khác đã nhận run, hoặc làm nó thất bại. Run dừng ở đây mà không ghi |
| -32023 | `CONFLICT` | `RunExists` | Một run id tự chọn đã được lưu |
| -32024 | `JOURNAL` | `RunError` | Một lần ghi journal thất bại |
| -32025 | `CONTRACT_MISMATCH` | `ContractMismatch` | Contract của agent khác contract của run; `data.expected` là contract mà run bắt đầu và `data.found` là của agent này |
| -32026 | `TERMINAL` | `RunTerminal` | Run đã kết thúc; `data.status` là trạng thái cuối |
| -32027 | `NOT_SUSPENDED` | `NotSuspended` | `run.approve` trên một run không chờ phê duyệt, hoặc `run.answer` trên một run không chờ input; host đã thử lại một trong hai thì tiếp tục run bằng `run.resume` |
| -32028 | `RECONCILE` | `RunError` | Trạng thái bền vững không khớp nhau |
| -32029 | `PROMPT_BLOCKED` | `PromptBlocked` | Một hook `user_prompt_submit` từ chối prompt; `data.reason` nói lý do. Chưa ghi gì |
| -32030 | `SESSION_BUSY` | `SessionBusy` | Session đã có một run chưa kết thúc; `data: {session_id, run_id}` |
| -32031 | `INVALID_OUTPUT_SCHEMA` | `InvalidOutputSchema` | `output_schema` của `run.start` không compile, hoặc root của nó không phải object; `data.reason` |
| -32032 | `INVALID_ANSWER` | `InvalidAnswer` | Một `run.answer` không khớp các yêu cầu input đang chờ; `data.reason`. Run vẫn chờ input |
| -32033 | `RUN_LEASED` | `RunLeased` | Một process khác điều khiển run và giữ lease còn sống; `data: {run_id, owner, expires_in_ms}` |
| -32034 | `INVALID_TOOL_CHOICE` | `InvalidToolChoice` | `tool_choice` của `run.start` nêu một tool mà run không cung cấp, hoặc provider từ chối nó; `data.reason` |
| -32035 | `MODELS_UNSUPPORTED` | `ModelsUnsupported` | `provider.models` hay `provider.model` nêu một provider không có danh sách model (`scripted`); `data.provider` |
| -32036 | `MODEL_NOT_FOUND` | `ModelNotFound` | `provider.model` nêu một model mà danh sách không có; `data.model`, và `data.truncated` khi danh sách bị cắt ngắn |
| -32037 | `PROVIDER_REQUEST_FAILED` | `ProviderRequestFailed` | Không hỏi được provider, hoặc nó trả lỗi; `data: {kind, status?}`, `kind` là một trong `unreachable` (connect, reset và timeout như nhau), `status`, `invalid_request`, `other`. Một thông báo cố định cho mỗi kind: không bao giờ có key, URL hay lời của vendor |
| -32038 | `INVALID_STEER` | `InvalidSteer` | `run.steer` với text trống hoặc quá 16 KiB, id không dùng được, hoặc một run đang rảnh (xếp hàng, hoặc đang chạy mà không server nào điều khiển: hãy resume nó trước); `data.reason`. Chưa ghi gì |
| -32040 | `NOT_BACKGROUND` | `NotBackground` | `schedule.create` hay `job.enqueue` cho một agent không chạy nền được; `data.reason` là `memory_store`, `host_tools`, `callbacks` hoặc `remote_execution` |
| -32041 | `SCHEDULE_NOT_FOUND` | `ScheduleNotFound` | Namespace của agent không có schedule với id đó |
| -32042 | `SCHEDULE_EXISTS` | `ScheduleExists` | Namespace đã có một schedule với id đó |
| -32043 | `JOB_NOT_FOUND` | `JobNotFound` | Namespace không có job với id đó, hoặc lịch sử của nó đã bị dọn |
| -32044 | `INVALID_SCHEDULE` | `InvalidSchedule` | Thời gian của schedule không dùng được; `data.reason` |
| -32045 | `WORKFLOW_HELD` | `WorkflowHeld` | Một client khác điều khiển workflow: connection này, connection khác hoặc server khác. `workflow.start` chưa ghi gì |
| -32046 | `WORKFLOW_BUDGET` | `WorkflowBudgetExceeded` | Budget đã hết (`data: {spent_tokens, spent_usd, max_tokens?, max_cost_usd?}`), hoặc `data: {kind: "unpriced", model}` khi `max_cost_usd` không tính được một lời gọi của model không có giá. Không lời gọi agent nào bắt đầu nữa |
| -32047 | `WORKFLOW_CALL_FAILED` | `WorkflowCallFailed` | Một lời gọi `workflow.agent` không cho kết quả; `data.kind` là `failed`, `aborted`, `timeout`, `approval` hoặc `invalid` |
| -32048 | `WORKFLOW_NOT_FOUND` | `WorkflowNotFound` | Connection này không điều khiển workflow nào có id đó: chưa gửi `workflow.start` cho nó, hoặc đã finish nó |
| -32049 | `WORKFLOW_NO_JOURNAL` | `WorkflowNoJournal` | `workflow.start` với câu trả lời `resume` cho một workflow mà journal trống (nó chưa chạy ở đây, hoặc journal của agent memory-store nằm ở connection khác). Chưa claim gì |

`RunError::ContractMismatch {expected, found}` nêu tên các contract từ phía của agent:
`expected` là contract của agent đã cố tiếp tục run, `found` là contract được lưu cùng
run. Wire đảo chúng lại, để `data.expected` là contract của run và `data.found` là của
agent, đúng như văn bản thông báo (`error.rs:260-269`, được test ở
`error.rs:407-428`).

Phần đăng nhập của socket trả lời các lần từ chối dưới những id khác nhau
(`login.rs:33-85`): `-32005` `kind: logins` dưới id null trước khi đọc bất cứ thứ gì;
`-32003` dưới id của request, hoặc null khi message đầu tiên
không có id hoặc không có message nào đến trong thời gian chờ đăng nhập;
`-32005` `kind: connections` dưới id `connection.auth`.
Client Python đọc dòng đầu tiên nó nhận được làm câu trả lời cho lần đăng nhập của nó,
bất kể id (`python/src/linch_rust/_remote.py:112-142`).

Client Python ánh xạ mã sang exception trong `_BY_CODE`
(`python/src/linch_rust/errors.py:166-178`), với các dải cho phần còn lại
(`errors.py:181-194`); `python/tests/test_protocol.py:202` cố định ánh xạ này.

### Các method workflow

Hàm workflow chạy trong client, và server giữ những gì phải bền vững. Wire là lát
cắt nhỏ nhất giữ được cả hai điều đó: một lời gọi phải replay được là một request
(`workflow.agent`, `workflow.interrupt`, hoặc `begin` và `complete` của một step),
nên một lần crash giữa hai request không làm mất gì mà journal đã có, và các bộ
đếm occurrence nằm cùng journal, trong `WorkflowSession` của kết nối. Client nêu
tên nhánh fan-out của nó trong mỗi lời gọi, nên `parallel` chạy ở phía client mà
vẫn tìm thấy record của từng nhánh khi resume. Một `wf.agent` bị hủy gửi
`workflow.cancel` cho `call_id` của nó và chờ lời gọi kết thúc, nên một `parallel`
thất bại đã ngừng tiêu tiền khi nó raise.

Server drive một `workflow.agent` như một run: kết nối kết thúc sẽ bỏ nó, để run
con ở trạng thái bền vững cuối cùng, và lần drive kế tiếp tiếp tục run đó. Chỉ
`workflow.cancel` mới abort một run con. `workflow.finish` làm yên các lời gọi
đang chạy, ghi trạng thái và giải phóng hold. `max_workflows` của tenant được lấy
ở `workflow.start`; `max_fan_out` và chỗ run của tenant được `LiveGate` lấy trước
mỗi lời gọi thật. Lỗi journal đến tenant dưới dạng văn bản cố định `the workflow
journal failed`; nguyên nhân đi vào log của server.

## Bên trong một kết nối

Mỗi kết nối là một `Server` (`server.rs:55-83`): các bảng agent đang mở,
run đang được điều khiển và run đang park dưới một khóa, để `agent.close` không thể
bỏ sót một run bắt đầu trong lúc nó kiểm tra; outbox của nó; peer của nó cho các
request từ server; và tenant của nó, là `None` đối với client cục bộ.

1. **Transport.** `serve_lines` (`crates/linch-rpc/src/server/connection.rs:73-97`)
   chia một luồng byte thành các dòng; `serve_messages` (`connection.rs:113-135`)
   nhận một message cho mỗi frame. Cả hai đưa các mục `Inbound` (`connection.rs:138-146`)
   cho `connection` (`connection.rs:148-211`), thứ spawn task writer,
   chạy phần đăng nhập khi access là `Login`, và đọc cho đến khi input kết thúc
   hoặc `stop` kích hoạt.
2. **Vòng lặp đọc.** `Server::receive` (`server.rs:181-217`) parse một dòng,
   đưa câu trả lời của client cho request của server mà nó nêu tên, và đọc
   mọi thứ khác như một request (`Request::from_value`,
   `crates/linch-rpc/src/message.rs:108`). Một request không có `id` là một
   notification và bị bỏ. Sau đó mỗi lời gọi lấy một trong 64 permit
   (`MAX_REQUESTS`, `server.rs:51`); vòng lặp await nó, nên request thứ 65
   dừng việc đọc cho đến khi một lời gọi kết thúc. Lời gọi chạy trong task riêng,
   nên một `agent.open` chậm không bao giờ giữ chân một `run.cancel`.
3. **Dispatch.** `Server::handle` (`server.rs:275-302`) bắt một panic thành
   `-32603`. Một method run trả về stream của nó: kết quả `RunAccepted`
   được xếp hàng trước, rồi một task điều khiển stream.
4. **Một run.** `Server::drive` (`run.rs:159-196`) mã hóa mỗi envelope một lần,
   thành một giá trị JSON thô, và xếp hàng nó. Khi stream kết thúc, nó drop stream,
   gỡ run khỏi các bảng (giữ chỗ của nó khi nó park, xem
   [các giới hạn](#các-giới-hạn)) và xếp hàng `run.end`. Một panic của run kết thúc nó với
   `-32603`, tại trạng thái bền vững cuối cùng của nó.
5. **Writer.** `write_messages` (`connection.rs:215-244`) lấy mọi thứ
   đã xếp hàng, gộp các envelope liên tiếp của một run thành một
   `run.event` (`Batch::push`, `crates/linch-rpc/src/server/outgoing.rs:74-83`)
   tối đa 64 KiB (`outgoing.rs:21`), và flush một lần khi không còn gì chờ.
6. **Request từ server.** `Peer::call` (`peer.rs:92-139`) xếp hàng request
   và chờ câu trả lời, timeout hoặc lệnh hủy của nó; thời gian chờ chỗ trống trong
   outbox được tính vào cả hai.
7. **Hết input.** `Server::stop` (`server.rs:159-178`) để các lời gọi
   đã đọc kết thúc, drop mọi run tại chỗ nó đang đứng (một run bền vững vẫn
   resume được), làm các request đang chờ của server thất bại, rồi đóng các MCP server
   của mọi agent. Writer xả hết hàng đợi và kết nối trả về.

### Thứ tự

- `RunAccepted` của một run được xếp hàng trước khi task điều khiển nó được
  spawn (`server.rs:288-299`), nên nó đến client trước mọi
  notification của run đó. Client Python dựa vào điều này: nó phân loại các
  notification của một run theo run id mà nó đọc từ kết quả đó
  (`python/src/linch_rust/_rpc.py:306-317`).
- Mọi message đi qua một hàng đợi tới một writer, nên wire giữ
  thứ tự của hàng đợi. Một batch chỉ gộp những gì đã được xếp hàng, và mọi
  message khác hay envelope của run khác sẽ đóng nó (`outgoing.rs:1-10`): một
  `tool.call` vẫn theo sau `tool_call_start` của lời gọi đó.
- Các envelope bền vững chỉ đến stream của server khi chúng đã bền vững;
  những envelope chỉ có trong stream đến ngay với seq bền vững gần nhất
  ([các lớp lưu trữ bền vững](./events.md#các-lớp-lưu-trữ-bền-vững)).
- `drive` drop stream trước `run.end` (`run.rs:183-185`), để run
  ở trạng thái bền vững cuối cùng khi client có thể resume nó, và gỡ
  run khỏi các bảng dưới khóa, để không có approve nào claim nó ở giữa.
- Câu trả lời cho các lời gọi đang bay đến theo thứ tự bất kỳ; hãy khớp chúng theo `id`.

### Backpressure

Không có buffer nào tăng không giới hạn trên server:

1. Outbox chứa 1024 message (`OUTBOX_CAPACITY`, `connection.rs:31`).
   Writer chờ transport, nên một client đọc chậm sẽ làm đầy nó.
2. Task của một run chờ chỗ trống trước khi poll stream của run lần nữa, và
   một run chỉ tiến lên khi stream của nó được poll (lõi không spawn task nào
   cho nó), nên run tạm dừng. Một request của server đang chờ chỗ trống tính thời gian
   đó vào timeout của nó.
3. Việc đọc dừng ở 64 lời gọi đang bay.
4. Client Rust giữ tối đa 1024 notification chưa đọc
   (`NOTIFICATION_CAPACITY`, `crates/linch-rpc/src/client.rs:29`), rồi
   ngừng đọc, nên các câu trả lời cũng phải chờ sau chúng (`client.rs:103-110`).
   Client Python làm tương tự với 1024 của riêng nó
   (`NOTIFICATION_CAPACITY`, `python/src/linch_rust/_rpc.py:48`): mỗi
   `run.event` chưa đọc giữ một chỗ trên các run của kết nối, và
   reader chờ một chỗ trước khi xếp hàng cái kế tiếp (`RunQueue`,
   `_rpc.py:62-98`). `Run.cancel` tiếp tục đọc run của nó khi chờ
   câu trả lời (`python/src/linch_rust/run.py:102-121`); một run không ai đọc
   trong khi event của nó vẫn đến sẽ làm kết nối bị nghẽn.

`Outbox::send_now` (`connection.rs:259-269`), dành cho mã không thể chờ,
giao một message cho một task khi hàng đợi đầy.

## Transport

| Transport | Điểm vào | Access | Runtime |
|---|---|---|---|
| stdio | `linch-server` không có đối số; `linch_rpc::serve` (`connection.rs:43-50`) | `Access::Local`: không đăng nhập, không tenant, không giới hạn | Current-thread (`crates/linch-server/src/main.rs:55-61`): một client, nên chuyển một envelope giữa các task của nó không đánh thức thread nào khác |
| Unix socket | `--config FILE` với `socket`; `Listeners` (`crates/linch-server/src/listen.rs:43-102`) | `Access::Login`: message đầu tiên là `connection.auth` | Multi-thread |
| HTTP với WebSocket | `--config FILE` với `http`; `crates/linch-server/src/http.rs:80-121` | `Access::Tenant`: header bearer được kiểm tra trước khi upgrade | Multi-thread |

**stdio.** Trên Linux, server mở lại các pipe của nó qua
`/proc/self/fd/0` và `/proc/self/fd/1` và điều khiển chúng ở chế độ non-blocking
(`main.rs:94-129`). Non-blocking là một cờ của open file
description, thứ mà fd 0 và 1 dùng chung với mọi process giữ cùng
pipe; các description mới giữ cờ này không rò sang các reader khác của một shell hay
supervisor. Mọi thứ không phải pipe dùng `stdin()` và `stdout()` của tokio.

**Unix socket.** Một tệp socket do một server đã dừng để lại sẽ bị thay thế, và
một tệp mà một server đang trả lời trên đó thì bị từ chối (`bind_socket`, `listen.rs:106-123`).
Mỗi kết nối chờ đăng nhập của nó với một trong 32 chỗ
(`MAX_PENDING_LOGINS`, `crates/linch-rpc/src/access.rs:162-166`): mỗi chỗ có thể
đệm một dòng 16 MiB trong 10 s, nên các chỗ này giới hạn mức đó ở 512 MiB. Kết nối thứ
33 nhận `-32005` ngay. Một lần đăng nhập có 10 s (`LOGIN_TIMEOUT`,
`login.rs:17`); im lặng quá mức đó nhận `-32003` và kết nối đóng
(`login.rs:42-54`).

**HTTP.** Một route, `GET /rpc` (`http.rs:29`). Trước khi upgrade, một
kết nối giữ một trong 32 chỗ trên toàn server
(`MAX_PENDING_UPGRADES`, `http.rs:38-43`); kết nối thứ 33 bị đóng mà không đọc. Mỗi
phần đầu request có 10 s (`HEADER_READ_TIMEOUT`, `http.rs:31-36`), sau đó
kết nối đóng mà không trả lời. `upgrade` (`http.rs:132-168`) kiểm tra
bearer token trước (`401` với `WWW-Authenticate: Bearer`), rồi lấy
chỗ kết nối của tenant (`429`), rồi trả lời `101` nêu tên
tenant trong `Linch-Tenant`. Một message WebSocket có thể dài 64 MiB
(`MAX_MESSAGE_BYTES`, `http.rs:49-52`) trước khi socket đóng; một message trên
16 MiB bị lõi từ chối như một dòng quá khổ. Mỗi socket được chấp nhận có TCP keepalive và
một TCP user timeout (`liveness.rs`, `tune`), và một WebSocket được ping mỗi `ping_interval` và bị
kết thúc sau `pong_timeout` im lặng (`http.rs`, `serve_socket`): một vòng đọc frame riêng nạp
kết nối qua một channel dung lượng một, nên một kết nối đang bận 64 lời gọi, thứ ngừng đọc input,
vẫn đọc được các pong của nó. Các mốc thời gian là phần `liveness` của cấu hình. Server nói HTTP
thuần: hãy đặt TLS phía trước nó hoặc listen trên loopback.

Một server đang listen không giữ pool riêng: mọi kết nối hành động thay cho một tenant
và dùng các pool của tenant đó (bên dưới).

## Tenant và allowlist

`ServerConfig::from_json` (`crates/linch-server/src/config.rs:186`) đọc
cấu hình, từ chối các trường không biết và giới hạn bằng 0; `tokens`
(`config.rs:187`) đọc từng token trực tiếp hoặc từ môi trường;
`AllowConfig::build` (`config.rs:220`) tạo các `Allowlists`.

- Một `Tenant` (`access.rs:45-152`) có tên gồm 1 đến 64 chữ cái ASCII,
  chữ số, `.`, `_` hoặc `-` (không bao giờ có `/`), các allowlist của nó, và một semaphore
  cho mỗi loại: run, agent và connection, dùng chung giữa mọi kết nối của nó.
- `Tokens` (`access.rs:168-233`) giữ mỗi token dưới dạng digest SHA-256 và
  so sánh một token được đưa ra với mọi digest trong thời gian hằng số. Không token nào
  được ghi log hay echo lại.
- `Tenant::admit` (`crates/linch-rpc/src/access/admit.rs:20-136`) kiểm tra một
  cấu hình `agent.open` trước khi bất cứ thứ gì bắt đầu: không đọc khóa nào, không
  khởi động process MCP nào, không kết nối cơ sở dữ liệu nào cho cấu hình bị từ chối. Mỗi
  lần từ chối là `-32004` với `data.kind`:

| Kiểm tra | `kind` | Ở đâu |
|---|---|---|
| Namespace là tên của tenant hoặc bắt đầu bằng `<tenant>/` | `namespace` | `admit.rs:28-38` |
| Provider scripted chỉ khi được phép; một `base_url` do client chọn chỉ ở một origin được phép, vì khóa của server sẽ được gửi tới đó | `provider`, `provider_base_url` | `admit.rs:154-170`, `admit.rs:241-255` |
| Chế độ quyền nằm trong danh sách của tenant; `tasks` và `deep_agent`, vốn tự động cho phép các task tool, cần `skip_dangerous` ở đó | `permission_mode`, `auto_allow` | `admit.rs:41-59` |
| Các mục `auto_allow` và các rule chỉ cho phép khi các chế độ của tenant gồm `skip_dangerous` | `auto_allow`, `permission_rule` | `admit_grants`, `admit.rs:211-235` |
| `project_root` và `workspace` là thư mục đã tồn tại dưới một gốc được phép, được thay bằng đường dẫn canonical của chúng | `project_root`, `workspace` | `admit.rs:60-72`, `admit_dir` ở `admit.rs:172-181` |
| Khi skill hoặc subagent bật, thư mục cấu hình là thư mục đã tồn tại dưới một gốc được phép, hoặc `.linch` mặc định ở đó hoặc bị thiếu | `config_dir` | `admit.rs:73-77`, `admit.rs:183-209` |
| `Bash` chỉ khi tenant cho phép các exec tool; nếu không thì nó bị tắt, không bị từ chối | không có | `admit.rs:78` |
| Execution backend bằng một trong các mục `execution` của tenant (mặc định `[local]`); khi không có, nó thành mục đầu tiên, nên một tenant có thể bị giữ trong sandbox | `execution` | `admit_execution` trong `admit.rs` |
| Một DSN Postgres chỉ cho một đích được phép | `postgres` | `admit.rs:79-89` |
| `stores.max_connections` tối đa bằng `max_pool_connections` của tenant (mặc định: kích thước pool mặc định của server); khi không có, nó được đặt thành mặc định hoặc mức trần, tùy giá trị nào nhỏ hơn | `pool_connections`, với `data.max` là mức trần | `admit.rs:90-105` |
| Một MCP stdio server chỉ với `command` và `args` của một mục, chỉ đặt các tên `env` mà mục đó liệt kê; một MCP URL chỉ ở một origin được phép | `mcp_command`, `mcp_env`, `mcp_url` | `admit.rs:107-134` |
| Tối đa `MAX_GUARDRAILS` (32) redaction rule và verifier, và keyword cho mỗi verifier, mỗi pattern tối đa `MAX_PATTERN_BYTES` (1024) byte: server biên dịch và chạy chúng trên mọi response | `redaction`, `verification` | `admit_guardrails` trong `admit.rs` |
| Tối đa 16 section trong `spec.system` (tên tối đa 64 byte, văn bản tối đa 16 KiB, tổng cộng 64 KiB cùng `append`), và tối đa 32 id, mỗi id 128 byte, trong mỗi danh sách của `context_budget`; các `system_sections` của operator thay một section cùng tên và được thêm vào đầu tiên, ở mọi lần admission | `system`, `context_budget` | `admit_system` trong `admit.rs` |

`principal` của một method run, khi được đưa ra, phải nêu tên tenant
(`admit_principal`, `admit.rs:138-152`); `workflow.start` cũng vậy, và nó trở thành
principal của run của mọi lời gọi và được lưu cùng dòng journal
(`linch_ext_workflows.principal`): một claim nêu principal khác là
`JournalStoreError::PrincipalMismatch` và không thay đổi gì. Một resume, approve hoặc answer không có
principal sẽ hành động thay cho principal được lưu cùng run khi nó bắt đầu
(`continue_run` của lõi, từ `RunRecord::principal`).

**Postgres.** `PgTarget::parse` (`crates/linch-rpc/src/access/allowlists.rs:185-222`)
đọc một DSN theo cách store kết nối với nó, qua `PgConnectOptions` của sqlx,
nên một ghi đè `?host=` hay `?user=` được tính và một user hay `sslmode` bị thiếu lấy
mặc định của server. Một mục cho phép một DSN có cùng host, port, socket, database, user,
`options` khởi động và tệp TLS, và một `sslmode` ít nhất nghiêm ngặt bằng của nó, xếp
hạng từ `disable` (0) đến `verify-full` (5) (`PgTarget::allows`, `allowlists.rs:224-245`).

Facade dùng chung một pool cho mỗi đích giữa các agent được mở với cùng một
`PgPools` (`crates/linch/src/pools.rs:19-39`). Một `Tenant` sở hữu một cái
(`access.rs:58`, `access.rs:82`) và `connection` đưa cho kết nối của tenant cái đó
(`connection.rs:163-179`), nên các pool được khóa theo tenant và đích:
hai tenant trên cùng một đích không bao giờ dùng chung pool, và một tenant không thể
đặt kích thước pool của tenant khác (R3 front 5). Mọi kết nối của một tenant dùng chung
các pool của nó, nên `max_pool_connections` giới hạn các kết nối của tenant trên mỗi
đích. Một kết nối cục bộ (stdio) dùng các pool mà `Access::Local` của nó mang theo. Khóa của nó (`pools.rs:45-74`) là URL đã chuẩn hóa
của sqlx cho DSN cộng với `options` khởi động của nó, thứ mà URL đó bỏ
đi và có thể đặt `search_path`; các DSN chỉ khác nhau về thứ tự tham số hoặc về
các cài đặt mà sqlx bỏ qua sẽ dùng chung một pool. Một pool lấy kích thước từ
lần open đã kết nối nó (`pools.rs:94-112`); việc kết nối bỏ cuộc sau
5 s và các migration sau 30 s (`pools.rs:146-164`).

**Lệnh MCP.** Một `McpCommand` (`allowlists.rs:100-122`) không được liệt kê
`PATH`, `HOME` hay các biến được biết là chọn mã nào mà một chương trình nạp
(`CODE_LOADING_ENV` và các tiền tố của nó, `allowlists.rs:124-161`). Danh sách này là
nỗ lực tối đa (best effort).

## Run nền

Các method của scheduler đưa vào hàng đợi công việc mà một worker chạy, không bao giờ là
kết nối đã yêu cầu ([cách dùng](../usage/scheduler.md)).

- **Agent nào.** `agent.open` tính một `Background` cho mỗi agent (`server/schedule.rs`):
  định nghĩa đã lưu của nó, tức cấu hình đã được admit, được serialize kèm các secret của nó
  (giá trị env và header của MCP) và với DSN của store bị xóa trắng; hoặc lý do nó không có
  định nghĩa: kho lưu trữ trong bộ nhớ, host tool hoặc callback, những thứ cần kết nối này.
  Mọi method của scheduler nêu tên một agent đang mở; pool Postgres của nó
  (`OpenedAgent::pool`) giữ các hàng, namespace của nó phân vùng chúng, và các bảng hàng đợi
  được migrate trên cơ sở dữ liệu của nó ở lần gọi scheduler đầu tiên (một lần cho mỗi
  agent), nên một server không bao giờ lập lịch thì không bao giờ tạo chúng.
- **Kiểm tra tenant.** Trước mọi truy vấn: `max_schedules` và `max_queued_jobs` phải được đặt
  (`-32004`, kind `schedules` và `background_runs`), và `Timing::shortest_gap` của một lịch
  phải ít nhất bằng `min_schedule_interval` (`kind: schedule_interval`). Bản thân các số đếm
  là các `linch_worker::Cap` trên tên của tenant làm namespace root, được lấy trong
  transaction của lần insert dưới một advisory lock, nên chúng đúng trên nhiều server dùng
  chung một cơ sở dữ liệu (`-32005`, kind `schedules`, `jobs`).
- **Ai chạy chúng.** `linch-server --config` có phần `worker` chạy một `BackgroundWorker`
  (`crates/linch-server/src/worker.rs`) cạnh các listener của nó, trên DSN của phần đó, dùng
  chung các `Arc<Tenant>` của listener; không có phần này thì nó không chạy gì. stdio thì
  không bao giờ.
- **Ở mỗi lần thử.** `BackgroundAgents::open_job` (`crates/linch-rpc/src/background.rs`) giải
  mã định nghĩa, kiểm tra namespace của nó, chạy `Tenant::admit` trên đó với store được để
  riêng (store là DSN của worker, lựa chọn của operator, không phải của client), lấy một
  trong các chỗ `max_runs` của tenant cho lần thử (không còn chỗ: job chờ, `Busy`), giới hạn
  kích thước pool ở `max_pool_connections`, và mở nó bằng các pool của tenant, không có host
  tool hay hook. Một namespace có root không phải tenant cũng không thuộc `local_namespaces`
  bị từ chối, và worker thậm chí không dequeue nó.
- **Principal.** `schedule.create` và `job.enqueue` nhận một `principal`, được
  `Tenant::admit_principal` kiểm tra như với `run.start`, và được lưu cùng lịch hoặc job; worker
  đưa nó cho mọi lần thử của run ([worker](./worker.md#principal)).
- **Secret.** Định nghĩa đã lưu giữ các secret của cấu hình (`to_value_with_secrets`, DSN bị
  làm trống); chỉ `linch_worker_agents` giữ chúng, các hàng khác gọi tên digest, và không câu
  trả lời hay lỗi nào mang chúng (`tests/schedule.rs`, một giá trị sentinel trong một giá trị
  env của MCP). Một định nghĩa không còn được gọi tên sẽ bị prune.
- **Cơ sở dữ liệu nào.** Các hàng đi tới pool riêng của agent đã mở, nên một lịch nằm cùng các
  run của agent của nó; một worker chỉ phục vụ DSN của phần của nó và mở các run của nó ở đó,
  nên một lần kích hoạt không bao giờ chạy trên cơ sở dữ liệu khác với nơi nó được lưu.

Worker nằm trong sidecar để một cấu hình duy nhất giữ các tenant, allowlist của họ và worker,
và các run nền dùng chung giới hạn run và pool của các tenant với các run tương tác. Một cấu
hình chỉ có phần `worker` là dạng tiến trình riêng: một server không lắng nghe gì và chỉ chạy
job, được mở rộng tách biệt với các server lắng nghe.

## Các giới hạn

| Giới hạn | Giá trị | Ở đâu | Vượt quá |
|---|---|---|---|
| `max_pool_connections` cho mỗi tenant | `linch_postgres::default_max_connections()` nếu không đặt (`allowlists.rs:325-328`) | `Tenant::admit` (`admit.rs:90-105`), trước khi bất cứ thứ gì kết nối | `-32004`, `kind: pool_connections`, `max` là mức trần |
| `max_connections` cho mỗi tenant | 8 nếu không đặt (`allowlists.rs:24`) | `Tenant::connect` (`access.rs:103-136`); kết nối giữ chỗ cho đến khi nó kết thúc (`connection.rs:161-180`) | Socket: `-32005` dưới id `connection.auth`; HTTP: `429` |
| `max_runs` cho mỗi tenant | 16 nếu không đặt (`allowlists.rs:17`) | `run_place`, lấy trong `start` và `claim` (`run.rs:152-157`) và được run đang điều khiển giữ | `-32005`, không có gì bắt đầu |
| `max_agents` cho mỗi tenant | 16 nếu không đặt (`allowlists.rs:29`) | Lấy trong `open` sau các kiểm tra (`agents.rs:20-27`), được giải phóng khi các MCP server của agent đã tắt (`server.rs:110-119`) | `-32005`, trước khi bất kỳ MCP server nào khởi động |
| Số lời gọi đang bay cho mỗi kết nối | 64 | `server.rs:51` | Việc đọc phải chờ |
| Số đăng nhập đang chờ | 32 cho mỗi `Tokens` | `access.rs:162-166` | `-32005` `kind: logins`, rồi đóng |
| Số HTTP upgrade đang chờ | 32 cho mỗi server | `http.rs:38-43` | Đóng mà không đọc |
| Một dòng hoặc một message | 16 MiB (`connection.rs:28`); 64 KiB đầu của nó được giữ để tìm `id` (`crates/linch-rpc/src/lines.rs:18`) | `Server::too_long` (`server.rs:242-261`) | `-32600`, dưới id của request khi phần đầu cho thấy nó; một câu trả lời làm request của server thất bại |
| Chờ đăng nhập, phần đầu HTTP | 10 s mỗi loại | `login.rs:17`, `http.rs:36` | Đăng nhập nhận `-32003`, rồi kết nối đóng; kết nối HTTP đóng |

Một kết nối cục bộ (stdio) không có giới hạn nào của tenant.

**Một run đã park giữ chỗ của nó.** Một run suspend để chờ phê duyệt trong khi
các subagent nền của nó đang chạy vẫn tiếp tục dùng các provider stream của server,
nên khi stream của nó kết thúc, `keep_place` (`run.rs:103-143`) chuyển chỗ run của nó
vào bảng `parked` của kết nối và chờ
`Agent::parked_children` ([họ run](./skills-subagents.md#họ-run)).
Chỗ được giải phóng khi các worker kết thúc; một approve hoặc resume của run trên
kết nối này tiếp quản nó thay vì lấy một chỗ mới (`run.rs:84-89`).
Dù cách nào, run chỉ giữ một chỗ, nên các run đang chạy của một tenant có tối đa
`max_runs × (1 + max_background)` provider stream.

## Các client

`Client` Rust (`client.rs:97-101`) có một method tổng quát,
`call<P, T>(method, params)` (`client.rs:192`), không có wrapper cho từng method.
`Client::connect_with` (`client.rs:141`) trả lời các request của server bằng một
`RequestHandler` (`client.rs:57`); `Client::in_process` (`client.rs:174`)
khởi động một server trên các pipe trong bộ nhớ, như các test làm.

`Connection` của client Python (`python/src/linch_rust/_rpc.py:189`) có
một task reader. Nó giải quyết các câu trả lời theo id, phân loại các envelope
`run.event` và `run.end` theo run của chúng (`_rpc.py:280-304`), chạy `tool.call` và
`hook.call` mỗi cái trong một task và hủy một cái khi có `$/cancelRequest`
(`_rpc.py:319-362`). `Agent` (`python/src/linch_rust/agent.py:50`) bọc
các method: `run`, `resume` và `approve` qua `_start` (`agent.py:203`),
và `close`, `close_session` và `events` (`agent.py:159-201`).

## Thêm một method RPC

| Bước | Ở đâu | Cái gì bắt lỗi nếu bỏ sót |
|---|---|---|
| 1. Đặt tên | Một hằng số `method::` trong `protocol.rs:28-66` | Không có gì; mọi bước khác đều dùng nó |
| 2. Params và kết quả của nó | `protocol.rs` hoặc một tệp `protocol/`, với `deny_unknown_fields` và doc comment | `-32602` cho một trường không biết chỉ khi kiểu đó từ chối chúng |
| 3. Handler | Một method `impl Server` trong `server/agents.rs` hoặc `server/run.rs`. Chạy các kiểm tra tenant (`admit_principal`, một chỗ) trước mọi thứ khác, và trả về `Reply::Run` cho một run | Test trong `crates/linch-rpc/tests/tenants.rs` và `limits.rs` cho các kiểm tra của nó |
| 4. Dispatch | Một nhánh trong `Server::dispatch` (`server.rs:304-322`) | `-32601` lúc chạy |
| 5. Lỗi | Một mã mới trong `error.rs` cùng phép chuyển đổi của nó | `crates/linch-rpc/tests/wire.rs` cho hình dạng trên wire |
| 6. Tài liệu giao thức | Method trong phần Methods của crate docs, một mã mới trong bảng của nó (`lib.rs:120-369`, `lib.rs:518-569`) | Chỉ review |
| 7. Client Rust | Không cần: `Client::call` là tổng quát. Một helper có kiểu trong `crates/linch-rpc/tests/common/mod.rs` nếu test cần | Biên dịch các test |
| 8. Client Python | Một method trên `Agent` (`agent.py`), và với một mã mới thì một exception trong `errors.py` cùng mục `_BY_CODE` của nó | `python/tests/test_protocol.py` |
| 9. Tài liệu sử dụng | `docs/usage/server.md` và các trang Python gọi nó | Chỉ review |
| 10. Test | `crates/linch-rpc/tests/<area>.rs` end to end qua `Client::in_process` hoặc wire thô (`wire.rs`); một test Python với server đã build | `cargo test -p linch-rpc`, `pytest` |

Một trường được thêm vào `AgentConfig` đến được `agent.open` mà không cần đổi gì ở đây;
`python/tests/test_config.py:53` kiểm tra rằng cấu hình có kiểu của Python nêu tên
mọi trường mà server đọc.

## Ví dụ

Đoạn này chạy với `linch-rpc` ở revision này: nó ghi các dòng thô vào một server
trong process, như một client stdio làm, và in cả hai chiều.

```rust
use linch::provider::scripted::TurnBuilder;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // The server end of an in-memory pipe, as `linch-server` serves stdio.
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (server_in, server_out) = tokio::io::split(theirs);
    tokio::spawn(linch_rpc::serve(server_in, server_out));
    let (reader, mut writer) = tokio::io::split(ours);
    let mut lines = BufReader::new(reader).lines();
    let mut send = async |message: Value| {
        println!("> {message}");
        let line = format!("{message}\n");
        writer.write_all(line.as_bytes()).await.unwrap();
    };

    let turns = [TurnBuilder::new("m").text("hello").build()];
    let config = json!({
        "namespace": "rpc",
        "spec": {"model": "m"},
        "provider": {"id": "scripted", "turns": turns},
    });
    send(json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open", "params": config})).await;
    let start = json!({"agent": "agent-1", "session_id": "s1", "prompt": "hi", "run_id": "r1"});
    send(json!({"jsonrpc": "2.0", "id": 2, "method": "run.start", "params": start})).await;
    let mut seen = Vec::new();
    while let Some(line) = lines.next_line().await.unwrap() {
        println!("< {line}");
        let message: Value = serde_json::from_str(&line).unwrap();
        let method = message["method"].as_str().unwrap_or("response").to_owned();
        seen.push(method.clone());
        if method == "run.end" {
            break;
        }
    }
    // The answer comes before the run's first notification.
    assert_eq!(seen[..2], ["response", "response"]);
    assert_eq!(seen.last().unwrap(), "run.end");

    // Handles are per connection; an unknown one is -32001.
    let stray = json!({"agent": "agent-9", "session_id": "s2", "prompt": "hi"});
    send(json!({"jsonrpc": "2.0", "id": 3, "method": "run.start", "params": stray})).await;
    let line = lines.next_line().await.unwrap().unwrap();
    println!("< {line}");
    let answer: Value = serde_json::from_str(&line).unwrap();
    assert_eq!((answer["id"].clone(), answer["error"]["code"].clone()), (json!(3), json!(-32001)));
}
```

Đầu ra của nó (`>` là gửi, `<` là nhận). Run được script đã sẵn sàng ngay, nên
mọi envelope của nó đã được xếp hàng trước khi writer chạy và đi ra trong một
`run.event`; seq 5 là một event `model_call_*` chỉ dành cho journal, được lưu nhưng không
được delivery:

```text
> {"jsonrpc":"2.0","id":1,"method":"agent.open","params":{"namespace":"rpc","spec":{"model":"m"},"provider":{"id":"scripted","turns":[{"events":[{"type":"message_start","model":"m"},{"type":"text_delta","text":"hello"},{"type":"message_end","stop_reason":"end_turn","usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0}}]}]}}}
> {"jsonrpc":"2.0","id":2,"method":"run.start","params":{"agent":"agent-1","session_id":"s1","prompt":"hi","run_id":"r1"}}
< {"jsonrpc":"2.0","id":1,"result":{"agent":"agent-1","contract":"a80570d2a77d5d1600656180319cb890b0a31a879948779ba862b3303bfec300"}}
< {"jsonrpc":"2.0","id":2,"result":{"run_id":"r1"}}
< {"jsonrpc":"2.0","method":"run.event","params":{"run_id":"r1","envelopes":[{"v":1,"seq":1,"run_id":"r1","ts_ms":1794009573573,"event":{"session_id":"s1","run_id":"r1","model":"m","provider":"scripted","tools":[],"type":"init"}},{"v":1,"seq":2,"run_id":"r1","ts_ms":1794009573573,"event":{"message":{"role":"user","content":[{"type":"text","text":"hi"}]},"subtype":"prompt","type":"user"}},{"v":1,"seq":3,"run_id":"r1","ts_ms":1794009573573,"event":{"model":"m","system_blocks":0,"messages":1,"selected_tools":[],"budget":{},"metadata":{},"type":"context_build"}},{"v":1,"seq":4,"run_id":"r1","ts_ms":1794009573573,"event":{"delta":{"kind":"text","text":"hello"},"type":"partial_assistant"}},{"v":1,"seq":6,"run_id":"r1","ts_ms":1794009573573,"event":{"message":{"role":"assistant","content":[{"type":"text","text":"hello"}]},"stop_reason":"end_turn","type":"assistant"}},{"v":1,"seq":7,"run_id":"r1","ts_ms":1794009573573,"event":{"usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"cumulative":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"actual_model":"m","type":"usage"}},{"v":1,"seq":8,"run_id":"r1","ts_ms":1794009573574,"event":{"subtype":"success","stop_reason":"end_turn","total_usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"duration_ms":0,"final_text":"hello","type":"result"}}]}}
< {"jsonrpc":"2.0","method":"run.end","params":{"run_id":"r1"}}
> {"jsonrpc":"2.0","id":3,"method":"run.start","params":{"agent":"agent-9","session_id":"s2","prompt":"hi"}}
< {"jsonrpc":"2.0","id":3,"error":{"code":-32001,"message":"no open agent \"agent-9\""}}
```

## Chỗ Rust khác Python

| Linch bản Python | linch-rust |
|---|---|
| Một thư viện trong process của host; không có gì đi qua một wire | Một sidecar mà client điều khiển qua JSON-RPC, hoặc cùng kernel đó được nhúng trong Rust |
| Host tool và hook là các callable Python | Tool và callback của một client từ xa là các request `tool.call` và `hook.call`, mỗi cái có timeout và, với callback, một fail mode |
| Process của host được tin cậy | stdio được tin cậy; socket và HTTP hoạt động thay cho một tenant, với token, allowlist và giới hạn |
| Mỗi agent tự kết nối store của nó | Các agent trên cùng một đích Postgres dùng chung một pool giữa các kết nối của một server, theo từng tenant |
| `reload_skills()` và `reload_subagents()` trên agent | Không phải method RPC ở revision này; lõi và facade có chúng ([reload](./skills-subagents.md#catalog-và-reload)) |

## Test

| Test | Cố định điều gì |
|---|---|
| `crates/linch-rpc/tests/wire.rs` | Framing và độ nghiêm ngặt, mọi lỗi JSON-RPC trên wire thô |
| `crates/linch-rpc/tests/runs.rs`, `approve.rs`, `cancel.rs`, `sessions.rs`, `close.rs`, `skipped.rs` | Các method end to end |
| `crates/linch-rpc/tests/host_tools.rs`, `callbacks.rs`, `permissions.rs`, `requests.rs`, `mcp.rs` | Các request của server, timeout và lệnh hủy của chúng, các MCP server |
| `crates/linch-rpc/tests/login.rs`, `tenants.rs`, `allowlists.rs`, `limits.rs`, `client_limits.rs` | Đăng nhập, admission, mọi giới hạn và backpressure ở cả hai phía |
| `crates/linch-rpc/tests/pools.rs`, `shutdown.rs` | Các pool Postgres dùng chung; việc hết input |
| `crates/linch-server/tests/stdio.rs`, `socket.rs`, `http.rs`, `listen.rs` | Binary và các transport của nó |
| `python/tests/test_protocol.py`, `python/tests/test_config.py` | Việc đọc wire của client Python, ánh xạ lỗi và các trường cấu hình |

---

Quay lại [mục lục kiến trúc](./README.md).
