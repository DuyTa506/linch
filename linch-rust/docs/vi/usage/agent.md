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
| `workspace` | không có: không có workspace tool | Một thư mục đã tồn tại, là gốc của `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash`, `BashOutput` và `BashKill` ([Tool](./tools.md#workspace-tool)). Đường dẫn tương đối được phân giải theo thư mục làm việc của server, cũng chính là của process này |
| `bash` | `True` | Workspace tool có gồm `Bash`, `BashOutput` và `BashKill` hay không. Không dùng tới nếu không có workspace |
| `execution` | `local`: `Bash` chạy với tư cách người dùng của server | Nơi `Bash` chạy: một `SandboxExecution` (Landlock, seccomp, rlimit), một `DockerExecution` ([Sandbox](./sandbox.md)) hoặc một `RemoteExecution` chạy lệnh trong client của bạn ([Thực thi từ xa](./remote-execution.md)). Là một phần của hợp đồng run trừ khi là `local` |
| `ask_user` | `False` | Model có `AskUser` hay không; tool này tạm dừng run cho đến khi bạn trả lời ([Tool](./tools.md#hỏi-người-dùng)) |
| `schedule_tools` | `False` | Model có `CreateSchedule`, `ListSchedules` và `CancelSchedule` hay không; các tool này lập lịch các run của agent này; cần một tenant có `max_schedules` ([Scheduler](./scheduler.md#để-model-lập-lịch)) |
| `memory` | không có | Bộ nhớ dài hạn trong store: model có `MemorySave`, `MemorySearch` và `MemoryDelete`, và có thể được cho xem các kết quả khớp nhất trước mỗi request, `{"scope"?, "recall"?, "max_memories"?, "max_memories_per_scope"?}` ([Bộ nhớ](./memory.md#bộ-nhớ-dài-hạn-có-sẵn)) |
| `permissions` | mode `default`, project root là workspace (hoặc `/`) | Lời gọi nào chạy, hỏi hay bị từ chối ([bên dưới](#permissions), [Bảo mật](./security.md#permission-modes)) |
| `mcp_servers` | `[]` | Các MCP server được kết nối lần lượt khi agent được mở ([bên dưới](#mcp_servers)) |
| `skills` | `False` | Tool `Skill`, với skill `verify` tích hợp sẵn và các skill trong `config_dir/skills` ([Skill](./skills.md)) |
| `subagents` | `False` | Tool `Subagent`, với subagent `verification` tích hợp sẵn và các subagent trong `config_dir/agents` ([Subagent](./subagents.md)) |
| `workers` | không có | `{"background": bool, "retain": bool}`, cả hai là `False` khi không có: worker nền và worker được giữ lại. Cần `subagents` ([Subagent](./subagents.md)) |
| `mailbox` | `False` | Một mailbox và tool `send_message` giữa các session của agent này: trong process với kho memory, bảng `linch_mailbox` với kho Postgres ([Subagent](./subagents.md)) |
| `tool_search` | không có | `{"max_tools": int, "max_skills": int}`, là 30 và 50 khi không có. Vượt quá `max_tools`, một session mới chỉ được cấp các tool luôn được nạp và `tool_search`, mà model gọi để nạp phần còn lại theo từ khóa hoặc `select:a,b` |
| `config_dir` | `.linch` | Thư mục chứa `skills/` và `agents/`. Đường dẫn tương đối được phân giải theo workspace, hoặc theo thư mục làm việc của server nếu không có workspace. Không dùng tới trừ khi `skills` hoặc `subagents` được bật |
| `filesystem` | không có | Các tool hệ thống tệp ảo và việc offload kết quả lớn ([bên dưới](#filesystem)) |
| `tasks` | `False` | Các tool task của session `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, chạy không cần phê duyệt ở mọi mode ([Task](./tasks.md)) |
| `task_ledger` | không có | Một danh sách task dùng chung cho mọi session, hiện kèm mỗi request: `{"list", "max_chars"?}`; cần `tasks` ([Task](./tasks.md#một-ledger-tồn-tại-qua-các-session)) |
| `deep_agent` | không có | Preset deep agent ([bên dưới](#deep_agent)) |
| `profile` | không có | Một vai trò miền nghiệp vụ có phiên bản cho các run của agent ([bên dưới](#profile)) |
| `max_provider_concurrency` | không có: không giới hạn | Số lời gọi provider tối đa mà các run của nó, và của các agent trên cùng key và endpoint provider, thực hiện cùng lúc; phần còn lại chờ đến lượt ([Provider](./providers.md#limit-concurrent-calls)). Không thuộc hợp đồng của run |
| `pricing` | không có: bảng có sẵn | USD cho mỗi triệu token theo đúng model id, cho model mà bảng không có hoặc để thay giá của nó ([Giám sát](./monitoring.md#chi-phí-được-tính-thế-nào)) |

## `spec`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `model` | bắt buộc | Model id gửi kèm mọi request; không được để trống |
| `system_prompt` | `""` | System prompt; để trống thì không gửi system block |
| `system_blocks` | `[]` | Các system block của riêng bạn, `{"text": str, "cacheable": bool}` (`cacheable` là `False` khi không có), gửi theo thứ tự trước system prompt ([bên dưới](#system-block)) |
| `system` | không có | Các phần system prompt có tên, văn bản thêm vào sau prompt, và việc có bỏ văn bản prompt của các preset hay không, `{"sections"?, "append"?, "replace_defaults"?}`, là `SystemPromptConfig` của Python ([bên dưới](#system-sections)) |
| `context_budget` | không có: không giới hạn | Mức trần cho context mà các callback `context_build` thêm vào một request và những gì bị bỏ trước, `{"max_tokens", "drop_order"?, "pinned"?}`, là `ContextBudget` của Python ([Memory](./memory.md#context-budget)) |
| `max_turns` | không có: không giới hạn | Số lần gọi provider nhiều nhất mà một run được thực hiện. Một run cần thêm một lần nữa sẽ kết thúc bằng event `loop_guard` (`reason: "max_turns"`) và một `result` có subtype `error`. [Loop guard](#loop_guard) kết thúc một run bị kẹt mà không cần nó |
| `thinking` | không có: mặc định của provider | `{"type": "disabled"}`, `{"type": "adaptive", "display"?}` hoặc `{"type": "enabled", "budget_tokens"?, "display"?}`; `display` là `summarized` hoặc `omitted`. Cách mỗi provider ánh xạ nó nằm ở [trang providers của kiến trúc](../architecture/providers.md#the-adapters) |
| `effort` | không có | Mức nỗ lực suy luận: `low`, `medium`, `high`, `xhigh` hoặc `max`, cho các provider hỗ trợ |
| `max_output_tokens` | không có: mặc định của provider | Số token nhiều nhất mà một phản hồi được sinh ra |
| `temperature` | không có: mặc định của provider | Temperature lấy mẫu, từ 0 đến 2 (`0` được phép; `anthropic` nhận tối đa 1). Giá trị ngoài khoảng làm `agent.open` thất bại. Provider nào bỏ qua hoặc từ chối nó có trong [Providers](./providers.md#temperature) |
| `max_tool_concurrency` | `4` | Số lời gọi tool scope đọc nhiều nhất chạy cùng lúc; `0` được tính là 1 |
| `call_timeout_ms` | không có: chờ | Thời gian dài nhất một lời gọi tool được chạy, tính bằng mili giây, tối thiểu 100 (`0` và giá trị nhỏ hơn làm `agent.open` thất bại: mọi lời gọi sẽ hết giờ ngay); quá mức đó, lời gọi kết thúc bằng lỗi timeout mà model đọc được |
| `compaction` | không có: không bao giờ nén | Khi nào tóm tắt phần cũ của cuộc hội thoại ([bên dưới](#compaction)) |
| `budget` | không có: không giới hạn | Giới hạn token và USD cho mỗi run và các subagent của nó, `{"max_tokens"?, "max_cost_usd"?, "warn_ratio"?}` ([bên dưới](#budget)) |
| `provider_retry` | bật: 5 lần thử, từ 1 giây gấp đôi đến 30 giây, jitter 0.2 | Gọi lại cùng model sau một lỗi có thể thử lại, `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "jitter"?}`, là các mặc định của Python; `{"max_attempts": 1}` tắt nó ([providers](./providers.md#lỗi-và-thử-lại)) |
| `fallback_models` | `[]` | Các model để chuyển sang, theo thứ tự, khi một model hết lượt thử lại; run giữ model mà nó đã chuyển sang ([providers](./providers.md#lỗi-và-thử-lại)) |
| `output_schema` | không có | Một JSON Schema mà câu trả lời cuối cùng phải thỏa, `{"name", "schema", "strict"?, "description"?}`; câu trả lời đã parse là `result.structured_output` ([Structured output](./structured-output.md)) |
| `tool_choice` | không có: mặc định của provider, `auto` | Model có phải gọi tool hay không và tool nào, `{"type": "auto" \| "none" \| "required"}` hoặc `{"type": "tool", "name"}`; bị từ chối khi open nếu nó nêu một tool mà agent không đưa ra hoặc provider từ chối nó ([Structured output](./structured-output.md#choose-which-tool-the-model-calls)) |
| `final_tool_name` | không có | Một tool đã đăng ký mà lời gọi của nó kết thúc run, input của nó là câu trả lời, mà không chạy ([Structured output](./structured-output.md#name-your-own-final-tool)) |
| `structured_output_retries` | `0` | Bao nhiêu câu trả lời không thỏa `output_schema` được gửi lại cho model trước khi run thất bại; cũng giới hạn schema riêng của một run, nên có thể đặt mà không cần `output_schema` |
| `redaction` | không có | Các regex rule che response, kết quả tool và tùy chọn cả prompt trước khi bất cứ thứ gì ghi log, stream hay nhìn thấy chúng, `{"rules": [{"pattern", "replacement"?}], "response"?, "tool_results"?, "prompt"?}` ([Bảo mật](./security.md#redaction)) |
| `verification` | không có | Các kiểm tra mà câu trả lời cuối cùng phải qua, và ngân sách retry mà chúng dùng chung với các callback `stop`, `{"max_retries"?, "verifiers"?}` ([Bảo mật](./security.md#verifiers)) |
| `truncation_recovery` | không có: câu trả lời bị cắt là câu cuối | Tiếp tục một câu trả lời văn bản bị giới hạn đầu ra cắt, `{"max_attempts"?, "feedback"?}` ([Memory](./memory.md#cut-off-answers)) |
| `loop_guard` | bật: 3 lời gọi giống nhau với cùng kết quả, 10 input giống nhau, 3 batch lỗi, 25 lượt không có lời gọi | Kết thúc một run bị kẹt, `{"max_identical_tool_calls"?, "max_identical_inputs"?, "max_consecutive_failures"?, "max_idle_turns"?}`, là `LoopGuard` của Python ([bên dưới](#loop_guard)) |
| `policy` | không có: không kiểm tra gì | Các tool chỉ được chạy sau khi những tool khác đã thành công, và điều một câu trả lời cuối cùng phải thỏa, `{"prerequisites"?, "gates"?, "max_corrections"?}` ([bên dưới](#policy)) |
| `tool_retry` | không có: lời gọi lỗi không được chạy lại | Chạy lại một tool sau kết quả lỗi có `code` nằm trong danh sách, chỉ với tool tự khai là idempotent, `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "retry_on"?, "tools"?}` ([Tool](./tools.md#tool_retry)) |
| `tool_cache` | không có: mọi lời gọi đều chạy | Trả một lời gọi giống hệt của tool đọc có tên từ cache, `{"tools", "ttl_ms"?, "max_entries"?, "max_value_bytes"?, "invalidate_on_write"?, "scope"?}` ([Tool](./tools.md#tool_cache)) |
| `tool_result_cap` | không có: kết quả đi vào nguyên vẹn | Cắt một kết quả tool nặng hơn `max_tokens` token ước lượng thành phần đầu và đuôi quanh một marker trong view của model, giữ bản đầy đủ trong log, và thêm tool `ReadToolResult`, `{"max_tokens", "head_fraction"?}` ([Tool](./tools.md#long-tool-results)) |

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

<a id="system-sections"></a>

### Các system section

`spec.system` dựng system prompt từ các lớp có tên, là `SystemPromptConfig` của
Python Linch:

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `sections` | `[]` | Các lớp có tên, mỗi lớp `{"name", "text", "order"?, "cacheable"?}`. `name` dành cho operator và không bao giờ được gửi; nó là duy nhất và không được để trống, `text` cũng vậy. `order` (mặc định `0`) sắp xếp các section, thấp nhất trước, khi bằng nhau thì giữ thứ tự bạn viết. `cacheable` mặc định là `True` |
| `append` | không có | Văn bản được gửi làm system block cuối cùng, sau prompt và danh sách skill, không bao giờ cacheable |
| `replace_defaults` | `False` | Bỏ văn bản prompt mà các preset thêm vào `system_prompt`: chỉ dẫn của deep agent, ghi chú của các filesystem tool và `system_prompt` của một profile. Core không có prompt mặc định của riêng nó, nên đây chính là các mặc định ở đây |

Một request gửi: các section theo `order`, rồi `system_blocks`, rồi
`system_prompt` (cùng những gì các preset đã thêm vào), rồi danh sách skill khi
bật `skills`, rồi `append`. Một subagent nhận cùng những thứ đó, theo sau là chỉ
dẫn trong định nghĩa của nó. Các section là một phần của [hợp đồng của
run](#hợp-đồng-của-run): một run không được resume bởi một agent có section khác,
và một spec không có chúng giữ nguyên contract như trước đây.

Provider nhận một cache breakpoint (Anthropic) đặt nó ở block cacheable cuối
cùng, nên mọi thứ trước đó cũng được cache. Vì vậy một section không cacheable
không được đứng trước một section cacheable hay một trong các `system_blocks`
của bạn; agent từ chối mở (`BuildError`) thay vì cache văn bản bạn đã đánh dấu là
hay thay đổi. Hãy cho section đó một `order` cao hơn.

`append` của Python Linch thay thế `system_prompt`; ở đây nó được thêm vào, nên
văn bản mà một preset đặt vào prompt không bị mất. `placement` của Python (trước
các mặc định, sau chúng, sau block môi trường) không có tương ứng: không có block
môi trường, và `order` chỉ đặt một section so với các section khác, tất cả đều đi
trước `system_blocks` và prompt của bạn.

```python
spec = {
    "model": "claude-sonnet-4-6",
    "system_prompt": "Answer billing questions.",
    "system": {
        "sections": [
            {"name": "policy", "text": "Company policy: ...", "order": 1},
            {"name": "tone", "text": "Be brief.", "order": 2},
        ],
        "append": "Today's promotions: ...",
    },
}
```

Một tenant từ xa được có tối đa 16 section, mỗi section tối đa 16 KiB (tên tối đa
64 byte) và tổng cộng 64 KiB cùng `append`, vì chúng đi kèm mọi request
([Server](./server.md#những-gì-admission-từ-chối-và-lý-do)). Operator có thể thêm
section riêng của mình vào mọi agent của một tenant; chúng được gửi cùng với của
tenant, không thể bị gỡ hay đổi tên, và `replace_defaults` để chúng lại. Thay đổi
chúng làm thay đổi contract của các agent của tenant, nên các run đang chạy của
chúng bị từ chối khi resume cho đến khi kết thúc.

### `compaction`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `threshold_tokens` | không có: theo context window | Nén trước một lượt mà cuộc hội thoại có số đo ở mức này trở lên: ước lượng (khoảng bốn ký tự mỗi token, 1000 mỗi ảnh), hoặc số token prompt provider báo gần nhất cộng với ước lượng phần phát sinh sau đó, lấy số lớn hơn ([Memory](./memory.md#compaction)); tối thiểu 1 |
| `keep_recent_turns` | `10` | Số lượt assistant được giữ nguyên văn sau bản tóm tắt; tối thiểu 1 |
| `prompt` | prompt tích hợp sẵn | Chỉ dẫn cho lời gọi tóm tắt |
| `max_output_tokens` | `4096` | Số token nhiều nhất bản tóm tắt được dùng; tối thiểu 1 |
| `micro` | `False` | Xóa các kết quả tool cũ trước khi tóm tắt, không có lời gọi model ([Memory](./memory.md#micro-compaction)) |
| `max_forced` | `3` | Số bản tóm tắt nhiều nhất một run tạo ra vì provider từ chối một prompt là quá dài so với context window; `0` là không bao giờ ([Memory](./memory.md#when-the-prompt-does-not-fit)) |
| `keep_user_tokens` | `15000` | Số token của các message user mà bản tóm tắt gộp đi, được checkpoint mang nguyên văn theo thứ tự mới nhất trước, do harness sao chép; `0` là không mang gì. Các block nguyên văn cũng co lại cho vừa: toàn bộ checkpoint nhỏ hơn phần nó thay thế, nếu không bản tóm tắt bị từ chối |

Khi không có `threshold_tokens` (`"compaction": {}` bật tính năng này), run nén
giống như Python Linch: khi giá trị ước tính cộng với phần dự trữ cho đầu ra đạt
80% [context window](./providers.md#context-window) của provider cho model trong
spec. Phần dự trữ là `spec.max_output_tokens` nếu được đặt, nếu không thì bằng một
phần năm cửa sổ, tối thiểu 1 và tối đa 32768. `threshold_tokens` thay thế quy tắc
đó. Nó phải thấp hơn cửa sổ nếu cửa sổ đã biết (được cấu hình, do server báo, hoặc có trong
bảng của adapter; mặc định 128000 của model không rõ chỉ là phỏng đoán và không được kiểm
tra): `agent.open` thất bại với `-32011`,
`compaction.threshold_tokens (N) must be below the context window (W)`.

Compaction thực hiện thêm một lời gọi provider trên provider và model của agent,
và run báo cáo nó bằng một [event](./events.md) `compaction`. Khi compaction bật, một prompt bị
provider từ chối vì quá dài sẽ được rút ngắn và gửi lại; khi tắt, lần từ chối kết thúc run.
[Memory](./memory.md) nói về cả hai, và về bộ nhớ ngoài.

### `loop_guard`

Loop guard kết thúc một run bị kẹt, trước lời gọi provider kế tiếp, kể cả khi
`max_turns` chưa đặt. Nó bật trừ khi bạn thay đổi; chỉ đặt những trường cần đổi.

| Trường | Mặc định | Run kết thúc khi |
|---|---|---|
| `max_identical_tool_calls` | `3` | Cùng một lời gọi nhận cùng kết quả chừng này lần trong 20 kết quả gần nhất: một tool, cùng input, cùng kết quả, không phải lỗi. Lời gọi chạm tới ngưỡng vẫn chạy trước. Một lời gọi có kết quả đã đổi (một file đọc lại sau khi sửa) là có tiến triển và không được đếm, và một lời gọi cách xa từ lâu trong một run dài khỏe mạnh cũng vậy. Một kết quả đã được chuyển sang filesystem (offload kết quả lớn) được tính theo nội dung của nó, bất kể tên file. Một lời gọi write hay exec thành công với một input mới (xem dòng kế tiếp) cũng xóa bộ đếm này của mọi lời gọi khác: một lần chạy test in cùng một lỗi sau mỗi lần trong mười bản vá khác nhau không phải là lần lặp, còn cùng một lời gọi với cùng kết quả mà không có bản vá nào ở giữa vẫn kết thúc run ở lần thứ ba. `0` tắt kiểm tra này |
| `max_identical_inputs` | `10` | Một tool được gọi với cùng input chừng này lần trong 50 lời gọi gần nhất, bất kể kết quả và bất kể có lỗi hay không: luật của Python đếm cái này trên toàn run với ngưỡng của `max_identical_tool_calls`; ở đây cách đếm nới hơn (10, trong một cửa sổ), và một lời gọi write hay exec thành công với một input mới (không nằm trong 50 lời gọi như vậy gần nhất) xóa bộ đếm của mọi lời gọi khác, cả input giống nhau lẫn kết quả giống nhau (một lần sửa đổi thứ mà lần chạy test kế tiếp tìm thấy), nên một run sửa-và-test dài không bị cắt. Cùng một lời gọi, hay một chu trình các lời gọi write hoặc exec, chạy đi chạy lại mà không có lần sửa mới nào ở giữa vẫn được đếm. Nó chặn một vòng lặp mà kết quả đổi mỗi lần (một timestamp) hoặc luân phiên giữa vài lời gọi. Một tool poll không được đếm. `0` tắt kiểm tra này; không có trên wire khi bằng `10` |
| `max_consecutive_failures` | `3` | Chừng này batch tool liên tiếp mà mọi lời gọi đều lỗi. Một batch có một lời gọi thành công thì đếm lại từ đầu. `0` tắt kiểm tra này |
| `max_idle_turns` | `25` | Thêm một lượt liên tiếp nữa sẽ kết thúc mà không có lời gọi provider, điều mà chỉ một callback `before_provider_call` trả lời `force_continue` mới gây ra. Tối thiểu `1`: kiểm tra này luôn bật |

Một run kích hoạt nó sẽ stream một event `loop_guard` (`reason`
`repeated_tool_call`, `repeated_failures` hoặc `idle_turns`), một `error` tên
`LoopGuardError` và một `result` có subtype `error`, rồi kết thúc `failed`. Các con
số được đọc lại từ log của run khi resume, nên một sự cố không đặt lại chúng.

```python
spec = {"model": "deepseek-flash", "loop_guard": {"max_identical_tool_calls": 5}}
```

Python Linch đếm các input giống nhau bất kể kết quả, kể cả lời gọi lỗi. Ở đây một
lần lặp còn phải trả về cùng kết quả, một lời gọi lỗi thuộc về số lần lỗi (Linch bảo
model lặp lại một số lời gọi bị từ chối, một tool bị hoãn). Một tool poll được chờ chứ không bị đếm là lần
lặp: `BashOutput` có thể đọc một lệnh nền đang chạy bao nhiêu lần model cần, miễn là
câu trả lời còn đổi; cùng một câu trả lời `max_idle_turns` lần liên tiếp thì run kết
thúc (một lệnh không in gì và không bao giờ xong), và đọc lại một lệnh đã xong là một
lần lặp ngay (`BashOutput` đánh dấu nó đã xong trong metadata của kết quả). Một [host
tool](./tools.md#host-tool) khai báo `polls=True`, và một [MCP
server](#mcp_servers) liệt kê các tool poll của nó trong `polls`; kết quả của chúng báo
một lần poll đã xong bằng metadata `{"poll_done": true}`.
`force_final_answer` của Python không có. Một tenant từ xa không được nâng
`max_idle_turns` quá 25.

Giá trị mặc định không được ghi vào spec, nên bỏ `loop_guard` đi thì giữ nguyên
[hợp đồng của run](#hợp-đồng-của-run) của một agent từ trước khi có guard, và các run
của nó vẫn resume được; mọi giá trị khác đều đổi hợp đồng.

Guard không bắt được gì. Nó đếm một input nhiều nhất 10 lần trong 50 kết quả gần nhất, nên
một chu trình hơn 5 lời gọi khác nhau mà kết quả đổi mỗi lần (mỗi lần một timestamp khác, chẳng
hạn) không bao giờ đạt 10 trong 50 và không bị bắt: chỉ `max_turns` và [budget](#budget)
kết thúc một run như vậy, và cả hai mặc định đều không có. Một tool `polls` của
[MCP](#mcp_servers) mà câu trả lời đổi ở mọi lần gọi (một thời gian đã trôi trong dòng trạng
thái) nằm ngoài mọi bộ đếm, nên chỉ `max_turns` và budget giới hạn nó. Hãy đặt `max_turns`
`max_turns` (và một budget) cho một run phải kết thúc.

Trên một server có tenant, đây là quy tắc chứ không phải lời khuyên: `agent.open`
của một tenant bị từ chối (`-32004`, `kind: run_bound`) khi spec không có
`max_turns` lẫn một `budget` có `max_tokens` hoặc `max_cost_usd` (`max_turns` của
`profile` và của profile deep agent được tính), và thông báo nêu tên cả hai thiết lập
([Server](./server.md#những-gì-admission-từ-chối-và-lý-do)). Một kết nối local
tin cậy vẫn giữ mặc định không giới hạn.

### `policy`

`spec.policy` nêu điều một run phải làm theo thứ tự: tool nào chỉ được chạy sau khi
những tool khác đã thành công (prerequisite), và run phải làm xong những gì trước
khi được kết thúc (completion gate). Khi không đặt, không có gì được kiểm tra.

```python
spec = {
    "model": "deepseek-flash",
    "policy": {
        "prerequisites": [{"tool": "Edit", "requires": ["Read"]}],
        "gates": [
            {"name": "tested", "succeeded": ["Test"]},
            {"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}},
        ],
        "max_corrections": 2,
    },
}
```

**Prerequisite.** Một lời gọi `tool` được trả lời bằng một kết quả lỗi nêu tên thứ cần
chạy trước, và không chạy, cho tới khi mọi tool trong `requires` đã thành công trong
một batch tool trước đó của run. Các lời gọi trong cùng một batch không bao giờ mở
khóa cho nhau. Một lời gọi được tính là thành công khi kết quả của nó không phải lỗi:
một lời gọi bị ngắt, một lời gọi bị từ chối và một lần khởi động `run_in_background`
(chỉ xác nhận) thì không. Việc từ chối diễn ra trước các callback `pre_tool_use` của
lời gọi và trước permission, nên một lời gọi bị từ chối không hỏi ai phê duyệt. Run
ghi một event `policy` (`rule` là `prerequisite`, `action` là `refuse`) một lần cho
mỗi lời gọi bị từ chối (tính theo response và `tool_use_id`, nên một provider dùng lại
một id ở lượt sau vẫn có event mới). Thông báo gửi model chỉ nêu tên các tool cần chạy
trước khi run đang cung cấp chúng ở lượt này; với một tool bị `allowed_tools` của skill
ẩn đi, hoặc bị tool search hoãn, nó chỉ nói rằng một prerequisite chưa thành công
(`missing` của event vẫn liệt kê chúng).

Một prerequisite là công cụ giúp model làm đúng thứ tự, **không phải ranh giới bảo
mật**. Nó bảo vệ các lời gọi của một run; một tool có thể khởi động công việc khác
(`Bash`, một subagent) làm được điều mà một tool bị từ chối sẽ làm. Một lời gọi được
tính chỉ theo tên tool và kết quả đã render của nó: một host tool của client cùng tên,
một callback `pre_tool_use` trả lời lời gọi, một callback `post_tool_use` thay kết quả
và một cache hit đều được tính là thành công. Hãy giữ
[permission và sandbox](./security.md) cho bảo mật. `ToolPrerequisite` của Python
Linch là cùng một quy tắc; ở đây các lời gọi của chính một subagent nhận các
prerequisite của parent trong run của nó.

Một batch bị từ chối thất bại như mọi batch khác: [loop guard](#loop_guard) đếm một
batch mà mọi lời gọi đều bị từ chối là một batch lỗi, nên một model bỏ qua thông báo
ba lần liên tiếp sẽ làm run kết thúc.

**Gate.** Một câu trả lời cuối cùng (văn bản, hoặc input của final tool) được kiểm
tra trước output schema, các verifier và các callback `stop`. Một gate được thỏa khi
mỗi tool trong `succeeded` đã thành công `min_calls` lần (mặc định 1) trong run, và
câu trả lời qua được `check`, một trong các [kiểm tra của verifier](./security.md#verifiers).
Khi một gate không đạt, run tiếp tục: câu trả lời được gửi lại cho model dưới dạng một
system reminder nêu những gì còn thiếu, một lượt sửa cho lần không đạt đó, trả lời
cùng lúc mọi gate không đạt. Câu mặc định của một gate `check` chỉ nói câu trả lời
không thỏa yêu cầu: nó không hiện pattern hay keyword, vốn thuộc về operator. Hãy đặt
`feedback` để nói với model cần sửa gì; nó được hiện nguyên văn. Một lần sửa là một event
`policy` (`rule` là `completion`, `action` là `correct`, `attempt` là số lần sửa đã
dùng). Khi `max_corrections` (2 nếu vắng, tính chung cho mọi gate) đã dùng hết, lần
không đạt kế tiếp kết thúc run `failed`: một event `policy` có `action` là `stop`, một
`error` tên `PolicyGateError` và một `result` có subtype `error` mang câu trả lời cuối
cùng. Một gate không bao giờ cho câu trả lời đi qua, khác với một verifier, thứ chấp
nhận câu trả lời khi các retry của nó đã hết. `max_corrections: 0` kết thúc run ở lần
không đạt đầu tiên.

Input của final tool được xét như văn bản JSON của nó và như từng chuỗi nó chứa (key và
value, ở mọi độ sâu), vì văn bản JSON escape những gì một pattern có thể nêu: một check
`forbids` không đạt khi nó khớp bất kỳ cái nào, một check `matches` hay `keywords` khi
nó không đạt trên cái nào. Khi một callback `stop` viết lại câu trả lời, các gate xét
lại bản viết lại; không đạt ở đó kết thúc run ngay với một event `policy` stop và không
có lần sửa nào, vì hook đã viết lại một lần sẽ viết lại cả câu trả lời kế tiếp.

Một `tool_choice` có hiệu lực mọi lượt không đi cùng được với các rule mà nó khiến không
bao giờ thỏa: `none` hoặc một tool được nêu tên với prerequisite hay gate cần tool
`succeeded`, `required` hoặc một tool được nêu tên với bất kỳ rule nào trừ khi run có
final tool để kết thúc (`required` cùng final tool thì được; chính final tool chỉ với
các gate kiểm tra câu trả lời). `agent.open` từ chối agent đó, và một run mang
`tool_choice` riêng thất bại với `InvalidToolChoice` trước khi ghi gì.

Một lượt sửa có một lời gọi provider và không chạy tool nào, nên loop guard không đếm
nó là lượt rảnh hay lượt lặp; `max_turns` và budget giới hạn nó như mọi lượt khác. Gate
không được kiểm tra trên các run kết thúc theo cách khác (giới hạn lượt, budget, một
lỗi).

`agent.open` từ chối một policy không thể chạy (`OpenError`, code -32011): một tool mà
agent không có, final tool (lời gọi của nó không bao giờ chạy, nên không bao giờ có thể
thành công), một vòng lặp prerequisite, hai mục cho cùng một tool, một gate không có cả
`succeeded` lẫn `check`, một tên gate lặp hoặc trống, hoặc một `check` không biên dịch
được. `policy: {}` được phép và không làm gì, nhưng nó được đặt, nên nó đổi hợp đồng.

Không có gì được checkpoint. Các lần thành công, các lần từ chối đã thực hiện và các
lần sửa đã dùng được đọc lại từ log của run khi resume, nên một sự cố ở bất kỳ lần ghi
nào cũng không thể bỏ qua một gate, dùng một lần sửa hai lần hay báo một lần từ chối hai
lần. Các run của một subagent nhận các prerequisite, không nhận gate (nó trả lời parent
bằng văn bản, với các tool riêng của nó). Policy là một phần của
[hợp đồng của run](#hợp-đồng-của-run): bỏ nó đi thì giữ nguyên hợp đồng của một agent
từ trước khi có policy, và mọi policy đều đổi hợp đồng. Một tenant từ xa được thêm vào
policy mà operator của nó yêu cầu; các rule đó được giữ, nhưng chúng ràng buộc thứ tự
lời gọi của model, không ràng buộc host của tenant
([Server](./server.md#những-gì-admission-từ-chối-và-lý-do)).

### `budget`

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `max_tokens` | không có: không giới hạn token | Số token nhiều nhất mà run và các subagent của nó được tiêu, cộng cả bốn nhóm usage |
| `max_cost_usd` | không có: không giới hạn chi phí | Số USD nhiều nhất chúng được tiêu, tính giá từ [bảng tích hợp sẵn](./events.md#mức-sử-dụng-và-chi-phí); mọi model có thể phục vụ một lời gọi (`model` và từng model trong `fallback_models`) cần có giá (bảng tích hợp sẵn hoặc `pricing` của agent), nếu không `agent.open` thất bại với lỗi agent không hợp lệ (`-32011`) nêu tên model không có giá; với model không có giá, hãy đặt giới hạn `max_tokens` |
| `warn_ratio` | `0.9` | Tỷ lệ của một trong hai giới hạn mà tại đó một event `budget` loại `warning` được phát ra |

Giống Python Linch, mỗi lời gọi provider được tính phí một lần khi nó kết thúc, và
run kiểm tra budget trước lời gọi kế tiếp (kể cả lời gọi tóm tắt của compaction).
Một run có mức chi tiêu đã chạm giới hạn sẽ dừng tại đó với một event `budget`
loại `exceeded`, một `error` tên `BudgetExceededError` và một `result` có subtype
`error`, `stop_reason` là `error`. Run con của một subagent tính phí vào budget
của run cha, nên một budget giới hạn cả cây, và cảnh báo chỉ phát ra một lần cho
cả cây, ở run nào có lời gọi vượt qua tỷ lệ. Khi có đặt giới hạn, một lời gọi mà
usage không xác định (nó không nhận được câu trả lời, hoặc bị cắt sau khi output đã
bắt đầu, trước khi provider báo cáo gì) nghĩa là không còn giữ được giới hạn nữa:
lời gọi kế tiếp, kể cả một lần [thử lại](./providers.md#lỗi-và-thử-lại), bị từ chối
với một lỗi tên `BudgetAccountingError`. Một lời gọi thất bại trước khi tốn bất cứ
thứ gì (không kết nối được, hoặc status 4xx, 503 hay 529 trước khi có output) được tính bằng
không và vẫn được thử lại như thường; một 500, 502 hay 504 có thể đã bị tính tiền ở
upstream, nên nó được tính là không rõ. Một run bị crash giữa chừng một lời gọi sẽ
resume vào đúng lỗi đó. Giới hạn chi phí cần mọi lời gọi đều có giá: một lời gọi có
usage mà không có giá (provider phục vụ một model mà `pricing` không có đơn giá,
kể cả [model dự phòng](./providers.md#lỗi-và-thử-lại)) làm lời gọi kế tiếp bị từ chối
với một `BudgetAccountingError` nói rõ điều đó. Chỉ có giới hạn token thì không bị
ảnh hưởng. Giá được tra theo id mà provider báo cáo, nếu không thì `spec.model`, nên
hãy khóa `pricing` theo các id mà nó báo cáo.

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
| `polls` | `[]` | Các tool của server, theo tên server liệt kê, poll một thứ đang chạy: loop guard chờ các câu trả lời lặp lại của chúng, tối đa `max_idle_turns` câu giống nhau liên tiếp, thay vì kết thúc run ở lần thứ ba. Một kết quả MCP không có metadata để báo việc chờ đã xong |
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
| `memory` | `False` | Cho model thấy chỉ dẫn lưu ký ức dưới `/memories/` và 200 dòng đầu (tối đa 16 KiB) của `MEMORY.md`, bao khung như dữ liệu không đáng tin, đọc lại cho mỗi request ([Memory](./memory.md#file-memory)). Deep agent luôn có |
| `memory_scope` | `"principal"` | `/memories/` của ai mà model thấy: `"principal"` cho mỗi principal một thư mục riêng (`/memories/<key>/MEMORY.md`), `"namespace"` một tệp cho mọi run của namespace ([Memory](./memory.md#file-memory)) |
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
  `system` và `context_budget` của nó được tính khi được đặt, và không được đưa lên
  wire (để digest không đổi) khi không đặt;
- mọi tool được đăng ký lúc mở, theo thứ tự (host tool, rồi workspace tool, task
  tool, filesystem tool và MCP tool), mỗi tool kèm name, description, schema và
  scope;
- `point`, `policy_id` và `policy_version` của từng callback, theo thứ tự khai báo;
- các định nghĩa skill và subagent được nạp lúc mở;
- policy `permissions`, khi nó không phải policy mặc định: một `mode` khác `default`,
  hoặc bất kỳ `layers`, `auto_allow`, `path_fields` hay `command_fields` nào, và khi
  đó cả `project_root` của nó. Vì vậy một run không bao giờ tiếp tục dưới một policy
  cho phép nhiều hơn (hoặc ít hơn) policy nó đã bắt đầu. Policy mặc định không thêm
  gì, dù `project_root` của nó là gì, nên nó để contract như trước khi policy được
  tính.

Nó không bao gồm namespace, các kho lưu trữ, config của provider, `timeout` của
một host tool, các lượt scripted, hay id, version và tool bị từ chối của một
profile. Bật hoặc tắt offload làm thay đổi nó; đổi ngưỡng thì không. Để đổi một
agent trong khi có các run đang chạy, hãy để chúng kết thúc trước, hoặc giữ config
cũ để resume chúng.

## Callback

Callback là một hàm mà server hỏi tại một điểm hook của một run. Khai
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
| `post_tool_use` | `tool_use_id`, `tool_name`, `input`, `result` | `{"action": "continue"}`, `{"action": "replace", "result"}`, `{"action": "mutate", "content"?, "is_error"?}`; một `replace` là một kết quả hoàn toàn mới, một `mutate` chỉ đổi giá trị output (`content`) hoặc cờ lỗi (`is_error`) và giữ nguyên metadata và mã lỗi của kết quả; bỏ `content` đi để giữ nó, vì `null` bị từ chối như một câu trả lời xấu (nó sẽ đọc như vắng mặt). Request kế tiếp và session log mang kết quả đã mutate |
| `tool_execute` | `tool_use_id`, `tool_name`, `input`, `attempt`, `phase` (`before` hoặc `after`), `result` (chỉ `after`) | `before`: `{"action": "next", "after"?}` chạy phần bên trong (các callback sau, rồi đến tool; với `after` server gọi lại với kết quả), `{"action": "veto", "result"}` bỏ qua nó và dùng `result`. `after`: `{"action": "continue"}`, `{"action": "replace", "result"}`. Đây là waterfall `tools/execute` của Python; decorator `tool_middleware` che hai lời gọi sau `async def layer(call, next)` (`await next()` chạy tool và trả về kết quả của nó; trả về mà không gọi nó thì phủ quyết lời gọi). Callback được khai báo đầu tiên là ngoài cùng, một layer thấy mọi lần thử của một lời gọi được retry, và input là cái mà permission đã cho phép: hãy viết lại input trong `pre_tool_use`. Một callback không được khai báo thì không tốn gì |
| `user_prompt_submit` | `content` (các content block) | `{"action": "continue"}`, `{"action": "rewrite", "content"}`, `{"action": "block", "reason"}` |
| `context_build` | `messages` (cuộc hội thoại mà model sẽ thấy) | `{"blocks"?: [{"text"}], "metadata"?: {...}}`; `metadata` dành cho observer và eval: nó nằm trên `ContextBuildEvent` của lời gọi, key của callback đến sau thắng, và model không bao giờ thấy nó |
| `before_provider_call` | `turn`, `request` (toàn bộ provider request sắp được gửi) | `{"action": "continue"}`, `{"action": "rewrite", "request"}`, `{"action": "force_continue", "feedback"}`, `{"action": "block", "reason"}`; một rewrite là thứ được gửi đi và được `context_build` ghi lại, và một block kết thúc run trước khi provider được gọi. `force_continue` bỏ qua lời gọi: `feedback` đi tới model trong một `<system-reminder>` và run chạy thêm một lượt, bị giới hạn bởi `max_turns` và bởi `max_idle_turns` của [loop guard](#loop_guard) (mặc định 25 lượt liên tiếp). `retry` bị từ chối ở đây (Python bỏ qua nó) |
| `after_provider_call` | `turn`, `assembly` (`message`, `stop_reason`, `usage?`, `model`: thứ lời gọi provider vừa dựng xong) | `{"action": "continue"}`, `{"action": "rewrite", "assembly"}`, `{"action": "retry", "feedback"}`, `{"action": "block", "reason"}`; một rewrite là thứ được stage và nối vào session, và một block kết thúc run trước khi response được stage. Một retry (`"force_continue"` cũng vậy, như trong Python) giữ response, không chạy lời gọi tool nào của nó, và gửi `feedback` cho model trước một lượt khác: trong một `<system-reminder>`, hoặc, khi response có gọi tool, dưới dạng kết quả lỗi của lời gọi đầu tiên (các lời gọi còn lại ghi "Tool not executed; revise and resubmit your final answer."). Chỉ `max_turns` giới hạn nó |
| `stop` | `text` (văn bản trả lời cuối cùng cho đến lúc này, của model hoặc của một rewrite trước đó) | `{"action": "allow"}`, `{"action": "continue", "feedback"}`, `{"action": "retry", "feedback"}`, `{"action": "rewrite", "text"}`, `{"action": "block", "reason"}`; một rewrite thay văn bản trả lời cuối cùng của run (không phải tin nhắn assistant, đã được nối vào session trước khi `stop` chạy), và một block kết thúc run với trạng thái failed. Đây cũng là nơi `before_final_answer` của Python nằm: không có point riêng. `continue` (hoặc `"force_continue"`, tên Python dùng cho nó ở `stop`) gửi `feedback` trong một `<system-reminder>` và chạy thêm một lượt, chỉ bị giới hạn bởi `max_turns`. `retry` là retry của `before_final_answer` trong Python: được chấp nhận một lần cho mỗi lần gọi run (với `spec.verification`, tối đa `max_retries` của nó cho mỗi run, mỗi lần là một event `verification`; xem [Bảo mật](./security.md#a-verifier-in-your-process)), sau đó được tính như `allow` và các callback phía sau quyết định. Hãy khai báo callback retry trước các callback khác, vì Python chạy `before_final_answer` trước `stop`. Một hook `before_final_answer` của Python trả lời `force_continue` thì ở đây phải trả lời `retry`: `force_continue` ở `stop` là `continue` không giới hạn |
| `post_turn` | `turn`, `stop_reason` | `{}` |

Kết quả tool trong một request hoặc một câu trả lời có dạng `{"output":
{"value", "metadata"?}}` hoặc `{"error": {"message", "code"?, "details"?}}`.

Mười ba điểm nữa chỉ quan sát: callback không thể thay đổi những gì chúng
báo cáo, nên câu trả lời luôn là `{}` và không điểm nào trong số đó có thể
fail closed.

| `point` | Request còn có |
|---|---|
| `agent_start` | `model`, `prompt`, `tools` |
| `agent_stop` | `status`, `stop_reason` |
| `turn_start` | `turn` |
| `provider_call_start` | `turn`, `model` |
| `provider_call_stop` | `turn`, `model`, `disposition`, `usage?` (vắng khi không xác định; bằng không với một lời gọi thất bại mà không tốn gì), `duration_ms` |
| `tool_use_start` | `tool_use_id`, `tool_name`, `input` |
| `tool_use_stop` | `tool_use_id`, `tool_name`, `is_error`, `duration_ms` |
| `post_tool_use_failure` | `tool_use_id`, `tool_name`, `input`, `error` |
| `pre_compact` | `turn`, `messages`, `tokens`, `strategy` |
| `post_compact` | `turn`, `messages_before`, `messages_after`, `tokens_before`, `tokens_after`, `strategy` |
| `subagent_start` | `child_session_id`, `subagent_run_id`, `subagent_type`, `display_name`, `prompt` |
| `subagent_stop` | `child_session_id`, `subagent_run_id`, `subagent_type`, `display_name`, `subtype`, `text`, `error?` |
| `event_emit` | `event` (event sắp được gửi tới bên gọi) |

`tool_use_start`/`tool_use_stop` bao quanh một lời gọi thực sự chạy (không
phải lời gọi được phục vụ sẵn hoặc bị `pre_tool_use` chặn);
`post_tool_use_failure` nổ ra cùng lúc với `post_tool_use` khi kết quả là
lỗi. `subagent_start`/`subagent_stop` nổ ra trên callback của agent cha,
không phải của agent con. `event_emit` nổ ra một lần cho mỗi event, theo
đúng thứ tự gửi đi, nhưng chỉ với event vừa được stream vừa được ghi bền:
không bao giờ cho record của chính một hook (vì sẽ tự đệ quy vào chính nó)
và không bao giờ cho một delta hay tool-progress chỉ stream.

| Đối số | Mặc định | Điều khiển gì |
|---|---|---|
| `point` | bắt buộc | Một trong hai mươi ba điểm ở trên |
| `policy_id`, `policy_version` | bắt buộc | Đi vào hợp đồng của run. Hãy tăng version mỗi khi hành vi, matcher, timeout hoặc fail mode thay đổi |
| `matcher` | mọi tool | Glob tên tool (`*`, `?`), chỉ ở các điểm tool (`pre_tool_use`, `permission_request`, `post_tool_use`, `tool_execute`); một lần không khớp không bao giờ đến process này |
| `timeout` | không có | Server chờ câu trả lời bao lâu; vắng thì chờ đến khi callback trả lời, run bị hủy hoặc kết nối kết thúc |
| `fail_mode` | `closed` ở `pre_tool_use` và `permission_request`, `open` ở các điểm còn lại | Điều xảy ra khi có exception, câu trả lời sai hoặc timeout: `closed` đưa ra câu trả lời chặn của điểm đó, `open` bỏ qua callback. `context_build` và `post_turn` không thể fail closed |

Các callback ở cùng một điểm chạy theo thứ tự khai báo. Một lần rewrite ở
`pre_tool_use` được đưa cho callback kế tiếp và được kiểm tra lại theo schema của
tool. Ở `permission_request`, một deny dừng chuỗi còn allow thì không, nên deny
đến sau thắng; nếu không có câu trả lời, lời gọi hỏi host như bình thường
([phê duyệt](./tools.md#phê-duyệt)). Một `user_prompt_submit` bị chặn sẽ kết thúc
run bằng `PromptBlocked` trước khi bất kỳ thứ gì được ghi. Mỗi lời gọi xuất hiện
dưới dạng một [event](./events.md) `hook` chỉ có trong stream. `retry` và
`force_continue` của Python chỉ là câu trả lời hợp lệ ở `after_provider_call` và
`stop`, và `force_continue` còn hợp lệ ở `before_provider_call`; mọi point khác từ
chối chúng như một câu trả lời sai, được xử lý theo fail mode.

Phản hồi của một `retry`, `continue` hoặc `force_continue` được ghi vào session như
message user kế tiếp, và đến dưới dạng event `user` với subtype `notification`.
Python đánh dấu cùng message đó là `prompt`; ở đây `prompt` vẫn là prompt riêng của
run, điều mà observer OpenTelemetry dựa vào.

```python
@callback("stop", "cite-sources", 1)
async def cite(request: dict[str, Any]) -> dict[str, Any]:
    if "http" not in request["text"]:
        return {"action": "retry", "feedback": "Cite a source for each claim."}
    return {"action": "allow"}
```

## Session

Session là một chuỗi id bất kỳ. Run đầu tiên trên một id sẽ tạo ra nó; các run sau
trên cùng id tiếp tục cuộc hội thoại với toàn bộ lịch sử của nó. Một session có
nhiều nhất một run chưa kết thúc ([Run](./runs.md#một-run-đang-chạy-cho-mỗi-session)).
Một run bị bỏ lại đang chờ một approval hoặc một câu trả lời giữ session cho đến khi
nó được approve, answer, hoặc kết thúc bằng `await agent.cancel(run_id)`
([Run](./runs.md#cancel-a-suspended-run)), thứ trả lời các lời gọi đang chờ của nó
như đã bị abort để run kế tiếp của session có thể bắt đầu.

`await agent.close_session(session_id)` báo cho agent rằng bạn tạm xong với một
session: các workspace tool quên những gì chúng đã đọc (nên một lệnh ghi sau đó
phải đọc lại tệp), và với kho memory, các tệp ảo của nó bị bỏ. Không có gì bền
vững bị xóa; session và các run của nó vẫn còn.

## Các trang liên quan

- [Bắt đầu nhanh](./quickstart.md): cùng các cấu trúc này, từng bước.
- [Provider](./providers.md): mọi config `provider`.
- [Tool](./tools.md): host tool, workspace tool, permission, phê duyệt.
- [Run](./runs.md): bắt đầu, stream, resume, phê duyệt, hủy.
