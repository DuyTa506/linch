//! Runs over RPC on the scripted provider and memory stores: a full run,
//! concurrent runs, a run id the client chooses, replay from a seq, and
//! run errors in `run.end`.

mod common;

use std::future::pending;

use common::*;
use linch::events::kinds::ResultSubtype;
use linch::{Event, Persistence, RunId};
use linch_rpc::protocol::{Cancelled, Events, method};
use linch_rpc::{ClientError, ErrorObject, code};
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
    let extra = json!({"max_bytes": 8 * 1024 * 1024});
    let pages = read_pages(&rpc, &agent, &run_id, extra).await;
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

/// A request with neither `limit` nor `max_bytes` gets everything that fits
/// in one message, as it did before the members existed: 12 MiB of events
/// is not cut at the 4 MiB an explicit page defaults to.
#[tokio::test]
async fn run_events_without_limits_answers_everything_that_fits_in_a_message() {
    let mut rpc = Rpc::new();
    let text = "x".repeat(6 * 1024 * 1024);
    let agent = rpc.open(scripted(vec![text_turn(&text)], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    let durable = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .count();
    let pages = read_pages(&rpc, &agent, &run_id, json!({})).await;
    let sizes: Vec<usize> = pages.iter().map(Vec::len).collect();
    assert_eq!(sizes, vec![durable], "pages of {sizes:?} envelopes");
}

/// The same for the count: 1000 envelopes is not a bound without `limit`.
#[tokio::test]
async fn run_events_without_limits_answers_more_than_a_thousand_envelopes() {
    let mut rpc = Rpc::new();
    let turns = (0..300)
        .map(|n| tool_turn(&format!("c{n}"), &format!("Missing{n}"), json!({"n": n})))
        .chain([text_turn("Done.")])
        .collect();
    let mut config = scripted(turns, None, "");
    config["spec"]["max_turns"] = json!(400);
    config["spec"]["loop_guard"] = json!({"max_consecutive_failures": 0});
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    let durable = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .count();
    assert!(
        durable > 1000,
        "{durable} durable envelopes, ending {:?}",
        ended.last()
    );
    let pages = read_pages(&rpc, &agent, &run_id, json!({})).await;
    let sizes: Vec<usize> = pages.iter().map(Vec::len).collect();
    assert_eq!(sizes, vec![durable], "pages of {sizes:?} envelopes");
}

/// A prompt 32 KiB under the 16 MiB a message may carry streams live in a
/// `run.event` that fits; `run.events` must answer the same envelope.
#[tokio::test]
async fn a_run_whose_envelopes_streamed_live_can_be_replayed() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("Hi.")], None, "")).await;
    let prompt = "x".repeat(16 * 1024 * 1024 - 32 * 1024);
    let run_id = rpc.start(&agent, "s1", &prompt).await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "the run itself streams fine");
    let durable = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .count();
    let extra = json!({"max_bytes": 8 * 1024 * 1024});
    let pages = read_pages(&rpc, &agent, &run_id, extra).await;
    assert_eq!(pages.iter().map(Vec::len).sum::<usize>(), durable);
}

/// A request without `limit` and `max_bytes` is the whole remainder in one
/// answer or an error: never a first page with a cursor, which a client
/// that does not know the cursor would take for the whole run.
#[tokio::test]
async fn run_events_without_limits_is_the_whole_run_or_an_error() {
    let mut rpc = Rpc::new();
    let text = "x".repeat(9 * 1024 * 1024);
    let agent = rpc.open(scripted(vec![text_turn(&text)], None, "")).await;
    let prompt = "p".repeat(9 * 1024 * 1024);
    let run_id = rpc.start(&agent, "s1", &prompt).await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "the run itself streams fine");
    let params = json!({"agent": agent, "run_id": run_id});
    let answer = rpc.call(method::RUN_EVENTS, params).await;
    let error = match answer {
        Ok(answer) => panic!(
            "{} envelopes and next_after_seq {:?} for a run that is longer than a message",
            answer["events"].as_array().unwrap().len(),
            answer.get("next_after_seq")
        ),
        Err(ClientError::Rpc(error)) => error,
        Err(other) => panic!("{other:?}"),
    };
    assert_eq!(error.code, code::EVENTS_TOO_LONG, "{error:?}");
    assert!(error.message.contains("max_bytes"), "{}", error.message);
    let seq = error.data.as_ref().and_then(|data| data["seq"].as_u64());
    let durable: Vec<u64> = (ended.envelopes.iter())
        .filter(|e| e.event.persistence() == Persistence::Both)
        .map(|e| e.seq)
        .collect();
    assert!(
        durable.contains(&seq.expect("data.seq")),
        "{seq:?} {durable:?}"
    );
    // The error names the way: `limit` or `max_bytes` pages with a cursor.
    for extra in [
        json!({"limit": 1000}),
        json!({"max_bytes": 4 * 1024 * 1024}),
    ] {
        let pages = read_pages(&rpc, &agent, &run_id, extra).await;
        assert!(pages.len() > 1);
        assert_eq!(pages.iter().map(Vec::len).sum::<usize>(), durable.len());
    }
}

/// A `limit` or `max_bytes` above the most is lowered to it, whatever its
/// size; a value that is not a count is `-32602`.
#[tokio::test]
async fn run_events_lowers_any_large_limit_and_refuses_what_is_not_a_count() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(vec![text_turn("Hi.")], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    rpc.until_end(&run_id).await;
    let mut refused = Vec::new();
    for (field, value) in [
        ("limit", json!(10_001)),
        ("limit", json!(u32::MAX)),
        ("limit", json!(u64::from(u32::MAX) + 1)),
        ("limit", json!(u64::MAX)),
        ("max_bytes", json!(u64::MAX)),
    ] {
        let params = json!({"agent": agent, "run_id": run_id, field: value});
        if let Err(error) = rpc.call(method::RUN_EVENTS, params).await {
            refused.push(format!("{field}={value}: {error}"));
        }
    }
    assert!(refused.is_empty(), "not lowered but refused: {refused:#?}");
    for field in ["limit", "max_bytes"] {
        for value in [json!(-1), json!(1.5), json!("5"), json!([1]), json!(true)] {
            let params = json!({"agent": agent, "run_id": run_id, field: value.clone()});
            let error = rpc.call(method::RUN_EVENTS, params).await.unwrap_err();
            assert_eq!(code_of(error), code::INVALID_PARAMS, "{field}={value}");
        }
    }
}

/// The report of a run with one 9 MiB answer is readable: long text in it
/// is cut with a marker and counted, so the answer stays under a message.
#[tokio::test]
async fn run_report_of_a_large_run_can_be_read() {
    let mut rpc = Rpc::new();
    let text = "x".repeat(9 * 1024 * 1024);
    let agent = rpc.open(scripted(vec![text_turn(&text)], None, "")).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "the run itself streams fine");
    for timeline in [false, true] {
        let params = json!({"agent": agent, "run_id": run_id, "timeline": timeline});
        let report = rpc.call("run.report", params).await;
        let report = report.unwrap_or_else(|error| panic!("timeline {timeline}: {error}"));
        assert!(
            report["truncated"]["strings"].as_u64().unwrap() >= 1,
            "{timeline}"
        );
        assert!(report.to_string().len() < 4 * 1024 * 1024, "{timeline}");
    }
}

/// Tool calls with inputs and results just under the 64 KiB a string keeps
/// (nothing to cut) make a report over its 8 MiB outside the timeline: the
/// text of each call is shortened, and the answer says so.
#[tokio::test]
async fn run_report_of_a_run_with_many_tool_results_can_be_read() {
    let result = "r".repeat(60 * 1024);
    let mut rpc = Rpc::with_handler(handler(move |_, _, _| {
        let result = result.clone();
        async move { Ok(json!({"value": result})) }
    }));
    let input = "q".repeat(40 * 1024);
    let mut turns: Vec<_> = (0..90)
        .map(|n| {
            let input = json!({"q": format!("{n} {input}")});
            tool_turn(&format!("call_{n}"), "lookup", input)
        })
        .collect();
    turns.push(text_turn("Done."));
    let tools = json!({"tools": [host_tool("lookup", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let run_id = rpc.start(&agent, "s1", "Go.").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "the run itself streams fine");
    for timeline in [false, true] {
        let params = json!({"agent": agent, "run_id": run_id, "timeline": timeline});
        let report = rpc.call("run.report", params).await;
        let report = report.unwrap_or_else(|error| panic!("timeline {timeline}: {error}"));
        assert!(report.to_string().len() <= 8 * 1024 * 1024, "{timeline}");
        assert!(
            report["truncated"]["tool_call_text"].as_u64().unwrap() >= 1,
            "{timeline}: {}",
            report["truncated"]
        );
        let calls = report["report"]["tool_calls"].as_array().unwrap();
        assert_eq!(calls.len(), 90, "{timeline}: every call is still listed");
    }
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
