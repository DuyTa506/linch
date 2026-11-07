//! The mailbox: `send_message` queues a peer message for a session, and a
//! run of that session sees it in its next request, logged in its prompt
//! or in its tool results. A run with a mailbox and no message writes
//! exactly what a run without one writes. A run acks what it drained once
//! the message that carries it is logged, and releases it when nothing
//! was logged.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, ContentBlock, InMemoryMailbox, JsonObject, Mailbox, MailboxError,
    MailboxMessage, Message, PermissionMode, Provider, Role, RunError, RunOptions,
    ScriptedProvider, ScriptedTurn, StoreError, Tool, ToolContext, ToolOutput, ToolRegistry,
    ToolResult, ToolScope,
};
use serde_json::json;

/// An in-memory mailbox that keeps what was sent, acked and released.
#[derive(Default)]
struct Recording {
    inner: InMemoryMailbox,
    sent: Mutex<Vec<MailboxMessage>>,
    acked: Mutex<Vec<MailboxMessage>>,
    released: Mutex<Vec<MailboxMessage>>,
}

impl Recording {
    fn sent(&self) -> Vec<MailboxMessage> {
        self.sent.lock().unwrap().clone()
    }
    fn acked(&self) -> Vec<MailboxMessage> {
        self.acked.lock().unwrap().clone()
    }
    fn released(&self) -> Vec<MailboxMessage> {
        self.released.lock().unwrap().clone()
    }
}

#[async_trait]
impl Mailbox for Recording {
    async fn send(&self, message: MailboxMessage) -> Result<(), MailboxError> {
        self.sent.lock().unwrap().push(message.clone());
        self.inner.send(message).await
    }
    async fn drain(&self, recipient: &str) -> Result<Vec<MailboxMessage>, MailboxError> {
        self.inner.drain(recipient).await
    }
    async fn ack(&self, messages: &[MailboxMessage]) -> Result<(), MailboxError> {
        self.acked.lock().unwrap().extend_from_slice(messages);
        self.inner.ack(messages).await
    }
    async fn release(&self, messages: &[MailboxMessage]) -> Result<(), MailboxError> {
        self.released.lock().unwrap().extend_from_slice(messages);
        self.inner.release(messages).await
    }
}

/// A tool that drops a message from `bob` into `alice`'s inbox.
struct Poke(Arc<Recording>, JsonObject);

#[async_trait]
impl Tool for Poke {
    fn name(&self) -> &str {
        "Poke"
    }
    fn description(&self) -> &str {
        "Poke alice."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.1
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let message = MailboxMessage::new("bob", "alice", "status?");
        self.0.send(message).await.unwrap();
        Ok(ToolOutput::new("poked"))
    }
}

fn mailbox_agent(
    turns: Vec<ScriptedTurn>,
    tools: Arc<ToolRegistry>,
    stores: &Arc<Stores>,
    mailbox: Option<&Arc<Recording>>,
) -> (Agent, Arc<ScriptedProvider>) {
    let provider = Arc::new(ScriptedProvider::new(turns));
    let mut builder = AgentBuilder::new(spec())
        .provider(provider.clone() as Arc<dyn Provider>)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS);
    if let Some(mailbox) = mailbox {
        builder = builder.mailbox(mailbox.clone() as Arc<dyn Mailbox>);
    }
    (builder.build().unwrap(), provider)
}

fn peer(from: &str, content: &str) -> ContentBlock {
    ContentBlock::text(format!(
        "<peer-message><from>{from}</from><type>message</type><content>{content}</content>\
         </peer-message>"
    ))
}

#[tokio::test]
async fn a_message_sent_to_a_session_reaches_its_next_run_in_the_prompt() {
    let stores = Arc::new(Stores::default());
    let mailbox = Arc::new(Recording::default());
    let send = TurnBuilder::new(MODEL)
        .tool_call(
            "m1",
            "send_message",
            &json!({"to": " alice ", "content": "plan?"}),
        )
        .build();
    let lead_turns = vec![send, text_turn("sent")];
    let lead_tools = Arc::new(ToolRegistry::new());
    let (lead, _) = mailbox_agent(lead_turns, lead_tools.clone(), &stores, Some(&mailbox));
    finish(lead.run("tell alice", RunOptions::new("lead"))).await;
    let lead_log = messages(&*stores, "lead").await;
    assert_eq!(
        result_texts(&lead_log[2]),
        ["Message delivered to 'alice'."]
    );
    let names: Vec<String> = (lead_tools.snapshot().definitions().into_iter())
        .map(|tool| tool.name)
        .collect();
    assert_eq!(names, ["send_message"]);

    let alice_tools = Arc::new(ToolRegistry::new());
    let (alice, provider) =
        mailbox_agent(vec![text_turn("ok")], alice_tools, &stores, Some(&mailbox));
    finish(alice.run("go", RunOptions::new("alice"))).await;
    let prompt = Message::new(
        Role::User,
        vec![ContentBlock::text("go"), peer("lead", "plan?")],
    );
    assert_eq!(messages(&*stores, "alice").await[0], prompt, "logged");
    assert_eq!(provider.requests()[0].messages, [prompt], "and sent");
    assert!(mailbox.drain("alice").await.unwrap().is_empty());
    let alices: Vec<_> = (mailbox.sent().into_iter())
        .filter(|message| message.recipient == "alice")
        .collect();
    assert_eq!(mailbox.acked(), alices, "acked once the prompt is logged");
    assert!(mailbox.released().is_empty());
}

#[tokio::test]
async fn a_message_that_arrives_during_a_tool_batch_joins_its_results() {
    let stores = Arc::new(Stores::default());
    let mailbox = Arc::new(Recording::default());
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let tools = Arc::new(ToolRegistry::new());
    let _ = tools.register(Arc::new(Poke(mailbox.clone(), schema)));
    let turns = vec![tool_turn(0, 1, "Poke"), text_turn("ok")];
    let (alice, provider) = mailbox_agent(turns, tools, &stores, Some(&mailbox));
    finish(alice.run("go", RunOptions::new("alice"))).await;

    let log = messages(&*stores, "alice").await;
    assert_eq!(log.len(), 4, "prompt, call, results, answer");
    assert_eq!(
        log[0],
        Message::user_text("go"),
        "nothing waited at the start"
    );
    assert_eq!(result_texts(&log[2]), ["poked"]);
    assert_eq!(log[2].content.len(), 2);
    assert_eq!(log[2].content[1], peer("bob", "status?"));
    let second = &provider.requests()[1];
    assert_eq!(second.messages.last(), Some(&log[2]));
    assert_eq!(
        mailbox.acked(),
        mailbox.sent(),
        "acked once the results are logged"
    );
    assert!(mailbox.released().is_empty());
}

#[tokio::test]
async fn a_mailbox_adds_no_writes_when_no_message_waits() {
    let mut writes = Vec::new();
    for with in [false, true] {
        let stores = Arc::new(Stores::default());
        let runs = Runs::default();
        let tools = registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]);
        let mailbox = Arc::new(Recording::default());
        let mailbox = with.then_some(&mailbox);
        let (agent, _) = mailbox_agent(script(2, 2, "Echo"), tools, &stores, mailbox);
        finish(agent.run("go", RunOptions::new("s1"))).await;
        writes.push(stores.log.lock().unwrap().clone());
    }
    assert_eq!(writes[0], writes[1]);
}

#[tokio::test]
async fn a_run_the_store_refuses_gives_its_drained_messages_back() {
    let stores = Arc::new(Stores::default());
    let mailbox = Arc::new(Recording::default());
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Write", ToolScope::Write, &runs)]);
    let provider = Arc::new(ScriptedProvider::new(vec![tool_turn(0, 1, "Write")]));
    let agent = AgentBuilder::new(spec())
        .provider(provider as Arc<dyn Provider>)
        .tools(tools)
        .permissions(policy(PermissionMode::Default))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .mailbox(mailbox.clone() as Arc<dyn Mailbox>)
        .build()
        .unwrap();
    // The first run waits for approval, so the session stays busy.
    finish(agent.run("go", RunOptions::new("alice"))).await;
    let message = MailboxMessage::new("bob", "alice", "status?");
    mailbox.send(message.clone()).await.unwrap();
    let (_, error) = drain(agent.run("again", RunOptions::new("alice"))).await;
    let busy = matches!(error, Some(RunError::Store(StoreError::SessionBusy { .. })));
    assert!(busy, "{error:?}");
    let once = std::slice::from_ref(&message);
    assert_eq!(mailbox.released(), once, "released, not sent again");
    assert_eq!(mailbox.sent(), once);
    assert!(mailbox.acked().is_empty());
    assert_eq!(mailbox.drain("alice").await.unwrap(), [message]);
}
