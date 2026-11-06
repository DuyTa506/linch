use std::sync::Arc;

use super::*;
use crate::events::kinds::{
    AssistantEvent, ErrorEvent, ResultSubtype, ToolCallEndEvent, ToolCallStartEvent,
    ToolProgressEvent, UsageEvent, UserEvent, UserEventSubtype,
};
use crate::events::model_call::{
    CallDisposition, ModelCallDeliveryEvent, ModelCallKind, ModelCallReceiptEvent,
};
use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, ToolResultBlock, ToolUseBlock};
use crate::run::StopReason;
use crate::session_log::{LogEntry, MessageEntry};
use crate::store::SessionStore;
use crate::store::faulty::{FaultTiming, Faulty};
use crate::store::memory::{MemoryRunStore, MemorySessionStore};
use crate::usage::Usage;

mod model;
mod turn;

const NS: &str = "ns";

fn run_id() -> RunId {
    RunId::new("r1")
}

fn checkpoint(turn: u32, session_len: u64, phase: Phase) -> Checkpoint {
    Checkpoint {
        turn,
        usage: Usage::default(),
        usage_complete: true,
        session_len,
        phase,
    }
}

fn started() -> Checkpoint {
    let prompt = Message::user_text("hi");
    checkpoint(0, 0, Phase::Started { prompt })
}

fn durable(n: u64) -> Event {
    let mut error = JsonObject::new();
    error.insert("n".into(), n.into());
    Event::Error(ErrorEvent { error })
}

fn receipt(id: &str, disposition: CallDisposition) -> Event {
    Event::ModelCallReceipt(ModelCallReceiptEvent {
        run_id: run_id(),
        logical_call_id: "lc".into(),
        delivery_attempt_id: id.into(),
        kind: ModelCallKind::Primary,
        actual_model: None,
        disposition,
        usage: None,
        cost_usd: None,
        duration_ms: 1,
    })
}

fn progress() -> Event {
    Event::ToolProgress(ToolProgressEvent {
        tool_use_id: "c1".into(),
        tool_name: "Read".into(),
        message: "half way".into(),
        data: None,
    })
}

/// A created and claimed run behind `store`, with a fresh journal.
async fn open(store: Arc<dyn RunStore>) -> Journal {
    let lease = Lease {
        namespace: NS.into(),
        run_id: run_id(),
        owner: "w".into(),
        epoch: 0,
    };
    let mut journal = Journal::new(store, lease, 0);
    let contract = "0".repeat(64).parse().unwrap();
    let ready = journal.create(SessionId::new("s"), None, contract, started());
    assert!(ready.await.unwrap().is_empty());
    journal
}

fn seqs(envelopes: &[Envelope]) -> Vec<u64> {
    envelopes.iter().map(|e| e.seq).collect()
}

#[tokio::test]
async fn create_writes_the_staged_events_and_claims_the_run() {
    let store = Arc::new(MemoryRunStore::new());
    let lease = Lease {
        namespace: NS.into(),
        run_id: run_id(),
        owner: "w".into(),
        epoch: 0,
    };
    let mut journal = Journal::new(store.clone(), lease, 0);
    journal.stage(durable(1));
    journal.stage(receipt("a", CallDisposition::Accepted));
    let contract = "0".repeat(64).parse().unwrap();
    let ready = journal.create(SessionId::new("s"), None, contract, started());
    assert_eq!(
        seqs(&ready.await.unwrap()),
        [1],
        "journal-only is not delivered"
    );
    let record = store.load(NS, &run_id()).await.unwrap();
    assert_eq!(
        (record.status, record.epoch, record.last_seq),
        (RunStatus::Running, 1, 2)
    );
    assert_eq!(journal.lease().epoch, 1);
    assert_eq!(journal.durable_seq(), 2);
}

#[tokio::test]
async fn an_empty_flush_writes_nothing() {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    let before = store.writes();
    assert!(journal.flush().await.unwrap().is_empty());
    assert!(journal.flush_checkpoint().await.unwrap().is_empty());
    assert_eq!(journal.stage(progress()).map(|e| e.seq), Some(0));
    assert!(journal.flush().await.unwrap().is_empty());
    assert_eq!(store.writes(), before);
}

#[tokio::test]
async fn durable_events_reach_the_caller_only_after_their_write() {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    assert!(journal.stage(durable(1)).is_none());
    assert!(
        journal
            .stage(receipt("a", CallDisposition::Failed))
            .is_none()
    );
    assert_eq!(journal.last_seq(), 2);
    assert_eq!(
        journal.stage(progress()).unwrap().seq,
        0,
        "last durable seq"
    );
    let ready = journal.flush().await.unwrap();
    assert_eq!(seqs(&ready), [1]);
    assert_eq!(ready[0].event, durable(1));
    assert_eq!(journal.stage(progress()).unwrap().seq, 2);
    let stored = store.events(NS, &run_id(), 0).await.unwrap();
    assert_eq!(seqs(&stored), [1, 2], "stream-only events are never stored");
}

#[tokio::test]
async fn save_writes_through_or_holds_by_phase() {
    let message = Message::assistant_text("a");
    let phases = [
        (
            Phase::Started {
                prompt: message.clone(),
            },
            true,
        ),
        (
            Phase::AssistantReceived {
                message: message.clone(),
                stop_reason: StopReason::EndTurn,
            },
            true,
        ),
        (
            Phase::AwaitingApproval {
                decisions: Vec::new(),
                pending: vec!["c1".into()],
            },
            true,
        ),
        (Phase::ProviderPending { attempt: 1 }, false),
        (
            Phase::ToolBatch {
                decisions: Vec::new(),
                after_seq: 1,
            },
            false,
        ),
        (Phase::ToolResults { message }, false),
    ];
    for (phase, through) in phases {
        let store = Arc::new(MemoryRunStore::new());
        let mut journal = open(store.clone()).await;
        journal.stage(durable(1));
        let saved = checkpoint(1, 1, phase);
        let ready = journal.save(saved.clone()).await.unwrap();
        let record = store.load(NS, &run_id()).await.unwrap();
        let landed = record.last_seq == 1 && record.checkpoint == saved;
        assert_eq!(
            (landed, ready.len()),
            (through, usize::from(through)),
            "{saved:?}"
        );
        assert_eq!(store.writes().commit, u64::from(through));
    }
}

#[tokio::test]
async fn a_held_checkpoint_covers_only_the_events_staged_before_it() {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    journal.stage(durable(1));
    let held = checkpoint(
        1,
        0,
        Phase::ToolResults {
            message: Message::user_text("results"),
        },
    );
    assert!(journal.save(held.clone()).await.unwrap().is_empty());
    journal.stage(durable(2));
    assert_eq!(store.writes().commit, 0, "held, not written");
    assert_eq!(seqs(&journal.flush_checkpoint().await.unwrap()), [1]);
    let record = store.load(NS, &run_id()).await.unwrap();
    assert_eq!((record.last_seq, &record.checkpoint), (1, &held));
    assert_eq!((journal.durable_seq(), journal.last_seq()), (1, 2));
    assert_eq!(seqs(&journal.flush().await.unwrap()), [2]);
    let record = store.load(NS, &run_id()).await.unwrap();
    assert_eq!((record.last_seq, &record.checkpoint), (2, &held));
    assert_eq!(store.writes().commit, 2);
}

#[tokio::test]
async fn awaiting_approval_writes_through_and_suspends_the_run() {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    journal.stage(durable(1));
    let phase = Phase::AwaitingApproval {
        decisions: Vec::new(),
        pending: vec!["c1".into()],
    };
    let ready = journal.save(checkpoint(1, 2, phase)).await.unwrap();
    assert_eq!(seqs(&ready), [1]);
    let record = store.load(NS, &run_id()).await.unwrap();
    assert_eq!(record.status, RunStatus::AwaitingApproval);
    journal.stage(durable(2));
    let error = journal.flush().await.unwrap_err();
    assert!(matches!(
        error,
        JournalError::Store(StoreError::InvalidTransition { .. })
    ));
    assert!(matches!(journal.flush().await, Err(JournalError::Poisoned)));
}

#[tokio::test]
async fn finish_commits_the_held_state_before_the_result() {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    let result = ResultEvent {
        subtype: ResultSubtype::Success,
        stop_reason: StopReason::EndTurn,
        total_usage: Usage::default(),
        usage_complete: true,
        final_text: Some("done".into()),
        structured_output: None,
        structured_error: None,
        trace_id: None,
    };
    let refused = journal
        .finish(RunStatus::AwaitingInput, result.clone())
        .await;
    assert!(matches!(refused, Err(JournalError::NotTerminal(_))));
    journal.stage(durable(1));
    let held = checkpoint(1, 1, Phase::ProviderPending { attempt: 1 });
    journal.save(held.clone()).await.unwrap();
    let ready = journal.finish(RunStatus::Completed, result.clone()).await;
    let ready = ready.unwrap();
    assert_eq!(seqs(&ready), [1, 2]);
    assert_eq!(ready[1].event, Event::Result(result));
    assert_eq!(
        store.writes().commit,
        2,
        "held state, then status and result"
    );
    let record = store.load(NS, &run_id()).await.unwrap();
    let state = (record.status, record.last_seq, &record.checkpoint);
    assert_eq!(state, (RunStatus::Completed, 2, &held));
}

#[tokio::test]
async fn finish_with_nothing_held_writes_the_pending_events_with_the_result() {
    let store = Arc::new(MemoryRunStore::new());
    let mut journal = open(store.clone()).await;
    let result = ResultEvent {
        subtype: ResultSubtype::Error,
        stop_reason: StopReason::Error,
        total_usage: Usage::default(),
        usage_complete: false,
        final_text: None,
        structured_output: None,
        structured_error: None,
        trace_id: None,
    };
    journal.stage(receipt("d1", CallDisposition::Failed));
    journal.stage(durable(2));
    let ready = journal.finish(RunStatus::Failed, result).await.unwrap();
    assert_eq!(seqs(&ready), [2, 3], "the receipt is journal-only");
    assert_eq!(store.writes().commit, 1, "one terminal write");
    let record = store.load(NS, &run_id()).await.unwrap();
    assert_eq!((record.status, record.last_seq), (RunStatus::Failed, 3));
}

#[tokio::test]
async fn a_failed_write_poisons_the_journal() {
    let inner = Arc::new(MemoryRunStore::new());
    let store = Arc::new(Faulty::new(inner.clone(), 3, FaultTiming::Before));
    let mut journal = open(store).await;
    journal.stage(durable(1));
    let error = journal.flush().await.unwrap_err();
    assert!(matches!(error, JournalError::Store(StoreError::Backend(_))));
    journal.stage(durable(2));
    assert!(matches!(journal.flush().await, Err(JournalError::Poisoned)));
    assert!(matches!(
        journal.save(started()).await,
        Err(JournalError::Poisoned)
    ));
    assert_eq!(inner.load(NS, &run_id()).await.unwrap().last_seq, 0);
    assert_eq!(inner.writes().commit, 0);
}
