# Chạy server cho client từ xa

[Bản gốc tiếng Anh](../../usage/server.md)

Dùng `linch-server --config` khi các client ở tiến trình khác hoặc máy khác cần dùng chung một sidecar, mỗi tenant có token, allowlist và giới hạn riêng.

[← Hướng dẫn sử dụng](./README.md)

Theo mặc định, `Server()` khởi động `linch-server` như một tiến trình con và giao tiếp
với nó qua stdin và stdout. Client đó sở hữu server, nên không có đăng nhập, không có
tenant và không có allowlist. Ngược lại, `linch-server --config FILE` lắng nghe trên
một Unix socket, trên HTTP kèm WebSocket, hoặc cả hai, cho nhiều client cùng lúc. Mỗi
kết nối đăng nhập bằng một bearer token và hành động thay mặt tenant của token đó, và
mọi `agent.open` đều phải qua các allowlist của tenant trước khi bất cứ thứ gì chạy.

Từ Python, `Server.connect(address, token=...)` kết nối tới một server như vậy và được
dùng giống `Server()` ([bên dưới](#kết-nối-từ-python)). Client bằng các ngôn ngữ khác
nói giao thức JSON-RPC được mô tả trong tài liệu của crate `linch-rpc`
(`crates/linch-rpc/src/lib.rs`); chương trình Rust có thể dùng `linch_rpc::Client`.

| | `linch-server` (stdio) | `linch-server --config FILE` |
|---|---|---|
| Do ai khởi động | `Server()` trong tiến trình của bạn | Bạn, dưới dạng một dịch vụ |
| Client | Client đã khởi động nó | Nhiều client: `Server.connect(...)`, qua socket hoặc WebSocket |
| Đăng nhập | Không có | `connection.auth` hoặc `Authorization: Bearer` |
| Allowlist và giới hạn | Không có | Theo từng tenant |
| Dừng | Khi stdin của nó đóng | Khi nhận SIGINT hoặc SIGTERM |

---

## Khởi động server lắng nghe

`/etc/linch/linch-server.json`:

```json
{
  "socket": "/run/linch/linch.sock",
  "http": "127.0.0.1:8731",
  "tenants": {
    "acme": {
      "tokens": [{"env": "LINCH_TOKEN_ACME"}],
      "allow": {
        "scripted_provider": true,
        "permission_modes": ["default", "accept_edits"],
        "workspace_roots": ["/srv/linch/acme"],
        "max_runs": 4,
        "max_connections": 2
      }
    },
    "ops": {
      "tokens": [{"env": "LINCH_TOKEN_OPS"}],
      "allow": {
        "exec_tools": true,
        "permission_modes": ["default", "accept_edits", "skip_dangerous"],
        "workspace_roots": ["/srv/linch/ops"],
        "provider_base_urls": ["http://vllm.internal:8000/v1"],
        "postgres_dsns": ["postgres://linch_ops@db.internal:5432/linch"],
        "mcp_commands": [
          {"command": "/usr/local/bin/git-mcp", "args": ["--read-only"], "env": ["GIT_TOKEN"]}
        ],
        "mcp_urls": ["https://mcp.internal"]
      }
    }
  }
}
```

```bash
export DEEPSEEK_API_KEY=...          # provider keys the agents may use
export LINCH_TOKEN_ACME=... LINCH_TOKEN_OPS=...
cd /var/lib/linch                    # a directory without a .linch folder
linch-server --config /etc/linch/linch-server.json
```

```text
linch-server: listening on unix:/run/linch/linch.sock
linch-server: listening on ws://127.0.0.1:8731/rpc
```

- Cấu hình được đọc một lần khi khởi động, và đọc nghiêm ngặt: một trường không xác
  định ở bất kỳ cấp nào, một biến token chưa được đặt, một workspace root không tồn
  tại hoặc một giới hạn bằng 0 sẽ dừng server với mã thoát 2 và lý do trên stderr.
  Mã thoát 1 nghĩa là lỗi I/O; 0 là dừng bình thường.
- Tệp socket do một server đã dừng để lại sẽ được thay thế, và tệp bị xóa khi tắt.
  Quyền của tệp theo umask.
- HTTP là HTTP thuần: hãy đặt TLS phía trước, hoặc chỉ lắng nghe trên loopback.
- Hãy chạy server từ một thư mục không có `.linch`. Agent của tenant có bật skill hoặc
  subagent mà không có workspace sẽ phân giải `.linch` mặc định theo thư mục làm việc
  của server, và bị từ chối khi thư mục đó tồn tại ngoài các root của tenant.

---

<a id="config"></a>

## Cấu hình

| Trường | Ý nghĩa |
|---|---|
| `socket` | Đường dẫn của Unix socket để lắng nghe |
| `http` | Địa chỉ cho HTTP kèm WebSocket, như `127.0.0.1:8731` |
| `tenants` | Từng tenant theo tên: 1 đến 64 chữ cái ASCII, chữ số, `.`, `_` hoặc `-` |
| `tenants.<name>.tokens` | Các bearer token của tenant, mỗi token là `{"env": "VAR"}` (đọc khi khởi động) hoặc `{"value": "..."}` |
| `tenants.<name>.allow` | Các allowlist của tenant, bên dưới |

Bắt buộc có ít nhất một trong `socket` và `http`, và ít nhất một token. Một token chỉ
thuộc đúng một tenant: cùng một token xuất hiện hai lần sẽ bị từ chối.

| Trường `allow` | Khi không có | Cho phép gì |
|---|---|---|
| `scripted_provider` | `false` | Provider `scripted`, cho kiểm thử và đánh giá |
| `exec_tools` | `false` | `Bash` cho agent có workspace, chạy lệnh với tư cách người dùng của server |
| `max_runs` | 16 | Số run mà tenant điều khiển cùng lúc, trên tất cả các kết nối của nó |
| `max_agents` | 16 | Số agent mà tenant giữ mở cùng lúc, trên tất cả các kết nối của nó |
| `max_connections` | 8 | Số kết nối hành động thay mặt tenant cùng lúc |
| `max_pool_connections` | Kích thước pool mặc định của server: số core, trong khoảng 10 đến 32 | `stores.max_connections` lớn nhất mà một cấu hình có thể yêu cầu; pool của tenant trên mỗi đích có tối đa chừng này kết nối |
| `provider_base_urls` | không có | Các endpoint mà `base_url` của provider có thể nêu, khớp theo scheme, host và cổng |
| `permission_modes` | `default`, `accept_edits` | Các chế độ mà một cấu hình có thể chọn |
| `workspace_roots` | không có | Các thư mục mà `workspace`, `project_root` hoặc `config_dir` có thể là hoặc nằm dưới |
| `postgres_dsns` | không có | Các đích Postgres mà DSN của kho lưu trữ có thể dùng: host, cổng hoặc socket, cơ sở dữ liệu, người dùng, tùy chọn khởi động và tệp TLS đều phải khớp một mục, và `sslmode` của nó phải bằng hoặc chặt hơn `sslmode` của mục đó |
| `mcp_commands` | không có | Các MCP stdio server, `{"command", "args", "env"}`: lệnh và danh sách đối số phải khớp chính xác, và transport chỉ được đặt các tên `env` được liệt kê |
| `mcp_urls` | không có | Các endpoint mà một MCP HTTP transport có thể nêu, khớp như `provider_base_urls` |

Các provider thật ở endpoint công khai của chúng và các kho lưu trữ trong bộ nhớ không
cần mục nào. Một mục `postgres_dsns` không có người dùng đại diện cho người dùng của
chính server; mục không có tùy chọn hay tệp TLS thì không cho phép cái nào. `sslmode`
của nó là mức yếu nhất mà một DSN được yêu cầu, theo thứ tự `disable`, `allow`,
`prefer`, `require`, `verify-ca`, `verify-full`: một mục có `sslmode=verify-full` từ
chối DSN có `sslmode=require` hoặc không có. Một mục hoặc DSN không có `sslmode` dùng
mặc định của server, `PGSSLMODE` hoặc nếu không thì `prefer`, nên DSN có
`sslmode=disable` cần một mục nói rõ điều đó. Một mục `mcp_commands` không được liệt
kê `PATH`, `HOME`, `XDG_CONFIG_HOME`, `BASH_ENV`, `ENV`, `LD_*`, `DYLD_*`,
`PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONUSERBASE`, `NODE_OPTIONS`,
`NODE_PATH`, `npm_config_*` (không phân biệt hoa thường), `PIP_*`, `UV_*`,
`PERL5LIB`, `PERLLIB`, `PERL5OPT`, `RUBYLIB`, `RUBYOPT`, `GEM_HOME`, `GEM_PATH`,
`CLASSPATH`, `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS` hay
`GCONV_PATH`: chúng quyết định chương trình chạy mã nào, hoặc đọc các tệp rc và cấu
hình nào (một `.npmrc` dưới `HOME` có thể đặt `node-options` cho `npx`). Danh sách đó
chỉ là nỗ lực hết sức, không phải bảo đảm: một chương trình có thể đọc các biến khác,
nên chỉ liệt kê những tên mà bạn biết tác động của chúng lên chương trình đó. Một tên
lệnh trần được tìm trong `PATH` của server; hãy ưu tiên đường dẫn tuyệt đối.

---

<a id="connect-from-python"></a>

## Kết nối từ Python

`Server.connect(address, token=...)` nhận địa chỉ mà server đã in khi khởi động:
`unix:/run/linch/linch.sock` cho socket, `ws://127.0.0.1:8731/rpc` cho WebSocket
(`wss://` khi có TLS phía trước). Dùng nó như một async context manager, giống
`Server()`: vào khối là đăng nhập, ra khỏi khối là đóng kết nối, việc này bỏ dở các
run mà kết nối đang điều khiển ở nguyên trạng thái hiện tại (một run Postgres vẫn
resume được). Tool của ứng dụng chủ, callback, phê duyệt và event stream hoạt động
như qua stdio. WebSocket cần extra `websocket`, thêm `websockets`; socket không cần
gì thêm:

```bash
pip install 'linch-rust[websocket]'
```

```python
import asyncio
import os

from linch_rust import AgentConfig, Allow, NotAllowed, Server, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn

SOCKET = os.environ.get("LINCH_SOCKET", "unix:/run/linch/linch.sock")
WEBSOCKET = os.environ.get("LINCH_WEBSOCKET", "ws://127.0.0.1:8731/rpc")
TOKEN = os.environ["LINCH_TOKEN_ACME"]


@tool(scope="write")
async def save_note(text: str) -> str:
    """Save a note for the support team."""
    return f"saved: {text}"


def support(turns: list[dict]) -> AgentConfig:
    return {
        "namespace": "acme/support",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
    }


async def main() -> None:
    async with Server.connect(SOCKET, token=TOKEN) as server:
        try:
            await server.open_agent({**support([]), "tasks": True})
        except NotAllowed as refused:
            print("refused:", refused.kind)

        turns = [tool_turn("c1", "save_note", {"text": "call back"}), text_turn("Saved.")]
        agent = await server.open_agent(support(turns), tools=[save_note])
        run = await agent.run("s1", "Note: call back")
        stopped = await run.result()
        assert isinstance(stopped, SuspendedEvent)
        print("asks:", stopped.pending_request_ids)
        result = await (await agent.approve(run.id, {"c1": Allow()})).result()
        print("final:", result.final_text)

    async with Server.connect(WEBSOCKET, token=TOKEN) as server:
        agent = await server.open_agent(support([text_turn("Hello over WebSocket.")]))
        print("final:", (await (await agent.run("s2", "hi")).result()).final_text)


asyncio.run(main())
```

```text
refused: auto_allow
asks: ['c1']
final: Saved.
final: Hello over WebSocket.
```

| Bị raise | Khi nào |
|---|---|
| `ValueError` bởi `Server.connect` | Địa chỉ không bắt đầu bằng `unix:`, `ws://` hay `wss://` |
| `OSError` khi vào khối | Không có gì lắng nghe ở địa chỉ đó |
| `Unauthenticated` (-32003) khi vào khối | Token không có trong cấu hình (HTTP `401` trên WebSocket) |
| `LimitReached` (-32005) khi vào khối | Tenant đang giữ `max_connections` kết nối (`kind == "connections"`; HTTP `429` trên WebSocket), hoặc 32 lượt đăng nhập đang chờ cùng lúc (`kind == "logins"`) |
| `NotAllowed` (-32004) | `open_agent` hoặc một lệnh gọi run không qua được một [bước kiểm tra](#những-gì-admission-từ-chối-và-lý-do); `.kind` nêu tên bước đó |
| `LimitReached` (-32005) bởi `open_agent` | Tenant đang giữ mở `max_agents` agent; không có gì được khởi động (`kind == "agents"`) |
| `LimitReached` (-32005) bởi `run`, `resume` hoặc `approve` | Tenant đang điều khiển `max_runs` run; không có gì được khởi động (`kind == "runs"`, `.max` là giới hạn) |

- Server từ xa sở hữu các khóa provider: chúng lấy từ môi trường của server, và
  `Server.connect` không nhận `env`. `server.pid` raise `LinchError`.
- `server.tenant` là tenant mà một kết nối từ xa hành động thay mặt (`"acme"`), như
  câu trả lời đăng nhập của socket hoặc header `Linch-Tenant` của bước nâng cấp
  WebSocket nêu. Nó là `None` với server được khởi chạy bằng `Server()`.
- Mỗi kết nối có các handle agent riêng (`agent-1` luôn là của chính kết nối) và chỉ
  có thể hủy các run mà nó điều khiển. Một run Postgres có thể được resume hoặc approve
  từ một kết nối khác mở cùng cấu hình.

---

## Giao thức trên từng transport

- **Socket:** mỗi dòng một message JSON-RPC 2.0, như trên stdio. Dòng đầu tiên phải là
  `connection.auth` kèm token, được trả lời bằng `{"tenant": "acme"}`; sau đó kết nối
  nhận các method giống stdio: `agent.open` với một [cấu hình agent](./agent.md),
  `run.start`, `run.resume`, `run.approve`, `run.cancel`, `run.events`,
  `session.close`, `agent.close`. Một message đầu tiên không phải `connection.auth`,
  hoặc một token không xác định, nhận `-32003` và kết nối đóng; 10 giây im lặng cũng
  vậy.
- **HTTP:** `GET /rpc` với `Authorization: Bearer <token>` nâng cấp lên một WebSocket
  mang một message JSON-RPC cho mỗi text frame, với các method giống nhau và không có
  `connection.auth`. Phản hồi `101` của nó nêu tenant của token trong header
  `Linch-Tenant`, như câu trả lời đăng nhập của socket.
- Trên cả hai, event của run đến dưới dạng các notification `run.event` và kết thúc
  bằng `run.end`, và server gửi các request `tool.call` và `hook.call` trên cùng kết
  nối để client trả lời.

Server HTTP trả lời trước bước nâng cấp:

| Request | Phản hồi |
|---|---|
| Không có token, hoặc token không xác định | `401` kèm `WWW-Authenticate: Bearer` |
| Tenant đã giữ `max_connections` | `429`, phần body nêu giới hạn |
| Token hợp lệ nhưng không có header WebSocket | `400` |
| Một yêu cầu nâng cấp WebSocket hợp lệ | `101 Switching Protocols`, kèm `Linch-Tenant: <name>` nêu tenant của token |
| Phần đầu request không hoàn tất trong 10 giây | Không trả lời: kết nối đóng |

```bash
curl -i http://127.0.0.1:8731/rpc                     # HTTP/1.1 401 Unauthorized
curl -i -H "Authorization: Bearer $LINCH_TOKEN_ACME" \
     http://127.0.0.1:8731/rpc                        # HTTP/1.1 400 Bad Request
```

---

<a id="tenants-and-namespaces"></a>

## Tenant và namespace

- `namespace` của một agent phải là tên của tenant hoặc bắt đầu bằng `<tenant>/`
  (`acme`, `acme/support`). Kho lưu trữ được khóa theo namespace, nên trên một cơ sở
  dữ liệu Postgres dùng chung, run và session của tenant khác trông như không tồn tại
  (`-32021`).
- Một `principal` truyền cho `run.start`, `run.resume` hoặc `run.approve`
  (`agent.run(..., principal=...)` từ Python) phải nêu tenant của kết nối, nếu không
  lệnh gọi raise `NotAllowed` (`kind == "principal"`). Server không tự điền giá trị.
- Token chỉ được lưu dưới dạng digest SHA-256 và được so sánh trong thời gian không
  đổi. Không token nào được ghi ra bất kỳ đầu ra nào.

---

## Giới hạn

| Giới hạn | Giá trị | Khi vượt quá |
|---|---|---|
| `max_runs` | 16 cho mỗi tenant nếu không đặt | `run.start`, `run.resume` hoặc `run.approve` nhận `-32005`, `data: {"kind": "runs", "max": ...}`; không có gì được khởi động. Một run giữ chỗ cho đến `run.end` của nó; run tạm dừng khi các subagent nền vẫn đang chạy giữ chỗ cho đến khi chúng kết thúc, hoặc cho đến khi approve hay resume của nó tiếp quản |
| `max_agents` | 16 cho mỗi tenant nếu không đặt | `agent.open` nhận `-32005` (`kind: agents`) sau khi qua các bước kiểm tra, trước khi bất kỳ MCP server nào khởi động. Một agent giữ chỗ cho đến khi `agent.close`, hoặc việc kết thúc kết nối của nó, đã dừng các MCP server của nó |
| `max_connections` | 8 cho mỗi tenant nếu không đặt | Đăng nhập qua socket nhận `-32005` (`kind: connections`) và đóng; HTTP nhận `429` |
| Số lượt đăng nhập chờ cùng lúc | 32, trên toàn server | `-32005` (`kind: logins`) và kết nối đóng |
| Thời gian chờ đăng nhập | 10 giây | `-32003` và kết nối đóng |
| Kết nối HTTP đang chờ nâng cấp | 32, trên toàn server; kết nối đã nâng cấp trả lại chỗ | Kết nối bị đóng mà không đọc |
| Phần đầu request HTTP | 10 giây cho mỗi phần đầu trước khi nâng cấp | Kết nối đóng mà không trả lời |
| Một message | 16 MiB | Request nhận `-32600` |
| Một pool Postgres | Một pool cho mỗi tenant và đích, `stores.max_connections` kết nối (số core, trong khoảng 10 đến 32, nếu không đặt) | Các truy vấn chờ một kết nối; lần mở của tenant đã kết nối pool của nó đặt kích thước, và các lần mở sau của tenant dùng chung ([Kho lưu trữ](./stores.md#kết-nối-và-pool)) |
| `max_pool_connections` | Kích thước pool mặc định cho mỗi tenant nếu không đặt | `agent.open` yêu cầu `stores.max_connections` lớn hơn nhận `-32004`, `data: {"kind": "pool_connections", "max": ...}`; không có gì được kết nối |
| Số request đang xử lý cho mỗi kết nối | 64 | Việc đọc chờ cho đến khi một request được trả lời |
| Subagent nền cho mỗi run | `workers.max_background` của cấu hình agent, 4 nếu không đặt | Lệnh gọi `Subagent` nhận một lỗi tool và không khởi động gì ([Subagent](./subagents.md#worker-chạy-nền)) |

Các giá trị mặc định giới hạn những gì một tenant tiêu tốn: mỗi run stream từ một
provider bằng khóa của server, mỗi agent đang mở giữ cấu hình của nó trong bộ nhớ và
chạy các MCP stdio server của nó như tiến trình con, và mỗi kết nối có thể đệm một
message lớn. Các subagent nền của một run cũng stream, ngoài chỗ của run, tối đa
`workers.max_background` của nó (4 nếu không đặt) cùng lúc, nên các run đang chạy của
một tenant có tối đa `max_runs × (1 + max_background)` provider stream đang mở: 80 với
các giá trị mặc định. Các subagent foreground chỉ đọc chạy song song được tính tối đa
đến `max_tool_concurrency` của spec thay cho một chỗ của agent cha. Một run đang chờ
phê duyệt giữ các subagent nền của nó tiếp tục chạy cho đến khi được approve hoặc
resume, hoặc session hay agent của nó đóng, và giữ một chỗ của nó khi bất kỳ subagent
nào còn chạy, nên giới hạn vẫn đúng.

---

## Những gì admission từ chối, và lý do

Mọi bước kiểm tra chạy trước khi bất cứ điều gì xảy ra: với một `agent.open` bị từ
chối, không có khóa nào được đọc, không có tiến trình MCP nào khởi động và không có cơ
sở dữ liệu nào được kết nối. Một lần từ chối là `-32004` với `data.kind` nêu tên bước
kiểm tra: `NotAllowed` trong Python, có `kind` là tên đó.

| `kind` | Bị từ chối khi | Lý do |
|---|---|---|
| `namespace` | Namespace không thuộc tenant | Kho lưu trữ được giới hạn theo namespace |
| `principal` | `principal.tenant` là tenant khác | Một client không thể hành động thay mặt tenant khác |
| `provider` | Provider `scripted` mà không có `scripted_provider` | Nó dành cho kiểm thử và đánh giá |
| `provider_base_url` | Một `base_url` ngoài `provider_base_urls`; vLLM, SGLang và llama.cpp luôn nêu một giá trị | Khóa của server sẽ bị gửi đến đó |
| `permission_mode` | Một chế độ ngoài `permission_modes` | Chế độ quyết định cái gì chạy mà không hỏi |
| `auto_allow` | Các mục `auto_allow`, `tasks: True` hoặc `deep_agent`, mà không có `skip_dangerous` trong `permission_modes` | Chúng cho phép lệnh gọi chạy mà không hỏi, như `skip_dangerous`; `tasks` và `deep_agent` thêm bốn tool task vào `auto_allow` |
| `permission_rule` | Một quy tắc cho phép, mà không có `skip_dangerous` | Tương tự; các quy tắc hỏi hoặc từ chối luôn được chấp nhận |
| `workspace` | Không phải một thư mục tồn tại dưới `workspace_roots` | Các tool workspace đọc và ghi ở đó |
| `project_root` | Tương tự, cho `permissions.project_root` | Các quy tắc đường dẫn được phân giải theo nó; khi không có root, một cấu hình hoàn toàn không thể gửi `permissions` và giữ chính sách mặc định |
| `config_dir` | Khi skill hoặc subagent bật: không phải một thư mục tồn tại dưới các root, qua một đường dẫn tuyệt đối, `..` hoặc một symlink | `SKILL.md` và các tệp agent sẽ được nạp từ cây thư mục của tenant khác |
| `postgres` | Một DSN không khớp chính xác mục `postgres_dsns` nào, hoặc yêu cầu `sslmode` yếu hơn | Một client có thể chọn cơ sở dữ liệu, role, `search_path` hay tệp chứng chỉ khác, hoặc gửi thông tin xác thực của server mà không có TLS |
| `pool_connections` | `stores.max_connections` lớn hơn `max_pool_connections` của tenant | Mọi pool trên một cơ sở dữ liệu dùng chung giới hạn kết nối của nó; `data.max` là mức trần |
| `mcp_command` | Một lệnh stdio và danh sách đối số không khớp mục nào | Server sẽ khởi động chương trình đó |
| `mcp_env` | Một tên `env` mà mục khớp không liệt kê | Nó có thể thay đổi những gì chương trình nạp |
| `mcp_url` | Một URL MCP HTTP ngoài `mcp_urls` | Server sẽ kết nối tới đó |

Một đường dẫn không tồn tại bị từ chối giống như đường dẫn ngoài các root, để client
không thể dò hệ thống tệp của server. Ba thay đổi được thực hiện âm thầm thay vì từ
chối:

- Khi không có `exec_tools`, `bash` của cấu hình bị tắt và agent mở mà không có
  `Bash`. Event `init` của run liệt kê các tool nó thực sự có, và hợp đồng của nó khác
  với cùng cấu hình đó trên stdio.
- Workspace, project root và thư mục cấu hình được thay bằng các đường dẫn chuẩn tắc
  đã được kiểm tra.
- Một kho Postgres không có `max_connections` nhận kích thước pool mặc định hoặc
  `max_pool_connections` của tenant, tùy giá trị nào nhỏ hơn.

---

## Môi trường của server bảo vệ những gì

Server đọc khóa provider từ môi trường của chính nó; một cấu hình không bao giờ mang
theo khóa.

| Ai | Thấy gì từ môi trường của server |
|---|---|
| `Bash` | Chỉ `PATH`, `HOME`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR` |
| Một MCP stdio server | Chỉ `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`, cộng với `env` mà transport của nó đặt |
| Tiến trình khác của cùng người dùng | Trên Linux, server được đánh dấu không dump được, nên không đọc được `/proc/<server pid>/environ` và bộ nhớ của nó |

Điều đó chỉ bảo vệ tiến trình server. Khi tiến trình Python của bạn khởi động server
bằng `Server()`, các khóa trong môi trường của chính tiến trình Python vẫn đọc được
bởi mọi tiến trình của cùng người dùng, kể cả các tiến trình con của server, qua
`/proc/<python pid>/environ`. Khi agent chạy các lệnh không đáng tin, hãy giữ khóa
ngoài môi trường tiến trình của bạn, đọc chúng từ một kho bí mật, và chỉ trao cho
server:

```python
from linch_rust import Server

server = Server(env={"DEEPSEEK_API_KEY": "<read from your secret store>"})
```

`env` là toàn bộ môi trường của server; không có gì khác được truyền theo.

---

## Các trang liên quan

- [Cấu hình agent](./agent.md): những gì `agent.open` nhận
- [Run](./runs.md): resume, approve, cancel và `run.events`
- [Task của session](./tasks.md) và [Deep agent](./deep-agent.md): lý do chúng cần
  `skip_dangerous` ở đây
- [Khắc phục sự cố](./troubleshooting.md)
