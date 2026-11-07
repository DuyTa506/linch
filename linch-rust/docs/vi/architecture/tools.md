# Tools

[Bản gốc tiếng Anh](../../architecture/tools.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Trang này nói về hợp đồng tool trong `linch-core` (trait `Tool`, các scope,
registry và cách một batch chạy) và các tool trong `linch-tools`: các tool
workspace, hệ thống tệp ảo cùng việc chuyển kết quả ra ngoài, và các tool
task của session. Dùng trang này khi bạn viết một tool, đổi cách các lời gọi được lập lịch,
hoặc đụng đến giới hạn truy cập tệp.

[Giao thức tool](../../../../docs/architecture/tool-protocol.md) của Linch bản Python
mô tả cùng những ý tưởng. Điểm khác trong Rust:

- Một tool là bất kỳ kiểu nào implement trait `Tool`; không có struct cơ sở
  và không có decorator `@tool`. Các method bắt buộc của trait chính là các
  attribute mà Python đọc bằng duck typing.
- `scope()` không có mặc định. Ở Linch 3.x, một scope `read` mặc định đã để các hàm có
  tác dụng phụ chạy mà không được phê duyệt và chạy song song (issue #99,
  `crates/linch-core/src/tool.rs:4-7`).
- Kết quả luôn là `Result<ToolOutput, ToolOutputError>`. Không có đối tượng
  `ToolResult` kiểu cũ; `output_schema` là tùy chọn.

## Trait Tool

`Tool` (`crates/linch-core/src/tool.rs:226`) là `Send + Sync` và không giữ trạng thái
giữa các lời gọi; dữ liệu theo từng lời gọi đến qua `ToolContext`.

| Method | Mặc định | Hợp đồng |
|---|---|---|
| `name()` | bắt buộc | 1 đến 64 chữ cái ASCII, chữ số, `_` hoặc `-`: những gì Anthropic, OpenAI và Gemini đều chấp nhận (`crates/linch-core/src/registry.rs:278-281`) |
| `description()` | bắt buộc | Hiển thị cho model |
| `input_schema()` | bắt buộc | JSON Schema draft 2020-12 với `"type": "object"` ở cấp cao nhất |
| `scope()` | bắt buộc | `Read`, `Write` hoặc `Exec` |
| `output_schema()` | `None` | Khi được đặt, một giá trị thành công phải thỏa mãn nó |
| `validate(input)` | identity | Các kiểm tra ngoài schema; chạy sau khi xác thực schema và chạy lại sau khi một hook `pre_tool_use` viết lại input |
| `parallel(input)` | `scope().is_parallel_safe()` | Lời gọi này có được chạy cùng các lời gọi đọc khác hay không |
| `summarize(input)` | tên tool | Một dòng cho event và lời nhắc phê duyệt |
| `execute(input, ctx)` | bắt buộc | Chạy trên input đã xác thực; trả về nhanh khi `ctx.cancel` kích hoạt |

`ToolOutput` giữ `value` mà model nhìn thấy và `metadata` mà model không thấy.
`ToolOutputError` giữ `message` mà model nhìn thấy, một `code` tùy chọn dành cho host,
và `details` tùy chọn (`tool.rs:42-83`).

`ToolContext` (`tool.rs:117`) mang session id, run id, `tool_use_id`,
`principal` của bên gọi, một token `cancel` (bị hủy khi run abort hoặc khi
lời gọi kết thúc) và `deadline` của lời gọi khi agent đặt call timeout.
`idempotency_key()` là `run_id:tool_use_id` (`tool.rs:182-192`). Một run đã resume
không bao giờ chạy lại một lời gọi có phần bắt đầu đã bền vững mà phần kết thúc thì chưa:
nó báo lời gọi đó là bị gián đoạn. `Subagent` và `SubagentContinue` là
ngoại lệ: chúng được plan lại và resume run con bền vững của chúng
(`crates/linch-core/src/runner/batch.rs:118-139`). `report_progress` là
nỗ lực tối đa (best effort) và không bao giờ làm tool thất bại (`tool.rs:195`).

### Scope và phê duyệt

| Scope | Chế độ quyền mặc định | Chạy song song |
|---|---|---|
| `Read` | được phép | có, cùng các lời gọi đọc kế bên |
| `Write` | hỏi | không |
| `Exec` | hỏi | không |

Scope quyết định việc tự động phê duyệt (`crates/linch-core/src/permissions/engine.rs:320`)
và, qua `parallel` mặc định, tính đồng thời. Nó cũng là một phần của run contract
digest, bên cạnh schema (`registry.rs:216-218`).

## Registry

`ToolRegistry` (`registry.rs:72`) được dùng chung giữa các run sau một `Arc`.

- `register(tool)` (`registry.rs:86`) thêm một tool dưới một tên còn trống. Nó thất bại
  khi tên không hợp lệ, trùng tên, schema cấp cao nhất không phải đối tượng,
  hoặc schema không hợp lệ theo draft 2020-12 hoặc có một `$ref` cần I/O.
  Schema được biên dịch một lần ở đây.
- `replace(tool)` (`registry.rs:95`) ẩn mọi tool cùng tên. Tool thay thế
  giữ vị trí cũ trong `definitions()`.
- Mọi thay đổi của tập hiển thị đều tăng `generation()` (`registry.rs:100`).
- `snapshot()` (`registry.rs:107`) trả về một `ToolSnapshot` bất biến. Mỗi
  lượt lấy một snapshot và dùng nó xuyên suốt, nên một đăng ký diễn ra
  giữa lượt không bao giờ thay đổi những gì lượt đó thấy.
- `definitions()` (`registry.rs:209`) liệt kê các tool theo thứ tự đăng ký đầu tiên.
  Thứ tự ổn định giữa các snapshot vì prompt cache phụ thuộc vào nó.

### Đăng ký có thể hoàn tác

Mỗi đăng ký trả về một `Registration` (`crates/linch-core/src/registration.rs:19`).
`dispose()` hoàn tác đúng đăng ký đó và là idempotent; drop handle không hoàn tác gì
(`registration.rs:1-6`). Dispose một tool đã bị một `replace` sau đó ẩn đi không thay đổi
gì nhìn thấy được. Dispose tool đang hiển thị sẽ khôi phục đăng ký còn sống gần nhất của
tên đó, hoặc gỡ tên đi. Cùng kiểu handle này được trả về từ `HookChain::register`
(`crates/linch-core/src/hooks/chain.rs:132`) và từ MCP client, nên một
host hoàn tác cả một cụm kết nối bằng cách dispose các handle mà nó đã giữ.

### Thay đổi catalog giữa session

Prompt đầu tiên của một session được ghi log cùng một `PrefixEntry`: các block system,
các tool và các skill được liệt kê tại thời điểm đó
(`crates/linch-core/src/catalog.rs`). Mọi request sau đó của session
đều bắt đầu bằng đúng những byte này, nên prompt cache giữ chúng bất kể
registry, skill hay subagent làm gì. Một thay đổi thì đi vào phần đuôi:
một `CatalogChangeBlock` (văn bản `<system-reminder>` của Linch bản Python) trong
message user kế tiếp mà run vẫn ghi log, nên nó không tốn thêm lần ghi nào.

- Một tool bị gỡ giữ lại hàng của nó; một lời gọi tới nó bị từ chối với lý do chưa đăng ký.
- Một tool được thêm sẽ được nối vào các tool của request, việc này làm mất cache
  một lần. `native_catalog_changes` trên `AnthropicOptions` hoặc `ResponsesOptions`
  gửi nó tại thông báo thay vào đó (mặc định tắt, chưa được kiểm chứng trên môi trường
  thật). Request nêu tên các hàng đó trong `ProviderRequest::appended_tools`, để
  adapter cũng giữ chúng ngoài `tools` qua một lần compaction.
- Một mô tả mới được nói bằng văn bản; một input schema mới thay thế hàng của nó.
- Một lần compaction không bao giờ viết lại prefix; bản tóm tắt của nó nhắc lại các
  thay đổi.

`Agent::reload_skills` và `Agent::reload_subagents` (cùng
`OpenedAgent::reload_*`, vốn quét lại thư mục cấu hình cho loại mà
cấu hình đã bật, và nếu không thì chỉ áp dụng lại danh sách của một deep agent và
các skill tích hợp sẵn) là các đăng ký có thể hoàn tác cùng loại. Run contract vẫn là
contract mà agent được build với nó.

`AgentBuilder::tool_search(ToolSearch)` trì hoãn một catalog lớn
(`crates/linch-core/src/tool_search.rs`): vượt quá `max_tools`, một session mới
chỉ được đưa các tool mà `always_load` của chúng cho phép cùng `tool_search`. Những gì một
lần tìm kiếm thấy được thông báo dưới dạng tool được thêm ở phần đuôi và được đưa ra từ
request kế tiếp; một lời gọi tới tool chưa được đưa ra bị từ chối với lý do bị trì hoãn.

## Cách một batch chạy

Các lời gọi tool của một response đi qua hai bước
(`crates/linch-core/src/scheduler.rs:1-22`), để một run có thể suspend giữa
chúng:

1. **Plan.** Với mỗi lời gọi: phân giải tên trong snapshot của lượt, xác thực
   input, chạy chuỗi `pre_tool_use`, xác thực lại input cuối cùng,
   đánh giá quyền (và chuỗi `permission_request` khi gặp `Ask`), rồi
   áp dụng mọi phê duyệt đã ghi từ một lần suspend trước. Một lời gọi vẫn
   cần phê duyệt sẽ làm run suspend.
2. **Execute** (`crates/linch-core/src/scheduler/execute.rs:119`). Kết quả
   được trả về mỗi lời gọi một cái, theo thứ tự lời gọi. Một kết quả thành công được kiểm tra
   theo `output_schema` của tool, rồi chuỗi `post_tool_use` chạy trên kết quả.
   Một tool bị panic nhận một kết quả lỗi nêu tên panic đó.

### Chỉ công việc đọc mới chạy song song

`execute` duyệt các lời gọi theo thứ tự và gom chúng thành nhóm
(`execute.rs:137-151`). Một lời gọi chỉ vào nhóm hiện tại nếu nhóm
bắt đầu bằng một lời gọi đọc, nhóm còn dưới giới hạn, và lời gọi này cũng là
lời gọi đọc (`in_read_group`, `execute.rs:157`). Một lời gọi không chạy tool nào
(bị từ chối, được phục vụ từ một hook, bị abort) cũng vào được. Mọi lời gọi khác chạy
riêng, nên lời gọi kế tiếp phải chờ nó.

Một lời gọi đọc là lời gọi mà `parallel(input)` của tool trả về true. Mặc định
chỉ trả về true với scope `Read`. Một tool có thể trả về true cho một lời gọi chỉ
làm công việc đọc bất kể scope của nó, ví dụ một subagent mà mọi tool đều chỉ đọc
(`tool.rs:250-257`). Đừng bao giờ trả về true cho một lời gọi ghi hoặc chạy
bất cứ thứ gì.

Giới hạn là `AgentSpec::max_tool_concurrency`, mặc định 4; 0 được tính là 1
(`crates/linch-core/src/agent/spec.rs:53-55`). `call_timeout_ms` giới hạn từng
lời gọi: tới deadline, scheduler drop future của lời gọi và báo một lỗi
timeout.

### Tính bền vững quanh một tool

Một tool thực sự chạy tốn hai lần ghi run-store: phần bắt đầu của nó được làm bền vững
trước khi nó chạy, và phần kết thúc ngay sau khi nó trả về
(`crates/linch-core/src/runner/sink.rs:1-12`). Một lời gọi không chạy gì có
phần bắt đầu và kết thúc được stage và ghi cùng barrier kế tiếp. Xem
[stores](./stores.md#số-lần-ghi-của-group-commit) cho số lần ghi trên mỗi lượt.

## Tool workspace

`workspace_tools(fs, reads)` (`crates/linch-tools/src/lib.rs:51`) trả về
`Read`, `Write`, `Edit`, `Grep`, `Glob` và `Bash` trên một `LocalFs`, dùng chung
một `ReadTracker`. Crate này chỉ dành cho Unix vì Bash dùng process group.

| Tool | Scope | Hành vi |
|---|---|---|
| `Read` | read | Văn bản UTF-8 với số dòng; `offset`/`limit`, mặc định 2000 dòng; tệp trên 10 MiB bị từ chối (`crates/linch-tools/src/read.rs:14-17`) |
| `Write` | write | Thay cả một tệp theo cách nguyên tử và tạo các thư mục cha. Một tệp đã tồn tại phải đã được đọc đầy đủ tại stamp hiện tại của nó |
| `Edit` | write | Thay đúng đoạn văn bản, một lần trừ khi có `replace_all`. Tệp phải đã được đọc tại stamp hiện tại của nó, gồm mọi dòng mà một kết quả khớp chạm tới |
| `Grep` | read | Engine của ripgrep trong process; các chế độ `files_with_matches`, `count`, `content`; mặc định 250 mục, đầu ra giới hạn 30 000 byte (`crates/linch-tools/src/grep.rs:15-25`) |
| `Glob` | read | Quy tắc glob của ripgrep, mới nhất trước, tối đa 1000 đường dẫn (`crates/linch-tools/src/glob.rs:13`) |
| `Bash` | exec | `/bin/sh -c` trong thư mục gốc; xem bên dưới |

Grep và Glob bỏ qua các tệp bị ignore và không đi theo symlink; Grep cũng bỏ qua
các tệp ẩn và nhị phân, và Glob chỉ liệt kê tệp ẩn khi mẫu
nêu tên một đoạn bắt đầu bằng dấu chấm (`crates/linch-tools/src/fs.rs:232-248`).

### Đọc trước khi ghi

`ReadTracker` (`crates/linch-tools/src/tracker.rs:24`) ghi lại, theo từng session và
đường dẫn workspace, stamp của tệp (thời gian sửa đổi và kích thước) cùng các dòng mà một
`Read` đã bao phủ. Một thay đổi stamp, do bất kỳ ai, làm bằng chứng vô hiệu. Một lần ghi
bị từ chối trả về mã `read_before_write` (`crates/linch-tools/src/input.rs:13`). Bằng
chứng nằm trong bộ nhớ: một process khởi động lại phải đọc lại trước khi ghi,
đó là chiều an toàn. Host gọi `ReadTracker::forget` khi một
session kết thúc (`tracker.rs:65`).

### Giới hạn truy cập

Mọi tool tệp đều đi qua trait `ExecutionFs` (`fs.rs:209`), nên một
thế giới thực thi từ xa có thể thay `LocalFs` mà không đổi các tool.
`LocalFs` (`crates/linch-tools/src/fs/local.rs:26`) canonicalize thư mục gốc
một lần; `new` là blocking, nên hãy gọi nó trước khi runtime khởi động hoặc từ một
blocking task. Mỗi đường dẫn được phân giải bằng cách canonicalize tiền tố dài nhất đã
tồn tại của nó; phần còn lại chỉ được chứa các tên thuần (`local.rs:68-72`). `..`, đường
dẫn tuyệt đối nằm ngoài thư mục gốc và symlink dẫn ra ngoài đều bị từ chối với
`FsError::Escape`. Các đường dẫn trả về là đường dẫn workspace: tương đối, phân tách bằng `/`,
symlink đã được phân giải, để một tệp có một tên. Phép kiểm tra bảo vệ các đối số đường
dẫn; nó không ngăn một process đồng thời hoán đổi một symlink giữa lúc kiểm tra
và lúc dùng. Công việc tệp chặn chạy trên blocking pool của Tokio.

### Bash

`Bash` (`crates/linch-tools/src/bash.rs:48`) không bị `LocalFs` giới hạn: nó
bắt đầu trong thư mục gốc, nhưng lệnh có thể chạm tới mọi thứ mà user của process truy
cập được. Hãy dùng permission engine (mặc định nó hỏi cho `exec`) hoặc một thế giới thực
thi được sandbox khi điều đó quan trọng. Những gì nó bảo đảm:

- stdin bị đóng và lệnh dẫn đầu một process group mới
  (`crates/linch-tools/src/bash/process.rs:66`);
- cả group nhận `SIGKILL` khi timeout, khi hủy, khi future của lời gọi
  bị drop, và khi shell thoát, nên các job nền không bao giờ sống lâu hơn
  lời gọi (`process.rs:135-148`);
- chỉ chín biến của host được truyền qua (`PATH`, `HOME`, `TERM`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `USER`, `SHELL`, `TMPDIR`), nên thông tin xác thực của host
  nằm ngoài môi trường (`bash.rs:33`); `with_env` thêm các biến khác (`bash.rs:82`);
- timeout mặc định là 120 s và bị chặn ở 30 phút (`bash.rs:22-25`);
  khi có deadline của lời gọi, nó kết thúc 100 ms trước deadline đó, để đầu ra cho đến lúc
  đó đến được model thay vì một timeout chung chung (`bash.rs:140`);
- mỗi stream giữ 4000 byte đầu và 26 000 byte cuối
  (`bash/capture.rs:5-8`);
- mọi exit status khác 0 là một kết quả lỗi mang cùng văn bản, với mã
  `exit_status`, `timeout` hoặc `aborted` (`bash.rs:151`).

## Hệ thống tệp ảo và chuyển kết quả ra ngoài

Hệ thống tệp ảo tách biệt với workspace: ghi chú nháp và các kết quả được chuyển ra
ngoài nằm ở bất cứ đâu backend giữ chúng. Hợp đồng,
`FileBackend` (`crates/linch-core/src/vfs.rs:83`), nằm trong lõi để một crate store có
thể triển khai nó mà không cần các tool. Nó lưu văn bản UTF-8 theo đường dẫn gốc `/` với
`read`, `write`, `edit`, `ls`, `exists` và `delete`. Mọi backend
chuẩn hóa đường dẫn bằng `normalize_path` (`vfs.rs:120`): `a//b/` và `/a/b` là
một tệp, `..` và NUL bị từ chối, và thư mục gốc không phải là tệp. `edit` là
nguyên tử theo từng backend và dùng `apply_edit` (`vfs.rs:163`). Mọi backend đều đạt
`vfs::conformance::backend_suite`.

Một tệp chứa tối đa `MAX_FILE_BYTES` (10 MiB, `vfs.rs:25`), giới hạn `Read` và `Edit` của
workspace. `apply_edit` từ chối một lần sửa mà kết quả sẽ vượt giới hạn đó, với
`VfsError::TooLarge`, dựa trên số lần khớp trước khi nó dựng kết quả, nên mọi backend
sửa qua nó (kể cả Postgres) đều bị giới hạn và `replace_all` không thể nhân một tệp lên
vô hạn. `write_file` từ chối nội dung vượt giới hạn; `write` riêng của một backend thì
không kiểm tra, để hook chuyển kết quả ra ngoài có thể lưu một kết quả lớn. Các tool báo
cả hai là `too_large`, nêu tên đường dẫn, và để tệp như cũ.

| Backend | Tệp nằm ở đâu |
|---|---|
| `MemoryBackend` | Bộ nhớ, mất cùng process |
| `DiskBackend` | Tệp thật dưới một thư mục gốc, bị giới hạn như `LocalFs` |
| `linch_postgres::PgFileBackend` | Bảng `linch_files`, theo namespace, dùng chung giữa các process |
| `CompositeBackend` | Định tuyến theo tiền tố đường dẫn dài nhất tới các backend khác |
| `ScopedBackend` | Một cây con của backend khác làm thư mục gốc riêng của nó |

Các tool và hook chuyển kết quả ra ngoài tìm backend của một session qua
`SessionFiles` (`crates/linch-tools/src/vfs.rs:53`): `MemorySessions` cấp cho
mỗi session một backend bộ nhớ mới (gọi `forget` khi nó kết thúc), và
`SharedFiles` cấp cho mọi session một backend chung.

`vfs_tools(files)` (`crates/linch-tools/src/vfs/tools.rs:35`) trả về `ls` và
`read_file` (scope read) cùng `write_file` và `edit_file` (scope write).
`vfs::PROMPT` (`tools.rs:24`) dạy model về hệ thống tệp ảo; host
thêm nó vào system prompt khi cung cấp các tool này.

`ResultOffload` (`crates/linch-tools/src/vfs/offload.rs:72`) là một hook
`post_tool_use`. Một kết quả thành công có giá trị là chuỗi và ước lượng vượt
`threshold_tokens` (bốn byte cho mỗi token) được ghi nguyên vẹn vào
`/offload/{tool}_{tool_use_id}.txt` và được thay bằng 10 dòng đầu cùng
một con trỏ mà model có thể theo bằng `read_file`. Lỗi, giá trị có cấu trúc và
bốn tool hệ thống tệp ảo không bao giờ được chuyển ra ngoài, nên đọc lại một tệp
đã chuyển ra ngoài không bao giờ bị chuyển ra ngoài lần nữa (`offload.rs:51-62`).

- Từ đó trở đi, bản thay thế là kết quả của lời gọi: event kết thúc tool,
  session log và các request sau đều giữ con trỏ, không giữ payload.
- Việc ghi vào backend hoàn tất trước khi hook trả lời, nên một con trỏ đã được ghi log
  nêu tên một tệp đã được ghi khi backend là bền vững. Một sự cố giữa lúc ghi
  và phần kết thúc của tool để lại một tệp không được tham chiếu; một lần thử lại lời gọi
  ghi cùng đường dẫn đó (`offload.rs:9-17`).
- Hook fail open (`offload.rs:119`): một lỗi lưu trữ hiện trong hook
  record và kết quả gốc đi tới model.
- Policy id `linch.vfs.offload`, phiên bản 1, của nó là một phần của run contract.

Với `MemorySessions`, một run được resume trong process khác sẽ thấy con trỏ nhưng
không thấy tệp.

## Tool task của session

`task_tools(store, namespace)` (`crates/linch-tools/src/tasks.rs:24`) trả về
`TaskCreate` và `TaskUpdate` (scope write) cùng `TaskList` và `TaskGet` (scope read)
trên một `TaskStore` (`crates/linch-core/src/store/tasks.rs`). Tên, schema và các văn bản
mà model nhìn thấy khớp với của Python. Task là dữ liệu của session, không phải log entry:
chỉ các lời gọi và kết quả của chúng đi vào session log. Hợp đồng của store nằm ở trang
[stores](./stores.md#task-store).

## Viết một tool bằng Rust

Đoạn này biên dịch và chạy được ở revision này. Nó đăng ký một tool scope read,
điều khiển một run gọi tool đó qua `ScriptedProvider`, rồi dispose đăng ký:

```rust
use std::sync::Arc;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Event, JsonObject, JsonValue, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, Tool,
    ToolContext, ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};
use serde_json::json;

/// Counts the words in a text. It only reads, so its calls may run in
/// parallel with other read calls and need no approval.
struct WordCount {
    schema: JsonObject,
}

impl WordCount {
    fn new() -> Self {
        let schema = json!({
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to count."}},
            "required": ["text"]
        });
        let JsonValue::Object(schema) = schema else { unreachable!() };
        Self { schema }
    }
}

#[async_trait]
impl Tool for WordCount {
    fn name(&self) -> &str {
        "word_count"
    }

    fn description(&self) -> &str {
        "Count the words in a text."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        if ctx.cancel.is_cancelled() {
            return Err(ToolOutputError::new("cancelled"));
        }
        let text = input.get("text").and_then(JsonValue::as_str).unwrap_or_default();
        ctx.report_progress("counting", None);
        Ok(ToolOutput::new(text.split_whitespace().count()))
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let tools = Arc::new(ToolRegistry::new());
    let registration = tools.register(Arc::new(WordCount::new())).unwrap();
    assert!(tools.register(Arc::new(WordCount::new())).is_err(), "names are unique");

    let provider = ScriptedProvider::new([
        TurnBuilder::new("m")
            .tool_call("c1", "word_count", &json!({"text": "one two three"}))
            .build(),
        TurnBuilder::new("m").text("Three words.").build(),
    ]);
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(Arc::new(provider))
        .tools(Arc::clone(&tools))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .build()
        .unwrap();
    let mut stream = agent.run("How many words?", RunOptions::new("s1"));
    let mut result = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            result = Some(end.result);
        }
    }
    assert!(!result.expect("the tool ran").is_error);

    registration.dispose();
    registration.dispose(); // a second dispose does nothing
    assert!(tools.snapshot().resolve("word_count").is_none());
}
```

Việc kết nối các tool tích hợp sẵn theo cùng khuôn mẫu. Đoạn này cũng biên dịch và
chạy được: nó đăng ký các tool workspace, các tool hệ thống tệp ảo và
hook chuyển kết quả ra ngoài, và giữ mọi handle:

```rust
use std::path::Path;
use std::sync::Arc;

use linch_core::{Hooks, Registration, ToolRegistry};
use linch_tools::vfs::{MemorySessions, OffloadConfig, ResultOffload, SessionFiles, vfs_tools};
use linch_tools::{LocalFs, ReadTracker, workspace_tools};

/// Register the workspace tools over `root`, the virtual filesystem tools,
/// and the offload hook. Keep the handles to undo every registration.
fn wire(root: &Path, tools: &ToolRegistry, hooks: &Hooks) -> Vec<Registration> {
    let fs = Arc::new(LocalFs::new(root).expect("an existing directory"));
    let reads = Arc::new(ReadTracker::new());
    let files: Arc<dyn SessionFiles> = Arc::new(MemorySessions::new());
    let mut registrations = Vec::new();
    for tool in workspace_tools(fs, reads).into_iter().chain(vfs_tools(Arc::clone(&files))) {
        registrations.push(tools.register(tool).expect("names are free"));
    }
    let offload = ResultOffload::new(files, OffloadConfig::new(8_000)).expect("a valid prefix");
    registrations.push(hooks.post_tool_use.register(Arc::new(offload)).expect("a policy id"));
    registrations
}

fn main() {
    let (tools, hooks) = (ToolRegistry::new(), Hooks::default());
    let registrations = wire(&std::env::temp_dir(), &tools, &hooks);
    assert_eq!(tools.snapshot().definitions().len(), 10);
    for registration in &registrations {
        registration.dispose();
    }
    assert!(tools.snapshot().definitions().is_empty());
}
```

Run contract digest bao phủ các tool được đăng ký khi
`AgentBuilder::build` chạy (`crates/linch-core/src/agent/builder.rs:216`). Một
tool được đăng ký sau đó, chẳng hạn một tool MCP sau một lần relist, được đưa ra từ
snapshot của lượt kế tiếp nhưng không thuộc contract.

---

Quay lại [mục lục kiến trúc](./README.md).
