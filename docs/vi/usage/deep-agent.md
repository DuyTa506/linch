# Cấu hình sẵn cho deep agent

[Bản gốc tiếng Anh](../../usage/deep-agent.md)

Dùng `create_deep_agent` khi bạn muốn mô hình điều phối các subagent và tool trong workspace.

[← Hướng dẫn sử dụng](./README.md)

`create_deep_agent()` là factory cần được bật tường minh, thiết lập cấu hình nhiều agent chỉ bằng một lệnh gọi — tool cho workspace/tác vụ, danh sách subagent tích hợp sẵn, tính năng dự án đáng tin cậy, kho lưu trữ bền vững tùy chọn và system prompt mở rộng. Một `Agent` thuần vẫn giữ tính trung lập, không có tool và tắt các tính năng; dùng cấu hình sẵn này khi bạn muốn tổ chức một agent điều phối cùng các worker mà không phải tự lắp ghép từng thành phần.

Khác với [workflow](./workflows.md), deep agent do LLM dẫn dắt: chính mô hình quyết định cần tạo subagent nào, khi nào chạy chúng ở chế độ nền và khi nào tiếp tục một worker hiện có. Vì vậy, việc điều phối không có tính xác định và không cung cấp nhật ký workflow được định địa chỉ theo nội dung. Nếu bạn cấu hình các kho lưu trữ bền vững, checkpoint thông thường của session/run vẫn có thể resume một run; hãy dùng workflow khi yêu cầu của bạn là phát lại chính quá trình điều phối.

---

## `create_deep_agent()`

```python
import os
from linch import CompositeFileBackend, OpenAIChatCompletionsProvider
from linch import PostgresRunStore, StateFileBackend
from linch.ext.deep_agent import create_deep_agent
from linch.filesystem.postgres import PostgresFileBackend
from linch.sessions.postgres import PostgresSessionStore

dsn = os.environ["LINCH_POSTGRES_DSN"]
agent = create_deep_agent(
    model="gpt-5",
    provider=OpenAIChatCompletionsProvider(),  # configure options for your endpoint
    cwd=".",
    durable=True,
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
    filesystem=CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": PostgresFileBackend(dsn, namespace="tenant-1")},
    ),
    permissions={"mode": "skip-dangerous"},
)
session = await agent.session()
# At application shutdown:
await agent.close()
```

Khi bỏ qua `profile`, `profile="balanced"` đặt `durable=True`, giới hạn agent ở 64 lượt và 1.000.000 token, giữ lại worker để tiếp tục và bật subagent chạy nền. Dùng `profile="coordinator"` cho agent cha giao việc nặng cho worker, hoặc chỉ chọn `profile="unbounded"` khi ứng dụng chủ tự kiểm soát chi phí và vòng đời ở mức tương đương. Khi bỏ qua `features`, factory bật skill, subagent và hệ thống tệp ảo; MCP chỉ được bật khi có cấu hình `mcp_servers`. Giá trị `features=` tường minh được sử dụng đúng như được cung cấp.

`durable=True` yêu cầu adapter `session_store` và `run_store` tường minh, cùng `filesystem` tường minh khi tính năng đó được bật. Không có kho lưu trữ lâu dài tự động hay giá trị mặc định ngầm định từ biến môi trường. Thiếu cấu hình sẽ gây ra `ConfigError`; adapter InMemory tường minh được chấp nhận cho kiểm thử do ứng dụng chủ kiểm soát nhưng chỉ lưu trạng thái trong vòng đời tiến trình. Dùng `durable=False` cho bản demo tạm thời. Khi tắt các tính năng hệ thống tệp, bạn không cần cung cấp hệ thống tệp.

Để duy trì trạng thái qua lần khởi động lại, dùng các kho Postgres như trên. Hệ thống tệp kết hợp giữ nội dung lớn được chuyển ra ngoài trong `StateFileBackend` cục bộ của tiến trình và định tuyến `/memories/` đến Postgres. Cấu hình cơ sở dữ liệu cục bộ theo [Hướng dẫn bắt đầu nhanh](./quickstart.md#5-giữ-trạng-thái-khi-tiến-trình-khởi-động-lại). Sau đó, lịch sử session và checkpoint của run có thể resume sau khi khởi động lại. Worker đang chạy tách rời chỉ tồn tại trong tiến trình và không được resume; khi khôi phục, worker có trạng thái checkpoint là `running` được đánh dấu `killed` và thông báo khôi phục cho biết worker chưa hoàn thành. Theo mặc định, thông báo hoàn thành dùng hàng đợi trong bộ nhớ. Truyền `durability=DurabilityOptions(durable_inbox=True)` cùng kho session hỗ trợ hộp thư đến bền vững khi việc chuyển thông báo hoàn thành cần tiếp tục được bảo đảm sau lần khởi động lại.

Xem [Cấu hình agent](./agent.md) và [Hệ thống tệp ảo](./filesystem.md) để biết các quy ước về kho lưu trữ và định tuyến bên dưới.

---

> Ở trang này, worker là subagent. Đây không phải tiến trình
> [`linch.worker`](./production.md) dùng để chạy job trong hàng đợi.

## Worker chạy nền

Tạo worker chạy nền bằng cách truyền `run_in_background=True` cho tool `Subagent`. Đầu vào bắt buộc của tool là `description` và `prompt` không rỗng; `subagent_type` và `run_in_background` là các trường tùy chọn:

```json
{
  "description": "research asyncio",
  "prompt": "Summarise Python asyncio.gather in 2 sentences.",
  "subagent_type": "researcher",
  "run_in_background": true
}
```

Lệnh gọi tool `Subagent` trả về ngay với xác nhận; mô hình có thể tiếp tục run hiện tại và chọn nội dung cuối cùng. Sau khi worker hoàn thành, `<task-notification>` của worker được chuyển đến ở ranh giới lượt tiếp theo: một lượt provider khác trong `session.run()` hiện tại có thể nhận thông báo, hoặc một `session.run()` sau đó có thể lấy thông báo ra khỏi hàng đợi.

```python
# Turn 1 — request a background worker; the tool call returns promptly
async for event in session.run(
    "Call Subagent with this input: "
    "description='research asyncio', "
    "prompt='Summarise Python asyncio.gather in 2 sentences.', "
    "subagent_type='researcher', run_in_background=True."
):
    if event.type == "tool_call_end" and event.tool_name == "Subagent":
        print(event.result)  # ToolResult text contains the background acknowledgement.

# Wait for the worker (optional; a later run can receive the notification after it finishes)
for handle in session.workers.values():
    if handle.task and not handle.task.done():
        await handle.task

# A later run can receive the notification if the current run ended first.
async for event in session.run("Summarise what the background researcher found."):
    if event.type == "result":
        print(event.final_text)
```

Hành vi cốt lõi: việc tạo worker chạy nền **không** chặn lượt hiện tại. Worker chạy như một tác vụ tách rời trong khi agent điều phối tiếp tục làm việc. Khi hoàn thành, worker gọi `Session.notify()` với thông điệp `<task-notification>`. Theo mặc định, thông điệp đó đi vào hàng đợi chờ trong bộ nhớ của session. Ranh giới lượt provider tiếp theo lấy thông báo ra khỏi hàng đợi để agent điều phối thấy kết quả mà không cần thăm dò; ranh giới đó có thể là một lượt provider khác trong `session.run()` hiện tại hoặc thời điểm bắt đầu một `session.run()` sau đó. Khi có `RunStore`, Linch cũng ghi bản ghi kiểm toán `BackgroundWorkerEvent` kèm thông tin nguồn gốc; riêng kho run không bảo đảm chuyển kết quả một cách bền vững. Hàng đợi thông báo mặc định nằm trong bộ nhớ, còn `durability=DurabilityOptions(durable_inbox=True)` có thể lưu bền vững thông báo hoàn thành sau khi `Session.notify()` chuyển đến kho session có hỗ trợ. Không chế độ lưu trữ bền vững nào resume chính tác vụ đang hoạt động; ứng dụng chủ cần thực thi bền vững nên tự quản lý hàng đợi hoặc workflow. Việc tự chờ `handle.task` (như ở lượt 1.5 bên trên) là tùy chọn — nó chỉ quyết định thời điểm, không quyết định việc chuyển thông báo.

Đóng session sẽ hủy các worker chạy nền chưa xong. Job của `linch.worker` đóng session
khi run kết thúc, nên subagent nền tạo trong job sẽ dừng tại thời điểm đó. Trong job, hãy
dùng subagent foreground, hoặc xếp hàng phần việc tiếp theo thành một run riêng.

---

## Tách nhánh/tiếp tục

Với các profile mặc định của factory, kết quả `Subagent` chạy tiền cảnh được giữ lại có hậu tố `[Worker ID: agent-xxxx]` để agent điều phối có thể gọi lại cùng worker với đầy đủ ngữ cảnh qua `SubagentContinue`. Worker chạy nền luôn được giữ lại.

Đầu vào của tool tiếp tục là:

```json
{"to": "agent-a1b2", "message": "Give a one-line code example."}
```

```python
# Turn 1 — spawn foreground worker
async for event in session.run(
    "Call Subagent with description='explain asyncio.gather', "
    "prompt='Explain asyncio.gather in 1 sentence.', "
    "subagent_type='researcher'."
):
    if event.type == "tool_call_end" and event.tool_name == "Subagent":
        print(event.result)   # ToolResult text contains the retained Worker ID.

# Turn 2 — continue the same worker with its full context
async for event in session.run(
    "Use SubagentContinue(to='agent-a1b2', message='Give a one-line code example.')"
):
    if event.type == "result":
        print(event.final_text)
```

Việc tiếp tục worker resume worker đó với toàn bộ ngữ cảnh trước đây, nên bạn có thể giao việc qua nhiều lượt mà không phải giải thích lại tác vụ. Khi bật chế độ giữ lại (như trong các profile mặc định của factory), worker vẫn được gắn vào session sau run đầu tiên để bạn có thể tiếp tục như vậy. Mỗi lúc một worker chỉ chạy một cuộc hội thoại: khi một run nền hoặc một lần tiếp tục đang chạy nó, trạng thái của nó là `running`, và một `SubagentContinue` khác gửi tới nó trả về lỗi `WorkerBusy` mà không thay đổi worker.

`session.workers` là một `dict[str, WorkerHandle]`. Mỗi handle cung cấp `handle.child_session_id`, `handle.status` và `handle.last_result_text`. Các trạng thái gồm `running`, `completed`, `failed` hoặc `killed`. Kiểm tra chúng từ ứng dụng chủ để theo dõi worker nào đang hoạt động, kết quả gần nhất của chúng và session con tương ứng với từng worker.

---

<a id="generate-a-project-subagent"></a>

## `generate_subagent_definition`

Dùng `create_subagent_definition()` để chuyển yêu cầu bằng ngôn ngữ tự nhiên thành một subagent dự án thông thường được lưu trên đĩa. Theo mặc định, SDK ghi `<agent.cwd>/.linch/agents/<name>.md` (hoặc `config_dir` đã cấu hình của `Agent`), xác thực bằng cùng bộ nạp được dùng khi chạy và mặc định nạp lại `Agent` để subagent mới xuất hiện ngay trong danh mục của tool `Subagent`.

```python
from linch import Agent, FeatureFlags, create_subagent_definition, workspace_tools

agent = Agent(
    model="gpt-5",
    tools=workspace_tools(),
    features=FeatureFlags(
        skills=False,
        subagents=True,
        mcp=False,
        filesystem=False,
    ),
    permissions={"mode": "skip-dangerous"},
    cwd=".",
)

try:
    created = await create_subagent_definition(
        agent,
        "Create a test-runner subagent that runs focused tests after code changes.",
        tools=["Read", "Grep", "Bash"],
    )

    print(created.file_path)            # absolute path under cwd/.linch/agents/
    print(agent.subagent_registry.get("test-runner"))
finally:
    await agent.close()
```

Đây là cách thực hiện chỉ với một lệnh gọi: tạo, xác thực, ghi và nạp lại. Với ứng dụng chủ muốn kiểm soát nhiều hơn, hãy gọi riêng `generate_subagent_definition()` và `write_subagent_definition()` — cách này cho phép bạn chọn tường minh thư mục cấu hình, chính sách ghi đè và thời điểm nạp lại.

---

## Chế độ điều phối

```python
agent = create_deep_agent(
    model="...",
    profile="coordinator",
    coordinator=True,          # parent orchestrates only
    durable=False,
    permissions={"mode": "skip-dangerous"},
)
session = await agent.session()
# Parent omits the built-in Edit/Write/Bash/Grep/Glob/Read tools.
# It still has orchestration tools, and may also expose Skill, supplied tools,
# or configured MCP tools. Workers use the configured worker tool registry.
# At application shutdown:
await agent.close()
```

Với `coordinator=True`, agent cha bỏ các tool workspace tích hợp sẵn phục vụ công việc nặng và giữ các chức năng điều phối. Sau khi `agent.session()` nạp các danh mục tùy chọn, profile mặc định cũng bật tool `Skill`; tool do bên gọi cung cấp và máy chủ MCP đã cấu hình có thể bổ sung thêm tool. Worker dùng registry worker đã cấu hình, giúp ngữ cảnh của agent điều phối tập trung vào quản lý tác vụ thay vì nội dung tệp và đầu ra lệnh.

Để dừng worker chạy nền đang hoạt động, gọi `TaskStop` với `{"task_id": "agent-xxxx"}` (và `reason` tùy chọn). Handle vẫn nằm trong `session.workers` để có thể tiếp tục sau này bằng `SubagentContinue`. Việc dừng hủy tác vụ đang thực thi nhưng không loại bỏ worker — bạn có thể gọi lại worker khi đã có những gì cần thiết.

---

## Các trang liên quan

- [Workflow](./workflows.md) — phương án thay thế chế độ điều phối có tính xác định và có nhật ký
- [Cấu hình agent](./agent.md) — kho lưu trữ bền vững và budget của run
- [Hệ thống tệp ảo](./filesystem.md) — phân vùng `/memories` và backend kết hợp
