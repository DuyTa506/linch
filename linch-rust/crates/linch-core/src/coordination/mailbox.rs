//! Peer messages, the [`Mailbox`] a host plugs in, and the in-process
//! default.

use std::collections::HashMap;
use std::sync::{Mutex, PoisonError};

use async_trait::async_trait;
use thiserror::Error;

/// One peer-to-peer message.
///
/// `sender` and `recipient` are addresses: the session id of the agent
/// that sent it and of the one it is for. `kind` is a neutral category the
/// host interprets (Python's `type`). A request carries a `request_id`; its
/// response echoes that value in `in_reply_to` (see
/// [`Correlator`](super::Correlator)).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MailboxMessage {
    /// Address of the agent that sent it.
    pub sender: String,
    /// Address of the inbox it goes to.
    pub recipient: String,
    /// The message body.
    pub content: String,
    /// Neutral category, `message` by default.
    pub kind: String,
    /// Set on a request that expects a reply.
    pub request_id: Option<String>,
    /// The request this message answers.
    pub in_reply_to: Option<String>,
    /// Stable message id.
    pub id: String,
}

impl MailboxMessage {
    /// A message of kind `message` with a fresh id and no correlation.
    pub fn new(
        sender: impl Into<String>,
        recipient: impl Into<String>,
        content: impl Into<String>,
    ) -> Self {
        Self {
            sender: sender.into(),
            recipient: recipient.into(),
            content: content.into(),
            kind: "message".into(),
            request_id: None,
            in_reply_to: None,
            id: uuid::Uuid::new_v4().simple().to_string(),
        }
    }

    /// The message as the recipient's model sees it: a `<peer-message>`
    /// element, every value XML-escaped.
    pub fn render(&self) -> String {
        let mut text = format!(
            "<peer-message><from>{}</from><type>{}</type>",
            escape(&self.sender),
            escape(&self.kind)
        );
        if let Some(id) = &self.request_id {
            text.push_str(&format!("<request-id>{}</request-id>", escape(id)));
        }
        if let Some(id) = &self.in_reply_to {
            text.push_str(&format!("<in-reply-to>{}</in-reply-to>", escape(id)));
        }
        text.push_str(&format!(
            "<content>{}</content></peer-message>",
            escape(&self.content)
        ));
        text
    }
}

/// `&`, `<` and `>` as XML entities, as Python's `xml.sax.saxutils.escape`.
fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// A mailbox could not send or drain.
#[derive(Debug, Error)]
#[error("mailbox backend error")]
pub struct MailboxError(#[source] pub Box<dyn std::error::Error + Send + Sync>);

/// Where peer messages wait for their recipient.
#[async_trait]
pub trait Mailbox: Send + Sync {
    /// Queue `message` in its recipient's inbox. Returns once the message
    /// is accepted; concurrent sends to one inbox never drop a message.
    async fn send(&self, message: MailboxMessage) -> Result<(), MailboxError>;

    /// Remove and return every message waiting for `recipient`, oldest
    /// first. A message is returned by at most one drain.
    async fn drain(&self, recipient: &str) -> Result<Vec<MailboxMessage>, MailboxError>;
}

/// The in-process mailbox: one FIFO inbox per recipient.
///
/// Messages live as long as the mailbox value, which agents, sessions and
/// workers may share; nothing survives the process.
#[derive(Debug, Default)]
pub struct InMemoryMailbox {
    inboxes: Mutex<HashMap<String, Vec<MailboxMessage>>>,
}

impl InMemoryMailbox {
    /// An empty mailbox.
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait]
impl Mailbox for InMemoryMailbox {
    async fn send(&self, message: MailboxMessage) -> Result<(), MailboxError> {
        let mut inboxes = self.inboxes.lock().unwrap_or_else(PoisonError::into_inner);
        let inbox = inboxes.entry(message.recipient.clone()).or_default();
        inbox.push(message);
        Ok(())
    }

    async fn drain(&self, recipient: &str) -> Result<Vec<MailboxMessage>, MailboxError> {
        let mut inboxes = self.inboxes.lock().unwrap_or_else(PoisonError::into_inner);
        // Removing the bucket keeps the map from growing by one stale entry
        // per address ever written to.
        Ok(inboxes.remove(recipient).unwrap_or_default())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_drain_takes_one_inbox_in_order_and_once() {
        let mailbox = InMemoryMailbox::new();
        for content in ["one", "two"] {
            let message = MailboxMessage::new("lead", "alice", content);
            mailbox.send(message).await.unwrap();
        }
        let other = MailboxMessage::new("lead", "bob", "three");
        mailbox.send(other).await.unwrap();
        let drained = mailbox.drain("alice").await.unwrap();
        let contents: Vec<&str> = drained.iter().map(|m| m.content.as_str()).collect();
        assert_eq!(contents, ["one", "two"]);
        assert!(mailbox.drain("alice").await.unwrap().is_empty());
        assert_eq!(mailbox.drain("bob").await.unwrap().len(), 1);
        assert!(mailbox.inboxes.lock().unwrap().is_empty());
    }

    #[test]
    fn a_message_renders_as_an_escaped_peer_message() {
        let mut message = MailboxMessage::new("a<b", "c", "x & y > z");
        message.kind = "plan".into();
        message.request_id = Some("r1".into());
        message.in_reply_to = Some("r0".into());
        assert_eq!(
            message.render(),
            "<peer-message><from>a&lt;b</from><type>plan</type>\
             <request-id>r1</request-id><in-reply-to>r0</in-reply-to>\
             <content>x &amp; y &gt; z</content></peer-message>"
        );
        let plain = MailboxMessage::new("a", "b", "hi");
        assert_eq!(
            plain.render(),
            "<peer-message><from>a</from><type>message</type>\
             <content>hi</content></peer-message>"
        );
        assert_eq!(plain.id.len(), 32);
        assert_ne!(plain.id, MailboxMessage::new("a", "b", "hi").id);
    }
}
