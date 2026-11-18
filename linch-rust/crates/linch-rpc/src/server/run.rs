//! The run methods and the task that drives one run's stream.

use std::collections::VecDeque;
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex};

use futures::{FutureExt, StreamExt};
use linch::{Envelope, Event, Principal, RunError, RunId, RunOptions, RunStream};
use tokio::sync::OwnedSemaphorePermit;
use tokio_util::sync::CancellationToken;

use super::{Driven, Parked, Reply, Server, Tables, lock, to_value};
use crate::error::{ErrorObject, code};
use crate::protocol::{
    AnswerParams, ApproveParams, CancelParams, Cancelled, ResumeParams, RunEnd, StartParams, method,
};

impl Server {
    pub(super) fn start(&self, params: StartParams) -> Result<Reply, ErrorObject> {
        self.admit_principal(params.principal.as_ref())?;
        let mut options = RunOptions::new(params.session_id);
        options.run_id = params.run_id;
        options.principal = params.principal;
        options.trace_id = params.trace_id;
        options.output_schema = params.output_schema;
        options.tool_choice = params.tool_choice;
        options.owner = self.0.owner.clone();
        let cancel = options.cancel.clone();
        let mut tables = lock(&self.0.tables);
        let agent = tables.agent(&params.agent)?;
        if let Some(tenant) = &self.0.tenant {
            let choice = options.tool_choice.as_ref();
            tenant.admit_tool_choice(choice, agent.spec())?;
        }
        // A fresh run id cannot be in use; a chosen one can.
        if let Some(run_id) = &options.run_id {
            tables.not_driven(run_id)?;
        }
        let place = self.run_place()?;
        let stream = agent.run(params.prompt, options);
        let driven = Driven {
            agent: params.agent,
            cancel,
            _place: place,
        };
        tables.runs.insert(stream.run_id().clone(), driven);
        Ok(Reply::Started(stream))
    }

    pub(super) fn resume(&self, params: ResumeParams) -> Result<Reply, ErrorObject> {
        let run_id = params.run_id;
        self.claim(params.agent, &run_id, params.principal, |agent, options| {
            agent.resume(run_id.clone(), options)
        })
    }

    pub(super) fn approve(&self, params: ApproveParams) -> Result<Reply, ErrorObject> {
        let decisions = params.decisions.into_iter().collect();
        let run_id = params.run_id;
        self.claim(params.agent, &run_id, params.principal, |agent, options| {
            agent.approve(run_id.clone(), decisions, options)
        })
    }

    pub(super) fn answer_run(&self, params: AnswerParams) -> Result<Reply, ErrorObject> {
        let answers = params.answers.into_iter().collect();
        let run_id = params.run_id;
        self.claim(params.agent, &run_id, params.principal, |agent, options| {
            agent.answer(run_id.clone(), answers, options)
        })
    }

    /// Queue the text of a steer for a live run of the agent. It goes
    /// through the agent's stores, so it reaches a run any server drives.
    pub(super) async fn steer_run(
        &self,
        params: crate::protocol::SteerParams,
    ) -> Result<serde_json::Value, ErrorObject> {
        self.admit_principal(params.principal.as_ref())?;
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        let id = agent
            .steer(params.run_id, params.text, params.id)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        to_value(&crate::protocol::Steered { queued: true, id })
    }

    /// Cancel a run this connection drives with its token; else, given an
    /// agent, a suspended run through the agent's stores. The run counts
    /// as driven meanwhile, so an approve on this connection cannot claim
    /// it in between.
    pub(super) async fn cancel(&self, params: CancelParams) -> Result<Cancelled, ErrorObject> {
        let run_id = params.run_id;
        let (agent, _cancelling, kept) = {
            let mut tables = lock(&self.0.tables);
            if let Some(driven) = tables.runs.get(&run_id) {
                driven.cancel.cancel();
                return Ok(Cancelled { cancelled: true });
            }
            let Some(handle) = params.agent else {
                return Ok(Cancelled { cancelled: false });
            };
            let agent = tables.agent(&handle)?;
            let driven = Driven {
                agent: handle,
                cancel: CancellationToken::new(),
                _place: None,
            };
            tables.runs.insert(run_id.clone(), driven);
            // A suspended run that kept its place for its workers frees it
            // once the cancel has stopped them.
            let kept = (tables.parked.remove(&run_id)).and_then(|held| lock(&held).take());
            let cancelling = Cancelling {
                server: self,
                run_id: &run_id,
            };
            (agent, cancelling, kept)
        };
        let mut options = RunOptions::new("");
        options.owner = self.0.owner.clone();
        let cancelled = agent.cancel(run_id.clone(), options).await;
        drop(kept);
        match cancelled {
            Ok(cancelled) => Ok(Cancelled { cancelled }),
            Err(error) => Err(ErrorObject::from(&error)),
        }
    }

    /// Continue `run_id` on the agent `handle` with `continue_run`, and
    /// mark it as driven by this server, unless it already is.
    fn claim(
        &self,
        handle: String,
        run_id: &RunId,
        principal: Option<Principal>,
        continue_run: impl FnOnce(&linch::Agent, RunOptions) -> RunStream,
    ) -> Result<Reply, ErrorObject> {
        self.admit_principal(principal.as_ref())?;
        let mut tables = lock(&self.0.tables);
        let agent = tables.agent(&handle)?;
        tables.not_driven(run_id)?;
        // A suspended run that kept its place for its workers takes it back.
        let kept = (tables.parked.remove(run_id)).and_then(|held| lock(&held).take());
        let place = match kept {
            Some(place) => Some(place),
            None => self.run_place()?,
        };
        // Resume and approve use the session stored with the run.
        let mut options = RunOptions::new("");
        options.principal = principal;
        options.owner = self.0.owner.clone();
        let driven = Driven {
            agent: handle,
            cancel: options.cancel.clone(),
            _place: place,
        };
        tables.runs.insert(run_id.clone(), driven);
        Ok(Reply::Run(continue_run(&agent, options)))
    }

    /// Hold the place of `run_id`, whose stream ended, while it is
    /// suspended with background workers running here; they take no place
    /// of their own. An approve or resume takes the place over in
    /// [`Self::claim`]; else it is freed once the workers end. The task
    /// holds the server and the place weakly, so a closed connection frees
    /// both as before.
    fn keep_place(&self, tables: &mut Tables, run_id: &RunId, driven: Driven) {
        let Driven {
            agent,
            _place: Some(place),
            ..
        } = driven
        else {
            return;
        };
        let Ok(agent) = tables.agent(&agent) else {
            return;
        };
        let Some(workers) = agent.parked_children(run_id) else {
            return;
        };
        let held: Parked = Arc::new(Mutex::new(Some(place)));
        tables.parked.insert(run_id.clone(), Arc::clone(&held));
        let (state, held) = (Arc::downgrade(&self.0), Arc::downgrade(&held));
        let run_id = run_id.clone();
        tokio::spawn(async move {
            workers.await;
            let Some(held) = held.upgrade() else {
                return;
            };
            drop(lock(&held).take());
            if let Some(state) = state.upgrade() {
                let mut tables = lock(&state.tables);
                let ours =
                    (tables.parked.get(&run_id)).is_some_and(|kept| Arc::ptr_eq(kept, &held));
                if ours {
                    tables.parked.remove(&run_id);
                }
            }
        });
    }

    pub(super) fn admit_principal(&self, principal: Option<&Principal>) -> Result<(), ErrorObject> {
        match &self.0.tenant {
            Some(tenant) => tenant.admit_principal(principal),
            None => Ok(()),
        }
    }

    /// A place among the tenant's live runs; a local client needs none.
    fn run_place(&self) -> Result<Option<OwnedSemaphorePermit>, ErrorObject> {
        (self.0.tenant.as_ref())
            .map(|tenant| tenant.run_place())
            .transpose()
    }

    /// Log, once for the run that hit it, a store call that waited out the
    /// pool's acquire timeout, with the tenant and what the run's pool held
    /// then: the only place the tenant and the pool are both known.
    fn warn_pool_exhausted(&self, error: &RunError, run_id: &RunId) {
        if !linch::postgres::is_pool_exhausted(error) {
            return;
        }
        let tables = lock(&self.0.tables);
        let opened = tables
            .runs
            .get(run_id)
            .and_then(|run| tables.opened(&run.agent).ok());
        let pool = opened.and_then(|opened| opened.agent.pool());
        let (max, open, idle) = pool.map_or((0, 0, 0), |pool| {
            let max = pool.options().get_max_connections();
            (
                max,
                pool.size(),
                u32::try_from(pool.num_idle()).unwrap_or(u32::MAX),
            )
        });
        let tenant = self
            .0
            .tenant
            .as_ref()
            .map_or("local", |tenant| tenant.name());
        tracing::warn!(
            tenant,
            run = %run_id,
            max_connections = max,
            open,
            in_use = open.saturating_sub(idle),
            "pool timed out: raise max_pool_connections or lower concurrent runs"
        );
    }

    /// Poll a new run's stream until the run is stored: its `init` envelope,
    /// which comes only after the create commit, or the end of the stream
    /// (an error: nothing was stored). What it yielded meanwhile is returned
    /// for [`Self::drive`] to forward in order.
    pub(super) async fn until_stored(&self, stream: &mut RunStream) -> VecDeque<Next> {
        let mut held = VecDeque::new();
        loop {
            let next = Next::poll(stream).await;
            let stored = matches!(&next, Next::Item(Ok(e)) if matches!(e.event, Event::Init(_)));
            let ended = !matches!(next, Next::Item(Ok(_)));
            held.push_back(next);
            if stored || ended {
                return held;
            }
        }
    }

    /// Forward every envelope as soon as the stream yields it, for the
    /// writer to send in `run.event`s, then send `run.end`. `held` is what
    /// [`Self::until_stored`] polled already.
    pub(super) async fn drive(&self, mut stream: RunStream, mut held: VecDeque<Next>) {
        let run_id = stream.run_id().clone();
        let error = loop {
            let next = match held.pop_front() {
                Some(next) => next,
                None => Next::poll(&mut stream).await,
            };
            match next {
                // A panic inside the run (a tool, a hook) ends it like an
                // error: it stays at its last durable state and can be
                // resumed.
                Next::Panicked => {
                    let message = "the run failed inside the server (a panic); \
                                   it stays at its last durable state";
                    break Some(ErrorObject::new(code::INTERNAL_ERROR, message));
                }
                Next::End => break None,
                Next::Item(Err(error)) => {
                    self.warn_pool_exhausted(&error, &run_id);
                    break Some(ErrorObject::from(&error));
                }
                Next::Item(Ok(envelope)) => match serde_json::value::to_raw_value(&envelope) {
                    Ok(raw) => self.0.outbox.envelope(run_id.clone(), raw).await,
                    Err(error) => {
                        let message = format!("envelope could not be encoded: {error}");
                        break Some(ErrorObject::new(code::INTERNAL_ERROR, message));
                    }
                },
            }
        };
        // Dropping the stream first leaves the run at its last durable
        // state before a client can resume it.
        drop(stream);
        // Under one lock, so an approve cannot claim the run in between.
        {
            let mut tables = lock(&self.0.tables);
            if let Some(driven) = tables.runs.remove(&run_id) {
                self.keep_place(&mut tables, &run_id, driven);
            }
        }
        let end = RunEnd { run_id, error };
        let params = to_value(&end).expect("run.end params encode");
        self.0.outbox.notify(method::RUN_END, params).await;
    }
}

/// What a run's stream yielded.
pub(super) enum Next {
    Item(Result<Box<Envelope>, RunError>),
    End,
    /// The poll panicked.
    Panicked,
}

impl Next {
    async fn poll(stream: &mut RunStream) -> Self {
        match AssertUnwindSafe(stream.next()).catch_unwind().await {
            Err(_) => Self::Panicked,
            Ok(None) => Self::End,
            Ok(Some(item)) => Self::Item(item.map(Box::new)),
        }
    }
}

/// A store cancel in progress: its entry among the driven runs, removed
/// when the cancel ends, a panic included.
struct Cancelling<'a> {
    server: &'a Server,
    run_id: &'a RunId,
}

impl Drop for Cancelling<'_> {
    fn drop(&mut self) {
        lock(&self.server.0.tables).runs.remove(self.run_id);
    }
}

impl Tables {
    /// "Run busy" when this server already drives `run_id`.
    fn not_driven(&self, run_id: &RunId) -> Result<(), ErrorObject> {
        if self.runs.contains_key(run_id) {
            return Err(ErrorObject::new(
                code::RUN_BUSY,
                format!("run {run_id} is already running on this server"),
            ));
        }
        Ok(())
    }
}
