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

/// Envelopes in a `run.events` answer when the request names a `max_bytes`
/// but no `limit`.
const EVENTS_LIMIT_DEFAULT: usize = 1000;
/// The most envelopes one `run.events` answer carries, whatever `limit` says.
const EVENTS_LIMIT_MAX: usize = 10_000;
/// The most encoded bytes of envelopes one `run.events` answer carries
/// beyond its first, whatever `max_bytes` says, and what it carries when the
/// request names a `limit` but no `max_bytes`: half of [`MAX_LINE_BYTES`].
const EVENTS_BYTES_MAX: usize = MAX_LINE_BYTES / 2;
/// Bytes the answer's framing takes in a message, at most: the JSON-RPC
/// envelope with a 20 digit id, `events` and a 20 digit `next_after_seq`
/// come to 105 bytes.
const EVENTS_FRAMING_BYTES: usize = 128;
/// The encoded bytes of envelopes (each followed by its comma) that one
/// answer may carry so that the whole message fits [`MAX_LINE_BYTES`]: what a
/// request with neither `limit` nor `max_bytes` gets.
const EVENTS_MESSAGE_BYTES: usize = MAX_LINE_BYTES - EVENTS_FRAMING_BYTES;

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

    /// One page of a run's durable envelopes. A request with neither
    /// `limit` nor `max_bytes` gets every envelope that fits in one message;
    /// otherwise at most `limit` envelopes and `max_bytes` encoded bytes
    /// (each lowered to its most). When more follow, the answer names the
    /// seq to continue from.
    pub(super) async fn events(&self, params: EventsParams) -> Result<JsonValue, ErrorObject> {
        if params.limit == Some(0) {
            return Err(invalid_params("limit is at least 1"));
        }
        if params.max_bytes == Some(0) {
            return Err(invalid_params("max_bytes is at least 1"));
        }
        let whole = params.limit.is_none() && params.max_bytes.is_none();
        let (limit, max_bytes) = if whole {
            (usize::MAX, EVENTS_MESSAGE_BYTES)
        } else {
            (
                lowered(params.limit, EVENTS_LIMIT_DEFAULT, EVENTS_LIMIT_MAX),
                lowered(params.max_bytes, EVENTS_BYTES_MAX, EVENTS_BYTES_MAX),
            )
        };
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        let events = agent
            .events(&params.run_id, params.after_seq)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        let (events, next_after_seq) = page(events, limit, max_bytes, whole)?;
        to_value(&Events {
            events,
            next_after_seq,
        })
    }
}

/// `value`, or `default` when absent, lowered to `most`.
fn lowered(value: Option<u64>, default: usize, most: usize) -> usize {
    value.map_or(default, |value| {
        usize::try_from(value).map_or(most, |value| value.min(most))
    })
}

/// The first `limit` envelopes of `events` that fit in `max_bytes` encoded
/// (a comma counts with each), and the seq of the last one when more
/// follow. The first envelope is always taken, so a reader makes progress;
/// one that cannot fit in a message with its answer's framing is refused,
/// naming its seq. With `whole` the page is all of `events` or an error
/// (`EVENTS_TOO_LONG`, naming the first envelope that did not fit): a
/// cursor is only for a caller that asked to be paged.
fn page(
    mut events: Vec<Envelope>,
    limit: usize,
    max_bytes: usize,
    whole: bool,
) -> Result<(Vec<Envelope>, Option<u64>), ErrorObject> {
    let mut bytes = 0;
    let mut taken = 0;
    for envelope in &events {
        if taken == limit {
            break;
        }
        let size = encoded_len(envelope) + 1;
        if taken == 0 && size > EVENTS_MESSAGE_BYTES {
            let message = format!(
                "event {seq} is {size} bytes; with the answer's framing it is longer than the \
                 {MAX_LINE_BYTES} bytes a message may carry, so run.events cannot return it; \
                 ask again with after_seq {seq} to continue after it",
                seq = envelope.seq
            );
            return Err(ErrorObject::new(code::EVENT_TOO_LARGE, message)
                .with_data(json!({ "seq": envelope.seq })));
        }
        if taken > 0 && bytes + size > max_bytes {
            break;
        }
        bytes += size;
        taken += 1;
    }
    let more = taken < events.len();
    if whole && more {
        let seq = events[taken].seq;
        let message = format!(
            "run.events without limit or max_bytes answers the whole run in one message, and \
             the {total} envelopes after after_seq are longer than one message ({taken} fit; \
             event {seq} does not); ask again with max_bytes or limit to read them in pages, \
             following next_after_seq",
            total = events.len()
        );
        return Err(
            ErrorObject::new(code::EVENTS_TOO_LONG, message).with_data(json!({ "seq": seq }))
        );
    }
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
        let (first, next) = page(events.clone(), 2, usize::MAX, false).unwrap();
        assert_eq!((seqs(&first), next), (vec![1, 2], Some(2)));
        let (rest, next) = page(events, 5, usize::MAX, false).unwrap();
        assert_eq!((seqs(&rest), next), (vec![1, 2, 3, 4, 5], None));
    }

    #[test]
    fn a_page_stops_before_the_envelope_that_passes_the_byte_bound() {
        let events: Vec<_> = (1..=4).map(|seq| envelope(seq, 1000)).collect();
        let (taken, next) = page(events, 100, 2500, false).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1, 2], Some(2)));
    }

    #[test]
    fn an_envelope_over_the_byte_bound_is_returned_alone() {
        let events = vec![envelope(1, 5000), envelope(2, 10)];
        let (taken, next) = page(events, 100, 100, false).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1], Some(1)));
    }

    /// An envelope whose encoding, with its comma, is `size` bytes.
    fn envelope_of(seq: u64, size: usize) -> Envelope {
        let empty = encoded_len(&envelope(seq, 0)) + 1;
        envelope(seq, size - empty)
    }

    /// The answer with one envelope is its framing and the envelope: an
    /// envelope that fits with the framing (as one that streamed live in a
    /// `run.event` does) is returned; one byte past it is refused.
    #[test]
    fn an_envelope_that_fits_a_message_with_its_framing_is_returned_alone() {
        let live = envelope_of(2, MAX_LINE_BYTES - 100_000);
        let (taken, next) =
            page(vec![envelope(1, 10), live], 100, EVENTS_BYTES_MAX, false).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1], Some(1)));
        let edge = envelope_of(2, EVENTS_MESSAGE_BYTES);
        let (taken, next) = page(vec![edge], usize::MAX, EVENTS_MESSAGE_BYTES, true).unwrap();
        assert_eq!((seqs(&taken), next), (vec![2], None));
        let the_answer = json!({"jsonrpc": "2.0", "id": u64::MAX, "result": Events {
            events: taken, next_after_seq: Some(u64::MAX)}});
        assert!(the_answer.to_string().len() < MAX_LINE_BYTES);
    }

    #[test]
    fn a_whole_answer_that_does_not_fit_a_message_is_an_error_naming_the_first_that_did_not() {
        let events = vec![
            envelope(1, 10),
            envelope(2, 10),
            envelope_of(3, EVENTS_MESSAGE_BYTES),
        ];
        let error = page(events.clone(), usize::MAX, EVENTS_MESSAGE_BYTES, true).unwrap_err();
        assert_eq!(error.code, code::EVENTS_TOO_LONG);
        assert_eq!(error.data, Some(json!({"seq": 3})));
        assert!(error.message.contains("max_bytes"), "{}", error.message);
        // The same events paged are not an error.
        let (taken, next) = page(events, usize::MAX, EVENTS_MESSAGE_BYTES, false).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1, 2], Some(2)));
    }

    #[test]
    fn an_envelope_too_long_for_one_message_is_refused_with_its_seq() {
        let too_long = envelope_of(7, EVENTS_MESSAGE_BYTES + 1);
        let error = page(vec![too_long], usize::MAX, EVENTS_MESSAGE_BYTES, true).unwrap_err();
        assert_eq!(error.code, code::EVENT_TOO_LARGE);
        assert_eq!(error.data, Some(json!({"seq": 7})));
        assert!(error.message.contains("after_seq 7"), "{}", error.message);
        // Earlier envelopes are still returned: the refusal waits for its turn.
        let events = vec![envelope(1, 10), envelope_of(2, MAX_LINE_BYTES)];
        let (taken, next) = page(events, 100, EVENTS_MESSAGE_BYTES, false).unwrap();
        assert_eq!((seqs(&taken), next), (vec![1], Some(1)));
    }
}
