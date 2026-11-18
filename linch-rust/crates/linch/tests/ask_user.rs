//! `ask_user` in an `AgentConfig`: off by default; on, the agent has
//! `AskUser`, a call suspends the run, and `answer` continues it.

use std::collections::HashMap;

use futures::StreamExt;
use linch::ask_user::{AnswerValue, AskUserAnswer};
use linch::events::kinds::SuspendStatus;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions, RunStream};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

fn config(extra: Value) -> AgentConfig {
    let ask = json!({"questions": [{
        "id": "db", "header": "Database", "question": "Which database?",
        "options": [{"label": "Postgres", "description": "durable"},
                    {"label": "Memory", "description": "for tests"}]
    }]});
    let turns = [
        TurnBuilder::new(MODEL)
            .tool_call("ask_1", "AskUser", &ask)
            .build(),
        TurnBuilder::new(MODEL).text("Postgres it is.").build(),
    ];
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

async fn events(mut stream: RunStream) -> Vec<Event> {
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        events.push(item.unwrap().event);
    }
    events
}

#[test]
fn ask_user_is_off_unless_asked_for() {
    let off = config(json!({}));
    assert!(!off.ask_user);
    assert!(
        serde_json::to_value(&off)
            .unwrap()
            .get("ask_user")
            .is_none()
    );
    let on = config(json!({"ask_user": true}));
    assert_eq!(serde_json::to_value(&on).unwrap()["ask_user"], json!(true));
}

#[tokio::test]
async fn an_agent_with_ask_user_waits_for_the_answer() {
    let off = config(json!({})).open().await.unwrap();
    let Event::Init(init) = &events(off.agent().run("go", RunOptions::new("s1"))).await[0] else {
        panic!("no init")
    };
    assert!(!init.tools.contains(&"AskUser".to_owned()));

    let opened = config(json!({"ask_user": true})).open().await.unwrap();
    let stream = opened.agent().run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let asked = events(stream).await;
    let Some(Event::Suspended(suspended)) = asked.last() else {
        panic!("not suspended: {asked:?}")
    };
    assert_eq!(suspended.status, SuspendStatus::AwaitingInput);
    let answer = AskUserAnswer::new([("db".to_owned(), AnswerValue::One("Postgres".into()))]);
    let answers = HashMap::from([("ask_1".to_owned(), answer)]);
    let done = events(
        opened
            .agent()
            .answer(run_id, answers, RunOptions::new("s1")),
    )
    .await;
    let Some(Event::Result(result)) = done.last() else {
        panic!("no result: {done:?}")
    };
    assert_eq!(result.final_text.as_deref(), Some("Postgres it is."));
}
