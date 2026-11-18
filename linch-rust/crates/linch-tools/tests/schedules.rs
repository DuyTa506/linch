//! `CreateSchedule`, `ListSchedules` and `CancelSchedule` over a fake
//! backend: names, scopes, schemas, what the model may choose, and the
//! texts it sees.

mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use common::input;
use linch_core::{
    Principal, RunId, SessionId, Tool, ToolContext, ToolOutputError, ToolResult, ToolScope,
};
use linch_tools::{
    NewSchedule, SCHEDULE_TOOL_NAMES, ScheduleBackend, ScheduleEntry, ScheduleTiming,
    schedule_tools,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

#[derive(Default)]
struct Fake {
    created: Mutex<Vec<NewSchedule>>,
    listed: Mutex<Vec<SessionId>>,
    cancelled: Mutex<Vec<(SessionId, String)>>,
}

fn entry(id: &str, prompt: &str, paused: bool) -> ScheduleEntry {
    ScheduleEntry {
        id: id.into(),
        prompt: prompt.into(),
        paused,
        next_fire: "2026-11-17T07:00:00.000Z".into(),
    }
}

#[async_trait]
impl ScheduleBackend for Fake {
    async fn create(&self, schedule: NewSchedule) -> Result<ScheduleEntry, ToolOutputError> {
        let made = entry("s-1", &schedule.prompt, false);
        self.created.lock().unwrap().push(schedule);
        Ok(made)
    }

    async fn list(&self, session: &SessionId) -> Result<Vec<ScheduleEntry>, ToolOutputError> {
        self.listed.lock().unwrap().push(session.clone());
        Ok(vec![entry("a", "digest", false), entry("b", "beat", true)])
    }

    async fn cancel(&self, session: &SessionId, id: &str) -> Result<bool, ToolOutputError> {
        let call = (session.clone(), id.to_owned());
        self.cancelled.lock().unwrap().push(call);
        Ok(id == "a")
    }
}

fn tools() -> (Arc<Fake>, Vec<Arc<dyn Tool>>) {
    let fake = Arc::new(Fake::default());
    let tools = schedule_tools(fake.clone());
    (fake, tools)
}

fn find<'a>(tools: &'a [Arc<dyn Tool>], name: &str) -> &'a dyn Tool {
    tools
        .iter()
        .find(|tool| tool.name() == name)
        .unwrap()
        .as_ref()
}

fn context(principal: Option<Principal>) -> ToolContext {
    ToolContext::new(
        SessionId::from("chat"),
        RunId::from("run-1"),
        "toolu_1",
        principal,
        CancellationToken::new(),
        None,
    )
}

async fn call(tool: &dyn Tool, value: Value, principal: Option<Principal>) -> ToolResult {
    let input = tool.validate(input(value))?;
    tool.execute(input, context(principal)).await
}

fn text(result: ToolResult) -> String {
    let output = result.expect("the call succeeds");
    output.value.as_str().expect("text output").to_owned()
}

#[test]
fn names_scopes_and_schemas() {
    let (_, tools) = tools();
    let names: Vec<&str> = tools.iter().map(|tool| tool.name()).collect();
    assert_eq!(names, SCHEDULE_TOOL_NAMES);
    assert_eq!(
        names,
        ["CreateSchedule", "ListSchedules", "CancelSchedule"],
        "Python's names"
    );
    let scopes: Vec<ToolScope> = tools.iter().map(|tool| tool.scope()).collect();
    // Creating schedules future runs: exec, so `accept_edits` still asks.
    assert_eq!(scopes, [ToolScope::Exec, ToolScope::Read, ToolScope::Write]);
    let create = find(&tools, "CreateSchedule").input_schema();
    let properties: Vec<&String> = create["properties"].as_object().unwrap().keys().collect();
    // No id, session, queue, principal or config: the host picks those.
    let mut properties: Vec<&str> = properties.iter().map(|key| key.as_str()).collect();
    properties.sort_unstable();
    assert_eq!(properties, ["cron", "interval_s", "payload", "timezone"]);
    assert_eq!(create["required"], json!(["payload"]));
    assert_eq!(create["additionalProperties"], json!(false));
    let cancel = find(&tools, "CancelSchedule").input_schema();
    assert_eq!(cancel["required"], json!(["id"]));
}

#[tokio::test]
async fn create_takes_the_calling_session_and_principal() {
    let (fake, tools) = tools();
    let create = find(&tools, "CreateSchedule");
    let principal = Principal::new("acme", "ada", ["intern"]).unwrap();
    let said = text(
        call(
            create,
            json!({"payload": "Check the queue.", "interval_s": 90}),
            Some(principal.clone()),
        )
        .await,
    );
    assert_eq!(said, "Scheduled s-1 at 2026-11-17T07:00:00.000Z");
    let said = text(
        call(
            create,
            json!({"payload": "Digest.", "cron": "0 7 * * *", "timezone": "Europe/Berlin"}),
            None,
        )
        .await,
    );
    assert!(said.starts_with("Scheduled s-1"), "{said}");
    let created = fake.created.lock().unwrap();
    assert_eq!(created.len(), 2);
    assert_eq!(created[0].prompt, "Check the queue.");
    assert_eq!(
        created[0].timing,
        ScheduleTiming::Interval(Duration::from_secs(90))
    );
    assert_eq!(created[0].session_id, SessionId::from("chat"));
    assert_eq!(created[0].run_id, RunId::from("run-1"));
    assert_eq!(created[0].call_key, "run-1:toolu_1");
    assert_eq!(created[0].principal, Some(principal));
    assert_eq!(
        created[1].timing,
        ScheduleTiming::Cron {
            pattern: "0 7 * * *".into(),
            timezone: Some("Europe/Berlin".into())
        }
    );
    assert_eq!(created[1].principal, None);
}

#[tokio::test]
async fn create_refuses_a_bad_timing_before_the_backend() {
    let (fake, tools) = tools();
    let create = find(&tools, "CreateSchedule");
    for bad in [
        json!({"payload": "p"}),
        json!({"payload": "p", "cron": "0 * * * *", "interval_s": 60}),
        json!({"payload": "p", "interval_s": 0}),
        json!({"payload": "p", "interval_s": -5}),
        json!({"payload": "p", "interval_s": 60, "timezone": "UTC"}),
        json!({"payload": "", "interval_s": 60}),
    ] {
        let error = call(create, bad.clone(), None).await.expect_err("refused");
        assert_eq!(error.code.as_deref(), Some("invalid_input"), "{bad}");
    }
    assert!(fake.created.lock().unwrap().is_empty());
}

#[tokio::test]
async fn list_and_cancel_say_what_python_says_for_the_calling_session() {
    let (fake, tools) = tools();
    let listed = text(call(find(&tools, "ListSchedules"), json!({}), None).await);
    assert_eq!(
        listed,
        "a: digest enabled=true next_fire=2026-11-17T07:00:00.000Z\n\
         b: beat enabled=false next_fire=2026-11-17T07:00:00.000Z"
    );
    let cancel = find(&tools, "CancelSchedule");
    assert_eq!(
        text(call(cancel, json!({"id": "a"}), None).await),
        "Cancelled a"
    );
    let error = call(cancel, json!({"id": "zz"}), None).await.unwrap_err();
    assert_eq!(error.message, "No schedule zz");
    // Both see only the calling run's session, as Python's tools do.
    assert_eq!(*fake.listed.lock().unwrap(), [SessionId::from("chat")]);
    let chat = |id: &str| (SessionId::from("chat"), id.to_owned());
    assert_eq!(*fake.cancelled.lock().unwrap(), [chat("a"), chat("zz")]);
}

#[tokio::test]
async fn an_empty_list_says_so() {
    struct Empty;
    #[async_trait]
    impl ScheduleBackend for Empty {
        async fn create(&self, _: NewSchedule) -> Result<ScheduleEntry, ToolOutputError> {
            unreachable!()
        }
        async fn list(&self, _: &SessionId) -> Result<Vec<ScheduleEntry>, ToolOutputError> {
            Ok(Vec::new())
        }
        async fn cancel(&self, _: &SessionId, _: &str) -> Result<bool, ToolOutputError> {
            unreachable!()
        }
    }
    let tools = schedule_tools(Arc::new(Empty));
    let listed = text(call(find(&tools, "ListSchedules"), json!({}), None).await);
    assert_eq!(listed, "No schedules registered.");
}
