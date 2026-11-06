//! Sessions over RPC: `session.close` forgets what the session read.

mod common;

use common::*;
use linch_rpc::code;
use linch_rpc::protocol::method;
use serde_json::json;

#[tokio::test]
async fn session_close_forgets_what_the_session_read() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("notes.txt"), "alpha\n").unwrap();
    let mut rpc = Rpc::new();
    let write = json!({"file_path": "notes.txt", "content": "beta\n"});
    let turns = vec![
        tool_turn("call_1", "Read", json!({"file_path": "notes.txt"})),
        text_turn("Read it."),
        tool_turn("call_2", "Write", write),
        text_turn("Tried."),
    ];
    let agent = rpc
        .open(scripted(turns, Some(dir.path()), "accept_edits"))
        .await;
    let read = rpc.start(&agent, "s1", "Read notes.txt").await;
    assert!(rpc.until_end(&read).await.error.is_none());

    let close = json!({"agent": agent, "session_id": "s1"});
    let closed = rpc.call(method::SESSION_CLOSE, close).await.unwrap();
    assert_eq!(closed, json!({}));

    let write = rpc.start(&agent, "s1", "Overwrite notes.txt").await;
    let ended = rpc.until_end(&write).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let results = tool_results(&ended);
    assert_eq!(results.len(), 1, "{results:?}");
    assert_eq!(results[0]["is_error"], json!(true), "{results:?}");
    let text = results[0].to_string();
    assert!(text.contains("Read notes.txt before changing it"), "{text}");
    let kept = std::fs::read_to_string(dir.path().join("notes.txt")).unwrap();
    assert_eq!(kept, "alpha\n");

    let unknown = json!({"agent": "agent-99", "session_id": "s1"});
    let error = rpc.call(method::SESSION_CLOSE, unknown).await.unwrap_err();
    assert_eq!(code_of(error), code::AGENT_NOT_FOUND);
}
