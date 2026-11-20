//! The agent methods: `agent.open`, `agent.close`, `session.close` and
//! `run.events`, and the handle lookup the run methods share.

use std::sync::Arc;
use std::sync::atomic::Ordering;

use linch::{Agent, Envelope, JsonObject, JsonValue, RemoteExec};
use serde_json::json;

use super::exec::HostExec;
use super::schedule::Background;
use super::{MAX_LINE_BYTES, Opened, Server, Tables, callback, host, lock, to_value};
use crate::error::{ErrorObject, code, invalid_params};
use crate::protocol::{
    AgentOpened, CloseParams, Events, EventsParams, OpenParams, SessionCloseParams, Skipped,
};
use crate::schedule_tools::{Binding, Bound};

/// Envelopes in a `run.events` answer when the request has no `limit`.
const EVENTS_LIMIT_DEFAULT: usize = 1000;
/// The most envelopes one `run.events` answer carries, whatever `limit` says.
const EVENTS_LIMIT_MAX: usize = 10_000;
/// Encoded bytes of envelopes in a `run.events` answer when the request has
/// no `max_bytes`.
const EVENTS_BYTES_DEFAULT: usize = 4 * 1024 * 1024;
/// The most encoded bytes one `run.events` answer carries beyond its first
/// envelope; half of [`MAX_LINE_BYTES`], so the message stays under it.
const EVENTS_BYTES_MAX: usize = MAX_LINE_BYTES / 2;
/// Room the answer's other members take in a message.
const EVENTS_MESSAGE_OVERHEAD: usize = 64 * 1024;

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
        let remote = params.config.runs_bash_remotely();
        let remote =
            remote.then(|| Arc::new(HostExec::new(&handle, &self.0.peer)) as Arc<dyn RemoteExec>);
        let opened = params
            .config
            .open_with_remote(tools, hooks, &self.0.pools, remote)
            .await
            .map_err(|error| {
                let error = ErrorObject::from(&error);
                match &self.0.tenant {
                    Some(tenant) => tenant.open_error(error),
                    None => error,
                }
            })?;
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
            journals: Arc::default(),
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
                || tables
                    .workflows
                    .values()
                    .any(|run| run.agent_handle == params.agent)
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

    /// One page of a run's durable envelopes: at most `limit` of them and
    /// `max_bytes` encoded, with the seq to continue from when more follow.
    pub(super) async fn events(&self, params: EventsParams) -> Result<JsonValue, ErrorObject> {
        let limit = match params.limit {
            None => EVENTS_LIMIT_DEFAULT,
            Some(0) => return Err(invalid_params("limit is at least 1")),
            Some(limit) => (limit as usize).min(EVENTS_LIMIT_MAX),
        };
        let max_bytes = match params.max_bytes {
            None => EVENTS_BYTES_DEFAULT,
            Some(0) => return Err(invalid_params("max_bytes is at least 1")),
            Some(bytes) => {
                usize::try_from(bytes).map_or(EVENTS_BYTES_MAX, |b| b.min(EVENTS_BYTES_MAX))
            }
        };
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        let events = agent
            .events(&params.run_id, params.after_seq)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        let (events, next_after_seq) = page(events, limit, max_bytes)?;
        to_value(&Events {
            events,
            next_after_seq,
        })
    }
}

/// The first `limit` envelopes of `events` that fit in `max_bytes` encoded,
/// and the seq of the last one when more follow. The first envelope is
/// always taken, so a reader makes progress; one too long for any message
/// is refused.
fn page(
    mut events: Vec<Envelope>,
    limit: usize,
    max_bytes: usize,
) -> Result<(Vec<Envelope>, Option<u64>), ErrorObject> {
    let mut bytes = 0;
    let mut taken = 0;
    for envelope in &events {
        if taken == limit {
            break;
        }
        let size = encoded_len(envelope);
        if taken == 0 && size > MAX_LINE_BYTES - EVENTS_MESSAGE_OVERHEAD {
            let message = format!(
                "event {} is {size} bytes, more than the {MAX_LINE_BYTES} a message may carry",
                envelope.seq
            );
            return Err(ErrorObject::new(code::INTERNAL_ERROR, message));
        }
        if taken > 0 && bytes + size > max_bytes {
            break;
        }
        bytes += size;
        taken += 1;
    }
    let more = taken < events.len();
    events.truncate(taken);
    let next = events.last().filter(|_| more).map(|envelope| envelope.seq);
    Ok((events, next))
}

/// Bytes `value` takes encoded as JSON, without keeping them.
fn encoded_len<T: serde::Serialize>(value: &T) -> usize {
    struct Count(usize);
    impl std::io::Write for Count {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0 += bytes.len();
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut count = Count(0);
    // Envelopes hold only JSON values, which always encode.
    serde_json::to_writer(&mut count, value).expect("an envelope encodes");
    count.0
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

#[cfg(test)]
mod tests {
    use super::*;

    /// An envelope of an unknown kind, which decodes as ignorable, whose
    /// text is `len` bytes.
    fn envelope(seq: u64, len: usize) -> Envelope {
        let value = json!({
            "v": 1, "seq": seq, "run_id": "r", "ts_ms": 0, "ignorable": true,
            "event": {"type": "filler", "text": "x".repeat(len)},
        });
        Envelope::from_value(value).unwrap()
    }

    fn seqs(events: &[Envelope]) -> Vec<u64> {
        events.iter().map(|envelope| envelope.seq).collect()
    }

    #[test]
    fn a_page_stops_at_the_limit_and_names_the_seq_to_continue_from() {
        let events: Vec<_> = (1..=5).map(|seq| envelope(seq, 10)).collect();
        let (first, next) = page(events.clone(), 2, usize::MAX).unwrap();
        assert_eq!((seqs(&first), next), (vec![1, 2], Some(2)));
        let (rest, next) = page(events, 5, usize::MAX).unwrap();
        assert_eq!((seqs(&rest), next), (vec![1, 2, 3, 4, 5], None));
    }

    #[test]
    fn a_page_stops_before_the_envelope_that_passes_the_byte_bound() {
        let events: Vec<_> = (1..=4).map(|seq| envelope(seq, 1000)).collect();
        let (taken, next) = page(events, 100, 2500).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1, 2], Some(2)));
    }

    #[test]
    fn an_envelope_over_the_byte_bound_is_returned_alone() {
        let events = vec![envelope(1, 5000), envelope(2, 10)];
        let (taken, next) = page(events, 100, 100).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1], Some(1)));
    }

    #[test]
    fn an_envelope_too_long_for_one_message_is_refused_with_its_seq() {
        let events = vec![envelope(1, 10), envelope(2, MAX_LINE_BYTES)];
        let (taken, next) = page(events.clone(), 100, EVENTS_BYTES_MAX).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1], Some(1)));
        let error = page(events[1..].to_vec(), 100, EVENTS_BYTES_MAX).unwrap_err();
        assert_eq!(error.code, code::INTERNAL_ERROR);
        assert!(error.message.contains("event 2 "), "{}", error.message);
    }
}
