//! Execution: batching, the sink barrier, abort and timeout.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use futures::future;
use serde_json::json;
use tokio::sync::{Barrier, Notify};

use super::*;

/// Fail instead of hanging when a test's concurrency assumption is wrong.
async fn within<T>(work: impl std::future::Future<Output = T>) -> T {
    tokio::time::timeout(Duration::from_secs(5), work)
        .await
        .expect("finished in time")
}

fn ids(results: &[ToolResultBlock]) -> Vec<&str> {
    results
        .iter()
        .map(|block| block.tool_use_id.as_str())
        .collect()
}

#[tokio::test]
async fn reads_run_together_and_results_keep_call_order() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    let barrier = Arc::new(Barrier::new(2));
    let done = Arc::new(Notify::new());
    let (b1, d1) = (Arc::clone(&barrier), Arc::clone(&done));
    // `slow` finishes only after `fast`, and both pass the barrier only when
    // they run at the same time.
    fx.tool(
        TestTool::new("slow", ToolScope::Read, &log).body(move |_, _| {
            let (barrier, done) = (Arc::clone(&b1), Arc::clone(&d1));
            async move {
                barrier.wait().await;
                done.notified().await;
                Ok(ToolOutput::new("slow"))
            }
            .boxed()
        }),
    );
    let (b2, d2) = (barrier, done);
    fx.tool(
        TestTool::new("fast", ToolScope::Read, &log).body(move |_, _| {
            let (barrier, done) = (Arc::clone(&b2), Arc::clone(&d2));
            async move {
                barrier.wait().await;
                done.notify_one();
                Ok(ToolOutput::new("fast"))
            }
            .boxed()
        }),
    );
    let sink = RecSink::default();

    let blocks = [
        use_block("a", "slow", json!({})),
        use_block("b", "fast", json!({})),
    ];
    let results = within(fx.run(&blocks, &sink, 4)).await;

    assert_eq!(ids(&results), ["a", "b"]);
    assert_eq!(text(&results[0]), "slow");
    assert_eq!(
        sink.finished_ids(),
        ["b", "a"],
        "the sink sees completion order"
    );
}

#[tokio::test]
async fn writes_run_alone_and_later_calls_wait() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    // Each tool yields to the timer, so calls that overlap would interleave.
    let napping = |input: JsonObject, _| {
        async move {
            tokio::time::sleep(Duration::from_millis(2)).await;
            Ok(ToolOutput::new(JsonValue::Object(input)))
        }
        .boxed()
    };
    fx.tool(TestTool::new("Read", ToolScope::Read, &log).body(napping));
    fx.tool(TestTool::new("Write", ToolScope::Write, &log).body(napping));
    let sink = RecSink::logging(&log);

    let blocks = [
        use_block("r1", "Read", json!({})),
        use_block("w1", "Write", json!({})),
        use_block("w2", "Write", json!({})),
        use_block("r2", "Read", json!({})),
    ];
    let results = fx.run(&blocks, &sink, 4).await;

    assert_eq!(ids(&results), ["r1", "w1", "w2", "r2"]);
    let expected = [
        "start r1",
        "exec Read",
        "end r1",
        "start w1",
        "exec Write",
        "end w1",
        "start w2",
        "exec Write",
        "end w2",
        "start r2",
        "exec Read",
        "end r2",
    ];
    assert_eq!(lines(&log), expected);
}

#[tokio::test]
async fn read_groups_respect_the_concurrency_cap() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    let running = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));
    let (r, p) = (Arc::clone(&running), Arc::clone(&peak));
    fx.tool(
        TestTool::new("Read", ToolScope::Read, &log).body(move |_, _| {
            let (running, peak) = (Arc::clone(&r), Arc::clone(&p));
            async move {
                let now = running.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(now, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(10)).await;
                running.fetch_sub(1, Ordering::SeqCst);
                Ok(ToolOutput::new("ok"))
            }
            .boxed()
        }),
    );
    let blocks: Vec<_> = (0..5)
        .map(|i| use_block(&format!("r{i}"), "Read", json!({})))
        .collect();

    let results = fx.run(&blocks, &RecSink::default(), 2).await;
    assert_eq!(results.len(), 5);
    assert_eq!(peak.load(Ordering::SeqCst), 2);

    peak.store(0, Ordering::SeqCst);
    fx.run(&blocks, &RecSink::default(), 0).await;
    assert_eq!(peak.load(Ordering::SeqCst), 1, "zero counts as one");
}

#[tokio::test]
async fn an_abort_before_execution_starts_nothing() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    fx.tool(TestTool::new("Read", ToolScope::Read, &log));
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let sink = RecSink::logging(&log);
    let plan = fx
        .plan(&[
            use_block("r", "Read", json!({})),
            use_block("w", "Write", json!({})),
        ])
        .await;
    let options = options(4);
    options.cancel.cancel();

    let results = fx.execute(plan, &sink, &options).await.unwrap();

    assert!(
        results
            .iter()
            .all(|block| block.is_error && text(block) == "Tool call aborted")
    );
    assert_eq!(lines(&log), ["end r", "end w"], "no tool started");
}

#[tokio::test]
async fn an_abort_cancels_running_calls_and_later_ones_never_start() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    let started = Arc::new(Notify::new());
    let token = Arc::new(Mutex::new(None));
    let (s, t) = (Arc::clone(&started), Arc::clone(&token));
    fx.tool(
        TestTool::new("Hang", ToolScope::Write, &log).body(move |_, ctx| {
            *t.lock().unwrap() = Some(ctx.cancel.clone());
            s.notify_one();
            future::pending().boxed()
        }),
    );
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let sink = RecSink::logging(&log);
    let plan = fx
        .plan(&[
            use_block("h", "Hang", json!({})),
            use_block("w", "Write", json!({})),
        ])
        .await;
    let options = options(4);

    let abort = async {
        started.notified().await;
        options.cancel.cancel();
    };
    let (results, ()) = within(future::join(fx.execute(plan, &sink, &options), abort)).await;

    let results = results.unwrap();
    assert_eq!(ids(&results), ["h", "w"]);
    assert!(
        results
            .iter()
            .all(|block| text(block) == "Tool call aborted")
    );
    assert_eq!(lines(&log), ["start h", "exec Hang", "end h", "end w"]);
    let tool_token = token.lock().unwrap().clone().unwrap();
    assert!(tool_token.is_cancelled(), "the tool saw the abort");
}

#[tokio::test]
async fn a_timeout_becomes_an_error_result() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    let token = Arc::new(Mutex::new(None));
    let t = Arc::clone(&token);
    fx.tool(
        TestTool::new("Hang", ToolScope::Read, &log).body(move |_, ctx| {
            *t.lock().unwrap() = Some(ctx.cancel.clone());
            future::pending().boxed()
        }),
    );
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let sink = RecSink::default();
    let plan = fx
        .plan(&[
            use_block("h", "Hang", json!({})),
            use_block("w", "Write", json!({})),
        ])
        .await;
    let mut options = options(4);
    options.call_timeout = Some(Duration::from_millis(20));

    let results = within(fx.execute(plan, &sink, &options)).await.unwrap();

    assert!(results[0].is_error);
    assert_eq!(text(&results[0]), r#"Tool "Hang" timed out after 20ms"#);
    assert!(!results[1].is_error, "the next call still runs");
    assert!(token.lock().unwrap().clone().unwrap().is_cancelled());
    assert!(
        !options.cancel.is_cancelled(),
        "a timeout does not abort the run"
    );
}

#[tokio::test]
async fn a_sink_error_stops_the_next_tool_from_starting() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let sink = RecSink {
        log: Arc::clone(&log),
        fail_start: Some("w2"),
        ..RecSink::default()
    };
    let plan = fx
        .plan(&[
            use_block("w1", "Write", json!({})),
            use_block("w2", "Write", json!({})),
            use_block("w3", "Write", json!({})),
        ])
        .await;

    let result = fx.execute(plan, &sink, &options(4)).await;

    assert_eq!(result, Err(SinkDown));
    assert_eq!(
        lines(&log),
        ["start w1", "exec Write", "end w1", "start w2"]
    );
}

#[tokio::test]
#[should_panic(expected = "awaiting approval")]
async fn executing_a_plan_awaiting_approval_panics() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let plan = fx.plan(&[use_block("w", "Write", json!({}))]).await;
    let _ = fx.execute(plan, &RecSink::default(), &options(4)).await;
}

#[test]
fn the_execute_future_is_send() {
    fn assert_send<T: Send>(_: &T) {}
    let fx = Fixture::new(PermissionMode::Default);
    let sink = RecSink::default();
    let options = options(1);
    let plan = BatchPlan { calls: Vec::new() };
    let future = execute(plan, &sink, &fx.hooks, &fx.run, &options);
    assert_send(&future);
}
