# Tool

[Bản gốc tiếng Anh](../../usage/tools.md)

Dùng tool để cung cấp cho agent các hành động, bước kiểm tra, quyền hạn và cơ chế kiểm soát thực thi.

[← Hướng dẫn sử dụng](./README.md)

Tool là cách agent tác động lên thế giới: tìm kiếm cơ sở tri thức, đọc tệp,
chạy lệnh shell. Tool của Linch là các **protocol duck-typed** — bạn triển khai một
tập nhỏ thuộc tính và phương thức, rồi chúng được đưa thẳng vào
`ToolRegistry`. Trang này trình bày cách viết tool (decorator `@tool`,
`FunctionTool` và tool dựa trên class), bộ lập lịch chạy chúng, timeout và retry,
backend thực thi Bash, dependency dùng chung cùng các quy tắc permission quyết
định một tool có được phép chạy hay không.

---

<a id="custom-tools"></a>

## `tool` và `Tool` — tool tùy chỉnh

Dùng `@tool` cho trường hợp thông thường. Nó bọc một function đồng bộ hoặc async
thành một object tool Linch chuẩn, suy ra JSON schema tối thiểu từ chữ ký của
function và inject `ToolContext` khi function yêu cầu `ctx`. Bạn viết một
function Python thông thường — không cần base class hay schema thủ công — còn
decorator xử lý phần kết nối protocol.

```python
from linch import Agent, ToolContext, tool
from linch import empty_tools, workspace_tools

@tool(description="Search the internal knowledge base.", tags=("rag",))
async def search_kb(query: str, ctx: ToolContext) -> str:
    results = await ctx.deps.kb.search(query)
    return "\n".join(results)

# No built-in tools (pure domain agent)
agent = Agent(..., tools=empty_tools(search_kb), deps=my_app_state)

# SWE tools minus Bash, plus custom
registry = workspace_tools(exclude={"Bash"}, extra=[search_kb])
agent = Agent(..., tools=registry, deps=my_app_state)
```

Schema được suy ra từ annotation, vì vậy hãy đặt tên parameter thật rõ ràng
— model sẽ nhìn thấy chúng. Parameter `ctx` là trường hợp đặc biệt: khi khai
báo nó, runtime inject một `ToolContext` (xem [Dependency](#dependency-trạng-thái-ứng-dụng-dùng-chung)
bên dưới) thay vì coi đây là argument do model cung cấp. Decorator `@tool`
cũng nhận `scope`, `parallel`, `tags`, `summary`, `resources`, `retryable` và
`execution_timeout_ms` để ghi đè các giá trị mặc định được thảo luận ở phần sau
của trang này.

`empty_tools(...)` tạo một registry chỉ chứa các tool bạn truyền vào — đây là
lựa chọn phù hợp cho agent miền nghiệp vụ thuần, không bao giờ được chạm vào hệ
thống tệp hoặc shell. `workspace_tools(exclude=..., extra=...)` bắt đầu từ bộ
SWE chuẩn (`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `TaskCreate`,
`TaskList`, `TaskGet`, `TaskUpdate`) rồi thêm hoặc bớt tool.
`Agent` sao chép registry được truyền vào constructor. Hãy đăng ký các tool thêm
về sau trên `agent.tools` để cơ chế discovery của request nhìn thấy chúng; việc
thay đổi registry gốc không làm thay đổi agent hiện có.

<a id="tool-search-for-large-catalogs"></a>

### `ToolSearchPlugin` — danh mục lớn

Agent tự động mount `ToolSearchPlugin(max_tools=30, max_skills=50)` khi session
đầu tiên được tạo. Danh mục không vượt quá các ngưỡng này vẫn giữ cấu trúc
request thông thường: không thêm tool `tool_search` và request gửi đến provider không
đổi. Khi vượt một ngưỡng, các tool không có thuộc tính duck-typed
`always_load` có giá trị truthy sẽ được hoãn nạp. Mô tả của `tool_search` chứa tên
các tool chưa được nạp nhưng không chứa schema của chúng, để prompt ban đầu vẫn
nhỏ.

```python
from linch import Agent, ToolSearchPlugin

agent = Agent(model="gpt-5", provider=provider, tools=registry)

# Opt out before the first session to restore the unfiltered catalog.
await agent.tool_search.dispose()

# Or choose different thresholds explicitly.
mount = await agent.use(ToolSearchPlugin(max_tools=100, max_skills=20))
```

Dùng `select:name_a,name_b` để tìm chính xác theo tên, hoặc dùng các từ khóa
thông thường. Kết quả khớp từ khóa được chấm điểm theo token trong tên và mô tả
của từng tool. Một lần tìm kiếm trả về các schema khớp và cung cấp các tool đó
cho request tiếp theo trong phần còn lại của session. Đặt
`always_load = True` cho một tool nhỏ, thiết yếu để giữ tool đó trong mọi
request dùng cơ chế hoãn nạp. Lời gọi đến tool chưa được nạp sẽ bị từ chối kèm chỉ dẫn
phải tìm kiếm trước; sau khi được nạp, các bước validation, hook và permission
thông thường vẫn được áp dụng.

Nếu skill hoặc context đặt giới hạn tool tường minh, danh sách đó phải bao gồm
`tool_search` khi cần tìm tool trong danh mục lớn. Việc tìm kiếm giữ nguyên các
giới hạn đó.

Khi có hơn 50 skill có thể được model gọi, system reminder cũng trì hoãn tên
và mô tả theo cách tương tự. `Skill` vẫn khả dụng và chỉ nạp nội dung của skill được
chọn sau khi model gọi nó. Tên tool và skill đã nạp được giữ trong
`linch.tool_search` metadata bền vững của session, nên tồn tại qua resume và các run sau.
Metadata lưu tập tên có giới hạn thay vì phát lại văn bản kết quả tìm kiếm, vì
việc offload kết quả, middleware và compaction có thể làm văn bản đó thay đổi.

Kiểm tra resolver chạy trước validation tool, hook và permission. Nó chỉ đọc tên
và input thô của provider; một veto không rỗng trở thành lỗi tool rõ ràng và
không cấp quyền hay thực thi tool chưa được nạp. Nếu provider phát ra một tool
terminal chưa được nạp, đường đi dừng turn sẽ ghi log cùng phản hồi yêu cầu tìm kiếm
trước khi yêu cầu request tiếp theo.

### FunctionTool

Để khởi tạo tường minh hoặc đăng ký động, hãy dùng trực tiếp `FunctionTool`.
Đây là cùng cơ chế mà decorator `@tool` sử dụng, được cung cấp dưới dạng class
để bạn có thể xây dựng tool tại runtime (chẳng hạn đăng ký một tool cho mỗi
tenant hoặc mỗi integration đã cấu hình):

```python
from linch import FunctionTool, ToolContext

def lookup_customer(customer_id: str, ctx: ToolContext) -> dict:
    return ctx.deps.crm.lookup(customer_id)

customer_tool = FunctionTool(
    lookup_customer,
    name="LookupCustomer",
    description="Look up a customer profile.",
    scope="read",
    parallel=True,
)
```

<a id="class-based-tools"></a>

### `ToolContext` và `ToolResult` — tool dựa trên class

Tool duck-typed dựa trên class vẫn được hỗ trợ và phù hợp khi bạn cần validation
tùy chỉnh, khai báo resource hoặc hành vi thực thi phong phú hơn. Hãy chọn class
khi chữ ký function quá thô — ví dụ khi bạn muốn từ chối input xấu *trước*
khi thực thi, khai báo các resource tool chạm tới, hoặc trả về một `ToolResult`
có metadata về nguồn gốc.

```python
from linch import Agent, ResourceAccess, ToolContext, ToolResult, empty_tools, workspace_tools

class MyTool:
    name = "search_kb"
    description = "Search the internal knowledge base."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"          # "read" | "write" | "exec"
    parallel = True         # can run concurrently when scope is read

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return raw

    def resources(self, input: dict) -> list[ResourceAccess]:
        return [ResourceAccess(resource="kb:default", mode="read")]

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        results = await ctx.deps.kb.search(input["query"])  # use deps
        return ToolResult(
            content="\n".join(results),
            summary=f"search_kb({input['query'][:40]})",
            metadata={"query": input["query"]},
        )

    def summarize(self, input: dict) -> str:
        return f"search_kb({input.get('query','')[:40]})"

# No built-in tools (pure domain agent)
agent = Agent(..., tools=empty_tools(MyTool()))

# SWE tools minus Bash, plus custom
registry = workspace_tools(exclude={"Bash"}, extra=[MyTool()])
agent = Agent(..., tools=registry)
```

Bốn phương thức ánh xạ vào vòng đời tool: `validate()` chuẩn hóa/kiểm tra raw
input của model (raise để từ chối), `execute()` thực hiện công việc và trả về
`ToolResult`, `summarize()` tạo nhãn một dòng hiển thị trong event và UI, còn
`resources()` (tùy chọn) khai báo các resource mà lời gọi đọc hoặc ghi để
scheduler tránh xung đột. `ToolResult` mang `content` gọn mà model nhìn thấy,
cùng các trường phong phú tùy chọn (`summary`, `metadata`, `citations`,
`attachments`) để app host dùng cho provenance và kết xuất mà không làm phình
context của model.

Để có JSON contract chuẩn, truyền `output_schema=` vào `@tool`. Khi đó function
trả về giá trị JSON thô, `ToolOutput` hoặc `ToolOutputError`; trả về
`ToolResult` legacy sẽ bị từ chối. Linch kiểm tra tính hợp lệ bằng JSON Schema Draft
2020-12 và mặc định render giá trị không phải string thành JSON compact, được
sắp xếp. Một `render_output` sync tùy chỉnh cũng yêu cầu `renderer_id` và
`renderer_version` ổn định; các giá trị này trở thành một phần của khả năng
tương thích durable-run.

---

## Scheduler

Bộ lập lịch chạy đồng thời các tool độc lập, an toàn khi parallel, tôn trọng
`Agent(max_tool_concurrency=...)` và dùng các khai báo `ResourceAccess` tùy
chọn để tránh xung đột read/write trên cùng tệp, database, index hoặc resource
do host định nghĩa. Với boolean `parallel=True` đơn giản, chỉ tool có scope
read được chấp nhận; tool write/exec vẫn chạy tuần tự.

Một tool cũng có thể cung cấp callable `parallel(input) -> bool` nhận biết input.
Callable này là ngoại lệ cho những tool có độ an toàn phụ thuộc vào lần gọi.
Tool tích hợp `Subagent` dùng nó để trả về `True` chỉ cho subagent có tên mà
các tool đã resolve đều có scope read; subagent đa dụng, kế thừa, không xác
định và chạy nền đều fail closed và vẫn tuần tự.

Trên thực tế, khi model phát ra nhiều lời gọi tool trong một lượt, các lời gọi
được chấp nhận theo khai báo boolean hoặc callable `parallel` có thể fan out
đồng thời. Xung đột resource vẫn tuần tự hóa các lời gọi đó. Giới hạn concurrency
mặc định là `max(1, os.cpu_count() or 4)`; đặt giới hạn bằng
`Agent(max_tool_concurrency=...)` hoặc biến môi trường
`AGENTKIT_MAX_TOOL_CONCURRENCY`.

`ResourceAccess` là cơ chế kiểm soát chi tiết hơn. Hai tool cùng khai báo
`read` trên `"kb:default"` có thể chạy chồng lấp; một `read` và một `write` trên
cùng resource sẽ bị tuần tự hóa. Hãy dùng nó khi các tool khác nhau dùng chung
một resource nền mà chỉ scope không thể mô tả — ví dụ tool search và tool
re-index trên cùng vector store.

<a id="batching-strategy"></a>

### `ToolBatchingStrategy`

`Agent(tool_batching_strategy=...)` (mặc định là
`"greedy"`) kiểm soát cách các lời gọi an toàn khi parallel trong một lượt được
nhóm thành batch:

- **`"greedy"`** — mỗi batch là prefix liên tiếp của các lời gọi tương thích; batch kết thúc tại xung đột resource đầu tiên, lời gọi không parallel hoặc khi chạm giới hạn concurrency.
- **`"maximal"`** — trong mỗi chuỗi các lời gọi an toàn khi parallel, nhận *mọi* lời gọi hiện tương thích (theo thứ tự của provider, tối đa đến giới hạn) và chỉ hoãn những lời gọi xung đột sang batch sau, để một xung đột đơn lẻ không còn làm đình trệ các lời gọi không liên quan phía sau. Lời gọi không parallel vẫn là rào chắn cứng, và hai strategy tạo ra batch giống hệt nhau khi không có hai lời gọi nào xung đột.

Dù theo cách nào, các block kết quả tool trả về cho model vẫn giữ thứ tự lời
gọi tool ban đầu của model; chỉ concurrency khi thực thi thay đổi. `"greedy"`
và `"maximal"` là các giá trị của type public `ToolBatchingStrategy`.

Kết quả tool rất lớn có thể được offload vào virtual filesystem để không làm
ngập context của model — xem [offload kết quả](./filesystem.md).

---

## Timeout

Đặt `Agent(tool_timeout_ms=N)` (hoặc env `AGENTKIT_TOOL_TIMEOUT_MS`) để áp dụng
thời hạn cho toàn agent. Tool hết thời gian trả về `is_error=True` và run tiếp
tục. Ghi đè theo từng tool: đặt `execution_timeout_ms` làm class attribute trên
tool; `0` loại tool khỏi mặc định của agent.

Mặc định là `None`, không có timeout ở cấp agent.
Khi được đặt, timeout chuyển thành kết quả `is_error=True` thay vì raise,
vì vậy một tool chậm không làm các lời gọi parallel còn lại dừng theo; model
nhận được thông báo đủ rõ để biết cách xử lý và phản ứng. Exception có kiểu
`ToolTimeoutError` (`kind="tool_timeout"`, `retryable=True`) khả dụng cho
bên tiêu thụ event và policy hook.

---

## Retry

Truyền `Agent(tool_retry=RetryOptions(max_attempts=3, base_delay_ms=50))` để
retry khi có lỗi tạm thời. Tool scope read retry mọi exception (chúng
idempotent); tool write/exec chỉ retry khi tool khai báo `retryable = True`.
`AbortError` không bao giờ được retry.

Retry được **giới hạn bởi tác dụng phụ** một cách có chủ ý: chạy lại một lần đọc
hoặc search là vô hại, nhưng chạy lại mù quáng một thao tác ghi hoặc lệnh shell
có thể nhân đôi tác động. Vì vậy tool write/exec phải opt in tường minh qua
attribute `retryable` (hoặc ghi đè `@tool(retryable=True)`) chỉ khi effect của
nó thực sự idempotent. Retry dùng exponential backoff bắt đầu từ
`base_delay_ms`.

---

<a id="bash-execution-backend"></a>

## `ExecutionBackend` — thực thi Bash

`BashTool(*, execution=None, max_processes=4, max_runtime_ms=1_800_000)` là tool scope exec, không parallel. Khởi
tạo trực tiếp dùng mặc định `LocalBackend`; object `execution` được truyền vào
phải cung cấp `.shell`.

```python
from linch import Agent, BashTool, empty_tools

bash = BashTool()
tool_input = bash.validate({"command": "pwd", "timeout_ms": 5_000})
agent = Agent(model="gpt-5", provider=provider, tools=empty_tools(bash))
```

Đầu vào yêu cầu một chuỗi `command` không rỗng. Số nguyên `timeout_ms` tùy chọn có
mặc định `120000`; giá trị không dương được đặt lại về mặc định, còn giá trị
trên `1800000` bị giới hạn. Thực thi dùng `ctx.cwd`. Kết quả chứa stdout,
tiếp theo là `[stderr]` và stderr nếu có. Exit khác 0 đặt `is_error=True`; timeout
raise `ToolExecutionError`.

Với backend Local và Docker, Bash hỗ trợ tiến trình được quản lý. `timeout_ms` là thời điểm lời gọi trả về, không phải hạn buộc
hủy: lệnh chưa xong trả về đầu ra và mã như `p1`. Đặt
`run_in_background=True` để trả về ngay. Dùng `BashProcess` tiếp trong cùng lượt
chạy (`BashProcessTool` khi tự tạo danh sách công cụ):

```python
# Model-facing tool inputs, within the same run:
{"command": "python -m http.server 8000", "run_in_background": True}
{"action": "wait", "id": "p1", "wait_ms": 1000}
{"action": "status", "id": "p1"}
{"action": "kill", "id": "p1"}
```

`wait` trả về đầu ra mới kể từ lần đọc trước, rồi trạng thái thoát hoặc thông báo
còn chạy. Sau ba lần đọc không có đầu ra, công cụ gợi ý dừng tiến trình. Thời gian
chờ được giới hạn dưới hạn scheduler để giữ đầu ra. Mỗi lượt chạy có tối đa bốn
tiến trình đang chạy; lệnh vượt giới hạn bị hủy khi thời gian chờ Bash hết.
`max_runtime_ms` là hạn buộc hủy riêng, mặc định 30 phút. Không chuyển đầu ra lệnh
chạy lâu qua `tail`, vì sẽ làm chậm đầu ra thấy được. Backend bên thứ ba chỉ có
`run`, hoặc lời gọi Bash trực tiếp không có Session đang chạy, vẫn hủy khi hết hạn.

Khi lượt chạy kết thúc, mọi nhóm tiến trình được theo dõi, kể cả tiến trình con
còn sống sau lệnh foreground, đều bị hủy và tệp spill bị xóa trước khi Session
nhận lượt mới hay trả kết quả cuối. Session con có registry riêng. Registry không
bền vững: sau khởi động lại, mã cũ không còn; hãy chạy lệnh mới. Worker dừng bình
thường sẽ đóng Session và hủy tiến trình; SIGKILL có thể để tiến trình sống tiếp.
Docker dùng một handle `docker run` cho mỗi lệnh và xóa container khi hủy, không
dùng container cố định cho Session. BashProcess chạy tuần tự, không được cache và
không yêu cầu duyệt lại lệnh đã được Bash duyệt; quy tắc deny tường minh vẫn áp
dụng. Schema Bash/BashProcess mới sẽ chặn resume hợp đồng công cụ cũ: cần hoàn tất
các lượt cũ trước khi nâng cấp.

Để inject một môi trường thực thi khác, truyền backend `.shell`/`.fs` nhất quán làm
`execution_backend=...` cho `Agent`. Agent chỉ thay thế tool `Bash` hiện có; nó
không thêm quyền truy cập shell vào registry tùy chỉnh vốn cố ý bỏ qua `Bash`.

`ToolRule`, `PathRule` và `BashRule` vẫn quyết định một lệnh Bash có được phép
chạy hay không. Execution backend chỉ thay đổi nơi và cách lệnh đã được duyệt
chạy. `DockerBackend` mặc định dùng workspace mount có quyền ghi, network mặc
định của Docker, không forward environment và root filesystem bình thường của
container. Các control hardening của nó là opt-in.

```python
from linch import Agent, DockerBackend, RemoteExecutionBackend

agent = Agent(
    ...,
    execution_backend=RemoteExecutionBackend(
        shell=DockerBackend(
            image="python:3.12-slim",
            network="none",
            read_only_root=True,
            workspace_mount="rw",
            tmpfs=("/tmp:rw,noexec,nosuid,nodev,size=64m",),
            forward_env=(),
            # Required for durable runs when env/forward_env is non-empty:
            resume_fingerprint_key=my_host_secret,
        ),
        fs=my_container_filesystem,  # host-supplied transport in the same world
    ),
)
```

`DockerBackend` là một shell transport; không thể truyền trực tiếp nó vào
`Agent(execution_backend=...)`. Filesystem transport bên trên phải truy cập cùng
container workspace và ánh xạ session cwd hiệu dụng một cách nhất quán. Với run
bền vững, môi trường từ xa và filesystem của nó cũng cần `resume_policy_id` ổn định
và `resume_policy_config` tương thích JSON, không phải `None` (cộng với
`resume_policy_version` tùy chọn). Xem [Mở rộng](./extending.md).

Dùng `workspace_mount="ro"` để kiểm tra workspace chỉ đọc,
`env={...}` cho biến môi trường container tường minh,
`forward_env=(...)` để allowlist biến môi trường host và `user="1000:1000"`
để chạy container không phải root.

Khi bỏ qua `docker_path`, `DockerBackend` dùng `shutil.which("docker")` (không
cần dependency Docker SDK). `docker_path` tường minh bỏ qua việc tra cứu đó.
Cả hai backend đều route timeout và abort qua cùng một đường đi, vì vậy
`session.abort()` ngắt lệnh đang chạy thay vì để rò rỉ process chạy mãi.
Backend chỉ nói về *nơi* và *cách* một lệnh đã được duyệt chạy — **có** chạy hay
không được quyết định bởi [permission](#quyền-hạn).

Đối với triển khai durable nghiêm ngặt, dùng `docker_path` tin cậy tường minh
và pin `image` bằng digest bất biến thay vì tag có thể thay đổi. Contract của
run ghi lại policy backend đã cấu hình và HMAC giá trị environment được cấu hình/
forward bằng `resume_fingerprint_key`, để các giá trị không được sao chép vào
run metadata và thay đổi sẽ chặn resume. Key phải ổn định giữa các host resume
cùng một run và Linch không bao giờ lưu key. Hãy bảo vệ cả key này lẫn run
store. Một Bash backend tùy chỉnh trên run durable phải cung cấp
`resume_policy_id`, `resume_policy_config` tương thích JSON và không phải
`None` (chỉ dùng `{}` khi thực sự không có cấu hình), cùng
`resume_policy_version` tùy chọn. Danh tính đó là khẳng định của host: đưa vào
mọi input liên quan đến authority và tăng version khi hành vi backend hoặc
policy sandbox remote thay đổi; nếu không, hãy bắt đầu run mới. Việc dùng
`BashTool(execution=...)` độc lập cũng fingerprint shell transport hiệu dụng;
nó vẫn giữ các yêu cầu identity policy shell tương tự.

---

<a id="mcp-tools"></a>

## Tool MCP

Cài dependency client tùy chọn bằng `pip install 'linch[mcp]'`. Bật MCP tường
minh bằng `FeatureFlags(mcp=True)` và cấu hình các server có tên trên agent.
`await agent.connect_mcp()` khởi tạo connection, liệt kê tool của server và
đăng ký chúng; việc tạo session cũng tự động connect.

Để có ví dụ stdio hoàn toàn local, lưu server chỉ dùng thư viện chuẩn này thành
`server.py`. Nó đọc mỗi JSON-RPC message trên một dòng và dành stdout cho
message của protocol:

```python
"""One local MCP tool over newline-delimited JSON-RPC on stdin/stdout.

Only the standard library is needed. Keep stdout exclusively for MCP messages.
"""

import json
import sys


ADD_TOOL = {
    "name": "add",
    "description": "Add two integers locally.",
    "inputSchema": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def tool_result(text, *, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(request):
    # MCP notifications, including notifications/initialized, need no response.
    if "id" not in request:
        return None

    response = {"jsonrpc": "2.0", "id": request["id"]}
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        response["result"] = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tiny-local-adder", "version": "1.0.0"},
        }
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [ADD_TOOL]}
    elif method == "tools/call":
        args = params.get("arguments", {})
        if params.get("name") != "add":
            result = tool_result("Unknown tool", is_error=True)
        elif (
            not isinstance(args, dict)
            or set(args) != {"a", "b"}
            or any(type(value) is not int for value in args.values())
        ):
            result = tool_result("Expected exactly two integers: a and b", is_error=True)
        else:
            result = tool_result(str(args["a"] + args["b"]))
        response["result"] = result
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def main():
    # A complete request occupies one line. Flush each reply so the client can
    # continue its handshake/tool call without waiting for a full stdout buffer.
    for line in sys.stdin:
        response = handle(json.loads(line))
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
```

Lưu client này cạnh server thành `client.py` rồi chạy `python client.py`.
Provider được script hóa sẽ gọi tool MCP local thật mà không cần model API hay
API key:

```python
import asyncio
import sys
from pathlib import Path

from linch import Agent, FeatureFlags, McpStdioServerConfig, ToolCallEndEvent
from linch.ext.evals import ScriptedProvider, TextTurn, ToolUseTurn


async def main():
    server_path = Path(__file__).with_name("server.py").resolve()
    provider = ScriptedProvider(turns=[
        ToolUseTurn(tool_name="mcp__local__add", tool_input={"a": 2, "b": 3}),
        TextTurn(text="The sum is 5."),
    ])
    agent = Agent(
        model="scripted",
        provider=provider,
        mcp_servers={
            "local": McpStdioServerConfig(
                type="stdio", command=sys.executable, args=["-I", str(server_path)]
            ),
        },
        features=FeatureFlags(mcp=True),
    )
    try:
        await agent.connect_mcp()
        session = await agent.session()
        async for event in session.run("Add 2 and 3 using the local tool."):
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name, event.result)  # mcp__local__add 5
    finally:
        await agent.close()


asyncio.run(main())
```

Tên MCP có dạng `mcp__SERVER__TOOL`. Mỗi ký tự bên ngoài
`[a-zA-Z0-9_-]` trong tên server hoặc tool trở thành `_`; giữ nguyên chữ hoa
chữ thường. Ví dụ server `ops.prod` với tool `get.status` trở thành
`mcp__ops_prod__get_status`. Tên va chạm sau khi chuẩn hóa sẽ gây ra
`ConfigError`.

Permission rule có thể nhắm tới một server đã chuẩn hóa bằng
`ToolRule("mcp__ops_prod__*", "deny")`; dấu `*` cuối khớp với toàn bộ tiền tố
literal `mcp__ops_prod__`. Không có dạng glob nào khác được hỗ trợ; rule
`ToolRule("*", ...)` hiện có vẫn khớp mọi tool.

Chú thích của server bị bỏ qua theo mặc định: mọi tool có scope exec, chạy tuần
tự và không destructive. Chỉ đặt `trust_annotations=True` trong cấu hình server
khi host đứng ra bảo chứng cho lời khai của server đó. Khi bật trust, chỉ
`readOnlyHint: true` mới cấp scope read và khả năng parallel; các tool khác có
scope exec và chạy tuần tự, còn `destructive` chỉ là true khi server ghi rõ
`destructiveHint: true`. Tool read-only không bao giờ là destructive. Tool không
có chú thích không thêm ask rule nên có thể chạy trong `skip-dangerous`; trong
`acceptEdits`, tool exec vẫn hỏi. Tool destructive rõ ràng từ server đã được host
trust sẽ thêm ask rule kể cả trong `skip-dangerous`.

Hint chỉ là lời khai của server, không được kiểm chứng. `trust_annotations` và
`skip-dangerous` là quyết định của host; host chịu trách nhiệm về server mình
tin tưởng và hậu quả của việc cho phép lời gọi nguy hiểm. Server không trung
thực có thể gắn nhãn read-only cho tool có ghi dữ liệu để được tự động duyệt và
chạy parallel. Approval dùng `can_use_tool`, vì vậy một ask không có callback sẽ
bị từ chối.

Kết quả MCP bao gồm text của embedded resource; nội dung resource nhị phân vẫn
bị bỏ qua. Khi không spill, kết quả text tổng hợp, kể cả các dấu phân cách, bị
giới hạn ở 30.000 ký tự, dùng cùng giới hạn output với Grep. Khi có call context,
text lớn hơn được lưu dưới `ctx.cwd/.linch/output/`; kết quả trả về preview head/
tail có giới hạn và nêu đường dẫn chứa toàn bộ text. Nếu không thể ghi spill thì
trả về text đã cắt.

Nếu kết nối MCP bị mất, lời gọi thất bại trả về lỗi và không bao giờ được retry.
Connection owner reconnect với năm lần exponential backoff, bắt đầu từ 0,5 giây
và tối đa 30 giây; sau đó tool báo server không khả dụng cho tới khi host tạo
connection mới (hoặc Agent mới với server được cấu hình). Tên tool được giữ
nguyên, tool biến mất sau reconnect trả về lỗi, và catalog không tự xây lại.
Subprocess stdio luôn được reap trên mọi đường dọn dẹp.

---

## Dependency (trạng thái ứng dụng dùng chung)

Dependency là các object ứng dụng tùy ý — kết nối DB, vector store, API
client, config dict — mà tool truy cập qua `ctx.deps`. Đây là điểm kết nối duy nhất để
đưa hạ tầng đang hoạt động vào code của tool mà không dùng global.

```python
# Anything: a DB connection, vector store, API client, config dict
agent = Agent(..., deps={"db": my_db, "vector_store": vs})

# Access inside a class-based tool:
async def execute(self, input, ctx: ToolContext) -> ToolResult:
    results = await ctx.deps["vector_store"].search(input["query"])
    ...

# Or inside a function tool:
from linch import tool

@tool
async def search_docs(query: str, ctx: ToolContext) -> str:
    return await ctx.deps["vector_store"].search(query)

# Override per-run (e.g. tenant-specific connection)
from linch import RunOptions
async for event in session.run("...", RunOptions(deps=tenant_db)):
    ...
```

`deps` có thể là bất kỳ object nào — dict, dataclass hoặc class app-state của
riêng bạn. Tool yêu cầu `ctx` sẽ nhận nó trong mỗi lời gọi `execute()`. Ghi đè
theo run (`RunOptions(deps=...)`) là pattern multi-tenant: một `Agent` dùng
chung nhưng mỗi run được scope vào kết nối hoặc context dành cho tenant.
Cùng object `deps` cũng điều khiển việc xây dựng context theo request, vì vậy
memory và RAG recall có thể dùng chung chính các backend mà tool sử dụng — xem
[Ngữ cảnh và bộ nhớ](./context-and-memory.md).

---

## Quyền hạn

Permission kiểm soát mọi lời gọi tool *trước* khi thực thi. Rule được đánh giá theo
thứ tự; `deny` khớp sẽ chặn lời gọi. Lời gọi cần approval phát ra một
`PermissionRequestEvent` trước khi engine gọi `can_use_tool`. Không có callback,
các lời gọi đó bị từ chối.

```python
from linch import ToolRule, PathRule, BashRule

agent = Agent(
    ...,
    permissions={
        "mode": "acceptEdits",          # auto-allow write scope; ask for Bash
        "rules": [
            ToolRule(tool="Bash", decision="deny"),              # block Bash entirely
            PathRule(paths=["secrets/**"], decision="deny"),    # block secret paths
            BashRule(patterns=["rm -rf*"], decision="deny"),     # block dangerous commands
        ],
        "can_use_tool": my_approval_callback,   # async or sync
    },
)
```

Ba loại rule nhắm đến các đối tượng khác nhau:

| Rule | Khớp trên | Ghi chú |
|---|---|---|
| `ToolRule` | Tên tool | Cho phép/từ chối cả tool, ví dụ chặn hoàn toàn `Bash`. |
| `PathRule` | Đường dẫn tệp (glob) | `*`/`?` **không** đi qua `/`; `**` thì có. Khớp regex có neo. |
| `BashRule` | Lệnh Bash | Pattern string dùng fnmatch và token-prefix matching; pattern `{"regex": "..."}` dùng `re.search`. |

Khi không rule nào khớp, tool scope read và bốn task tool được cho phép ở mọi
mode. Các mặc định còn lại là:

- `"default"` — yêu cầu phê duyệt lời gọi write/exec.
- `"acceptEdits"` — cho phép mọi tool scope write và yêu cầu phê duyệt lời gọi exec.
- `"skip-dangerous"` — cho phép mọi lời gọi.

`can_use_tool` là callback approval tương tác của bạn (sync hoặc async). Khi
một lời gọi tool cần approval, loop phát `PermissionRequestEvent` và chờ
callback trả về response allow/deny — xem [Event](./events.md). Khi không cấu
hình callback, quyết định ask được giải quyết thành deny. Các quyết định đã
giải quyết được checkpoint trước khi thực thi và dùng lại trong durable replay;
callback chưa được giải quyết có thể được gọi lại sau resume.

### Principal và quy tắc theo role cho từng run

Host xác thực caller và cung cấp một `Principal` bất biến cho mỗi run. Tenant,
user và role set bất biến của nó khả dụng cho permission rule và cho tool qua
property chỉ đọc `ctx.principal`.

```python
from linch import Agent, Principal, RunOptions, ToolRule

principal = Principal(
    tenant="acme",
    user="alice",
    roles=frozenset({"editor"}),
)
agent = Agent(
    ...,
    permissions={
        "mode": "acceptEdits",
        "rules": [
            ToolRule("Write", "deny", unless_role="editor"),
            ToolRule("Bash", "deny", role="contractor"),
        ],
    },
)
session = await agent.session()
async for event in session.run("Update the report", RunOptions(principal=principal)):
    ...
```

`ToolRule`, `PathRule` và `BashRule` nhận điều kiện `role` và `unless_role`
dạng keyword-only. `role="editor"` chỉ áp dụng rule khi role đó hiện diện;
`unless_role="editor"` chỉ áp dụng khi role đó vắng mặt. Nếu cung cấp cả hai,
cả hai điều kiện phải đúng. Điều kiện thu hẹp lúc một rule khớp; thứ tự
first-match, deny override theo layer và mode default hiện có vẫn được áp dụng.
Dùng `deny` với `unless_role` để yêu cầu một role. Với `principal=None`, điều
kiện nhìn thấy role set rỗng; rule không có điều kiện role vẫn giữ hành vi cũ.

Tool có thể dùng tenant để scope các query ứng dụng:

```python
@tool
async def customer(customer_id: str, ctx: ToolContext) -> dict:
    if ctx.principal is None:
        raise ValueError("An authenticated principal is required")
    return await ctx.deps.customers.get(
        tenant=ctx.principal.tenant, customer_id=customer_id
    )
```

Với run durable, `RunContract` được fingerprint trong run metadata ghi tenant,
user và role đã sort của principal, cùng các điều kiện role trong policy
permission. Resume và worker takeover khôi phục principal đã lưu khi resume
options không cung cấp principal. Tenant, user hoặc role set khác đi, hay role
rule thay đổi, đều làm contract check thất bại trước khi thực thi tool. Hãy bắt
đầu run mới để thay đổi identity. Tool spawn hoặc tiếp tục subagent sẽ truyền
principal tương tự cho các con. Callback approval cũng nhận nó qua
`request.principal`; việc định tuyến approval vẫn thuộc trách nhiệm của host.

Để biến đổi hoặc chặn giữa chừng lời gọi và kết quả tool bằng code (thay vì chỉ
allow/deny), dùng các chokepoint `PreToolUse` / `PostToolUse` và
`ToolMiddlewareHook` được mô tả trong [Hook](./hooks.md).

---

## Các trang liên quan

- [Hook](./hooks.md) — `PreToolUse`/`PostToolUse` và middleware của tool.
- [Hệ thống tệp ảo](./filesystem.md) — offload kết quả tool lớn.
- [Ngữ cảnh và bộ nhớ](./context-and-memory.md) — cùng `deps` điều khiển context.
- [Event](./events.md) — event gọi tool và permission.
