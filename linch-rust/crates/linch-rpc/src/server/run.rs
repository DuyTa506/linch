//! The run methods and the task that drives one run's stream.

use futures::StreamExt;
use linch::{Principal, RunId, RunOptions, RunStream};

use super::{Reply, Server, lock, to_value};
use crate::error::{ErrorObject, code};
use crate::protocol::{
    ApproveParams, CancelParams, Cancelled, ResumeParams, RunEnd, StartParams, method,
};

/// Lease owner written with each claim this server makes.
const OWNER: &str = "linch-rpc";

impl Server {
    pub(super) fn start(&self, params: StartParams) -> Result<Reply, ErrorObject> {
        let agent = self.agent(&params.agent)?;
        let mut options = RunOptions::new(params.session_id);
        options.principal = params.principal;
        options.owner = OWNER.into();
        let cancel = options.cancel.clone();
        let stream = agent.run(params.prompt, options);
        // A fresh run id cannot be in use.
        lock(&self.0.runs).insert(stream.run_id().clone(), cancel);
        Ok(Reply::Run(stream))
    }

    pub(super) fn resume(&self, params: ResumeParams) -> Result<Reply, ErrorObject> {
        let agent = self.agent(&params.agent)?;
        let options = self.claim(&params.run_id, params.principal)?;
        Ok(Reply::Run(agent.resume(params.run_id, options)))
    }

    pub(super) fn approve(&self, params: ApproveParams) -> Result<Reply, ErrorObject> {
        let agent = self.agent(&params.agent)?;
        let options = self.claim(&params.run_id, params.principal)?;
        let decisions = (params.decisions.into_iter())
            .map(|(tool_use_id, decision)| (tool_use_id, decision.into()))
            .collect();
        Ok(Reply::Run(agent.approve(params.run_id, decisions, options)))
    }

    pub(super) fn cancel(&self, params: CancelParams) -> Cancelled {
        let token = lock(&self.0.runs).get(&params.run_id).cloned();
        if let Some(token) = &token {
            token.cancel();
        }
        Cancelled {
            cancelled: token.is_some(),
        }
    }

    /// Mark `run_id` as driven by this server, unless it already is.
    fn claim(
        &self,
        run_id: &RunId,
        principal: Option<Principal>,
    ) -> Result<RunOptions, ErrorObject> {
        let mut runs = lock(&self.0.runs);
        if runs.contains_key(run_id) {
            return Err(ErrorObject::new(
                code::RUN_BUSY,
                format!("run {run_id} is already running on this server"),
            ));
        }
        // Resume and approve use the session stored with the run.
        let mut options = RunOptions::new("");
        options.principal = principal;
        options.owner = OWNER.into();
        runs.insert(run_id.clone(), options.cancel.clone());
        Ok(options)
    }

    /// Forward every envelope as `run.event`, then send `run.end`.
    pub(super) async fn drive(&self, mut stream: RunStream) {
        let run_id = stream.run_id().clone();
        let error = loop {
            match stream.next().await {
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
        lock(&self.0.runs).remove(&run_id);
        let end = RunEnd { run_id, error };
        let params = to_value(&end).expect("run.end params encode");
        self.0.outbox.notify(method::RUN_END, params).await;
    }
}
