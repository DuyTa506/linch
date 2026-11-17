//! [`execute`]: run a decided plan.

use std::any::Any;
use std::borrow::Cow;
use std::convert::Infallible;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::pin::pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
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
use crate::tool::{Report, ToolContext, ToolOutputError, ToolProgress, ToolResult, ToolScope};
use crate::tool_cache::{
    CacheScope, Lookup, PutLimits, STORE_TIMEOUT, ToolCacheError, ToolCacheHandle, cache_key,
};
use crate::tool_retry::{ERROR_BYTES, RetryClock, TIMEOUT_CLASS, ToolRetry};

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
    /// The failed attempt's error message, redacted when the run redacts
    /// tool results and cut to [`ERROR_BYTES`] bytes.
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
    /// What the run has spent retrying, which its retries stop at
    /// [`crate::tool_retry::RETRY_BUDGET`]; shared by every batch of the
    /// run.
    pub retry_clock: Arc<RetryClock>,
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

/// A call's cache entry: the cache and the call's key in it.
struct Slot<'a> {
    cache: &'a ToolCacheHandle,
    key: String,
}

/// The tool's own result of the last attempt, kept when `tool_execute`
/// layers wrap it, to tell whether they changed what the call returned.
type Raw = Mutex<Option<ToolResult>>;

/// A store call's answer, or `None` when it failed or took past
/// [`STORE_TIMEOUT`]: either is a miss for the model's purposes.
async fn bounded<T>(call: impl Future<Output = Result<T, ToolCacheError>>) -> Option<T> {
    tokio::time::timeout(STORE_TIMEOUT, call).await.ok()?.ok()
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Whether the `tool_execute` layers returned the tool's own `result`
/// unchanged. Without layers (`raw` is `None`) nothing could change it.
fn untouched(raw: Option<&Raw>, result: &ToolResult) -> bool {
    raw.is_none_or(|raw| lock(raw).as_ref() == Some(result))
}

/// `text` cut to at most `max` bytes, at a character boundary.
fn cut(text: &str, max: usize) -> &str {
    let mut end = text.len().min(max);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

impl<S: ToolSink> Runner<'_, S> {
    async fn call(&self, call: &PlannedCall) -> Result<CallOutcome, S::Error> {
        let mut started = None;
        let mut cache_hit = false;
        let finished = match &call.action {
            _ if self.options.cancel.is_cancelled() => error(ABORTED),
            PlannedAction::Run => self.run(call, &mut started, &mut cache_hit).await?,
            PlannedAction::Serve { result } => {
                self.unless_aborted(async { Ok(self.finish(call, result.clone(), None).await) })
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

    /// A planned `Run` call: served from the cache, or run (behind the
    /// durable start) and its result stored.
    async fn run(
        &self,
        call: &PlannedCall,
        started: &mut Option<Instant>,
        cache_hit: &mut bool,
    ) -> Result<Finished, S::Error> {
        let slot = self.slot_of(call);
        // The epoch the lookup saw: a result is stored only if no write
        // cleared the namespace since.
        let mut epoch = None;
        if let Some(slot) = &slot {
            let Some(lookup) = (self.options.cancel)
                .run_until_cancelled(bounded(
                    slot.cache.store.get(&slot.cache.namespace, &slot.key),
                ))
                .await
            else {
                return Ok(error(ABORTED));
            };
            match lookup {
                Some(Lookup {
                    hit: Some(output), ..
                }) => {
                    *cache_hit = true;
                    let served = self.finish(call, Ok(output), None);
                    return self.unless_aborted(async { Ok(served.await) }).await;
                }
                Some(Lookup { epoch: seen, .. }) => epoch = Some(seen),
                None => {}
            }
        }
        self.stop_cache_on_background(call);
        // Before the tool starts, so a crash during a write never leaves
        // entries it made stale.
        if self.clear_before(call).await {
            return Ok(error(ABORTED));
        }
        self.sink.tool_starting(call).await?;
        *started = Some(Instant::now());
        let raw = (slot.is_some() && !self.hooks.tool_execute.is_empty()).then(Raw::default);
        let finished = self
            .unless_aborted(async {
                let result = self.invoke(call, raw.as_ref()).await?;
                // A layer's veto or replacement is never cached.
                let storing = match (&slot, epoch) {
                    (Some(slot), Some(epoch)) if untouched(raw.as_ref(), &result) => {
                        Some((slot, epoch))
                    }
                    _ => None,
                };
                Ok(self.finish(call, result, storing).await)
            })
            .await?;
        self.clear_after(call).await;
        Ok(finished)
    }

    /// The call's cache entry, when its tool is cacheable: a read-scope
    /// tool the policy names that neither polls, asks the user nor keeps
    /// session state, with an input that can be digested, while the
    /// agent's caching has not ended.
    fn slot_of(&self, call: &PlannedCall) -> Option<Slot<'_>> {
        let cache = self
            .options
            .cache
            .as_ref()
            .filter(|cache| !cache.stopped())?;
        let tool = call.tool.as_ref()?.tool();
        let cacheable = tool.scope() == ToolScope::Read
            && !tool.polls()
            && !tool.asks_user()
            && tool.cache_safe();
        if !cacheable || !cache.config.names(&call.tool_name) {
            return None;
        }
        let session = match cache.config.scope {
            CacheScope::Session => Some(self.run.session_id.as_str()),
            CacheScope::Namespace => None,
        };
        let key = cache_key(
            session,
            self.run.principal.as_ref(),
            &call.tool_name,
            &call.input,
        )?;
        Some(Slot { cache, key })
    }

    /// Store the successful `result` of a call that ran, when it is small
    /// enough. A store failure or timeout loses only the entry.
    async fn store(&self, slot: &Slot<'_>, epoch: u64, result: &ToolResult) {
        let Ok(output) = result else { return };
        let config = &slot.cache.config;
        let size = serde_json::to_vec(output).map_or(u64::MAX, |bytes| bytes.len() as u64);
        if size > config.max_value_bytes {
            return;
        }
        let limits = PutLimits {
            ttl: config.ttl(),
            max_entries: config.max_entries,
            quota: config.quota.clone(),
        };
        let put = slot
            .cache
            .store
            .put(&slot.cache.namespace, &slot.key, output, epoch, &limits);
        let _ = bounded(put).await;
    }

    /// Ends caching for the agent at the first call that starts work
    /// outliving it: nothing says when that work changes what a read tool
    /// answers.
    fn stop_cache_on_background(&self, call: &PlannedCall) {
        let Some(cache) = self.options.cache.as_ref() else {
            return;
        };
        let launches =
            (call.tool.as_ref()).is_some_and(|tool| tool.tool().launches_background(&call.input));
        if launches {
            cache.stop();
        }
    }

    /// The cache to clear around `call`: it is a write or exec tool's and
    /// the policy says to.
    fn clearing(&self, call: &PlannedCall) -> Option<&ToolCacheHandle> {
        let cache = self.options.cache.as_ref()?;
        let writes =
            (call.tool.as_ref()).is_some_and(|tool| tool.tool().scope() != ToolScope::Read);
        (writes && cache.config.invalidate_on_write).then_some(cache)
    }

    /// Clear the namespace's entries before a write or exec tool starts.
    /// Whether the run abort fired first, so the tool must not start. A
    /// clear that fails or times out leaves entries that may be stale, so
    /// caching ends for the agent.
    async fn clear_before(&self, call: &PlannedCall) -> bool {
        let Some(cache) = self.clearing(call) else {
            return false;
        };
        let clearing = bounded(cache.store.clear(&cache.namespace));
        match self.options.cancel.run_until_cancelled(clearing).await {
            None => true,
            Some(Some(())) => false,
            Some(None) => {
                cache.stop();
                false
            }
        }
    }

    /// Clear the namespace's entries after a write or exec tool ended. Not
    /// cut short by the run abort: an aborted write may have changed
    /// state, and the store call is bounded by [`STORE_TIMEOUT`] anyway.
    async fn clear_after(&self, call: &PlannedCall) {
        let Some(cache) = self.clearing(call) else {
            return;
        };
        if bounded(cache.store.clear(&cache.namespace)).await.is_none() {
            cache.stop();
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
    async fn invoke(&self, call: &PlannedCall, raw: Option<&Raw>) -> Result<ToolResult, S::Error> {
        let tool = call.tool.as_ref().expect("a Run entry holds its tool");
        let idempotent = tool.tool().idempotent();
        let retry = (self.options.retry.as_ref()).filter(|r| r.covers(&call.tool_name, idempotent));
        // When this call's retry time was last charged to the run's clock;
        // `None` until a failure is retried.
        let mut charged = None::<tokio::time::Instant>;
        let mut attempt = 1;
        loop {
            let (result, timed_out) = self.run_attempt(call, tool, attempt, raw).await;
            if let Some(since) = charged.as_mut() {
                // The wait and the attempt that just ended.
                let now = tokio::time::Instant::now();
                self.options.retry_clock.charge(now - *since);
                *since = now;
            }
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
            // The run's retries are bounded as a whole, not only each
            // attempt by the call timeout.
            let spent = self.options.retry_clock.spent();
            let Some(delay) = retry.wait(attempt + 1, spent) else {
                return Ok(result);
            };
            charged.get_or_insert_with(tokio::time::Instant::now);
            attempt += 1;
            let retrying = Retrying {
                attempt,
                max_attempts: retry.max_attempts,
                class: class.to_owned(),
                error: self.retry_error(&failure.message),
                delay,
            };
            self.sink.tool_retrying(call, &retrying).await?;
            tokio::time::sleep(delay).await;
        }
    }

    /// The failed attempt's error as the `tool_retry` event keeps it:
    /// redacted first (the event is persisted and streamed before any
    /// `post_tool_use` hook sees the failure), then cut, so a secret is
    /// never split past the pattern that would have matched it.
    fn retry_error(&self, message: &str) -> String {
        let redacted = match self.options.redactor.as_deref() {
            Some(redactor) => redactor.text(message),
            None => Cow::Borrowed(message),
        };
        cut(&redacted, ERROR_BYTES).to_owned()
    }

    /// One attempt of the tool, with its own context, deadline and
    /// progress channel. Also says whether it ran past the call timeout.
    async fn run_attempt(
        &self,
        call: &PlannedCall,
        tool: &Arc<RegisteredTool>,
        attempt: u32,
        raw: Option<&Raw>,
    ) -> (ToolResult, bool) {
        if let Some(raw) = raw {
            *lock(raw) = None;
        }
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
        let execution = self.body(call, tool, ctx, attempt, raw);
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
        raw: Option<&Raw>,
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
        let body = Box::pin(async {
            let result = tool.tool().execute(call.input.clone(), ctx).await;
            if let Some(raw) = raw {
                *lock(raw) = Some(result.clone());
            }
            result
        });
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
    /// `storing` names the cache entry and the epoch a lookup saw when the
    /// tool just ran and its result may be cached: a successful result is
    /// stored after the schema check and redaction, before
    /// `post_tool_use`.
    async fn finish(
        &self,
        call: &PlannedCall,
        result: ToolResult,
        storing: Option<(&Slot<'_>, u64)>,
    ) -> Finished {
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
        if let Some((slot, epoch)) = storing {
            self.store(slot, epoch, &finished.result).await;
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
