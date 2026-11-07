# Skill và subagent

[Bản gốc tiếng Anh](../../architecture/skills-subagents.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Skill và subagent là các tool tích hợp sẵn của `linch-core`. Tool `Skill`
trả prompt của một skill cho model (`crates/linch-core/src/skills.rs`,
`skills/`); tool `Subagent` chạy một tác vụ dưới dạng một run con bền vững
(`crates/linch-core/src/subagents.rs`, `subagents/`), ở foreground hoặc ở
background, và các worker tool địa chỉ hóa các run con mà nó giữ lại.
Các run con của một run nằm trong family của nó (`crates/linch-core/src/runner/family.rs`).
Dùng trang này khi bạn đổi các tool này, cách các run con bắt đầu, resume hoặc
kết thúc, hoặc cách một lần reload thay thế skill và subagent.

Linch bản Python có cùng các tool này ([skill và subagent](../../../../docs/architecture/skills-subagents.md));
[chỗ Rust khác biệt](#chỗ-rust-khác-python) nằm ở cuối trang. Các trang hướng dẫn sử dụng
([skills](../usage/skills.md), [subagents](../usage/subagents.md))
trình bày chúng từ client Python.

## Skill

`AgentBuilder::skills` (`crates/linch-core/src/agent/builder.rs:152`) bật
skill bằng các giá trị `Skill` của host (`crates/linch-core/src/skills.rs:49`);
facade `linch` nạp chúng từ `.linch/skills/*/SKILL.md`. Lúc build
(`builder.rs:235-245`):

- skill `verify` tích hợp sẵn được thêm vào trừ khi đã có một skill mang tên đó,
  không phân biệt hoa thường, và tập được sắp xếp theo tên viết thường
  (`skills.rs:151-158`);
- mọi skill, sau khi serialize, đi vào run contract dưới dạng tính năng `skills`;
- tool `Skill` được đăng ký suốt vòng đời của agent;
- danh sách đi vào system prompt dưới dạng một block cacheable sau
  prompt của spec, nên nó là một phần của prefix đã đóng băng của mỗi session mới.

Một agent thuần không có skill và không có tool `Skill`.

### Danh sách skill

`listing` (`crates/linch-core/src/skills/listing.rs:25`) ghi một dòng
`- name: description` cho mỗi skill mà model được phép gọi (không gồm những skill có
`disable_model_invocation`), sắp xếp theo tên viết thường. Một mô tả là `description`
của skill, rồi ` — when_to_use` và ` (args: argument_hint)`
khi được đặt, cắt ở 250 ký tự. Danh sách có thể dùng 1% cửa sổ ngữ cảnh của provider,
tính bốn ký tự một token (5120 ký tự cho 128000;
8000 khi không có cửa sổ), như ngân sách của Python. Vượt quá mức đó, mọi
mô tả bị cắt về một phần chia đều; dưới 20 ký tự cho mỗi phần, danh sách
chỉ giữ tên (`listing.rs:5-14`, `listing.rs:26-29`). Block đọc là
"The following skills are available for use with the Skill tool:"
(`crates/linch-core/src/skills/set.rs:93-100`). Khi tool search trì hoãn các
skill, block của một session mới chỉ nêu tên chúng (`set.rs:87-91`).

### Tool `Skill`

`SkillTool` (`crates/linch-core/src/skills/tool.rs:21`):

| Thuộc tính | Giá trị |
|---|---|
| Input | `skill` (bắt buộc; dấu `/` đứng đầu bị bỏ), `args` |
| Scope | `Read`: nó chạy không cần phê duyệt và chạy cạnh các lời gọi đọc khác; một rule có điều kiện `skill` vẫn có thể deny hoặc ask (`tool.rs:18-20`) |
| `always_load` | `true`: tool search không bao giờ trì hoãn nó (`tool.rs:78`) |
| Kết quả | Phần thân của skill với các placeholder đã được điền (`substitute`, `crates/linch-core/src/skills/substitute.rs:15`): `$ARGUMENTS`, `${SKILL_DIR}`, `${LINCH_SKILL_DIR}`, `${LINCH_SESSION_ID}` và từng đối số có tên, trong một lượt, không có shell quoting |
| Lỗi | `Unknown skill: <name>`; `Skill is not invokable by the model: <name>` (`tool.rs:94-105`) |
| Event | `skill_invoked` `{name, args}` ngay trước `tool_call_start` của lời gọi, `skill_completed` `{name, is_error}` ngay sau `tool_call_end` của nó, như trong Python. Tool sink của run stage chúng cùng phần bắt đầu và phần kết thúc, nên chúng đi cùng các lần ghi đó (`crates/linch-core/src/runner/sink.rs:102-131` và 173-180). Một lời gọi bị deny, bị chặn hoặc được phục vụ cũng nhận chúng; một tool không xác định hoặc input không hợp lệ thì không (`skill_name`, `sink.rs:104-108`) |

Tool đọc tập skill hiện tại của agent ở mỗi lời gọi, nên một
[reload](#catalog-và-reload) có hiệu lực ngay lập tức.

Một phần thân đã nạp vẫn nằm trong lịch sử dưới dạng kết quả tool. Khi một lần compaction
gộp kết quả đó đi, phần thân quay lại dưới dạng một `<system-reminder>` trong
message tóm tắt, vốn là một projection có ghi log: provider view có nó,
full history thì không (`skills.rs:11-14`, `skills.rs:165-193`).

### Overlay

Một skill mà frontmatter nêu `allowed_tools` hoặc `model` sẽ đặt chúng cho
lượt model ngay sau khi nó nạp, và chỉ lượt đó (`skills.rs:16-21`):

1. `overlays` (`crates/linch-core/src/skills/overlay.rs:27`) giữ, cho mỗi
   skill, một `allowed_tools` không rỗng và một `model` không rỗng.
2. Ở mỗi lượt, `Runner::narrow` (`crates/linch-core/src/runner/turn.rs:159-175`)
   nhìn vào provider view: skill cuối cùng có overlay trong số các lời gọi `Skill`
   thành công của message assistant ngay trước message user cuối cùng (`overlay.rs:45-69`).
3. `allowed_tools` của nó lọc snapshot tool của lượt: request chỉ đưa ra
   các tool đó, và một lời gọi tool khác bị từ chối với lý do chưa đăng ký. `model` của nó
   thay model của request, kèm hậu tố `[1m]` của model run khi model run có hậu tố đó
   (`model_override`, `overlay.rs:73`;
   `turn.rs:272-284`).

Không có gì được lưu cho overlay; một lượt được resume đọc overlay mà nó đã được dispatch
cùng từ chính view đó (`overlay.rs:1-5`). Overlay chỉ thu hẹp: các permission
rule vẫn quyết định mọi lời gọi. Event `context_build` của lượt nêu tên model mà nó
yêu cầu (`crates/linch-core/src/events/kinds/control.rs:44-47`). Các trường
`model_override` và `allowed_tools` của `skill_invoked` vẫn là `None`: tool chạy
trước lượt mà overlay áp dụng, và overlay không do tool tính.

### `skills_loaded`

Một run mới stage `skills_loaded` khi agent của nó có skill, run cung cấp
tool `Skill` (một run con mà định nghĩa bỏ nó đi thì không), và log của session
rỗng, giữa `init` và event `user` của prompt, để nó đi cùng
lần ghi create (`crates/linch-core/src/runner/start.rs:97-105`). Nó chứa một
descriptor cho mỗi skill, sắp xếp theo tên: `name`, `description`, và
`when_to_use` cùng `argument_hint` khi được đặt (`set.rs:55-78`). Quy tắc này
bền vững: các run sau của một session không bao giờ gửi nó, kể cả sau khi khởi động lại.

## Subagent

`AgentBuilder::subagents` (`builder.rs:164`) bật subagent bằng các
`SubagentDefinition` của host (`crates/linch-core/src/subagents.rs:146`);
facade nạp chúng từ `.linch/agents/*.md`. `Roster`
(`subagents.rs:236-278`) thêm agent `verification` tích hợp sẵn trừ khi đã có một agent
mang tên đó, và giữ một agent đa dụng, `_default`, cho một lời gọi
không có `subagent_type` (hoặc có giá trị trống, `general-purpose` hay `_default`).
Roster đi vào contract dưới dạng tính năng `subagents`, và cài đặt
`Workers`, khi được đưa ra, dưới dạng `workers`.

Lúc build đăng ký (`builder.rs:261-277`):

| Tool | Khi nào | Scope |
|---|---|---|
| `Subagent` | subagent bật | `Exec` |
| `SubagentContinue` | `workers.background` hoặc `workers.retain` | `Exec` |
| `TaskStop` | `workers.background` | `Exec` |

`workers` mà không có `subagents` làm build thất bại (`builder.rs:252-254`).

### Tool `Subagent`

`SubagentTool` (`crates/linch-core/src/subagents/tool.rs:32`) nhận
`description` (không trống), `prompt` (không rỗng), `subagent_type` và
`run_in_background`. Mô tả của nó liệt kê các loại trong roster.

Nó có scope exec, nên nó hỏi ở chế độ quyền `default`. Predicate `parallel` của nó
(`tool.rs:124-146`) chỉ cho một lời gọi chạy cạnh các lời gọi đọc khác
khi nó ở foreground và nêu tên một loại mà định nghĩa liệt kê các
tool của nó, mỗi tool là một tool scope read đã đăng ký; `*`, một loại hoặc
tool không xác định, hoặc không có danh sách đều fail closed.

Một lời gọi (`tool.rs:167-212`):

1. phân giải định nghĩa, hoặc trả lời bằng các loại mà nó biết;
2. từ chối `run_in_background` trừ khi `workers.background` bật;
3. đặt tên cho run con, và session của nó, theo idempotency key của lời gọi,
   `<parent run id>:<tool_use_id>` (`tool.rs:192`;
   `crates/linch-core/src/tool.rs:182-192`);
4. ghi danh worker vào worker registry của agent;
5. chạy run con ở background (xem [worker](#worker-chạy-nền))
   hoặc ở foreground. Một run con foreground mà không gì có thể địa chỉ hóa lại
   sẽ bị đóng session ngay; kết quả của một run con được giữ lại kết thúc bằng
   `[Worker ID: <id>]`.

### Agent con

`AgentInner::child` (`crates/linch-core/src/agent/child.rs:109-153`) dựng
agent mà một run con chạy với nó:

- spec, provider, permission engine, hook, store và
  namespace, skill và mailbox của agent cha;
- các tool của agent cha qua `ChildTools` (`child.rs:15-40`): không bao giờ có
  `Subagent`, `SubagentContinue`, `TaskStop` hay `tool_search`, và chỉ các tool
  có tên khi định nghĩa nêu tên (`*` nêu tất cả);
- các system block của agent cha (`system_blocks` của spec, rồi system
  prompt của nó), danh sách skill khi run con giữ tool `Skill`, rồi
  `User-provided instructions:` và phần thân của định nghĩa;
- không có subagent, nên một run con không thể sinh thêm; và `child: true`, nên một lời
  gọi sẽ phải hỏi thì bị deny (`crates/linch-core/src/runner/batch.rs:23-24`,
  `batch.rs:73-75`), trừ khi một rule hoặc một hook `permission_request` cho phép.

**Contract của run con đến từ bản build đã đóng băng của agent cha.** Nó được digest từ
các tool và chính sách hook mà contract của chính agent cha được dựng từ đó
(`built_tools`, `built_hooks`), đã lọc, cộng với định nghĩa dưới dạng
tính năng `subagent` (`child.rs:96-123`). Một tool được đăng ký trên agent cha
sau đó không làm nó đổi, nên một run con resume được qua các lần đăng ký; các run của nó
vẫn dùng các tool sống, như của agent cha. Một host có thể lấy cùng agent đó
bằng `Agent::subagent` và bắt đầu các run của nó dưới một run cha bằng
`Agent::run_child` (`child.rs:42-83`); khi đó host điều khiển và kết thúc chúng.

### Run con foreground

`subagents::run::run` (`crates/linch-core/src/subagents/run.rs:27-76`)
nạp run con trước:

| Tìm thấy | Rồi |
|---|---|
| Không có gì | Start nó với run của agent cha làm `parent_run_id`, principal và trace id của lời gọi, và một child token của cancel token của lời gọi |
| Một run terminal | Đọc lại phần kết thúc của nó từ các event của nó (`run.rs:136-145`) |
| Một run thuộc contract khác | Đánh dấu nó thất bại vĩnh viễn, để session của nó nhận run trở lại, và đưa `ContractMismatch` cho agent cha (`run.rs:113-133`) |
| Một run đang sống | Resume nó |

Khi run con đã tồn tại, `first_use` (`run.rs:78-111`) trước hết từ chối một lời gọi
mà id đã được một response trước đó của run dùng: log của agent cha
chứa event của run con đó trước response cuối cùng của nó, nên provider
đã lặp lại một tool-use id.

Family của agent cha nhận run con, và lời gọi chờ nó. Mỗi
envelope trong stream của run con được bọc trong một event `subagent` và giao
cho agent cha (`run.rs:147-164`); xem [bọc subagent](./events.md#bọc-subagent).
Các event chỉ có trong stream đến stream của agent cha ngay và các event bền vững đến cùng
phần kết thúc của lời gọi, nên trên stream của agent cha, `partial_assistant` của một run con
có thể đến trước `init` của nó. Phần kết thúc của run con trở thành kết quả
(`run.rs:195-224`): văn bản cuối cùng của nó, hoặc một lỗi giữ lại đầu ra
một phần (`Subagent aborted.`, `Subagent encountered an error: ...`).

Một lời gọi bị timeout hoặc lệnh hủy của run cắt ngắn sẽ hủy token của run con
và để run con ghi abort của nó; agent cha chờ mọi run con trước lần ghi terminal
của nó (`subagents.rs:30-33`).

`total_usage` của agent cha chỉ đếm các lời gọi model của chính nó; usage của run con đến
agent cha ở dạng được bọc, như trong Python
(`crates/linch-core/tests/e2e_subagents.rs:70-73`).

## Worker chạy nền

`Workers` (`subagents.rs:102-133`) có ba cài đặt, tất cả nằm trong contract:

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `background` | `false` | Một lời gọi có thể đặt `run_in_background` |
| `retain` | `false` | Một worker foreground vẫn địa chỉ hóa được sau khi nó trả lời |
| `max_background` | 4 | Số run con background mà một run có thể có đang chạy; một lời gọi vượt quá nhận một lỗi tool. Python không có giới hạn này |

Một lời gọi background (`crates/linch-core/src/subagents/background.rs:96-115`)
không bao giờ chạy song song. Nó giao run con cho family và trả lời ngay
với `Worker '<id>' started in background...`, sau một event `child_run_status`
(`running`) đi cùng phần kết thúc của lời gọi. Giới hạn chỉ được kiểm tra khi
một lời gọi bắt đầu một run con mới (`background.rs:148-159`); các run con của một run
được resume đã được nhận khi chúng bắt đầu.

Run con tiếp tục chạy cạnh agent cha, được điều khiển bởi stream của agent cha:

- các envelope bền vững của nó đến log của agent cha ở dạng được bọc, ở lượt kế tiếp của
  agent cha (`background.rs:279-292`);
- phần kết thúc của nó trở thành một `Completion` trong inbox của family. Ở cuối
  batch tool kế tiếp của agent cha, `Runner::notes` (`crates/linch-core/src/runner/inbox.rs:50-70`)
  stage một event `child_run_status` của phần kết thúc và thêm một
  `<task-notification>` (văn bản của Python, được escape như `html.escape` của Python,
  `background.rs:55-91`) vào các kết quả tool được ghi log;
- khi agent cha trả lời bằng văn bản trong lúc một run con background đang chạy,
  `Runner::hear_back` (`inbox.rs:72-109`) không kết thúc run: nó chờ
  phần kết thúc kế tiếp và ghi log thông báo dưới dạng một message `user` có subtype
  `notification`, để model thực hiện thêm một lượt;
- một agent cha vẫn kết thúc (bị hủy, thất bại, hết lượt) sẽ hủy các run con của nó,
  chờ các abort của chúng, và ghi lại mỗi phần kết thúc mà nó chưa báo
  trong lần ghi terminal của nó (`crates/linch-core/src/runner/end.rs:19-42`).

`SubagentContinue` (`crates/linch-core/src/subagents/control.rs:74-166`)
phân giải một worker của session đang gọi theo id hoặc tên hiển thị duy nhất
(`crates/linch-core/src/subagents/workers.rs:74-105`), từ chối worker còn
đang chạy, và chạy lại nó trên session riêng của nó, lịch sử còn nguyên, dưới dạng một
run con foreground mới của run đang gọi, được đặt tên theo lời gọi continue.
`TaskStop` (`control.rs:168-252`) hủy một worker background đang chạy và
đánh dấu nó đã dừng, nên phần kết thúc của nó được ghi lại mà không có thông báo; worker
vẫn địa chỉ hóa được.

Worker registry (`workers.rs:26-130`) giữ, theo từng session cha và trong
bộ nhớ process, tên hiển thị của mỗi worker (tên định nghĩa của nó, hoặc
`Agent #N` cho worker đa dụng thứ N) và việc nó có địa chỉ hóa được hay không.
`Agent::close_session` (`crates/linch-core/src/agent.rs:329-345`)
đóng các family đã suspend của session, các session của worker của nó, rồi
session.

## Họ run

Các run con của một run là `Family` của nó (`crates/linch-core/src/runner/family.rs:44`).
Family nằm trên agent, được liệt kê theo run id (`family.rs:370-401`), từ lúc
create hoặc claim của run cho đến khi stream của nó buông ra
(`crates/linch-core/src/runner/start.rs:333-345`). Đó là điều giúp nó
sống lâu hơn một stream kết thúc khi run đang suspend.

- **Khi một stream đang điều khiển run, lõi không spawn task nào.**
  `RunStream::poll_next` poll các run con của family cạnh future của run
  (`crates/linch-core/src/runner.rs:128-156`), nên các run con chỉ tiến lên khi
  bên gọi poll.
- **Suspend:** trước lần ghi `AwaitingApproval`, runner park family;
  một lần ghi thất bại sẽ unpark nó (`crates/linch-core/src/runner/batch.rs:141-180`).
- **Keeper:** khi stream của một run đã park dừng, các run con của nó chuyển cho
  một task, được spawn trên Tokio runtime của bên gọi, task này điều khiển chúng cho đến khi
  một stream của run gắn vào lại hoặc không còn gì (`family.rs:230-268`).
  Không có runtime thì chúng chờ, không được poll, cho stream đó. Đây là loại
  task duy nhất mà lõi spawn.
- **Tiếp quản:** một approve hoặc resume trong process này gắn một stream mới vào cùng
  family; keeper thấy driver thay đổi và thoát
  (`family.rs:180-211`). Các run con chưa bao giờ trải qua một sự cố.
- **Đóng:** đóng session của run hoặc drop agent sẽ hủy các run con của
  một family đã park; keeper điều khiển chúng cho đến khi các abort của chúng
  bền vững (`family.rs:270-290`; `agent.rs:202-205`).
- **Mọi cách dừng khác** (một stream bị drop, một run thất bại) drop các run con
  cùng family, giống như một sự cố.

`Agent::parked_children` (`agent.rs:348-368`) trả về một future hoàn tất
khi các run con của một run đã suspend xong. Server RPC dùng nó để giữ chỗ của một
run đã suspend dưới `max_runs` trong khi các worker của nó chạy
([RPC](./rpc.md#các-giới-hạn)).

## Resume và reattach sau sự cố

Một run con là một run bền vững bình thường, nên một sự cố để nó ở trạng thái bền vững
cuối cùng giống như run cha. Resume run cha sẽ nhặt các run con lên:

- **Foreground:** một lời gọi `Subagent` hoặc `SubagentContinue` có phần bắt đầu bền vững
  và không có phần kết thúc bền vững được plan lại thay vì bị báo là bị gián đoạn
  (`rerun_subagents`, `batch.rs:118-139`). Lời gọi chạy lại và tìm thấy run con của nó:
  nó resume run con đó (journal của run con không bao giờ trả phí một lời gọi model hai lần),
  đọc lại một run đã xong, hoặc làm thất bại một run mà contract đã đổi.
- **Background:** sau claim, `reattach`
  (`crates/linch-core/src/subagents/background.rs:184-252`, được gọi ở
  `start.rs:313-315`) ghi danh lại các worker của run từ các event
  `tool_call_start` của nó, theo thứ tự lời gọi, rồi nhận từng run con mà một
  `child_run_status` đánh dấu `running` không có phần kết thúc nào sau đó, và family
  chưa giữ: nó resume run con, start nó (create của nó chưa bao giờ được ghi),
  hoặc đọc lại phần kết thúc của nó. Một run con không thể nhặt lên sẽ báo một phần
  kết thúc thất bại.
- **Suspend trong process này:** family vẫn còn được liệt kê, nên resume
  hoặc approve tiếp quản nó nguyên trạng (`start.rs:206-208`).

Contract của các run con đến từ bản build của agent cha, nên một lần resume sau một đăng ký
trên agent cha vẫn khớp; một định nghĩa bị đổi bởi một lần reload thì không, và run con đó
kết thúc với trạng thái `failed`. Với store trong bộ nhớ, mọi thứ mất cùng process.

## Catalog và reload

Cả hai reload là các đăng ký có thể hoàn tác được xây trên `Versions`
(`crates/linch-core/src/agent/versions.rs:8-13`): một ngăn xếp các giá trị mà giá trị
cuối là giá trị hiện tại. Một lần push (`versions.rs:47-75`) trả về một `Registration`; dispose nó gỡ
đúng giá trị đó, nên một lần push sau vẫn là giá trị hiện tại, và dispose hai lần
không làm gì. Run contract vẫn là contract mà agent được build với nó, nên các run
resume được qua các lần reload (`crates/linch-core/src/agent/reload.rs:1-9`).

| | `Agent::reload_skills` (`reload.rs:20-44`) | `Agent::reload_subagents` (`reload.rs:46-87`) |
|---|---|---|
| Nhận | Các skill mới; `verify` được thêm vào như lúc build | Các định nghĩa mới |
| Thay thế | `SkillSet` hiện tại; tool `Skill` đọc nó ở mỗi lời gọi | Roster, và tool `Subagent`, qua `ToolRegistry::replace`, vì mô tả của nó liệt kê roster |
| Dispose | Khôi phục tập trước đó | Khôi phục tool và roster trước đó. Agent giữ đăng ký và dispose nó khi bị drop, để tool bị thay thế không bao giờ sống lâu hơn agent trong registry dùng chung (`agent.rs:206-213`) |
| Bị từ chối | `BuildError::Invalid` khi build không có skill | `BuildError::Invalid` khi build không có subagent |

Một session đã có prefix đóng băng giữ nó từng byte và được báo về thay đổi trong một
thông báo catalog ở đuôi message user được ghi log kế tiếp của nó; chỉ một session mới
mới đưa catalog sống vào prefix của nó
(`crates/linch-core/src/catalog.rs:1-22`). Với subagent, schema của tool
không đổi, nên thông báo nói rằng tool đã được cập nhật.

`OpenedAgent::reload_skills` và `reload_subagents` của facade
(`crates/linch/src/open/reload.rs:10-59`) quét lại thư mục cấu hình,
ngoài vòng lặp async, chỉ cho loại mà cấu hình đã bật, thêm lại roster của deep
agent, và trả về các mục mà lần quét bỏ ra. Không reload nào là method RPC hay lời gọi
client Python ở revision này.

### Danh sách deep agent

`linch-ext` chứa preset deep agent. `deep_agent_subagents`
(`crates/linch-ext/src/deep_agent/subagents.rs:7-12`) là roster của Python:
`researcher`, `planner`, `implementer` và một `verification` chỉ đọc, thứ
thay chỗ cái tích hợp sẵn của lõi (cái đó có `Bash`).
`DeepAgent::subagents` (`crates/linch-ext/src/deep_agent.rs:122-135`) đặt các định nghĩa
của host lên đầu và thêm mỗi mục roster mà không định nghĩa nào có tên đó,
không phân biệt hoa thường. `workers` của profile
(`crates/linch-ext/src/deep_agent/profile.rs:79-86`) đặt `background` và
`retain` từ các cờ của profile, với `max_background` mặc định.

Facade bật preset từ `AgentConfig.deep_agent`: `deep::expand`
(`crates/linch/src/deep.rs:10-33`) đặt task, hệ thống tệp ảo, các
worker (trừ khi cấu hình đặt chúng) và spec; `deep::add_roster`
(`deep.rs:35-45`) bật skill, nên skill `verify` tích hợp sẵn được thêm vào, và
thêm roster sau các subagent của thư mục cấu hình, lúc open
(`crates/linch/src/open.rs:156`) và ở mỗi lần reload. Hai phần của preset Python chưa có hỗ
trợ ở kernel (`deep_agent.rs:33-39`): một coordinator giữ các tool workspace
(`COORDINATOR_EXCLUDED_TOOLS` chỉ được nêu tên), và `max_tokens` của profile
không được thực thi.

## Ví dụ

Đoạn này biên dịch và chạy được với `linch-core` ở revision này. Một skill
thu hẹp lượt kế tiếp về `Subagent` trên một model nhỏ, subagent chạy dưới dạng
một run con, và một lần reload skill được hoàn tác:

```rust
use std::sync::Arc;

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, Skill,
    SubagentDefinition,
};
use serde_json::json;

async fn drain(agent: &Agent, session: &str, prompt: &str) -> Vec<Envelope> {
    let stream = agent.run(prompt, RunOptions::new(session));
    stream.map(|item| item.expect("the run continues")).collect().await
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // `scan` narrows the turn after it loads to `Subagent` on a small model.
    let mut scan = Skill::new("scan", "/skills/scan", "Survey the repo.", "Delegate a survey.");
    scan.allowed_tools = Some(vec!["Subagent".into()]);
    scan.model = Some("small-model".into());
    // `scout` names its tools: none, so the child gets none.
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Report what you find.");
    scout.tools = Some(vec![]);

    // Parent and child share the provider, so turns go in call order.
    let spawn = json!({"description": "Scout", "prompt": "Look around.", "subagent_type": "scout"});
    let provider = Arc::new(ScriptedProvider::new([
        TurnBuilder::new("m").tool_call("c1", "Skill", &json!({"skill": "scan"})).build(),
        TurnBuilder::new("m").tool_call("c2", "Subagent", &spawn).build(),
        TurnBuilder::new("m").text("found 3 files").build(), // the child
        TurnBuilder::new("m").text("done").build(),
        TurnBuilder::new("m").text("again").build(),
        TurnBuilder::new("m").text("back").build(),
    ]));
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::SkipDangerous, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(provider.clone())
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .skills(vec![scan])
        .subagents(vec![scout])
        .build()
        .unwrap();

    let events = drain(&agent, "s1", "survey").await;
    let run_id = events[0].run_id.clone();
    let requests = provider.requests();
    let tools = |at: usize| -> Vec<String> {
        requests[at].tools.iter().map(|tool| tool.name.clone()).collect()
    };
    assert_eq!(tools(0), ["Skill", "Subagent"]);
    assert_eq!((tools(1), requests[1].model.as_str()), (vec!["Subagent".into()], "small-model"));
    assert!(tools(2).is_empty(), "the child's request");
    assert!(requests[2].system.last().unwrap().text.starts_with("User-provided instructions:"));

    // The child's events are wrapped; its run id is the call's key.
    let child = format!("{run_id}:c2");
    let init = events.iter().find_map(|envelope| match &envelope.event {
        Event::Subagent(sub) if sub.child_run_id.as_str() == child => match &*sub.event {
            Event::Init(init) => Some(init),
            _ => None,
        },
        _ => None,
    });
    assert_eq!(init.unwrap().parent_run_id.as_ref(), Some(&run_id));
    let answer = events.iter().find_map(|envelope| match &envelope.event {
        Event::ToolCallEnd(end) if end.tool_use_id() == "c2" => Some(json!(end.result)),
        _ => None,
    });
    assert_eq!(answer.unwrap()["content"][0]["text"], "found 3 files");
    let Event::Result(result) = &events.last().unwrap().event else {
        panic!("the run ends with its result");
    };
    assert_eq!(result.final_text.as_deref(), Some("done"));

    // A reload is a registration: new sessions list `lint` until it is
    // disposed, then `scan` again.
    let listing = |at: usize| provider.requests()[at].system.last().unwrap().text.clone();
    let lint = Skill::new("lint", "/skills/lint", "Lint the code.", "Run the linter.");
    let reload = agent.reload_skills(vec![lint]).unwrap();
    drain(&agent, "s2", "hi").await;
    assert!(listing(4).contains("- lint: Lint the code."));
    reload.dispose();
    reload.dispose(); // idempotent
    drain(&agent, "s3", "hi").await;
    assert!(listing(5).contains("- scan: Survey the repo."));
}
```

## Chỗ Rust khác Python

| Linch bản Python | linch-rust |
|---|---|
| Phần thân của một skill đã gọi được đặt lại dưới dạng `<system-reminder>` ở mỗi lượt | Phần thân là kết quả của tool `Skill`; nó chỉ quay lại dưới dạng reminder sau khi một lần compaction gộp kết quả đó đi |
| Tool `Skill` đặt một overlay đang chờ (`allowed_tools`, `model`) trên session, được giữ trong checkpoint | Overlay được đọc từ provider view ở mỗi lượt; không có gì được lưu |
| `skill_invoked` trước `tool_call_start` của lời gọi, `skill_completed` sau `tool_call_end` của nó | Giống nhau |
| `skills_loaded` một lần cho mỗi đối tượng session (`skills_loaded_emitted`), nên phát lại sau khi khởi động lại, mỗi khi agent có skill | Một lần cho mỗi session, ở run đầu tiên của nó (log của nó rỗng), và chỉ khi run cung cấp tool `Skill` |
| `reload_skills()` và `reload_subagents()` quét lại dự án | Lõi nhận các danh sách mới; facade quét lại; cả hai chưa có trên wire |
| System block của một run con mới được dựng từ tên tool của nó; một fork tái sử dụng prefix của agent cha | Một run con nhận system block và prompt của agent cha, danh sách skill nếu nó có `Skill`, rồi phần thân của nó. Fork là `Agent::fork_session`, không phải một chế độ của subagent |
| Một worker background là một task `asyncio`: nó có thể sống lâu hơn run, và một lần khởi động lại không thể resume nó | Một run con là một run bền vững do stream của agent cha điều khiển; agent cha chờ các worker của nó trước khi kết thúc, và resume chúng cùng nó trên Postgres |
| Không giới hạn số worker background | `max_background`, mặc định 4 |
| Worker id dạng `agent-a1b2` | `<parent run id>:<tool_use_id>` |
| Thông báo được xả ở đầu lượt kế tiếp, tùy chọn từ một inbox bền vững | Các phần kết thúc được ghi lại dưới dạng event `child_run_status` và thông báo được nối vào message user được ghi log kế tiếp của agent cha |
| Một đối tượng `RunBudget` trên cấu hình agent (hoặc các tùy chọn run), dùng chung bởi mọi run dùng nó và được cây subagent kế thừa | Một `budget` của spec cho mỗi run gốc một bộ đo riêng (`crates/linch-core/src/budget.rs`), mà family của run giao cho mọi run con; một run gốc được resume cộng các receipt bền vững của chính nó và của các run con. Tổng của agent cha vẫn chỉ đếm các lời gọi của chính nó, như `ResultEvent` của Python |
| Một coordinator mất các tool nặng của nó | Không áp dụng: kernel đưa cho agent cha mọi tool mà các subagent của nó có thể dùng |

## Test

| Test | Cố định điều gì |
|---|---|
| `crates/linch-core/tests/e2e_skills.rs` | Danh sách trong prefix được cache, phần thân của tool, contract, reminder sau compaction |
| `crates/linch-core/tests/e2e_skill_overlay.rs` | Event của skill, tool và model của overlay, qua một sự cố |
| `crates/linch-core/tests/e2e_subagents.rs` | Run con foreground: id của cha, session mới, tool đã lọc, việc bọc, các loại chỉ đọc chạy song song, tổng usage |
| `crates/linch-core/tests/e2e_background.rs`, `e2e_workers.rs` | Run con background, thông báo, `hear_back`, continue, stop, đóng session |
| `crates/linch-core/tests/e2e_background_suspend.rs` | Các run con của một agent cha đã suspend tiếp tục chạy và được tiếp quản |
| `crates/linch-core/tests/e2e_subagent_crash.rs`, `e2e_background_crash.rs` | Một sự cố ở mỗi lần ghi, rồi một lần resume nhặt run con lên |
| `crates/linch-core/tests/e2e_child_contract.rs`, `e2e_child.rs` | Contract của run con từ bản build của agent cha; run con do host điều khiển |
| `crates/linch-core/tests/e2e_reload.rs` | Reload như các đăng ký, prefix được giữ, thông báo catalog |
| `crates/linch-core/tests/e2e_subagent_cleanup.rs` | Một lời gọi bị cắt ngắn để run con của nó bị abort trước khi agent cha kết thúc |
| `crates/linch-core/tests/e2e_budget.rs` | Chi tiêu của một run con được tính vào budget của agent cha, một cây đã cạn budget từ chối lời gọi đầu tiên của run con, một cây được resume giữ chi tiêu của các run con của nó |

---

Quay lại [mục lục kiến trúc](./README.md).
