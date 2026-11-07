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
(`crates/linch-rpc/src/lib.rs:1-374`): framing, mọi method, request
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
| `crates/linch-rpc/src/server/peer.rs`, `server/host.rs`, `server/callback.rs` | Các request từ server: host tool (`tool.call`) và callback (`hook.call`) |
| `crates/linch-rpc/src/client.rs` | `Client` Rust |
| `crates/linch-server/src/main.rs` | Binary: stdio, hoặc các listener với `--config FILE` |
| `crates/linch-server/src/{config,listen,http}.rs` | Cấu hình, Unix socket và transport HTTP |
| `crates/linch/src/pools.rs` | `PgPools`: các pool Postgres theo đích; một server giữ một bộ cho mỗi tenant và một bộ cho các kết nối cục bộ của nó |

## Method và message

Mỗi tên method là một hằng số `method::` (`crates/linch-rpc/src/protocol.rs:25-55`),
và `Server::dispatch` (`crates/linch-rpc/src/server.rs:297-315`) ánh xạ từng tên
tới handler của nó. Các kiểu params từ chối trường không biết; `OpenParams` tách
`tools` và `callbacks` ra và đọc phần còn lại thành một `AgentConfig` nghiêm ngặt
(`crates/linch-rpc/src/protocol/open.rs:13-28`).

| Method | Params | Handler | Kết quả |
|---|---|---|---|
| `connection.auth` | `AuthParams` (`protocol.rs:62`) | `login` (`crates/linch-rpc/src/server/login.rs:24`), chỉ là message đầu tiên trên socket; ở nơi khác là `-32600` | `Authenticated` `{tenant}` |
| `agent.open` | `OpenParams` | `Server::open` (`crates/linch-rpc/src/server/agents.rs:19-48`) | `AgentOpened` `{agent, contract, skipped?}` |
| `agent.close` | `CloseParams` | `agents.rs:51-75`; `-32002` khi kết nối đang điều khiển một run của nó | `{}` sau khi các MCP server của nó đã tắt |
| `session.close` | `SessionCloseParams` | `agents.rs:76-85`: `Agent::close_session` | `{}` |
| `run.start` | `StartParams` (`protocol.rs:154`) | `Server::start` (`crates/linch-rpc/src/server/run.rs:20-43`) | `RunAccepted` `{run_id}`, rồi các notification |
| `run.resume` | `ResumeParams` (`protocol.rs:178`) | `run.rs:45-50`, qua `claim` | như trên |
| `run.approve` | `ApproveParams` (`protocol.rs:191`) | `run.rs:52-58`, qua `claim` | như trên |
| `run.cancel` | `CancelParams` | `run.rs:60-69`: hủy một run mà kết nối này điều khiển | `{cancelled}` |
| `run.events` | `EventsParams` | `agents.rs:87-94`: `Agent::events` | `{events}`, các envelope bền vững sau `after_seq` |

Server gửi hai notification. `run.event` `{run_id, envelopes}`
(`RunEvent`, `protocol.rs:256`) mang một hoặc nhiều envelope liên tiếp
của một run; `run.end` `{run_id, error?}` (`RunEnd`, `protocol.rs:266`) là
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
docs (`lib.rs:328-364`) và các mã nằm trong `crates/linch-rpc/src/error.rs:24-100`.

| Mã | Đến từ |
|---|---|
| -32700, -32600, -32601, -32602, -32603 | JSON-RPC: dòng, đối tượng request, method, params; một panic |
| -32001, -32002 | Một agent handle không biết; một run mà kết nối đã điều khiển, hoặc `agent.close` của một agent đang có một run |
| -32003 | Message đầu tiên của socket không phải `connection.auth` với một token đã biết |
| -32004 | Một kiểm tra tenant từ chối; `data.kind` nêu tên nó |
| -32005 | Đạt một giới hạn và chưa có gì bắt đầu; `data: {kind, max}`, `kind` là một trong `runs`, `agents`, `connections`, `logins` (`limit_reached`, `error.rs:126-131`) |
| -32010 đến -32015 | `agent.open`: `BuildError`, provider, store và MCP server không khả dụng |
| -32020 đến -32030 | Một run, trong `run.end`, và `run.events`: lỗi store, `RunError`, session bận |

`RunError::ContractMismatch {expected, found}` nêu tên các contract từ phía của agent:
`expected` là contract của agent đã cố tiếp tục run, `found` là contract được lưu cùng
run. Wire đảo chúng lại, để `data.expected` là contract của run và `data.found` là của
agent, đúng như văn bản thông báo (`error.rs:157-165`, được test ở
`error.rs:222-242`).

Phần đăng nhập của socket trả lời các lần từ chối dưới những id khác nhau
(`login.rs:33-85`): `-32005` `kind: logins` dưới id null trước khi đọc bất cứ thứ gì;
`-32003` dưới id của request, hoặc null khi message đầu tiên
không có id hoặc không có message nào đến trong thời gian chờ đăng nhập;
`-32005` `kind: connections` dưới id `connection.auth`.
Client Python đọc dòng đầu tiên nó nhận được làm câu trả lời cho lần đăng nhập của nó,
bất kể id (`python/src/linch_rust/_remote.py:112-142`).

Client Python ánh xạ mã sang exception trong `_BY_CODE`
(`python/src/linch_rust/errors.py:158-170`), với các dải cho phần còn lại
(`errors.py:173-186`); `python/tests/test_protocol.py:196` cố định ánh xạ này.

## Bên trong một kết nối

Mỗi kết nối là một `Server` (`server.rs:53-79`): các bảng agent đang mở,
run đang được điều khiển và run đang park dưới một khóa, để `agent.close` không thể
bỏ sót một run bắt đầu trong lúc nó kiểm tra; outbox của nó; peer của nó cho các
request từ server; và tenant của nó, là `None` đối với client cục bộ.

1. **Transport.** `serve_lines` (`crates/linch-rpc/src/server/connection.rs:84-109`)
   chia một luồng byte thành các dòng; `serve_messages` (`connection.rs:127-150`)
   nhận một message cho mỗi frame. Cả hai đưa các mục `Inbound` (`connection.rs:153-161`)
   cho `connection` (`connection.rs:163-221`), thứ spawn task writer,
   chạy phần đăng nhập khi access là `Login`, và đọc cho đến khi input kết thúc
   hoặc `stop` kích hoạt.
2. **Vòng lặp đọc.** `Server::receive` (`server.rs:174-210`) parse một dòng,
   đưa câu trả lời của client cho request của server mà nó nêu tên, và đọc
   mọi thứ khác như một request (`Request::from_value`,
   `crates/linch-rpc/src/message.rs:108`). Một request không có `id` là một
   notification và bị bỏ. Sau đó mỗi lời gọi lấy một trong 64 permit
   (`MAX_REQUESTS`, `server.rs:49`); vòng lặp await nó, nên request thứ 65
   dừng việc đọc cho đến khi một lời gọi kết thúc. Lời gọi chạy trong task riêng,
   nên một `agent.open` chậm không bao giờ giữ chân một `run.cancel`.
3. **Dispatch.** `Server::handle` (`server.rs:268-295`) bắt một panic thành
   `-32603`. Một method run trả về stream của nó: kết quả `RunAccepted`
   được xếp hàng trước, rồi một task điều khiển stream.
4. **Một run.** `Server::drive` (`run.rs:159-196`) mã hóa mỗi envelope một lần,
   thành một giá trị JSON thô, và xếp hàng nó. Khi stream kết thúc, nó drop stream,
   gỡ run khỏi các bảng (giữ chỗ của nó khi nó park, xem
   [các giới hạn](#các-giới-hạn)) và xếp hàng `run.end`. Một panic của run kết thúc nó với
   `-32603`, tại trạng thái bền vững cuối cùng của nó.
5. **Writer.** `write_messages` (`connection.rs:225-254`) lấy mọi thứ
   đã xếp hàng, gộp các envelope liên tiếp của một run thành một
   `run.event` (`Batch::push`, `crates/linch-rpc/src/server/outgoing.rs:74-83`)
   tối đa 64 KiB (`outgoing.rs:21`), và flush một lần khi không còn gì chờ.
6. **Request từ server.** `Peer::call` (`peer.rs:92-139`) xếp hàng request
   và chờ câu trả lời, timeout hoặc lệnh hủy của nó; thời gian chờ chỗ trống trong
   outbox được tính vào cả hai.
7. **Hết input.** `Server::stop` (`server.rs:153-172`) để các lời gọi
   đã đọc kết thúc, drop mọi run tại chỗ nó đang đứng (một run bền vững vẫn
   resume được), làm các request đang chờ của server thất bại, rồi đóng các MCP server
   của mọi agent. Writer xả hết hàng đợi và kết nối trả về.

### Thứ tự

- `RunAccepted` của một run được xếp hàng trước khi task điều khiển nó được
  spawn (`server.rs:281-292`), nên nó đến client trước mọi
  notification của run đó. Client Python dựa vào điều này: nó phân loại các
  notification của một run theo run id mà nó đọc từ kết quả đó
  (`python/src/linch_rust/_rpc.py:254-265`).
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
   (`NOTIFICATION_CAPACITY`, `python/src/linch_rust/_rpc.py:46`): mỗi
   `run.event` chưa đọc giữ một chỗ trên các run của kết nối, và
   reader chờ một chỗ trước khi xếp hàng cái kế tiếp (`RunQueue`,
   `_rpc.py:60-96`). `Run.cancel` tiếp tục đọc run của nó khi chờ
   câu trả lời (`python/src/linch_rust/run.py:102-121`); một run không ai đọc
   trong khi event của nó vẫn đến sẽ làm kết nối bị nghẽn.

`Outbox::send_now` (`connection.rs:269-279`), dành cho mã không thể chờ,
giao một message cho một task khi hàng đợi đầy.

## Transport

| Transport | Điểm vào | Access | Runtime |
|---|---|---|---|
| stdio | `linch-server` không có đối số; `linch_rpc::serve` (`connection.rs:43-57`) | `Access::Local`: không đăng nhập, không tenant, không giới hạn | Current-thread (`crates/linch-server/src/main.rs:49-55`): một client, nên chuyển một envelope giữa các task của nó không đánh thức thread nào khác |
| Unix socket | `--config FILE` với `socket`; `Listeners` (`crates/linch-server/src/listen.rs:42-101`) | `Access::Login`: message đầu tiên là `connection.auth` | Multi-thread |
| HTTP với WebSocket | `--config FILE` với `http`; `crates/linch-server/src/http.rs:77-116` | `Access::Tenant`: header bearer được kiểm tra trước khi upgrade | Multi-thread |

**stdio.** Trên Linux, server mở lại các pipe của nó qua
`/proc/self/fd/0` và `/proc/self/fd/1` và điều khiển chúng ở chế độ non-blocking
(`main.rs:88-123`). Non-blocking là một cờ của open file
description, thứ mà fd 0 và 1 dùng chung với mọi process giữ cùng
pipe; các description mới giữ cờ này không rò sang các reader khác của một shell hay
supervisor. Mọi thứ không phải pipe dùng `stdin()` và `stdout()` của tokio.

**Unix socket.** Một tệp socket do một server đã dừng để lại sẽ bị thay thế, và
một tệp mà một server đang trả lời trên đó thì bị từ chối (`bind_socket`, `listen.rs:105-122`).
Mỗi kết nối chờ đăng nhập của nó với một trong 32 chỗ
(`MAX_PENDING_LOGINS`, `crates/linch-rpc/src/access.rs:135-139`): mỗi chỗ có thể
đệm một dòng 16 MiB trong 10 s, nên các chỗ này giới hạn mức đó ở 512 MiB. Kết nối thứ
33 nhận `-32005` ngay. Một lần đăng nhập có 10 s (`LOGIN_TIMEOUT`,
`login.rs:17`); im lặng quá mức đó nhận `-32003` và kết nối đóng
(`login.rs:42-54`).

**HTTP.** Một route, `GET /rpc` (`http.rs:30`). Trước khi upgrade, một
kết nối giữ một trong 32 chỗ trên toàn server
(`MAX_PENDING_UPGRADES`, `http.rs:39-44`); kết nối thứ 33 bị đóng mà không đọc. Mỗi
phần đầu request có 10 s (`HEADER_READ_TIMEOUT`, `http.rs:32-37`), sau đó
kết nối đóng mà không trả lời. `upgrade` (`http.rs:135-171`) kiểm tra
bearer token trước (`401` với `WWW-Authenticate: Bearer`), rồi lấy
chỗ kết nối của tenant (`429`), rồi trả lời `101` nêu tên
tenant trong `Linch-Tenant`. Một message WebSocket có thể dài 64 MiB
(`MAX_MESSAGE_BYTES`, `http.rs:50-53`) trước khi socket đóng; một message trên
16 MiB bị lõi từ chối như một dòng quá khổ. Server nói HTTP thuần:
hãy đặt TLS phía trước nó hoặc listen trên loopback.

Mọi listener của một server dùng chung một `PgPools` (`listen.rs:77-101`) cho các client
cục bộ; các kết nối của một tenant dùng `PgPools` riêng của tenant (bên dưới).

## Tenant và allowlist

`ServerConfig::from_json` (`crates/linch-server/src/config.rs:153`) đọc
cấu hình, từ chối các trường không biết và giới hạn bằng 0; `tokens`
(`config.rs:187`) đọc từng token trực tiếp hoặc từ môi trường;
`AllowConfig::build` (`config.rs:220`) tạo các `Allowlists`.

- Một `Tenant` (`access.rs:41-125`) có tên gồm 1 đến 64 chữ cái ASCII,
  chữ số, `.`, `_` hoặc `-` (không bao giờ có `/`), các allowlist của nó, và một semaphore
  cho mỗi loại: run, agent và connection, dùng chung giữa mọi kết nối của nó.
- `Tokens` (`access.rs:141-206`) giữ mỗi token dưới dạng digest SHA-256 và
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
| Một DSN Postgres chỉ cho một đích được phép | `postgres` | `admit.rs:79-89` |
| `stores.max_connections` tối đa bằng `max_pool_connections` của tenant (mặc định: kích thước pool mặc định của server); khi không có, nó được đặt thành mặc định hoặc mức trần, tùy giá trị nào nhỏ hơn | `pool_connections`, với `data.max` là mức trần | `admit.rs:90-105` |
| Một MCP stdio server chỉ với `command` và `args` của một mục, chỉ đặt các tên `env` mà mục đó liệt kê; một MCP URL chỉ ở một origin được phép | `mcp_command`, `mcp_env`, `mcp_url` | `admit.rs:107-134` |

`principal` của một method run, khi được đưa ra, phải nêu tên tenant
(`admit_principal`, `admit.rs:138-152`); server không tự điền nó.

**Postgres.** `PgTarget::parse` (`crates/linch-rpc/src/access/allowlists.rs:137-174`)
đọc một DSN theo cách store kết nối với nó, qua `PgConnectOptions` của sqlx,
nên một ghi đè `?host=` hay `?user=` được tính và một user hay `sslmode` bị thiếu lấy
mặc định của server. Một mục cho phép một DSN có cùng host, port, socket, database, user,
`options` khởi động và tệp TLS, và một `sslmode` ít nhất nghiêm ngặt bằng của nó, xếp
hạng từ `disable` (0) đến `verify-full` (5) (`PgTarget::allows`, `allowlists.rs:176-197`).

Facade dùng chung một pool cho mỗi đích giữa các agent được mở với cùng một
`PgPools` (`crates/linch/src/pools.rs:19-39`). Một `Tenant` sở hữu một cái
(`access.rs:54`, `access.rs:77`) và `Server::new` đưa cho kết nối của tenant cái đó
thay vì cái của process (`server.rs:136`), nên các pool được khóa theo tenant và đích:
hai tenant trên cùng một đích không bao giờ dùng chung pool, và một tenant không thể
đặt kích thước pool của tenant khác (R3 front 5). Mọi kết nối của một tenant dùng chung
các pool của nó, nên `max_pool_connections` giới hạn các kết nối của tenant trên mỗi
đích. Các kết nối cục bộ (stdio) giữ các pool của process. Khóa của nó (`pools.rs:45-74`) là URL đã chuẩn hóa
của sqlx cho DSN cộng với `options` khởi động của nó, thứ mà URL đó bỏ
đi và có thể đặt `search_path`; các DSN chỉ khác nhau về thứ tự tham số hoặc về
các cài đặt mà sqlx bỏ qua sẽ dùng chung một pool. Một pool lấy kích thước từ
lần open đã kết nối nó (`pools.rs:94-112`); việc kết nối bỏ cuộc sau
5 s và các migration sau 30 s (`pools.rs:146-164`).

**Lệnh MCP.** Một `McpCommand` (`allowlists.rs:54-74`) không được liệt kê
`PATH`, `HOME` hay các biến được biết là chọn mã nào mà một chương trình nạp
(`CODE_LOADING_ENV` và các tiền tố của nó, `allowlists.rs:76-113`). Danh sách này là
nỗ lực tối đa (best effort).

## Các giới hạn

| Giới hạn | Giá trị | Ở đâu | Vượt quá |
|---|---|---|---|
| `max_pool_connections` cho mỗi tenant | `linch_postgres::default_max_connections()` nếu không đặt (`allowlists.rs:275-278`) | `Tenant::admit` (`admit.rs:90-105`), trước khi bất cứ thứ gì kết nối | `-32004`, `kind: pool_connections`, `max` là mức trần |
| `max_connections` cho mỗi tenant | 8 nếu không đặt (`allowlists.rs:24`) | `Tenant::connect` (`access.rs:92-109`); kết nối giữ chỗ cho đến khi nó kết thúc (`connection.rs:179-190`) | Socket: `-32005` dưới id `connection.auth`; HTTP: `429` |
| `max_runs` cho mỗi tenant | 16 nếu không đặt (`allowlists.rs:17`) | `run_place`, lấy trong `start` và `claim` (`run.rs:152-157`) và được run đang điều khiển giữ | `-32005`, không có gì bắt đầu |
| `max_agents` cho mỗi tenant | 16 nếu không đặt (`allowlists.rs:29`) | Lấy trong `open` sau các kiểm tra (`agents.rs:19-26`), được giải phóng khi các MCP server của agent đã tắt (`server.rs:104-111`) | `-32005`, trước khi bất kỳ MCP server nào khởi động |
| Số lời gọi đang bay cho mỗi kết nối | 64 | `server.rs:49` | Việc đọc phải chờ |
| Số đăng nhập đang chờ | 32 cho mỗi `Tokens` | `access.rs:135-139` | `-32005` `kind: logins`, rồi đóng |
| Số HTTP upgrade đang chờ | 32 cho mỗi server | `http.rs:39-44` | Đóng mà không đọc |
| Một dòng hoặc một message | 16 MiB (`connection.rs:28`); 64 KiB đầu của nó được giữ để tìm `id` (`crates/linch-rpc/src/lines.rs:18`) | `Server::too_long` (`server.rs:235-254`) | `-32600`, dưới id của request khi phần đầu cho thấy nó; một câu trả lời làm request của server thất bại |
| Chờ đăng nhập, phần đầu HTTP | 10 s mỗi loại | `login.rs:17`, `http.rs:37` | Đăng nhập nhận `-32003`, rồi kết nối đóng; kết nối HTTP đóng |

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

`Connection` của client Python (`python/src/linch_rust/_rpc.py:160`) có
một task reader. Nó giải quyết các câu trả lời theo id, phân loại các envelope
`run.event` và `run.end` theo run của chúng (`_rpc.py:232-252`), chạy `tool.call` và
`hook.call` mỗi cái trong một task và hủy một cái khi có `$/cancelRequest`
(`_rpc.py:267-310`). `Agent` (`python/src/linch_rust/agent.py:50`) bọc
các method: `run`, `resume` và `approve` qua `_start` (`agent.py:203`),
và `close`, `close_session` và `events` (`agent.py:159-201`).

## Thêm một method RPC

| Bước | Ở đâu | Cái gì bắt lỗi nếu bỏ sót |
|---|---|---|
| 1. Đặt tên | Một hằng số `method::` trong `protocol.rs:25-55` | Không có gì; mọi bước khác đều dùng nó |
| 2. Params và kết quả của nó | `protocol.rs` hoặc một tệp `protocol/`, với `deny_unknown_fields` và doc comment | `-32602` cho một trường không biết chỉ khi kiểu đó từ chối chúng |
| 3. Handler | Một method `impl Server` trong `server/agents.rs` hoặc `server/run.rs`. Chạy các kiểm tra tenant (`admit_principal`, một chỗ) trước mọi thứ khác, và trả về `Reply::Run` cho một run | Test trong `crates/linch-rpc/tests/tenants.rs` và `limits.rs` cho các kiểm tra của nó |
| 4. Dispatch | Một nhánh trong `Server::dispatch` (`server.rs:297-315`) | `-32601` lúc chạy |
| 5. Lỗi | Một mã mới trong `error.rs` cùng phép chuyển đổi của nó | `crates/linch-rpc/tests/wire.rs` cho hình dạng trên wire |
| 6. Tài liệu giao thức | Method trong phần Methods của crate docs, một mã mới trong bảng của nó (`lib.rs:118-246`, `lib.rs:328-364`) | Chỉ review |
| 7. Client Rust | Không cần: `Client::call` là tổng quát. Một helper có kiểu trong `crates/linch-rpc/tests/common/mod.rs` nếu test cần | Biên dịch các test |
| 8. Client Python | Một method trên `Agent` (`agent.py`), và với một mã mới thì một exception trong `errors.py` cùng mục `_BY_CODE` của nó | `python/tests/test_protocol.py` |
| 9. Tài liệu sử dụng | `docs/usage/server.md` và các trang Python gọi nó | Chỉ review |
| 10. Test | `crates/linch-rpc/tests/<area>.rs` end to end qua `Client::in_process` hoặc wire thô (`wire.rs`); một test Python với server đã build | `cargo test -p linch-rpc`, `pytest` |

Một trường được thêm vào `AgentConfig` đến được `agent.open` mà không cần đổi gì ở đây;
`python/tests/test_config.py:52` kiểm tra rằng cấu hình có kiểu của Python nêu tên
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
