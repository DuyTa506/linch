# Giao thức tool

[Bản gốc tiếng Anh](../../architecture/tool-protocol.md)

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Với các tool ứng dụng thông thường, hãy ưu tiên hàm hỗ trợ `@tool`. Hàm này bọc một hàm Python đồng bộ hoặc
bất đồng bộ trong một đối tượng tương thích với Tool thông thường, suy ra JSON
schema tối thiểu, truyền `ToolContext` vào hàm khi hàm yêu cầu `ctx`, và chuyển
các giá trị trả về thông thường thành `ToolResult` khi không khai báo `output_schema`.
Khi có `output_schema`, nó nhận JSON thô, `ToolOutput` hoặc `ToolOutputError`
thay thế.

```python
from linch import ToolContext, tool

@tool(description="Search the product knowledge base.", tags=("rag",))
async def search_kb(query: str, ctx: ToolContext) -> str:
    return await ctx.deps.kb.search(query)
```

Runtime vẫn sử dụng cùng giao thức **duck-typed** — không có lớp cơ sở, không có
bước kiểm tra `isinstance(tool, Tool)`. `FunctionTool` triển khai cấu trúc này,
và các tool nâng cao có thể triển khai trực tiếp:

```python
from collections.abc import Callable, Iterable
from typing import Literal

from linch import (
    JsonValue,
    ResourceAccess,
    SystemPromptSection,
    ToolContext,
    ToolOutput,
    ToolOutputError,
    ToolResult,
)


class MyTool:
    name: str                                      # unique registry key
    description: str                               # shown to the model
    input_schema: dict                             # JSON Schema object
    scope: Literal["read", "write", "exec"]
    parallel: bool | Callable[[dict], bool]         # concurrency declaration

    # Optional attributes (all duck-typed via getattr)
    execution_timeout_ms: float                    # per-tool timeout; 0 = opt-out
    retryable: bool                                # opt write/exec tool into retry
    always_load: bool                              # keep in deferred catalogs when truthy

    def validate(self, raw: dict) -> dict: ...
    def resources(self, input: dict) -> list[ResourceAccess]: ...  # optional
    async def execute(
        self, input: dict, ctx: ToolContext
    ) -> ToolResult | ToolOutput | ToolOutputError | JsonValue: ...
    def summarize(self, input: dict) -> str: ...   # one-line for logs

    # Optional prompt guidance while this tool is active.
    system_prompt_sections: (
        Iterable[SystemPromptSection] | Callable[[], Iterable[SystemPromptSection]]
    )
```

Tool có thể chọn dùng Canonical Tool Output V2 bằng cách khai báo `output_schema`. Một tool V2
trả về một giá trị JSON, `ToolOutput` hoặc `ToolOutputError`; nó không được trả về
`ToolResult` kiểu cũ. Linch từ chối nghiêm ngặt các tham chiếu vòng, số thực không hữu hạn,
khóa đối tượng không phải chuỗi và các đối tượng Python tùy ý; kiểm tra các giá trị
thành công bằng JSON Schema Draft 2020-12, rồi kết xuất dạng biểu diễn dành cho provider.
Chuỗi được kết xuất nguyên trạng; các giá trị khác dùng JSON gọn và có tính xác định. Bộ kết xuất
đồng bộ tùy chỉnh phải khai báo `renderer_id` và `renderer_version` ổn định.
`ToolCallEndEvent.tool_output` mang giá trị chuẩn, trong khi các dạng biểu diễn
`result`, `tool_result` và `ToolResultBlock` hiện có vẫn được cung cấp.

`ToolContext` mang theo: `cwd`, `session_id`, `run_id`, `session_store`, `signal`
(hủy), `file_read_tracker`, `deps`, `filesystem`, `idempotency_key` và một
kênh tiếp nhận tiến độ theo cơ chế best-effort. Tool báo cáo tiến độ tạm thời bằng
`ctx.report_progress(message, data=None)`. Tiến độ trở thành một `ToolProgressEvent` chỉ dành cho
bên tiếp nhận trực tiếp; nó không phải lịch sử provider, kết quả tool hay event
run được lưu bền vững. `tool_timeout_s` chỉ nhận keyword; đối số vị trí thứ
sáu vẫn là `file_read_tracker`.

`deps` được truyền từ `Agent(deps=...)` hoặc ghi đè theo từng run bằng `RunOptions(deps=...)`. Dùng nó để truyền trạng thái ứng dụng vào tool mà không cần biến toàn cục.

`idempotency_key` là một chuỗi ổn định được suy ra từ id của run và id của lần dùng tool
(`f"{run_id}:{call.id}"`). Thực thi tool bền vững có ngữ nghĩa **ít nhất một lần** — một tác
động phụ xảy ra trước khi bản ghi hoàn tất được lưu bền vững có thể được thực hiện lại khi
resume. Một tích hợp làm thay đổi dữ liệu có thể truyền khóa này tới hệ thống bên ngoài (hoặc dùng nó làm khóa
cho bảng khử trùng lặp riêng) để khử trùng lặp hoặc đối soát một ý định thực thi bị gián đoạn.
Khóa này ổn định qua các lần resume cho cùng một lệnh gọi tool; bản thân Linch không
cam kết thực thi đúng một lần giữa các kho session, kho run và
hệ thống bên ngoài tách biệt.

### Thứ tự bảo đảm an toàn cho lệnh gọi tool

Bộ lập lịch xác định một đầu vào chuẩn trước khi thực thi:

1. giải mã lệnh gọi tool thô của provider;
2. chạy cơ chế phủ quyết tuần tự `tools/resolve` tùy chọn với tên tool, đầu vào thô
   và session; lý do không rỗng đầu tiên lập tức trả về lỗi tool;
3. phân giải và kiểm tra tính hợp lệ của lệnh gọi tool, rồi từ chối nếu tool đó
   không được cung cấp trong yêu cầu provider hiện tại;
4. chạy các hook `PreToolUse`, có thể biến đổi hoặc kết thúc sớm lệnh gọi;
5. kiểm tra lại tính hợp lệ của đầu vào đã biến đổi;
6. đánh giá các quy tắc và `can_use_tool` trên đầu vào cuối cùng đó;
7. lưu bền vững các quyết định đã xác định và thực thi tool.

Callback `tools/resolve` coi đầu vào thô là chỉ đọc và chạy trước bước
kiểm tra tool, hook và quyền. Một `Context` cha dùng chung có thể chia sẻ
bus event giữa các agent, vì vậy listener lọc theo session được cung cấp (listener
tìm kiếm tool mặc định kiểm tra agent sở hữu) trước khi trả về phủ quyết.

Callback phê duyệt không thể viết lại đầu vào. Việc sửa đổi thuộc về `PreToolUse`, nơi đầu vào được kiểm tra lại tính hợp lệ và
quyền; điều này ngăn việc phê duyệt một đường dẫn hoặc lệnh nhưng lại thực thi một đường dẫn hoặc lệnh khác.

`BashTool` giao việc thực thi lệnh cho một backend. `LocalBackend` cung cấp
hành vi subprocess cục bộ mặc định với đầu ra có giới hạn; `DockerBackend`
dùng `docker run --rm` khi Docker daemon khả dụng. Truyền
`Agent(execution_backend=...)` chỉ thay thế tool `Bash` đã có, vì vậy một
registry bị giới hạn không chứa `Bash` sẽ không được cấp thêm quyền truy cập shell.

`execution_backend` là tùy chọn; một agent trần không có môi trường thực thi. Khi
được cung cấp, nó phải có cả `.shell` và `.fs`, để các thao tác shell và hệ thống tệp
chia sẻ một môi trường thực thi nhất quán.
`LocalExecutionBackend` cung cấp cặp này ở máy cục bộ; `RemoteExecutionBackend`
gói các transport do ứng dụng chủ cung cấp. Siêu dữ liệu giới hạn môi trường mang tính khai báo: một backend
từ xa chỉ được mô tả là chạy trong sandbox khi bên nhúng cung cấp siêu dữ liệu
giới hạn không rỗng, và SDK không tự xác minh ranh giới đó.
`cwd` có hiệu lực của session được truyền tới transport shell; một backend từ xa
phải ánh xạ cwd đó và các đường dẫn hệ thống tệp của nó vào cùng môi trường nếu nó tuyên bố
bảo đảm tính nhất quán. Các giá trị chỉ có shell bị từ chối. Ghép `DockerBackend` (một transport
shell) với một transport hệ thống tệp trong cùng môi trường thực thi bằng
`RemoteExecutionBackend(shell=..., fs=...)`, hoặc dùng `LocalExecutionBackend`.
Một `filesystem=` được chỉ định tường minh phải là chính đối tượng `.fs` của môi trường đó.

Quyền và backend thực thi là các lớp riêng biệt. `ToolRule`, `PathRule`
và `BashRule` quyết định một lệnh gọi tool có được phép chạy hay không. Nếu một lệnh gọi Bash được
phê duyệt, backend đã cấu hình quyết định ranh giới runtime. Các giá trị mặc định của `DockerBackend`
là: mount workspace có quyền ghi, mạng mặc định của Docker,
không chuyển tiếp biến môi trường và không dùng hệ thống tệp gốc chỉ đọc. Các giới hạn
tài nguyên mặc định là `memory="2g"`, `cpus=2`, `pids_limit=512`,
`cap_drop=("ALL",)` và `no_new_privileges=True`; `None` tắt từng giới hạn.
Các tùy chọn chủ động bật như `network="none"`, `workspace_mount="ro"`,
`read_only_root=True`, `tmpfs=(...)`, `env={...}`, `forward_env=(...)` và
`user="1000:1000"` giới hạn các lệnh Bash đã được phê duyệt bên trong container.

Shell Local và Docker còn triển khai capability tùy chọn `ShellStarter.start`,
trả về `ShellProcess` có `read`, `wait` có thời hạn và `kill`. `wait` trả về `None`
khi tiến trình vẫn chạy. Triển khai `ShellBackend` chỉ có `run` không cần thêm
phương thức. Trong run của session, Bash dùng handle trực tiếp nếu có:
`timeout_ms` trả đầu ra hiện tại và ID tiến trình thay vì kill; `run_in_background`
trả ngay. `BashProcess` wait/status/kill thao tác trên lệnh đã được phê duyệt và
chạy tuần tự. Registry do run sở hữu giới hạn tiến trình sống (mặc định bốn), áp
thời gian chạy tối đa (mặc định 30 phút), kill các nhóm tiến trình đã theo dõi và
xóa spill shell khi run kết thúc. Lỗi dọn dẹp giữ session đang hoạt động để host
thử đóng lại; việc xóa container Docker chỉ là best effort. Khi hết dung lượng,
lệnh mới còn chạy bị kill
lúc lời gọi trả về. Handle không bền vững; kill cứng worker có thể để lại tiến
trình con. Stdin shell đóng; kết quả timeout/abort giữ đầu ra đã thu được.
ID gồm `p` và tám chữ số hex thường của UUID4, không tồn tại qua resume hoặc
cleanup. Registry giữ tối đa 128 ID đã kết thúc gần nhất; đường dẫn spill tồn tại
đến cleanup. `max_processes` và `max_runtime_ms` thuộc fingerprint bền vững.
`run_in_background` khi đã đạt `max_processes` bị từ chối trước khi spawn; lệnh
foreground chạy quá timeout khi đã đạt giới hạn sẽ bị kill. `kill` của `BashProcess`
trên tiến trình đã kết thúc giữ nguyên trạng thái thoát của tiến trình đó.
Bash chạy nền cần registry được quản lý và `ShellStarter`. Chỉ đúng `BashProcess`
tích hợp bỏ qua lần duyệt thứ hai. Timeout và giới hạn phải là số nguyên dương,
không nhận boolean hay giá trị không hữu hạn; timeout hợp lệ giới hạn ở 1.800.000 ms.

Mỗi stream shell giữ 4.000 byte đầu và 26.000 byte cuối. Phần vượt giới hạn có
thể được ghi vào file riêng mode 0600 dưới `cwd/.linch/output/`; lỗi spill hoặc
stream vượt 64 MiB làm bỏ file spill và giữ đầu ra có giới hạn. `ExecResult`
ghi trạng thái cắt ngắn, đường dẫn spill, nguyên nhân abort/timeout và signal;
metadata kết quả tool chứa đường dẫn để đọc trong run. Text MCP dùng chung bộ
thu spill có giới hạn; host quản lý thời gian lưu file spill MCP. I/O spill được chuyển bất đồng bộ sau khi vượt giới hạn byte của shell hoặc
ngưỡng 30.000 ký tự của MCP. Không ghi đè `.gitignore` hiện có
và từ chối spill qua thư mục symlink.

`LocalBackend` chỉ kế thừa PATH, HOME, TERM, LANG, LC_ALL, LC_CTYPE, USER,
SHELL và TMPDIR. Host truyền giá trị khác qua `env`/`forward_env`; định danh bền
vững dùng `resume_fingerprint_key` ổn định của host để băm có khóa các giá trị đó.
Allowlist giúp kiểm soát môi trường, không phải ranh giới bảo mật. Phiên bản chính
sách Local/Docker là `2`; cấu hình chính sách vẫn thuộc định danh resume.
Metadata giới hạn container mô tả cấu hình và không được Linch tự xác minh.

Tool MCP mặc định bỏ qua annotation của server và là tool `exec` tuần tự. Với
`trust_annotations=True`, tool tự khai chỉ đọc có scope `read` và chạy song song;
chỉ destructive hint tường minh trên tool không chỉ đọc mới thêm quy tắc hỏi
cho thao tác phá hủy. Host bảo chứng các khai báo và chịu rủi ro annotation sai.
Tên server sau chuẩn hóa từ chối `__`, `_` đầu hoặc cuối bằng `ConfigError`;
wildcard chọn đúng một server. `timeout_s` mỗi call phải dương và hữu hạn.
Reconnect thử năm lần với backoff lũy thừa từ 0,5 đến tối đa 30 giây, mỗi lần
kết nối có timeout 30 giây. Cleanup từng tài nguyên có hạn năm giây trong task
sở hữu kết nối và tiếp tục với tài nguyên kế tiếp khi hết hạn; giới hạn phụ thuộc
vào việc tài nguyên hỗ trợ hủy. Danh sách tool và annotation không được liệt kê
lại sau reconnect; server thay đổi cần session mới.

Về cấu hình MCP, cách đặt tên tool và ví dụ stdio cục bộ, xem
[Tool MCP](../usage/tools.md#tool-mcp).

### ToolRegistry

```python
registry.register(tool)                   # add; raises if name exists → Disposable
registry.replace(tool)                    # swap same-named tool → Disposable
registry.unregister(name)                 # remove by name
registry.select(names={...}, tags={...})  # runtime subset (per-request)
registry.copy()                           # shallow clone
registry.schemas()                        # provider-ready schema list
workspace_tools(exclude=..., extra=...)   # configurable software-workspace preset
empty_tools(*extra)                       # no built-ins + optional extras
```

`register`/`replace` là **các tác động có thể hoàn tác**: mỗi hàm trả về một
`linch.Disposable`. `register` từ chối tên trùng lặp; dùng `replace` để
chủ động thay thế nóng. Hủy một đăng ký sẽ xóa đúng đăng ký đó
(kiểm tra định danh đối tượng, dùng một lần và có tính lũy đẳng). Hủy một lần thay thế sẽ khôi phục
đăng ký trước đó nếu nó vẫn sở hữu vị trí; hủy một handle đã lỗi thời
không bao giờ xóa tool mới hơn. Vì việc dọn dẹp có thể bất đồng bộ, hãy luôn dùng
`await disposer.dispose()` (hoặc `await disposer()`). Xem [kernel.md](./kernel.md).

### Tìm kiếm tool được trì hoãn nạp

`ToolSearchPlugin` là plugin danh mục yêu cầu mặc định. Nó trì hoãn nạp tool khi
danh mục được cung cấp vượt quá 30 tool, và trì hoãn nạp mô tả skill khi có hơn
50 skill mà mô hình có thể gọi. Một tool có thuộc tính duck-typed
`always_load` mang giá trị truthy vẫn được cung cấp; mọi tool khác được trì hoãn nạp đều được biểu diễn
bằng tên trong mô tả `tool_search`, không kèm schema. `tool_search` nhận
`select:a,b` và truy vấn từ khóa thông thường trên tên và mô tả, trả về các
schema hoặc mô tả skill khớp với truy vấn, và nạp các tên đã chọn cho
yêu cầu tiếp theo.

Việc lọc được gắn vào quá trình dựng yêu cầu thông qua capability
`request_catalog` của agent. Nó chỉ thay đổi những gì được cung cấp cho provider: ranh giới
cho các tool được cung cấp, bước kiểm tra tính hợp lệ, hook và bộ máy quyền thông thường vẫn chi phối
việc thực thi. Một lệnh gọi bị trì hoãn nạp mà bỏ qua sự tuân thủ của provider sẽ nhận được
lỗi tool “search first” kèm hướng xử lý. Các tên đã nạp được lưu bền vững trong
`Session.meta["linch.tool_search"]` có giới hạn; siêu dữ liệu bền vững là nguồn dữ liệu chuẩn
vì nội dung kết quả tìm kiếm có thể được chuyển ra ngoài, biến đổi bởi middleware hoặc
loại khỏi dạng biểu diễn dành cho provider trong quá trình nén ngữ cảnh.

Bộ dựng yêu cầu gọi `request_catalog.prepare()` trước khi chọn tool để
các đăng ký MCP đến muộn và danh mục động khác được đánh giá theo
số lượng hiện tại. Khi cả hai số lượng đều chưa vượt ngưỡng, `prepare()` giữ nguyên registry:
không thêm đăng ký `tool_search` và bộ phân giải không trả về phủ quyết, vì vậy
yêu cầu giữ nguyên từng byte. Khi vượt một ngưỡng, danh mục
đăng ký `tool_search`, lọc yêu cầu sau bước chọn ngữ cảnh và
bật chốt chặn lệnh gọi bị trì hoãn nạp; `scheduler.py` không có thêm nhánh
khám phá. Luồng xử lý tool kết thúc vẫn giữ chốt chặn dừng lượt để một lệnh gọi
kết thúc bị trì hoãn nạp phát ra phản hồi search-first đã được ghi log trước yêu cầu tiếp theo.

Tập tên đã chọn là siêu dữ liệu session bền vững. Bản chụp hiện tại theo từng yêu cầu
của các tên ứng viên/được cung cấp/skill là trạng thái mở rộng session chỉ chứa JSON,
được runtime sao chép vào `checkpoint.extension_state["linch.session"]` để
phát lại giữa lượt; plugin không triển khai hook có thể tạo checkpoint.

### Chuỗi xử lý tool

Trước bước kiểm tra tính hợp lệ, `agent.context.on("tools/resolve", listener)` đăng ký một
cơ chế phủ quyết tuần tự. Listener nhận `(tool_name, raw_input, session)` và trả về
một lý do gây lỗi tức thì hoặc `None`. Các đăng ký trả về một `Disposable`; khi
không có listener, quá trình phân giải dùng luồng ban đầu mà không gửi qua bus.

`ToolPipeline` là điểm mở rộng tùy chọn theo từng Agent, bao quanh mọi lệnh gọi tool:

```text
tools/pre-execute → tools/execute → tools/post-execute
```

Đăng ký các giai đoạn bằng `on_pre_execute`, `on_execute` và `on_post_execute`;
mỗi đăng ký trả về một `Disposable`. Listener thực thi là các wrapper bao quanh:
chúng nhận một bản ghi `ToolExecution` và phải `await next()` đúng một lần để
chạy phần thân bên trong, hoặc có thể chủ động kết thúc sớm. Listener trước và sau
cũng có thể thay thế kết quả ứng viên. Khi không có listener, bộ lập lịch
giữ luồng thực thi trực tiếp. Một pipeline đang hoạt động đóng góp
`resume_policy_id`/phiên bản/cấu hình ổn định, có thứ tự vào định danh run bền vững; listener tùy chỉnh
phải cung cấp `resume_policy_id` ổn định riêng.
Pipeline chạy sau khi kiểm tra tính hợp lệ chuẩn và đánh giá quyền. Listener của pipeline
không được sửa đổi `input`, `tool` hoặc các quyết định có ảnh hưởng đến phân quyền;
đặt các thao tác viết lại trong `PreToolUse`, nơi chúng được kiểm tra lại tính hợp lệ và quyền.

`Agent` không ngầm cài đặt `workspace_tools()`. Hãy truyền bộ công cụ thiết lập sẵn một cách tường minh
khi việc cấp quyền là có chủ đích và hiển thị rõ ràng.
Hệ thống tệp cục bộ của dự án, skill, subagent và tài nguyên MCP cũng
chịu kiểm soát tin cậy bằng `FeatureFlags`, và không phải các mặc định có sẵn ngầm cho agent trần.

## Cơ sở thiết kế

- **Giao thức duck-typed, không có lớp cơ sở.** Không có bước kiểm tra `isinstance(tool, Tool)`,
  vì vậy tool có thể là bất cứ thứ gì có đúng các thuộc tính/phương thức — một wrapper MCP,
  hàm `@tool` hay lớp viết tay đều có thể dùng thay thế như nhau. Kế thừa sẽ
  ràng buộc mọi tool với một kiểu SDK và làm phức tạp các adapter MCP/hàm.
- **`@tool` cho 90% trường hợp, giao thức trực tiếp cho phần còn lại.** Hầu hết tool là hàm
  thông thường; decorator suy ra schema và bọc giá trị trả về, nên trường hợp phổ biến
  chỉ cần một dòng. Các nhu cầu nâng cao (kiểm tra tùy chỉnh, `resources()`, thời gian chờ) chuyển xuống
  dùng giao thức mà không cần một mô hình tư duy khác.
- **`scope` + `parallel` được khai báo để bộ lập lịch có thể suy luận về tính an toàn.** Một
  tool khai báo `read`/`write`/`exec` và độ an toàn khi chạy song song cho phép bộ lập lịch mặc định chạy song song
  các thao tác đọc và chạy tuần tự các thao tác ghi — khả năng chạy đồng thời được suy ra từ khai báo thay vì
  dựa vào kỷ luật của bên gọi.
- **Quyền và backend thực thi là các lớp riêng biệt.** *Liệu* một lệnh gọi Bash có được phép
  chạy hay không (quy tắc) độc lập với việc nó chạy *ở đâu* (Local hay Docker). Tách riêng chúng
  cho phép bạn chạy trong sandbox mà không phải viết lại chính sách quyền, và
  `execution_backend` chỉ thay thế một tool `Bash` đã có — nó không bao giờ cấp quyền truy cập shell
  cho một registry không chứa tool đó.
- **`deps` thay cho biến toàn cục.** Trạng thái ứng dụng được truyền qua `ToolContext.deps`
  (theo từng agent hoặc run), nên các tool vẫn thuần túy, dễ kiểm thử và nhiều agent không
  chia sẻ trạng thái ở cấp module.

---

Quay lại [mục lục kiến trúc](./README.md).
