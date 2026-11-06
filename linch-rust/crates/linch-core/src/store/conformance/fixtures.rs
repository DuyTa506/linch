//! Records and assertions shared by the conformance checks.

use std::sync::Arc;

use crate::canonical::Digest;
use crate::events::kinds::ErrorEvent;
use crate::events::{Envelope, Event, WIRE_VERSION};
use crate::ids::{RunId, SessionId};
use crate::message::Message;
use crate::run::{Checkpoint, Phase, RunStatus};
use crate::session_log::{LogEntry, MessageEntry};
use crate::store::{Commit, Lease, NewRun, RunRecord};
use crate::usage::Usage;

pub(super) fn same_record(actual: &RunRecord, expected: &RunRecord) {
    let key = |r: &RunRecord| {
        let ids = (r.namespace.clone(), r.run_id.clone(), r.session_id.clone());
        (ids, r.status, r.epoch, r.last_seq, r.checkpoint.clone())
    };
    assert_eq!(key(actual), key(expected), "record unchanged");
}

/// Events-only, checkpoint-only, status-only and empty commits.
pub(super) fn commit_shapes(next_seq: u64) -> Vec<Commit> {
    vec![
        Commit {
            events: vec![envelope("r1", next_seq)],
            ..Commit::default()
        },
        Commit {
            checkpoint: Some(checkpoint(2)),
            ..Commit::default()
        },
        Commit {
            status: Some(RunStatus::Running),
            ..Commit::default()
        },
        Commit::default(),
    ]
}

pub(super) fn new_run(namespace: &str, run_id: &str, events: u64) -> NewRun {
    NewRun {
        namespace: namespace.into(),
        run_id: RunId::new(run_id),
        // A session per run: one session holds one live run at a time.
        session_id: SessionId::new(format!("s-{run_id}")),
        parent_run_id: Some(RunId::new("parent")),
        contract: contract(),
        checkpoint: Checkpoint {
            phase: Phase::Started {
                prompt: Message::user_text("hi"),
            },
            ..checkpoint(0)
        },
        events: (1..=events).map(|seq| envelope(run_id, seq)).collect(),
    }
}

pub(super) fn lease_for(namespace: &str, run_id: &str, epoch: u64) -> Lease {
    Lease {
        namespace: namespace.into(),
        run_id: RunId::new(run_id),
        owner: "w".into(),
        epoch,
    }
}

pub(super) fn envelope(run_id: &str, seq: u64) -> Envelope {
    let mut error = crate::json::JsonObject::new();
    error.insert("seq".into(), seq.into());
    Envelope {
        v: WIRE_VERSION,
        seq,
        run_id: RunId::new(run_id),
        ignorable: false,
        event: Event::Error(ErrorEvent { error }),
    }
}

pub(super) fn checkpoint(turn: u32) -> Checkpoint {
    Checkpoint {
        turn,
        usage: Usage::default(),
        usage_complete: true,
        session_len: u64::from(turn),
        phase: Phase::ProviderPending { attempt: 1 },
    }
}

pub(super) fn checkpoint_at(turn: u32) -> Checkpoint {
    Checkpoint {
        usage_complete: false,
        ..checkpoint(turn)
    }
}

pub(super) fn contract() -> Digest {
    "0".repeat(64).parse().expect("a valid digest")
}

pub(super) fn entry(text: &str) -> LogEntry {
    LogEntry::Message(MessageEntry {
        message: Arc::new(Message::user_text(text)),
        visible: true,
        historical: true,
    })
}
