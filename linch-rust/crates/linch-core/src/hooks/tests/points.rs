//! Per-point semantics, registration errors, and the policy digest.

use crate::message::{ContentBlock, TextBlock};
use crate::policy::{PermissionDecision, Principal};
use crate::run::StopReason;
use crate::tool::ToolOutputError;

use super::*;

fn approval(tool: &str) -> ApprovalRequest {
    ApprovalRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        tool_use_id: "toolu_1".into(),
        tool_name: tool.into(),
        input: JsonObject::new(),
        reason: Some("writes outside the project".into()),
        principal: Some(Principal::new("t", "u", ["dev"]).unwrap()),
    }
}

fn allow() -> Act<ApprovalResponse> {
    Act::Answer(ApprovalResponse::Allow)
}

#[tokio::test]
async fn an_allow_does_not_skip_a_later_deny() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.permission_request,
        Scripted::new("ok", &log, |_| allow()),
    );
    let _ = register(
        &hooks.permission_request,
        Scripted::new("no", &log, |_| {
            Act::Answer(ApprovalResponse::Deny {
                reason: "prod".into(),
            })
        }),
    );
    let done = hooks.permission_request.run(&approval("Bash")).await;
    assert_eq!(calls(&log), ["ok", "no"]);
    assert_eq!(
        done.outcome,
        PermissionDecision::Deny {
            reason: "prod".into()
        }
    );
}

#[tokio::test]
async fn permission_allows_asks_or_fails_closed() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.permission_request,
        Scripted::new("bash_only", &log, |_| allow()).matcher(&["Bash"]),
    );
    let allowed = hooks.permission_request.run(&approval("Bash")).await;
    assert_eq!(allowed.outcome, PermissionDecision::Allow {});
    let unanswered = hooks.permission_request.run(&approval("Write")).await;
    assert_eq!(
        unanswered.outcome,
        PermissionDecision::Ask {
            reason: Some("writes outside the project".into())
        },
        "no matching hook leaves the call to the host"
    );

    let _ = register(
        &hooks.permission_request,
        Scripted::new("dead", &log, |_| Act::Fail),
    );
    let failed = hooks.permission_request.run(&approval("Bash")).await;
    assert_eq!(
        failed.outcome,
        PermissionDecision::Deny {
            reason: "hook `dead` failed".into()
        }
    );

    // Failing open never allows: the call still waits for the host.
    let hooks = Hooks::default();
    let _ = register(
        &hooks.permission_request,
        Scripted::new("dead", &log, |_| Act::Fail).fail_mode(FailMode::Open),
    );
    let open = hooks.permission_request.run(&approval("Bash")).await;
    assert!(matches!(open.outcome, PermissionDecision::Ask { .. }));
}

fn post_request(result: ToolResult) -> PostToolUseRequest {
    PostToolUseRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        tool_use_id: "toolu_1".into(),
        tool_name: "Read".into(),
        input: JsonObject::new(),
        result,
    }
}

#[tokio::test]
async fn post_tool_use_replacements_chain_and_closed_failures_withhold() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.post_tool_use,
        Scripted::new("redact", &log, |req: &PostToolUseRequest| {
            let text = req.result.as_ref().unwrap().value.as_str().unwrap();
            Act::Answer(PostToolUseResponse::Replace {
                result: Ok(ToolOutput::new(text.replace("secret", "***"))),
            })
        }),
    );
    let _ = register(
        &hooks.post_tool_use,
        Scripted::new("shout", &log, |req: &PostToolUseRequest| {
            let text = req.result.as_ref().unwrap().value.as_str().unwrap();
            Act::Answer(PostToolUseResponse::Replace {
                result: Ok(ToolOutput::new(text.to_uppercase())),
            })
        }),
    );
    let done = hooks
        .post_tool_use
        .run(post_request(Ok(ToolOutput::new("a secret"))))
        .await;
    assert_eq!(done.outcome, Ok(ToolOutput::new("A ***")));

    // Open by default: a failing hook keeps the result as it stands.
    let hooks = Hooks::default();
    let _ = register(
        &hooks.post_tool_use,
        Scripted::new("dead", &log, |_| Act::Fail),
    );
    let kept = hooks
        .post_tool_use
        .run(post_request(Ok(ToolOutput::new("x"))))
        .await;
    assert_eq!(kept.outcome, Ok(ToolOutput::new("x")));

    let hooks = Hooks::default();
    let _ = register(
        &hooks.post_tool_use,
        Scripted::new("redact", &log, |_| Act::Fail).fail_mode(FailMode::Closed),
    );
    let withheld = hooks
        .post_tool_use
        .run(post_request(Ok(ToolOutput::new("a secret"))))
        .await;
    assert_eq!(
        withheld.outcome,
        Err(ToolOutputError::new(
            "hook `redact` failed; the tool result was withheld"
        ))
    );
}

fn prompt(text: &str) -> UserPromptRequest {
    UserPromptRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        content: vec![ContentBlock::text(text)],
    }
}

#[tokio::test]
async fn user_prompt_rewrites_chain_and_a_block_stops() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.user_prompt_submit,
        Scripted::new("expand", &log, |req: &UserPromptRequest| {
            let mut content = req.content.clone();
            content.push(ContentBlock::text("(be brief)"));
            Act::Answer(UserPromptResponse::Rewrite { content })
        }),
    );
    let done = hooks.user_prompt_submit.run(prompt("hi")).await;
    assert_eq!(
        done.outcome,
        UserPromptOutcome::Submit {
            content: vec![ContentBlock::text("hi"), ContentBlock::text("(be brief)")],
        }
    );

    let _ = register(
        &hooks.user_prompt_submit,
        Scripted::new("moderate", &log, |_| {
            Act::Answer(UserPromptResponse::Block {
                reason: "off topic".into(),
            })
        }),
    );
    let blocked = hooks.user_prompt_submit.run(prompt("hi")).await;
    assert_eq!(
        blocked.outcome,
        UserPromptOutcome::Blocked {
            reason: "off topic".into()
        }
    );
}

#[tokio::test]
async fn context_blocks_accumulate_in_order_and_failures_add_nothing() {
    let hooks = Hooks::default();
    let log = new_log();
    for (id, text) in [("profile", "user: ada"), ("rag", "doc 7")] {
        let _ = register(
            &hooks.context_build,
            Scripted::new(id, &log, move |_| {
                Act::Answer(ContextBuildResponse {
                    blocks: vec![TextBlock { text: text.into() }],
                })
            }),
        );
    }
    let _ = register(
        &hooks.context_build,
        Scripted::new("dead_rag", &log, |_| Act::Hang).timeout(SHORT),
    );
    let request = ContextBuildRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        messages: Arc::new(Vec::new()),
    };
    let done = hooks.context_build.run(&request).await;
    let texts: Vec<_> = done.outcome.iter().map(|b| b.text.as_str()).collect();
    assert_eq!(texts, ["user: ada", "doc 7"]);
    assert_eq!(done.records.len(), 3);
}

#[tokio::test]
async fn stop_continues_on_the_first_feedback_or_a_closed_failure() {
    let request = StopRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        text: "done".into(),
    };
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.stop,
        Scripted::new("ok", &log, |_| Act::Answer(StopResponse::Allow)),
    );
    assert_eq!(hooks.stop.run(&request).await.outcome, StopOutcome::Stop);
    let _ = register(
        &hooks.stop,
        Scripted::new("cite", &log, |_| {
            Act::Answer(StopResponse::Continue {
                feedback: "cite sources".into(),
            })
        }),
    );
    let _ = register(
        &hooks.stop,
        Scripted::new("never", &log, |_| Act::Answer(StopResponse::Allow)),
    );
    let done = hooks.stop.run(&request).await;
    assert_eq!(
        done.outcome,
        StopOutcome::Continue {
            feedback: "cite sources".into()
        }
    );
    assert!(!calls(&log).contains(&"never".to_owned()));

    // Open by default; closed blocks the stop.
    let hooks = Hooks::default();
    let _ = register(&hooks.stop, Scripted::new("v", &log, |_| Act::Fail));
    assert_eq!(hooks.stop.run(&request).await.outcome, StopOutcome::Stop);
    let hooks = Hooks::default();
    let _ = register(
        &hooks.stop,
        Scripted::new("v", &log, |_| Act::Fail).fail_mode(FailMode::Closed),
    );
    assert_eq!(
        hooks.stop.run(&request).await.outcome,
        StopOutcome::Continue {
            feedback: "hook `v` failed".into()
        }
    );
}

#[tokio::test]
async fn post_turn_observes_every_hook_and_records_failures() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(&hooks.post_turn, Scripted::new("dead", &log, |_| Act::Fail));
    let _ = register(
        &hooks.post_turn,
        Scripted::new("audit", &log, |_| Act::Answer(())),
    );
    let request = PostTurnRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        stop_reason: StopReason::EndTurn,
    };
    let done = hooks.post_turn.run(&request).await;
    assert_eq!(calls(&log), ["dead", "audit"]);
    assert!(matches!(done.records[0].status, HookStatus::Failed { .. }));
    assert_eq!(
        done.records[1].status,
        HookStatus::Answered { verdict: "observe" }
    );
}

#[test]
fn registration_rejects_bad_settings() {
    let hooks = Hooks::default();
    let log = new_log();
    let blank = Scripted::new(" ", &log, |_| Act::Answer(()));
    assert_eq!(
        hooks.post_turn.register(Arc::new(blank)).unwrap_err(),
        HookError::BlankPolicyId
    );
    let matched = Scripted::new("m", &log, |_| Act::Answer(StopResponse::Allow)).matcher(&["*"]);
    assert_eq!(
        hooks.stop.register(Arc::new(matched)).unwrap_err(),
        HookError::MatcherNotSupported {
            point: HookPoint::Stop
        }
    );
    let closed = Scripted::new("c", &log, |_| Act::Answer(ContextBuildResponse::default()))
        .fail_mode(FailMode::Closed);
    assert_eq!(
        hooks.context_build.register(Arc::new(closed)).unwrap_err(),
        HookError::FailClosedNotSupported {
            point: HookPoint::ContextBuild
        }
    );
    let closed = Scripted::new("c", &log, |_| Act::Answer(())).fail_mode(FailMode::Closed);
    assert!(hooks.post_turn.register(Arc::new(closed)).is_err());
    assert!(hooks.context_build.is_empty() && hooks.post_turn.is_empty());
}

#[test]
fn the_digest_covers_the_policy_set_and_pins_its_encoding() {
    let log = new_log();
    let empty = Hooks::default();
    assert_eq!(
        empty.policy_digest(),
        digest_with_domain(POLICY_DOMAIN, &json!([])).unwrap()
    );

    let hooks = Hooks::default();
    let post = register(
        &hooks.post_tool_use,
        Scripted::new("redact", &log, |_| {
            Act::Answer(PostToolUseResponse::Continue)
        })
        .version(2),
    );
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("cache", &log, |_| Act::Answer(PreToolUseResponse::Continue)),
    );
    let _ = register(
        &hooks.pre_tool_use,
        Scripted::new("cache", &log, |_| Act::Answer(PreToolUseResponse::Continue)),
    );
    let expected = json!([["pre_tool_use", "cache", 1], ["post_tool_use", "redact", 2]]);
    assert_eq!(
        hooks.policy_digest(),
        digest_with_domain(POLICY_DOMAIN, &expected).unwrap()
    );

    post.dispose();
    let after = json!([["pre_tool_use", "cache", 1]]);
    assert_eq!(
        hooks.policy_digest(),
        digest_with_domain(POLICY_DOMAIN, &after).unwrap()
    );
}
