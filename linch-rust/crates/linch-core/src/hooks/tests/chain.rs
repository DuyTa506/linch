//! Failure handling, matching, registration lifetime and snapshots.

use super::*;

#[tokio::test]
async fn an_error_under_each_fail_mode() {
    // pre_tool_use fails closed by default: the error blocks the call.
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("gate", &log, |_| Act::Fail),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Bash", obj(json!({}))))
        .await;
    let PreToolUseOutcome::Blocked { reason, .. } = done.outcome else {
        panic!("expected a block");
    };
    assert_eq!(
        reason, "hook `gate` failed",
        "the error text stays out of it"
    );
    assert_eq!(
        done.records[0].status,
        HookStatus::Failed {
            error: "backend down".into(),
            fail_mode: FailMode::Closed,
        }
    );

    // Overridden to open: the failing hook is skipped and the call runs.
    let hooks = Hooks::default();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("gate", &log, |_| Act::Fail).fail_mode(FailMode::Open),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Bash", obj(json!({}))))
        .await;
    assert!(matches!(done.outcome, PreToolUseOutcome::Run { .. }));
    assert!(matches!(
        done.records[0].status,
        HookStatus::Failed {
            fail_mode: FailMode::Open,
            ..
        }
    ));
}

#[tokio::test]
async fn a_timeout_under_each_fail_mode() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("slow", &log, |_| Act::Hang).timeout(SHORT),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Bash", obj(json!({}))))
        .await;
    assert_eq!(
        done.outcome,
        PreToolUseOutcome::Blocked {
            input: obj(json!({})),
            reason: "hook `slow` timed out".into(),
        }
    );
    assert_eq!(
        done.records[0].status,
        HookStatus::TimedOut {
            after: SHORT,
            fail_mode: FailMode::Closed,
        }
    );

    let hooks = Hooks::default();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("slow", &log, |_| Act::Hang)
            .timeout(SHORT)
            .fail_mode(FailMode::Open),
    );
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("next", &log, |_| Act::Answer(PreToolUseResponse::Continue)),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Bash", obj(json!({}))))
        .await;
    assert!(matches!(done.outcome, PreToolUseOutcome::Run { .. }));
    assert_eq!(done.records.len(), 2, "the next hook still ran");
}

#[tokio::test]
async fn a_hook_that_answers_in_time_is_not_cut() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("fast", &log, |_| Act::Answer(PreToolUseResponse::Continue))
            .timeout(Duration::from_secs(5)),
    );
    let done = hooks
        .pre_tool_use
        .run(pre_request("Bash", obj(json!({}))))
        .await;
    assert!(matches!(done.outcome, PreToolUseOutcome::Run { .. }));
    assert_eq!(
        done.records[0].status,
        HookStatus::Answered {
            verdict: "continue"
        }
    );
}

#[tokio::test]
async fn the_matcher_filters_hooks_by_tool_name() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("writes", &log, |_| {
            Act::Answer(PreToolUseResponse::Block {
                reason: "no writes".into(),
            })
        })
        .matcher(&["Write", "Edit"]),
    );
    let read = hooks
        .pre_tool_use
        .run(pre_request("Read", obj(json!({}))))
        .await;
    assert!(matches!(read.outcome, PreToolUseOutcome::Run { .. }));
    assert!(read.records.is_empty(), "a filtered hook leaves no record");
    let edit = hooks
        .pre_tool_use
        .run(pre_request("Edit", obj(json!({}))))
        .await;
    assert!(matches!(edit.outcome, PreToolUseOutcome::Blocked { .. }));
    assert_eq!(calls(&log), ["writes"]);
}

#[tokio::test]
async fn dispose_removes_only_its_own_registration() {
    let hooks = Hooks::default();
    let log = new_log();
    let hook = Arc::new(Scripted::new("same", &log, |_| {
        Act::Answer(PreToolUseResponse::Continue)
    }));
    let first = hooks.pre_tool_use.register(hook.clone()).unwrap();
    let second = hooks.pre_tool_use.register(hook).unwrap();
    first.dispose();
    first.dispose();
    let done = hooks
        .pre_tool_use
        .run(pre_request("Read", obj(json!({}))))
        .await;
    assert_eq!(done.records.len(), 1, "the second registration stays");
    second.dispose();
    assert!(hooks.pre_tool_use.is_empty());
}

#[tokio::test]
async fn a_run_keeps_the_snapshot_it_started_with() {
    let hooks = Arc::new(Hooks::default());
    let log = new_log();
    let (inner_hooks, inner_log) = (Arc::clone(&hooks), Arc::clone(&log));
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("adder", &log, move |_| {
            let late = Scripted::new("late", &inner_log, |_| {
                Act::Answer(PreToolUseResponse::Continue)
            });
            let _ = register(&inner_hooks.pre_tool_use, late);
            Act::Answer(PreToolUseResponse::Continue)
        }),
    );
    let _ = hooks
        .pre_tool_use
        .run(pre_request("Read", obj(json!({}))))
        .await;
    assert_eq!(
        calls(&log),
        ["adder"],
        "a hook added mid-run waits for the next run"
    );
    let _ = hooks
        .pre_tool_use
        .run(pre_request("Read", obj(json!({}))))
        .await;
    assert_eq!(calls(&log), ["adder", "adder", "late"]);
}

#[test]
fn dispose_after_the_chain_is_dropped_does_nothing() {
    let hooks = Hooks::default();
    let log = new_log();
    let reg = register(
        &hooks.post_turn,
        Scripted::new("observer", &log, |_| Act::Answer(())),
    );
    drop(hooks);
    reg.dispose();
    assert!(reg.is_disposed());
}

#[test]
fn chain_futures_are_send() {
    fn assert_send<T: Send>(_: T) {}
    let hooks = Hooks::default();
    assert_send(
        hooks
            .pre_tool_use
            .run(pre_request("Read", JsonObject::new())),
    );
}
