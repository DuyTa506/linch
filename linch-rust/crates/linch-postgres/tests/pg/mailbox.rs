//! The Postgres mailbox: the conformance suite, tenants kept apart, leases
//! that run out, claimers on separate pools, and agent runs that take a
//! message once across a restart and add no write when none waits.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::coordination::conformance::mailbox_suite;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, ContentBlock, JsonObject, LogEntry, Mailbox, MailboxMessage,
    Message, PermissionEngine, PermissionMode, PermissionPolicy, Role, RunOptions,
    ScriptedProvider, ScriptedTurn, SessionId, SessionStore, Tool, ToolContext, ToolOutput,
    ToolRegistry, ToolResult, ToolScope,
};
use linch_postgres::{PgMailbox, PgPool, PgRunStore, PgSessionStore};
use serde_json::json;
use sqlx::postgres::PgPoolOptions;
use tokio::task::JoinSet;

use crate::common::TestDb;

const MODEL: &str = "scripted-model";

#[tokio::test]
async fn the_mailbox_passes_the_conformance_suite() {
    let Some(db) = TestDb::migrated("mailbox").await else {
        return;
    };
    mailbox_suite(&PgMailbox::new(db.pool.clone(), "ns")).await;
    db.finish().await;
}

/// Two namespaces with the same session id are two inboxes.
#[tokio::test]
async fn namespaces_keep_their_inboxes_apart() {
    let Some(db) = TestDb::migrated("mailbox_tenants").await else {
        return;
    };
    let (a, b) = (
        PgMailbox::new(db.pool.clone(), "tenant-a"),
        PgMailbox::new(db.pool.clone(), "tenant-b"),
    );
    let for_a = MailboxMessage::new("lead", "alice", "for a");
    let mut for_b = for_a.clone();
    for_b.content = "for b".into();
    a.send(for_a.clone()).await.unwrap();
    b.send(for_b.clone()).await.unwrap();
    assert_eq!(
        a.drain("alice").await.unwrap(),
        std::slice::from_ref(&for_a)
    );
    assert_eq!(
        b.drain("alice").await.unwrap(),
        std::slice::from_ref(&for_b)
    );
    a.ack(&[for_a]).await.unwrap();
    b.release(std::slice::from_ref(&for_b)).await.unwrap();
    assert_eq!(
        b.drain("alice").await.unwrap(),
        [for_b],
        "b's message stays"
    );
    db.finish().await;
}

/// A lease that ran out lets another mailbox (a restarted process) drain
/// the message again; the first holder's ack is then stale and ignored.
#[tokio::test]
async fn a_lease_that_runs_out_is_drained_again() {
    let Some(db) = TestDb::migrated("mailbox_lease").await else {
        return;
    };
    let crashed = PgMailbox::new(db.pool.clone(), "ns").with_lease(Duration::ZERO);
    let message = MailboxMessage::new("lead", "alice", "hi");
    crashed.send(message.clone()).await.unwrap();
    assert_eq!(
        crashed.drain("alice").await.unwrap(),
        std::slice::from_ref(&message)
    );
    let restarted = PgMailbox::new(db.pool.clone(), "ns");
    assert_eq!(
        restarted.drain("alice").await.unwrap(),
        std::slice::from_ref(&message)
    );
    crashed.ack(std::slice::from_ref(&message)).await.unwrap();
    crashed
        .release(std::slice::from_ref(&message))
        .await
        .unwrap();
    assert!(
        restarted.drain("alice").await.unwrap().is_empty(),
        "the live lease is kept"
    );
    restarted.ack(&[message]).await.unwrap();
    let left: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_mailbox")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(left, 0);
    db.finish().await;
}

/// Mailboxes on two pools, as two processes would be, drain one inbox at
/// once: each message is delivered exactly once.
#[tokio::test]
async fn claimers_on_two_pools_deliver_each_message_once() {
    let Some(db) = TestDb::migrated("mailbox_claimers").await else {
        return;
    };
    let other = PgPoolOptions::new()
        .max_connections(4)
        .connect_with(db.options.clone())
        .await
        .unwrap();
    let sender = PgMailbox::new(db.pool.clone(), "ns");
    let mut sent = Vec::new();
    for n in 0..40 {
        let message = MailboxMessage::new("lead", "alice", n.to_string());
        sender.send(message.clone()).await.unwrap();
        sent.push(message.id);
    }
    let mut drains = JoinSet::new();
    for n in 0..8 {
        let pool = if n % 2 == 0 { &db.pool } else { &other };
        let mailbox = PgMailbox::new(pool.clone(), "ns");
        drains.spawn(async move {
            let mut taken = Vec::new();
            for _ in 0..5 {
                let drained = mailbox.drain("alice").await.unwrap();
                mailbox.ack(&drained).await.unwrap();
                taken.extend(drained.into_iter().map(|message| message.id));
            }
            taken
        });
    }
    let mut delivered: Vec<String> = drains.join_all().await.into_iter().flatten().collect();
    delivered.sort_unstable();
    sent.sort_unstable();
    assert_eq!(delivered, sent);
    other.close().await;
    db.finish().await;
}

/// A read tool that does nothing, so a run has a tool batch.
struct Noop(JsonObject);

#[async_trait]
impl Tool for Noop {
    fn name(&self) -> &str {
        "Noop"
    }
    fn description(&self) -> &str {
        "Do nothing."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.0
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new("done"))
    }
}

fn agent(
    pool: &PgPool,
    namespace: &str,
    turns: Vec<ScriptedTurn>,
    mailbox: Option<Arc<PgMailbox>>,
) -> (Agent, Arc<ScriptedProvider>) {
    let provider = Arc::new(ScriptedProvider::new(turns));
    let tools = Arc::new(ToolRegistry::new());
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let _ = tools.register(Arc::new(Noop(schema)));
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let mut builder = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider.clone())
        .tools(tools)
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(PgRunStore::new(pool.clone())))
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace(namespace);
    if let Some(mailbox) = mailbox {
        builder = builder.mailbox(mailbox);
    }
    (builder.build().unwrap(), provider)
}

async fn run(agent: &Agent, prompt: &str, session: &str) {
    let items: Vec<_> = agent.run(prompt, RunOptions::new(session)).collect().await;
    for item in items {
        item.expect("the run streams");
    }
}

fn text(text: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL).text(text).build()
}

fn noop_then_text() -> Vec<ScriptedTurn> {
    let call = TurnBuilder::new(MODEL).tool_call("t1", "Noop", &json!({}));
    vec![call.build(), text("ok")]
}

/// A message sent before the server stops waits in the table, reaches the
/// first run after the restart, and no later run sees it again.
#[tokio::test]
async fn a_message_survives_a_restart_and_is_delivered_once() {
    let Some(db) = TestDb::migrated("mailbox_restart").await else {
        return;
    };
    let before = PgPoolOptions::new()
        .max_connections(2)
        .connect_with(db.options.clone())
        .await
        .unwrap();
    let message = MailboxMessage::new("lead", "alice", "plan?");
    let sender = PgMailbox::new(before.clone(), "ns");
    sender.send(message.clone()).await.unwrap();
    drop(sender);
    before.close().await;

    let mailbox = Arc::new(PgMailbox::new(db.pool.clone(), "ns"));
    let (alice, provider) = agent(&db.pool, "ns", vec![text("a"), text("b")], Some(mailbox));
    run(&alice, "go", "alice").await;
    run(&alice, "again", "alice").await;
    let requests = provider.requests();
    let prompt = Message::new(
        Role::User,
        vec![
            ContentBlock::text("go"),
            ContentBlock::text(message.render()),
        ],
    );
    assert_eq!(
        requests[0].messages,
        std::slice::from_ref(&prompt),
        "the first run sees it"
    );
    let last = requests[1].messages.last().unwrap();
    assert_eq!(last, &Message::user_text("again"), "the second does not");
    let sessions = PgSessionStore::new(db.pool.clone());
    let logged = sessions.load("ns", &SessionId::new("alice")).await.unwrap();
    let messages: Vec<_> = (logged.into_iter())
        .filter_map(|entry| match entry {
            LogEntry::Message(entry) => Some(Message::clone(&entry.message)),
            _ => None,
        })
        .collect();
    assert_eq!(messages.len(), 4, "two prompts, two answers");
    assert_eq!(messages[0], prompt, "logged once");
    let left: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_mailbox")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(left, 0, "acked once the prompt is logged");
    db.finish().await;
}

/// Count every row written to the store tables, by table and operation.
async fn count_writes(pool: &PgPool) {
    let setup = [
        "CREATE TABLE test_writes (tbl TEXT NOT NULL, op TEXT NOT NULL)",
        "CREATE FUNCTION test_count_write() RETURNS trigger LANGUAGE plpgsql AS $$ \
         BEGIN INSERT INTO test_writes VALUES (TG_TABLE_NAME, TG_OP); RETURN NULL; END $$",
    ];
    for sql in setup {
        sqlx::query(sql).execute(pool).await.unwrap();
    }
    for table in [
        "linch_runs",
        "linch_run_events",
        "linch_sessions",
        "linch_session_entries",
        "linch_mailbox",
    ] {
        let sql = format!(
            "CREATE TRIGGER test_count AFTER INSERT OR UPDATE OR DELETE ON {table} \
             FOR EACH ROW EXECUTE FUNCTION test_count_write()"
        );
        sqlx::query(sqlx::AssertSqlSafe(sql))
            .execute(pool)
            .await
            .unwrap();
    }
}

async fn writes(pool: &PgPool) -> Vec<(String, String, i64)> {
    let counted = sqlx::query_as(
        "SELECT tbl, op, count(*) FROM test_writes GROUP BY tbl, op ORDER BY tbl, op",
    )
    .fetch_all(pool)
    .await
    .unwrap();
    sqlx::query("DELETE FROM test_writes")
        .execute(pool)
        .await
        .unwrap();
    counted
}

/// A run with a tool batch writes the same rows with the mailbox as
/// without it when no message waits.
#[tokio::test]
async fn a_run_without_mail_writes_what_a_run_without_a_mailbox_writes() {
    let Some(db) = TestDb::migrated("mailbox_writes").await else {
        return;
    };
    count_writes(&db.pool).await;
    let (plain, _) = agent(&db.pool, "plain", noop_then_text(), None);
    run(&plain, "go", "s1").await;
    let without = writes(&db.pool).await;
    let mailbox = Arc::new(PgMailbox::new(db.pool.clone(), "mail"));
    let (mailed, _) = agent(&db.pool, "mail", noop_then_text(), Some(mailbox));
    run(&mailed, "go", "s1").await;
    let with = writes(&db.pool).await;
    assert!(!without.is_empty(), "the run wrote");
    assert_eq!(with, without);
    db.finish().await;
}
