# Kernel

[Bản gốc tiếng Anh](../../architecture/kernel.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Kernel của Rust rất nhỏ. Một `Agent` là một giá trị sở hữu mọi thứ mà các run của nó
đọc: spec, provider, tool registry, permission engine, các chuỗi hook và các store.
Mỗi đăng ký đều trả về một `Registration` hoàn tác đúng đăng ký đó. Các lời gọi tool đi
qua một đường plan rồi execute cố định, có ba điểm hook trên đó. Không có gì là toàn cục.
Dùng trang này khi bạn thêm một registry hoặc một điểm mở rộng, thêm một điểm hook,
hoặc kiểm tra rằng một thay đổi vẫn giữ các agent độc lập với nhau.

Linch bản Python dựng cùng các bảo đảm này trên một IoC container theo phạm vi
([kernel](../../../../docs/architecture/kernel.md)). Điểm khác trong Rust:

- Không có `Context`, không tra cứu service theo tên và không có event bus. Một agent
  được gắn bằng các giá trị truyền vào `AgentBuilder`, và `build` từ chối khi thiếu
  một giá trị bằng `BuildError::Missing` (`crates/linch-core/src/agent.rs:1-8`,
  `crates/linch-core/src/agent/builder.rs:222-231`).
- `Disposable` là `Registration`. `dispose` của nó là đồng bộ và chạy phần hoàn tác
  một lần; hoàn tác thất bại sẽ không được thử lại (`crates/linch-core/src/registration.rs:38-48`).
- Các dispatcher `emit`, `serial` và `waterfall` của Python không có tương đương trong
  Rust. Các điểm mở rộng là bảy chuỗi hook có kiểu, mỗi chuỗi cho một `HookPoint`,
  mỗi chuỗi có quy tắc riêng để kết hợp các câu trả lời
  ([chuỗi hook](#chuỗi-hook)).
- Không có `ToolPipeline` với các seam `tools/pre-execute`, `tools/execute` và
  `tools/post-execute`. Một batch chạy qua `plan` và `execute`
  (`crates/linch-core/src/scheduler.rs:1-22`); các chuỗi `pre_tool_use` và
  `post_tool_use` là nơi để thêm hành vi, và `execute` không có waterfall nào để bọc
  ([các seam của lời gọi tool](#các-seam-của-lời-gọi-tool)).
- Trong `linch-core` không có plugin và không có profile theo miền. Các điểm mở rộng
  là trait (`Provider`, `Tool`, `RunStore`, `SessionStore`,
  `TaskStore`, `FileBackend`, `Hook`) được đưa vào `AgentBuilder`.

## Agent là scope

`Agent` là một `Arc<AgentInner>`. Nó rẻ để clone, và các bản clone dùng chung mọi
binding (`agent.rs:101-105`). `AgentInner` giữ những gì một run đọc từ nó
(`agent.rs:108-162`):

| Trường | Là gì |
|---|---|
| `spec` | `AgentSpec` serde: model, prompt, giới hạn, compaction, budget (`agent/spec.rs:29-68`) |
| `provider`, `permissions`, `runs`, `sessions` | Các binding mà builder yêu cầu |
| `tools` | `ToolRegistry` của agent này |
| `hooks` | Các chuỗi `Hooks` của agent này |
| `namespace` | Không gian khóa của các run và session của agent này |
| `contract` | Digest của những gì một run cần để resume (bên dưới) |
| `families`, `worker_registry` | Các run con của subagent và các worker thuộc về các run và session của agent này |
| `registrations`, `reloads` | Các đăng ký mà chính agent này đã thực hiện, được hoàn tác khi drop |

`AgentBuilder::build` (`agent/builder.rs:207-323`) kiểm tra rằng provider,
permission engine, cả hai store và một namespace đều có mặt, và model cùng namespace
không để trống. Nó đăng ký các tool tích hợp sẵn mà cài đặt yêu cầu
(`Skill`, `send_message`, `Subagent`, `SubagentContinue`, `TaskStop`,
`tool_search`) vào registry của agent, rồi tính digest của contract. Không có gì trong
`build` thực hiện I/O (`builder.rs:24-26`).

### Run contract

`contract_digest` băm spec, các tool được đăng ký lúc build cùng scope của chúng,
digest chính sách hook và các tính năng khác
(`builder.rs:285-288`). Mỗi run lưu digest này. `resume` và `approve`
từ chối một run có digest khác với digest của agent bằng
`RunError::ContractMismatch` (`runner/start.rs:425-431`). Digest chính sách hook
bao gồm point, `policy_id` và `policy_version` của từng hook, theo thứ tự trong chuỗi
(`hooks.rs:84-100`), nên đổi hành vi của một hook nghĩa là phải tăng
`policy_version` của nó (`hooks/hook.rs:76-78`).

## Đăng ký

`Registration` (`registration.rs:19-21`) giữ một closure hoàn tác:

- `dispose` lấy closure ra và chạy nó. Lần gọi thứ hai không thấy gì và
  không làm gì (`registration.rs:38-48`).
- Drop handle không hoàn tác gì cả. Bên gọi không bao giờ dispose sẽ giữ đăng ký
  suốt vòng đời của registry (`registration.rs:5-6`,
  được cố định bởi `dropping_does_not_undo`, `registration.rs:88-97`).
- `Registration::noop` là một handle đã hoàn tác sẵn (`registration.rs:31-34`).
  `is_disposed` cho biết phần hoàn tác đã chạy chưa (`registration.rs:51-56`).

Mỗi phần hoàn tác chỉ gỡ những gì đăng ký của nó đã thêm. Trong `linch-core`, chúng
đến từ tool registry, các chuỗi hook và các reload của agent (các reload dựng
đăng ký của chúng bằng `Versions::push`, `agent/versions.rs:49`):

| Registry | Đăng ký | Hoàn tác |
|---|---|---|
| `ToolRegistry::register` và `replace` | `registry.rs:86-97` | Gỡ đăng ký `id` dưới tên đó. Chỉ tăng generation nếu đăng ký đó là đăng ký đang hiển thị, nên dispose một tool bị thay thế và đã bị ẩn thì không thay đổi gì (`registry.rs:141-149`, `251-271`) |
| `HookChain::register` | `hooks/chain.rs:132-180` | Gỡ mục có id của đăng ký này, chỉ khi nó vẫn còn đó. Đăng ký cùng một hook hai lần cho hai mục độc lập (`chain.rs:123-124`, `166-179`) |
| `Agent::reload_skills`, `reload_subagents` | `agent/reload.rs:39`, `64-86` | Khôi phục tập mà agent có trước đó, trừ khi một reload sau đó đã thay thế nó |

Cả hai registry giữ trạng thái của phần hoàn tác sau một con trỏ `Weak`
(`registry.rs:141-143`, `hooks/chain.rs:166-169`), nên một handle sống lâu hơn
registry của nó sẽ không làm gì.

Agent dispose các đăng ký của chính nó khi bị drop: các reload trước, mới nhất trước,
rồi đến các tool tích hợp sẵn (`agent.rs:202-217`). Một lần build thất bại giữa chừng
sẽ dispose các tool tích hợp sẵn mà nó đã đăng ký (`builder.rs:326-355`).

## Chuỗi hook

Một hook chạy tại một point. `Point` cố định kiểu request và response của point đó,
nên một hook chỉ có thể trả lời những gì point cho phép. Nó được niêm phong (sealed):
chỉ các kiểu đánh dấu trong `linch-core` mới implement nó (`hooks/hook.rs:38-61`).
Một `Hook<P>` cung cấp `policy_id` và `policy_version`, và có thể cung cấp một bộ khớp
tool, một timeout và một fail mode. Chuỗi đọc các giá trị này một lần, lúc đăng ký
(`hooks/hook.rs:63-99`). Một hook phục vụ nhiều point implement trait một lần cho mỗi point.

`Hooks` giữ một `HookChain` cho mỗi point (`hooks.rs:53-70`) và được đưa cho
builder (`builder.rs:85-89`). Một chuỗi được dùng chung giữa các run. Mỗi `run` làm việc
trên một snapshot của các mục được lấy khi nó bắt đầu, nên một đăng ký diễn ra trong
lúc đó chỉ có hiệu lực từ run kế tiếp (`hooks/chain.rs:61-65`, `188-191`).
`register` từ chối một policy id để trống, một bộ khớp tại point không liên quan đến
tool, và một fail mode closed tại `context_build` hoặc `post_turn`
(`hooks/chain.rs:132-146`).

Mỗi chuỗi tự quyết định cách kết hợp các câu trả lời. Một chuỗi chỉ dừng sớm
ở những nơi bảng dưới đây nói như vậy:

| Point | Quy tắc | Fail mode mặc định |
|---|---|---|
| `user_prompt_submit` | Một lần viết lại được chuyển cho hook kế tiếp. Một block hoặc một lỗi closed dừng chuỗi và run không bao giờ được tạo (`hooks/turn.rs:83-105`, `runner/hooks.rs:81-115`) | open |
| `context_build` | Mọi hook đều chạy. Các block được nối theo thứ tự; metadata được gộp, khóa đến sau thắng. Không thể fail closed (`hooks/turn.rs:150-154`) | open |
| `pre_tool_use` | Một lần viết lại được chuyển cho hook kế tiếp. Một block, một resolve hoặc một lỗi closed dừng chuỗi. Chỉ khi chuỗi đã chạy mọi hook khớp thì tool mới được chạy (`hooks/tool.rs:101-136`) | closed |
| `permission_request` | Câu trả lời nghiêm ngặt nhất thắng. Một deny hoặc lỗi closed dừng chuỗi; một allow thì không (`hooks/tool.rs:226-258`) | closed |
| `post_tool_use` | Một bản thay thế được chuyển cho hook kế tiếp. Một lỗi closed giữ lại kết quả và dừng chuỗi (`hooks/tool.rs:312-336`) | open |
| `stop` | Hook đầu tiên giữ run tiếp tục sẽ dừng chuỗi. Một lỗi closed làm run thất bại (`hooks/turn.rs:246-250`) | open |
| `post_turn` | Mọi hook đều chạy; lỗi chỉ được ghi lại. Không thể fail closed (`hooks/turn.rs:310-312`) | open |

Các giá trị mặc định nằm trong `HookPoint::default_fail_mode` (`policy.rs:140-149`).
Việc xử lý lỗi nằm trong `Entry::invoke` và `failed` (`hooks/chain.rs:208-259`):
một hook gặp lỗi hoặc quá thời gian sẽ bị bỏ qua nếu là open, và bị biến thành câu
trả lời chặn của point nếu là closed.

Hook không phải là barrier. Không có gì được flush quanh một chuỗi, và một chuỗi không có
hook nào sẽ bị bỏ qua trước khi request của nó được dựng, nên một run không có hook
sẽ không làm việc gì cho hook (`runner/hooks.rs:1-9`). Mỗi lần gọi để lại một `HookRecord`,
mà run gửi cho bên gọi dưới dạng event `hook` chỉ có trong stream, tại seq bền vững
gần nhất (`runner/hooks.rs:41-79`).

## Các seam của lời gọi tool

Các lời gọi tool của một response đi qua hai hàm để một run có thể suspend ở giữa
chúng ([tools](./tools.md#cách-một-batch-chạy)).

`plan` (`scheduler/plan.rs:139-158`) quyết định mọi lời gọi và không chạy gì.
Với mỗi lời gọi, theo thứ tự (`plan.rs:168-206`):

1. Phân giải tên trong snapshot của lượt. Một tool không xác định cho ra một kết quả lỗi.
2. Xác thực input theo schema của tool.
3. Chạy chuỗi `pre_tool_use` (`plan.rs:212-254`), rồi xác thực lại input cuối cùng.
   Một câu trả lời `Resolve` trở thành một `Serve` tạm thời.
4. Đánh giá quyền, và khi gặp `Ask` thì chạy chuỗi `permission_request`
   (`plan.rs:258-293`). Sau đó áp dụng câu trả lời của host đã được ghi lại: một deny
   đã ghi luôn từ chối; một allow đã ghi chỉ nâng một `Ask` lên khi input đúng là
   input mà host đã thấy (`plan.rs:286-312`).

`execute` (`scheduler/execute.rs:120-153`) chạy một plan đã quyết định và trả về một
kết quả cho mỗi lời gọi, theo thứ tự lời gọi. Quanh mỗi tool, nó hỏi `ToolSink`:
tool chỉ bắt đầu sau khi `tool_starting` trả về, và mỗi kết quả được chuyển cho
`tool_finished` ngay khi nó có (`execute.rs:34-55`, `189-223`). Một giá trị thành công
được kiểm tra theo `output_schema` của tool, rồi chuỗi `post_tool_use` chạy
(`execute.rs:310-338`). Một tool bị panic cho ra một kết quả lỗi
(`execute.rs:341-353`).

Để thêm hành vi vào việc thực thi tool, dùng một trong các cách sau:

- Một hook `pre_tool_use` để viết lại, chặn hoặc phục vụ một lời gọi.
- Một hook `post_tool_use` để thay thế hoặc che một kết quả.
- Một wrapper `Tool` được đăng ký bằng `ToolRegistry::replace`, thứ ẩn tool được bọc
  và được hoàn tác bằng `Registration` của nó.
- `call_timeout_ms` và `max_tool_concurrency` trong spec cho timeout và
  giới hạn song song (`agent/spec.rs:53-59`).

Đừng sửa `scheduler/` hay `runner/` cho một cổng kiểm soát mới.

## Không có biến toàn cục, nhiều agent

Một `Agent` không có tra cứu theo tên và không có trạng thái toàn cục: mọi binding là
một giá trị mà host truyền vào (`agent.rs:6-8`). Trạng thái theo từng agent nằm trong
`AgentInner`: registry, hook, store, các family và worker của subagent
(`agent.rs:108-162`). Trạng thái theo từng run nằm trong `Runner` của run
(`runner/turn.rs:88-121`). Handle dùng chung duy nhất, từ một worker tool trở lại
agent của nó, là một `OnceLock<Weak<AgentInner>>` theo từng agent được đặt ở cuối
`build` (`subagents/control.rs:14-18`, `builder.rs:250`, `320-321`). Nó là `Weak`, nên
registry giữ tool không giữ agent sống.

Các tenant được tách theo namespace, không theo process. Namespace của một agent là
một phần của mọi khóa store, nên hai agent có namespace khác nhau không bao giờ dùng
chung run hay session, kể cả trên cùng một cơ sở dữ liệu (`builder.rs:103-107`,
[stores](./stores.md#khóa-và-đa-tenant)). Nhiều agent chạy trên một Tokio runtime.
Một run chỉ tiến lên khi stream của nó được poll (`runner.rs:36-39`), và lõi chỉ spawn
một loại task: task điều khiển các subagent chạy nền của một run đang suspend
(`runner.rs:38-39`).

Một registry hay chuỗi hook mà bạn thêm phải theo cùng các quy tắc: nó nằm trong
`AgentInner` hoặc trên một giá trị mà builder nhận, nó trao ra các `Registration`,
và phần hoàn tác được kiểm tra định danh.
