//! What a call's result becomes: refusals, served results, post_tool_use,
//! the output schema, rendering and progress.

use std::time::Duration;

use futures::future;
use serde_json::json;

use super::*;
use crate::hooks::{PostToolUse, PostToolUseResponse, PreToolUse, PreToolUseResponse};

#[tokio::test]
async fn refusals_render_as_errors_without_starting() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Exec", ToolScope::Exec, &log));
    let sink = RecSink::logging(&log);
    let mut approvals = HashMap::new();
    let deny = crate::hooks::ApprovalResponse::Deny {
        reason: "no".into(),
    };
    let approval = RecordedApproval {
        input: JsonObject::new(),
        response: deny,
    };
    approvals.insert("x".to_owned(), approval);
    let blocks = [
        use_block("u", "Nope", json!({})),
        use_block("x", "Exec", json!({})),
    ];
    let plan = fx.plan_with(&blocks, &approvals).await;

    let results = fx.execute(plan, &sink, &options(4)).await.unwrap();

    assert_eq!(text(&results[0]), r#"Tool "Nope" is not registered"#);
    assert_eq!(text(&results[1]), "Tool call denied: no");
    assert!(results.iter().all(|block| block.is_error));
    assert_eq!(lines(&log), ["end u", "end x"]);
    assert_eq!(sink.outcome("x").duration, Duration::ZERO);
}

#[tokio::test]
async fn a_served_result_skips_the_tool_but_not_post_tool_use() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Read", ToolScope::Read, &log));
    let pre = hook::<PreToolUse>("cache", &log, |_| PreToolUseResponse::Resolve {
        result: Ok(ToolOutput::new("cached")),
    });
    let _pre = fx.hooks.pre_tool_use.register(pre).unwrap();
    let post = hook::<PostToolUse>("post", &log, |request| {
        assert_eq!(request.result, Ok(ToolOutput::new("cached")));
        PostToolUseResponse::Continue
    });
    let _post = fx.hooks.post_tool_use.register(post).unwrap();
    let sink = RecSink::logging(&log);

    let results = fx.run(&[use_block("r", "Read", json!({}))], &sink, 4).await;

    assert_eq!(text(&results[0]), "cached");
    assert_eq!(lines(&log), ["hook cache", "hook post", "end r"]);
    assert_eq!(sink.outcome("r").hook_records.len(), 1);
}

#[tokio::test]
async fn post_tool_use_replaces_and_the_output_schema_is_checked() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    let schema = json!({"type": "object", "required": ["n"]});
    fx.tool(TestTool::new("Read", ToolScope::Read, &log).output_schema(schema));
    let post = hook::<PostToolUse>("post", &log, |request| {
        let value = if request.tool_use_id == "good" {
            json!({"z": 1, "n": "é"})
        } else {
            json!({"m": 2})
        };
        PostToolUseResponse::Replace {
            result: Ok(ToolOutput::new(value)),
        }
    });
    let _post = fx.hooks.post_tool_use.register(post).unwrap();
    let sink = RecSink::default();

    let blocks = [
        use_block("good", "Read", json!({})),
        use_block("bad", "Read", json!({})),
    ];
    let results = fx.run(&blocks, &sink, 4).await;

    assert_eq!(
        text(&results[0]),
        r#"{"n":"é","z":1}"#,
        "compact canonical JSON"
    );
    assert!(!results[0].is_error);
    let bad = sink.outcome("bad");
    assert!(results[1].is_error);
    assert!(text(&results[1]).contains("does not match its output schema"));
    assert!(bad.result.is_err());
    assert_eq!(bad.contract_error.expect("recorded").tool, "Read");
}

#[tokio::test]
async fn results_render_strings_values_and_errors() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(
        TestTool::new("Read", ToolScope::Read, &log).body(|input, _| {
            let result = match input.get("kind").and_then(JsonValue::as_str) {
                Some("text") => Ok(ToolOutput::new("plain text")),
                Some("error") => Err(ToolOutputError::new("it broke")),
                _ => Ok(ToolOutput::new(json!([1, 2.5, null]))),
            };
            future::ready(result).boxed()
        }),
    );
    let sink = RecSink::default();

    let blocks = [
        use_block("t", "Read", json!({"kind": "text"})),
        use_block("v", "Read", json!({})),
        use_block("e", "Read", json!({"kind": "error"})),
    ];
    let results = fx.run(&blocks, &sink, 4).await;

    assert_eq!(results[0], ToolResultBlock::text("t", "plain text", false));
    assert_eq!(
        results[1],
        ToolResultBlock::text("v", "[1,2.5,null]", false)
    );
    assert_eq!(results[2], ToolResultBlock::text("e", "it broke", true));
    assert_eq!(
        sink.outcome("e").result,
        Err(ToolOutputError::new("it broke"))
    );
}

#[tokio::test]
async fn progress_reaches_the_sink_and_late_reports_are_dropped() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    let kept = Arc::new(Mutex::new(None));
    let k = Arc::clone(&kept);
    fx.tool(
        TestTool::new("Read", ToolScope::Read, &log).body(move |_, ctx| {
            ctx.report_progress("half", None);
            ctx.report_progress("done", None);
            *k.lock().unwrap() = Some(ctx);
            future::ready(Ok(ToolOutput::new("ok"))).boxed()
        }),
    );
    let sink = RecSink::default();

    fx.run(&[use_block("r", "Read", json!({}))], &sink, 4).await;
    let ctx = kept.lock().unwrap().take().unwrap();
    ctx.report_progress("late", None);

    let progress = sink.progress.lock().unwrap().clone();
    let expected = [
        ("r".to_owned(), "half".to_owned()),
        ("r".into(), "done".into()),
    ];
    assert_eq!(progress, expected);
    assert!(
        ctx.cancel.is_cancelled(),
        "the call's token ends with the call"
    );
}

/// R1-sec F5: a tool that panics, before or after it first awaits, gives
/// an error result; the batch goes on and the run does not die.
#[tokio::test]
async fn a_panicking_tool_becomes_an_error_result() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Boom", ToolScope::Read, &log).body(|_, _| panic!("kaboom")));
    fx.tool(TestTool::new("Bang", ToolScope::Read, &log).body(|_, _| {
        async {
            tokio::task::yield_now().await;
            panic!("bang {}", 1)
        }
        .boxed()
    }));
    fx.tool(TestTool::new("Read", ToolScope::Read, &log));
    let sink = RecSink::default();
    let blocks = [
        use_block("a", "Boom", json!({})),
        use_block("b", "Bang", json!({})),
        use_block("c", "Read", json!({})),
    ];
    let results = fx.run(&blocks, &sink, 4).await;
    assert_eq!(text(&results[0]), r#"Tool "Boom" panicked: kaboom"#);
    assert_eq!(text(&results[1]), r#"Tool "Bang" panicked: bang 1"#);
    assert!(results[0].is_error && results[1].is_error);
    assert!(!results[2].is_error);
    assert_eq!(sink.finished_ids().len(), 3);
}
