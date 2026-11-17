# Changelog

Notable changes to `linch-rust`: the Rust crates under `crates/` and the
`linch-rust` Python client that ships the `linch-server` binary. The Python
package `linch` has its own [changelog](../CHANGELOG.md).

## Unreleased

### Added

- Schedule tools: `schedule_tools: true` gives the model `CreateSchedule` (exec), `ListSchedules` (read) and `CancelSchedule` (write), Python's `register_schedule_tools`. A model-made schedule runs the same stored agent in the calling run's session for its principal, under the tenant's `max_schedules` and `min_schedule_interval_ms` (the `schedule.create` admission); the model lists and cancels only its own agent's schedules, and a subagent cannot create one. Off by default; `agent.open` refuses it without `max_schedules`, on a local connection, or for an agent that cannot run in the background.

- Cancel a suspended run: `Agent::cancel`, `run.cancel` with an `agent` handle, and `Agent.cancel(run_id)` / `Run.cancel()` in Python end a run waiting for an approval or an answer, from any process and across restarts on Postgres; each pending call gets a `Tool call aborted` result so the session stays paired, the run ends `aborted`, and a late approve or answer gets `RunTerminal`.
- Execution backends: `AgentConfig.execution` runs `Bash` (foreground and
  background) through `local` (the default, unchanged), `sandbox` or
  `docker`; the file tools share the backend's workspace. A non-local
  backend is part of the run contract through `Bash`'s description.
- The `sandbox` backend: Landlock (ABI 6, fail closed) confines reads and
  writes to the workspace, a private per-session `HOME`/`TMPDIR` and system
  directories, and scopes signals; a seccomp filter refuses Unix sockets,
  `io_uring` and, with the network off, every socket; `prlimit` caps each
  process's memory, CPU time and file size, and the command's processes.
- The `docker` backend: `docker run --rm` per command with Python Linch's
  limits (2 GiB, 2 CPUs, 512 pids, no capabilities, no new privileges),
  network `none` by default, and `docker rm -f` on timeout, cancel, kill and
  when a background command's lifeline fires.
- Tenant admission of execution backends: `allow.execution` lists the
  backends a tenant's configs may choose, with the operator's policy; a
  config naming none gets the first, so a tenant can be held to a sandbox.
- `LocalExecution`, `SandboxExecution`, `DockerExecution` and `Execution`
  TypedDicts in the Python client.
- Context-length recovery: each adapter maps its provider's refusal of a
  prompt longer than the context window to `ProviderError::ContextLength`
  (error kind `context_length`, never retried); with `spec.compaction` set,
  the run shortens its view and calls again, and without it the run ends.
- Forced compaction: `spec.compaction.max_forced` (3) bounds the summaries a
  run makes for context-length refusals; the `compaction` event has `forced`.
- Micro-compaction: `spec.compaction.micro` clears old tool results with no
  model call, before a summary (strategy `micro`). Python's
  `CompactionLadder` micro rung.
- `DETAILED_PROMPT` (`DETAILED_COMPACTION_PROMPT` in the Python client), for
  `spec.compaction.prompt`. Python's `DetailedCompaction`.
- Truncation recovery: `spec.truncation_recovery` continues a text answer
  the output limit cut off and joins the parts into the final answer.
  Python's `TruncationRecovery`.
- A workspace agent must read a file again after a compaction before it
  edits it (`AgentBuilder::on_compact`). Python's `reset_read_tracker`.
- `linch_rust.memory`: `retrieval` turns any `Retriever` into a
  `context_build` callback for external memory, with
  `examples/external_memory.py` on a folder of Markdown notes.
- `Compaction` and `TruncationRecovery` TypedDicts in the Python client.
- Redaction: `spec.redaction` holds regex rules that scrub assistant
  responses (text and unsigned reasoning, before `after_provider_call` and
  before they are logged), tool results (before `post_tool_use`, and
  `AskUser` answers, background workers' answers and mailbox peer
  messages) and, when asked, the prompt; a hook's rewrite is
  scrubbed again, and `partial_assistant` or `tool_progress` events are not
  streamed while their surface is scrubbed. Python's `RedactionHook`.
- Final-answer verifiers: `spec.verification` holds built-in checks (a
  pattern that must or must not match, keywords that must appear) run after
  the output schema, each failure a `verification` event; a `retry`
  verifier feeds back while `max_retries` (2) lasts, then the answer is
  accepted as `exhausted`, and a `stop` verifier fails the run with a
  `VerificationError`. Python's `FinalAnswerVerifierHook`.
- With `spec.verification`, a `stop` hook's `retry` draws on its
  `max_retries`, durably, as a `verification` event under the hook's policy
  id, instead of one retry per run call: a client callback is a verifier.
- A remote tenant may give at most 32 redaction rules and 32 verifiers, each
  pattern at most 1024 bytes (`NotAllowed`, kinds `redaction` and
  `verification`).
- `Redaction`, `Verification`, `Verifier`, `VerifierCheck` and
  `Permissions` TypedDicts in the Python client.
- Twelve observe-only hook points, for parity with Python's lifecycle hooks:
  `agent_start`, `agent_stop`, `turn_start`, `provider_call_start`,
  `provider_call_stop`, `tool_use_start`, `tool_use_stop`,
  `post_tool_use_failure`, `pre_compact`, `post_compact`, `subagent_start`,
  `subagent_stop`. Each answers `()` (`{}` on the wire), can never fail
  closed, and costs nothing when no hook is registered on it. Available to
  `@callback` in `linch_rust` and to `Hook<P>` implementations in
  `linch-core`.
- `before_provider_call`: a hook point for the full provider request about to
  be sent. A hook may rewrite it (the rewrite is what is sent and what
  `context_build` records) or block the call, ending the run before the
  provider is ever reached. Defaults open, but unlike the observe-only
  points its blocking answer can fail closed. Available to `@callback` in
  `linch_rust` and to `Hook<BeforeProviderCall>` in `linch-core`.
- `after_provider_call`: a hook point for the response a provider call just
  assembled, before it is staged and appended to the session. A hook may
  rewrite it (the rewrite is what gets staged and appended) or block it,
  ending the run before anything is staged. Defaults open, but like
  `before_provider_call` its blocking answer can fail closed. Available to
  `@callback` in `linch_rust` and to `Hook<AfterProviderCall>` in
  `linch-core`.
- `stop` can now rewrite the run's final-answer text, or block the stop
  outright. This is Python's `before_final_answer` hook point, folded into
  `stop` rather than added as a separate point: the rewrite replaces only the
  text the run reports as its result, not the assistant message already
  appended to the session (that message is logged before `stop` ever runs).
  Available as `{"action": "rewrite", "text"}` / `{"action": "block",
  "reason"}` to `@callback` in `linch_rust` and as `StopResponse::Rewrite`/
  `StopResponse::Block` to `Hook<Stop>` in `linch-core`.
- `event_emit`: a thirteenth observe-only hook point, for parity with
  Python's `EVENT_EMIT`. It fires once per event, in delivery order, but
  only for an event that is both streamed to the caller and durably logged:
  never for a hook's own record (it would otherwise recurse into itself)
  and never for a stream-only delta or tool-progress event. Answers `()`
  (`{}` on the wire) and can never fail closed. Available to `@callback` in
  `linch_rust` and to `Hook<EventEmit>` in `linch-core`.
- `after_provider_call` can retry, as Python's `retry`/`force_continue` there: the response is kept and logged, none of its tool calls run, and the feedback goes to the model (in a `<system-reminder>`, or as the first call's error result) before another turn; `{"action": "retry"|"force_continue", "feedback"}` on the wire, `AfterProviderCallResponse::Retry` in `linch-core`.
- `stop` can retry the final answer once per run call, as Python's `before_final_answer` `retry` (`_MAX_FINAL_ANSWER_REENTRIES`); a spent retry counts as `allow`. `{"action": "retry", "feedback"}` on the wire, `StopResponse::Retry` in `linch-core`.
- `stop` accepts `{"action": "force_continue", "feedback"}`, Python's name for its `continue`.
- `before_provider_call` accepts `force_continue`, as Python's there: the call is skipped, the feedback goes to the model in a `<system-reminder>`, durable before the next turn, and the run takes another turn, bounded only by `max_turns`; `{"action": "force_continue", "feedback"}` on the wire, `BeforeProviderCallResponse::ForceContinue` in `linch-core`. `retry` stays refused there.
- `Phase::AssistantReceived` records an `after_provider_call` retry in an optional `retry_feedback`, durable with the paid response; checkpoints without one are unchanged, but a binary older than this one cannot resume a run whose checkpoint has one.
- `spec.provider_retry` retries a provider call that failed with a retryable error (no connection or no response, 408, 429, 5xx) on the same model, after an exponential, jittered wait that honours `Retry-After`: `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "jitter"?}`, on by default with Python's `RetryOptions` (5, 1000, 30000, 0.2); `{"max_attempts": 1}` turns it off. The default stays off the wire, so a spec that does not set it keeps its run contract. Each attempt is its own durable delivery; a crash during the wait resumes with the next attempt and keeps the turn's count. `ProviderRetry` in `linch-core` and the Python client.
- A run that retried or failed over holds `provider_retry` / `model_fallback` events, which a binary older than this one refuses as an unknown event type: it cannot read or resume such a run (the error is explicit; nothing is resumed wrongly). Since retries are on by default, any run that met a retryable failure is one, so roll back only once those runs have ended.
- `ProviderError::Connect`: a connection never made (refused, DNS, TLS handshake, connect timeout), so the request never went out; its error event's `kind` is `connect`, and a scripted turn can fail with `{"type": "connect", "message"}`.
- A call that failed before costing anything (`Connect`, or an error status before any stream event) settles with a zero usage instead of an unknown one: no `usage` event, `usage_complete` stays true, and a capped budget may retry it. A reset, a read timeout or a failure after output began stays unknown and, under a cap, still ends the run.
- `spec.fallback_models` fails a run over to the next model, in order, once a model has no retry left; the run keeps that model, a resumed run included, and its usage and cost name it. Empty by default.
- `provider_retry` and `model_fallback` events report each retry (`model`, `retry`, `delay_ms`, `error`) and each failover (`from_model`, `to_model`, `error`); the Python client decodes them as `ProviderRetryEvent` and `ModelFallbackEvent`.
- Structured output: `spec.output_schema` (`{name, schema, strict?, description?}`) makes the final answer a JSON object checked against the schema (raw or in a fenced block); the parsed value is `result.structured_output`. `OutputSchema` in `linch_rust` and `linch-core`.
- `spec.structured_output_retries` (default 0) sends a failing answer back to the model with Python's feedback wording, recorded as a `verification` event (`verifier: "output_schema"`, `action: "retry"`); the count is read back on resume, so a crash never grants more.
- Past `structured_output_retries` the run fails: `verification` with `action: "stop"`, an `error` named `StructuredOutputError`, and a `result` of subtype `error` with `structured_error`.
- `stop` runs only on an answer that satisfies the schema and gets the parsed `structured_output` in its request; a `rewrite` is checked again and one that fails the schema fails the run.
- `Agent.run(output_schema=...)`, `run.start` `output_schema` and `RunOptions::output_schema` give one run its own schema; it is stored on the run's first `init` (`InitEvent.output_schema`) so a resume or approve keeps it.
- `InvalidOutputSchema` (code -32031, `RunError::InvalidOutputSchema`): a run's own schema does not compile or its root is not an object; nothing is written.
- `InitEvent.output_schema`: only the first `init` of a run started with its own schema has it; other events are unchanged, but a binary older than this one cannot read the events of a run whose `init` has one.
- Anthropic, OpenAI Chat, OpenAI Responses and Gemini send the output schema in their native structured-output form (`output_config.format`, `response_format`, `text.format`, `responseMimeType` with `responseJsonSchema`); no forced final tool. The shapes follow each vendor's API docs and have not been run against a live API. A fallback model (`spec.fallback_models`) is sent the same schema and its answers are checked the same way.
- `AskUser` (config `ask_user: true`, off by default, as Python's opt-in `AskUserPlugin`): 1 to 4 questions with 2 to 4 options each; a call suspends the run with an `input_request` event (its `questions`) and a `suspended` event of status `awaiting_input`. A subagent's call gets an error instead.
- `Agent.answer(run_id, {tool_use_id: Answer(...)})`, `run.answer` and `Agent::answer` commit each answer as its call's result and continue the run; a request left out is asked again, `Answer.declined(note)` declines. The answer is durable before the run goes on, so a crash never asks twice or answers twice.
- `InvalidAnswer` (code -32032, `RunError::InvalidAnswer`): an answer that names no pending request, misses a question or answers a single-select question with a list; nothing is written.
- `Phase::AwaitingInput` and `InputRequestEvent.questions` are new in the journal; other checkpoints and events are unchanged, but a binary older than this one cannot resume a run waiting for input.
- `Bash` takes `run_in_background`: the command runs on after the call returns its `process_id`; `BashOutput` (read scope) returns what it printed since the last read, waiting up to `wait_ms` for it to end, and `BashKill` (exec scope) kills its process group. At most 4 run at once per session; each is killed after `timeout_ms` (30 minutes by default), when its session closes, when its agent closes and when the server dies, even by SIGKILL.
- A background process does not survive a server restart: `BashOutput` and `BashKill` on an id from before it fail with `no_such_process`, saying so.
- Run leases on Postgres: one server drives a run at a time. Another agent's `resume`, `approve` or `answer` of a run that is driven elsewhere ends with `RunLeased` (code -32033, `StoreError::RunLeased`, `data: {run_id, owner, expires_in_ms}`) and writes nothing. A crashed server's runs are free once their lease runs out (30 s by default; `stores.lease_ttl_ms`, at least 1000, or `PgRunStore::with_lease_ttl`). The driver renews the lease on a timer, one small `UPDATE` per running run every third of the lease, so a long model call keeps it. Memory stores have no leases and are unchanged.
- `RunStore::seize`, `lease_ttl`, `renew` and `release`, defaulted for a store whose claims never expire, and `store::conformance::run_lease_suite` for one whose claims do. A wrapper store must forward them.
- `LeaseLost` (-32022) and `RunLeased` (-32033) in the Python client, both `RunError`s.
- `Agent::released` waits for the lease releases of runs that were dropped; `OpenedAgent::close` waits for them (at most `RELEASE_WAIT`, 5 s), so a `linch-server` that exits normally frees the runs it was driving at once.
- A workflow `agent` call whose run is still leased to a runner that crashed waits until the lease runs out (or the workflow is cancelled) instead of failing.
- Local-server cache usage reads Python's fields: SGLang's top-level
  `cached_tokens` without `prompt_tokens_details`, llama.cpp's
  `cache_read_tokens` and `prompt_cache_hit_tokens` (details or `usage`) and
  `timings.cache_n` when usage reports no cache hit.
- `pricing` in the agent config: rates per model id (`input`, `output`, and
  `cache_read` and `cache_creation`, which default to `input`) that this agent
  prices a model with, replacing the published table for it or adding a model
  the table lacks, such as one a local server serves. Without it a model the
  table does not list costs `None`, as in Python.
- Eval scorer `cost_under` (`budget_usd`): passes when the run's total cost is
  below the budget, unknown when it has none. Python's `cost_under`.
- Eval results report each case's `run_id`, `total_cost_usd`, `usage` and
  `duration_ms` and the same totals for the whole eval; the Markdown report has
  a Cost USD column, as Python's does.
- RPC `eval.run`: runs cases with built-in scorers on an open agent and
  returns the per-case and total pass rate, cost, tokens and duration plus a
  Markdown report; each case's `run_id` is a stored run. It takes a place among
  a tenant's live runs and refuses `agent.close` while it runs.
- RPC `run.report`: the run report (tool calls and timings, usage, context,
  recovery, risk) of a stored run as JSON and Markdown, from `Agent::run_record`
  and its events.
- Python client: `Agent.eval(cases, scorers, include_events=)` and
  `Agent.report(run_id, timeline=)` with the `EvalCase`, `Scorer`, `CaseResult`,
  `EvalReport`, `RunReport` and `RunReportResult` typed dicts; a scorer may also be
  a Python callable, applied to each case result once the cases are done.
- Scheduler: `schedule.create`, `schedule.list`, `schedule.pause`, `schedule.resume` and `schedule.delete` store schedules on an open agent's Postgres database: a cron pattern (five or six fields) in an IANA time zone, daylight saving included, or an interval. Each fire is queued exactly once across any number of workers, and missed fires are caught up in order, as Python Linch's are. Python: `Agent.create_schedule`, `schedules`, `pause_schedule`, `resume_schedule`, `delete_schedule`.
- Heartbeats: a schedule with a `session_id` runs each fire as a new run in that session and skips a fire (counted in `skips`) while its previous fire's job is unfinished or the session has a live run, one suspended for approval or input included. Python: `Agent.heartbeat`, prompted with `CHECK_IN` by default.
- Background runs: `job.enqueue` queues a one-off run; `job.get` and `job.list` report each job's state (`queued`, `running`, `retrying`, `failed`, `done`, `superseded`), attempts, error and run id, for running and ended jobs. Python: `Agent.enqueue`, `job`, `jobs`, `wait_job`, and the `Schedule` and `Job` dicts.
- An agent with memory stores, host tools or callbacks cannot be scheduled or queued: -32040 (`NotBackground`, `data.reason`). Also -32041 `ScheduleNotFound`, -32042 `ScheduleExists`, -32043 `JobNotFound`, -32044 `InvalidSchedule`, all `SchedulerError`s in Python.
- `linch-server --config` takes a `worker` section: the server fires schedules and runs queued jobs on that database, opening each job's stored agent config and checking it against its tenant's allowlists again at every attempt; each background run takes one of the tenant's `max_runs` places. A config may have only a worker, whose tenants need no token. Without the section nothing polls.
- Tenant allowlists take `max_schedules` and `max_queued_jobs` (absent: none allowed, `NotAllowed` kinds `schedules` and `background_runs`; at the cap `LimitReached` kinds `schedules` and `jobs`, counted across servers) and `min_schedule_interval_ms` (60000 unless set; `NotAllowed` kind `schedule_interval`).
- A schedule's fires and a queued run act for a principal: `schedule.create` and `job.enqueue` take `principal`, admitted as for `run.start` (a tenant's must name the tenant), stored with the schedule or job, and given to every start, resume and approve of the background run, so a permission rule with `role` or `unless_role` holds there as it does for a run started directly. Python: `principal=` on `create_schedule`, `heartbeat` and `enqueue`; Python Linch's `RunRequest.principal`.
- A stored agent definition (which holds the config's MCP env and header values) is deleted once no schedule and no unfinished job names it: `linch_worker::prune_definitions`, which a worker runs with the history pruning, from its first poll and every 10 minutes after.
- `linch-worker`: `Timing` (cron in a time zone, or an interval), `create_schedule` with an optional `Cap`, `get_schedule`, `pause_schedule`, `resume_schedule`, `delete_schedule`, `enqueue` with a stored agent definition, `AgentFactory` and `Worker::definitions`, `job`, `jobs` and `prune_history`, and `WorkerConfig::history_retention` (7 days). A job that ends moves to `linch_worker_history` instead of being deleted.

### Changed

- The permission policy joins the run contract when it is not the default (a mode other than `default`, or any layers, `auto_allow`, path or command fields; the facade's `tasks` and a profile's denied tools add to it), so a run is never resumed or approved under a policy that allows more, or less, than the one it started with (`ContractMismatch`).
- A run cancelled while it plans its tool calls (a hook holding it, or an approve or answer in flight) answers each call with `Tool call aborted` before it ends, instead of leaving the calls unanswered in the session.
- A binary older than this one cannot resume a run a cancel left half done (the `aborting` checkpoint phase); finish such a run with `cancel` or `resume` before rolling back.
- A provider's refusal of a prompt longer than the context window ends a run without `spec.compaction` as `kind` `context_length` with the message `prompt exceeds the model's context window: ...`, where it was `kind` `status` with `provider returned HTTP 400: ...`; `retryable` stays false.
- A binary older than this one cannot resume a run of an agent with `spec.compaction.micro` that crashed during a micro-compaction (the `cleared` checkpoint phase), nor read the events of a run that made a forced compaction (`forced: true`; events refuse unknown fields). Roll back only once those runs have ended.
- `ProviderError::retryable()` is true for an HTTP 408, as Python treats a request timeout.
- With an `after_provider_call` hook attached, an accepted call's receipt and `usage` event are written before the hooks run (`provider_call_stop` included), with `ProviderPending` at the totals that count them; the response is written after them, as before. A crash while such a hook runs no longer loses the paid usage: the resume keeps it in the totals and the budget and calls again under the next delivery attempt, which spends no `provider_retry`. The run costs one more write per accepted call; without an `after_provider_call` hook nothing changes, a `provider_call_stop` observer alone included. `ReconcileError::MissingAssistant` is gone.
- A binary older than this one refuses to resume a run that crashed between such a receipt and its response (an accepted receipt with no `assistant` event, under `provider_pending`): it fails with `MissingAssistant` and resumes nothing wrongly, but an older `linch-worker` marks such a run `failed` (it fails a run on any reconcile error). Roll back only once those runs have been resumed or ended.
- A call that failed with a 500, 502 or 504 before any stream event now settles with an unknown usage instead of a zero one: a gateway can send those after the upstream model already ran and billed. Only a connection never made, a 4xx (408 and 429 included), a 503 or a 529 before any event counts as costing nothing; any other status is unknown too. Under a capped `budget` such a failure is no longer retried, and `usage_complete` is false.

- `stop` feedback (`continue`) reaches the model wrapped in `<system-reminder>`, as Python's does, instead of as bare text.
- A workspace agent now also has `BashOutput` and `BashKill` (`bash: false` leaves all three out), so its run contract changes: a run started under an older binary cannot be resumed.
- `agent.close_session` and `session.close` also kill the session's background `Bash` commands.
- DeepSeek and the vLLM, SGLang and llama.cpp profiles still refuse an output schema, now saying why: no native JSON-schema response format that carries the schema.
- A Postgres resume of a run another server drives is refused (`RunLeased`) instead of taking the run over and paying for its in-flight model call again; it no longer fences the live driver out. `Agent::fail` and a parent resuming its subagents' runs still take a run over, and the driver they fence stops at its next heartbeat with `LeaseLost`.
- A journal write fenced by another claim answers -32022 (`LeaseLost`) on the RPC, as a fenced claim does, instead of -32024.
- A run whose stream ends with an error or is dropped releases its Postgres lease (one extra write), so another server can resume it at once; a run that ends or suspends writes nothing more.
- A worker job whose run another process leases waits like a busy session (released with back-off, not counting a failure).
- `store::conformance::run_lease_suite` takes two pairs of handles, `(short, long)`: the steps that expect a live lease without renewing it use the long TTL, so a pause on a loaded machine no longer lets their lease run out; only the renewal and expiry steps wait out the short one.

- An intercept callback without `timeout_ms` (Python `timeout`) no longer times out
  after 30 seconds: it waits until the client answers, the run is cancelled or the
  connection ends, as Python's hooks do. Set `timeout` to bound it.
- `@tool` and `@callback` accept plain functions as well as `async` ones, as Python's tools and
  hooks do; a plain function runs in a worker thread.
- `Edit` accepts an empty `old_string`, as Python's does, instead of refusing it as invalid input: an empty file takes `new_string`; a non-empty file is refused as ambiguous unless `replace_all` is set, which inserts at every position and needs the whole file read.
- `Edit` and `Write` on a file changed since it was read now say `Read this file again before editing it.` / `Read this file again before writing it.` (were `<path> changed since it was last read; Read it again.`), and `Read` of a file that changed mid-read says `File changed while reading; Read this file again.`, the words Python's tools use. The error codes are unchanged.

### Upgrading

- Runs started before this release by an agent whose permission policy is not the default (including an agent with `tasks` or a profile that denies tools) can no longer be resumed or approved under the new binary: the policy is now in the run contract, so they end with `ContractMismatch`. Let them finish on the old binary, or end them with `cancel`, which does not check the contract. Runs of agents with the default policy keep their contracts.

- Postgres migration 6 adds `linch_runs.lease_holder` and `lease_until` (nullable, no rewrite). As with every migration, a server of an earlier version then refuses to start on that database, and a running one ignores leases: until every server is upgraded, an old one can still take over a run a new one drives (the epoch still fences the loser's writes).

- Worker migrations 5 and 6 (`crates/linch-worker/migrations/0005_scheduler.sql`, `0006_background_principal.sql`) run when a worker, or the first scheduler call on a database, calls `linch_worker::migrate`. Migration 5 adds `linch_worker_agents` and `linch_worker_history` and new nullable or defaulted columns on schedules and jobs (`cron` and `session_id` become nullable); migration 6 adds `principal` to schedules and jobs and two partial indexes on their `agent` column. None rewrites a table. Existing schedules keep their cron pattern, read in UTC, and their fixed session. As with every migration, a worker of an earlier version then refuses to start on that database; one already running does not know the new columns, and one with its own agent for a namespace may take a job queued with a stored definition and run it with that agent, without the job's principal. Upgrade every worker on a database before scheduling there through `linch-server`.
- A job that ends now moves to `linch_worker_history` instead of being deleted: code that counted `linch_worker_jobs` rows to see finished work should read `job` or `jobs` (or `linch_worker_history`) instead.
- `linch_worker` API breaks, for code that uses the crate directly:
  - `cancel_schedule` is gone: use `pause_schedule` (keeps the schedule) or `delete_schedule`.
  - `Schedule` has `timing` (`Timing::Cron { pattern, timezone }` or `Timing::Every(Duration)`) instead of `cron: String`; `session_id` is an `Option<SessionId>` (`None`: each fire starts its own session); it gains `agent` and `principal`, so a struct literal must set them (`None` keeps the old behaviour).
  - `RunRequest` gains `agent` and `principal`.
  - `create_schedule(pool, &schedule, cap)` takes a cap (`None` for none) and returns the stored `ScheduleInfo` instead of the first fire time.
- `store::conformance::run_lease_suite(a, b)` is now `run_lease_suite((a, b), (long_a, long_b))`: give a second pair of handles whose lease TTL outlasts any pause (60 s for Postgres).

- Runs of workspace agents started before this release cannot be resumed: the workspace tool list gained `BashOutput` and `BashKill`, so the run contract changed and a resume ends with `ContractMismatch`. Let those runs finish on the old binary, or start new runs.

## 0.1.1 - 2026-11-07

### Fixed

- The task tools (`TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`) now belong
  to the session of the root run of the agent tree. A subagent's worker used to
  see an empty list and could not update the coordinator's tasks; it now reads
  and updates them. Unrelated sessions stay apart.
- `TaskStop` on a worker that already finished no longer says it stopped it. The
  reply says the worker already finished and how it ended (`completed`, `failed`
  or `killed`); the worker's status is unchanged, as before.

### Changed

- `linch_tools::task_tools` takes the run store as a second argument:
  `task_tools(store, runs, namespace)`. `TaskCreate::new`, `TaskList::new`,
  `TaskGet::new` and `TaskUpdate::new` take it too, as `new(store, runs,
  namespace)`. Hosts that build the tools themselves pass their `RunStore`; a
  call whose run the store does not know uses its own session.

### Tests

- New tests pin that none of the Python bugs #99 to #108 is present in the Rust
  port (tool scope, file namespaces, secrets kept out of argv, PreToolUse
  actions, Grep cancellation, offload, the roster, worker status and the child
  tool filter).
