//! [`execute`]: run a decided plan.

use std::any::Any;
use std::convert::Infallible;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::pin::pin;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::FutureExt;
use futures::future::{self, Either};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use crate::canonical::to_canonical_string;
use crate::events::Event;
use crate::hooks::{HookRecord, Hooks, PostToolUseFailureRequest, PostToolUseRequest, ToolCall};
use crate::json::JsonValue;
use crate::message::ToolResultBlock;
use crate::redaction::Redactor;
use crate::registry::{OutputContractError, RegisteredTool};
use crate::tool::{
    Report, ToolContext, ToolOutput, ToolOutputError, ToolProgress, ToolResult, ToolScope,
};
use crate::tool_cache::{ToolCacheHandle, cache_key};
use crate::tool_retry::{TIMEOUT_CLASS, ToolRetry};

use super::RunIdentity;
use super::plan::{BatchPlan, PlannedAction, PlannedCall};

/// Model-facing text of a call the run abort stopped or never started.
pub(crate) const ABORTED: &str = "Tool call aborted";

/// Receives what [`execute`] does, so the run loop can make it durable and
/// emit events.
///
/// Calls for one read group overlap: the sink must accept concurrent calls.
#[async_trait]
pub trait ToolSink: Send + Sync {
    /// Error that stops the batch, such as a lost durability lease.
    type Error: Send;

    /// Called before a tool runs, only for [`PlannedAction::Run`] calls.
    ///
    /// This is the durability barrier: the tool starts only after it
    /// returns. An error stops the batch: this tool and every later one do
    /// not run, tools already running in the same group are cancelled, and
    /// [`execute`] returns the error.
    async fn tool_starting(&self, call: &PlannedCall) -> Result<(), Self::Error>;

    /// Called before a running call's next attempt, after an attempt
    /// failed with an error [`ToolRetry`] retries: the barrier of a retry,
    /// as [`Self::tool_starting`] is of the first attempt. The next attempt
    /// (after its backoff wait) starts only after it returns, and an error
    /// stops the batch the same way. The default does nothing.
    async fn tool_retrying(&self, call: &PlannedCall, retry: &Retrying) -> Result<(), Self::Error> {
        let _ = (call, retry);
        Ok(())
    }

    /// Called exactly once per call with its result, as soon as the result
    /// exists, so within a read group in completion order. Calls that run
    /// no tool (served, refused or aborted) get only this call. An error
    /// stops the batch like one from [`Self::tool_starting`].
    async fn tool_finished(
        &self,
        call: &PlannedCall,
        outcome: &CallOutcome,
    ) -> Result<(), Self::Error>;

    /// A progress report from the call's running tool. Best effort: reports
    /// sent after the tool returned are dropped.
    fn tool_progress(&self, call: &PlannedCall, progress: ToolProgress);

    /// An event the call's running tool put in the run's log (a subagent's
    /// wrapped event), before the call's [`Self::tool_finished`]. Sync, so
    /// taking it never waits: the sink stages it with its next write. The
    /// default drops it.
    fn tool_event(&self, call: &PlannedCall, event: Event) {
        let _ = (call, event);
    }
}

/// A retry about to happen, as [`ToolSink::tool_retrying`] sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Retrying {
    /// The attempt about to run, from 2.
    pub attempt: u32,
    /// Most attempts the policy allows.
    pub max_attempts: u32,
    /// The class of the failure that is retried (the error's code, or
    /// `"timeout"`).
    pub class: String,
    /// The failed attempt's error message.
    pub error: String,
    /// How long the call waits before the attempt.
    pub delay: Duration,
}

/// One call's result, as [`ToolSink::tool_finished`] sees it.
#[derive(Debug, Clone, PartialEq)]
pub struct CallOutcome {
    /// What the model sees. [`execute`] returns these in call order.
    pub block: ToolResultBlock,
    /// The result `block` was rendered from, after `post_tool_use`, with the
    /// output's structured value and metadata.
    pub result: ToolResult,
    /// Time from the tool's start to its final result. Zero for a call that
    /// ran no tool.
    pub duration: Duration,
    /// Set when the tool's success value broke its `output_schema`; the
    /// result became an error before `post_tool_use`, which may replace it.
    pub contract_error: Option<OutputContractError>,
    /// The `post_tool_use` hooks called, in call order.
    pub hook_records: Vec<HookRecord>,
    /// Whether the tool cache served the result: no tool ran.
    pub cache_hit: bool,
}

/// Limits for one [`execute`] call.
#[derive(Debug, Clone)]
pub struct ExecuteOptions {
    /// Most read calls run at once. Zero counts as one.
    pub max_concurrency: usize,
    /// Longest one tool may run before it gets a timeout error result; the
    /// tool sees the end as [`ToolContext::deadline`]. `None` waits for the
    /// tool. With `retry`, the limit is per attempt.
    pub call_timeout: Option<Duration>,
    /// Run abort. Cancelling it cancels running tools, and calls not yet
    /// started get an aborted error result.
    pub cancel: CancellationToken,
    /// Scrubs every result before the `post_tool_use` chain and again
    /// after a hook replaced it; progress reports are then dropped, since
    /// a match can span two of them. `None` keeps results as they are.
    pub redactor: Option<Arc<Redactor>>,
    /// Runs an idempotent tool again after a failure it reports as
    /// transient (see [`crate::tool_retry`]). `None` never retries.
    pub retry: Option<ToolRetry>,
    /// Serves an identical call of a named read tool from the cache, after
    /// the call was planned (see [`crate::tool_cache`]). `None` caches
    /// nothing.
    pub cache: Option<ToolCacheHandle>,
}

/// Run a decided plan and return one result per call, in call order.
///
/// A contiguous run of read calls (see [`crate::Tool::parallel`]) executes
/// concurrently, in groups of at most [`ExecuteOptions::max_concurrency`];
/// any other call runs alone, so a call after it waits for it. Calls that run no tool join a
/// read group. Each tool gets a child of the run's cancel token, cancelled
/// when the call ends. A success value of a run or served call is checked
/// against the tool's `output_schema`, then the `post_tool_use` chain of
/// `hooks` runs on the result (a violation reaches it as an error).
///
/// With `tool_execute` layers registered in `hooks`, each attempt of a
/// tool runs behind them (see [`crate::hooks::ToolMiddleware`]).
///
/// A run abort is not an error: the calls it stops get an aborted error
/// result, and a tool that panics gets an error result naming the panic.
/// Only a [`ToolSink`] error stops the batch and is returned.
///
/// # Panics
///
/// When a call of `plan` is awaiting approval or input: the run must
/// suspend instead.
pub async fn execute<S: ToolSink>(
    plan: BatchPlan,
    sink: &S,
    hooks: &Hooks,
    run: &RunIdentity,
    options: &ExecuteOptions,
) -> Result<Vec<ToolResultBlock>, S::Error> {
    assert!(
        !plan.is_awaiting_approval() && !plan.is_awaiting_input(),
        "execute got a plan with calls awaiting approval or input; suspend the run instead"
    );
    let runner = Runner {
        sink,
        hooks,
        run,
        options,
    };
    let cap = options.max_concurrency.max(1);
    let mut results = Vec::with_capacity(plan.calls.len());
    let mut calls = plan.calls.iter().peekable();
    while let Some(first) = calls.next() {
        let mut group = vec![first];
        while group.len() < cap
            && in_read_group(first)
            && let Some(next) = calls.next_if(|call| in_read_group(call))
        {
            group.push(next);
        }
        let outcomes =
            future::try_join_all(group.into_iter().map(|call| runner.call(call))).await?;
        results.extend(outcomes.into_iter().map(|outcome| outcome.block));
    }
    Ok(results)
}

/// Whether `call` may share a group with read calls: its tool says the call
/// is parallel ([`crate::Tool::parallel`]), or it runs no tool at all.
fn in_read_group(call: &PlannedCall) -> bool {
    match (&call.action, &call.tool) {
        (PlannedAction::Run, Some(tool)) => tool.tool().parallel(&call.input),
        (PlannedAction::Run, None) => false,
        _ => true,
    }
}

struct Runner<'a, S> {
    sink: &'a S,
    hooks: &'a Hooks,
    run: &'a RunIdentity,
    options: &'a ExecuteOptions,
}

/// A call's result before rendering.
struct Finished {
    result: ToolResult,
    contract_error: Option<OutputContractError>,
    hook_records: Vec<HookRecord>,
}

impl From<ToolResult> for Finished {
    fn from(result: ToolResult) -> Self {
        Self {
            result,
            contract_error: None,
            hook_records: Vec::new(),
        }
    }
}

impl<S: ToolSink> Runner<'_, S> {
    async fn call(&self, call: &PlannedCall) -> Result<CallOutcome, S::Error> {
        let mut started = None;
        let mut cache_hit = false;
        let finished = match &call.action {
            _ if self.options.cancel.is_cancelled() => error(ABORTED),
            PlannedAction::Run => match self.cached(call).await {
                Some(output) => {
                    cache_hit = true;
                    let served = self.finish(call, Ok(output), false);
                    self.unless_aborted(async { Ok(served.await) }).await?
                }
                None => {
                    // Before the tool starts, so a crash during a write
                    // never leaves entries it made stale.
                    self.clear_for_write(call).await;
                    self.sink.tool_starting(call).await?;
                    started = Some(Instant::now());
                    let finished = self
                        .unless_aborted(async {
                            let result = self.invoke(call).await?;
                            Ok(self.finish(call, result, true).await)
                        })
                        .await?;
                    self.clear_for_write(call).await;
                    finished
                }
            },
            PlannedAction::Serve { result } => {
                self.unless_aborted(async { Ok(self.finish(call, result.clone(), false).await) })
                    .await?
            }
            PlannedAction::Error { error } => Finished::from(Err(error.clone())),
            PlannedAction::Blocked { reason } => error(reason),
            PlannedAction::Denied { reason } => error(format!("Tool call denied: {reason}")),
            PlannedAction::AwaitingApproval { .. } | PlannedAction::AwaitingInput => {
                unreachable!("execute rejects plans awaiting approval or input")
            }
        };
        let (result, block) = render(&call.tool_use_id, finished.result);
        let outcome = CallOutcome {
            block,
            result,
            duration: started.map_or(Duration::ZERO, |at| at.elapsed()),
            contract_error: finished.contract_error,
            hook_records: finished.hook_records,
            cache_hit,
        };
        self.sink.tool_finished(call, &outcome).await?;
        Ok(outcome)
    }

    /// The cache and the key of `call`, when its tool is cacheable: a
    /// read-scope tool the policy names that neither polls nor asks the
    /// user, with an input that can be digested.
    fn cache_of(&self, call: &PlannedCall) -> Option<(&ToolCacheHandle, String)> {
        let handle = self.options.cache.as_ref()?;
        let tool = call.tool.as_ref()?.tool();
        let cacheable = tool.scope() == ToolScope::Read && !tool.polls() && !tool.asks_user();
        if !cacheable || !handle.config.names(&call.tool_name) {
            return None;
        }
        Some((handle, cache_key(&call.tool_name, &call.input)?))
    }

    /// The live cached result of `call`. A store failure is a miss.
    async fn cached(&self, call: &PlannedCall) -> Option<ToolOutput> {
        let (handle, key) = self.cache_of(call)?;
        handle
            .store
            .get(&handle.namespace, &key)
            .await
            .ok()
            .flatten()
    }

    /// Store the successful `result` of a call that ran, when it is
    /// cacheable and small enough. A store failure loses only the entry.
    async fn store(&self, call: &PlannedCall, result: &ToolResult) {
        let (Some((handle, key)), Ok(output)) = (self.cache_of(call), result) else {
            return;
        };
        let size = serde_json::to_vec(output).map_or(u64::MAX, |bytes| bytes.len() as u64);
        if size > handle.config.max_value_bytes {
            return;
        }
        let (ttl, room) = (handle.config.ttl(), handle.config.max_entries);
        let _ = handle
            .store
            .put(&handle.namespace, &key, output, ttl, room)
            .await;
    }

    /// Clear the namespace's entries around a write or exec tool's call,
    /// when the policy says so. A store failure is ignored.
    async fn clear_for_write(&self, call: &PlannedCall) {
        let Some(handle) = self.options.cache.as_ref() else {
            return;
        };
        let writes = call
            .tool
            .as_ref()
            .is_some_and(|tool| tool.tool().scope() != ToolScope::Read);
        if writes && handle.config.invalidate_on_write {
            let _ = handle.store.clear(&handle.namespace).await;
        }
    }

    /// `work`, or an aborted result once the run abort fires. An abort that
    /// already fired never polls `work`.
    async fn unless_aborted(
        &self,
        work: impl Future<Output = Result<Finished, S::Error>>,
    ) -> Result<Finished, S::Error> {
        match self.options.cancel.run_until_cancelled(work).await {
            Some(finished) => finished,
            None => Ok(error(ABORTED)),
        }
    }

    /// Run the tool, again after a retried failure, forwarding its
    /// progress to the sink. Only the sink's retry barrier can fail.
    async fn invoke(&self, call: &PlannedCall) -> Result<ToolResult, S::Error> {
        let tool = call.tool.as_ref().expect("a Run entry holds its tool");
        let idempotent = tool.tool().idempotent();
        let retry = (self.options.retry.as_ref()).filter(|r| r.covers(&call.tool_name, idempotent));
        let mut attempt = 1;
        loop {
            let (result, timed_out) = self.run_attempt(call, tool, attempt).await;
            let (Some(retry), Err(failure)) = (retry, &result) else {
                return Ok(result);
            };
            let class = if timed_out {
                Some(TIMEOUT_CLASS)
            } else {
                failure.code.as_deref()
            };
            let Some(class) = class.filter(|class| retry.retries(class)) else {
                return Ok(result);
            };
            if attempt >= retry.max_attempts {
                return Ok(result);
            }
            attempt += 1;
            let delay = retry.delay(attempt);
            let retrying = Retrying {
                attempt,
                max_attempts: retry.max_attempts,
                class: class.to_owned(),
                error: failure.message.clone(),
                delay,
            };
            self.sink.tool_retrying(call, &retrying).await?;
            tokio::time::sleep(delay).await;
        }
    }

    /// One attempt of the tool, with its own context, deadline and
    /// progress channel. Also says whether it ran past the call timeout.
    async fn run_attempt(
        &self,
        call: &PlannedCall,
        tool: &Arc<RegisteredTool>,
        attempt: u32,
    ) -> (ToolResult, bool) {
        let token = self.options.cancel.child_token();
        // Cancels the token however the call ends, so work the tool spawned
        // stops too.
        let _stop = token.clone().drop_guard();
        let (progress, mut reports) = mpsc::unbounded_channel();
        // One instant for the tool and the timeout, so a tool that stops
        // just before its deadline is never cut off by the timeout.
        let limit = self.options.call_timeout;
        let deadline = limit.and_then(|limit| tokio::time::Instant::now().checked_add(limit));
        let ctx = ToolContext::new(
            self.run.session_id.clone(),
            self.run.run_id.clone(),
            call.tool_use_id.clone(),
            self.run.principal.clone(),
            token,
            None,
        )
        .with_reports(progress)
        .with_deadline(deadline)
        .with_trace_id(self.run.trace_id.clone());
        let execution = self.body(call, tool, ctx, attempt);
        let execution = self.timed(call, deadline, caught(&call.tool_name, execution));
        let outcome = {
            let forward = pin!(async {
                while let Some(report) = reports.recv().await {
                    self.report(call, report);
                }
                future::pending::<Infallible>().await
            });
            match future::select(pin!(execution), forward).await {
                Either::Left((outcome, _)) => outcome,
                Either::Right((never, _)) => match never {},
            }
        };
        while let Ok(report) = reports.try_recv() {
            self.report(call, report);
        }
        outcome
    }

    /// The tool body, behind the `tool_execute` layers when any are
    /// registered, and called directly otherwise.
    async fn body(
        &self,
        call: &PlannedCall,
        tool: &Arc<RegisteredTool>,
        ctx: ToolContext,
        attempt: u32,
    ) -> ToolResult {
        let pipeline = &self.hooks.tool_execute;
        if pipeline.is_empty() {
            return tool.tool().execute(call.input.clone(), ctx).await;
        }
        let tool_call = ToolCall {
            session_id: self.run.session_id.clone(),
            run_id: self.run.run_id.clone(),
            tool_use_id: call.tool_use_id.clone(),
            tool_name: call.tool_name.clone(),
            input: call.input.clone(),
            attempt,
        };
        let body = Box::pin(async { tool.tool().execute(call.input.clone(), ctx).await });
        pipeline.run(&tool_call, body).await
    }

    fn report(&self, call: &PlannedCall, report: Report) {
        match report {
            Report::Progress(_) if self.options.redactor.is_some() => {}
            Report::Progress(progress) => self.sink.tool_progress(call, progress),
            Report::Event(event) => self.sink.tool_event(call, *event),
        }
    }

    /// `execution`, or a timeout error result at `deadline`; the flag says
    /// which.
    async fn timed(
        &self,
        call: &PlannedCall,
        deadline: Option<tokio::time::Instant>,
        execution: impl Future<Output = ToolResult>,
    ) -> (ToolResult, bool) {
        let (Some(deadline), Some(limit)) = (deadline, self.options.call_timeout) else {
            return (execution.await, false);
        };
        match tokio::time::timeout_at(deadline, execution).await {
            Ok(result) => (result, false),
            Err(_) => {
                let message = format!(
                    "Tool {:?} timed out after {}ms",
                    call.tool_name,
                    limit.as_millis()
                );
                (Err(ToolOutputError::new(message)), true)
            }
        }
    }

    /// Check a success value against the tool's output schema, then run
    /// the `post_tool_use` chain on the result. The schema describes what
    /// the tool returns, so a hook may replace a valid value (with an
    /// offload pointer, say) without breaking it, and gets a violation as
    /// an error result.
    ///
    /// `fresh` says the tool just ran, so a successful result is stored
    /// in the tool cache after the schema check and redaction, before
    /// `post_tool_use`.
    async fn finish(&self, call: &PlannedCall, result: ToolResult, fresh: bool) -> Finished {
        let mut finished = Finished::from(result);
        if let (Ok(output), Some(tool)) = (&finished.result, &call.tool)
            && let Err(violation) = tool.validate_output(output)
        {
            finished.result = Err(ToolOutputError::new(format!(
                "Tool {:?} returned output that does not match its output schema",
                call.tool_name
            )));
            finished.contract_error = Some(violation);
        }
        let redactor = self.options.redactor.as_deref();
        if let Some(redactor) = redactor {
            redactor.result(&mut finished.result);
        }
        if fresh {
            self.store(call, &finished.result).await;
        }
        if !self.hooks.post_tool_use.is_empty() {
            let redacted = redactor.map(|_| finished.result.clone());
            let chain = self
                .hooks
                .post_tool_use
                .run(PostToolUseRequest {
                    session_id: self.run.session_id.clone(),
                    run_id: self.run.run_id.clone(),
                    tool_use_id: call.tool_use_id.clone(),
                    tool_name: call.tool_name.clone(),
                    input: call.input.clone(),
                    result: finished.result,
                })
                .await;
            finished.result = chain.outcome;
            finished.hook_records = chain.records;
            if let (Some(redactor), Some(redacted)) = (redactor, redacted)
                && finished.result != redacted
            {
                redactor.result(&mut finished.result);
            }
        }
        if let Err(error) = &finished.result
            && !self.hooks.post_tool_use_failure.is_empty()
        {
            let chain = self
                .hooks
                .post_tool_use_failure
                .run(&PostToolUseFailureRequest {
                    session_id: self.run.session_id.clone(),
                    run_id: self.run.run_id.clone(),
                    tool_use_id: call.tool_use_id.clone(),
                    tool_name: call.tool_name.clone(),
                    input: call.input.clone(),
                    error: error.clone(),
                })
                .await;
            finished.hook_records.extend(chain.records);
        }
        finished
    }
}

/// `execution`, with a panic in the tool turned into its error result, so a
/// broken tool fails its call and not the run.
async fn caught(name: &str, execution: impl Future<Output = ToolResult>) -> ToolResult {
    let panic = match AssertUnwindSafe(execution).catch_unwind().await {
        Ok(result) => return result,
        Err(panic) => panic,
    };
    let message = match panic_text(&*panic) {
        Some(text) => format!("Tool {name:?} panicked: {text}"),
        None => format!("Tool {name:?} panicked"),
    };
    Err(ToolOutputError::new(message))
}

/// The message of a `panic!`, which is a `&str` or a `String`.
fn panic_text(panic: &(dyn Any + Send)) -> Option<&str> {
    (panic.downcast_ref::<&str>().copied())
        .or_else(|| panic.downcast_ref::<String>().map(String::as_str))
}

fn error(message: impl Into<String>) -> Finished {
    Finished::from(Err(ToolOutputError::new(message)))
}

/// Render a result for the model: a string value as its text, any other
/// value as compact canonical JSON, an error as `is_error` with its message.
/// A value too deep to encode becomes an error result.
fn render(tool_use_id: &str, result: ToolResult) -> (ToolResult, ToolResultBlock) {
    let text = match &result {
        Err(error) => {
            let block = ToolResultBlock::text(tool_use_id, error.message.clone(), true);
            return (result, block);
        }
        Ok(output) => match &output.value {
            JsonValue::String(text) => Ok(text.clone()),
            value => to_canonical_string(value),
        },
    };
    match text {
        Ok(text) => (result, ToolResultBlock::text(tool_use_id, text, false)),
        Err(encode) => {
            let error = ToolOutputError::new(format!("Tool output could not be encoded: {encode}"));
            let block = ToolResultBlock::text(tool_use_id, error.message.clone(), true);
            (Err(error), block)
        }
    }
}
