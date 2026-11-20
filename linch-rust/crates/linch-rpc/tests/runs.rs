//! Runs over RPC on the scripted provider and memory stores: a full run,
//! concurrent runs, a run id the client chooses, replay from a seq, and
//! run errors in `run.end`.

mod common;

use std::future::pending;

use common::*;
use linch::events::kinds::ResultSubtype;
use linch::{Event, Persistence, RunId};
use linch_rpc::protocol::{Cancelled, Events, method};
use linch_rpc::{ErrorObject, code};
use serde_json::{Value, json};

fn subtype(event: &Event) -> ResultSubtype {
    match event {
        Event::Result(result) => result.subtype,
        other => panic!("expected a result, got {other:?}"),
    }
}

/// An agent that reads `notes.txt` from a workspace, then answers.
async fn reader(rpc: &Rpc, dir: &tempfile::TempDir) -> String {
    std::fs::write(dir.path().join("notes.txt"), "alpha\n").unwrap();
    let turns = vec![
        tool_turn("call_1", "Read", json!({"file_path": "notes.txt"})),
        text_turn("It says alpha."),
    ];
    rpc.open(scripted(turns, Some(dir.path()), "default")).await
}

#[tokio::test]
async fn a_run_streams_every_envelope_then_ends() {
    let dir = tempfile::tempdir().unwrap();
    let mut rpc = Rpc::new();
    let agent = reader(&rpc, &dir).await;
    let run_id = rpc.start(&agent, "s1", "Read notes.txt").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let kinds = ended.kinds();
    assert_eq!(kinds[..2], ["init", "user"]);
    assert!(kinds.contains(&"tool_call_end".to_owned()), "{kinds:?}");
    assert_eq!(subtype(ended.last()), ResultSubtype::Success);
    let Event::Result(result) = ended.last() else {
        unreachable!()
    };
    assert_eq!(result.final_text.as_deref(), Some("It says alpha."));
    assert!(ended.envelopes.iter().all(|e| e.run_id == run_id));
}

#[tokio::test]
async fn run_start_stores_the_trace_id_and_echoes_it() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("done")], None, "")).await;
    let params =
        json!({"agent": agent, "session_id": "s1", "prompt": "go", "trace_id": "4bf92f35"});
    let run_id = rpc.accepted(method::RUN_START, params).await;
    let ended = rpc.until_end(&run_id).await;
    let Event::Init(init) = &ended.envelopes[0].event else {
        panic!("{:?}", ended.envelopes[0])
    };
    assert_eq!(init.trace_id.as_deref(), Some("4bf92f35"));
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(result.trace_id.as_deref(), Some("4bf92f35"));
}

#[tokio::test]
async fn runs_of_two_agents_progress_concurrently() {
    let mut rpc = Rpc::new();
    let first = rpc.open(scripted(vec![text_turn("one")], None, "")).await;
    let second = rpc.open(scripted(vec![text_turn("two")], None, "")).await;
    assert_ne!(first, second);
    let a = rpc.start(&first, "s1", "go").await;
    let b = rpc.start(&second, "s1", "go").await;
    let (b_end, a_end) = (rpc.until_end(&b).await, rpc.until_end(&a).await);
    for (ended, text) in [(a_end, "one"), (b_end, "two")] {
        let Event::Result(result) = ended.last() else {
            panic!("{:?}", ended.last())
        };
        assert_eq!(result.final_text.as_deref(), Some(text));
    }
}

#[tokio::test]
async fn run_start_keeps_the_run_id_the_client_chose() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("done")], None, "")).await;
    let start = |session: &str| json!({"agent": agent, "session_id": session, "prompt": "go", "run_id": "chosen-1"});
    let run_id = rpc.accepted(method::RUN_START, start("s1")).await;
    assert_eq!(run_id, RunId::new("chosen-1"));
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    assert_eq!(subtype(ended.last()), ResultSubtype::Success);
    assert!(ended.envelopes.iter().all(|e| e.run_id == run_id));

    // The id is stored now: a second start with it writes nothing.
    let again = rpc.accepted(method::RUN_START, start("s2")).await;
    let ended = rpc.until_end(&again).await;
    assert!(ended.envelopes.is_empty());
    assert_eq!(ended.error.expect("conflict").code, code::CONFLICT);
}

#[tokio::test]
async fn a_chosen_run_id_the_connection_drives_is_busy() {
    let mut rpc = Rpc::with_handler(handler(|_, _, _| pending::<Result<Value, ErrorObject>>()));
    let turns = vec![
        tool_turn("call_1", "lookup", json!({"q": "x"})),
        text_turn("unused"),
    ];
    let tools = json!({"tools": [host_tool("lookup", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let start = |session: &str| json!({"agent": agent, "session_id": session, "prompt": "go", "run_id": "held"});
    let held = rpc.accepted(method::RUN_START, start("s1")).await;
    loop {
        let envelope = rpc.next_event(&held).await.expect("still running");
        if matches!(envelope.event, Event::ToolCallStart(_)) {
            break;
        }
    }
    let busy = rpc.call(method::RUN_START, start("s2")).await.unwrap_err();
    assert_eq!(code_of(busy), code::RUN_BUSY);

    // The held run is still the one driven, so cancel reaches it.
    let cancel = json!({"run_id": held});
    let cancelled: Cancelled = rpc.client.call(method::RUN_CANCEL, &cancel).await.unwrap();
    assert!(cancelled.cancelled);
    let ended = rpc.until_end(&held).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    assert_eq!(subtype(ended.last()), ResultSubtype::Aborted);
}

#[tokio::test]
async fn run_events_replays_the_durable_envelopes_after_a_seq() {
    let dir = tempfile::tempdir().unwrap();
    let mut rpc = Rpc::new();
    let agent = reader(&rpc, &dir).await;
    let run_id = rpc.start(&agent, "s1", "Read notes.txt").await;
    let durable: Vec<_> = (rpc.until_end(&run_id).await.envelopes.into_iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .collect();
    assert!(durable.len() > 4);
    let events = |after_seq: u64| {
        let params = json!({"agent": agent, "run_id": run_id, "after_seq": after_seq});
        let client = &rpc.client;
        async move { client.call::<_, Events>(method::RUN_EVENTS, &params).await }
    };
    assert_eq!(events(0).await.unwrap().events, durable);
    let cut = durable[2].seq;
    let tail: Vec<_> = durable.iter().filter(|e| e.seq > cut).cloned().collect();
    assert_eq!(events(cut).await.unwrap().events, tail);
    let last = durable.last().unwrap().seq;
    assert!(events(last).await.unwrap().events.is_empty());
}

/// Read a whole run page by page, following `next_after_seq`; every page
/// is one answer that the client accepted.
async fn read_pages(rpc: &Rpc, agent: &str, run_id: &RunId, extra: Value) -> Vec<Vec<Value>> {
    let mut pages = Vec::new();
    let mut after_seq = 0_u64;
    loop {
        let mut params = json!({"agent": agent, "run_id": run_id, "after_seq": after_seq});
        for (name, value) in extra.as_object().unwrap() {
            params[name] = value.clone();
        }
        let answer = rpc.call(method::RUN_EVENTS, params).await;
        let answer = answer.unwrap_or_else(|error| panic!("run.events failed: {error}"));
        pages.push(answer["events"].as_array().unwrap().clone());
        match answer.get("next_after_seq") {
            Some(next) => after_seq = next.as_u64().unwrap(),
            None => return pages,
        }
        assert!(pages.len() < 1000, "the cursor does not advance");
    }
}

/// A run whose two largest envelopes are 9 MiB each: one answer with all of
/// them is past the 16 MiB a message may carry.
#[tokio::test]
async fn run_events_of_a_large_run_can_be_read_page_by_page() {
    let mut rpc = Rpc::new();
    let text = "x".repeat(9 * 1024 * 1024);
    let agent = rpc.open(scripted(vec![text_turn(&text)], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "the run itself streams fine");
    let durable = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .count();
    let pages = read_pages(&rpc, &agent, &run_id, json!({})).await;
    assert!(pages.len() > 1, "{} page for a run of 18 MiB", pages.len());
    let read: Vec<_> = pages.iter().flatten().collect();
    assert_eq!(read.len(), durable);
    let seqs: Vec<u64> = read.iter().map(|e| e["seq"].as_u64().unwrap()).collect();
    assert!(seqs.windows(2).all(|pair| pair[0] < pair[1]), "{seqs:?}");
}

#[tokio::test]
async fn run_events_with_a_limit_answers_one_page_and_the_next_seq() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("Hi.")], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    let durable: Vec<_> = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .collect();
    assert!(durable.len() > 3);
    let params = json!({"agent": agent, "run_id": run_id, "limit": 2});
    let first = rpc.call(method::RUN_EVENTS, params).await.unwrap();
    assert_eq!(first["events"].as_array().unwrap().len(), 2);
    assert_eq!(first["next_after_seq"], json!(durable[1].seq));
    let pages = read_pages(&rpc, &agent, &run_id, json!({"limit": 2})).await;
    assert!(pages.iter().all(|page| page.len() <= 2));
    assert_eq!(pages.iter().map(Vec::len).sum::<usize>(), durable.len());
    // No limit: a small run is one page and carries no cursor.
    let whole = json!({"agent": agent, "run_id": run_id});
    let whole = rpc.call(method::RUN_EVENTS, whole).await.unwrap();
    assert_eq!(whole["events"].as_array().unwrap().len(), durable.len());
    assert!(whole.get("next_after_seq").is_none(), "{whole}");
    // A limit that takes the rest is the last page.
    let rest = json!({"agent": agent, "run_id": run_id, "limit": durable.len()});
    let rest = rpc.call(method::RUN_EVENTS, rest).await.unwrap();
    assert!(rest.get("next_after_seq").is_none(), "{rest}");
}

/// An envelope over the byte bound is still returned, alone, so a reader
/// always makes progress.
#[tokio::test]
async fn run_events_with_a_byte_bound_below_one_event_returns_one_event_per_page() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("Hi.")], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    let durable = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .count();
    let pages = read_pages(&rpc, &agent, &run_id, json!({"max_bytes": 1})).await;
    assert!(pages.iter().all(|page| page.len() == 1));
    assert_eq!(pages.len(), durable);
}

/// The members added for paging stay off the wire while unset.
#[test]
fn unset_run_events_paging_members_are_not_on_the_wire() {
    use linch_rpc::protocol::EventsParams;
    let params = EventsParams {
        agent: "agent-1".into(),
        run_id: RunId::new("r1"),
        after_seq: 0,
        limit: None,
        max_bytes: None,
    };
    let wire = r#"{"agent":"agent-1","run_id":"r1","after_seq":0}"#;
    assert_eq!(serde_json::to_string(&params).unwrap(), wire);
    assert_eq!(serde_json::from_str::<EventsParams>(wire).unwrap(), params);
    let events = Events {
        events: vec![],
        next_after_seq: None,
    };
    assert_eq!(serde_json::to_string(&events).unwrap(), r#"{"events":[]}"#);
}

#[tokio::test]
async fn run_events_refuses_a_zero_limit_or_byte_bound() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("Hi.")], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    rpc.until_end(&run_id).await;
    for field in ["limit", "max_bytes"] {
        let params = json!({"agent": agent, "run_id": run_id, field: 0});
        let error = rpc.call(method::RUN_EVENTS, params).await.unwrap_err();
        assert_eq!(code_of(error), code::INVALID_PARAMS, "{field}");
    }
}

#[tokio::test]
async fn resuming_a_finished_run_ends_with_terminal() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("done")], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    rpc.until_end(&run_id).await;
    let params = json!({"agent": agent, "run_id": run_id});
    let resumed = rpc.accepted(method::RUN_RESUME, params).await;
    assert_eq!(resumed, run_id);
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.envelopes.is_empty());
    let error = ended.error.expect("terminal");
    assert_eq!(error.code, code::TERMINAL);
    assert_eq!(error.data, Some(json!({"status": "completed"})));
}

#[tokio::test]
async fn an_unknown_run_or_agent_is_reported() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![], None, "")).await;
    let missing = RunId::new("no-such-run");
    let params = json!({"agent": agent, "run_id": missing});
    rpc.accepted(method::RUN_RESUME, params.clone()).await;
    let error = rpc.until_end(&missing).await.error.expect("not found");
    assert_eq!(error.code, code::NOT_FOUND);
    let error = rpc.call(method::RUN_EVENTS, params).await.unwrap_err();
    assert!(matches!(error, linch_rpc::ClientError::Rpc(e) if e.code == code::NOT_FOUND));
    let params = json!({"agent": "agent-99", "session_id": "s", "prompt": "p"});
    let error = rpc.call(method::RUN_START, params).await.unwrap_err();
    assert!(matches!(error, linch_rpc::ClientError::Rpc(e) if e.code == code::AGENT_NOT_FOUND));
}

#[tokio::test]
async fn agent_open_reports_build_and_key_errors() {
    let rpc = Rpc::new();
    let mut blank = scripted(vec![], None, "");
    blank["spec"]["model"] = json!(" ");
    let error = rpc.call(method::AGENT_OPEN, blank).await.unwrap_err();
    assert!(matches!(error, linch_rpc::ClientError::Rpc(e) if e.code == code::BUILD_INVALID));
    let mut workspace = scripted(vec![], None, "");
    workspace["workspace"] = json!("/nonexistent/linch-p11");
    let error = rpc.call(method::AGENT_OPEN, workspace).await.unwrap_err();
    assert!(matches!(error, linch_rpc::ClientError::Rpc(e) if e.code == code::BUILD_INVALID));
    if std::env::var_os("GEMINI_API_KEY").is_none() {
        let mut gemini = scripted(vec![], None, "");
        gemini["provider"] = json!({"id": "gemini"});
        let error = rpc.call(method::AGENT_OPEN, gemini).await.unwrap_err();
        let linch_rpc::ClientError::Rpc(error) = error else {
            panic!("{error:?}")
        };
        assert_eq!(error.code, code::PROVIDER_UNAVAILABLE);
        assert_eq!(error.data, Some(json!({"env": "GEMINI_API_KEY"})));
    }
}

#[tokio::test]
async fn an_unreachable_postgres_fails_agent_open_fast() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let rpc = Rpc::new();
    let mut config = scripted(vec![], None, "");
    let dsn = format!("postgres://linch:pg-secret@127.0.0.1:{port}/linch");
    config["stores"] = json!({"type": "postgres", "dsn": dsn});
    let started = std::time::Instant::now();
    let error = rpc.call(method::AGENT_OPEN, config).await.unwrap_err();
    // sqlx alone would retry the refused connection for 30 s.
    assert!(
        started.elapsed() < std::time::Duration::from_secs(15),
        "{:?}",
        started.elapsed()
    );
    let linch_rpc::ClientError::Rpc(error) = error else {
        panic!("{error:?}")
    };
    assert_eq!(error.code, code::STORE_UNAVAILABLE);
    assert!(!error.message.contains("pg-secret"), "{}", error.message);
}
