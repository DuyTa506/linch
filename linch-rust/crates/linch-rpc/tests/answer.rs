//! `run.answer`: an `AskUser` call suspends the run with an
//! `input_request`, and the answer continues it.

mod common;

use common::*;
use linch::events::kinds::SuspendStatus;
use linch::{Event, RunId};
use linch_rpc::protocol::method;
use linch_rpc::{ClientError, code};
use serde_json::{Value, json};

struct Asked {
    rpc: Rpc,
    agent: String,
    run_id: RunId,
}

/// A run with `AskUser` on that asked one question and suspended.
async fn asked() -> Asked {
    let mut rpc = Rpc::new();
    let question = json!({"questions": [{
        "id": "db", "header": "Database", "question": "Which database?",
        "options": [{"label": "Postgres", "description": "durable"},
                    {"label": "Memory", "description": "for tests"}]
    }]});
    let turns = vec![
        tool_turn("ask_1", "AskUser", question),
        text_turn("Postgres it is."),
    ];
    let agent = rpc
        .open(hosted(turns, "default", json!({"ask_user": true})))
        .await;
    let run_id = rpc.start(&agent, "s1", "Set up storage").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let Event::InputRequest(request) = &ended.envelopes[ended.envelopes.len() - 2].event else {
        panic!("no input_request: {:?}", ended.kinds())
    };
    assert_eq!(request.request_id, "ask_1");
    assert_eq!(request.questions[0].id, "db");
    match ended.last() {
        Event::Suspended(suspended) => {
            assert_eq!(suspended.status, SuspendStatus::AwaitingInput);
            assert_eq!(suspended.pending_request_ids, ["ask_1"]);
        }
        other => panic!("expected suspended, got {other:?}"),
    }
    Asked { rpc, agent, run_id }
}

impl Asked {
    async fn answer(&mut self, answers: Value) -> Ended {
        let params = json!({"agent": self.agent, "run_id": self.run_id, "answers": answers});
        let accepted = self.rpc.accepted(method::RUN_ANSWER, params).await;
        assert_eq!(accepted, self.run_id);
        self.rpc.until_end(&self.run_id).await
    }
}

#[tokio::test]
async fn an_answer_over_rpc_continues_the_run() {
    let mut run = asked().await;
    let ended = run
        .answer(json!({"ask_1": {"answers": {"db": "Postgres"}}}))
        .await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let results = tool_results(&ended);
    assert_eq!(
        results[0]["content"][0]["text"],
        r#"{"answers":{"db":"Postgres"}}"#
    );
    match ended.last() {
        Event::Result(result) => assert_eq!(result.final_text.as_deref(), Some("Postgres it is.")),
        other => panic!("expected a result, got {other:?}"),
    }
    // Answered already: the run is finished.
    let again = run
        .answer(json!({"ask_1": {"answers": {"db": "Memory"}}}))
        .await;
    assert_eq!(again.error.expect("refused").code, code::TERMINAL);
}

#[tokio::test]
async fn an_answer_that_does_not_fit_is_refused_and_the_run_still_waits() {
    let mut run = asked().await;
    for wrong in [
        json!({}),
        json!({"ask_1": {"answers": {"db": ["Postgres"]}}}),
        json!({"ask_9": {"answers": {"db": "Postgres"}}}),
    ] {
        let ended = run.answer(wrong.clone()).await;
        assert!(ended.envelopes.is_empty(), "{wrong}");
        let error = ended.error.expect("refused");
        assert_eq!(error.code, code::INVALID_ANSWER, "{wrong}");
        assert!(error.data.unwrap()["reason"].is_string());
    }
    let ended = run
        .answer(json!({"ask_1": {"accepted": false, "note": "later"}}))
        .await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let text = tool_results(&ended)[0]["content"][0]["text"].clone();
    assert!(
        text.as_str().unwrap().contains("User note: later"),
        "{text}"
    );
}

#[tokio::test]
async fn a_malformed_answer_is_invalid_params_and_approve_is_not_an_answer() {
    let mut run = asked().await;
    for answers in [
        json!({"ask_1": {"answers": {"db": 1}}}),
        json!({"ask_1": {"answers": {}, "extra": true}}),
        json!(["Postgres"]),
    ] {
        let params = json!({"agent": run.agent, "run_id": run.run_id, "answers": answers});
        let error = run.rpc.call(method::RUN_ANSWER, params).await.unwrap_err();
        assert!(
            matches!(&error, ClientError::Rpc(e) if e.code == code::INVALID_PARAMS),
            "{error:?}"
        );
    }
    let approve = json!({"agent": run.agent, "run_id": run.run_id,
                         "decisions": {"ask_1": {"decision": "allow"}}});
    run.rpc.accepted(method::RUN_APPROVE, approve).await;
    let ended = run.rpc.until_end(&run.run_id).await;
    assert_eq!(ended.error.expect("refused").code, code::NOT_SUSPENDED);
}
