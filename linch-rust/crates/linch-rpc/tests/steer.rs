//! `run.steer` over RPC: a suspended run takes a steer, answers `{queued, id}`,
//! delivers it as an `alignment` user message once approved, refuses a bad or
//! a late one with its own code, and a tenant reaches only its own runs.

mod common;

use common::*;
use linch::events::kinds::{ResultSubtype, UserEventSubtype};
use linch::{Event, RunId};
use linch_rpc::protocol::{SteerParams, Steered, method};
use linch_rpc::{Allowlists, code};
use serde_json::{Value, json};

struct Suspended {
    rpc: Rpc,
    agent: String,
    run_id: RunId,
}

/// A run that asked to write `new.txt` and has suspended on it.
async fn suspended() -> (Suspended, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let mut rpc = Rpc::new();
    let input = json!({"file_path": "new.txt", "content": "written\n"});
    let turns = vec![tool_turn("call_1", "Write", input), text_turn("Done.")];
    let agent = rpc.open(scripted(turns, Some(dir.path()), "default")).await;
    let run_id = rpc.start(&agent, "s1", "Write new.txt").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(matches!(ended.last(), Event::Suspended(_)));
    (Suspended { rpc, agent, run_id }, dir)
}

impl Suspended {
    async fn steer(&self, text: &str, id: Option<&str>) -> Result<Steered, linch_rpc::ClientError> {
        let mut params = json!({"agent": self.agent, "run_id": self.run_id, "text": text});
        if let Some(id) = id {
            params["id"] = json!(id);
        }
        let answer = self.rpc.call(method::RUN_STEER, params).await?;
        Ok(serde_json::from_value(answer).unwrap())
    }
}

#[test]
fn the_params_leave_unset_fields_off_the_wire() {
    let params = SteerParams {
        agent: "a1".into(),
        run_id: RunId::from("r1"),
        text: "go".into(),
        id: None,
        principal: None,
    };
    let value = serde_json::to_value(&params).unwrap();
    assert_eq!(value, json!({"agent": "a1", "run_id": "r1", "text": "go"}));
}

#[tokio::test]
async fn a_suspended_run_takes_a_steer_and_delivers_it_after_the_approval() {
    let (mut run, dir) = suspended().await;
    let steered = run.steer("use tabs", Some("st-1")).await.unwrap();
    assert_eq!((steered.queued, steered.id.as_str()), (true, "st-1"));
    let again = run.steer("use tabs", Some("st-1")).await.unwrap();
    assert_eq!(again.id, "st-1", "a resent id is taken once");
    let made = run.steer("and spaces never", None).await.unwrap();
    assert!(!made.id.is_empty());

    let params = json!({
        "agent": run.agent, "run_id": run.run_id, "decisions": {"call_1": {"decision": "allow"}},
    });
    run.rpc.accepted(method::RUN_APPROVE, params).await;
    let ended = run.rpc.until_end(&run.run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let steers: Vec<_> = ended
        .envelopes
        .iter()
        .filter_map(|e| match &e.event {
            Event::User(user) if user.subtype == UserEventSubtype::Alignment => {
                Some(user.steer_id.clone().unwrap())
            }
            _ => None,
        })
        .collect();
    assert_eq!(steers, ["st-1".to_owned(), made.id]);
    match ended.last() {
        Event::Result(result) => assert_eq!(result.subtype, ResultSubtype::Success),
        other => panic!("expected a result, got {other:?}"),
    }
    assert!(dir.path().join("new.txt").exists());

    let error = run.steer("too late", None).await.unwrap_err();
    assert_eq!(code_of(error), code::TERMINAL);
}

#[tokio::test]
async fn a_bad_steer_and_a_full_queue_are_refused_with_their_own_codes() {
    let (run, _dir) = suspended().await;
    for text in ["   ".to_owned(), "x".repeat(16 * 1024 + 1)] {
        let error = run.steer(&text, None).await.unwrap_err();
        assert_eq!(code_of(error), code::INVALID_STEER);
    }
    let error = run.steer("hi", Some("bad id!")).await.unwrap_err();
    assert_eq!(code_of(error), code::INVALID_STEER);
    let unknown = json!({"agent": run.agent, "run_id": "no-such-run", "text": "hi"});
    let error = run.rpc.call(method::RUN_STEER, unknown).await.unwrap_err();
    assert_eq!(code_of(error), code::NOT_FOUND);

    for n in 0..16 {
        run.steer("more", Some(&format!("f{n}"))).await.unwrap();
    }
    let error = run.steer("one too many", None).await.unwrap_err();
    assert_eq!(refusal(error).0, code::LIMIT_REACHED);
}

#[tokio::test]
async fn a_tenant_steers_only_its_own_runs() {
    let allow = || Allowlists::new().scripted_provider(true);
    let mut owner = Rpc::remote(tenant("acme", allow()));
    let agent = owner.open(config_in("acme")).await;
    let run_id = owner.start(&agent, "s1", "hi").await;
    assert!(matches!(
        owner.until_end(&run_id).await.last(),
        Event::Result(_)
    ));

    let other = Rpc::remote(tenant("beta", allow()));
    let steer = json!({"agent": agent, "run_id": run_id, "text": "hijack"});
    let error = other.call(method::RUN_STEER, steer).await.unwrap_err();
    assert_eq!(
        code_of(error),
        code::AGENT_NOT_FOUND,
        "another tenant's handle"
    );
    let own = other.open(config_in("beta")).await;
    let steer = json!({"agent": own, "run_id": run_id, "text": "hijack"});
    let error = other.call(method::RUN_STEER, steer).await.unwrap_err();
    assert_eq!(
        code_of(error),
        code::NOT_FOUND,
        "another tenant's run is unknown"
    );

    // The run exists for its owner, which is told it has finished.
    let steer = json!({"agent": agent, "run_id": run_id, "text": "mine"});
    let error = owner.call(method::RUN_STEER, steer).await.unwrap_err();
    assert_eq!(code_of(error), code::TERMINAL);
}

fn config_in(namespace: &str) -> Value {
    let mut config = scripted(vec![text_turn("Done.")], None, "");
    config["namespace"] = json!(namespace);
    config
}
