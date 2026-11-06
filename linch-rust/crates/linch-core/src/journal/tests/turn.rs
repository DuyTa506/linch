//! A whole tool turn through the journal: its write count, and recovery
//! after a crash on each of its writes.

use super::*;

fn tool_call(i: usize) -> ToolUseBlock {
    ToolUseBlock {
        id: format!("c{i}"),
        name: "Read".into(),
        input: JsonObject::new(),
    }
}

fn assistant(k: usize) -> Message {
    let calls = (0..k).map(|i| ContentBlock::ToolUse(tool_call(i)));
    Message::new(Role::Assistant, calls.collect())
}

fn entry(message: Message) -> Vec<LogEntry> {
    vec![LogEntry::Message(MessageEntry {
        message: Arc::new(message),
        visible: true,
        historical: true,
    })]
}

/// One tool turn with `k` tools, placing the barriers as the runner does.
async fn tool_turn(
    journal: &mut Journal,
    sessions: &dyn SessionStore,
    k: usize,
) -> Result<(), JournalError> {
    let sid = SessionId::new("s");
    journal
        .save(checkpoint(0, 1, Phase::ProviderPending { attempt: 1 }))
        .await?;
    journal.stage(Event::ModelCallDelivery(ModelCallDeliveryEvent {
        run_id: run_id(),
        logical_call_id: "lc".into(),
        delivery_attempt_id: "a1".into(),
        kind: ModelCallKind::Primary,
        turn_index: Some(0),
        requested_model: "m".into(),
    }));
    journal.flush().await?;
    let message = assistant(k);
    journal.stage(receipt("a1", CallDisposition::Accepted));
    journal.stage(Event::Assistant(AssistantEvent {
        message: message.clone(),
        stop_reason: StopReason::ToolUse,
    }));
    journal.stage(Event::Usage(UsageEvent {
        usage: Usage::default(),
        cumulative: Usage::default(),
        cost_usd: None,
        cumulative_cost_usd: None,
    }));
    let phase = Phase::AssistantReceived {
        message: message.clone(),
        stop_reason: StopReason::ToolUse,
    };
    journal.save(checkpoint(0, 1, phase)).await?;
    journal.flush_checkpoint().await?;
    sessions.append(NS, &sid, 1, entry(message.clone())).await?;
    let phase = Phase::ToolBatch {
        decisions: Vec::new(),
        after_seq: journal.last_seq(),
    };
    journal.save(checkpoint(0, 2, phase)).await?;
    let mut results = Vec::new();
    for call in message.tool_uses() {
        journal.stage(Event::ToolCallStart(ToolCallStartEvent {
            tool_use_id: call.id.clone(),
            tool_name: call.name.clone(),
            input: JsonObject::new(),
            summary: String::new(),
        }));
        journal.flush().await?;
        let result = ToolResultBlock::text(&call.id, "ok", false);
        journal.stage(Event::ToolCallEnd(ToolCallEndEvent {
            tool_name: call.name.clone(),
            result: result.clone(),
            duration_ms: 1,
            canonical_output: None,
            metadata: JsonObject::new(),
        }));
        journal.flush().await?;
        results.push(ContentBlock::ToolResult(result));
    }
    let results = Message::new(Role::User, results);
    journal.stage(Event::User(UserEvent {
        message: results.clone(),
        subtype: UserEventSubtype::ToolResult,
    }));
    let phase = Phase::ToolResults {
        message: results.clone(),
    };
    journal.save(checkpoint(0, 2, phase)).await?;
    journal.flush_checkpoint().await?;
    sessions.append(NS, &sid, 2, entry(results)).await?;
    Ok(())
}

async fn sessions_with_prompt() -> MemorySessionStore {
    let sessions = MemorySessionStore::new();
    let sid = SessionId::new("s");
    sessions.create(NS, &sid).await.unwrap();
    sessions
        .append(NS, &sid, 0, entry(Message::user_text("hi")))
        .await
        .unwrap();
    sessions
}

#[tokio::test]
async fn a_tool_turn_costs_three_plus_two_k_run_writes_and_two_session_writes() {
    for k in 1..=3 {
        let store = Arc::new(MemoryRunStore::new());
        let sessions = sessions_with_prompt().await;
        let mut journal = open(store.clone()).await;
        let (runs, logs) = (store.writes(), sessions.writes());
        tool_turn(&mut journal, &sessions, k).await.unwrap();
        let commits = store.writes().commit - runs.commit;
        assert_eq!(commits, 3 + 2 * k as u64, "k = {k}");
        assert_eq!(sessions.writes().append - logs.append, 2, "k = {k}");
        let record = store.load(NS, &run_id()).await.unwrap();
        assert_eq!(record.last_seq, journal.last_seq(), "nothing left pending");
    }
}

/// Crash on run write `n` of a 2-tool turn (after create and claim), then
/// reconcile from what the stores hold.
async fn crash_at(write: u64, timing: FaultTiming) -> Recovery {
    let inner = Arc::new(MemoryRunStore::new());
    let store = Arc::new(Faulty::new(inner.clone(), write + 2, timing));
    let sessions = sessions_with_prompt().await;
    let mut journal = open(store).await;
    let stopped = tool_turn(&mut journal, &sessions, 2).await;
    assert!(
        matches!(stopped, Err(JournalError::Store(_))),
        "the turn stops"
    );
    let record = inner.load(NS, &run_id()).await.unwrap();
    let events = inner.events(NS, &run_id(), 0).await.unwrap();
    let log = sessions.load(NS, &SessionId::new("s")).await.unwrap();
    reconcile(&record.checkpoint, &events, &log).unwrap()
}

#[tokio::test]
async fn a_crash_recovers_from_the_durable_state() {
    let received = crash_at(2, FaultTiming::After).await;
    assert!(matches!(
        received,
        Recovery::Received {
            appended: false,
            ..
        }
    ));
    let redispatch = crash_at(2, FaultTiming::Before).await;
    assert!(matches!(
        redispatch,
        Recovery::Redispatch {
            attempt: 2,
            abandoned: Some(_)
        }
    ));

    let Recovery::ToolBatch { calls, .. } = crash_at(3, FaultTiming::After).await else {
        panic!("tool batch expected");
    };
    let interrupted = ToolResultBlock::text("c0", INTERRUPTED_TOOL_TEXT, true);
    assert_eq!(
        calls,
        [
            ToolRecovery::Interrupted {
                tool_name: "Read".into(),
                result: interrupted
            },
            ToolRecovery::Replan(tool_call(1))
        ]
    );
    // The held batch checkpoint never landed: the response is already in
    // the session, and the batch is planned again from the start.
    let replan = crash_at(3, FaultTiming::Before).await;
    assert!(matches!(replan, Recovery::Received { appended: true, .. }));
    let Recovery::ToolBatch { calls, .. } = crash_at(4, FaultTiming::After).await else {
        panic!("tool batch expected");
    };
    let done = ToolResultBlock::text("c0", "ok", false);
    assert_eq!(
        calls,
        [ToolRecovery::Done(done), ToolRecovery::Replan(tool_call(1))]
    );

    let results = crash_at(7, FaultTiming::After).await;
    assert!(matches!(
        results,
        Recovery::ToolResults {
            appended: false,
            ..
        }
    ));
}
