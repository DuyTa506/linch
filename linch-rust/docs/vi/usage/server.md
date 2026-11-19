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
| Dừng | Khi stdin của nó đóng | Khi nhận SIGINT hoặc SIGTERM: các run đang chạy bị drop, các job của worker có `worker.drain_timeout_ms` để hoàn tất ([Scheduler](./scheduler.md#dừng-một-worker)) |

`linch-server --migrate` và `linch-server --check` là cách dùng thứ ba của binary: chúng áp
dụng hoặc đọc schema cơ sở dữ liệu rồi thoát
([Stores: Migration như một bước deploy](./stores.md#migration-như-một-bước-deploy)).

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
| `cache_salt_secret` | `{"env": "VAR"}` hoặc `{"value": "..."}`, ít nhất 16 byte: nguồn để suy ra salt prompt-cache của các tenant ([bên dưới](#cô-lập-prompt-cache)). Bắt buộc với tenant có `allow.cache_isolation` bật |
| `worker` | Chạy một worker trong tiến trình này: nó kích hoạt các lịch và chạy các job nền ([Scheduler](./scheduler.md#chạy-một-worker)). Không có: không chạy gì |
| `liveness` | Cách listener HTTP nhận ra một client đã biến mất: TCP keepalive và ping WebSocket ([bên dưới](#liveness)). Mọi trường đều có mặc định |

Bắt buộc có ít nhất một trong `socket`, `http` và `worker`, và với một listener thì cần
ít nhất một token. Một token chỉ thuộc đúng một tenant: cùng một token xuất hiện hai lần
sẽ bị từ chối. Một tenant chỉ do worker phục vụ thì không cần token.

| Trường `allow` | Khi không có | Cho phép gì |
|---|---|---|
| `scripted_provider` | `false` | Provider `scripted`, cho kiểm thử và đánh giá |
| `provider_info` | `false` | `provider.capabilities`, `provider.models` và `provider.model`: các lời gọi model gửi key của server đến `base_url` của provider, nên `base_url` đó cũng phải được cho phép. `provider.models` và `provider.model` bị giới hạn 10 lần một phút cho mỗi tenant (`-32005`, `kind: provider_calls`); `provider.capabilities` không gửi key |
| `exec_tools` | `false` | `Bash` cho agent có workspace, chạy lệnh qua execution backend của agent |
| `task_ledger` | `false` | `task_ledger` của cấu hình: một danh sách task dùng chung cho mọi session của tenant, các task còn mở của nó đi kèm mỗi request ([Tasks](./tasks.md#một-ledger-tồn-tại-qua-các-session)) |
| `cache_isolation` | `false` | Giữ prompt cache của provider của tenant tách khỏi tenant khác trên cùng một key: mỗi agent gửi một salt suy ra từ tên tenant (`cache_salt` tới `vllm` và `sglang`, `prompt_cache_key` tới `openai-chat` và `openai-responses`), và agent trên provider không có trường như vậy bị từ chối. Cần `cache_salt_secret` cấp cao nhất của config ([bên dưới](#cô-lập-prompt-cache)) |
| `provider_request` | `false` | `request` (trường body, header, timeout) và `chat` của provider trong config ([Providers](./providers.md)). Header credential, `proxy-*` và forwarding vẫn bị từ chối, số lượng và kích thước bị giới hạn |
| `max_runs` | 16 | Số run mà tenant điều khiển cùng lúc, trên tất cả các kết nối của nó |
| `max_agents` | 16 | Số agent mà tenant giữ mở cùng lúc, trên tất cả các kết nối của nó |
| `max_connections` | 8 | Số kết nối hành động thay mặt tenant cùng lúc |
| `max_pool_connections` | Kích thước pool mặc định của server: số core, trong khoảng 10 đến 32 | `stores.max_connections` lớn nhất mà một cấu hình có thể yêu cầu; pool của tenant trên mỗi đích có tối đa chừng này kết nối |
| `max_provider_concurrency` | không có: không có trần | Số lời gọi provider tối đa mà các run của tenant thực hiện cùng lúc trên mỗi key và endpoint provider; một cấu hình có thể xin ít hơn ([Provider](./providers.md#limit-concurrent-calls)) |
| `provider_base_urls` | không có | Các endpoint mà `base_url` của provider có thể nêu, khớp theo scheme, host, cổng và path. Một mục có path (`https://gw.example/openai/v1`) chỉ cho phép path đó; một mục không có path (`https://gw.example`) cho phép path của chính từng provider ở đó (`/v1` cho OpenAI, không có cho DeepSeek, Anthropic và Gemini, `/v1` hoặc không có cho vLLM, SGLang và llama.cpp). Một `base_url` có query, fragment, userinfo, mã hóa phần trăm, dấu gạch ngược hoặc đoạn path `.`/`..`/rỗng bị từ chối dù có liệt kê gì |
| `permission_modes` | `default`, `accept_edits` | Các chế độ mà một cấu hình có thể chọn |
| `workspace_roots` | không có | Các thư mục mà `workspace`, `project_root` hoặc `config_dir` có thể là hoặc nằm dưới |
| `postgres_dsns` | không có | Các đích Postgres mà DSN của kho lưu trữ có thể dùng: host, cổng hoặc socket, cơ sở dữ liệu, người dùng, tùy chọn khởi động và tệp TLS đều phải khớp một mục, và `sslmode` của nó phải bằng hoặc chặt hơn `sslmode` của mục đó |
| `mcp_commands` | không có | Các MCP stdio server, `{"command", "args", "env"}`: lệnh và danh sách đối số phải khớp chính xác, và transport chỉ được đặt các tên `env` được liệt kê |
| `mcp_urls` | không có | Các endpoint mà một MCP HTTP transport có thể nêu, khớp như `provider_base_urls` |
| `max_schedules` | không có: không có lịch | Số lịch mà tenant được giữ trong mỗi cơ sở dữ liệu ([Scheduler](./scheduler.md#tenant-được-lập-lịch-gì)); không có nó thì một agent có `schedule_tools` bị từ chối |
| `min_schedule_interval_ms` | 60000 | Khoảng thời gian ngắn nhất giữa hai lần kích hoạt của các lịch của nó |
| `tool_cache` | `false` | `spec.tool_cache`, giữ kết quả trong bộ nhớ hoặc cơ sở dữ liệu của server cho namespace của tenant; giới hạn 1024 mục, ttl một giờ, 1 MiB mỗi kết quả và 32 tool. Agent cũng cần `spec.redaction` bao phủ kết quả tool, và các mục chỉ phục vụ session đã lưu chúng |
| `tool_cache_shared` | `false` | Việc `tool_cache` của tenant có được dùng `scope: "namespace"` hay không, vốn phục vụ kết quả của một session cho mọi session của namespace; một tool read có thể trả lời theo từng session, nên operator quyết định |
| `max_tool_cache_bytes` | 67108864 (64 MiB) | Số byte kết quả cache mà mọi namespace của tenant được giữ cùng nhau; tối thiểu 1 |
| `workflow` | không có: không có workflow | `workflow.*` ([Workflows](./workflows.md#admission)): `{}` cho phép chúng với các mức trần mặc định, hoặc đặt `max_workflows` (4), `max_records` (10000), `max_fan_out` (8), `max_record_bytes` (1 MiB), `max_tokens`, `max_cost_usd` |
| `max_queued_jobs` | không có: không có run nền | Số job nền chưa xong mà nó được có khi đưa thêm một job vào hàng đợi |
| `system_sections` | không có | Các system prompt section `{"name", "text", "order"?, "cacheable"?}` được thêm vào mọi agent của tenant; tenant không thể gỡ hay đổi tên chúng, và `replace_defaults` để chúng lại. Thay đổi chúng làm resume các run đang chạy của tenant bị từ chối ([Config của agent](./agent.md#system-sections)) |
| `memory` | `false`: không có bộ nhớ dài hạn | Config có được bật [`memory`](./memory.md#built-in-long-term-memory) hay không |
| `max_memories` | không có, từ chối `memory` (server không nạp config có `memory` mà không có cap) | Số memory nó được giữ trong mọi namespace của một database; `max_memories` và `max_memories_per_scope` của agent bị giữ dưới mức này |
| `required_policy` | không có | Một [`spec.policy`](./agent.md#policy) mà mọi agent của tenant đều mang; policy riêng của tenant được thêm vào nó, và các rule của operator vẫn nằm trong run. Nó sắp thứ tự các lời gọi của model, đếm từ kết quả tool của chính agent; nó không ràng buộc những gì host của tenant làm (một host tool, hook hay cache có thể trả lời lời gọi bắt buộc), nên hãy giữ permission và sandbox cho bảo mật ([bên dưới](#những-gì-admission-từ-chối-và-lý-do)) |
| `execution` | `[{"backend": "local"}]` | Các execution backend mà một cấu hình có thể chọn, mỗi cái kèm toàn bộ policy của nó; `execution` của cấu hình phải bằng một mục, và cấu hình không nêu gì nhận mục đầu tiên, nên `[{"backend": "sandbox"}]` giữ tenant trong sandbox ([Sandbox](./sandbox.md)) |

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
        "spec": {"model": MODEL, "max_turns": 20},
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
| `IncompatibleServer` khi vào khối | [Bắt tay phiên bản](#bắt-tay-phiên-bản) thất bại: protocol của server nằm ngoài dải client này nói, hoặc server có trước `server.info` |
| `HandshakeTimeout` khi vào khối | Server không trả lời `server.info` trong 15 giây |
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

### Bắt tay phiên bản

Vào `Server()` hoặc `Server.connect(...)` thực hiện thêm một lời gọi trước khi trả về:
`server.info`, sau bước đăng nhập. Câu trả lời nằm ở `server.info`:

```python
async with Server.connect(SOCKET, token=TOKEN) as server:
    info = server.info
    info.server_version      # "0.1.1"
    info.protocol            # 1: giao thức wire; client này nói từ 1 đến 1
    info.contract_version    # 1: phiên bản digest của run contract
    info.has("cache_isolation")   # một tính năng tùy chọn, theo tên
    info.has("exec_idempotency_key")   # exec.start mang khóa của lời gọi
    info.backends            # ("local", "sandbox", "docker", "remote")
    info.max_message_bytes   # 16777216
    info.default_max_runs    # 16
```

- **Không khớp thì lỗi ngay, có tên.** Protocol nằm ngoài dải của client ném
  `IncompatibleServer` (`.protocol`, `.server_version`, `.supported`), server không có
  `server.info` cũng vậy (`.protocol` là `None`): một binary cũ hơn client. Tiến trình
  con được dừng, hoặc kết nối được đóng, trước khi lỗi đến tay bạn, nên không rò chỗ
  kết nối của tenant. Server không bao giờ trả lời ném `HandshakeTimeout`.
- **Số protocol hiếm khi đổi.** Nó chỉ đổi với một thay đổi wire mà client cũ sẽ đọc sai.
  Một method, member hoặc tính năng tùy chọn mới thì thêm một tên capability; kiểm tra
  bằng `info.has(...)` và bỏ qua các tên bạn không biết.
- **Mọi kết nối nhận cùng một câu trả lời.** Tenant thấy đúng những gì client local
  thấy: các hằng của bản build, không có giới hạn, tên, host hay đường dẫn riêng của
  nó. `backends` liệt kê những gì bản build cài đặt, không phải những gì host chạy được
  hay tenant được dùng; `default_max_runs` là mức giới hạn mặc định, không phải của
  riêng tenant.

Qua stdio trên Linux, `Server()` nới pipe stdout của tiến trình con từ 64 KiB lên
256 KiB ngay sau khi spawn (`F_SETPIPE_SZ`). Server nối các event đang chờ thành
dòng tối đa 64 KiB, nên với pipe mặc định, một đợt event dồn dập làm nó bị chặn ở
gần như mọi lần ghi. Nếu kernel từ chối việc đổi kích thước (ví dụ vượt
`fs.pipe-user-pages-soft`), pipe giữ kích thước mặc định, một dòng log mức debug
ghi lại điều đó và việc khởi động vẫn tiếp tục. Không có gì thay đổi trên dây, và
transport Unix socket và WebSocket không bị ảnh hưởng.

---

## Giao thức trên từng transport

- **Socket:** mỗi dòng một message JSON-RPC 2.0, như trên stdio. Dòng đầu tiên phải là
  `connection.auth` kèm token, được trả lời bằng `{"tenant": "acme"}`; sau đó kết nối
  nhận các method giống stdio: `agent.open` với một [cấu hình agent](./agent.md),
  `run.start`, `run.resume`, `run.approve`, `run.cancel`, `run.events`,
  `eval.run`, `run.report`, `session.close`, `agent.close`. Một message đầu tiên không phải `connection.auth`,
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

### Điều gì giữ các tenant tách nhau

Mọi bảng mà store và worker dùng (`linch_runs`, `linch_run_events`, `linch_sessions`,
`linch_session_entries`, `linch_files`, `linch_memories`, `linch_mailbox`, `linch_tasks`,
`linch_tool_cache`, `linch_ext_workflows`, `linch_worker_schedules`) có `namespace` trong
khóa chính. Các bảng job (`linch_worker_jobs`, `linch_worker_history`) được khóa theo một
id toàn cục và có cột `namespace` mà `job.get` và `job.list` luôn ràng buộc
(`crates/linch-worker/src/status.rs:153-186`). Mỗi truy vấn nêu namespace, nên một run,
session, file, memory, workflow, job hay schedule chỉ tìm thấy được dưới namespace của
agent đang hỏi.
Mọi namespace của một tenant bắt đầu bằng tên nó, và tên không chứa `/`, nên `acme/support`
không với tới được từ `acme-eu`. Id của run, session, workflow, job và schedule mà tenant
khác đang giữ trả lời y hệt id không ai có: cùng code, message và data, chỉ khác id được
hỏi (`tenant_oracle` trong `crates/linch-rpc/tests` cố định điều này cho `run.events`,
`run.report`, `run.resume`, `run.approve`, `run.answer`, `run.steer`, `run.cancel`,
`workflow.status`, `workflow.start`, `job.get`, `schedule.pause`, `schedule.resume`,
`schedule.delete` và `session.close`). Một claim bị từ chối (`-32033`) chỉ cho thấy owner
của run trong namespace của chính tenant đó.

Những gì khóa này không làm, và cách xử lý:

- **Một database, một bộ bảng.** Cô lập là namespace trong mỗi truy vấn, không phải
  row-level security hay một schema cho mỗi tenant. Tenant cần chịu được một lỗi trong
  truy vấn thì cần database hoặc schema riêng: cho nó một mục `postgres_dsns` riêng (một
  DSN có `search_path` riêng).
- **Các user của một tenant không tách nhau.** Run, session, file và scope memory dùng
  chung thuộc về namespace. `principal.user` chỉ phân vùng memory theo principal và các
  role phân quyền. Muốn tách hai user, cho mỗi user một namespace (`acme/ada`, `acme/bob`).
- **Tên queue là dùng chung.** `job.enqueue` và `schedule.create` nhận một `queue` do
  tenant chọn, và `linch_worker_queues` (giới hạn đồng thời `max_held`) chỉ khóa theo tên.
  Tenant nêu tên queue của tenant khác sẽ vào FIFO của nó và tính vào giới hạn của nó. Hãy
  đặt một queue cho mỗi tenant trong `queues` của worker (worker phục vụ các queue luân
  phiên, nên đó là chia phần công bằng) và để host đặt `queue` được gửi.
- **Id job là một bộ đếm.** `linch_worker_jobs.id` là một identity sequence duy nhất, nên
  tenant đọc id job của mình biết được server đã xếp hàng bao nhiêu job ở giữa. Nó không
  đọc được gì khác.
- **Queue của worker là vào trước ra trước.** Tenant có `max_queued_jobs` lớn có thể lấp
  đầy các slot `max_jobs` của worker và khiến job của tenant khác cùng queue phải chờ.
  `max_runs` của từng tenant giới hạn số nó chạy cùng lúc, không phải số nó giữ trong
  queue; đặt `max_queued_jobs` bằng mức worker dọn được.
- **Key provider là dùng chung.** `max_provider_concurrency` giới hạn các lời gọi của một
  tenant trên một key và endpoint, và mỗi tenant có gate riêng, nên không tenant nào giữ
  chỗ của tenant khác. Không có giới hạn xuyên tenant: tổng giới hạn của các tenant là
  điều nhà cung cấp thấy trên một key, nên giữ tổng đó dưới giới hạn của nhà cung cấp.
  Prompt cache của nhà cung cấp cũng dùng chung, trừ khi bật `cache_isolation`.

#### Cô lập prompt cache

Với `cache_salt_secret` của config và `allow.cache_isolation` bật cho một tenant, admission
đặt một salt vào mọi agent của tenant đó: HMAC-SHA256 viết thường dạng hex của tên tenant
dưới secret (64 ký tự, 256 bit), vào trường mà provider có cho việc này: `cache_salt` trên
`vllm` và `prompt_cache_key` trên `openai-chat` và `openai-responses`. Cùng một tenant gửi
cùng salt ở mọi run, từ mọi server và worker có cùng secret, nên số lần trúng cache của
riêng nó vẫn như cũ; hai tenant gửi salt khác nhau. Salt không phải tên tenant, không nằm
trong config (một schedule hay job đã lưu không chứa nó; worker suy ra lại), `extra_body`
của tenant không thay được nó, và nó bị ẩn khỏi `Debug` và khỏi lỗi mà provider vọng lại.
Giữ secret ít nhất 16 byte, giống nhau trên mọi server và worker của cụm, và ngoài kho mã;
đổi secret thì mỗi tenant có salt mới, nghĩa là cache của họ bắt đầu lại từ nguội. Xem
[Provider](./providers.md#giữ-prompt-cache-của-tenant-này-tách-khỏi-tenant-khác) để biết
mỗi nhà cung cấp làm và không làm gì.

```json
{
  "cache_salt_secret": {"env": "LINCH_CACHE_SALT_SECRET"},
  "tenants": {"acme": {"tokens": [{"env": "ACME_TOKEN"}],
                       "allow": {"cache_isolation": true,
                                 "provider_base_urls": ["https://vllm.internal/v1"]}}}
}
```

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
| Một client HTTP đã biến mất | TCP keepalive (30 giây nhàn rỗi, 3 probe cách nhau 10 giây) và TCP user timeout 60 giây; một ping WebSocket mỗi 30 giây và 60 giây im lặng ([Liveness](#liveness)) | Kết nối kết thúc, giải phóng chỗ `max_connections`, các hold workflow và các lời gọi của nó |
| Một message | 16 MiB | Request nhận `-32600` |
| Một pool Postgres | Một pool cho mỗi tenant và đích, `stores.max_connections` kết nối (số core, trong khoảng 10 đến 32, nếu không đặt) | Các truy vấn chờ một kết nối; lần mở của tenant đã kết nối pool của nó đặt kích thước, và các lần mở sau của tenant dùng chung ([Kho lưu trữ](./stores.md#kết-nối-và-pool)) |
| `max_pool_connections` | Kích thước pool mặc định cho mỗi tenant nếu không đặt | `agent.open` yêu cầu `stores.max_connections` lớn hơn nhận `-32004`, `data: {"kind": "pool_connections", "max": ...}`; không có gì được kết nối |
| `max_provider_concurrency` | Không có trần nếu không đặt | `agent.open` xin nhiều hơn nhận `-32004`, `data: {"kind": "provider_concurrency", "max": ...}`; cấu hình không xin gì nhận mức trần. Mọi agent của tenant trên một key và endpoint provider dùng chung chừng đó lời gọi provider; một tiến trình worker hay server thứ hai tự đếm |
| `max_tool_cache_bytes` | 64 MiB cho mỗi tenant nếu không đặt | Admission ghi `{"group": <tenant>, "max_bytes": ...}` vào `tool_cache.quota` của agent, bất kể client gửi gì, và mỗi lần put khiến store bỏ các mục cũ nhất của tenant (trong Postgres bằng tổng lũy kế độ dài đã lưu trên các hàng của tenant; trong memory trên store riêng của agent) cho đến khi tổng vừa. Agent dùng store memory giữ store riêng, nên bộ nhớ của một tenant tối đa bằng mức này nhân số agent đang mở |
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
| `cache_isolation` | `allow.cache_isolation` bật và provider của agent không có trường request cho cache salt (`deepseek`, `anthropic`, `gemini`, `llamacpp`) | Operator đã yêu cầu cache cô lập mà nhà cung cấp không có cách nào để làm; xem [Provider](./providers.md#giữ-prompt-cache-của-tenant-này-tách-khỏi-tenant-khác) |
| `provider_base_url` | Một `base_url` ngoài `provider_base_urls` (origin hoặc path), hoặc không phải dạng thường (query, fragment, userinfo, mã hóa phần trăm, đoạn dấu chấm); vLLM, SGLang và llama.cpp luôn nêu một giá trị | Khóa của server sẽ bị gửi đến đó, hoặc đến một path khác của origin đó |
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
| `execution` | Một `execution` không bằng mục `allow.execution` nào, hoặc có một cái khi danh sách đó rỗng | Backend quyết định `Bash` chạm tới được gì; policy của nó thuộc về operator |
| `redaction` | Hơn 32 rule trong `spec.redaction`, hoặc một pattern dài quá 1024 byte | Server biên dịch từng pattern và chạy nó trên mọi response và kết quả tool |
| `verification` | Hơn 32 verifier trong `spec.verification`, một pattern dài quá 1024 byte, hoặc hơn 32 keyword trong một verifier | Tương tự, cho mọi câu trả lời cuối cùng |
| `provider_concurrency` | `max_provider_concurrency` lớn hơn mức của tenant | Các run của tenant sẽ gọi cùng lúc nhiều hơn mức nhà vận hành cho phép trên key của server; `data.max` là mức trần |
| `system` | Hơn 16 section trong `spec.system`, một tên dài quá 64 byte, một section hoặc `append` dài quá 16 KiB, hoặc tổng cộng quá 64 KiB | Các section đi kèm mọi request của mọi run |
| `context_budget` | Hơn 32 id trong `drop_order` hoặc `pinned`, hoặc một id dài quá 128 byte | Server giữ chúng cho từng agent |
| `tool_cache` | `spec.tool_cache` mà không có `allow.tool_cache`; không có `spec.redaction` bao phủ kết quả tool; với `scope: "namespace"` mà không có `allow.tool_cache_shared`; hoặc vượt giới hạn của nó (`max_entries` 1024, `ttl_ms` 3600000, `max_value_bytes` 1048576, 32 tool) | Các mục do server lưu, nên thứ một tool trả về phải được redact trước, và một kết quả chỉ được phục vụ cho session khác khi operator cho phép. Bảng hoặc bộ nhớ chúng chiếm thuộc về operator |
| `tool_retry` | `spec.tool_retry.max_attempts` lớn hơn 5, `max_delay_ms` lớn hơn 60000, hoặc một danh sách dài quá 32 tên | Mỗi lần retry là một lần ghi bền và một khoảng chờ mà run giữ một slot suốt thời gian đó. Không có `allow.tool_retry`: một lần retry chỉ lặp lại một tool mà agent vốn đã gọi được, nên các mức trần là toàn bộ cơ chế kiểm soát |
| `policy` | Hơn 32 prerequisite hoặc gate, hơn 32 tool hoặc keyword trong một mục, một văn bản dài quá 1024 byte, `max_corrections` lớn hơn 16, hoặc một gate định nghĩa lại gate của `required_policy` | Server kiểm tra từng rule trên mọi batch tool và mọi câu trả lời cuối cùng, và mỗi lần sửa là một lời gọi provider mà policy yêu cầu; các gate của operator không thuộc quyền thay đổi của tenant |
| `loop_guard` | `spec.loop_guard.max_idle_turns` lớn hơn 25 | Mỗi lượt không có lời gọi provider là một lần ghi và một lần nối session trên server mà không budget nào đếm |
| `run_bound` | `spec.max_turns` không đặt, `spec.budget` không đặt `max_tokens` lẫn `max_cost_usd`, và cả `profile` lẫn profile `deep_agent` đều không cấp `max_turns` | Run của tenant phải có một giới hạn nào đó: một model cứ gọi tool mãi sẽ chạy trên key của operator cho đến khi loop guard dừng nó, mà một input luôn thay đổi thì né được loop guard (xem [loop guard](./agent.md#loop_guard)). Văn bản từ chối nêu tên cả hai thiết lập. Không có gì được thêm vào spec nên run contract không đổi; một root `local_namespaces` tin cậy và một client stdio không bị ràng buộc bởi quy tắc này |
| `tool_choice` | Một `tool_choice` bị ép (`required`, hoặc một tool không phải final tool), trong spec hoặc trong `run.start`, trên một agent không có `max_turns` và không có `final_tool_name` (riêng output schema không kết thúc run trên mọi provider) | Model bị ép gọi một tool mỗi lượt sẽ không bao giờ trả lời, và không budget nào kết thúc run |

Một đường dẫn không tồn tại bị từ chối giống như đường dẫn ngoài các root, để client
không thể dò hệ thống tệp của server. Bảy thay đổi được thực hiện âm thầm thay vì từ
chối:

- Khi không có `exec_tools`, `bash` của cấu hình bị tắt và agent mở mà không có
  `Bash`. Event `init` của run liệt kê các tool nó thực sự có, và hợp đồng của nó khác
  với cùng cấu hình đó trên stdio.
- Workspace, project root và thư mục cấu hình được thay bằng các đường dẫn chuẩn tắc
  đã được kiểm tra.
- Một cấu hình không có `execution` nhận mục `allow.execution` đầu tiên của tenant, giữ tenant trong sandbox khi đó là mục đầu.
- Một kho Postgres không có `max_connections` nhận kích thước pool mặc định hoặc
  `max_pool_connections` của tenant, tùy giá trị nào nhỏ hơn.
- Một cấu hình không có `max_provider_concurrency` nhận mức của tenant, khi tenant có.
- `system_sections` của operator được thêm vào những section của cấu hình, và một
  section của cấu hình có cùng tên bị thay bằng section của operator
  (`allow.system_sections`). Chúng không được tính vào giới hạn ở trên, và
  `replace_defaults` để chúng lại.
- Với `required_policy`, `spec.policy` của cấu hình được gộp với nó trước khi agent
  được xây dựng, nên nó là một phần của hợp đồng của run: một prerequisite lấy hợp của
  các yêu cầu của hai bên, các gate của operator được giữ nguyên (tenant có thể thêm
  gate dưới tên khác, hoặc lặp lại một gate y như cũ), và số lần sửa được phép là giá
  của operator khi tenant không đặt gì, còn không thì là giá trị nhỏ hơn trong hai mức
  trần (tenant đặt `max_corrections` có thể hạ nó, không thể nâng). Một rule nêu một
  tool mà agent không có thì từ chối open (`OpenError`), không bao giờ bị bỏ đi; lỗi
  gửi cho tenant không nêu tên tool hay lặp lại pattern, vì rule do operator viết (config
  server từ chối một `required_policy` có check không biên dịch được). Các rule chỉ ràng buộc
  thứ tự lời gọi của model: một host tool của client dưới tên bắt buộc, một hook
  `pre_tool_use`, một thay thế của `post_tool_use` hay một cache hit vẫn có thể thỏa
  một prerequisite, nên đây không phải cưỡng chế đối với host của chính tenant.

---

## Log

Các bản ghi `tracing` của server (job thất bại, retry và lỗi dọn dẹp của worker, lịch bị
pause của scheduler, journal workflow bị lỗi, một lời gọi tool lịch thất bại ở store) đi ra
**stderr**, không bao giờ ra stdout, nơi mang giao thức trên stdio. Mặc định chỉ ghi cảnh báo
và lỗi. `LINCH_LOG` đặt bộ lọc (`RUST_LOG` được đọc khi nó chưa đặt), theo cú pháp directive
của `tracing-subscriber`:

```bash
LINCH_LOG=info linch-server --config server.json            # ghi thêm các lần bỏ qua và retry
LINCH_LOG=linch_worker=debug,warn linch-server --config server.json
```

Một giá trị không phải bộ lọc hợp lệ được báo trên stderr và `warn` được dùng. Các bản ghi có
thể trích lại những gì một provider, một tool hay cơ sở dữ liệu đã nói, mà một endpoint có thể
đã lặp lại một credential vào đó: hãy giữ log riêng tư như cơ sở dữ liệu. Nó không mang token
nào và không mang key nào mà server tự đọc. Dưới systemd hoặc runtime container, stderr đã được
thu thập; với `Server()` của client Python, thứ khởi động binary cho bạn, nó là stderr của
process cha.

## Server nào điều khiển một run

Một run trên Postgres chỉ do một server điều khiển tại một thời điểm. Một resume, approve
hay answer gửi tới server khác bị từ chối với `RunLeased` (-32033), mà `owner` của nó nêu tên
server đang giữ run, để operator biết nên xem server nào. Tên đó là id của server: 8 ký tự hex
ngẫu nhiên tạo lúc khởi động và in ra stderr (`linch-server: server id 3fa91c0b`), hoặc
`server_id` của config (1 đến 64 chữ cái ASCII, chữ số, `.`, `_` hoặc `-`). Nó giống nhau cho
mọi tenant và kết nối của process server đó và cho các hàng run mà nó claim, và đổi sau mỗi
lần khởi động trừ khi được đặt. Tenant thấy nó trong lời từ chối, nên nó không chứa tên host,
đường dẫn hay pid: một giá trị cấu hình cũng không nên. Hãy cho mỗi process server một id riêng;
`worker.owner` mặc định là nó. Qua stdio mỗi process có một kết nối và id là của kết nối đó.

## Liveness

Một client biến mất mà không đóng kết nối (mất điện, đứt route, một VM bị kill) sẽ giữ chỗ
trong `max_connections` của tenant, hold của các workflow và các lời gọi đang chạy của nó cho đến
khi server khởi động lại. Vì vậy listener HTTP:

- đặt **TCP keepalive** trên mọi socket được chấp nhận và một **TCP user timeout** dài bằng thời
  gian keepalive mất để bỏ cuộc, nên kernel bỏ một peer không trả lời cả probe lẫn dữ liệu;
- ping mỗi WebSocket cứ `ping_interval_ms` và kết thúc cái nào không gửi gì cả (không frame, không
  pong) trong `pong_timeout_ms`. Sự im lặng được xét ngay khi nó kéo dài, không chỉ lúc một ping
  được gửi: kết nối kết thúc `pong_timeout_ms` sau frame cuối cùng client gửi (60 giây với mặc
  định), bất kể `ping_interval_ms` là bao nhiêu. Một probe được kernel của peer trả lời, nên một client còn sống nhưng
  bị treo vẫn qua được; một ping được code WebSocket của client trả lời, nên một client treo thì
  không.

Kết thúc một kết nối giống như input của nó kết thúc: các run của nó bị bỏ (một run Postgres giữ
trạng thái bền vững cuối cùng và có thể resume), các lời gọi của nó được trả lời, và các chỗ của nó
được giải phóng.

Một client vẫn kết nối nhưng **ngừng đọc** (buffer socket và hàng đợi 1024 message của server
đầy) làm các run của nó tạm dừng, và vẫn giữ chúng: lease của run được gia hạn bởi một task
riêng, nên không server nào khác tiếp quản được run khi kết nối còn sống ([Run
leases](./stores.md#run-leases)). Một WebSocket không đọc gì cũng không nhận được ping: nó bị kết
thúc khi một ping không gửi được trong `pong_timeout_ms`, và các run của nó bị bỏ như trên. Unix
socket và stdio không có write timeout: một client cục bộ không bao giờ đọc lại giữ các run của
nó cho đến khi đóng.

**Một client đang bận nhưng còn sống phải giữ cho event loop của nó chạy.** Pong đến từ code
WebSocket của client, chạy trên event loop của nó. Một client Python có loop bị chặn lâu hơn
`pong_timeout_ms` (mặc định 60 giây) bởi một host tool đồng bộ sẽ không trả lời ping nào, nên
server kết thúc kết nối; lời gọi kế tiếp của client ném `ConnectionClosed`, và các run của nó bị
bỏ như trên. Một tool chờ bằng `await` hoặc chạy trong một thread (`asyncio.to_thread`) để loop
rảnh, và kết nối vẫn còn. Vì vậy hãy chạy một host tool chặn lâu trong một thread. Điều này chỉ áp
dụng cho WebSocket: client Python khởi động server qua stdio và một Unix socket không có kiểm tra
liveness, nên cả hai đều không cắt một client đang bận. `test_remote.py` ghim quy tắc này với hai
mốc thời gian được rút ngắn (timeout 400 ms và một client bị chặn 1 giây).

| Trường `liveness` | Mặc định | Ý nghĩa |
|---|---|---|
| `keepalive_idle_secs` | 30 | Thời gian nhàn rỗi trước probe đầu tiên, tính bằng giây nguyên (1 đến 32767) |
| `keepalive_interval_secs` | 10 | Thời gian giữa các probe, tính bằng giây nguyên (1 đến 32767) |
| `keepalive_probes` | 3 | Số probe không được trả lời trước khi kết nối bị bỏ (1 đến 127), tức tổng cộng khoảng 60 giây |
| `ping_interval_ms` | 30000 | Thời gian giữa các ping WebSocket |
| `pong_timeout_ms` | 60000 | Thời gian im lặng sau đó một WebSocket bị kết thúc; giữ nó lớn hơn `ping_interval_ms` |

Mỗi trường ít nhất là 1, và một trường keepalive có giá trị lớn nhất mà các tùy chọn TCP của Linux nhận (ghi ở trên), nên một config mà kernel sẽ từ chối bị từ chối ngay khi nạp. Thời gian keepalive là giây nguyên vì đó là đơn vị của kernel; không có dạng mili giây, và tên `_ms` cho chúng là một trường không xác định. Một **Unix socket** không có cả hai: kernel đóng nó khi process của đầu
bên kia kết thúc, bất kể lý do, nên nó không cần probe. Kết nối Postgres giữ một workflow đặt các
keepalive riêng của cơ sở dữ liệu ([Workflows](./workflows.md#một-driver-cho-mỗi-workflow)).

## Môi trường của server bảo vệ những gì

Server đọc khóa provider từ môi trường của chính nó; một cấu hình không bao giờ mang
theo khóa.

| Ai | Thấy gì từ môi trường của server |
|---|---|
| `Bash` | Chỉ `PATH`, `HOME`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`; trong backend `sandbox`, `HOME` và `TMPDIR` là home riêng của nó, và `/proc/<pid>/environ` của process khác không đọc được ([Sandbox](./sandbox.md)) |
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
