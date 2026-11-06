//! The run methods and the task that drives one run's stream.

use std::panic::AssertUnwindSafe;

use futures::{FutureExt, StreamExt};
use linch::{Principal, RunId, RunOptions, RunStream};

use super::{Driven, Reply, Server, Tables, lock, to_value};
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
        options.owner = OWNER.into();
        let cancel = options.cancel.clone();
        let mut tables = lock(&self.0.tables);
        let agent = tables.agent(&params.agent)?;
        // A fresh run id cannot be in use; a chosen one can.
        if let Some(run_id) = &options.run_id {
            tables.not_driven(run_id)?;
        }
        let stream = agent.run(params.prompt, options);
        let driven = Driven {
            agent: params.agent,
            cancel,
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
        // Resume and approve use the session stored with the run.
        let mut options = RunOptions::new("");
        options.principal = principal;
        options.owner = OWNER.into();
        let driven = Driven {
            agent: handle,
            cancel: options.cancel.clone(),
        };
        tables.runs.insert(run_id.clone(), driven);
        Ok(Reply::Run(continue_run(&agent, options)))
    }

    fn admit_principal(&self, principal: Option<&Principal>) -> Result<(), ErrorObject> {
        match &self.0.tenant {
            Some(tenant) => tenant.admit_principal(principal),
            None => Ok(()),
        }
    }

    /// Forward every envelope as `run.event`, then send `run.end`.
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
                Some(Ok(envelope)) => match envelope.to_value() {
                    Ok(value) => self.0.outbox.notify(method::RUN_EVENT, value).await,
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
        lock(&self.0.tables).runs.remove(&run_id);
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
