# Config của agent

[Bản gốc tiếng Anh](../../usage/agent.md)

Dùng trang này để viết config mà một agent được mở từ đó, với mọi trường cùng giá
trị mặc định của nó, và để cung cấp cho agent các host tool và callback.

[<- Hướng dẫn sử dụng](./README.md)

Trong `linch_rust`, một agent là dữ liệu. Bạn viết một `AgentConfig` (một typed
dict), đưa nó cho một `Server` đang chạy, và nhận lại một handle `Agent`. Mọi việc
của một lượt đều chạy bên trong sidecar `linch-server`; nó chỉ gọi ngược vào
process của bạn cho các host tool và callback mà bạn đã khai báo.

```text
Server ─────── one linch-server child process, spoken to over stdio
  └── Agent ── opened from an AgentConfig; has a handle and a contract
        └── session id ── one conversation, a string you choose
              └── Run ── one prompt; iterate it for its envelopes
```

- Model và key nằm ở trang [Provider](./providers.md).
- Host tool, workspace tool, permission và phê duyệt nằm ở trang
  [Tool](./tools.md).
- Bắt đầu, resume, phê duyệt và hủy run nằm ở trang [Run](./runs.md).
- Kho memory và Postgres nằm ở trang [Kho lưu trữ](./stores.md).

---

## Mở một agent

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn

workspace = tempfile.mkdtemp()

config: AgentConfig = {
    "namespace": "support",
    "spec": {
        "model": MODEL,
        "system_prompt": "You answer support questions. Be brief.",
        "max_turns": 20,
        "max_output_tokens": 2048,
        "call_timeout_ms": 60_000,
        "compaction": {"threshold_tokens": 100_000, "keep_recent_turns": 6},
    },
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
    "workspace": workspace,
    "bash": False,
    "permissions": {"mode": "accept_edits", "project_root": workspace},
    "filesystem": {"offload_threshold_tokens": 20_000},
    "tasks": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("ticket-1", "Hi")
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(agent.handle, result.final_text)


asyncio.run(main())
```

```text
agent-1 Hello.
```

`Server(binary=None, *, env=None)` khởi động sidecar. `binary` ghi đè nơi tìm
server binary, còn `env` là toàn bộ môi trường của server, nơi nó đọc key của
provider (là môi trường của process này khi không truyền). Hãy dùng nó như một
async context manager: rời khỏi khối lệnh sẽ đóng đầu vào của server, server bỏ
các run của nó ngay tại chỗ đang dừng rồi thoát, và bị kill nếu sau 10 giây vẫn
chưa thoát. `Server.connect(address, token=...)` thì kết nối tới một server đang
chạy phục vụ nhiều client ([Server](./server.md#connect-from-python)); mọi thứ
bên dưới hoạt động y như vậy trên nó.

`server.open_agent(config, tools=(), callbacks=(), *, telemetry=None)` mở một
agent:

| Đối số | Ý nghĩa |
|---|---|
| `config` | `AgentConfig` bên dưới. Một dict thường vẫn chạy được lúc runtime; typed dict giúp type checker bắt lỗi chính tả của một trường |
| `tools` | Host tool tạo bằng `@tool`, chạy trong process này ([Tool](./tools.md#host-tool)) |
| `callbacks` | Intercept callback tạo bằng `@callback`, được trả lời trong process này ([bên dưới](#callback)) |
| `telemetry` | Các provider OpenTelemetry cho các run của agent này ([Observability](./observability.md)) |

Nó trả về một `Agent` có ba thuộc tính: `handle` (tên mà server dùng cho agent,
chẳng hạn `agent-1`), `contract` (một digest của những gì các run của nó phụ
thuộc vào; xem [Hợp đồng của run](#hợp-đồng-của-run)) và `skipped` ([bên
dưới](#định-nghĩa-bị-bỏ-qua)). Một server giữ nhiều agent; hãy mở chúng một lần và
dùng lại cho mọi session. `await agent.close()` giải phóng một agent trên server:
các kho lưu trữ của nó và các MCP server của nó, những MCP server này được ngắt
kết nối và được chờ cho tới khi dừng. Với kho memory, các run và session của nó sẽ
mất; với kho Postgres, chúng vẫn còn cho một agent khác có cùng config. Một agent
đã đóng sẽ raise `AgentNotFound`, và đóng một agent khi một run của nó vẫn đang
chạy sẽ raise `RunBusy`.

<a id="skipped-definitions"></a>

### Định nghĩa bị bỏ qua

Khi bật `skills` hoặc `subagents`, một thư mục skill hay một tệp subagent không
dùng được sẽ bị bỏ qua và những cái còn lại vẫn được nạp. `agent.skipped` liệt kê
từng cái dưới dạng `Skipped(path, reason, detail)`:

```python
import asyncio
import tempfile
from pathlib import Path

from linch_rust import AgentConfig, Server
from linch_rust.scripted import MODEL

root = Path(tempfile.mkdtemp()).resolve()
(root / "skills" / "notes").mkdir(parents=True)  # no SKILL.md
(root / "agents").mkdir()
(root / "agents" / "reviewer.md").write_text("Review the code.")  # no frontmatter

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": []},
    "skills": True,
    "subagents": True,
    "config_dir": str(root),
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        for skipped in agent.skipped:
            print(Path(skipped.path).relative_to(root), skipped.reason)


asyncio.run(main())
```

```text
skills/notes missing_skill_md
agents/reviewer.md missing_frontmatter
```

`reason` là một trong `missing_skill_md`, `missing_frontmatter`,
`invalid_frontmatter`, `io_error` (một symlink, hoặc một tệp không đọc được),
`name_collision_tool`, `name_collision_skill` và `name_collision` (một subagent
thứ hai trùng tên); `detail` nói chính xác điều gì sai.

Server đọc config một cách nghiêm ngặt. Một trường không xác định ở bất kỳ cấp nào
đều là `ProtocolError` (code -32602) kèm danh sách các trường nó chấp nhận, nên
một tùy chọn viết sai chính tả hoặc không được hỗ trợ sẽ không bao giờ bị bỏ sót.
Config không bao giờ chứa key của provider: server đọc key từ môi trường của nó.

## Trường cấp cao nhất

Chỉ `namespace`, `spec` và `provider` là bắt buộc. Mọi công tắc đều tắt khi không
có, trừ `bash`.

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `namespace` | bắt buộc | Không gian khóa của kho lưu trữ cho các run và session của agent này; không được để trống. Hai agent có cùng namespace trên một database Postgres sẽ thấy cùng các session |
| `spec` | bắt buộc | Model, chỉ dẫn và giới hạn của vòng lặp ([bên dưới](#spec)) |
| `provider` | bắt buộc | Provider của model, theo `id` ([Provider](./providers.md)) |
| `stores` | `{"type": "memory"}` | Nơi run và session được lưu ([Kho lưu trữ](./stores.md)) |
| `workspace` | không có: không có workspace tool | Một thư mục đã tồn tại, là gốc của `Read`, `Write`, `Edit`, `Grep`, `Glob` và `Bash` ([Tool](./tools.md#workspace-tool)). Đường dẫn tương đối được phân giải theo thư mục làm việc của server, cũng chính là của process này |
| `bash` | `True` | Workspace tool có gồm `Bash` hay không. Không dùng tới nếu không có workspace |
| `permissions` | mode `default`, project root là workspace (hoặc `/`) | Lời gọi nào chạy, hỏi hay bị từ chối ([bên dưới](#permissions)) |
| `mcp_servers` | `[]` | Các MCP server được kết nối lần lượt khi agent được mở ([bên dưới](#mcp_servers)) |
| `skills` | `False` | Tool `Skill`, với skill `verify` tích hợp sẵn và các skill trong `config_dir/skills` ([Skill](./skills.md)) |
| `subagents` | `False` | Tool `Subagent`, với subagent `verification` tích hợp sẵn và các subagent trong `config_dir/agents` ([Subagent](./subagents.md)) |
| `workers` | không có | `{"background": bool, "retain": bool}`, cả hai là `False` khi không có: worker nền và worker được giữ lại. Cần `subagents` ([Subagent](./subagents.md)) |
| `mailbox` | `False` | Một mailbox và tool `send_message` giữa các session của agent này: trong process với kho memory, bảng `linch_mailbox` với kho Postgres ([Subagent](./subagents.md)) |
| `tool_search` | không có | `{"max_tools": int, "max_skills": int}`, là 30 và 50 khi không có. Vượt quá `max_tools`, một session mới chỉ được cấp các tool luôn được nạp và `tool_search`, mà model gọi để nạp phần còn lại theo từ khóa hoặc `select:a,b` |
| `config_dir` | `.linch` | Thư mục chứa `skills/` và `agents/`. Đường dẫn tương đối được phân giải theo workspace, hoặc theo thư mục làm việc của server nếu không có workspace. Không dùng tới trừ khi `skills` hoặc `subagents` được bật |
| `filesystem` | không có | Các tool hệ thống tệp ảo và việc offload kết quả lớn ([bên dưới](#filesystem)) |
| `tasks` | `False` | Các tool task của session `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, chạy không cần phê duyệt ở mọi mode ([Task](./tasks.md)) |
| `deep_agent` | không có | Preset deep agent ([bên dưới](#deep_agent)) |
| `profile` | không có | Một vai trò miền nghiệp vụ có phiên bản cho các run của agent ([bên dưới](#profile)) |

## `spec`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `model` | bắt buộc | Model id gửi kèm mọi request; không được để trống |
| `system_prompt` | `""` | System prompt; để trống thì không gửi system block |
| `system_blocks` | `[]` | Các system block của riêng bạn, `{"text": str, "cacheable": bool}` (`cacheable` là `False` khi không có), gửi theo thứ tự trước system prompt ([bên dưới](#system-block)) |
| `max_turns` | không có: không giới hạn | Số lần gọi provider nhiều nhất mà một run được thực hiện. Một run cần thêm một lần nữa sẽ kết thúc bằng event `loop_guard` (`reason: "max_turns"`) và một `result` có subtype `error` |
| `thinking` | không có: mặc định của provider | `{"type": "disabled"}`, `{"type": "adaptive", "display"?}` hoặc `{"type": "enabled", "budget_tokens"?, "display"?}`; `display` là `summarized` hoặc `omitted`. Cách mỗi provider ánh xạ nó nằm ở [trang providers của kiến trúc](../architecture/providers.md#the-adapters) |
| `effort` | không có | Mức nỗ lực suy luận: `low`, `medium`, `high`, `xhigh` hoặc `max`, cho các provider hỗ trợ |
| `max_output_tokens` | không có: mặc định của provider | Số token nhiều nhất mà một phản hồi được sinh ra |
| `max_tool_concurrency` | `4` | Số lời gọi tool scope đọc nhiều nhất chạy cùng lúc; `0` được tính là 1 |
| `call_timeout_ms` | không có: chờ | Thời gian dài nhất một lời gọi tool được chạy, tính bằng mili giây; quá mức đó, lời gọi kết thúc bằng lỗi timeout mà model đọc được |
| `compaction` | không có: không bao giờ nén | Khi nào tóm tắt phần cũ của cuộc hội thoại ([bên dưới](#compaction)) |
| `budget` | không có: không giới hạn | Giới hạn token và USD cho mỗi run và các subagent của nó, `{"max_tokens"?, "max_cost_usd"?, "warn_ratio"?}` ([bên dưới](#budget)) |

### System block

Mỗi request gửi các system block theo thứ tự này: `system_blocks` đúng như bạn
viết, rồi `system_prompt` (không bao giờ cacheable), rồi danh sách skill khi bật
`skills` (cacheable). Một session giữ các block của run đầu tiên cho mọi request
của nó, và một subagent nhận cùng các block đó, theo sau là chỉ dẫn trong định
nghĩa của nó. Không block nào được để trống, và các block là một phần của
[hợp đồng của run](#hợp-đồng-của-run), kể cả các cờ.

Hãy đánh dấu một block là `cacheable` khi nó ổn định: provider nhận các cache
breakpoint (Anthropic) đặt system breakpoint ở block cacheable cuối cùng, nên mọi
thứ cho tới đó có thể được phục vụ từ prompt cache. Các block sau nó, chẳng hạn
system prompt khi không có danh sách skill theo sau, nằm ngoài breakpoint đó.

```python
spec = {
    "model": "claude-sonnet-4-6",
    "system_prompt": "Answer billing questions.",
    "system_blocks": [
        {"text": "Company policy: ...", "cacheable": True},
        {"text": "Today's promotions: ..."},
    ],
}
```

### `compaction`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `threshold_tokens` | không có: theo context window | Nén trước một lượt mà cuộc hội thoại được ước tính ở mức này trở lên (khoảng bốn ký tự mỗi token, 1000 mỗi ảnh); tối thiểu 1 |
| `keep_recent_turns` | `10` | Số lượt assistant được giữ nguyên văn sau bản tóm tắt; tối thiểu 1 |
| `prompt` | prompt tích hợp sẵn | Chỉ dẫn cho lời gọi tóm tắt |
| `max_output_tokens` | `4096` | Số token nhiều nhất bản tóm tắt được dùng; tối thiểu 1 |

Khi không có `threshold_tokens` (`"compaction": {}` bật tính năng này), run nén
giống như Python Linch: khi giá trị ước tính cộng với phần dự trữ cho đầu ra đạt
80% [context window](./providers.md#context-window) của provider cho model trong
spec. Phần dự trữ là `spec.max_output_tokens` nếu được đặt, nếu không thì bằng một
phần năm cửa sổ, tối thiểu 1 và tối đa 32768. `threshold_tokens` thay thế quy tắc
đó và bỏ qua cửa sổ.

Compaction thực hiện thêm một lời gọi provider trên provider và model của agent,
và run báo cáo nó bằng một [event](./events.md) `compaction`.

### `budget`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `max_tokens` | không có: không giới hạn token | Số token nhiều nhất mà run và các subagent của nó được tiêu, cộng cả bốn nhóm usage |
| `max_cost_usd` | không có: không giới hạn chi phí | Số USD nhiều nhất chúng được tiêu, tính giá từ [bảng tích hợp sẵn](./events.md#mức-sử-dụng-và-chi-phí); model không có giá thì không cộng thêm gì, nên chỉ `max_tokens` ràng buộc nó |
| `warn_ratio` | `0.9` | Tỷ lệ của một trong hai giới hạn mà tại đó một event `budget` loại `warning` được phát ra |

Giống Python Linch, mỗi lời gọi provider được tính phí một lần khi nó kết thúc, và
run kiểm tra budget trước lời gọi kế tiếp (kể cả lời gọi tóm tắt của compaction).
Một run có mức chi tiêu đã chạm giới hạn sẽ dừng tại đó với một event `budget`
loại `exceeded`, một `error` tên `BudgetExceededError` và một `result` có subtype
`error`, `stop_reason` là `error`. Run con của một subagent tính phí vào budget
của run cha, nên một budget giới hạn cả cây, và cảnh báo chỉ phát ra một lần cho
cả cây, ở run nào có lời gọi vượt qua tỷ lệ. Khi có đặt giới hạn, một lời gọi mà
usage không xác định (nó thất bại hoặc bị cắt trước khi provider báo cáo gì) nghĩa
là không còn giữ được giới hạn nữa: lời gọi kế tiếp bị từ chối với một lỗi tên
`BudgetAccountingError`. Một run bị crash giữa chừng một lời gọi sẽ resume vào
đúng lỗi đó.

Mỗi run của agent bắt đầu với mức chi tiêu bằng không. Một run được resume bắt đầu
từ mức chi tiêu mà bản ghi bền vững của nó và của các run con cho biết, nên một
lần crash không bao giờ đặt lại nó. Budget là một phần của
[hợp đồng của run](#hợp-đồng-của-run).

```python
spec = {"model": "claude-sonnet-4-6", "budget": {"max_tokens": 200_000, "max_cost_usd": 2.0}}
```

## `permissions`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `mode` | `default` | Quyết định khi không có rule nào khớp: `default` (lời gọi write và exec hỏi), `accept_edits` (lệnh ghi trong project root chạy; exec hỏi), `skip_dangerous` (mọi lời gọi đều chạy) |
| `project_root` | bắt buộc khi có `permissions` | Một đường dẫn tuyệt đối (đường dẫn tương đối bị từ chối lúc mở, khác với `workspace`) mà các đường dẫn tương đối và path pattern được phân giải theo |
| `layers` | `[]` | Các danh sách rule có tên, từ ít cụ thể nhất đến cụ thể nhất |
| `auto_allow` | `[]` | Tên các tool được cho phép ở mọi mode (các rule vẫn được áp dụng trước) |
| `path_fields` | tích hợp sẵn | Trường input nào chứa đường dẫn, theo từng tool, được thêm vào bảng tích hợp sẵn |
| `command_fields` | tích hợp sẵn | Trường input nào chứa lệnh shell, theo từng tool, được thêm vào `Bash` -> `command` |

Lời gọi scope đọc chạy ở mọi mode. Rule và layer nằm ở trang
[Tool](./tools.md#permission-rule).

## `mcp_servers`

Mỗi phần tử kết nối một MCP server khi agent được mở. Tool của nó có tên
`mcp__<name>__<tool>`; các tool mà nó có tại thời điểm đó đi vào hợp đồng của run.
Các khoảng thời gian tính bằng mili giây.

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `name` | bắt buộc | `<name>` trong `mcp__<name>__<tool>`: chữ cái ASCII, chữ số, `-`, và một dấu `_` đơn giữa chúng |
| `transport` | bắt buộc | `{"type": "stdio", "command", "args": [], "env": {}}` hoặc `{"type": "http", "url", "headers": {}}` |
| `trust_annotations` | `False` | Tool mà server đánh dấu chỉ đọc có nhận scope `read` thay vì `exec` hay không |
| `call_timeout_ms` | `300000` | Thời gian chờ dài nhất cho một lời gọi tool trước khi nó bị hủy |
| `reconnect` | xem bên dưới | Cách thử lại khi mất kết nối |

Các trường của `reconnect`: `attempts` (`5`; `0` là không bao giờ kết nối lại),
`initial_delay_ms` (`500`, nhân đôi mỗi lần), `max_delay_ms` (`30000`) và
`attempt_timeout_ms` (`30000`, gồm cả lần kết nối đầu tiên). Process của một stdio
server chỉ thấy `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` và `USER` từ môi
trường của server, cộng với `env` riêng của nó. Việc kết nối, liệt kê lại tool và
các giới hạn nằm ở [trang MCP của kiến trúc](../architecture/mcp.md). Một ví dụ
nằm ở trang [Tool](./tools.md#mcp-tool).

## `filesystem`

`{}` bật hệ thống tệp ảo: các tool `ls`, `read_file`, `write_file` và `edit_file`
trên các tệp văn bản không phải workspace, và một đoạn văn bản ngắn sau system
prompt để dạy model cách dùng chúng.

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `offload_threshold_tokens` | không có | Chuyển một kết quả tool dạng văn bản được ước tính trên số token này (bốn byte mỗi token) vào hệ thống tệp, để lại vài dòng đầu và đường dẫn của nó cho model `read_file` |
| `offload_threshold_fraction` | không có | Offload khi vượt tỷ lệ này của [context window](./providers.md#context-window) của provider, tối thiểu 1000 token; Python Linch dùng 0.1. Chỉ dùng khi không có `offload_threshold_tokens`. Không có cả hai thì kết quả không bao giờ bị offload |

Với kho memory, mỗi session có tệp riêng, mất cùng với server. Với kho Postgres,
mọi session của namespace dùng chung chúng. Xem
[Tool](./tools.md#hệ-thống-tệp-ảo).

## `deep_agent`

Preset deep agent lập kế hoạch bằng task, giao việc cho một danh sách subagent và
ghi chú vào hệ thống tệp ảo. Hành vi và các profile nằm ở trang
[Deep agent](./deep-agent.md).

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `profile` | `balanced` | Một profile tích hợp sẵn theo tên (`balanced`, `coordinator`, `unbounded`) hoặc một dict profile đầy đủ (`name`, `durable`, `coordinator`, `max_turns`, `max_tokens`, `retain_subagents`, `enable_background_subagents`, tất cả đều bắt buộc) |
| `durable` | của profile (`True` với các profile tích hợp sẵn) | Agent có cần kho Postgres hay không; một deep agent durable trên kho memory sẽ không mở được |
| `coordinator` | của profile | Agent có chạy như một coordinator hay không |

## `profile`

Domain profile là một vai trò có phiên bản. Xem [Profile](./profiles.md).

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `id` | bắt buộc | Profile id ổn định; không được để trống |
| `version` | bắt buộc | Bản sửa đổi của profile; không được để trống |
| `system_prompt` | không có | Chỉ dẫn về vai trò, đặt sau system prompt của agent |
| `tool_deny` | không có | Các tool mà lời gọi luôn bị từ chối, bất kể mode; model vẫn được cấp chúng |
| `max_turns` | không có | Thay thế `spec.max_turns`; tối thiểu 1 |

<a id="the-run-contract"></a>

## Hợp đồng của run

`agent.contract` là một digest của những gì một run phụ thuộc vào. Một run lưu nó
khi bắt đầu, và `resume` cùng `approve` từ chối một agent có contract khác
(`ContractMismatch`, code -32025). Nó bao gồm:

- `spec`, cùng những gì `profile`, `deep_agent` và `filesystem` thêm vào;
- mọi tool được đăng ký lúc mở, theo thứ tự (host tool, rồi workspace tool, task
  tool, filesystem tool và MCP tool), mỗi tool kèm name, description, schema và
  scope;
- `point`, `policy_id` và `policy_version` của từng callback, theo thứ tự khai báo;
- các định nghĩa skill và subagent được nạp lúc mở.

Nó không bao gồm namespace, các kho lưu trữ, config của provider, `timeout` của
một host tool, các lượt scripted, hay id, version và tool bị từ chối của một
profile. Bật hoặc tắt offload làm thay đổi nó; đổi ngưỡng thì không. Để đổi một
agent trong khi có các run đang chạy, hãy để chúng kết thúc trước, hoặc giữ config
cũ để resume chúng.

## Callback

Callback là một hàm bất đồng bộ mà server hỏi tại một điểm hook của một run. Khai
báo nó bằng `@callback(point, policy_id, policy_version, matcher=None,
timeout=None, fail_mode=None)` và truyền vào `open_agent(callbacks=...)`.

```python
import asyncio
from typing import Any

from linch_rust import AgentConfig, Server, ToolCallEndEvent, callback, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def lookup(email: str) -> str:
    """Find a customer by email."""
    return "Ada Lovelace"


@callback("pre_tool_use", "no-external-emails", 1, matcher=["lookup"])
async def guard(request: dict[str, Any]) -> dict[str, Any]:
    if not request["input"]["email"].endswith("@example.com"):
        return {"action": "block", "reason": "only example.com customers"}
    return {"action": "continue"}


config: AgentConfig = {
    "namespace": "support",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "lookup", {"email": "eve@other.org"}), text_turn("Sorry.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup], callbacks=[guard])
        run = await agent.run("s1", "Who is eve@other.org?")
        async for envelope in run:
            if isinstance(envelope.event, ToolCallEndEvent):
                print(envelope.event.is_error, envelope.event.result["content"][0]["text"])


asyncio.run(main())
```

```text
True only example.com customers
```

Hàm nhận dict request và trả về câu trả lời của điểm đó. Mọi request đều có
`session_id` và `run_id`:

| `point` | Request còn có | Câu trả lời |
|---|---|---|
| `pre_tool_use` | `tool_use_id`, `tool_name`, `input` | `{"action": "continue"}`, `{"action": "rewrite", "input"}`, `{"action": "block", "reason"}`, `{"action": "resolve", "result"}` |
| `permission_request` | `tool_use_id`, `tool_name`, `input`, `reason?`, `principal?` | `{"decision": "allow"}`, `{"decision": "deny", "reason"}` |
| `post_tool_use` | `tool_use_id`, `tool_name`, `input`, `result` | `{"action": "continue"}`, `{"action": "replace", "result"}` |
| `user_prompt_submit` | `content` (các content block) | `{"action": "continue"}`, `{"action": "rewrite", "content"}`, `{"action": "block", "reason"}` |
| `context_build` | `messages` (cuộc hội thoại mà model sẽ thấy) | `{"blocks"?: [{"text"}], "metadata"?: {...}}`; `metadata` dành cho observer và eval: nó nằm trên `ContextBuildEvent` của lời gọi, key của callback đến sau thắng, và model không bao giờ thấy nó |
| `stop` | `text` | `{"action": "allow"}`, `{"action": "continue", "feedback"}` |
| `post_turn` | `turn`, `stop_reason` | `{}` |

Kết quả tool trong một request hoặc một câu trả lời có dạng `{"output":
{"value", "metadata"?}}` hoặc `{"error": {"message", "code"?, "details"?}}`.

| Đối số | Mặc định | Điều khiển gì |
|---|---|---|
| `point` | bắt buộc | Một trong bảy điểm ở trên |
| `policy_id`, `policy_version` | bắt buộc | Đi vào hợp đồng của run. Hãy tăng version mỗi khi hành vi, matcher, timeout hoặc fail mode thay đổi |
| `matcher` | mọi tool | Glob tên tool (`*`, `?`), chỉ ở ba điểm tool; một lần không khớp không bao giờ đến process này |
| `timeout` | `30` giây | Server chờ câu trả lời trong bao lâu |
| `fail_mode` | `closed` ở `pre_tool_use` và `permission_request`, `open` ở các điểm còn lại | Điều xảy ra khi có exception, câu trả lời sai hoặc timeout: `closed` đưa ra câu trả lời chặn của điểm đó, `open` bỏ qua callback. `context_build` và `post_turn` không thể fail closed |

Các callback ở cùng một điểm chạy theo thứ tự khai báo. Một lần rewrite ở
`pre_tool_use` được đưa cho callback kế tiếp và được kiểm tra lại theo schema của
tool. Ở `permission_request`, một deny dừng chuỗi còn allow thì không, nên deny
đến sau thắng; nếu không có câu trả lời, lời gọi hỏi host như bình thường
([phê duyệt](./tools.md#phê-duyệt)). Một `user_prompt_submit` bị chặn sẽ kết thúc
run bằng `PromptBlocked` trước khi bất kỳ thứ gì được ghi. Mỗi lời gọi xuất hiện
dưới dạng một [event](./events.md) `hook` chỉ có trong stream.

## Session

Session là một chuỗi id bất kỳ. Run đầu tiên trên một id sẽ tạo ra nó; các run sau
trên cùng id tiếp tục cuộc hội thoại với toàn bộ lịch sử của nó. Một session có
nhiều nhất một run chưa kết thúc ([Run](./runs.md#một-run-đang-chạy-cho-mỗi-session)).

`await agent.close_session(session_id)` báo cho agent rằng bạn tạm xong với một
session: các workspace tool quên những gì chúng đã đọc (nên một lệnh ghi sau đó
phải đọc lại tệp), và với kho memory, các tệp ảo của nó bị bỏ. Không có gì bền
vững bị xóa; session và các run của nó vẫn còn.

## Các trang liên quan

- [Bắt đầu nhanh](./quickstart.md): cùng các cấu trúc này, từng bước.
- [Provider](./providers.md): mọi config `provider`.
- [Tool](./tools.md): host tool, workspace tool, permission, phê duyệt.
- [Run](./runs.md): bắt đầu, stream, resume, phê duyệt, hủy.
