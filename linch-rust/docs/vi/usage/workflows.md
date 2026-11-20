# Directed workflow

[Bản gốc tiếng Anh](../../usage/workflows.md)

Một directed workflow là một hàm Python chạy kế hoạch cố định của bạn: fan-out
tới các agent, kiểm tra những gì trả về, chuyển kết quả có kiểu của một step
sang step kế tiếp. Server ghi journal mọi lời gọi đã xong vào Postgres, nên khi
client hoặc server bị crash, cùng `run_id` đó được resume và không lời gọi nào đã
xong phải trả tiền hai lần.

[<- Hướng dẫn sử dụng](./README.md)

## Khi nào dùng

Hãy dùng workflow khi bạn biết **hình dạng** của công việc và muốn một chương
trình làm chủ nó: một pipeline ký hoặc phê duyệt, một fan-out review cố định,
recon, rồi một lần kiểm tra cho mỗi service, rồi một báo cáo. Luồng điều khiển là
code của bạn; model làm việc bên trong mỗi lời gọi.

Hãy dùng một agent mở khi model nên tự quyết định hình dạng. Những agent đó đã
được phục vụ bởi những gì có sẵn, nên workflow không lặp lại chúng:
[steering](./long-running.md) một run đang chạy, [task ledger](./tasks.md),
[compaction](./memory.md) cho context vượt quá cửa sổ, và
[lịch](./scheduler.md) cho heartbeat và run nền.

| Công việc | Hãy dùng |
|---|---|
| Các step đã biết, một số không đảo ngược được, một lần crash không được lặp lại công việc đã trả tiền | một workflow |
| Một agent làm việc hàng giờ cho một mục tiêu nó tự lập kế hoạch | một agent chạy dài ([Agent chạy dài](./long-running.md)) |
| Một run phải được bắt đầu đúng một lần dưới một id đã chọn, từ một job queue hoặc một orchestrator | [`run_or_resume`](#run_or_resume) |
| Một prompt theo bộ hẹn giờ | một [lịch](./scheduler.md) |

## Một ví dụ

```python
from linch_rust import Server, run_workflow, WorkflowContext, Budget

REPORT = {"name": "report", "schema": {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"]}}

async def pentest(wf: WorkflowContext) -> dict:
    await wf.phase("recon")
    services = await wf.step("recon", lambda: scan_network())        # JSON, journaled
    await wf.phase("checks")
    notes = await wf.parallel([
        (lambda s=s: wf.agent(f"check {s['name']} on port {s['port']}", label=s["name"]))
        for s in services])
    report = await wf.agent_result(
        "write the report from these notes:\n" + "\n".join(notes), output_schema=REPORT)
    return report.structured

async with Server() as server:
    agent = await server.open_agent(config)           # stores: Postgres
    result = await run_workflow(agent, pentest, run_id="pentest-2026-11",
                                budget=Budget(max_tokens=500_000), max_concurrency=4)
```

Chạy lại với cùng `run_id` sau một lần crash: step recon và mọi lần kiểm tra đã
xong được replay từ journal, và chỉ những lời gọi chưa xong mới chạy.

## Một hàm workflow có thể gọi gì

`run_workflow(agent, fn, run_id=...)` chạy `fn(wf)` trong process của bạn và trả
về những gì nó trả về. `wf` là một `WorkflowContext`:

| Lời gọi | Nó làm gì |
|---|---|
| `await wf.agent(prompt, ...)` | Một lời gọi agent có journal; trả về text cuối (JSON gọn khi có `output_schema`). `agent_result` trả về một `AgentResult` có `text`, `structured`, `session_id`, `tokens`, `cost_usd`, `replayed` |
| `await wf.step(name, fn, key=...)` | Chạy `fn` (đồng bộ hoặc bất đồng bộ, trong process của bạn) và ghi journal giá trị JSON của nó |
| `await wf.interrupt(key, payload)` | Park workflow cho đến khi có người trả lời; raise `WorkflowSuspended` |
| `await wf.parallel([thunks])` | Các nhánh đồng thời, tối đa `max_concurrency`; theo thứ tự đầu vào; lỗi đầu tiên hủy các nhánh còn lại |
| `await wf.settled([thunks])` | Như `parallel`, nhưng mọi nhánh đều chạy xong và mỗi kết quả là một `StepOutcome` |
| `await wf.pipeline(items, *stages)` | Mỗi item chạy qua mọi stage độc lập, không có barrier giữa các stage |
| `await wf.phase(title)` | Một event tiến độ cho `on_event`; không ghi journal gì |
| `wf.budget`, `wf.spent_tokens`, `wf.spent_usd` | Mức trần và những gì lần drive đã tiêu (tính cả replay) |

`wf.agent` nhận `name`, `subagent`, `label`, `key`, `output_schema`,
`fork`, `timeout_ms` và `retry`. `run_workflow` nhận `budget`,
`max_concurrency` (4 khi không đặt; một fan-out lồng nhau có giới hạn riêng),
`step_timeout_ms`, `deadline_ms`, `resume` (các câu trả lời cho interrupt),
`principal` (bên dưới) và `on_event`, nơi nhận các `WorkflowEvent`: `phase`, `agent_start`,
`agent_end`, `agent_replayed`, `step_start`, `step_end`, `step_replayed`,
`interrupt_requested`, `interrupt_resolved`, `interrupt_replayed` và
`interrupt_stale`.

Một `budget` có `max_cost_usd` cần có giá cho mọi model mà agent có thể dùng
(model của nó và từng model trong `fallback_models`, từ bảng tích hợp sẵn hoặc
`pricing` của agent), vì một lời gọi không có giá sẽ được tính là miễn phí và
giới hạn không bao giờ kích hoạt. `workflow.start` từ chối nó (`-32011`, nêu tên
model), và một lời gọi mà run của nó có usage nhưng không có chi phí (provider
phục vụ một model mà `pricing` không có) sẽ kết thúc lời gọi agent kế tiếp của
workflow với `-32046`, `data.kind: unpriced` (`WorkflowBudgetExceeded`,
`WorkflowError::BudgetUnpriced`); lời gọi đó vẫn nằm trong journal, và resume sẽ
từ chối lần nữa. Hãy đặt `pricing` hoặc giới hạn `max_tokens`. Mức trần
`max_cost_usd` của tenant (bên dưới) cũng là một `max_cost_usd`.

`parallel`, `settled` và `pipeline` chạy trong client. Mỗi nhánh đếm các lời gọi
của riêng nó, dưới đường dẫn nhánh của nó, nên các nhánh đến lời gọi của mình
theo thứ tự khác khi resume vẫn tìm thấy record của chính chúng. `name`, `key`
của một lời gọi (`key` của step được băm nên không bị chặn) và `branch` dài tối
đa 256 byte, như id của workflow (`-32602` nếu vượt).

Một step mà function của nó raise sẽ trả ticket lại: `wf.step` gửi
`workflow.step.abandon` trước khi raise lại, nên một workflow bắt các step thất bại
(để fallback, hoặc trong một fan-out `settled`) không bao giờ hết chỗ. Một client
không bao giờ trả ticket (client có từ trước lời gọi này) bị giới hạn 1024 step
đang chờ cho mỗi workflow (`-32005`, `kind: pending_steps`), không phải bởi
`max_records`; server bỏ mọi ticket khi kết nối kết thúc.

## Replay hoạt động thế nào

Một lời gọi được định danh bằng một **content key** cộng với **occurrence** của
nó: có bao nhiêu lời gọi cùng key đứng trước nó, đếm riêng bên trong từng nhánh
fan-out. Một lần resume replay mọi lời gọi mà journal có key và occurrence. Key là
một digest của:

| Lời gọi | Key |
|---|---|
| `wf.agent` | `name` (hoặc tên của subagent), prompt, `subagent`, `output_schema`, session `fork`; với `key=` thì chỉ tên và key |
| `wf.step` | tên step và `key` của nó |
| `wf.interrupt` | chỉ key của interrupt: một câu trả lời đã ghi journal được replay bất kể payload, nhưng một câu trả lời chưa ghi journal còn cần payload khớp ([bên dưới](#một-câu-trả-lời-dành-cho-câu-hỏi-đã-được-hỏi)) |

Sửa một prompt chỉ làm lời gọi đó trượt record của nó; các lời gọi trước và sau
nó, có key vẫn khớp, được replay. Đổi tên một step cũng chỉ ảnh hưởng step đó.

**Giới hạn, nói thẳng.** Hàm workflow chạy lại từ đầu ở mỗi lần resume; chỉ các
lời gọi có journal mới được replay. Vì vậy code nằm ngoài `wf.agent`, `wf.step`
và `wf.interrupt` phải mang tính xác định hoặc idempotent. Rẽ nhánh theo một giá
trị đã ghi journal thì được (giá trị đó được replay); rẽ nhánh theo đồng hồ, số
ngẫu nhiên, môi trường hoặc một câu trả lời HTTP lấy bằng code trần thì không:
hãy đặt những thứ đó bên trong một `wf.step`. Giá trị của một step là JSON; một
giá trị không encode được sẽ thất bại trước khi được ghi journal. Một lời gọi bị
hủy hoặc thất bại không ghi journal gì và chạy lại ở lần drive kế tiếp. Kết quả
agent trong journal là các object (`text`, `structured`, `session_id`, `tokens`,
`cost_usd`): journal do một bản build trước đó của engine ghi sẽ không được đọc;
hãy bắt đầu một `run_id` mới.

## Chuyển kết quả của một step sang step kế tiếp

Đầu vào của step kế tiếp do code của bạn dựng từ kết quả trước, nên không có gì
ẩn đi qua giữa các lời gọi. Có bốn cách để mang nó đi.

**Handoff có kiểu.** Cho lời gọi một `output_schema`. Câu trả lời được kiểm tra
theo nó, ghi journal, và trả về dưới dạng JSON gọn (`text`) và dưới dạng object
(`structured`). Dựng prompt kế tiếp từ nó:

```python
recon = await wf.agent_result("list the exposed services", output_schema=SERVICES)
for service in recon.structured["services"]:
    ...
```

**Artifact lớn theo đường dẫn.** Đừng đặt một bản scan 2 MB vào prompt hay vào
journal (record journal của một tenant bị giới hạn; xem [Admission](#admission)).
Hãy ghi nó vào workspace hoặc [filesystem backend](./tools.md) và truyền đường
dẫn cùng một hash:

```python
path, digest = await wf.step("save-scan", lambda: save_scan(raw))   # (path, sha256)
verdict = await wf.agent(f"read {path} (sha256 {digest}) and list the open ports")
```

Hash cho phép một step sau nhận ra tệp đã đổi bên dưới nó.

**`fork`.** Truyền `AgentResult` (hoặc `session_id`) của một lời gọi trước làm
`fork=`: run mới bắt đầu trên một bản sao của log bền vững của session đó, nên nó
thấy cùng cuộc hội thoại (`Agent::fork_session`). Hãy dùng nó để hỏi nhiều câu
tiếp theo về một context dài mà không phải gửi lại nó. Fork là một phần của key
của lời gọi. Chi phí: phần prefix được sao chép bị tính lại như input, và chỉ rẻ
ở nơi provider cache prefix; bản sao được lấy khi lời gọi bắt đầu, nên hãy fork
một session đã xong, không phải một session mà một run đang chạy vẫn còn ghi vào.

**Tệp dùng chung.** Các agent trong cùng một workspace đọc tệp của nhau. Cách này
mang được mọi thứ, nhưng nó là handoff ít thấy nhất và ít replay được nhất: một
lời gọi được replay không ghi lại tệp của nó. Hãy ưu tiên một step trả về đường
dẫn và hash.

| Handoff | Token | Dùng lại cache | Tính xác định |
|---|---|---|---|
| Kết quả `output_schema` trong prompt kế tiếp | chỉ kết quả | không | giá trị đã ghi journal được replay chính xác |
| path + hash | vài token | không | artifact nằm ngoài journal; hash phát hiện thay đổi |
| `fork` | cả prefix một lần nữa, như input | có, ở nơi provider cache | session nguồn không được đổi |
| tệp dùng chung | bất cứ gì agent đọc | không | một lần replay không tạo lại chúng |

### Một step không đảo ngược được

Ký, gửi và thanh toán đi qua `wf.step`, **sau** một phê duyệt bền vững, và mang
một idempotency key:

```python
async def sign_flow(wf: WorkflowContext) -> str:
    draft = await wf.agent("draft the contract", output_schema=CONTRACT)
    if not await wf.interrupt("approve", {"draft": draft.structured}):
        return "withdrawn"
    key = f"contract-{draft.structured['id']}"
    return await wf.step("sign", lambda: signer.sign(draft.structured, idempotency_key=key),
                         key=key)
```

`wf.interrupt` lưu workflow ở trạng thái `awaiting_input` và raise
`WorkflowSuspended`; không có gì chạy trong lúc nó chờ. Hãy trả lời bằng cách bắt
đầu cùng id với `resume={"approve": True}` (từ bất kỳ process nào). Câu trả lời
được ghi journal, nên nó không bao giờ bị hỏi lại.

### Một câu trả lời dành cho câu hỏi đã được hỏi

Một câu trả lời chỉ áp dụng cho câu hỏi mà workflow đang bị park ở đó: cùng key và payload
bằng nhau (so sánh bằng digest chuẩn tắc). Nếu bản nháp bị sửa giữa lúc hỏi và lúc resume,
lời gọi hỏi lại với payload hiện có: câu trả lời lỗi thời bị bỏ, một event `interrupt_stale` báo việc
đó, không có gì được ghi journal, và workflow được lưu là `awaiting_input` với câu hỏi đó.
Hãy trả lời câu hỏi đó. Vì vậy hãy trả lời interrupt mà `workflow_status` nêu tên, với payload
mà hàm sẽ dựng lại; một lần resume mang câu trả lời cho một workflow chưa từng bị park, hoặc
cho một interrupt khác với cái đã lưu (interrupt thứ hai trong cùng lần resume), cũng sẽ hỏi
lại. Nếu server crash sau khi nhận workflow và trước khi câu trả lời được ghi journal, câu
hỏi đã lưu không còn (trạng thái là `running`): hãy hỏi và trả lời lại.

Một lần crash sau khi signer đã chạy nhưng trước khi record của step được ghi
khiến lần resume chạy lại hàm. Journal không phân biệt được "chưa từng chạy" với
"đã chạy rồi mới crash", nên chốt chặn là key: hàm được gọi lại với **cùng**
`key`, và dịch vụ ký phải coi một key lặp lại là cùng một request và trả về kết
quả đầu tiên. Hãy truyền key cho dịch vụ; đừng tạo một key mới bên trong hàm.

<a id="admission"></a>
## Admission

Một tenant không thể chạy workflow trừ khi operator cho phép: `allow.workflow` mặc
định không có và `workflow.start` bị từ chối (`-32004`, `kind:
workflow`). [Phần `workflow` của cấu hình server](./server.md#cấu-hình) bật chúng
lên và đặt các mức trần:

| Mức trần | Mặc định | Nó giới hạn gì |
|---|---|---|
| `max_workflows` | 4 | Số workflow tenant drive cùng lúc, qua mọi kết nối của nó (`-32005`, `kind: workflows`) |
| `max_records` | 10000 | Số record journal của một workflow, tính cả replay (`-32005`, `kind: journal_records`); được kiểm tra trước khi một lời gọi tiêu tiền |
| `max_fan_out` | 8 | Số lời gọi agent của một workflow đang chạy cùng lúc (`-32005`, `kind: agent_calls_in_flight`); client hạ `max_concurrency` của nó xuống mức này |
| `max_record_bytes` | 1 MiB | Giá trị JSON của một record |
| `max_tokens`, `max_cost_usd` | không có | Một mức trần cho `budget` của workflow; workflow không có budget sẽ nhận nó, và workflow xin nhiều hơn bị từ chối (`-32004`, `kind: workflow_budget`) |

Dù `max_records` là bao nhiêu, server giữ bộ đếm cho tối đa 10.000 call key khác
nhau trong mỗi lần drive một workflow (nhiều hơn nếu `max_records` lớn hơn): một
lời gọi thất bại, hay một interrupt đã suspend, không ghi journal gì nhưng vẫn giữ
key của nó. Vượt mức đó thì lời gọi với một key mới bị từ chối (`-32005`, `kind:
call_keys`); lời gọi replay một record thì không bao giờ bị từ chối. Các key này
không tính vào `max_records`.

Mỗi lời gọi agent đang chạy cũng chiếm một chỗ trong `max_runs` của tenant. Mỗi
workflow giữ một kết nối cơ sở dữ liệu trong suốt thời gian hold của nó, nên
`max_workflows` giới hạn các kết nối đó. Journal key của một workflow là namespace
của agent (phải là namespace của tenant) cùng `run_id`, nên một tenant không thể
chạm tới workflow của tenant khác. Một tenant không bao giờ đọc nội dung lỗi của
một store hay của một run: một lỗi journal được trả lời bằng `the workflow journal
failed`.

## Hành động thay cho một principal

`run_workflow(..., principal={"tenant": "acme", "user": "ada", "roles":
["support"]})` (`principal` trong `workflow.start`) khiến run của mọi lời gọi
agent hành động thay cho principal đó, nên một quy tắc permission dựa trên role
áp dụng cho các lời gọi của workflow như với một run bắt đầu cho cùng principal.
Principal của một tenant phải nêu tên tenant, như với `run.start`. Không có
principal thì các lời gọi hành động thay cho không ai, và một tính năng theo
principal (bộ nhớ có cấu trúc) không có gì để chạm tới.

Principal được lưu cùng workflow khi nó được start lần đầu. Start lại cùng id
phải nêu cùng principal, đầy đủ (tenant, user và role; thứ tự role không quan
trọng), nếu không việc start bị từ chối bằng `NotAllowed` (`-32004`, `kind:
workflow_principal`) và không có gì thay đổi: các lời gọi đã ghi journal chạy
dưới quy tắc của principal đó, và replay dưới quy tắc khác sẽ đưa kết quả của
chúng cho một user mà chúng không được tạo ra cho. Điều đó gồm cả principal ở
chỗ trước đó không có, và không có principal ở chỗ trước đó có. Hãy start một
`run_id` mới để hành động thay cho người khác.

## Một driver cho mỗi workflow

`workflow.start` lấy một **hold** độc quyền trên `(namespace, run_id)`
(một advisory lock cấp session của Postgres trên một kết nối riêng, nên một server
bị kill giải phóng nó ngay; store bộ nhớ giữ nó trong kết nối) trước khi nó
claim bất cứ thứ gì. Một client thứ hai nhận `WorkflowHeld` (`-32045`) và client
đầu không bị ảnh hưởng. Journal còn được fence bằng một lease epoch, thứ từ chối
các record của một writer cũ. `workflow.finish` (được gửi khi `run_workflow` kết
thúc, kể cả khi bị hủy) giải phóng workflow; một client chết thì để việc đó cho
kết nối của nó kết thúc, nên hãy thử lại một lần start nhận `WorkflowHeld` sau một
lần crash.

Một client biến mất mà không đóng kết nối (mất điện, đứt route) kết thúc theo cùng cách một khi
server nhận ra: nó đặt TCP keepalive trên mọi socket được chấp nhận (30 giây nhàn rỗi, 3
probe cách nhau 10 giây, tức khoảng một phút) và một TCP user timeout cùng độ dài, ping mỗi
WebSocket cứ 30 giây, và kết thúc cái nào không gửi gì trong 60 giây. Cho đến lúc đó kết nối
vẫn giữ chỗ trong `max_connections` của tenant và hold của workflow, nên một lần start thay
thế nhận `WorkflowHeld` trong khoảng một phút; các mốc thời gian là phần
[`liveness`](./server.md#liveness) của cấu hình server. Kết nối cơ sở dữ liệu riêng của hold
đặt các keepalive của Postgres (`tcp_keepalives_idle` 30 giây, `tcp_keepalives_interval` 10
giây, `tcp_keepalives_count` 3), nên một máy chủ server biến mất giải phóng lock sau khoảng
một phút chứ không phải sau mặc định của hệ điều hành (hai giờ). Một Unix socket không cần cả
hai: kernel đóng nó khi process của đầu bên kia kết thúc.

Một server bị kill để lại run con của lời gọi nó đang drive ở trạng thái bền vững
cuối cùng, cùng lease của nó (từ 1 giây đến vài phút, `stores.lease_ttl_ms`). Lời
gọi được resume chờ hết lease và **tiếp tục run đó**, nên các lời gọi model của
nó không bị trả tiền hai lần.

<a id="run_or_resume"></a>
## `run_or_resume`

`run.start` với một `run_id` đã được lưu kết thúc bằng `RunExists`
(`-32023`, "run_id is already stored"). `run_or_resume` làm cho lời gọi trở nên
idempotent:

```python
result = await run_or_resume(agent, "job-7", "Reconcile the ledger")
```

| Trạng thái của `job-7` | Điều gì xảy ra |
|---|---|
| chưa được lưu | run được bắt đầu |
| đã xong | `ResultEvent` của nó được trả về, không có lời gọi model |
| đang chờ một phê duyệt hoặc một câu trả lời | `SuspendedEvent` của nó được trả về; hãy approve hoặc answer, rồi gọi lại |
| chưa xong (process của nó đã chết) | run được resume từ journal của nó |
| đang được process khác drive | chờ tối đa `wait` giây cho lease của nó, rồi resume |

Hãy dùng nó từ task của một job queue, hoặc từ một node LangGraph hay Prefect:
node có thể được thử lại bao nhiêu lần tùy ý và run chỉ xảy ra một lần.

## Lỗi

| Raise | Khi nào |
|---|---|
| `WorkflowHeld` (-32045) | Một client khác điều khiển workflow ([ở trên](#một-driver-cho-mỗi-workflow)); chưa ghi gì |
| `WorkflowBudgetExceeded` (-32046) | `budget` đã hết (`.spent_tokens`, `.spent_usd`), hoặc `data["kind"] == "unpriced"` (`data["model"]`): budget `max_cost_usd` trên model không có giá. Các lời gọi đã xong vẫn nằm trong journal |
| `WorkflowCallFailed` (-32047) | Một lời gọi `wf.agent` không cho kết quả; `.kind` là `failed`, `aborted`, `timeout`, `approval` (run con dừng để chờ phê duyệt) hoặc `invalid` (subagent không biết) |
| `WorkflowNotFound` (-32048) | Một lời gọi cho id workflow mà connection này không điều khiển: chưa gửi `workflow.start` cho nó, hoặc nó đã xong |
| `WorkflowNoJournal` (-32049) | Câu trả lời `resume` cho một workflow mà journal trống |
| `NotAllowed` (-32004) | `kind` là `workflow` (không có `allow.workflow`), `workflow_budget` (budget vượt trần) hoặc `workflow_principal` (principal khác với principal lúc workflow bắt đầu); một `principal` của tenant khác |
| `LimitReached` (-32005) | `kind` là `workflows`, `agent_calls_in_flight` hoặc `journal_records` ([Admission](#admission)) |
| `OpenError` (-32011) | `workflow.start` với budget `max_cost_usd` cho agent có model chưa có giá |
| `RunNotFound` (-32021) | `workflow_status` của một id mà namespace của agent không có |
| `WorkflowSuspended` | Không phải lỗi của server: một interrupt chưa có câu trả lời, và `run_workflow` raise nó (một `BaseException`) sau khi workflow được lưu là `awaiting_input` |
| `WorkflowTimeoutError` | Một lần thử của step vượt giới hạn, hoặc `deadline_ms` đã qua (phía client) |

Mọi lỗi của server đều là `WorkflowError` trừ `NotAllowed`, `LimitReached`,
`OpenError` và `RunNotFound`. `workflow_status(agent, run_id)` trả về một
`WorkflowStatus` (`status`, `records`, `interrupt_key`, `interrupt_payload`)
mà không điều khiển workflow. `Retry(max_attempts, base_delay_ms,
max_delay_ms)` truyền qua `retry=` lặp lại một `wf.agent` hay `wf.step` thất bại
hoặc quá hạn; chỉ lần thử thắng được ghi vào journal.

## Khác biệt so với Python Linch

| | Python | Ở đây |
|---|---|---|
| Hàm chạy ở đâu | trong process sở hữu agent | trong client; server giữ journal |
| Key của lời gọi | sha256 của loại subagent, prompt, tool filter và tùy chọn run (`journal.py:40`) | digest của name, prompt, subagent, `output_schema`, `fork`; `key=` thay thế chúng |
| Occurrence và nhánh | bộ đếm cho mỗi key | giống vậy, đếm riêng cho mỗi đường dẫn nhánh fan-out |
| `tools=` | một tool filter cho mỗi lời gọi | không nhận: định nghĩa subagent nêu tên các tool của nó, và `subagent=` chọn nó |
| `fork=True` | tiếp tục cuộc hội thoại của host | `fork=<session id hoặc AgentResult>`: tiếp tục một session được nêu tên |
| `isolation=` | một thư mục làm việc riêng cho mỗi nhánh | không có |
| `journal_snapshot_every` | snapshot phần fold của event log | không cần: journal là một bảng record được nạp bằng một query |
| Lease | `RunLease`, năm phút, được gia hạn | một hold cộng một epoch; không có hết hạn để chờ cho chính workflow |
| Câu trả lời interrupt | `resume=` ở `run_workflow` kế tiếp | giống vậy; `workflow.status` đọc câu hỏi đang chờ |
| Budget | một `RunBudget` dùng chung cho cả cây | `Budget(max_tokens, max_cost_usd)`; được kiểm tra trước mỗi lời gọi thật, nên một lời gọi đang chạy có thể vượt |
| `max_agent_concurrency` | giới hạn các lời gọi provider thật | `allow.workflow.max_fan_out` cho một tenant |
| Hủy | hủy task | hủy task: server abort run của lời gọi đó |

## Những chỗ còn thiếu

- Không có `workflow.answer` riêng: một câu trả lời đi qua `resume` của
  `workflow.start` kế tiếp.
- Deadline và timeout của step được thực thi trong client; một client chết thì
  không giữ cái nào. `timeout_ms` của một lời gọi agent được server thực thi.
- Một agent dùng store bộ nhớ chỉ ghi journal trong vòng đời kết nối của nó: sau một kết nối
  mới (hoặc một lần khởi động lại server) workflow chạy lại từ step đầu tiên, **kể cả các
  step**, vì journal trống. Một `workflow.start` mang câu trả lời `resume` cho một workflow mà
  journal không có gì bị từ chối (`-32049`, `WorkflowNoJournal`) thay vì chạy lại tất cả với
  những câu trả lời không có gì để trả lời. Một start không có câu trả lời không bị từ chối:
  đó là cách một workflow bắt đầu. Hãy dùng store Postgres cho workflow phải sống sót qua một
  kết nối.

Các method trên wire nằm ở [RPC](../architecture/rpc.md#các-method-workflow) và
engine nằm ở [Extensions](../architecture/ext.md#workflows-qua-rpc).
