//! The agent methods: `agent.open`, `agent.close` and `run.events`, and the
//! handle lookup the run methods share.

use std::sync::atomic::Ordering;

use linch::{Agent, JsonObject, JsonValue, OpenedAgent};

use super::{Server, Tables, callback, host, lock, to_value};
use crate::error::{ErrorObject, code};
use crate::protocol::{AgentOpened, CloseParams, Events, EventsParams, OpenParams};

impl Server {
    /// Open an agent whose host tools and callbacks call back over this
    /// connection. They are bound before the agent is built, so the
    /// contract covers them.
    pub(super) async fn open(&self, mut params: OpenParams) -> Result<JsonValue, ErrorObject> {
        if let Some(tenant) = &self.0.tenant {
            tenant.admit(&mut params.config).await?;
        }
        // The handle is named first: tools and callbacks carry it per call.
        let number = self.0.next_agent.fetch_add(1, Ordering::Relaxed) + 1;
        let handle = format!("agent-{number}");
        let tools = host::registry(params.tools, &handle, &self.0.peer)?;
        let hooks = callback::hooks(params.callbacks, &handle, &self.0.peer)?;
        let opened = params
            .config
            .open_with(tools, hooks)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        let contract = opened.agent().contract().to_string();
        lock(&self.0.tables).agents.insert(handle.clone(), opened);
        to_value(&AgentOpened {
            agent: handle,
            contract,
        })
    }

    /// Free the handle, then disconnect the agent's MCP servers and wait
    /// for them. Refused while this server drives a run of the agent.
    pub(super) async fn close(&self, params: CloseParams) -> Result<JsonValue, ErrorObject> {
        let opened: OpenedAgent = {
            let mut tables = lock(&self.0.tables);
            tables.agent(&params.agent)?;
            if tables.runs.values().any(|run| run.agent == params.agent) {
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

    pub(super) async fn events(&self, params: EventsParams) -> Result<JsonValue, ErrorObject> {
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        let events = agent
            .events(&params.run_id, params.after_seq)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        to_value(&Events { events })
    }
}

impl Tables {
    /// The agent named by `handle`, or "agent not found".
    pub(super) fn agent(&self, handle: &str) -> Result<Agent, ErrorObject> {
        let opened = self.agents.get(handle).ok_or_else(|| {
            ErrorObject::new(code::AGENT_NOT_FOUND, format!("no open agent {handle:?}"))
        })?;
        Ok(opened.agent().clone())
    }
}
