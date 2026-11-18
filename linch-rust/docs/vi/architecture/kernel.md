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
  `crates/linch-core/src/agent/builder.rs:333-347`).
- `Disposable` là `Registration`. `dispose` của nó là đồng bộ và chạy phần hoàn tác
  một lần; hoàn tác thất bại sẽ không được thử lại (`crates/linch-core/src/registration.rs:38-48`).
- Các dispatcher `emit`, `serial` và `waterfall` của Python không có tương đương trong
  Rust. Các điểm mở rộng là hai mươi hai chuỗi hook có kiểu cộng waterfall `tool_execute`, mỗi cái cho một `HookPoint`,
  mỗi chuỗi có quy tắc riêng để kết hợp các câu trả lời
  ([chuỗi hook](#chuỗi-hook)). Mười ba trong số đó chỉ quan sát: được thêm
  để tương đương với các lifecycle hook của Python, chúng trả lời `()`/`{}`
  và không thể fail closed (`hooks/observe.rs`).
- Các seam của `ToolPipeline` trong Python ánh xạ vào các điểm hook.
  `tools/pre-execute` là `pre_tool_use`, `tools/post-execute` là `post_tool_use`
  (cũng có thể `Mutate` một kết quả), và `tools/execute` là waterfall `tool_execute`
  (`hooks/execute.rs`): một `ToolMiddleware` bọc một lần thử của một lời gọi và
  phải await `Next::run` để chuyển vào bên trong, hoặc phủ quyết lời gọi bằng cách
  không làm vậy ([các seam của lời gọi tool](#các-seam-của-lời-gọi-tool)). Một batch
  vẫn chạy qua `plan` và `execute` (`crates/linch-core/src/scheduler.rs:1-22`).
- Trong `linch-core` không có plugin và không có profile theo miền. Các điểm mở rộng
  là trait (`Provider`, `Tool`, `RunStore`, `SessionStore`,
  `TaskStore`, `FileBackend`, `Hook`) được đưa vào `AgentBuilder`.

## Agent là scope

`Agent` là một `Arc<AgentInner>`. Nó rẻ để clone, và các bản clone dùng chung mọi
binding (`agent.rs:167-171`). `AgentInner` giữ những gì một run đọc từ nó
(`agent.rs:173-252`):

| Trường | Là gì |
|---|---|
| `spec` | `AgentSpec` serde: model, prompt, giới hạn, compaction, budget, provider retry và fallback model (`agent/spec.rs:36-204`) |
| `provider`, `permissions`, `runs`, `sessions` | Các binding mà builder yêu cầu |
| `tools` | `ToolRegistry` của agent này |
| `hooks` | Các chuỗi `Hooks` của agent này |
| `namespace` | Không gian khóa của các run và session của agent này |
| `contract` | Digest của những gì một run cần để resume (bên dưới) |
| `families`, `worker_registry` | Các run con của subagent và các worker thuộc về các run và session của agent này |
| `registrations`, `reloads` | Các đăng ký mà chính agent này đã thực hiện, được hoàn tác khi drop |

`AgentBuilder::build` (`agent/builder.rs:267-522`) kiểm tra rằng provider,
permission engine, cả hai store và một namespace đều có mặt, và model cùng namespace
không để trống. Nó đăng ký các tool tích hợp sẵn mà cài đặt yêu cầu
(`Skill`, `send_message`, `Subagent`, `SubagentContinue`, `TaskStop`,
`tool_search`) vào registry của agent, rồi tính digest của contract. Không có gì trong
`build` thực hiện I/O (`builder.rs:32-34`).

### Run contract

`contract_digest` băm spec, các tool được đăng ký lúc build cùng scope của chúng,
digest chính sách hook và các tính năng khác
(`builder.rs:470-476`). Mỗi run lưu digest này. `resume` và `approve`
từ chối một run có digest khác với digest của agent bằng
`RunError::ContractMismatch` (`runner/start.rs:765-771`). Digest chính sách hook
bao gồm point, `policy_id` và `policy_version` của từng hook, theo thứ tự trong chuỗi
(`hooks.rs:145-192`), nên đổi hành vi của một hook nghĩa là phải tăng
`policy_version` của nó (`hooks/hook.rs:76-78`). Điều đó bao gồm cả thứ một hook
trả lời, kể cả `retry`. Giới hạn riêng của kernel cho các lần retry ở `stop` (một
lần cho mỗi lần gọi run) là một hằng số, không phải một thiết lập, nên nó không nằm
trong digest; nó cũng không được checkpoint, và một lần resume bắt đầu lại từ đầu,
như `_MAX_FINAL_ANSWER_REENTRIES` của Python. Một agent có spec `verification` thì
giới hạn chúng bằng `max_retries` của spec đó, một thiết lập nằm trong digest cùng
phần còn lại của spec, được đếm từ các event `verification` bền vững nên một lần resume
giữ nguyên nó. `redaction` và `verification` của spec bị bỏ khỏi wire khi không đặt,
nên một agent không có chúng giữ nguyên digest của mình
(`tests/e2e_guardrails_off.rs` ghim điều này). Chính sách permission là một
feature (`permissions`) khi nó không phải chính sách mặc định
(`PermissionPolicy::is_default`: mode `default` và không có layer, trường
`auto_allow`, path hay command), nên một run không bao giờ được resume hay approve
dưới một chính sách cho phép nhiều hơn hoặc ít hơn chính sách của nó, như hợp đồng
của Python (`src/linch/loop/contracts.py:441`) giữ. Chính sách mặc định không quyết
định gì bằng `project_root` của nó, nên nó bị bỏ ra dù root là gì, và digest của các
agent dùng chính sách mặc định giữ nguyên như cũ
(`tests/e2e_policy_contract.rs` ghim một cái, lấy trước khi chính sách được tính);
một chính sách không mặc định được đưa vào đầy đủ, cả root. `Agent::cancel` không
kiểm tra contract. `provider_retry` và
`fallback_models` của spec nằm trong digest cùng phần còn lại của spec, nên model
fallback của một run được resume, đọc lại từ các event `model_fallback` của nó theo
vị trí trong danh sách, trỏ tới cùng một model. Retry được bật mặc định, nhưng chính
sách mặc định không được đưa vào wire, nên khi không đặt `provider_retry` (và
`fallback_models` rỗng), chúng không có trong dạng wire của spec, và do đó digest
vẫn như trước khi chúng tồn tại; `{"max_attempts": 1}`, tắt retry, làm digest đổi.

## Đăng ký

`Registration` (`registration.rs:19-21`) giữ một closure hoàn tác:

- `dispose` lấy closure ra và chạy nó. Lần gọi thứ hai không thấy gì và
  không làm gì (`registration.rs:38-48`).
- Drop handle không hoàn tác gì cả. Bên gọi không bao giờ dispose sẽ giữ đăng ký
  suốt vòng đời của registry (`registration.rs:5-6`,
  được cố định bởi `dropping_does_not_undo`, `registration.rs:88-97`).
- `Registration::noop` là một handle đã hoàn tác sẵn (`registration.rs:31-36`).
  `is_disposed` cho biết phần hoàn tác đã chạy chưa (`registration.rs:51-56`).

Mỗi phần hoàn tác chỉ gỡ những gì đăng ký của nó đã thêm. Trong `linch-core`, chúng
đến từ tool registry, các chuỗi hook và các reload của agent (các reload dựng
đăng ký của chúng bằng `Versions::push`, `agent/versions.rs:49`):

| Registry | Đăng ký | Hoàn tác |
|---|---|---|
| `ToolRegistry::register` và `replace` | `registry.rs:83-98` | Gỡ đăng ký `id` dưới tên đó. Chỉ tăng generation nếu đăng ký đó là đăng ký đang hiển thị, nên dispose một tool bị thay thế và đã bị ẩn thì không thay đổi gì (`registry.rs:142-150`, `266-287`) |
| `HookChain::register` | `hooks/chain.rs:132-180` | Gỡ mục có id của đăng ký này, chỉ khi nó vẫn còn đó. Đăng ký cùng một hook hai lần cho hai mục độc lập (`chain.rs:123-124`, `166-179`) |
| `Agent::reload_skills`, `reload_subagents` | `agent/reload.rs:39`, `64-86` | Khôi phục tập mà agent có trước đó, trừ khi một reload sau đó đã thay thế nó |

Cả hai registry giữ trạng thái của phần hoàn tác sau một con trỏ `Weak`
(`registry.rs:142-144`, `hooks/chain.rs:166-169`), nên một handle sống lâu hơn
registry của nó sẽ không làm gì.

Agent dispose các đăng ký của chính nó khi bị drop: các reload trước, mới nhất trước,
rồi đến các tool tích hợp sẵn (`agent.rs:314-330`). Một lần build thất bại giữa chừng
sẽ dispose các tool tích hợp sẵn mà nó đã đăng ký (`builder.rs:525-554`).

## Chuỗi hook

Một hook chạy tại một point. `Point` cố định kiểu request và response của point đó,
nên một hook chỉ có thể trả lời những gì point cho phép. Nó được niêm phong (sealed):
chỉ các kiểu đánh dấu trong `linch-core` mới implement nó (`hooks/hook.rs:38-61`).
Một `Hook<P>` cung cấp `policy_id` và `policy_version`, và có thể cung cấp một bộ khớp
tool, một timeout và một fail mode. Chuỗi đọc các giá trị này một lần, lúc đăng ký
(`hooks/hook.rs:63-99`). Một hook phục vụ nhiều point implement trait một lần cho mỗi point.

`Hooks` giữ một `HookChain` cho mỗi point trừ `tool_execute`, vốn là một `ToolPipeline` (`hooks.rs:92-142`) và được đưa cho
builder (`builder.rs:99-103`). Một chuỗi được dùng chung giữa các run. Mỗi `run` làm việc
trên một snapshot của các mục được lấy khi nó bắt đầu, nên một đăng ký diễn ra trong
lúc đó chỉ có hiệu lực từ run kế tiếp (`hooks/chain.rs:61-65`, `188-191`).
`register` từ chối một policy id để trống, một bộ khớp tại point không liên quan đến
tool, và một fail mode closed tại `context_build` hoặc `post_turn`
(`hooks/chain.rs:132-146`).

Mỗi chuỗi tự quyết định cách kết hợp các câu trả lời. Một chuỗi chỉ dừng sớm
ở những nơi bảng dưới đây nói như vậy:

| Point | Quy tắc | Fail mode mặc định |
|---|---|---|
| `user_prompt_submit` | Một lần viết lại được chuyển cho hook kế tiếp. Một block hoặc một lỗi closed dừng chuỗi và run không bao giờ được tạo (`hooks/turn.rs:87-109`, `runner/hooks.rs:134-180`) | open |
| `context_build` | Mọi hook đều chạy. Các block được nối theo thứ tự; metadata được gộp, khóa đến sau thắng. Không thể fail closed (`hooks/turn.rs:157-169`) | open |
| `pre_tool_use` | Một lần viết lại được chuyển cho hook kế tiếp. Một block, một resolve hoặc một lỗi closed dừng chuỗi. Chỉ khi chuỗi đã chạy mọi hook khớp thì tool mới được chạy (`hooks/tool.rs:103-136`) | closed |
| `permission_request` | Câu trả lời nghiêm ngặt nhất thắng. Một deny hoặc lỗi closed dừng chuỗi; một allow thì không (`hooks/tool.rs:228-258`) | closed |
| `post_tool_use` | Một bản thay thế hoặc một `Mutate` (một giá trị mới, hoặc cờ lỗi, giữ nguyên metadata và mã lỗi) được chuyển cho hook kế tiếp. Một lỗi closed giữ lại kết quả và dừng chuỗi (`hooks/tool.rs`) | open |
| `tool_execute` | Một waterfall, không phải chuỗi các câu trả lời: layer được đăng ký đầu tiên là ngoài cùng, và mỗi layer await `Next::run` để chạy các layer bên trong rồi đến tool, hoặc trả về một kết quả mà không gọi nó để phủ quyết lời gọi. Chạy một lần cho mỗi lần thử, nên một lời gọi được retry sẽ đi qua nó lần nữa. Một lỗi closed làm lời gọi thất bại (hoặc giữ lại kết quả khi nó xảy ra sau khi tool đã chạy) (`hooks/execute.rs`) | closed |
| `before_provider_call` | Một lần viết lại được chuyển cho hook kế tiếp; bản viết lại cuối cùng là bản được gửi đi và được `context_build` ghi lại. Một `force_continue` dừng chuỗi và bỏ qua lời gọi: không có `context_build` hay delivery nào được ghi, feedback của hook đi tới model trong một `<system-reminder>`, được ghi bền trước lượt kế tiếp như một stop feedback, và run chạy thêm một lượt, bị giới hạn bởi `max_turns` và bởi `max_idle_turns` của loop guard ([vòng đời của lượt](./turn-lifecycle.md#loop-guard); `force_continue` của Python tại đó). Một block hoặc một lỗi closed dừng chuỗi và làm run thất bại, trước khi provider được gọi (`hooks/call.rs`) | open, nhưng block của nó có thể fail closed |
| `after_provider_call` | Một lần viết lại được chuyển cho hook kế tiếp; bản viết lại cuối cùng là bản được stage và nối vào session. Một retry dừng chuỗi: response (cùng mọi lần viết lại trước đó) được chấp nhận, các lời gọi tool của nó không chạy, và phản hồi của hook đến model trước một lượt khác. Một block hoặc một lỗi closed dừng chuỗi và làm run thất bại, trước khi response được stage (`hooks/assembly.rs`). Receipt và usage của lời gọi đã bền vững khi chuỗi chạy ([vòng đời của lượt](./turn-lifecycle.md#một-lần-crash-giữa-receipt-và-response-của-nó)) | open, nhưng block của nó có thể fail closed |
| `stop` | Một lần viết lại được chuyển cho hook kế tiếp; bản viết lại cuối cùng là văn bản trả lời cuối cùng của run, không phải tin nhắn assistant đã được nối vào session từ trước. Hook đầu tiên giữ run tiếp tục (`Continue`, chỉ bị giới hạn bởi `max_turns`) sẽ dừng chuỗi; một `Retry` cũng vậy khi lần gọi run còn lượt retry duy nhất của nó, sau đó một retry được tính như allow. Một block hoặc một lỗi closed làm run thất bại (`hooks/turn.rs`). Với một output schema, nó chỉ chạy trên một câu trả lời thỏa schema, request của nó mang `structured_output` đã parse, và một bản viết lại được kiểm tra lại: bản không thỏa schema làm run thất bại | open |
| `post_turn` | Mọi hook đều chạy; lỗi chỉ được ghi lại. Không thể fail closed (`hooks/turn.rs:402-421`) | open |
| `agent_start`, `agent_stop`, `turn_start`, `provider_call_start`, `provider_call_stop`, `tool_use_start`, `tool_use_stop`, `post_tool_use_failure`, `pre_compact`, `post_compact`, `subagent_start`, `subagent_stop`, `event_emit` | Mọi hook đều chạy; lỗi chỉ được ghi lại. Không thể fail closed (`hooks/observe.rs`). `event_emit` chạy một lần cho mỗi event sắp được gửi tới bên gọi, nhưng chỉ với event vừa được stream vừa được ghi bền: không bao giờ cho record của chính một hook, cũng không cho một delta hay tool-progress chỉ stream (`runner/hooks.rs`) | open |

Các giá trị mặc định nằm trong `HookPoint::default_fail_mode` (`policy.rs:193-221`).
Việc xử lý lỗi nằm trong `Entry::invoke` và `failed` (`hooks/chain.rs:208-259`):
một hook gặp lỗi hoặc quá thời gian sẽ bị bỏ qua nếu là open, và bị biến thành câu
trả lời chặn của point nếu là closed.

Hook không phải là barrier. Không có gì được flush quanh một chuỗi, và một chuỗi không có
hook nào sẽ bị bỏ qua trước khi request của nó được dựng, nên một run không có hook
sẽ không làm việc gì cho hook (`runner/hooks.rs:1-9`). Mỗi lần gọi để lại một `HookRecord`,
mà run gửi cho bên gọi dưới dạng event `hook` chỉ có trong stream, tại seq bền vững
gần nhất (`runner/hooks.rs:94-132`).

## Các seam của lời gọi tool

Các lời gọi tool của một response đi qua hai hàm để một run có thể suspend ở giữa
chúng ([tools](./tools.md#cách-một-batch-chạy)).

`plan` (`scheduler/plan.rs:147-176`) quyết định mọi lời gọi và không chạy gì.
Với mỗi lời gọi, theo thứ tự (`plan.rs:237-287`):

1. Phân giải tên trong snapshot của lượt. Một tool không xác định cho ra một kết quả lỗi.
2. Xác thực input theo schema của tool.
3. Chạy chuỗi `pre_tool_use` (`plan.rs:289-335`), rồi xác thực lại input cuối cùng.
   Một câu trả lời `Resolve` trở thành một `Serve` tạm thời.
4. Đánh giá quyền, và khi gặp `Ask` thì chạy chuỗi `permission_request`
   (`plan.rs:337-366`). Sau đó áp dụng câu trả lời của host đã được ghi lại: một deny
   đã ghi luôn từ chối; một allow đã ghi chỉ nâng một `Ask` lên khi input đúng là
   input mà host đã thấy (`plan.rs:367-393`).

`execute` (`scheduler/execute.rs:153-207`) chạy một plan đã quyết định và trả về một
kết quả cho mỗi lời gọi, theo thứ tự lời gọi. Quanh mỗi tool, nó hỏi `ToolSink`:
tool chỉ bắt đầu sau khi `tool_starting` trả về, và mỗi kết quả được chuyển cho
`tool_finished` ngay khi nó có (`execute.rs:37-52`, `279-307`). Một giá trị thành công
được kiểm tra theo `output_schema` của tool, rồi, khi redaction của spec bao gồm kết quả
tool, `ExecuteOptions::redactor` che mọi chuỗi của kết quả, rồi chuỗi `post_tool_use`
chạy trên kết quả đã che, và một bản thay thế mà nó tạo ra lại bị che lần nữa
(`execute.rs`, `finish`). Khi đặt redactor đó, các báo cáo tiến độ bị bỏ. Một tool bị
panic cho ra một kết quả lỗi
(`execute.rs:737-749`).

Để thêm hành vi vào việc thực thi tool, dùng một trong các cách sau:

- Một hook `pre_tool_use` để viết lại, chặn hoặc phục vụ một lời gọi.
- Một hook `post_tool_use` để thay thế, mutate hoặc che một kết quả.
- Một layer `tool_execute` để bọc chính phần thân của tool: đo thời gian, tracing, một
  guard phủ quyết lời gọi, một lần viết lại kết quả trên đường đi ra. Khi không có layer
  nào được đăng ký thì phần thân được gọi trực tiếp.
- Một wrapper `Tool` được đăng ký bằng `ToolRegistry::replace`, thứ ẩn tool được bọc
  và được hoàn tác bằng `Registration` của nó.
- `call_timeout_ms` và `max_tool_concurrency` trong spec cho timeout và
  giới hạn song song (`agent/spec.rs:87-93`).

Đừng sửa `scheduler/` hay `runner/` cho một cổng kiểm soát mới. Redaction của spec là
ngoại lệ chứng minh quy tắc: nó không phải một hook vì nó phải chạy sau mọi hook, kể cả
khi không có hook nào được gắn, và trước log, nên nó nằm ở những vị trí cố định trong các
seam sẵn có (`after_provider_call` và `stop` trong `runner/hooks.rs`, `finish` trong
`scheduler/execute.rs`, prompt trong `submit`) thay vì trong một chuỗi mà host kiểm soát
thứ tự.

## Không có biến toàn cục, nhiều agent

Một `Agent` không có tra cứu theo tên và không có trạng thái toàn cục: mọi binding là
một giá trị mà host truyền vào (`agent.rs:6-8`). Trạng thái theo từng agent nằm trong
`AgentInner`: registry, hook, store, các family và worker của subagent
(`agent.rs:173-252`). Trạng thái theo từng run nằm trong `Runner` của run
(`runner/turn.rs:123-207`). Handle dùng chung duy nhất, từ một worker tool trở lại
agent của nó, là một `OnceLock<Weak<AgentInner>>` theo từng agent được đặt ở cuối
`build` (`subagents/control.rs:17-19`, `builder.rs:366`, `504-505`). Nó là `Weak`, nên
registry giữ tool không giữ agent sống.

Các tenant được tách theo namespace, không theo process. Namespace của một agent là
một phần của mọi khóa store, nên hai agent có namespace khác nhau không bao giờ dùng
chung run hay session, kể cả trên cùng một cơ sở dữ liệu (`builder.rs:117-121`,
[stores](./stores.md#khóa-và-đa-tenant)). Nhiều agent chạy trên một Tokio runtime.
Một run chỉ tiến lên khi stream của nó được poll (`runner.rs:39-40`), và lõi chỉ spawn
một loại task: task điều khiển các subagent chạy nền của một run đang suspend
(`runner.rs:41-44`).

Một registry hay chuỗi hook mà bạn thêm phải theo cùng các quy tắc: nó nằm trong
`AgentInner` hoặc trên một giá trị mà builder nhận, nó trao ra các `Registration`,
và phần hoàn tác được kiểm tra định danh.
