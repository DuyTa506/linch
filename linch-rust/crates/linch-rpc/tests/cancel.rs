//! Cancel over RPC: a run blocked in `Bash sleep 30` is aborted, while a
//! second run on the same server finishes alongside it.

mod common;

use std::time::{Duration, Instant};

use common::*;
use linch::Event;
use linch::events::kinds::ResultSubtype;
use linch_rpc::protocol::{Cancelled, method};
use linch_rpc::{ClientError, code};
use serde_json::json;

#[tokio::test]
async fn cancel_aborts_a_run_blocked_in_a_tool() {
    let dir = tempfile::tempdir().unwrap();
    let mut rpc = Rpc::new();
    let sleep = tool_turn("call_1", "Bash", json!({"command": "sleep 30"}));
    let config = scripted(
        vec![sleep, text_turn("unused")],
        Some(dir.path()),
        "skip_dangerous",
    );
    let agent = rpc.open(config).await;
    let started = Instant::now();
    let run_id = rpc.start(&agent, "s1", "Sleep.").await;
    loop {
        let envelope = rpc.next_event(&run_id).await.expect("still running");
        if matches!(envelope.event, Event::ToolCallStart(_)) {
            break;
        }
    }

    // The sleeping run is still driven: another run finishes meanwhile, and
    // a second driver for the same run is refused.
    let other = rpc.open(scripted(vec![text_turn("quick")], None, "")).await;
    let quick = rpc.start(&other, "s2", "go").await;
    assert!(rpc.until_end(&quick).await.error.is_none());
    let resume = json!({"agent": agent, "run_id": run_id});
    let busy = rpc.call(method::RUN_RESUME, resume).await.unwrap_err();
    assert!(
        matches!(&busy, ClientError::Rpc(e) if e.code == code::RUN_BUSY),
        "{busy:?}"
    );

    let cancel = json!({"run_id": run_id});
    let cancelled: Cancelled = rpc.client.call(method::RUN_CANCEL, &cancel).await.unwrap();
    assert!(cancelled.cancelled);
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    match ended.last() {
        Event::Result(result) => assert_eq!(result.subtype, ResultSubtype::Aborted),
        other => panic!("expected a result, got {other:?}"),
    }
    assert!(started.elapsed() < Duration::from_secs(20));

    let again: Cancelled = rpc.client.call(method::RUN_CANCEL, &cancel).await.unwrap();
    assert!(!again.cancelled);
}
