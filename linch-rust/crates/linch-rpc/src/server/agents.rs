//! The agent methods: `agent.open`, `agent.close`, `session.close` and
//! `run.events`, and the handle lookup the run methods share.

use std::sync::Arc;
use std::sync::atomic::Ordering;

use linch::{Agent, ExecutionConfig, JsonObject, JsonValue, RemoteExec};
use serde_json::json;

use super::exec::HostExec;
use super::schedule::Background;
use super::{Opened, Server, Tables, callback, host, lock, to_value};
use crate::error::{ErrorObject, code};
use crate::protocol::{
    AgentOpened, CloseParams, Events, EventsParams, OpenParams, SessionCloseParams, Skipped,
};
use crate::schedule_tools::{Binding, Bound};

impl Server {
    /// Open an agent whose host tools and callbacks call back over this
    /// connection. They are bound before the agent is built, so the
    /// contract covers them. A tenant's agent takes its place after the
    /// checks and before anything starts.
    pub(super) async fn open(&self, mut params: OpenParams) -> Result<JsonValue, ErrorObject> {
        let place = match &self.0.tenant {
            Some(tenant) => {
                tenant.admit(&mut params.config).await?;
                Some(tenant.agent_place()?)
            }
            None => None,
        };
        // The handle is named first: tools and callbacks carry it per call.
        let number = self.0.next_agent.fetch_add(1, Ordering::Relaxed) + 1;
        let handle = format!("agent-{number}");
        let background = Background::new(
            &params.config,
            !params.tools.is_empty(),
            !params.callbacks.is_empty(),
        );
        let scheduling = match (params.config.schedule_tools, &self.0.tenant) {
            (false, _) => None,
            (true, Some(tenant)) => Some((Arc::clone(tenant), background.for_tools(&handle)?)),
            (true, None) => return Err(local_schedule_tools()),
        };
        let tools = host::registry(params.tools, &handle, &self.0.peer)?;
        // After the host tools, of which such an agent has none: first in
        // the registry, as a worker's open of its definition puts them.
        let binding = match &scheduling {
            Some(_) => Some(Binding::register(&tools)?),
            None => None,
        };
        let hooks = callback::hooks(params.callbacks, &handle, &self.0.peer)?;
        // A `remote` backend runs its commands in this connection's client.
        let remote = matches!(params.config.execution, Some(ExecutionConfig::Remote(_)));
        let remote =
            remote.then(|| Arc::new(HostExec::new(&handle, &self.0.peer)) as Arc<dyn RemoteExec>);
        let opened = params
            .config
            .open_with_remote(tools, hooks, &self.0.pools, remote)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        if let (Some(binding), Some((tenant, (definition, migrated)))) = (binding, scheduling) {
            binding.bind(Bound {
                pool: (opened.pool().cloned()).expect("schedule_tools opened on Postgres"),
                namespace: opened.agent().namespace().to_owned(),
                definition,
                tenant,
                migrated,
            });
        }
        let contract = opened.agent().contract().to_string();
        let skipped = opened.skipped().iter().map(Skipped::from).collect();
        let opened = Opened {
            agent: opened,
            place,
            background,
        };
        lock(&self.0.tables).agents.insert(handle.clone(), opened);
        to_value(&AgentOpened {
            agent: handle,
            contract,
            skipped,
        })
    }

    /// Free the handle, then disconnect the agent's MCP servers and wait
    /// for them, then free its place. Refused while this server drives a
    /// run of the agent.
    pub(super) async fn close(&self, params: CloseParams) -> Result<JsonValue, ErrorObject> {
        let opened: Opened = {
            let mut tables = lock(&self.0.tables);
            tables.agent(&params.agent)?;
            if tables.runs.values().any(|run| run.agent == params.agent)
                || tables.evals.contains_key(&params.agent)
            {
                return Err(ErrorObject::new(
                    code::RUN_BUSY,
                    format!(
                        "agent {:?} has runs in progress; cancel them or wait for run.end",
                        params.agent
                    ),
                ));
            }
            tables
                .agents
                .remove(&params.agent)
                .expect("looked up above")
        };
        opened.close().await;
        Ok(JsonValue::Object(JsonObject::new()))
    }

    /// Run the agent's session-close callbacks, which drop what its
    /// workspace tools recorded as read in the session and kill its
    /// background `Bash` commands. Nothing is written.
    pub(super) fn close_session(
        &self,
        params: SessionCloseParams,
    ) -> Result<JsonValue, ErrorObject> {
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        agent.close_session(&params.session_id);
        Ok(JsonValue::Object(JsonObject::new()))
    }

    pub(super) async fn events(&self, params: EventsParams) -> Result<JsonValue, ErrorObject> {
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        let events = agent
            .events(&params.run_id, params.after_seq)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        to_value(&Events { events })
    }
}

/// A local client's agent may not have the schedule tools: it has no
/// tenant caps to hold the model to, and a stdio server runs no worker.
fn local_schedule_tools() -> ErrorObject {
    let message = "schedule_tools needs a tenant whose allowlists give it schedules \
                   (max_schedules), served by linch-server with a worker; a local \
                   connection has no caps";
    ErrorObject::new(code::NOT_ALLOWED, message).with_data(json!({ "kind": "schedule_tools" }))
}

impl Tables {
    /// The open agent named by `handle`, or "agent not found".
    pub(super) fn opened(&self, handle: &str) -> Result<&Opened, ErrorObject> {
        self.agents.get(handle).ok_or_else(|| {
            ErrorObject::new(code::AGENT_NOT_FOUND, format!("no open agent {handle:?}"))
        })
    }

    /// The agent named by `handle`, or "agent not found".
    pub(super) fn agent(&self, handle: &str) -> Result<Agent, ErrorObject> {
        Ok(self.opened(handle)?.agent.agent().clone())
    }
}
