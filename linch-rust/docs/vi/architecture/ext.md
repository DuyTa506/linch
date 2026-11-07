# Extensions: eval, báo cáo và workflow

[Bản gốc tiếng Anh](../../architecture/ext.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

`linch-ext` (`crates/linch-ext/src/`) chứa ba extension chỉ xây trên API công khai của
`linch-core`: bộ chạy eval, báo cáo run và workflow có journal. Mọi run mà chúng tạo ra
đều đi qua `Agent::run`, nên chúng hoạt động với mọi provider và store; để dùng offline,
có tính xác định, hãy gắn agent với một `ScriptedProvider` và các store trong bộ nhớ
(`crates/linch-ext/src/lib.rs:10-12`). Feature `postgres` thêm journal workflow bền vững
và `migrate` của nó.

Linch bản Python giữ cùng cách phân chia
([extensions](../../../../docs/architecture/subsystems.md#311-extensions-and-host-owned-boundaries)).
Journal workflow là nơi khác biệt nhiều nhất; xem [workflows](#workflows).

## Evals

`run_eval(agent, cases, scorers)` (`crates/linch-ext/src/evals/harness.rs:196`)
chạy lần lượt các case, mỗi case trên một session id mới mà
`Agent::close_session` giải phóng khi run kết thúc, cùng với session
của mọi run con mà các lời gọi `Subagent` của nó đã tạo. Một run suspend để chờ phê
duyệt được để nguyên trạng thái suspend. Vì các case chạy theo thứ tự, một
`ScriptedProvider` tiêu thụ các lượt của nó theo thứ tự case; run con của một subagent
lấy các lượt nằm giữa lời gọi `Subagent` của run cha và lượt kế tiếp của run cha.

Một case đạt khi run của nó không ghi nhận lỗi và không scorer nào trả về
`Some(false)`; `None` là một kết luận không xác định và không làm nó thất bại
(`crates/linch-ext/src/evals.rs:10-11`). `CaseResult` giữ đầu ra, các
kết luận, các envelope của run và lỗi; `EvalResult` thêm `total`,
`passed`, `pass_rate`, `to_value` (JSON) và `to_markdown`.

### Scorers

`Scorer` (`crates/linch-ext/src/evals/scorers.rs:23`) là một enum đóng mà dạng serde của
nó là một mục trong tệp suite: `{"type": "<snake_case>", ...fields}`, từ chối các type và
trường không biết.

| `type` | Kết luận |
|---|---|
| `text_contains` | Đầu ra chứa `substring` (mặc định `{expected}`, được thay bằng đáp án mong đợi của case), không phân biệt hoa thường |
| `tool_called` | Run đã bắt đầu một lời gọi `tool` |
| `schema_valid` | Đầu ra là JSON mà `schema` chấp nhận; đầu ra không phải JSON, hoặc một schema không hợp lệ, thì thất bại |
| `context_selected_tool` | Một event `context_build` đã đưa ra `tool` |
| `context_not_trimmed` | Không có event `context_build` nào báo `budget.trimmed: true` |
| `context_metadata_contains` | Metadata của một event `context_build` chứa `key` (kèm giá trị `expected` khi được đặt; số được so sánh theo giá trị, như `==` của Python) |
| `recovery_succeeded` | Một lời gọi `tool` thất bại (bất kỳ tool nào khi không đặt) được theo sau bởi một lời gọi thành công; không xác định khi không có lời gọi nào kết thúc |
| `run_completed` | Run kết thúc với một `result` thành công; không xác định khi không có |

Các scorer `context_*` chỉ trả về `None` với một run không có event `context_build`,
giống như của Python. Runner Rust stage một event như vậy trước mỗi lời gọi chính,
với `selected_tools` được đặt, `budget` rỗng và `metadata` được gộp từ những gì các hook
`context_build` của nó trả về, khóa của hook đến sau thắng, như `dict.update` của Python
(`crates/linch-core/src/hooks/turn.rs`,
`crates/linch-core/src/runner/hooks.rs`). Vì vậy với một run Rust,
`context_selected_tool` báo tool có được đưa ra hay không,
`context_metadata_contains` đọc metadata của các hook, và
`context_not_trimmed` luôn đạt: kernel không cắt bớt ngữ cảnh nào.

### Suite và benchmark

`EvalSuite` (`crates/linch-ext/src/evals/suite.rs:32`) là một tệp JSON:

```json
{
  "name": "capitals",
  "cases": [{"prompt": "Capital of France?", "expected": "Paris"}],
  "scorers": [{"type": "text_contains"}, {"type": "run_completed"}]
}
```

`cases` là bắt buộc; `expected` và `metadata` của một case, cùng `name`, `scorers` và
`metadata` của suite, có thể bỏ qua. `EvalSuite::from_json`
từ chối các trường không biết cùng prompt, substring, tool và khóa metadata rỗng.
`load_eval_suite(path)` (`suite.rs:98`) đọc một tệp và dùng tên gốc của tệp khi
tên rỗng. `run_eval_benchmark(suite, targets)`
(`crates/linch-ext/src/evals/benchmark.rs:153`) chạy một suite trên nhiều
agent có tên lần lượt, đo thời gian từng agent, và hiển thị bảng so sánh dưới dạng JSON
hoặc Markdown.

## Báo cáo

`build_run_report(events, record)` (`crates/linch-ext/src/reports.rs:113`)
gộp bất kỳ danh sách envelope nào thành một `RunReport`: envelope của một stream đang
chạy, của một eval case, hoặc của một store. Dựng báo cáo không bao giờ thất bại.
`load_run_report(runs, namespace, run_id)` (`reports.rs:212`) đọc record
và các event từ một `RunStore`, bỏ qua các delivery và receipt lời gọi model chỉ dành
cho journal, như `Agent::events` làm.

`RunReport` (`reports.rs:36-73`) giữ id của run và session, trạng thái, phase của
checkpoint, mọi lời gọi tool đã kết thúc, các permission request, event `context_build`
và `loop_guard`, các payload lỗi, `usage` cuối cùng, `result`,
toàn bộ timeline, và hai bản tóm tắt:

- `ReportSummary` (`crates/linch-ext/src/reports/summary.rs:14`): số lượng event,
  usage, thời gian và lỗi của tool, ngữ cảnh, khôi phục, prompt cache và các
  số liệu rủi ro;
- `LongRunSummary`: sự tăng trưởng ngữ cảnh, chất lượng và trạng thái resume.

Khi không có record, trạng thái được suy ra từ các event: `unknown`, rồi
`running` sau `init`, rồi trạng thái của event `suspended` cuối cùng, cho đến khi một
`result` đặt trạng thái cuối. Dạng serde của nó là báo cáo JSON;
`to_markdown()` hiển thị nó cho con người (`crates/linch-ext/src/reports/markdown.rs:11`).

## Workflows

Một workflow là mã của host nắm quyền điều khiển luồng; mọi lời gọi đã hoàn tất đều
được ghi vào journal, nên gọi lại `run_workflow` với cùng workflow id sẽ phát lại nó
(`crates/linch-ext/src/workflow.rs:1-11`). Python ghi các event workflow vào run store
và gộp chúng khi resume. Rust giữ một `JournalStore` riêng chỉ ghi các lời gọi đã hoàn
tất, nên các record chính là cache phát lại và không cần gộp
(`crates/linch-ext/src/workflow/journal.rs:1-8`).

### Các lời gọi

`run_workflow(options, f)` (`crates/linch-ext/src/workflow/engine.rs:99`)
claim workflow, rồi chạy `f` trên một `WorkflowContext` được nạp sẵn các record đã
lưu. Các lời gọi được ghi vào journal:

| Lời gọi | Giá trị ghi vào journal | Khi resume |
|---|---|---|
| `step(name, f)` / `step_with(name, StepOptions, f)` | Giá trị của `f`, qua JSON | Được phát lại mà không gọi `f` |
| `agent(&agent, name, prompt)` | Văn bản cuối cùng của run | Được phát lại; một run chưa xong được tiếp tục |
| `interrupt(key, &payload)` | Câu trả lời | Được phát lại mà không hỏi lại |

Mọi thứ khác trong hàm chạy lại ở mỗi lần resume. `parallel` và
`settled` tỏa các nhánh ra, tối đa `max_concurrency` nhánh cùng lúc cho mỗi lời gọi
(mặc định 4): `parallel` fail nhanh và bỏ các nhánh còn lại, `settled`
trả về mọi kết quả (`crates/linch-ext/src/workflow/fanout.rs:34`, `fanout.rs:59`).
Không có gì được spawn; mọi nhánh chạy trong task của bên gọi. `phase(title)`
phát một event tiến độ. Mỗi lời gọi phát một `WorkflowEvent` tới
sink `on_event` tùy chọn, với kind là `phase`, `step_start`, `step_end`,
`step_replayed`, `agent_start`, `agent_end`, `agent_replayed`,
`interrupt_requested`, `interrupt_resolved` hoặc `interrupt_replayed`
(`crates/linch-core/src/events/kinds/skills.rs:50`).

### Quy tắc phát lại

Một lời gọi được xác định bằng một khóa nội dung cộng với lần xuất hiện của nó: có bao
nhiêu lời gọi với khóa đó đã đến trước nó trong run này (`workflow.rs:43-51`). Các khóa
là digest được tách miền: `step_key` của tên và khóa input tùy chọn,
`agent_key` của tên và prompt, `interrupt_key` chỉ của khóa, nên một
payload có thể đổi giữa các lần resume mà không mất câu trả lời
(`journal.rs:164-196`). Hàm phải đi lại cùng một đường: rẽ nhánh
theo một giá trị đã ghi journal thì được, rẽ nhánh theo đồng hồ, số ngẫu nhiên hay
môi trường thì không, trừ khi giá trị đó đến từ một step.

Bên trong `parallel` và `settled`, mỗi nhánh đếm các lần xuất hiện riêng của nó: khóa
journal của một lời gọi ở đó là khóa nội dung của nó, `@`, và đường dẫn nhánh của nó
(chỉ số nhánh ở mỗi cấp, ngoài cùng trước, ví dụ `@1` hoặc `@0.2`). Khi phát lại, các lời
gọi đã ghi journal trả về ngay, nên các nhánh đến một tên dùng chung theo thứ tự khác
với khi chạy thật; một bộ đếm dùng chung giữa các nhánh sẽ đưa record của nhánh này cho
nhánh khác và chạy lại một step đã ghi journal. Đường dẫn là một biến task-local mà mỗi
nhánh được poll bên dưới nó
(`crates/linch-ext/src/workflow/context.rs:110`, `context.rs:161-183`).
Tính đồng thời được dựng theo cách khác (`join!` hoặc `select!`, hoặc một task được
spawn, vốn không kế thừa đường dẫn) không có đường dẫn riêng, nên các lời gọi ở đó cần
tên hoặc khóa khác nhau. Python đếm một lần xuất hiện cho mỗi khóa trên toàn run, kể cả
các nhánh.

Giá trị của một step đã bền vững trước khi `step` trả về. Một process gặp sự cố sau khi
`f` chạy nhưng trước khi record được ghi sẽ chạy lại `f` khi resume, nên các tác dụng
phụ cần idempotent (`crates/linch-ext/src/workflow/step.rs:48-55`). Với chính sách
`Retry` (`crates/linch-ext/src/workflow/policy.rs:18`), chỉ lần thử thắng cuộc được ghi
journal. Lỗi và timeout của step được thử lại; lỗi mã hóa và một workflow bị hủy thì không.

Một lời gọi `agent` chạy trên session riêng của nó với run id suy ra từ
workflow, khóa của lời gọi, lần xuất hiện của nó và một generation
(`crates/linch-ext/src/workflow/agent.rs:138`). Sau một sự cố, lần
`run_workflow` kế tiếp tiếp tục run đó thay vì bắt đầu lại, nên một lời gọi model không
bao giờ bị trả phí hai lần. Một run đã thất bại hoặc bị abort không bao giờ được tiếp
tục: lời gọi thất bại, và lần resume kế tiếp bắt đầu một run mới với generation tiếp
theo. Một run dừng để chờ phê duyệt làm lời gọi thất bại; hãy approve nó bằng
`Agent::approve`, rồi resume workflow, thứ đọc văn bản của run đã xong. Timeout và retry
của step không áp dụng cho lời gọi agent; deadline và cancel token của workflow thì có
(`agent.rs:17-45`). Khi run đã kết thúc (thành công với văn bản đã ghi journal, thất bại
hoặc bị abort), lời gọi đóng session của nó bằng
`Agent::close_session`, thứ giải phóng những gì agent giữ cho session đó
trong process này, chẳng hạn các tệp bộ nhớ và các subagent worker được giữ lại; một run
đang chờ phê duyệt giữ session của nó cho lần approve (`agent.rs:117-130`).

Một interrupt chưa có câu trả lời trả về `WorkflowError::Suspended`;
`run_workflow` khi đó lưu workflow với trạng thái `awaiting_input` cùng câu hỏi,
kể cả khi hàm đã bỏ qua lỗi đó. Các câu trả lời nằm trong
`WorkflowOptions::resume` theo khóa, mỗi câu dùng một lần, nên các interrupt trong một
vòng lặp cần khóa khác nhau (`crates/linch-ext/src/workflow/interrupt.rs:14-40`).

### Kết thúc

Khi kết thúc, `run_workflow` ghi trạng thái: `awaiting_input` kèm
interrupt, nếu không thì `completed` hoặc `failed`. Mất lease thì không ghi gì, vì
một process khác sở hữu workflow (`engine.rs:75-82`). `deadline` bỏ hàm đi
và làm run thất bại với `Timeout`; các step đã xong vẫn nằm trong journal.
`cancel` dừng workflow ở lời gọi kế tiếp sẽ chạy công việc thật, trong khi
các lần phát lại vẫn trả về. Drop future là một sự cố: trạng thái vẫn là
`running`.

### Journal store và lease

`JournalStore` (`journal.rs:123`) có `claim`, `load`, `append` và
`set_status`. Lease theo mô hình của lõi: `claim` luôn thắng, tạo
workflow khi nó mới, và cộng 1 vào epoch; mọi lần ghi dưới epoch cũ hơn
thất bại với `LeaseLost`. Không có hết hạn và không có heartbeat, nên hãy giữ một
runner cho mỗi workflow, ví dụ bằng một job queue. Mọi trạng thái đều có thể được claim
lại: một workflow thất bại resume từ journal của nó và một workflow đã hoàn thành phát
lại đến cuối (`journal.rs:44-59`). Một workflow đã suspend không giữ process nào; bất kỳ
process nào cũng có thể đọc câu hỏi của nó bằng `load`.

| Store | Ghi chú |
|---|---|
| `MemoryJournalStore` | Test và phát triển |
| `PgJournalStore` (feature `postgres`) | Các bảng `linch_ext_workflows` và `linch_ext_workflow_records`; các migration được ghi trong `linch_ext_migrations` (`crates/linch-ext/src/postgres.rs:14`). Mỗi lần ghi là một câu lệnh; một lần append record khóa hàng workflow `FOR SHARE` dưới epoch của lease, nên nó được ghi trước một lần claim cạnh tranh hoặc không ghi gì (`crates/linch-ext/src/workflow/postgres.rs:1-7`). Các lần ghi chạy ở mức cô lập mặc định của pool; trên `READ COMMITTED`, một lần ghi đã chờ một claim sẽ thất bại với lỗi serialization (`40001`) khi claim commit, không ghi gì, và chạy lại, tối đa ba lần thử, nên một claim vẫn thắng và một lần ghi bị fence là `LeaseLost` (`postgres.rs:45-74`). JSON là `TEXT` để một giá trị được phát lại đọc ra chính xác |

Mọi store đều đạt `workflow::conformance::journal_store_suite`
(`crates/linch-ext/src/workflow/conformance.rs:25`).

## Ví dụ

Đoạn này biên dịch và chạy được ở revision này. Nó chấm điểm một eval case, báo cáo về
run của nó, và chạy một workflow suspend ở một interrupt rồi hoàn tất
từ journal của nó; writer được script chỉ có một lượt, nên lời gọi agent thứ hai
sẽ thất bại:

```rust
use std::sync::Arc;

use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, ScriptedProvider, ScriptedTurn,
};
use linch_ext::evals::{EvalCase, EvalSuite, Scorer, run_eval};
use linch_ext::reports::build_run_report;
use linch_ext::workflow::{
    MemoryJournalStore, WorkflowContext, WorkflowError, WorkflowOptions, run_workflow,
};

/// An offline agent that answers from `turns`, on memory stores.
fn agent(turns: impl IntoIterator<Item = ScriptedTurn>) -> Agent {
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    AgentBuilder::new(AgentSpec::new("m"))
        .provider(Arc::new(ScriptedProvider::new(turns)))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .build()
        .unwrap()
}

/// Draft with the agent, then ask a human before publishing.
async fn publish(wf: WorkflowContext, writer: Agent) -> Result<String, WorkflowError> {
    wf.phase("Draft");
    let draft = wf.agent(&writer, "writer", "Draft the release note.").await?;
    let approved: bool = wf.interrupt("approve", &draft).await?;
    let id = wf.step("publish", || async { Ok::<_, std::io::Error>(42_u64) }).await?;
    Ok(if approved { format!("published #{id}") } else { "held".to_owned() })
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // Evals: one case, scored by the run's text and its ending.
    let suite = EvalSuite::from_json(
        r#"{"name": "capitals",
            "cases": [{"prompt": "Capital of France?", "expected": "Paris"}],
            "scorers": [{"type": "text_contains"}, {"type": "run_completed"}]}"#,
    )
    .unwrap();
    let capitals = agent([TurnBuilder::new("m").text("It is Paris.").build()]);
    let result = run_eval(&capitals, &suite.cases, &suite.scorers).await;
    assert_eq!(result.pass_rate(), 1.0);
    let scorer = Scorer::ToolCalled { tool: "search".into() };
    assert_eq!(scorer.score(&EvalCase::new("q", ""), "", &[]), Some(false));

    // Reports: any envelope list folds into one report.
    let report = build_run_report(&result.cases[0].events, None);
    assert_eq!(report.status, "completed");
    assert!(report.to_markdown().contains("completed"));

    // Workflows: the first call suspends at the interrupt; the second
    // replays the agent call from the journal and finishes.
    let journal = Arc::new(MemoryJournalStore::default());
    let writer = agent([TurnBuilder::new("m").text("v1.2 is out.").build()]);
    let options = WorkflowOptions::new(journal.clone(), "docs", "release-1.2");
    let first = run_workflow(options, |wf| publish(wf, writer.clone())).await;
    assert!(matches!(first, Err(WorkflowError::Suspended(question)) if question.key == "approve"));
    let mut options = WorkflowOptions::new(journal, "docs", "release-1.2");
    options.resume.insert("approve".into(), true.into());
    let done = run_workflow(options, |wf| publish(wf, writer.clone())).await.unwrap();
    assert_eq!(done, "published #42");
}
```

Để có journal bền vững, hãy bật feature `postgres`, chạy
`linch_ext::migrate(&pool)` một lần lúc khởi động, và truyền
`Arc::new(PgJournalStore::new(pool))` làm journal.

---

Quay lại [mục lục kiến trúc](./README.md).
