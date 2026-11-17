//! `filesystem.memory`: a plain agent (no deep agent preset) shows the model
//! `/memories/MEMORY.md`. Off, it stays off the wire and out of the
//! contract. The Postgres test is skipped without `LINCH_TEST_PG_DSN`.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::StreamExt;
use linch::hooks::{BeforeProviderCall, BeforeProviderCallRequest, BeforeProviderCallResponse};
use linch::provider::scripted::TurnBuilder;
use linch::{
    AgentConfig, CallbackError, Hook, Hooks, PgPools, Point, RunOptions, SessionId, ToolRegistry,
};
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

const MODEL: &str = "scripted-model";
const INDEX: &str = "- [Style](style.md): terse";
/// How the hook shows the index in a request, in the JSON the recorder keeps
/// (the model's own `write_file` call holds `INDEX` too).
const SHOWN: &str = "/memories/MEMORY.md:\\n- [Style]";

/// Records every request's text, as the model sees it.
#[derive(Default)]
struct Requests(Mutex<Vec<String>>);

#[async_trait]
impl Hook<BeforeProviderCall> for Requests {
    fn policy_id(&self) -> &str {
        "requests"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(
        &self,
        request: &BeforeProviderCallRequest,
    ) -> Result<<BeforeProviderCall as Point>::Response, CallbackError> {
        let text = serde_json::to_string(&request.request.messages).unwrap();
        self.0.lock().unwrap().push(text);
        Ok(BeforeProviderCallResponse::Continue)
    }
}

fn call(id: &str, tool: &str, input: Value) -> Value {
    json!(TurnBuilder::new(MODEL).tool_call(id, tool, &input).build())
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

fn config(turns: Vec<Value>, filesystem: Value, stores: Value) -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "mem-tenant",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": stores,
        "permissions": {"mode": "accept_edits", "project_root": "/"},
        "filesystem": filesystem,
    }))
    .unwrap()
}

/// Open `config` with a request recorder.
async fn open(config: AgentConfig) -> (linch::OpenedAgent, Arc<Requests>) {
    let requests = Arc::new(Requests::default());
    let hooks = Hooks::default();
    let _ = hooks
        .before_provider_call
        .register(requests.clone())
        .unwrap();
    let opened = config
        .open_with(
            Arc::new(ToolRegistry::new()),
            Arc::new(hooks),
            &PgPools::new(),
        )
        .await
        .unwrap();
    (opened, requests)
}

/// Run one prompt to its end; the requests it made, from the recorder.
async fn run(opened: &linch::OpenedAgent, requests: &Requests, session: &str) -> Vec<String> {
    let before = requests.0.lock().unwrap().len();
    let mut stream = opened.agent().run("go", RunOptions::new(session));
    while let Some(item) = stream.next().await {
        item.unwrap();
    }
    requests.0.lock().unwrap()[before..].to_vec()
}

fn write_index() -> Value {
    call(
        "call_1",
        "write_file",
        json!({"path": "/memories/MEMORY.md", "content": INDEX}),
    )
}

/// Off, nothing is written to the wire and the contract is the one an agent
/// without the field always had; on, the hook is part of it.
#[tokio::test]
async fn memory_is_off_the_wire_and_out_of_the_contract_unless_on() {
    let off = config(vec![], json!({}), json!({"type": "memory"}));
    let wire = serde_json::to_value(&off).unwrap();
    assert_eq!(wire["filesystem"], json!({}));
    let explicit = config(vec![], json!({"memory": false}), json!({"type": "memory"}));
    assert_eq!(
        serde_json::to_value(&explicit).unwrap()["filesystem"],
        json!({})
    );
    let on = config(vec![], json!({"memory": true}), json!({"type": "memory"}));
    assert_eq!(
        serde_json::to_value(&on).unwrap()["filesystem"],
        json!({"memory": true})
    );

    let digest =
        |config: AgentConfig| async move { config.open().await.unwrap().agent().contract() };
    let bare = digest(off).await;
    assert_eq!(bare, digest(explicit).await);
    assert_ne!(
        bare,
        digest(on).await,
        "the hook is a policy of the contract"
    );
}

/// A plain agent on memory stores: the index the model writes shows in the
/// next request of the same session, and it does not survive
/// `close_session`.
#[tokio::test]
async fn with_memory_stores_the_file_lives_and_dies_with_its_session() {
    let turns = vec![
        write_index(),
        text("Noted."),
        text("Hi."),
        text("Hi again."),
    ];
    let (opened, requests) = open(config(
        turns,
        json!({"memory": true}),
        json!({"type": "memory"}),
    ))
    .await;
    let first = run(&opened, &requests, "s1").await;
    assert_eq!(first.len(), 2);
    assert!(
        first[0].contains("Memories live under /memories/"),
        "told the file exists"
    );
    assert!(!first[0].contains(SHOWN), "nothing written yet");
    assert!(
        first[1].contains(SHOWN),
        "the next request shows what the model wrote"
    );

    let other = run(&opened, &requests, "s2").await;
    assert!(
        !other[0].contains(SHOWN),
        "memory stores keep each session's files apart"
    );

    opened.agent().close_session(&SessionId::new("s1"));
    let after = run(&opened, &requests, "s1").await;
    assert!(!after[0].contains(SHOWN), "close_session dropped the file");
    opened.close().await;
}

/// Without `memory` a plain filesystem agent is told nothing of it.
#[tokio::test]
async fn without_memory_the_model_is_not_told() {
    let turns = vec![write_index(), text("Noted.")];
    let (opened, requests) = open(config(turns, json!({}), json!({"type": "memory"}))).await;
    let seen = run(&opened, &requests, "s1").await;
    assert!(seen.iter().all(|r| !r.contains("Memories live under")));
    assert!(seen.iter().all(|r| !r.contains(SHOWN)));
}

struct TestDb {
    admin: PgPool,
    schema: String,
    dsn: String,
}

impl TestDb {
    async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
            eprintln!("skipping {name}: LINCH_TEST_PG_DSN is not set");
            return None;
        };
        let schema = format!("memfile_{name}");
        let admin = PgPoolOptions::new()
            .max_connections(1)
            .connect(&dsn)
            .await
            .expect("connect to the test database");
        for sql in [
            format!("DROP SCHEMA IF EXISTS {schema} CASCADE"),
            format!("CREATE SCHEMA {schema}"),
        ] {
            let query = sqlx::query(AssertSqlSafe(sql));
            query.execute(&admin).await.expect("reset the test schema");
        }
        let separator = if dsn.contains('?') { '&' } else { '?' };
        let dsn = format!("{dsn}{separator}options=-c%20search_path%3D{schema}");
        Some(Self { admin, schema, dsn })
    }

    async fn drop(self) {
        let sql = format!("DROP SCHEMA IF EXISTS {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("drop the schema");
        self.admin.close().await;
    }
}

/// With Postgres stores, session 1 writes the index and session 2 of the
/// same agent sees it in its first request; so does a new agent, as another
/// process would be, and it survives `close_session`.
#[tokio::test]
async fn with_postgres_stores_a_later_session_sees_what_an_earlier_one_wrote() {
    let Some(db) = TestDb::empty("plain").await else {
        return;
    };
    let stores = json!({"type": "postgres", "dsn": db.dsn});
    let filesystem = json!({"memory": true});
    let turns = vec![
        write_index(),
        text("Noted."),
        text("Hello."),
        text("Again."),
    ];
    let (opened, requests) = open(config(turns, filesystem.clone(), stores.clone())).await;
    let first = run(&opened, &requests, "s1").await;
    assert!(!first[0].contains(SHOWN));
    opened.agent().close_session(&SessionId::new("s1"));
    let second = run(&opened, &requests, "s2").await;
    assert!(
        second[0].contains(SHOWN),
        "session 2 sees the index: {second:?}"
    );
    assert!(second[0].contains("/memories/MEMORY.md:"));
    opened.close().await;

    let (reopened, requests) = open(config(vec![text("Hi.")], filesystem, stores)).await;
    let third = run(&reopened, &requests, "s3").await;
    assert!(third[0].contains(SHOWN), "another process sees it too");
    reopened.close().await;
    db.drop().await;
}
