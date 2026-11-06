//! [`execute`]: run a decided plan.

use std::any::Any;
use std::convert::Infallible;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::pin::pin;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::FutureExt;
use futures::future::{self, Either};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use crate::canonical::to_canonical_string;
use crate::events::Event;
use crate::hooks::{HookRecord, Hooks, PostToolUseRequest};
use crate::json::JsonValue;
use crate::message::ToolResultBlock;
use crate::registry::OutputContractError;
use crate::tool::{Report, ToolContext, ToolOutputError, ToolProgress, ToolResult};

use super::RunIdentity;
use super::plan::{BatchPlan, PlannedAction, PlannedCall};

/// Model-facing text of a call the run abort stopped or never started.
const ABORTED: &str = "Tool call aborted";

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
    /// Set when a success value broke the tool's `output_schema`; `result`
    /// is then an error.
    pub contract_error: Option<OutputContractError>,
    /// The `post_tool_use` hooks called, in call order.
    pub hook_records: Vec<HookRecord>,
}

/// Limits for one [`execute`] call.
#[derive(Debug, Clone)]
pub struct ExecuteOptions {
    /// Most read calls run at once. Zero counts as one.
    pub max_concurrency: usize,
    /// Longest one tool may run before it gets a timeout error result; the
    /// tool sees the end as [`ToolContext::deadline`]. `None` waits for the
    /// tool.
    pub call_timeout: Option<Duration>,
    /// Run abort. Cancelling it cancels running tools, and calls not yet
    /// started get an aborted error result.
    pub cancel: CancellationToken,
}

/// Run a decided plan and return one result per call, in call order.
///
/// A contiguous run of read calls (see [`crate::Tool::parallel`]) executes
/// concurrently, in groups of at most [`ExecuteOptions::max_concurrency`];
/// any other call runs alone, so a call after it waits for it. Calls that run no tool join a
/// read group. Each tool gets a child of the run's cancel token, cancelled
/// when the call ends. The `post_tool_use` chain of `hooks` runs on every
/// result of a run or served call, then a success value is checked against
/// the tool's `output_schema`.
///
/// A run abort is not an error: the calls it stops get an aborted error
/// result, and a tool that panics gets an error result naming the panic.
/// Only a [`ToolSink`] error stops the batch and is returned.
///
/// # Panics
///
/// When a call of `plan` is awaiting approval: the run must suspend
/// instead.
pub async fn execute<S: ToolSink>(
    plan: BatchPlan,
    sink: &S,
    hooks: &Hooks,
    run: &RunIdentity,
    options: &ExecuteOptions,
) -> Result<Vec<ToolResultBlock>, S::Error> {
    assert!(
        !plan.is_awaiting_approval(),
        "execute got a plan with calls awaiting approval; suspend the run instead"
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
        let finished = match &call.action {
            _ if self.options.cancel.is_cancelled() => error(ABORTED),
            PlannedAction::Run => {
                self.sink.tool_starting(call).await?;
                started = Some(Instant::now());
                self.unless_aborted(async {
                    let result = self.invoke(call).await;
                    self.finish(call, result).await
                })
                .await
            }
            PlannedAction::Serve { result } => {
                self.unless_aborted(self.finish(call, result.clone())).await
            }
            PlannedAction::Error { error } => Finished::from(Err(error.clone())),
            PlannedAction::Blocked { reason } => error(reason),
            PlannedAction::Denied { reason } => error(format!("Tool call denied: {reason}")),
            PlannedAction::AwaitingApproval { .. } => {
                unreachable!("execute rejects plans awaiting approval")
            }
        };
        let (result, block) = render(&call.tool_use_id, finished.result);
        let outcome = CallOutcome {
            block,
            result,
            duration: started.map_or(Duration::ZERO, |at| at.elapsed()),
            contract_error: finished.contract_error,
            hook_records: finished.hook_records,
        };
        self.sink.tool_finished(call, &outcome).await?;
        Ok(outcome)
    }

    /// `work`, or an aborted result once the run abort fires. An abort that
    /// already fired never polls `work`.
    async fn unless_aborted(&self, work: impl Future<Output = Finished>) -> Finished {
        match self.options.cancel.run_until_cancelled(work).await {
            Some(finished) => finished,
            None => error(ABORTED),
        }
    }

    /// Run the tool, forwarding its progress to the sink.
    async fn invoke(&self, call: &PlannedCall) -> ToolResult {
        let tool = call.tool.as_ref().expect("a Run entry holds its tool");
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
        .with_deadline(deadline);
        let execution = async { tool.tool().execute(call.input.clone(), ctx).await };
        let execution = self.timed(call, deadline, caught(&call.tool_name, execution));
        let result = {
            let forward = pin!(async {
                while let Some(report) = reports.recv().await {
                    self.report(call, report);
                }
                future::pending::<Infallible>().await
            });
            match future::select(pin!(execution), forward).await {
                Either::Left((result, _)) => result,
                Either::Right((never, _)) => match never {},
            }
        };
        while let Ok(report) = reports.try_recv() {
            self.report(call, report);
        }
        result
    }

    fn report(&self, call: &PlannedCall, report: Report) {
        match report {
            Report::Progress(progress) => self.sink.tool_progress(call, progress),
            Report::Event(event) => self.sink.tool_event(call, *event),
        }
    }

    /// `execution`, or a timeout error result at `deadline`.
    async fn timed(
        &self,
        call: &PlannedCall,
        deadline: Option<tokio::time::Instant>,
        execution: impl Future<Output = ToolResult>,
    ) -> ToolResult {
        let (Some(deadline), Some(limit)) = (deadline, self.options.call_timeout) else {
            return execution.await;
        };
        tokio::time::timeout_at(deadline, execution)
            .await
            .unwrap_or_else(|_| {
                Err(ToolOutputError::new(format!(
                    "Tool {:?} timed out after {}ms",
                    call.tool_name,
                    limit.as_millis()
                )))
            })
    }

    /// Run the `post_tool_use` chain, then check a success value against
    /// the tool's output schema.
    async fn finish(&self, call: &PlannedCall, result: ToolResult) -> Finished {
        let mut finished = Finished::from(result);
        if !self.hooks.post_tool_use.is_empty() {
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
        }
        if let (Ok(output), Some(tool)) = (&finished.result, &call.tool)
            && let Err(violation) = tool.validate_output(output)
        {
            finished.result = Err(ToolOutputError::new(format!(
                "Tool {:?} returned output that does not match its output schema",
                call.tool_name
            )));
            finished.contract_error = Some(violation);
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
