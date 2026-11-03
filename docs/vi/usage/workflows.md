# Workflow có định hướng (điều phối nhóm agent có thể phát lại)

[Bản gốc tiếng Anh](../../usage/workflows.md)

Dùng `run_workflow` khi mã của bạn cần điều khiển và phát lại một chuỗi điều phối đã xác định.

[← Hướng dẫn sử dụng](./README.md)

`run_workflow(agent, fn)` chạy một hàm Python async thông thường để điều phối các subagent — mã của bạn quyết định luồng điều khiển có định hướng, còn các subagent thực hiện công việc do mô hình dẫn dắt. Khi có kho lưu trữ run, nhật ký cho phép phát lại các lời gọi khớp và resume một run bền vững.

Dùng workflow có định hướng khi bạn đã biết cấu trúc điều phối (phân nhánh song song, xác minh, tổng hợp) và muốn dùng cùng `run_id` để phát lại các lời gọi khớp. Nếu muốn chính mô hình quyết định cần tạo subagent nào và vào lúc nào, hãy dùng [chế độ coordinator](./deep-agent.md) — workflow đánh đổi tính linh hoạt đó để mã quyết định kế hoạch và hỗ trợ resume.

---

<a id="run_workflowfn"></a>

## `run_workflow(agent, fn)`

Bạn truyền vào một hàm async nhận một ngữ cảnh `wf` duy nhất. Các thao tác cơ bản được ghi nhật ký (`wf.agent`, `wf.step` và `wf.interrupt`) trở thành các bản ghi có thể phát lại; các hàm hỗ trợ phân nhánh song song và pipeline điều phối công việc, còn `wf.phase` phát thông tin tiến độ. Giá trị trả về của hàm trở thành kết quả workflow.

```python
import os
from linch import Agent, RunBudget, PostgresRunStore
from linch.ext.workflow import run_workflow

async def review(wf):
    await wf.phase("Find")
    findings = await wf.parallel([
        lambda: wf.agent("review the diff for bugs", label="bugs"),
        lambda: wf.agent("review the diff for performance", label="perf"),
    ])
    await wf.phase("Verify")
    verdicts = await wf.pipeline(
        findings,
        lambda f: wf.agent(f"adversarially verify: {f}"),
    )
    return verdicts

agent = Agent(model="gpt-5", run_store=PostgresRunStore(os.environ["LINCH_POSTGRES_DSN"]))
result = await run_workflow(
    agent,
    review,
    budget=RunBudget(max_tokens=500_000),   # shared across every wf.agent child
    run_id="review-pr-42",                  # enables journaling + resume
    on_event=lambda e: print(e.type),       # WorkflowEvents + child SubagentEvents
)
```

Các đối số từ khóa của `run_workflow` điều khiển budget, việc ghi nhật ký và quan sát:

- `budget` — một `RunBudget` dùng chung, giới hạn token/chi phí trên toàn bộ workflow và mọi agent con được tạo bởi `wf.agent`. Xem [Budget của run](./agent.md) để biết ngữ nghĩa của budget.
- `run_id` — một định danh ổn định. Khi kết hợp với `Agent(run_store=...)`, định danh này bật ghi nhật ký và resume (xem bên dưới).
- `on_event` — một callback nhận cả `WorkflowEvent` (giai đoạn, bắt đầu, kết thúc và phát lại agent/bước) lẫn các `SubagentEvent` con do mỗi lời gọi `wf.agent` phát ra.
- `max_concurrency` — số nhánh mà một lần phân nhánh `wf.parallel` có thể chạy cùng lúc (4 nếu không đặt). Mỗi cấp lồng nhau có semaphore riêng, nên một `wf.parallel` lồng bên trong được giới hạn độc lập thay vì cạnh tranh — và gây deadlock — trên các slot mà bên gọi nó đang giữ.
- `max_agent_concurrency` — số lời gọi `wf.agent` được phép giữ một *slot provider đang hoạt động* cùng lúc (không giới hạn khi là `0`, giá trị mặc định). Đây là tham số điều chỉnh áp lực ngược lên provider, được tách riêng có chủ đích khỏi cấu trúc phân nhánh của `max_concurrency`: nó giới hạn chi phí thực tế bất kể các nhánh lồng nhau thế nào và không bao giờ chặn một lời gọi được phát lại.
- `step_timeout_ms` — budget thời gian thực mặc định cho mỗi lần thử của `wf.agent` và `wf.step`. Mỗi lời gọi có thể ghi đè giá trị này, và `0` loại lời gọi đó khỏi giới hạn. Lời gọi hết hạn sẽ phát sinh `WorkflowTimeoutError`.
- `deadline_ms` — giới hạn thời gian thực cho toàn bộ workflow. Khi hết hạn, hàm bị hủy và phát sinh `WorkflowTimeoutError`; với một run bền vững, các mục nhật ký đã hoàn tất vẫn được giữ lại, nên khi resume có thể phát lại các lời gọi khớp rồi tiếp tục.
- `resume` — một ánh xạ `{interrupt_key: value}` cung cấp câu trả lời cho các lời gọi `wf.interrupt` còn chờ từ lần tạm dừng trước. Xem [Con người tham gia vào luồng xử lý](#con-người-tham-gia-vào-luồng-xử-lý).
- `signal` — một `AbortContext` dừng workflow ở lời gọi được ghi nhật ký tiếp theo và được truyền vào mọi subagent mà workflow tạo ra.
- `journal_snapshot_every` — tạo checkpoint cho nhật ký sau mỗi N bản ghi để khi resume chỉ cần tổng hợp các event sau checkpoint thay vì toàn bộ nhật ký (mặc định tắt; cách này đánh đổi việc ghi định kỳ để chỉ cần tải một lần, nên chỉ có lợi với các run dài).
- `lease` — một `RunLease` tùy chọn để bảo vệ các thao tác ghi workflow bền vững; bộ máy thực thi gia hạn và giải phóng lease trong thời gian workflow hoạt động.

---

<a id="the-wf-context"></a>

## `WorkflowContext`

Đối số `wf` là một `linch.ext.workflow.WorkflowContext`. Nó cung cấp một tập giao diện nhỏ, cố định — bạn kết hợp các thao tác cơ bản này thành quy trình điều phối thay vì gọi trực tiếp vòng lặp:

- `await wf.agent(prompt, *, name=None, label=None, tools=None, run_options=None, key=None, output_schema=None, final_tool_name=None, fork=False, isolation=None, isolation_keep=False, timeout_ms=None, retry=None) -> str` — chạy một subagent (một định nghĩa có tên trong sổ đăng ký subagent, hoặc subagent đa dụng tích hợp sẵn) và trả về văn bản cuối cùng của nó. Khi agent con tạo `structured_output`, chuỗi trả về là JSON rút gọn. Agent con thất bại sẽ phát sinh `WorkflowError`; agent con bị hủy sẽ phát sinh `AbortError`. `fork=True` tiếp tục từ phần đầu hội thoại của agent chủ workflow, còn `isolation=` cấp cho agent con một thư mục làm việc riêng để xử lý các chỉnh sửa song song có xung đột.
- `await wf.step(name, fn, *, key=None, timeout_ms=None, retry=None) -> Any` — chạy *fn* (một callable không nhận đối số, đồng bộ hoặc async) như một bước được ghi nhật ký và trả về giá trị của nó. Đây là cách biến mã tất định, một lời gọi tool hoặc bất kỳ công việc nào có tác dụng phụ thành một nút có thể phát lại, thay vì thứ phải chạy lại mỗi khi resume. Xem [Ghi nhật ký cho mã của bạn](#ghi-nhật-ký-cho-mã-của-bạn) bên dưới.
- `await wf.interrupt(key, payload=None) -> Any` — tạm dừng workflow cho đến khi có người cung cấp câu trả lời cho *key*. Xem [Con người tham gia vào luồng xử lý](#con-người-tham-gia-vào-luồng-xử-lý).
- `await wf.parallel(thunks) -> list` — chạy đồng thời các thunk, chịu giới hạn bởi `max_concurrency` (mặc định 4); kết quả giữ nguyên thứ tự đầu vào. Một nhánh thất bại sẽ hủy các nhánh còn lại.
- `await wf.settled(thunks) -> list[StepOutcome]` — giống `parallel`, nhưng khi một nhánh thất bại, các nhánh còn lại vẫn tiếp tục chạy. Xem [Chính sách xử lý lỗi](#chính-sách-xử-lý-lỗi).
- `await wf.pipeline(items, *stages) -> list` — đưa từng phần tử qua tất cả các giai đoạn một cách độc lập, không có rào chắn đồng bộ giữa các giai đoạn.
- `await wf.phase(title)` — phát một dấu mốc giai đoạn tiến độ.
- `wf.budget` — `RunBudget` dùng chung (xem [Budget của run](./agent.md)).

Một vài lưu ý thực tế:

- `wf.parallel` nhận các *thunk* (callable không nhận đối số, trả về awaitable), chứ không nhận coroutine đã tạo sẵn, để ngữ cảnh có thể lập lịch chúng dưới semaphore giới hạn mức đồng thời. Bọc mỗi lời gọi bằng `lambda: wf.agent(...)`.
- Khi một nhánh `wf.parallel` phát sinh ngoại lệ, các nhánh còn lại bị hủy và được chờ kết thúc trước khi lỗi được truyền ra ngoài — một lần phân nhánh thất bại sẽ dừng tiêu tốn chi phí ngay lập tức. Các nhánh bị hủy không được ghi nhật ký, nên chúng sẽ chạy lại khi resume.
- `wf.pipeline` đưa từng phần tử qua mọi giai đoạn một cách độc lập — không có rào chắn đồng bộ giữa các giai đoạn, nên phần tử chậm không bao giờ chặn phần tử nhanh. Điều này khác với `parallel` + một `parallel` thứ hai, vốn phải chờ toàn bộ lô đầu tiên.
- `name` chọn một định nghĩa subagent đã đăng ký; `label` là nhãn dễ đọc được đưa vào các event phát ra để truy vết.

---

<a id="journal-and-resume"></a>

## `WorkflowJournal` — nhật ký và resume

Khi có `run_id` và `run_store`, mỗi kết quả `wf.agent` và `wf.step` được lưu bền vững dưới dạng một `WorkflowEvent` (`agent_end` hoặc `step_end`) trong nhật ký event của run. Gọi lại `run_workflow` với cùng `run_id` sẽ phát lại các lời gọi khớp từ nhật ký đó — các event `agent_replayed` hoặc `step_replayed` được phát ra thay cho lời gọi provider hoặc hàm của bước. Khóa của lời gọi agent được tạo từ mã băm nội dung của `(subagent_type, prompt, tools, run_options)` cộng với bộ đếm số lần xuất hiện, nên các lời gọi song song giống nhau vẫn phát lại an toàn. Với định danh tự động, việc chỉnh sửa prompt, tùy chọn đầu ra có cấu trúc hoặc bộ lọc tool khiến lời gọi đó không còn khớp khóa cũ, thay vì tái sử dụng kết quả được tạo theo một chính sách tool khác; các lời gọi phía sau có khóa riêng vẫn khớp vẫn có thể được phát lại. Với `key=`, bên gọi quyết định định danh và phải thay đổi nó khi các yếu tố phụ thuộc đó thay đổi.

Các loại `WorkflowEvent` bạn sẽ thấy trên luồng `on_event` gồm:

| Loại | Thời điểm phát ra |
|------|---------------|
| `phase` | Bạn gọi `await wf.phase(title)` |
| `agent_start` | Một lời gọi `wf.agent` bắt đầu chạy subagent thực tế |
| `agent_end` | Một lời gọi `wf.agent` hoàn tất; kết quả được ghi nhật ký |
| `agent_replayed` | Một run được resume tìm thấy kết quả đã ghi nhật ký khớp và bỏ qua lời gọi provider |
| `step_start` | Một lời gọi `wf.step` bắt đầu thực thi hàm của nó |
| `step_end` | Một lời gọi `wf.step` hoàn tất; giá trị mã hóa JSON được ghi nhật ký trong `result_text` |
| `step_replayed` | Một run được resume tìm thấy giá trị đã ghi nhật ký khớp và bỏ qua hàm |
| `interrupt_requested` | Một `wf.interrupt` chưa có câu trả lời; run sắp tạm dừng. `structured_output` mang `{"payload": ...}` |
| `interrupt_resolved` | Một câu trả lời từ `resume=` được chấp nhận và ghi nhật ký |
| `interrupt_replayed` | Một run được resume tìm thấy câu trả lời đã ghi nhật ký và không tạm dừng lần nữa |

Vì nhật ký dùng mã băm nội dung làm khóa, resume có tính chính xác: chỉnh sửa một prompt ở giữa workflow sẽ làm mất hiệu lực của lời gọi đó, còn bất kỳ lời gọi nào trước hoặc sau có khóa riêng vẫn khớp đều có thể phát lại từ nhật ký.

Các run workflow bền vững yêu cầu `schema_version=3` trong siêu dữ liệu và dùng công thức khóa lời gọi hiện hành có xét đến tool. Các phiên bản siêu dữ liệu bị thiếu, cũ hoặc không xác định đều bị từ chối; hãy bắt đầu một run workflow mới. Các đối số dùng để phát lại được mã hóa bằng JSON chuẩn hóa, trong đó các tập hợp được sắp xếp; khi dùng định danh tự động, các giá trị không được hỗ trợ sẽ phát sinh `ConfigError` trước khi thực thi. `wf.agent(key=...)` tường minh bỏ qua việc tuần tự hóa `run_options` và các yếu tố phụ thuộc; khóa do bên gọi quyết định và mọi bộ lọc tool vẫn cần có các giá trị có thể tạo dấu vân tay JSON. Bộ lọc tool vẫn là một phần của mã băm, và khóa do bên gọi quyết định phải bao gồm các phiên bản chính sách và yếu tố phụ thuộc mà khi thay đổi cần làm mất hiệu lực phát lại.

---

## Ghi nhật ký cho mã của bạn

Chỉ các lời gọi được ghi nhật ký mới được phát lại. `wf.agent` được ghi nhật ký, và `wf.step` cũng vậy; mọi thứ khác trong hàm workflow đều thực thi lại mỗi khi resume:

```python
async def flow(wf):
    findings = await wf.agent("review the diff")
    await post_to_github(findings)              # runs again on every resume
    return await wf.agent("summarize")
```

Bọc tác dụng phụ đó trong một bước để khi resume có thể phát lại kết quả đã được ghi nhật ký bền vững. Tác dụng phụ vẫn cần tính lũy đẳng để xử lý trường hợp tiến trình gặp sự cố trước khi ghi nhật ký:

```python
async def flow(wf):
    findings = await wf.agent("review the diff")
    await wf.step("publish", lambda: post_to_github(findings))
    return await wf.agent("summarize")
```

`name` của bước là định danh bền vững của nó — đổi tên sẽ làm mất hiệu lực mục nhật ký cũ của bước đó, giống như chỉnh sửa một prompt. Các lời gọi phía sau có khóa riêng vẫn khớp có thể được phát lại. Truyền `key=` khi cùng một bước chạy trên các đầu vào khác nhau, để mỗi đầu vào được ghi nhật ký riêng thay vì chỉ được phân biệt bằng thứ tự lời gọi:

```python
for url in urls:
    await wf.step("fetch", lambda u=url: http.get(u), key=url)
```

Giá trị của một bước được ghi nhật ký dưới dạng JSON, nên phải tuần tự hóa được thành JSON. Nếu không, `wf.step` phát sinh `ConfigError` — bất kể có cấu hình kho lưu trữ run hay không, để một giá trị không bao giờ có thể phát lại bị báo lỗi ngay trong quá trình phát triển thay vì lần đầu bạn truyền `run_id`.

---

## Chính sách xử lý lỗi

Mặc định, mỗi nút có một lần thử và lỗi sẽ được truyền ra ngoài. Có hai cách điều chỉnh hành vi đó.

`retry=` chạy lại một `wf.agent` hoặc `wf.step` thất bại với thời gian chờ tăng theo hàm mũ. Chỉ lần thử thành công mới được ghi nhật ký, nên một nút có thử lại vẫn chỉ phát lại đúng một lần — nhưng *fn* phải an toàn khi chạy nhiều lần:

```python
from linch import RetryOptions

await wf.agent("review the diff", retry=RetryOptions(max_attempts=3))
await wf.step("fetch", lambda: http.get(url), key=url, retry=RetryOptions(max_attempts=3))
```

Các lần thử thất bại không phát ra bản ghi kết quả/kết thúc được ghi nhật ký, nên một nút đã dùng hết số lần thử lại sẽ chạy lại từ đầu ở lần resume tiếp theo thay vì phát lại một thất bại. Event `*_start` bao quanh vẫn có thể được quan sát. `ConfigError` — tên subagent không xác định, giá trị không thể mã hóa JSON — không bao giờ được thử lại, vì lần nào cũng thất bại như nhau.

`wf.settled` cho phép bỏ hành vi hủy các nhánh còn lại. Trong khi `wf.parallel` hủy các nhánh còn lại ngay khi một nhánh phát sinh ngoại lệ, `settled` để tất cả chạy xong và trả về một `StepOutcome` cho mỗi nhánh, theo thứ tự đầu vào:

```python
outcomes = await wf.settled([
    lambda: wf.agent("review for bugs"),
    lambda: wf.agent("review for perf"),
])
findings = [o.value for o in outcomes if o.ok]
for failed in (o for o in outcomes if not o.ok):
    print(failed.error)
```

`StepOutcome` có `.ok`, `.value` và `.error`. Hủy và tạm dừng thuộc về luồng điều khiển chứ không phải kết quả của nhánh: chúng được truyền ra ngoài thay vì đưa vào một vị trí trong danh sách.

---

## Con người tham gia vào luồng xử lý

`wf.interrupt(key, payload)` tạm dừng một workflow tại điểm cần quyết định. Ở lần đầu đi qua, chưa có câu trả lời, nên một run bền vững phát ra event `interrupt_requested` mang payload, được đánh dấu `"suspended"` trong kho lưu trữ run — **không phải** thất bại — và `WorkflowSuspended` được phát sinh ra khỏi `run_workflow`:

```python
from linch.ext.workflow import WorkflowSuspended

async def flow(wf):
    plan = await wf.agent("draft a migration plan")
    approved = await wf.interrupt("approve-plan", {"plan": plan})
    if not approved:
        return "cancelled"
    return await wf.step("apply", lambda: apply_migration(plan))

try:
    await run_workflow(agent, flow, run_id="migrate-42")
except WorkflowSuspended as suspended:
    show_to_reviewer(suspended.key, suspended.payload)
```

Cung cấp câu trả lời bằng cách gọi lại cùng `run_id` với một ánh xạ `resume`. Các lời gọi đã ghi nhật ký khớp được phát lại, interrupt trả về giá trị và run tiếp tục:

```python
await run_workflow(agent, flow, run_id="migrate-42", resume={"approve-plan": True})
```

Câu trả lời được ghi nhật ký như bất kỳ nút nào khác, nên một lần resume *sau đó* không cần đối số `resume` — nó được phát lại dưới dạng `interrupt_replayed`. Giá trị phải tuần tự hóa được thành JSON, và payload cũng vậy.

Ba điều cần biết:

- `WorkflowSuspended` kế thừa `BaseException`, không phải `Exception`, nên một `except Exception:` bên trong hàm workflow không thể vô tình nuốt mất việc tạm dừng.
- Một mục `resume` được dùng một lần. `wf.interrupt` thứ hai với cùng khóa sẽ tạm dừng lần nữa thay vì âm thầm dùng lại quyết định trước — điều này cũng có nghĩa interrupt trong vòng lặp cần các khóa riêng biệt.
- Interrupt bên trong một nhánh `wf.parallel` sẽ hủy các nhánh còn lại, nên một lần phân nhánh không thể thu thập nhiều câu trả lời cùng lúc. Hãy yêu cầu các quyết định theo thứ tự. Nếu công việc ở các nhánh còn lại cần hoàn tất trước khi tạm dừng, dùng `wf.settled`; nó vẫn truyền `WorkflowSuspended` ra ngoài thay vì thu thập nhiều câu trả lời.

---

<a id="chay-qua-nhieu-tien-trinh"></a>

## Chạy qua nhiều tiến trình

`linch.worker` không điều phối workflow: job của nó chạy một prompt qua session, không
chạy hàm workflow. Ứng dụng chủ tự quyết định tiến trình nào gọi `run_workflow`, ví dụ
từ hàng đợi job hoặc dịch vụ của chính nó. Trạng thái workflow bền vững vẫn dùng chung
an toàn: chỉ cần `run_store` Postgres và `run_id` ổn định, mọi tiến trình có cùng hàm
workflow đều chạy tiếp được.

- **Khôi phục sau crash là việc của ứng dụng chủ.** Không có gì tự gọi lại workflow bị
  gián đoạn. Hãy tìm các run chưa xong và gọi lại `run_workflow` với cùng `run_id`.
- **Sau crash sẽ phải chờ.** Khi không truyền `lease=`, `run_workflow` claim run trong
  năm phút và gia hạn khi còn sống. Tiến trình chết sẽ để lại lease đó, nên gọi lại quá
  sớm sẽ ném `RunLeaseConflictError`. Hãy thử lại với backoff. Chính lỗi này cũng ngăn
  hai tiến trình cùng chạy một workflow.
- **Câu trả lời có thể đến từ đâu cũng được.** Run đang suspended không giữ tiến trình
  nào. Mọi tiến trình của ứng dụng chủ đều có thể trả lời bằng `run_workflow` với
  `resume=`.
- **Trong workflow, `max_concurrency` tính theo từng tiến trình.** Subagent chạy trong
  tiến trình đã gọi `run_workflow`. Muốn dùng cả fleet, hãy tách thành một workflow cho
  mỗi mục và để hàng đợi của bạn phân tán chúng.

---

## Quy tắc phát lại

Hàm workflow phải giữ luồng điều khiển tất định — không phân nhánh theo `random`, thời gian thực hoặc môi trường — để resume phát lại chính xác các lời gọi đã ghi nhật ký không thay đổi.

Phân nhánh dựa trên kết quả đã ghi nhật ký là hợp lệ: `if "LGTM" in await wf.agent(...)` phát lại y hệt, vì chính kết quả cũng được phát lại. Điều làm hỏng resume là sự biến thiên mà nhật ký chưa từng ghi nhận — một mẫu ngẫu nhiên, thời gian hiện tại, một biến môi trường hoặc phản hồi HTTP được lấy bằng mã gọi trực tiếp. Đưa sự biến thiên đó vào một `wf.step` để giá trị trả về được ghi nhật ký (và dùng `key=` khi cần định danh đầu vào của bước một cách tường minh). Khi đó, run được resume đi theo đúng đường đi của lần đầu.

---

## Các trang liên quan

- [Cấu hình sẵn deep agent](./deep-agent.md) — phương án coordinator do LLM dẫn dắt
- [Cấu hình agent](./agent.md) — `RunBudget` và các kho lưu trữ bền vững
- [Event](./events.md) — xử lý luồng `on_event`
- [Kiến trúc](../../architecture.md) — vị trí của vòng lặp nhóm agent trong thiết kế tổng thể
