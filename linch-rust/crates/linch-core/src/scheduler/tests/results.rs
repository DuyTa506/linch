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

/// The schema checks the value the tool returned, before `post_tool_use`:
/// a hook may replace a valid value with one the schema does not describe
/// (an offload pointer), and gets a violation as the error it became.
#[tokio::test]
async fn the_output_schema_checks_the_tool_value_before_post_tool_use() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    let schema = json!({"type": "object", "required": ["n"]});
    fx.tool(TestTool::new("Read", ToolScope::Read, &log).output_schema(schema));
    let seen = Arc::clone(&log);
    let post = hook::<PostToolUse>("post", &log, move |request| {
        let ok = if request.result.is_ok() {
            "ok"
        } else {
            "error"
        };
        push(&seen, format!("saw {} {ok}", request.tool_use_id));
        match request.tool_use_id.as_str() {
            "pointer" => PostToolUseResponse::Replace {
                result: Ok(ToolOutput::new("saved to /results/pointer")),
            },
            _ => PostToolUseResponse::Continue,
        }
    });
    let _post = fx.hooks.post_tool_use.register(post).unwrap();
    let sink = RecSink::default();

    let blocks = [
        use_block("pointer", "Read", json!({"n": 1})),
        use_block("plain", "Read", json!({"z": 1, "n": "é"})),
        use_block("bad", "Read", json!({"m": 2})),
    ];
    let results = fx.run(&blocks, &sink, 4).await;

    assert_eq!(text(&results[0]), "saved to /results/pointer");
    assert!(!results[0].is_error);
    assert!(sink.outcome("pointer").contract_error.is_none());
    assert_eq!(
        text(&results[1]),
        r#"{"n":"é","z":1}"#,
        "compact canonical JSON"
    );
    let bad = sink.outcome("bad");
    assert!(results[2].is_error);
    assert!(text(&results[2]).contains("does not match its output schema"));
    assert!(bad.result.is_err());
    assert_eq!(bad.contract_error.expect("recorded").tool, "Read");
    let saw = lines(&log);
    for line in ["saw pointer ok", "saw plain ok", "saw bad error"] {
        assert!(saw.iter().any(|seen| seen == line), "{saw:?}");
    }
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
