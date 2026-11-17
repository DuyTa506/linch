//! The wire shapes of the turn points, checked against the protocol
//! document.

use std::sync::Arc;

use linch::{ContentBlock, Message, RunId, SessionId, StopReason, TextBlock};

use super::*;

fn ids() -> (SessionId, RunId) {
    (SessionId::from("s1"), RunId::from("r1"))
}

fn minimal_request(model: &str) -> linch::ProviderRequest {
    linch::ProviderRequest {
        model: model.into(),
        system: vec![],
        tools: vec![],
        appended_tools: vec![],
        context_tail: 0,
        messages: vec![Message::user_text("hi")],
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

#[test]
fn before_provider_call_carries_the_request_and_reads_each_action() {
    let (session_id, run_id) = ids();
    let request = BeforeProviderCallRequest {
        session_id,
        run_id,
        turn: 2,
        request: minimal_request("m"),
    };
    let wire = BeforeProviderCall::encode(&request);
    assert_eq!(wire["turn"], 2);
    assert_eq!(wire["request"]["model"], "m");
    assert_eq!(
        BeforeProviderCall::decode(json!({"action": "continue"})).unwrap(),
        BeforeProviderCallResponse::Continue
    );
    let rewrite = json!({"action": "rewrite", "request": serde_json::to_value(minimal_request("m2")).unwrap()});
    assert_eq!(
        BeforeProviderCall::decode(rewrite).unwrap(),
        BeforeProviderCallResponse::Rewrite {
            request: Box::new(minimal_request("m2"))
        }
    );
    let block = json!({"action": "block", "reason": "no budget"});
    assert!(matches!(
        BeforeProviderCall::decode(block).unwrap(),
        BeforeProviderCallResponse::Block { reason } if reason == "no budget"
    ));
    assert!(BeforeProviderCall::decode(json!({"action": "continue", "x": 1})).is_err());
}

fn minimal_assembly(text: &str) -> linch::AssistantAssembly {
    linch::AssistantAssembly {
        message: Message::assistant_text(text),
        stop_reason: StopReason::EndTurn,
        usage: None,
        model: "m".into(),
    }
}

#[test]
fn after_provider_call_carries_the_assembly_and_reads_each_action() {
    let (session_id, run_id) = ids();
    let request = AfterProviderCallRequest {
        session_id,
        run_id,
        turn: 2,
        assembly: minimal_assembly("hi"),
    };
    let wire = AfterProviderCall::encode(&request);
    assert_eq!(wire["turn"], 2);
    assert_eq!(wire["assembly"]["model"], "m");
    assert_eq!(
        AfterProviderCall::decode(json!({"action": "continue"})).unwrap(),
        AfterProviderCallResponse::Continue
    );
    let rewrite = json!({"action": "rewrite", "assembly": serde_json::to_value(minimal_assembly("bye")).unwrap()});
    assert_eq!(
        AfterProviderCall::decode(rewrite).unwrap(),
        AfterProviderCallResponse::Rewrite {
            assembly: Box::new(minimal_assembly("bye"))
        }
    );
    let block = json!({"action": "block", "reason": "unsafe content"});
    assert!(matches!(
        AfterProviderCall::decode(block).unwrap(),
        AfterProviderCallResponse::Block { reason } if reason == "unsafe content"
    ));
    assert!(AfterProviderCall::decode(json!({"action": "continue", "x": 1})).is_err());
}

/// Python treats `retry` and `force_continue` alike at
/// `after_provider_call`: accept the response, send the feedback, take
/// another turn.
#[test]
fn after_provider_call_reads_retry_and_force_continue_alike() {
    for action in ["retry", "force_continue"] {
        let answer = json!({"action": action, "feedback": "cite a source"});
        assert_eq!(
            AfterProviderCall::decode(answer).unwrap(),
            AfterProviderCallResponse::Retry {
                feedback: "cite a source".into()
            },
            "{action}"
        );
        assert!(
            AfterProviderCall::decode(json!({ "action": action })).is_err(),
            "{action} needs its feedback"
        );
    }
}

/// At `stop`, `force_continue` is Python's stop `force_continue`, which
/// `continue` already is; `retry` is Python's `before_final_answer` retry,
/// honoured once per run call.
#[test]
fn stop_reads_retry_and_force_continue() {
    let retry = json!({"action": "retry", "feedback": "be precise"});
    assert_eq!(
        Stop::decode(retry).unwrap(),
        StopResponse::Retry {
            feedback: "be precise".into()
        }
    );
    let more = json!({"action": "force_continue", "feedback": "keep going"});
    assert_eq!(
        Stop::decode(more).unwrap(),
        StopResponse::Continue {
            feedback: "keep going".into()
        }
    );
}

/// Every point that does not honour `retry` or `force_continue` refuses
/// them, so a callback written for Python fails loudly instead of being
/// read as something else. `before_provider_call` honours only
/// `force_continue`: Python ignores a `retry` there.
#[test]
fn points_that_do_not_honour_retry_refuse_it() {
    for action in ["retry", "force_continue"] {
        let answer = json!({"action": action, "feedback": "again"});
        assert!(PreToolUse::decode(answer.clone()).is_err(), "{action}");
        assert!(PostToolUse::decode(answer.clone()).is_err(), "{action}");
        assert!(
            UserPromptSubmit::decode(answer.clone()).is_err(),
            "{action}"
        );
        assert!(PermissionRequest::decode(answer).is_err(), "{action}");
    }
    let retry = json!({"action": "retry", "feedback": "again"});
    assert!(BeforeProviderCall::decode(retry).is_err());
}

/// `force_continue` at `before_provider_call` skips the call and takes
/// another turn with the feedback, as in Python.
#[test]
fn before_provider_call_reads_force_continue() {
    let answer = json!({"action": "force_continue", "feedback": "plan first"});
    assert_eq!(
        BeforeProviderCall::decode(answer).unwrap(),
        BeforeProviderCallResponse::ForceContinue {
            feedback: "plan first".into()
        }
    );
    let bare = json!({"action": "force_continue"});
    assert!(
        BeforeProviderCall::decode(bare).is_err(),
        "force_continue needs its feedback"
    );
}

#[test]
fn user_prompt_submit_carries_content_and_reads_each_action() {
    let (session_id, run_id) = ids();
    let request = UserPromptRequest {
        session_id,
        run_id,
        content: vec![ContentBlock::text("hi")],
    };
    let wire = UserPromptSubmit::encode(&request);
    assert_eq!(
        wire,
        json!({"session_id": "s1", "run_id": "r1",
               "content": [{"type": "text", "text": "hi"}]})
    );
    let rewrite = json!({"action": "rewrite", "content": [{"type": "text", "text": "hello"}]});
    assert_eq!(
        UserPromptSubmit::decode(rewrite).unwrap(),
        UserPromptResponse::Rewrite {
            content: vec![ContentBlock::text("hello")]
        }
    );
    let block = json!({"action": "block", "reason": "no"});
    assert!(matches!(
        UserPromptSubmit::decode(block).unwrap(),
        UserPromptResponse::Block { reason } if reason == "no"
    ));
    assert!(UserPromptSubmit::decode(json!({"action": "continue", "x": 1})).is_err());
}

/// Linch 3.x #102: `retry` / `force_continue` are not `pre_tool_use` answers,
/// so they cannot cut the chain and let the tool run.
#[test]
fn pre_tool_use_refuses_actions_the_point_does_not_honour() {
    for action in ["retry", "force_continue"] {
        assert!(
            PreToolUse::decode(json!({ "action": action })).is_err(),
            "{action}"
        );
    }
    assert!(PreToolUse::decode(json!({"action": "continue"})).is_ok());
}

#[test]
fn context_build_carries_the_messages_and_reads_blocks() {
    let (session_id, run_id) = ids();
    let request = ContextBuildRequest {
        session_id,
        run_id,
        messages: Arc::new(vec![Arc::new(Message::user_text("hi"))]),
        principal: None,
    };
    let wire = ContextBuild::encode(&request);
    assert_eq!(wire["messages"][0]["role"], "user");
    assert_eq!(wire["messages"][0]["content"][0]["text"], "hi");
    let blocks = ContextBuild::decode(json!({"blocks": [{"text": "fact"}]})).unwrap();
    let fact = TextBlock {
        text: "fact".into(),
    };
    assert_eq!(blocks.blocks, [fact]);
    let empty = ContextBuild::decode(json!({})).unwrap();
    assert!(empty.blocks.is_empty() && empty.metadata.is_empty());
    let tagged = ContextBuild::decode(json!({"metadata": {"rag": "p1"}})).unwrap();
    assert_eq!(tagged.metadata["rag"], "p1");
}

#[test]
fn stop_and_post_turn_read_their_answers() {
    let (session_id, run_id) = ids();
    let request = StopRequest {
        session_id: session_id.clone(),
        run_id: run_id.clone(),
        text: "done".into(),
        structured_output: None,
    };
    assert_eq!(
        Stop::encode(&request),
        json!({"session_id": "s1", "run_id": "r1", "text": "done"})
    );
    let shaped = StopRequest {
        text: "{\"total\": 1}".into(),
        structured_output: json!({"total": 1}).as_object().cloned(),
        ..request.clone()
    };
    assert_eq!(
        Stop::encode(&shaped),
        json!({"session_id": "s1", "run_id": "r1", "text": "{\"total\": 1}",
               "structured_output": {"total": 1}})
    );
    assert_eq!(
        Stop::decode(json!({"action": "allow"})).unwrap(),
        StopResponse::Allow
    );
    let more = json!({"action": "continue", "feedback": "check the tests"});
    assert_eq!(
        Stop::decode(more).unwrap(),
        StopResponse::Continue {
            feedback: "check the tests".into()
        }
    );
    let rewrite = json!({"action": "rewrite", "text": "Done!"});
    assert_eq!(
        Stop::decode(rewrite).unwrap(),
        StopResponse::Rewrite {
            text: "Done!".into()
        }
    );
    let block = json!({"action": "block", "reason": "unsafe content"});
    assert_eq!(
        Stop::decode(block).unwrap(),
        StopResponse::Block {
            reason: "unsafe content".into()
        }
    );

    let turn = PostTurnRequest {
        session_id,
        run_id,
        turn: 2,
        stop_reason: StopReason::EndTurn,
    };
    assert_eq!(
        PostTurn::encode(&turn),
        json!({"session_id": "s1", "run_id": "r1", "turn": 2, "stop_reason": "end_turn"})
    );
    PostTurn::decode(json!({})).unwrap();
    assert!(PostTurn::decode(json!({"verdict": "ok"})).is_err());
}

/// `event_emit` carries the event about to be delivered, under its own
/// `type`-tagged wire shape, and answers like every other observe-only
/// point.
#[test]
fn event_emit_carries_the_event_and_answers_like_an_observe_only_point() {
    let (session_id, run_id) = ids();
    let request = EventEmitRequest {
        session_id,
        run_id,
        event: linch::Event::User(linch::events::kinds::UserEvent {
            message: Message::user_text("hi"),
            subtype: linch::events::kinds::UserEventSubtype::Prompt,
        }),
    };
    assert_eq!(
        EventEmit::encode(&request),
        json!({"session_id": "s1", "run_id": "r1", "event": {
            "type": "user",
            "message": Message::user_text("hi"),
            "subtype": "prompt",
        }})
    );
    EventEmit::decode(json!({})).unwrap();
    assert!(EventEmit::decode(json!({"verdict": "ok"})).is_err());
}
