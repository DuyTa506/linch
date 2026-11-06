# Extensions: evals, reports and workflows

> Part of the [linch-rust architecture guide](./README.md).

`linch-ext` (`crates/linch-ext/src/`) holds three extensions built only on the
public `linch-core` API: the eval harness, run reports, and journaled
workflows. Every run they make goes through `Agent::run`, so they work with
any provider and store; for offline, deterministic use, bind the agent to a
`ScriptedProvider` and the memory stores (`crates/linch-ext/src/lib.rs:10-12`).
The `postgres` feature adds the durable workflow journal and its `migrate`.

Python Linch keeps the same split
([extensions](../../../docs/architecture/subsystems.md#311-extensions-and-host-owned-boundaries)).
The workflow journal differs most; see [workflows](#workflows).

## Evals

`run_eval(agent, cases, scorers)` (`crates/linch-ext/src/evals/harness.rs:196`)
runs the cases one after another, each on a fresh session id that
`Agent::close_session` releases when the run ends, together with the session
of every child run its `Subagent` calls made. A run that suspends for approval
is left suspended. Because the cases run in order, a `ScriptedProvider`
consumes its turns in case order; a subagent's child run takes the turns
between its parent's `Subagent` call and the parent's next turn.

A case passes when its run records no error and no scorer returns
`Some(false)`; `None` is an unknown verdict and does not fail it
(`crates/linch-ext/src/evals.rs:10-11`). `CaseResult` keeps the output, the
verdicts, the run's envelopes and the error; `EvalResult` adds `total`,
`passed`, `pass_rate`, `to_value` (JSON) and `to_markdown`.

### Scorers

`Scorer` (`crates/linch-ext/src/evals/scorers.rs:23`) is a closed enum whose
serde form is the suite file's entry: `{"type": "<snake_case>", ...fields}`,
unknown types and fields rejected.

| `type` | Verdict |
|---|---|
| `text_contains` | The output contains `substring` (default `{expected}`, replaced by the case's expected answer), ignoring case |
| `tool_called` | The run started a call of `tool` |
| `schema_valid` | The output is JSON that `schema` accepts; output that is not JSON, or an invalid schema, fails |
| `context_selected_tool` | A `context_build` event offered `tool` |
| `context_not_trimmed` | No `context_build` event reports `budget.trimmed: true` |
| `context_metadata_contains` | A `context_build` event's metadata holds `key` (with value `expected` when set; numbers compare by value, as Python's `==`) |
| `recovery_succeeded` | A failed call of `tool` (any tool when unset) was followed by a successful one; unknown when no call ended |
| `run_completed` | The run ended with a successful `result`; unknown when it has none |

The `context_*` scorers return `None` only for a run with no `context_build`
event. The Rust runner stages one before every dispatch, with `selected_tools`
set and `budget` and `metadata` empty
(`crates/linch-core/src/runner/hooks.rs:161-170`). So on a Rust run
`context_selected_tool` reports whether the tool was offered,
`context_not_trimmed` always passes, and `context_metadata_contains` always
fails.

### Suites and benchmarks

`EvalSuite` (`crates/linch-ext/src/evals/suite.rs:32`) is one JSON file:

```json
{
  "name": "capitals",
  "cases": [{"prompt": "Capital of France?", "expected": "Paris"}],
  "scorers": [{"type": "text_contains"}, {"type": "run_completed"}]
}
```

`cases` is required; a case's `expected` and `metadata`, and the suite's
`name`, `scorers` and `metadata`, may be left out. `EvalSuite::from_json`
rejects unknown fields and empty prompts, substrings, tools and metadata keys.
`load_eval_suite(path)` (`suite.rs:98`) reads a file and uses its stem when
the name is empty. `run_eval_benchmark(suite, targets)`
(`crates/linch-ext/src/evals/benchmark.rs:153`) runs one suite against several
named agents in turn, timing each, and renders the comparison as JSON or
Markdown.

## Reports

`build_run_report(events, record)` (`crates/linch-ext/src/reports.rs:113`)
folds any envelope list into a `RunReport`: a live stream's envelopes, an eval
case's, or a store's. Building a report never fails.
`load_run_report(runs, namespace, run_id)` (`reports.rs:212`) reads the record
and the events from a `RunStore`, leaving out the journal-only model-call
deliveries and receipts, as `Agent::events` does.

`RunReport` (`reports.rs:36-73`) holds the run and session ids, the status, the
checkpoint phase, every ended tool call, permission requests, `context_build`
and `loop_guard` events, error payloads, the last `usage`, the `result`, the
whole timeline, and two summaries:

- `ReportSummary` (`crates/linch-ext/src/reports/summary.rs:14`): event counts,
  usage, tool timings and failures, context, recovery, prompt cache and risk
  figures;
- `LongRunSummary`: context growth, quality and resume state.

Without a record, the status is inferred from the events: `unknown`, then
`running` after `init`, then the last `suspended` event's status, until a
`result` sets the final one. Its serde form is the JSON report;
`to_markdown()` renders it for people (`crates/linch-ext/src/reports/markdown.rs:11`).

## Workflows

A workflow is host code that owns the control flow; every finished call is
journaled, so calling `run_workflow` again with the same workflow id replays
it (`crates/linch-ext/src/workflow.rs:1-11`). Python journals workflow events
in the run store and folds them on resume. Rust keeps a separate
`JournalStore` that writes only finished calls, so the records are the replay
cache and need no fold (`crates/linch-ext/src/workflow/journal.rs:1-8`).

### Calls

`run_workflow(options, f)` (`crates/linch-ext/src/workflow/engine.rs:99`)
claims the workflow, then runs `f` on a `WorkflowContext` seeded with the
stored records. The journaled calls:

| Call | Journaled value | On resume |
|---|---|---|
| `step(name, f)` / `step_with(name, StepOptions, f)` | `f`'s value, through JSON | Replayed without calling `f` |
| `agent(&agent, name, prompt)` | The run's final text | Replayed; an unfinished run is continued |
| `interrupt(key, &payload)` | The answer | Replayed without asking again |

Everything else in the function runs again on every resume. `parallel` and
`settled` fan branches out, at most `max_concurrency` at once per call
(default 4): `parallel` fails fast and drops the other branches, `settled`
returns every outcome (`crates/linch-ext/src/workflow/fanout.rs:32`, `fanout.rs:56`).
Nothing is spawned; every branch runs in the caller's task. `phase(title)`
emits a progress event. Every call emits a `WorkflowEvent` to the optional
`on_event` sink, with a kind of `phase`, `step_start`, `step_end`,
`step_replayed`, `agent_start`, `agent_end`, `agent_replayed`,
`interrupt_requested`, `interrupt_resolved` or `interrupt_replayed`
(`crates/linch-core/src/events/kinds/skills.rs:50`).

### Replay rule

A call is identified by a content key plus its occurrence: how many calls with
that key came before it in this run (`workflow.rs:43-51`). Keys are
domain-separated digests: `step_key` of the name and the optional input key,
`agent_key` of the name and the prompt, `interrupt_key` of the key alone, so a
payload may change across resumes without losing the answer
(`journal.rs:161-193`). The function must take the same path again: branching
on a journaled value is fine, branching on the clock, randomness or the
environment is not unless the value comes from a step.

A step's value is durable before `step` returns. A process that crashes after
`f` ran but before the record landed runs `f` again on resume, so side effects
should be idempotent (`crates/linch-ext/src/workflow/step.rs:47-54`). With a
`Retry` policy (`crates/linch-ext/src/workflow/policy.rs:18`), only the winning
attempt is journaled. Step errors and timeouts are retried; encode errors and
a cancelled workflow are not.

An `agent` call runs on its own session with a run id derived from the
workflow, the call's key, its occurrence and a generation
(`crates/linch-ext/src/workflow/agent.rs:121`). After a crash, the next
`run_workflow` continues that run instead of starting over, so a model call is
never paid twice. A run that failed or was aborted is never continued: the
call fails, and the next resume starts a fresh run with the next generation.
A run that stops for approval fails the call; approve it with `Agent::approve`,
then resume the workflow, which reads the finished run's text. Step timeouts
and retries do not apply to an agent call; the workflow deadline and cancel
token do (`agent.rs:17-37`).

An interrupt without an answer returns `WorkflowError::Suspended`;
`run_workflow` then stores the workflow as `awaiting_input` with the question,
even if the function dropped that error. Answers go in
`WorkflowOptions::resume` by key, each used once, so interrupts in a loop need
distinct keys (`crates/linch-ext/src/workflow/interrupt.rs:14-40`).

### Ending

At the end `run_workflow` writes the status: `awaiting_input` with the
interrupt, else `completed` or `failed`. A lost lease writes nothing, since
another process owns the workflow (`engine.rs:75-82`). `deadline` drops the
function and fails the run with `Timeout`; finished steps stay journaled.
`cancel` stops the workflow at its next call that would run live work, while
replays still return. Dropping the future is a crash: the status stays
`running`.

### Journal stores and leases

`JournalStore` (`journal.rs:120`) has `claim`, `load`, `append` and
`set_status`. Leases follow the core's model: `claim` always wins, creates the
workflow when new, and adds 1 to the epoch; every write under an older epoch
fails with `LeaseLost`. There is no expiry and no heartbeat, so keep one
runner per workflow, for example with a job queue. Any status can be claimed
again: a failed workflow resumes from its journal and a completed one replays
to the end (`journal.rs:41-56`). A suspended workflow holds no process; any
process can read its question with `load`.

| Store | Notes |
|---|---|
| `MemoryJournalStore` | Tests and development |
| `PgJournalStore` (feature `postgres`) | Tables `linch_ext_workflows` and `linch_ext_workflow_records`; migrations recorded in `linch_ext_migrations` (`crates/linch-ext/src/postgres.rs:14`). Every write is one statement; a record append locks the workflow row `FOR SHARE` under the lease's epoch, so it lands before a competing claim or writes nothing (`crates/linch-ext/src/workflow/postgres.rs:1-7`). Writes run at the pool's default isolation; above `READ COMMITTED` a write that waited on a claim fails with a serialization error (`40001`) once the claim commits, wrote nothing, and runs again, up to three tries in all, so a claim still wins and a fenced write is `LeaseLost` (`postgres.rs:45-74`). JSON is `TEXT` so a replayed value reads back exactly |

Every store passes `workflow::conformance::journal_store_suite`
(`crates/linch-ext/src/workflow/conformance.rs:25`).

## Example

This compiles and runs at this revision. It scores one eval case, reports on
its run, and runs a workflow that suspends at an interrupt and then finishes
from its journal; the scripted writer has one turn, so a second agent call
would fail:

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

For a durable journal, enable the `postgres` feature, run
`linch_ext::migrate(&pool)` once at startup, and pass
`Arc::new(PgJournalStore::new(pool))` as the journal.

---

Back to the [architecture index](./README.md).
