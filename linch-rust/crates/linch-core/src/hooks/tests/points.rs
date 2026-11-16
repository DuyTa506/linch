//! Per-point semantics, registration errors, and the policy digest.

use crate::events::kinds::ResultSubtype;
use crate::events::model_call::CallDisposition;
use crate::message::{ContentBlock, TextBlock};
use crate::policy::{PermissionDecision, Principal};
use crate::run::{RunStatus, StopReason};
use crate::tool::ToolOutputError;
use crate::usage::Usage;

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

fn minimal_request(model: &str) -> crate::provider::ProviderRequest {
    crate::provider::ProviderRequest {
        model: model.into(),
        system: vec![],
        tools: vec![],
        appended_tools: vec![],
        messages: vec![],
        max_output_tokens: None,
        temperature: None,
        stop_sequences: vec![],
        effort: None,
        thinking: None,
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

fn before_call(model: &str) -> BeforeProviderCallRequest {
    BeforeProviderCallRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        request: minimal_request(model),
    }
}

#[tokio::test]
async fn before_provider_call_rewrites_chain_and_a_block_stops() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.before_provider_call,
        Scripted::new("downgrade", &log, |req: &BeforeProviderCallRequest| {
            let mut request = req.request.clone();
            request.model = "cheaper".into();
            Act::Answer(BeforeProviderCallResponse::Rewrite {
                request: Box::new(request),
            })
        }),
    );
    let mut records = Vec::new();
    let sent = hooks
        .before_provider_call
        .run_into(before_call("gpt"), &mut records)
        .await;
    assert_eq!(
        sent,
        BeforeProviderCallOutcome::Send {
            request: Box::new(minimal_request("cheaper"))
        }
    );

    let _ = register(
        &hooks.before_provider_call,
        Scripted::new("budget", &log, |_| {
            Act::Answer(BeforeProviderCallResponse::Block {
                reason: "over budget".into(),
            })
        }),
    );
    let mut records = Vec::new();
    let blocked = hooks
        .before_provider_call
        .run_into(before_call("gpt"), &mut records)
        .await;
    assert_eq!(
        blocked,
        BeforeProviderCallOutcome::Blocked {
            reason: "over budget".into()
        }
    );
    assert_eq!(calls(&log), ["downgrade", "downgrade", "budget"]);
}

fn minimal_assembly(text: &str) -> crate::provider::AssistantAssembly {
    crate::provider::AssistantAssembly {
        message: crate::message::Message::assistant_text(text),
        stop_reason: StopReason::EndTurn,
        usage: None,
        model: "m".into(),
    }
}

fn after_call(text: &str) -> AfterProviderCallRequest {
    AfterProviderCallRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        assembly: minimal_assembly(text),
    }
}

#[tokio::test]
async fn after_provider_call_rewrites_chain_and_a_block_stops() {
    let hooks = Hooks::default();
    let log = new_log();
    let _ = register(
        &hooks.after_provider_call,
        Scripted::new("redact", &log, |req: &AfterProviderCallRequest| {
            let mut assembly = req.assembly.clone();
            assembly.message = crate::message::Message::assistant_text("[redacted]");
            Act::Answer(AfterProviderCallResponse::Rewrite {
                assembly: Box::new(assembly),
            })
        }),
    );
    let mut records = Vec::new();
    let sent = hooks
        .after_provider_call
        .run_into(after_call("the secret is 42"), &mut records)
        .await;
    assert_eq!(
        sent,
        AfterProviderCallOutcome::Send {
            assembly: Box::new(minimal_assembly("[redacted]"))
        }
    );

    let _ = register(
        &hooks.after_provider_call,
        Scripted::new("moderate", &log, |_| {
            Act::Answer(AfterProviderCallResponse::Block {
                reason: "unsafe content".into(),
            })
        }),
    );
    let mut records = Vec::new();
    let blocked = hooks
        .after_provider_call
        .run_into(after_call("the secret is 42"), &mut records)
        .await;
    assert_eq!(
        blocked,
        AfterProviderCallOutcome::Blocked {
            reason: "unsafe content".into()
        }
    );
    assert_eq!(calls(&log), ["redact", "redact", "moderate"]);
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
                    ..ContextBuildResponse::default()
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
    let texts: Vec<_> = (done.outcome.blocks.iter())
        .map(|b| b.text.as_str())
        .collect();
    assert_eq!(texts, ["user: ada", "doc 7"]);
    assert_eq!(done.records.len(), 3);
}

#[tokio::test]
async fn stop_continues_on_the_first_feedback_and_fails_closed() {
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

    // Open by default; closed fails the run.
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
        StopOutcome::Failed {
            reason: "hook `v` failed".into()
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

/// Every observe-only point runs its whole chain, in order, and records a
/// failure instead of letting it stop later hooks: the same shape as
/// [`post_turn_observes_every_hook_and_records_failures`], once per point.
macro_rules! observe_point_test {
    ($test_name:ident, $chain:ident, $request:expr) => {
        #[tokio::test]
        async fn $test_name() {
            let hooks = Hooks::default();
            let log = new_log();
            let _ = register(&hooks.$chain, Scripted::new("dead", &log, |_| Act::Fail));
            let _ = register(
                &hooks.$chain,
                Scripted::new("audit", &log, |_| Act::Answer(())),
            );
            let request = $request;
            let done = hooks.$chain.run(&request).await;
            assert_eq!(calls(&log), ["dead", "audit"]);
            assert!(matches!(done.records[0].status, HookStatus::Failed { .. }));
            assert_eq!(
                done.records[1].status,
                HookStatus::Answered { verdict: "observe" }
            );
        }
    };
}

observe_point_test!(
    agent_start_observes_every_hook_and_records_failures,
    agent_start,
    AgentStartRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        model: "gpt-5".into(),
        prompt: "hi".into(),
        tools: vec!["Read".into()],
    }
);

observe_point_test!(
    agent_stop_observes_every_hook_and_records_failures,
    agent_stop,
    AgentStopRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        status: RunStatus::Completed,
        stop_reason: StopReason::EndTurn,
    }
);

observe_point_test!(
    turn_start_observes_every_hook_and_records_failures,
    turn_start,
    TurnStartRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
    }
);

observe_point_test!(
    provider_call_start_observes_every_hook_and_records_failures,
    provider_call_start,
    ProviderCallStartRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        model: "gpt-5".into(),
    }
);

observe_point_test!(
    provider_call_stop_observes_every_hook_and_records_failures,
    provider_call_stop,
    ProviderCallStopRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        model: "gpt-5".into(),
        disposition: CallDisposition::Accepted,
        usage: Some(Usage::default()),
        duration_ms: 12,
    }
);

observe_point_test!(
    tool_use_start_observes_every_hook_and_records_failures,
    tool_use_start,
    ToolUseStartRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        tool_use_id: "toolu_1".into(),
        tool_name: "Read".into(),
        input: JsonObject::new(),
    }
);

observe_point_test!(
    tool_use_stop_observes_every_hook_and_records_failures,
    tool_use_stop,
    ToolUseStopRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        tool_use_id: "toolu_1".into(),
        tool_name: "Read".into(),
        is_error: false,
        duration_ms: 12,
    }
);

observe_point_test!(
    post_tool_use_failure_observes_every_hook_and_records_failures,
    post_tool_use_failure,
    PostToolUseFailureRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        tool_use_id: "toolu_1".into(),
        tool_name: "Read".into(),
        input: JsonObject::new(),
        error: ToolOutputError::new("boom"),
    }
);

observe_point_test!(
    pre_compact_observes_every_hook_and_records_failures,
    pre_compact,
    PreCompactRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        messages: 40,
        tokens: 9000,
        strategy: "summary".into(),
    }
);

observe_point_test!(
    post_compact_observes_every_hook_and_records_failures,
    post_compact,
    PostCompactRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        turn: 0,
        messages_before: 40,
        messages_after: 3,
        tokens_before: 9000,
        tokens_after: 500,
        strategy: "summary".into(),
    }
);

observe_point_test!(
    subagent_start_observes_every_hook_and_records_failures,
    subagent_start,
    SubagentStartRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        child_session_id: SessionId::from("child"),
        subagent_run_id: RunId::from("r:toolu_1"),
        subagent_type: "general-purpose".into(),
        display_name: "Agent #1".into(),
        prompt: "look into it".into(),
    }
);

observe_point_test!(
    subagent_stop_observes_every_hook_and_records_failures,
    subagent_stop,
    SubagentStopRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        child_session_id: SessionId::from("child"),
        subagent_run_id: RunId::from("r:toolu_1"),
        subagent_type: "general-purpose".into(),
        display_name: "Agent #1".into(),
        subtype: ResultSubtype::Success,
        text: "done".into(),
        error: None,
    }
);

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

/// None of the twelve observe-only points has a blocking verdict, so a
/// closed fail mode is refused at every one of them, the same way it is at
/// `post_turn`.
#[test]
fn every_observe_only_point_rejects_a_closed_fail_mode() {
    let hooks = Hooks::default();
    let log = new_log();
    macro_rules! assert_rejects_closed {
        ($chain:ident) => {
            let closed = Scripted::new("c", &log, |_| Act::Answer(())).fail_mode(FailMode::Closed);
            assert!(
                matches!(
                    hooks.$chain.register(Arc::new(closed)),
                    Err(HookError::FailClosedNotSupported { .. })
                ),
                "{} should refuse a closed fail mode",
                stringify!($chain)
            );
        };
    }
    assert_rejects_closed!(agent_start);
    assert_rejects_closed!(agent_stop);
    assert_rejects_closed!(turn_start);
    assert_rejects_closed!(provider_call_start);
    assert_rejects_closed!(provider_call_stop);
    assert_rejects_closed!(tool_use_start);
    assert_rejects_closed!(tool_use_stop);
    assert_rejects_closed!(post_tool_use_failure);
    assert_rejects_closed!(pre_compact);
    assert_rejects_closed!(post_compact);
    assert_rejects_closed!(subagent_start);
    assert_rejects_closed!(subagent_stop);
}

#[test]
fn the_digest_covers_each_points_chain_in_order_and_pins_its_encoding() {
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
    // Points in their fixed order, each chain in registration order with
    // its duplicate kept (R1-rpc 9).
    let expected = json!([
        ["pre_tool_use", "cache", 1],
        ["pre_tool_use", "cache", 1],
        ["post_tool_use", "redact", 2]
    ]);
    assert_eq!(
        hooks.policy_digest(),
        digest_with_domain(POLICY_DOMAIN, &expected).unwrap()
    );

    post.dispose();
    let after = json!([["pre_tool_use", "cache", 1], ["pre_tool_use", "cache", 1]]);
    assert_eq!(
        hooks.policy_digest(),
        digest_with_domain(POLICY_DOMAIN, &after).unwrap()
    );
}

/// The digest's fixed point order was extended for the twelve observe-only
/// points, each contributing its own `(point, id, version)` triples after
/// `post_turn`.
#[test]
fn the_digest_covers_the_observe_only_points_too() {
    let log = new_log();
    let hooks = Hooks::default();
    let before = hooks.policy_digest();
    let registration = register(
        &hooks.agent_start,
        Scripted::new("audit", &log, |_| Act::Answer(())),
    );
    let with_agent_start = hooks.policy_digest();
    assert_ne!(before, with_agent_start);
    let expected = json!([["agent_start", "audit", 1]]);
    assert_eq!(
        with_agent_start,
        digest_with_domain(POLICY_DOMAIN, &expected).unwrap()
    );
    let _ = register(
        &hooks.subagent_stop,
        Scripted::new("notify", &log, |_| Act::Answer(())),
    );
    let with_both = hooks.policy_digest();
    assert_ne!(with_both, with_agent_start);
    let expected = json!([["agent_start", "audit", 1], ["subagent_stop", "notify", 1]]);
    assert_eq!(
        with_both,
        digest_with_domain(POLICY_DOMAIN, &expected).unwrap()
    );

    registration.dispose();
    let expected = json!([["subagent_stop", "notify", 1]]);
    assert_eq!(
        hooks.policy_digest(),
        digest_with_domain(POLICY_DOMAIN, &expected).unwrap()
    );
}

/// A point's hook order decides what its chain does, so order and repeats count.
#[test]
fn the_digest_changes_with_a_chains_order_and_duplicates() {
    let log = new_log();
    let chain = |ids: &[&str]| {
        let hooks = Hooks::default();
        for id in ids {
            let continue_ = |_: &PreToolUseRequest| Act::Answer(PreToolUseResponse::Continue);
            let _ = register(&hooks.pre_tool_use, Scripted::new(id, &log, continue_));
        }
        hooks.policy_digest()
    };
    assert_eq!(chain(&["a", "b"]), chain(&["a", "b"]));
    assert_ne!(chain(&["a", "b"]), chain(&["b", "a"]));
    assert_ne!(chain(&["a"]), chain(&["a", "a"]));
}
