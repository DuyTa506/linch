# MCP client

[Bản gốc tiếng Anh](../../architecture/mcp.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

`linch-mcp` (`crates/linch-mcp/src/`) kết nối tới các server Model Context Protocol
và đăng ký tool của chúng vào một `ToolRegistry`. Một `McpServer` là một kết nối tới một
server, kèm một supervisor chạy nền giữ registry đồng bộ với nó. Dùng trang này khi bạn
đổi cách xử lý kết nối, cách ánh xạ tool, hoặc một giới hạn.

Linch bản Python mô tả các tool MCP của nó cho người dùng ở
[usage/tools](../../../../docs/usage/tools.md#mcp-tools). Client Rust được xây trên
`rmcp`, SDK Rust chính thức, và nói giao thức 2026-07-28 với phương án dự phòng là
handshake `initialize` của 2025-11-25. Transport HTTP+SSE, resource,
prompt, sampling, elicitation và OAuth không được hỗ trợ
(`crates/linch-mcp/src/lib.rs:22-24`). Crate này chỉ dành cho Unix: một server stdio
dẫn đầu process group của riêng nó.

## Cấu hình

`McpServerConfig` (`crates/linch-mcp/src/config.rs:179`):

| Trường | Mặc định (`new`) | Ý nghĩa |
|---|---|---|
| `name` | bắt buộc | `<server>` trong `mcp__<server>__<tool>`. Chữ cái ASCII, chữ số, `-` và một dấu `_` đơn giữa chúng, để tên tool tách ra gọn gàng (`config.rs:225`) |
| `transport` | bắt buộc | `Stdio { command, args, env }`, `Http { url, headers }` hoặc `Custom(Arc<dyn Connector>)` |
| `trust_annotations` | `false` | Có cấp scope `read` cho tool có `readOnlyHint` hay không |
| `call_timeout` | 300 s | Thời gian chờ tối đa cho một lời gọi tool trước khi nó bị hủy |
| `reconnect` | `ReconnectPolicy::default()` | 5 lần thử, 0.5 s tăng gấp đôi đến 30 s, 30 s cho mỗi lần thử (`config.rs:165-175`) |

`validate()` (`config.rs:208`) từ chối một tên sai và timeout bằng 0 cho lời gọi hoặc
lần thử; header được kiểm tra khi transport được dựng. `Custom` nhận một
`Connector` (`crates/linch-mcp/src/connector.rs:140`) cho các server trong process
và test: nó mở một transport và chạy `Handler::serve` trên đó.

Transport HTTP dùng một client `reqwest` riêng của crate
(`connector.rs`, `http_client`), dựng một lần cho mỗi lần thử kết nối. Nó không bao giờ
đi theo redirect: một phản hồi 3xx kết thúc yêu cầu đó như một lỗi HTTP, nên header của
transport và session id ở lại trên URL đã cấu hình (client mặc định của rmcp đi theo
307 và 308 cùng origin, sẽ đưa chúng tới một đường dẫn khác của origin; các adapter
provider từ chối mọi redirect theo cùng cách). Lỗi nói rõ điều đó: `unexpected server
response: HTTP 307 Temporary Redirect: ... (redirects are not followed; configure the
final URL)`. Một gateway redirect bằng 307 hoặc 308 cùng origin, trước đây vẫn chạy được,
giờ làm kết nối thất bại cho đến khi cấu hình ghi URL cuối cùng. Nó không giữ connection nhàn rỗi, giống
client của rmcp, nên mỗi lời gọi mở một connection: trên loopback, 50 lời gọi tuần tự mất
khoảng 0,36 ms mỗi lời gọi khi không pool và 0,26 ms khi có pool, nhưng 2 trên 9 lượt chạy
50 lời gọi có pool có một lời gọi mất 31 đến 48 ms (cú đứng do TCP delayed ACK mà ghi chú
của rmcp nhắc tới, sau một response body chưa được đọc hết) so với 0 trên 9 khi không pool,
nên pool vẫn tắt. Biến môi trường proxy (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) áp dụng như
client mặc định của reqwest, giống trước đây.

## Ánh xạ tool

Khi kết nối, và ở mỗi lần relist, mỗi tool được liệt kê trở thành một đăng ký
(`crates/linch-mcp/src/tool.rs:146-174`):

- **Tên.** `tool_name(server, tool)` (`tool.rs:36`) là
  `mcp__<server>__<tool>`, với mọi ký tự của tên tool nằm ngoài
  `[A-Za-z0-9_-]` được thay bằng `_` (MCP cho phép `.`, provider thì không). Registry
  từ chối kết quả dài hơn 64 ký tự.
- **Scope.** `exec`, hoặc `read` cho tool có `readOnlyHint` là true khi
  cấu hình đặt `trust_annotations`. Một tool `exec` xin phê duyệt ở chế độ quyền
  mặc định và không bao giờ chạy song song.
- **Schema.** `inputSchema` của server; thiếu `type` hoặc `properties` thì
  mặc định là một schema đối tượng rỗng.
- **Kết quả** (`crates/linch-mcp/src/result.rs:44`). Các block văn bản được nối bằng
  ký tự xuống dòng thành giá trị đầu ra. Ảnh đi vào `metadata.images` (tối đa 8 ảnh và
  tổng cộng 4 MiB dữ liệu base64 cùng MIME type, mỗi MIME type tối đa 255 byte;
  ảnh vượt một giới hạn bị bỏ và văn bản ghi giới hạn nào:
  `[image omitted: more than 8 images]`,
  `[image omitted: over 4 MiB of image data]` hoặc
  `[image omitted: MIME type over 255 bytes]`; một ảnh sau đó vẫn vừa thì vẫn được giữ)
  và để lại `[image]` trong văn bản; âm thanh, resource nhị phân và các block khác để lại
  một placeholder. Khi không có block nào, dùng `structuredContent`, nếu không thì
  `(no content)`. Một kết quả `isError` trở thành `ToolOutputError` với mã
  `mcp_tool_error` và cùng văn bản.

Một tool mà registry từ chối (tên quá dài, tên đã bị chiếm, schema không hợp lệ) bị
bỏ qua và được báo bởi `McpServer::skipped()`; nó không làm kết nối thất bại
(`crates/linch-mcp/src/catalog.rs:10-18`).

### Mã lỗi

Mọi lỗi là một `ToolOutputError` mà model đọc được; mã dành cho host
(`crates/linch-mcp/src/codes.rs`):

| Mã | Khi nào |
|---|---|
| `aborted` | `ToolContext::cancel` đã kích hoạt |
| `timeout` | Không có kết quả trong `call_timeout` |
| `mcp_unavailable` | Đang kết nối lại, không khả dụng hoặc đã ngắt kết nối; kết nối bị rớt trong lúc gọi; hoặc không gửi được request (một mã trạng thái lỗi HTTP trên POST của nó) |
| `mcp_tool_error` | Server đã chạy tool và trả về `isError: true` |
| `mcp_protocol_error` | Một lỗi JSON-RPC, hoặc một response mà client này không hỗ trợ (`input_required`, một task) |

Văn bản của một lỗi JSON-RPC là `MCP server "<tên>" rejected the call (code N):
<thông điệp>`; mã nay đứng trước thông điệp, nên một thông điệp bị cắt ở 30 000
byte vẫn giữ mã.

## Vòng đời

`McpServer::connect(config, registry)` (`crates/linch-mcp/src/server.rs:99`)
xác thực cấu hình, thực hiện một lần thử (kết nối, subscribe, liệt kê) trong
`attempt_timeout`, chuyển hướng các lời gọi tới kết nối mới, rồi đăng ký
các tool, nên một tool không bao giờ hiển thị trước khi nó có thể được gọi (`server.rs:361`).
Lần thử đầu tiên thất bại là `McpError::Connect` và không đăng ký gì; nó không
được thử lại. `ConnectError` của nó là một trong `Spawn`, `Handshake`, `Listen`,
`ListTools`, `ListTooLarge` (tool đầu tiên của lần liệt kê vượt 4 MiB), `Timeout` và
`HttpClient` (không dựng được client HTTP); hai biến thể `ListTooLarge` và `HttpClient`
là mới, nên một `match` đầy đủ trên `ConnectError` cần thêm hai nhánh.

`ServerStatus` (`server.rs:44`): `Connected`, `Reconnecting`, `Unavailable`
(mọi lần thử kết nối lại đều thất bại; các tool vẫn được đăng ký và thất bại với
`mcp_unavailable`) và `Closed`. `status()` đọc nó; `watch_status()`
trả về một `watch::Receiver`.

### Relist

Supervisor liệt kê lại các tool khi có `notifications/tools/list_changed`
và sau mỗi lần kết nối lại (`server.rs:236-273`). Từ giao thức 2026-07-28, thông báo đó
chỉ đến trên một stream `subscriptions/listen`, mà client mở trước khi liệt kê, nên không
bỏ lỡ thay đổi nào ở giữa
(`crates/linch-mcp/src/listener.rs:65`, `server.rs:388`); các server cũ hơn gửi nó
không cần yêu cầu tới `Handler`. Một subscription mà server kết thúc bình thường
được gia hạn sau 1 s.

Phần diff của relist chỉ chạm vào những gì đã thay đổi (`catalog.rs:33-72`):

- một tool không đổi giữ nguyên đăng ký của nó;
- một tool đã đổi được thay thế trước và đăng ký cũ của nó được dispose sau,
  để tên không bao giờ rời khỏi registry và giữ vị trí của nó (prompt
  cache phụ thuộc vào thứ tự);
- một tool mà server không còn liệt kê thì bị dispose.

Relist bị giới hạn tốc độ (`server.rs`, `MIN_RELIST_INTERVAL`): thông báo đầu tiên sau
một quãng yên lặng relist ngay, còn thông báo đến trong vòng 1 s kể từ lúc relist
trước bắt đầu, hoặc trong lúc một relist đang chạy, thì chờ hết giây đó; các thông báo
đến trong lúc chờ dùng chung một relist tiếp theo, nên một server gửi dồn dập tốn tối đa
một lần liệt kê mỗi giây (kết nối lại vẫn relist ngay). Một lần liệt kê bị cắt ở 1000 tool
(`MAX_TOOLS`) hoặc 4 MiB tool đúng như server gửi (`MAX_LIST_BYTES`: JSON của từng tool,
nên title, output schema, icons và `_meta` được tính cùng tên, mô tả và input schema):
các tool trước giới hạn được giữ theo thứ tự, phần còn lại bị bỏ, không hỏi thêm trang
nào, và một cảnh báo `tracing` (target `linch_mcp`, kèm tên server, số tool giữ lại và số
byte của chúng) báo điều đó. Nếu chỉ tool đầu tiên đã vượt 4 MiB thì lần liệt kê bị từ chối.

Một lần relist thất bại, bị từ chối hoặc hết thời gian giữ nguyên các tool hiện tại; một
transport đóng trong lúc relist sẽ bắt đầu một lần kết nối lại.

### Kết nối lại

Một kết nối được coi là mất khi transport đóng, hoặc với một server stdio
khi một lần ghi thất bại, vì stdin của nó đã mất (`tool.rs:94-100`). Trạng thái
chuyển thành `Reconnecting`, các lời gọi thất bại với `mcp_unavailable`, và supervisor
thử lại theo `ReconnectPolicy`: lần thử `n` chờ
`min(max_delay, initial_delay * 2^n)` (`config.rs:157`). Mỗi lần thử
kết nối lại, subscribe lại và relist. Sau lần thất bại cuối cùng, trạng thái là
`Unavailable` cho đến khi server bị ngắt kết nối. `attempts: 0` không bao giờ kết nối lại.

### Hủy

Một lời gọi kết thúc mà không có kết quả của server sẽ bị hủy trên server
(`crates/linch-mcp/src/codes.rs:5-10`): khi có `ToolContext::cancel`, khi hết
`call_timeout`, và khi bên gọi drop future của lời gọi (timeout lời gọi của
scheduler). Guard `Abandon` (`tool.rs:279-310`) gửi
`notifications/cancelled` trong một task nền, để một server đã ngừng
đọc không thể giữ lời gọi khỏi trả về; trên streamable HTTP, rmcp đóng stream của
request thay vào đó. Client áp dụng timeout riêng của nó thay vì của
rmcp, vì timeout của rmcp chờ việc gửi lệnh hủy (`tool.rs:228-229`).

### Đóng

`disconnect()` (`server.rs:171`) dispose mọi đăng ký cùng lúc, rồi
chờ transport đóng. Drop handle mà không gọi nó sẽ dispose các đăng ký
ngay lập tức và đóng server trong nền
(`server.rs:186-190`). Một lần sync chạy đua với việc đóng sẽ không đăng ký gì: nó kiểm
tra close token dưới khóa catalog (`server.rs:376-381`).

Với một server stdio:

- **Process group.** Server dẫn đầu group của riêng nó, nên những gì nó khởi động
  (`npx`, `uvx`, `sh -c`) đi theo nó (`connector.rs:184-187`).
- **Kill group.** rmcp chỉ gửi tín hiệu cho group khi server sống lâu hơn thời gian ân hạn
  của nó. `KillGroup` gửi `SIGKILL` cho cả group ngay khi server đã thoát,
  nên một server thoát khi stdin gặp EOF không để lại tiến trình con nào
  (`connector.rs:220-262`).
- **Lifeline.** Mỗi server có một watchdog `/bin/sh` nhỏ đọc stdin của chính nó,
  một pipe mà chỉ client giữ đầu ghi, và kill group của server khi nó gặp EOF.
  Kernel đóng pipe đó khi client chết, kể cả bằng `SIGKILL`, nên một server bỏ
  qua EOF của stdin và `SIGTERM` không sống lâu hơn client, như lifeline của tool
  `Bash`. Watchdog đứng ngoài group của server, chỉ dùng builtin, và bị kill khi
  kết nối bị drop (`connector.rs`, `start_lifeline`).
- **Đóng có giới hạn.** Việc đóng của chính rmcp mất tối đa khoảng 6 s (3 s sau khi
  stdin đóng, 3 s nữa sau `SIGTERM`). Khi server đã ngừng đọc và một lần ghi đã làm
  đầy stdin của nó, việc đóng đó sẽ chờ mãi; `BoundedClose` bỏ cuộc sau 10 s và drop
  transport sẽ gửi `SIGKILL` cho group (`connector.rs:39-42`, `connector.rs:193-218`).

## Các giới hạn

Những gì một server có thể khiến client giữ hoặc chờ
(`crates/linch-mcp/tests/limits.rs`):

| Giới hạn | Giá trị | Ở đâu |
|---|---|---|
| Số trang `tools/list` cho mỗi lần liệt kê | 100 | `server.rs:23` |
| Số tool giữ lại từ một lần liệt kê | 1000; phần còn lại bị bỏ, kèm một cảnh báo `tracing` | `server.rs:26` |
| Số byte giữ lại từ một lần liệt kê (từng tool đúng như server gửi, dạng JSON) | 4 MiB; các tool trước giới hạn được giữ theo thứ tự, phần còn lại bị bỏ, kèm một cảnh báo `tracing`; chỉ tool đầu tiên đã vượt thì kết nối thất bại với `ConnectError::ListTooLarge`, và một relist khi đó giữ nguyên các tool hiện có | `server.rs` (`MAX_LIST_BYTES`) |
| Thời gian giữa hai relist do `list_changed` | 1 s, các thông báo ở giữa dùng chung một relist | `server.rs` (`MIN_RELIST_INTERVAL`) |
| Một lần thử kết nối, subscribe lại hoặc relist | `attempt_timeout` (30 s) | `server.rs:259`, `server.rs:329` |
| Một lời gọi tool | `call_timeout` (300 s) | `tool.rs:264` |
| Số lệnh hủy đang chờ trên một kết nối | 64; các lệnh sau đó bị bỏ | `tool.rs:29` |
| Văn bản kết quả, và văn bản lỗi của tool (kể cả thông điệp của một lỗi JSON-RPC) | 30 000 byte, cắt ở ranh giới ký tự | `result.rs:10`, `tool.rs` (`coded`) |
| Số ảnh giữ lại từ một kết quả | 8 ảnh và tổng cộng 4 MiB dữ liệu base64 cùng MIME type; MIME type tối đa 255 byte; ảnh vượt một giới hạn bị bỏ, kèm ghi chú nêu giới hạn đó | `result.rs` (`MAX_IMAGES`, `MAX_IMAGE_BYTES`) |
| Tóm tắt đối số của tool | 80 ký tự | `tool.rs:23` |
| Đóng stdio | 10 s, rồi `SIGKILL` | `connector.rs:42` |

## Che dữ liệu nhạy cảm

- **Môi trường.** Một server stdio chỉ kế thừa `HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM` và `USER` từ host, cùng danh sách mà MCP Python SDK
  dùng; mọi thứ khác đến từ `env` của cấu hình
  (`connector.rs:35-37`, `connector.rs:176-183`).
- **Đầu ra debug.** `Debug` của `McpTransport` in tên của biến môi trường và header,
  không bao giờ in giá trị của chúng (`config.rs:89-135`), và hiện URL HTTP với
  user name, mật khẩu và query được thay bằng `redacted` và bỏ fragment (một chuỗi
  không phải URL, hoặc không có host, như `user:token@host:8443/mcp` vốn được parse
  thành scheme `user`, hiện là `[redacted]`), nên ghi log một cấu hình không làm lộ token.
- **Lỗi.** reqwest ghi URL trong văn bản của một yêu cầu thất bại, và user name, mật khẩu
  hay query của URL đó thường chứa token. Transport HTTP bọc client
  (`connector.rs`, `QuietClient`) để mọi lỗi nó phát ra đều bỏ URL
  (`reqwest::Error::without_url`); điều này đúng với một lần kết nối thất bại, một lần
  kết nối lại và văn bản của một lời gọi tool thất bại. Thông điệp lỗi JSON-RPC trong
  `ConnectError::Handshake`, `Listen` hoặc `ListTools` bị cắt ở 30 000 byte như lỗi của
  tool, và các biến thể này không có `source`, nên thông điệp không bị lặp đầy đủ dọc
  chuỗi lỗi.

## Ví dụ

Đoạn này biên dịch được ở revision này; `main` chạy các kiểm tra offline, còn
`session` cần một server đang chạy:

```rust
use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use linch_core::ToolRegistry;
use linch_mcp::{McpError, McpServer, McpServerConfig, McpTransport, ServerStatus, tool_name};

/// A stdio server whose read-only tools may run in parallel.
fn files_server() -> McpServerConfig {
    let args = ["--root".to_owned(), "/srv/data".to_owned()];
    let mut config = McpServerConfig::new("files", McpTransport::stdio("files-mcp", args));
    config.trust_annotations = true;
    config.call_timeout = Duration::from_secs(60);
    config
}

/// A streamable HTTP server with a bearer token from the host's secrets.
fn search_server(token: &str) -> McpServerConfig {
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_owned(), format!("Bearer {token}"));
    let url = "https://mcp.example/mcp".to_owned();
    McpServerConfig::new("search", McpTransport::Http { url, headers })
}

/// Connect, report what the registry refused, and disconnect.
async fn session(tools: Arc<ToolRegistry>) -> Result<(), McpError> {
    let server = McpServer::connect(files_server(), Arc::clone(&tools)).await?;
    for skipped in server.skipped() {
        eprintln!("{} left out: {}", skipped.remote_name, skipped.reason);
    }
    if server.status() == ServerStatus::Connected {
        println!("tools: {:?}", server.tools());
    }
    server.disconnect().await; // disposes every registration first
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    assert_eq!(files_server().validate(), Ok(()));
    assert_eq!(search_server("t").validate(), Ok(()));
    assert!(!format!("{:?}", search_server("secret").transport).contains("secret"));
    assert_eq!(tool_name("files", "read.text"), "mcp__files__read_text");
    let _ = session; // needs a running server
}
```

## Test

- `tests/duplex.rs`: một server rmcp trong process qua một duplex pipe; scope,
  kết quả, relist, kết nối lại, hủy, timeout, drop và disconnect.
- `tests/stdio.rs`: một process con thật (`examples/stdio_server.rs`); thoát
  sạch, kill một server đã ngừng đọc, kill những gì nó đã khởi động.
- `tests/http.rs`: streamable HTTP trên 127.0.0.1, gồm cả trường hợp một lỗi HTTP trên
  một POST chỉ làm thất bại đúng lời gọi đó.
- `tests/limits.rs`: các giới hạn ở trên.

---

Quay lại [mục lục kiến trúc](./README.md).
