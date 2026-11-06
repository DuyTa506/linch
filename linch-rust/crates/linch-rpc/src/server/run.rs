//! The run methods and the task that drives one run's stream.

use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex};

use futures::{FutureExt, StreamExt};
use linch::{Principal, RunId, RunOptions, RunStream};
use tokio::sync::OwnedSemaphorePermit;

use super::{Driven, Parked, Reply, Server, Tables, lock, to_value};
use crate::error::{ErrorObject, code};
use crate::protocol::{
    ApproveParams, CancelParams, Cancelled, ResumeParams, RunEnd, StartParams, method,
};

/// Lease owner written with each claim this server makes.
const OWNER: &str = "linch-rpc";

impl Server {
    pub(super) fn start(&self, params: StartParams) -> Result<Reply, ErrorObject> {
        self.admit_principal(params.principal.as_ref())?;
        let mut options = RunOptions::new(params.session_id);
        options.run_id = params.run_id;
        options.principal = params.principal;
        options.trace_id = params.trace_id;
        options.owner = OWNER.into();
        let cancel = options.cancel.clone();
        let mut tables = lock(&self.0.tables);
        let agent = tables.agent(&params.agent)?;
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
        Ok(Reply::Run(stream))
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

    pub(super) fn cancel(&self, params: CancelParams) -> Cancelled {
        let tables = lock(&self.0.tables);
        let driven = tables.runs.get(&params.run_id);
        if let Some(driven) = driven {
            driven.cancel.cancel();
        }
        Cancelled {
            cancelled: driven.is_some(),
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
        options.owner = OWNER.into();
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

    fn admit_principal(&self, principal: Option<&Principal>) -> Result<(), ErrorObject> {
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

    /// Forward every envelope as soon as the stream yields it, for the
    /// writer to send in `run.event`s, then send `run.end`.
    pub(super) async fn drive(&self, mut stream: RunStream) {
        let run_id = stream.run_id().clone();
        let error = loop {
            // A panic inside the run (a tool, a hook) ends it like an error:
            // it stays at its last durable state and can be resumed.
            let Ok(next) = AssertUnwindSafe(stream.next()).catch_unwind().await else {
                let message = "the run failed inside the server (a panic); \
                               it stays at its last durable state";
                break Some(ErrorObject::new(code::INTERNAL_ERROR, message));
            };
            match next {
                None => break None,
                Some(Err(error)) => break Some(ErrorObject::from(&error)),
                Some(Ok(envelope)) => match serde_json::value::to_raw_value(&envelope) {
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
