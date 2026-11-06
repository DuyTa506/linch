//! Planning: step order, refusals, permission, recorded approvals and
//! cached results.

use std::collections::HashMap;

use serde_json::json;

use super::*;
use crate::hooks::{ApprovalResponse, PermissionRequest, PreToolUse, PreToolUseResponse};
use crate::permissions::{PermissionLayer, Rule, RuleDecision, ToolRule};
use crate::registry::INVALID_INPUT_CODE;

fn deny_rule(tool: &str) -> PermissionPolicy {
    let mut policy = PermissionPolicy::new(PermissionMode::Default, "/project");
    let rule = Rule::from(ToolRule::new(tool, RuleDecision::Deny));
    policy.layers.push(PermissionLayer::new("project", [rule]));
    policy
}

fn rewrite_x_to(
    value: i64,
) -> impl Fn(&crate::hooks::PreToolUseRequest) -> PreToolUseResponse + Send + Sync + 'static {
    move |request| {
        let mut input = request.input.clone();
        input.insert("x".into(), value.into());
        PreToolUseResponse::Rewrite { input }
    }
}

fn cached() -> PreToolUseResponse {
    PreToolUseResponse::Resolve {
        result: Ok(ToolOutput::new("cached")),
    }
}

#[tokio::test]
async fn steps_run_in_order_on_the_rewritten_input() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log).log_validate(&log));
    let pre = hook::<PreToolUse>("pre", &log, rewrite_x_to(2));
    let _pre = fx.hooks.pre_tool_use.register(pre).unwrap();
    let perm = hook::<PermissionRequest>("perm", &log, |request| {
        assert_eq!(
            request.input,
            obj(json!({"x": 2})),
            "permission sees the final input"
        );
        ApprovalResponse::Allow
    });
    let _perm = fx.hooks.permission_request.register(perm).unwrap();

    let plan = fx.plan(&[use_block("t1", "Write", json!({"x": 1}))]).await;

    let expected = [
        r#"validate Write {"x":1}"#,
        "hook pre",
        r#"validate Write {"x":2}"#,
        "hook perm",
    ];
    assert_eq!(lines(&log), expected);
    let call = &plan.calls()[0];
    assert_eq!(call.action, PlannedAction::Run);
    assert_eq!(call.input, obj(json!({"x": 2})));
    assert_eq!(call.summary, "Write with 1 fields");
    assert_eq!(call.hook_records.len(), 2);
    assert!(!plan.is_awaiting_approval());
}

#[tokio::test]
async fn unknown_tools_and_invalid_input_skip_the_hooks() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    let schema = json!({"type": "object", "properties": {"x": {"type": "integer"}}});
    fx.tool(TestTool::new("Read", ToolScope::Read, &log).schema(schema));
    let pre = hook::<PreToolUse>("pre", &log, |_| PreToolUseResponse::Continue);
    let _pre = fx.hooks.pre_tool_use.register(pre).unwrap();

    let plan = fx
        .plan(&[
            use_block("t1", "Nope", json!({})),
            use_block("t2", "Read", json!({"x": "one"})),
        ])
        .await;

    assert!(lines(&log).is_empty(), "no hook ran: {:?}", lines(&log));
    let [unknown, invalid] = plan.calls() else {
        panic!("two calls")
    };
    let PlannedAction::Error { error } = &unknown.action else {
        panic!("{unknown:?}")
    };
    assert_eq!(error.message, r#"Tool "Nope" is not registered"#);
    assert_eq!(unknown.summary, "Nope");
    let PlannedAction::Error { error } = &invalid.action else {
        panic!("{invalid:?}")
    };
    assert_eq!(error.code.as_deref(), Some(INVALID_INPUT_CODE));
    assert_eq!(invalid.input, obj(json!({"x": "one"})));
}

#[tokio::test]
async fn a_rewrite_is_validated_again() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::SkipDangerous);
    let schema = json!({"type": "object", "properties": {"x": {"type": "string"}}});
    fx.tool(TestTool::new("Read", ToolScope::Read, &log).schema(schema));
    let pre = hook::<PreToolUse>("pre", &log, rewrite_x_to(7));
    let _pre = fx.hooks.pre_tool_use.register(pre).unwrap();

    let plan = fx
        .plan(&[use_block("t1", "Read", json!({"x": "ok"}))])
        .await;

    let call = &plan.calls()[0];
    let PlannedAction::Error { error } = &call.action else {
        panic!("{call:?}")
    };
    assert!(
        error
            .message
            .starts_with("A pre_tool_use hook produced invalid input: ")
    );
    assert_eq!(error.code.as_deref(), Some(INVALID_INPUT_CODE));
    assert_eq!(call.input, obj(json!({"x": 7})), "keeps the hook's input");
}

#[tokio::test]
async fn a_block_skips_permission() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let pre = hook::<PreToolUse>("pre", &log, |_| PreToolUseResponse::Block {
        reason: "not today".into(),
    });
    let _pre = fx.hooks.pre_tool_use.register(pre).unwrap();
    let ask = hook::<PermissionRequest>("perm", &log, |_| ApprovalResponse::Allow);
    let _perm = fx.hooks.permission_request.register(ask).unwrap();

    let plan = fx.plan(&[use_block("t1", "Write", json!({}))]).await;

    let reason = "not today".to_owned();
    assert_eq!(plan.calls()[0].action, PlannedAction::Blocked { reason });
    assert_eq!(lines(&log), ["hook pre"]);
}

#[tokio::test]
async fn permission_allows_denies_and_asks() {
    let log = new_log();
    let fx = Fixture::with_policy(deny_rule("Exec"));
    fx.tool(TestTool::new("Read", ToolScope::Read, &log));
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    fx.tool(TestTool::new("Exec", ToolScope::Exec, &log));

    let plan = fx
        .plan(&[
            use_block("r", "Read", json!({})),
            use_block("w", "Write", json!({})),
            use_block("x", "Exec", json!({})),
        ])
        .await;

    let actions: Vec<_> = plan.calls().iter().map(|call| &call.action).collect();
    assert_eq!(actions[0], &PlannedAction::Run);
    assert!(matches!(
        actions[1],
        PlannedAction::AwaitingApproval { reason: Some(_) }
    ));
    assert!(matches!(actions[2], PlannedAction::Denied { .. }));
    assert!(plan.is_awaiting_approval());
}

#[tokio::test]
async fn a_permission_request_hook_can_answer_an_ask() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let deny = hook::<PermissionRequest>("perm", &log, |_| ApprovalResponse::Deny {
        reason: "hook says no".into(),
    });
    let _perm = fx.hooks.permission_request.register(deny).unwrap();

    let plan = fx.plan(&[use_block("w", "Write", json!({}))]).await;

    let reason = "hook says no".to_owned();
    assert_eq!(plan.calls()[0].action, PlannedAction::Denied { reason });
}

fn recorded(
    id: &str,
    input: JsonValue,
    response: ApprovalResponse,
) -> HashMap<String, RecordedApproval> {
    let approval = RecordedApproval {
        input: obj(input),
        response,
    };
    HashMap::from([(id.to_owned(), approval)])
}

#[tokio::test]
async fn suspend_then_resume_with_recorded_approvals() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let blocks = [use_block("w", "Write", json!({"a": 1, "b": 2}))];

    let first = fx.plan(&blocks).await;
    assert!(first.is_awaiting_approval());
    let input = JsonValue::Object(first.calls()[0].input.clone());

    // Same input with its keys in another order still matches.
    let allow = recorded("w", json!({"b": 2, "a": 1}), ApprovalResponse::Allow);
    let resumed = fx.plan_with(&blocks, &allow).await;
    assert_eq!(resumed.calls()[0].action, PlannedAction::Run);
    assert!(!resumed.is_awaiting_approval());

    let reason = "user said no".to_owned();
    let deny = recorded(
        "w",
        input,
        ApprovalResponse::Deny {
            reason: reason.clone(),
        },
    );
    let denied = fx.plan_with(&blocks, &deny).await;
    assert_eq!(denied.calls()[0].action, PlannedAction::Denied { reason });

    let other_input = recorded("w", json!({"a": 9}), ApprovalResponse::Allow);
    let mismatch = fx.plan_with(&blocks, &other_input).await;
    assert!(
        mismatch.is_awaiting_approval(),
        "an approval covers only the input the host saw"
    );

    let other_call = recorded("other", json!({"a": 1, "b": 2}), ApprovalResponse::Allow);
    assert!(
        fx.plan_with(&blocks, &other_call)
            .await
            .is_awaiting_approval()
    );
}

#[tokio::test]
async fn a_recorded_allow_never_overrides_a_deny() {
    let log = new_log();
    let fx = Fixture::with_policy(deny_rule("Write"));
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    fx.tool(TestTool::new("Read", ToolScope::Read, &log));
    let mut approvals = recorded("w", json!({}), ApprovalResponse::Allow);
    let reason = "no reads".to_owned();
    let deny_read = ApprovalResponse::Deny {
        reason: reason.clone(),
    };
    approvals.extend(recorded("r", json!({}), deny_read));

    let plan = fx
        .plan_with(
            &[
                use_block("w", "Write", json!({})),
                use_block("r", "Read", json!({})),
            ],
            &approvals,
        )
        .await;

    assert!(matches!(
        plan.calls()[0].action,
        PlannedAction::Denied { .. }
    ));
    // A recorded deny is final even when policy would allow the call now.
    assert_eq!(plan.calls()[1].action, PlannedAction::Denied { reason });
}

/// R1-core 3: a hook may give the call another input on resume; the host's
/// deny still stands (fail closed), while its allow covers only the input
/// it saw.
#[tokio::test]
async fn a_recorded_deny_applies_whatever_the_input() {
    let log = new_log();
    let fx = Fixture::new(PermissionMode::Default);
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    let _rewrite = (fx.hooks.pre_tool_use)
        .register(hook::<PreToolUse>("rewrite", &log, rewrite_x_to(2)))
        .unwrap();
    let blocks = [use_block("w", "Write", json!({"x": 1}))];
    let reason = "user said no".to_owned();
    let deny = ApprovalResponse::Deny {
        reason: reason.clone(),
    };
    let seen = json!({"x": 1});
    let denied = fx
        .plan_with(&blocks, &recorded("w", seen.clone(), deny))
        .await;
    assert_eq!(denied.calls()[0].action, PlannedAction::Denied { reason });
    let allowed = (fx.plan_with(&blocks, &recorded("w", seen, ApprovalResponse::Allow))).await;
    assert!(allowed.is_awaiting_approval());
}

#[tokio::test]
async fn a_cached_result_is_served_only_when_permission_allows() {
    let log = new_log();
    let fx = Fixture::with_policy(deny_rule("Exec"));
    fx.tool(TestTool::new("Read", ToolScope::Read, &log));
    fx.tool(TestTool::new("Write", ToolScope::Write, &log));
    fx.tool(TestTool::new("Exec", ToolScope::Exec, &log));
    let pre = hook::<PreToolUse>("cache", &log, |_| cached());
    let _pre = fx.hooks.pre_tool_use.register(pre).unwrap();
    let blocks = [
        use_block("r", "Read", json!({})),
        use_block("w", "Write", json!({})),
        use_block("x", "Exec", json!({})),
    ];

    let plan = fx.plan(&blocks).await;

    let served = PlannedAction::Serve {
        result: Ok(ToolOutput::new("cached")),
    };
    assert_eq!(plan.calls()[0].action, served);
    assert!(matches!(
        plan.calls()[1].action,
        PlannedAction::AwaitingApproval { .. }
    ));
    assert!(matches!(
        plan.calls()[2].action,
        PlannedAction::Denied { .. }
    ));

    // On resume the approved call is served, not run.
    let approvals = recorded("w", json!({}), ApprovalResponse::Allow);
    let resumed = fx.plan_with(&blocks[1..2], &approvals).await;
    assert_eq!(resumed.calls()[0].action, served);
}
