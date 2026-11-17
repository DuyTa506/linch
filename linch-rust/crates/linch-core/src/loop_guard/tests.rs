use serde_json::json;

use super::*;
use crate::events::WIRE_VERSION;
use crate::events::kinds::{AssistantEvent, ToolCallEndEvent, UserEvent};
use crate::events::model_call::ModelCallDeliveryEvent;
use crate::ids::RunId;
use crate::json::JsonObject;
use crate::message::{Message, Role, ToolResultBlock, ToolUseBlock};
use crate::run::StopReason;

/// A batch of `calls`, each answered with a result that failed or not.
fn batch(watch: &mut Watch, calls: &[(&str, serde_json::Value, bool)]) {
    let asked: Vec<_> = calls.iter().map(|(n, i, _)| (*n, i.clone())).collect();
    watch.see(&calling(&asked));
    let failed: Vec<_> = calls.iter().map(|(_, _, failed)| *failed).collect();
    watch.see(&results(&failed));
}

fn calling(calls: &[(&str, serde_json::Value)]) -> Event {
    let blocks = calls.iter().enumerate().map(|(at, (name, input))| {
        ContentBlock::ToolUse(ToolUseBlock {
            id: format!("c{at}"),
            name: (*name).into(),
            input: input.as_object().cloned().unwrap_or_else(JsonObject::new),
        })
    });
    Event::Assistant(AssistantEvent {
        message: Message::new(Role::Assistant, blocks.collect()),
        stop_reason: StopReason::ToolUse,
    })
}

fn results(failed: &[bool]) -> Event {
    let answers: Vec<_> = failed.iter().map(|failed| ("r", *failed)).collect();
    answered(&answers)
}

/// Results with these texts, failed or not.
fn answered(answers: &[(&str, bool)]) -> Event {
    let blocks = answers.iter().enumerate().map(|(at, (text, failed))| {
        ContentBlock::ToolResult(ToolResultBlock::text(format!("c{at}"), *text, *failed))
    });
    Event::User(UserEvent {
        message: Message::new(Role::User, blocks.collect()),
        subtype: UserEventSubtype::ToolResult,
        steer_id: None,
    })
}

/// One call of `tool` with `input` that answered `text`.
fn once(watch: &mut Watch, tool: &str, input: serde_json::Value, text: &str) {
    watch.see(&calling(&[(tool, input)]));
    watch.see(&answered(&[(text, false)]));
}

/// The end of call `c0` of `tool`, with `poll_done` in its metadata or not.
fn ended(tool: &str, done: bool) -> Event {
    let mut metadata = JsonObject::new();
    metadata.insert(POLL_DONE.into(), done.into());
    Event::ToolCallEnd(ToolCallEndEvent {
        tool_name: tool.into(),
        result: ToolResultBlock::text("c0", "r", false),
        duration_ms: 0,
        canonical_output: None,
        metadata: Some(Box::new(metadata)),
        error_code: None,
        cache_hit: false,
    })
}

fn polling() -> HashSet<String> {
    HashSet::from(["BashOutput".to_owned()])
}

fn delivery(kind: ModelCallKind, turn: u64) -> Event {
    Event::ModelCallDelivery(ModelCallDeliveryEvent {
        run_id: RunId::new("r"),
        logical_call_id: "l".into(),
        delivery_attempt_id: "d".into(),
        kind,
        turn_index: Some(turn),
        requested_model: "m".into(),
    })
}

#[test]
fn identical_calls_trip_at_the_limit_whatever_the_key_order() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    let (read, other) = (json!({"a": 1, "b": 2}), json!({"b": 2, "a": 1}));
    batch(&mut watch, &[("Read", read.clone(), false)]);
    batch(
        &mut watch,
        &[("Read", other, false), ("Read", json!({"a": 3}), false)],
    );
    assert!(watch.tool_trip().is_none());
    batch(&mut watch, &[("Grep", read.clone(), false)]);
    assert!(watch.tool_trip().is_none(), "another tool is another call");
    batch(&mut watch, &[("Read", read.clone(), true)]);
    assert!(watch.tool_trip().is_none(), "a failed call does not count");
    watch.see(&calling(&[("Read", read.clone())]));
    assert!(watch.tool_trip().is_none(), "a call counts with its result");
    watch.see(&results(&[false]));
    let trip = watch.tool_trip().unwrap();
    assert_eq!(trip.reason, LoopGuardReason::RepeatedToolCall);
    assert_eq!(trip.action, LoopGuardAction::Stop);
    assert!(trip.detail.contains("'Read'") && trip.detail.contains("3 time(s)"));
}

#[test]
fn a_call_is_a_repeat_only_with_the_same_result() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    let read = json!({"path": "a"});
    for text in ["v1", "v1", "v2", "v2", "v3"] {
        once(&mut watch, "Read", read.clone(), text);
    }
    assert!(watch.tool_trip().is_none(), "each content twice at most");
    once(&mut watch, "Read", read.clone(), "v1");
    let trip = watch.tool_trip().unwrap();
    assert!(
        trip.detail.contains("same result 3 time(s)"),
        "{}",
        trip.detail
    );
}

/// Python counts identical inputs whatever the result: calls whose result
/// changes every time (a timestamp) are no repeats, but two of them that
/// alternate used to run until the budget ended them.
#[test]
fn identical_inputs_trip_whatever_the_result_and_the_other_calls_between() {
    for kinds in [2, 3] {
        let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
        let calls: Vec<_> = (0..kinds).map(|k| json!({"call": k})).collect();
        let mut n = 0;
        for round in 0.. {
            for call in &calls {
                n += 1;
                once(&mut watch, "Date", call.clone(), &format!("t{n}"));
                if watch.tool_trip().is_some() {
                    break;
                }
            }
            if watch.tool_trip().is_some() || round > 20 {
                break;
            }
        }
        let trip = watch.tool_trip().unwrap();
        assert_eq!(trip.reason, LoopGuardReason::RepeatedToolCall, "{kinds}");
        assert!(
            trip.detail.contains("identical inputs 10 time(s)"),
            "{kinds}: {}",
            trip.detail
        );
        assert!(trip.detail.contains("whatever the results"));
        assert_eq!(
            n,
            10 * kinds - (kinds - 1),
            "{kinds}: the 10th of the first"
        );
    }
}

/// An error counts for the input (Python's rule), though never as a repeat.
#[test]
fn failed_calls_count_as_identical_inputs_not_as_repeats() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    for n in 0..9 {
        watch.see(&calling(&[("Grep", json!({"n": n})), ("Read", json!({}))]));
        watch.see(&answered(&[("fine", false), (&format!("nope {n}"), true)]));
    }
    assert!(watch.tool_trip().is_none());
    watch.see(&calling(&[("Grep", json!({"n": 9})), ("Read", json!({}))]));
    watch.see(&answered(&[("fine", false), ("nope 9", true)]));
    let trip = watch.tool_trip().unwrap();
    assert!(trip.detail.contains("'Read'"), "{}", trip.detail);
    assert!(trip.detail.contains("identical inputs 10 time(s)"));
}

#[test]
fn a_limit_of_zero_turns_the_input_count_off_alone() {
    let guard = LoopGuard {
        max_identical_inputs: 0,
        ..LoopGuard::default()
    };
    let mut watch = Watch::new(&guard, HashSet::new());
    for n in 0..60 {
        once(&mut watch, "Date", json!({}), &format!("t{n}"));
    }
    assert!(watch.tool_trip().is_none());
}

/// The same successful call with the same result, a few times in a long
/// healthy run, is not a loop: the count looks at the latest results.
#[test]
fn repeats_spaced_far_apart_do_not_trip_and_close_ones_do() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    let status = json!({"command": "git status"});
    for round in 0..4 {
        once(&mut watch, "Bash", status.clone(), "clean");
        for n in 0..REPEAT_WINDOW {
            once(&mut watch, "Read", json!({"n": round * 100 + n}), "text");
        }
    }
    assert!(watch.tool_trip().is_none(), "four in 84 results");

    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    for round in 0..3 {
        once(&mut watch, "Bash", status.clone(), "clean");
        for n in 0..REPEAT_WINDOW / 2 - 2 {
            once(&mut watch, "Read", json!({"n": round * 100 + n}), "text");
        }
    }
    assert!(watch.tool_trip().is_some(), "three in the latest 19");
}

/// A long healthy run that reads one file now and then (20 times in 500
/// turns) is never a loop, whatever the results look like.
#[test]
fn one_file_read_twenty_times_over_five_hundred_turns_is_not_a_loop() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    for n in 0..500 {
        let (input, text) = match n % 25 {
            0 => (json!({"path": "src/lib.rs"}), "same text".to_string()),
            _ => (json!({"n": n}), format!("text {n}")),
        };
        once(&mut watch, "Read", input, &text);
        assert!(watch.tool_trip().is_none(), "turn {n}");
    }
}

/// A fix-and-test loop: an edit, then the same test command, ten times.
#[test]
fn ten_test_runs_between_ten_edits_are_not_a_loop() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    for n in 0..10 {
        once(&mut watch, "Edit", json!({"n": n}), "ok");
        let test = json!({"command": "cargo test"});
        once(&mut watch, "Bash", test, &format!("{n} failed"));
    }
    assert!(watch.tool_trip().is_none(), "{:?}", watch.tool_trip());
}

/// `ResultOffload` replaces a long result with a preview that names a file
/// made from the call's id, so identical results would never look alike.
#[test]
fn an_offloaded_result_repeats_whatever_its_file_name() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    let command = json!({"command": "cargo test"});
    for id in ["toolu_1", "toolu_2", "toolu_3"] {
        let call = ContentBlock::ToolUse(ToolUseBlock {
            id: id.into(),
            name: "Bash".into(),
            input: command.as_object().cloned().unwrap(),
        });
        watch.see(&Event::Assistant(AssistantEvent {
            message: Message::new(Role::Assistant, vec![call]),
            stop_reason: StopReason::ToolUse,
        }));
        let path = format!("/offload/Bash_{id}.txt");
        let text = format!(
            "line 1\n\n[Tool result truncated: ~900 tokens (3600 bytes) offloaded to '{path}'. \
             Showing the first 10 lines. Use read_file(path=\"{path}\", offset=...)]"
        );
        let result = ToolResultBlock::text(id, text, false);
        watch.see(&Event::User(UserEvent {
            message: Message::new(Role::User, vec![ContentBlock::ToolResult(result)]),
            subtype: UserEventSubtype::ToolResult,
            steer_id: None,
        }));
    }
    let trip = watch.tool_trip().unwrap();
    assert!(trip.detail.contains("'Bash'"), "{}", trip.detail);
}

/// The window arithmetic, pinned: a cycle of 5 calls whose results change
/// every time trips the input count (10 of one call in the latest 50
/// results); a cycle of 6 never reaches 10 in 50 and is left to
/// `max_turns` and the budget, which are unset by default.
#[test]
fn a_cycle_of_six_changing_calls_is_beyond_the_input_window() {
    let cycle = |kinds: usize| {
        let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
        for n in 0..500 {
            once(
                &mut watch,
                "Date",
                json!({"call": n % kinds}),
                &format!("t{n}"),
            );
            if watch.tool_trip().is_some() {
                return Some(n + 1);
            }
        }
        None
    };
    assert_eq!(cycle(5), Some(46), "the 10th call of the first kind");
    assert_eq!(cycle(6), None, "500 calls and the run is still going");
}

/// A declared poll whose answer changes on every call (an elapsed time in
/// a status line) is never "the same answer in a row" and is kept out of
/// the input count: only the tool's own end (`poll_done`, a process
/// timeout) or `max_turns` / the budget end such a run. Pinned as the
/// documented limit.
#[test]
fn a_poll_whose_answer_changes_every_time_is_never_a_loop() {
    let mut watch = Watch::new(&LoopGuard::default(), polling());
    let poll = json!({"process_id": "job_1"});
    for n in 0..500 {
        once(
            &mut watch,
            "BashOutput",
            poll.clone(),
            &format!("elapsed {n}s"),
        );
    }
    assert!(watch.tool_trip().is_none());
}

#[test]
fn a_poll_that_runs_on_is_not_a_repeat_until_the_idle_bound() {
    let guard = LoopGuard {
        max_idle_turns: 4,
        ..LoopGuard::default()
    };
    let mut watch = Watch::new(&guard, polling());
    let poll = json!({"process_id": "bash_1"});
    for _ in 0..4 {
        once(&mut watch, "BashOutput", poll.clone(), "running");
    }
    assert!(watch.tool_trip().is_none(), "four waits are allowed");
    // New output is progress and starts the count again.
    once(&mut watch, "BashOutput", poll.clone(), "line 1\nrunning");
    for _ in 0..4 {
        once(&mut watch, "BashOutput", poll.clone(), "running");
    }
    assert!(watch.tool_trip().is_none());
    once(&mut watch, "BashOutput", poll.clone(), "running");
    let trip = watch.tool_trip().unwrap();
    assert_eq!(trip.reason, LoopGuardReason::RepeatedToolCall);
    assert!(
        trip.detail.contains("polled 5 time(s) in a row"),
        "{}",
        trip.detail
    );
}

/// Ten waits on one command are neither repeats nor identical inputs.
#[test]
fn polling_a_running_command_is_not_counted_as_a_repeated_input() {
    let mut watch = Watch::new(&LoopGuard::default(), polling());
    for _ in 0..24 {
        once(
            &mut watch,
            "BashOutput",
            json!({"process_id": "b"}),
            "running",
        );
    }
    assert!(watch.tool_trip().is_none());
}

/// A command that finished answers the same thing every time: reading it
/// again is no wait, and three times is a loop.
#[test]
fn a_finished_command_read_again_is_a_repeat() {
    let mut watch = Watch::new(&LoopGuard::default(), polling());
    let poll = json!({"process_id": "bash_1"});
    for _ in 0..2 {
        watch.see(&calling(&[("BashOutput", poll.clone())]));
        watch.see(&ended("BashOutput", true));
        watch.see(&answered(&[("[exit code 0]", false)]));
    }
    assert!(watch.tool_trip().is_none());
    watch.see(&calling(&[("BashOutput", poll.clone())]));
    watch.see(&ended("BashOutput", true));
    watch.see(&answered(&[("[exit code 0]", false)]));
    let trip = watch.tool_trip().unwrap();
    assert!(trip.detail.contains("'BashOutput'"), "{}", trip.detail);
    assert!(trip.detail.contains("same result 3 time(s)"));

    // The same end without `poll_done` is a wait, and another tool's end
    // says nothing about a poll.
    let mut watch = Watch::new(&LoopGuard::default(), polling());
    for _ in 0..5 {
        watch.see(&calling(&[("BashOutput", poll.clone())]));
        watch.see(&ended("BashOutput", false));
        watch.see(&ended("Other", true));
        watch.see(&answered(&[("[exit code 0]", false)]));
    }
    assert!(watch.tool_trip().is_none());
}

#[test]
fn a_poll_is_never_a_repeat_but_its_failures_count() {
    let mut watch = Watch::new(&LoopGuard::default(), polling());
    let poll = json!({"process_id": "bash_1"});
    for _ in 0..5 {
        batch(&mut watch, &[("BashOutput", poll.clone(), false)]);
    }
    assert!(watch.tool_trip().is_none());
    for _ in 0..3 {
        batch(&mut watch, &[("BashOutput", poll.clone(), true)]);
    }
    assert_eq!(
        watch.tool_trip().unwrap().reason,
        LoopGuardReason::RepeatedFailures
    );
}

#[test]
fn a_failure_streak_trips_and_a_success_resets_it() {
    let mut watch = Watch::new(&LoopGuard::default(), HashSet::new());
    for batch in [&[true][..], &[true, true], &[false, true], &[true], &[true]] {
        watch.see(&results(batch));
    }
    assert!(watch.tool_trip().is_none());
    watch.see(&results(&[true]));
    assert_eq!(
        watch.tool_trip().unwrap().reason,
        LoopGuardReason::RepeatedFailures
    );
    watch.see(&results(&[false]));
    assert!(watch.tool_trip().is_some(), "a reached limit stays reached");
}

#[test]
fn zero_turns_a_tool_check_off() {
    let off = LoopGuard {
        max_identical_tool_calls: 0,
        max_identical_inputs: 0,
        max_consecutive_failures: 0,
        ..LoopGuard::default()
    };
    let mut watch = Watch::new(&off, polling());
    for failed in [true, false, true, true, false, true, true] {
        batch(&mut watch, &[("Read", json!({}), failed)]);
    }
    for _ in 0..40 {
        once(&mut watch, "BashOutput", json!({}), "running");
    }
    assert!(watch.tool_trip().is_none());
}

#[test]
fn idle_turns_count_from_the_last_primary_call() {
    let guard = LoopGuard {
        max_idle_turns: 2,
        ..LoopGuard::default()
    };
    let mut watch = Watch::new(&guard, HashSet::new());
    assert!(watch.idle_trip(0).is_none());
    assert!(watch.idle_trip(1).is_none());
    assert_eq!(
        watch.idle_trip(2).unwrap().reason,
        LoopGuardReason::IdleTurns
    );
    watch.see(&delivery(ModelCallKind::Primary, 4));
    watch.see(&delivery(ModelCallKind::Compaction, 6));
    assert!(watch.idle_trip(6).is_none(), "turns 5 and 6 skip");
    assert!(
        watch.idle_trip(7).is_some(),
        "a summary call is not the turn's"
    );
}

#[test]
fn the_counts_read_back_from_the_log_are_the_live_ones() {
    let events = [
        calling(&[("Read", json!({}))]),
        results(&[false]),
        delivery(ModelCallKind::Primary, 0),
        calling(&[("BashOutput", json!({})), ("Grep", json!({}))]),
        ended("BashOutput", true),
        answered(&[("[exit code 0]", false), ("x", true)]),
        calling(&[("BashOutput", json!({}))]),
        answered(&[("running", false)]),
        calling(&[("Read", json!({}))]),
        ended("BashOutput", true),
    ];
    let mut live = Watch::new(&LoopGuard::default(), polling());
    for event in &events {
        live.see(event);
    }
    let envelopes: Vec<Envelope> = (1..)
        .zip(events)
        .map(|(seq, event)| Envelope {
            v: WIRE_VERSION,
            seq,
            run_id: RunId::new("r"),
            ts_ms: 0,
            ignorable: false,
            event,
        })
        .collect();
    let read = Watch::of(&LoopGuard::default(), polling(), &envelopes);
    let counts = |w: &Watch| {
        (
            w.pending.clone(),
            w.settled.clone(),
            w.results.clone(),
            w.inputs.clone(),
            w.polling.clone(),
            w.failures,
            w.last_call,
        )
    };
    assert_eq!(counts(&read), counts(&live));
    assert!(!read.polling.is_empty() && !read.settled.is_empty());
}

#[test]
fn the_idle_bound_cannot_be_turned_off() {
    let off = LoopGuard {
        max_idle_turns: 0,
        ..LoopGuard::default()
    };
    assert!(off.problem().is_some());
    assert!(LoopGuard::default().problem().is_none());
}

/// A field added after the guard shipped stays off the wire at its default,
/// and a guard written without it reads back with the default.
#[test]
fn the_identical_inputs_limit_is_off_the_wire_at_its_default() {
    let default = serde_json::to_value(LoopGuard::default()).unwrap();
    assert_eq!(
        default,
        json!({
            "max_identical_tool_calls": 3,
            "max_consecutive_failures": 3,
            "max_idle_turns": 25,
        })
    );
    let set = LoopGuard {
        max_identical_inputs: 4,
        ..LoopGuard::default()
    };
    assert_eq!(
        serde_json::to_value(&set).unwrap()["max_identical_inputs"],
        4
    );
    let old: LoopGuard = serde_json::from_value(default).unwrap();
    assert_eq!(old, LoopGuard::default());
    let bad = serde_json::from_value::<LoopGuard>(json!({"max_identical_input": 1}));
    assert!(bad.is_err(), "unknown fields are refused");
}
