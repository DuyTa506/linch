//! The wire shapes of the turn points, checked against the protocol
//! document.

use std::sync::Arc;

use linch::{ContentBlock, Message, RunId, SessionId, StopReason, TextBlock};

use super::*;

fn ids() -> (SessionId, RunId) {
    (SessionId::from("s1"), RunId::from("r1"))
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

#[test]
fn context_build_carries_the_messages_and_reads_blocks() {
    let (session_id, run_id) = ids();
    let request = ContextBuildRequest {
        session_id,
        run_id,
        messages: Arc::new(vec![Arc::new(Message::user_text("hi"))]),
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
    };
    assert_eq!(
        Stop::encode(&request),
        json!({"session_id": "s1", "run_id": "r1", "text": "done"})
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
