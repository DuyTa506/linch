//! The `send_message` tool: put a message in a peer's inbox.

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::json;

use super::{Mailbox, MailboxMessage};
use crate::json::{JsonObject, JsonValue};
use crate::tool::{Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope};

/// Name of the tool that sends a peer message.
pub const SEND_MESSAGE_TOOL: &str = "send_message";

const DESCRIPTION: &str = "Send a message to a peer agent's mailbox by address.

The recipient receives it at the top of its next turn. Use 'request_id'
to mark a message that expects a reply, and 'in_reply_to' to answer one.
Message meaning is up to you \u{2014} this only delivers the content.";

/// Sends a message from the calling session to the address `to`.
///
/// Exec scope, and never parallel: it changes another agent's inbox.
pub(crate) struct SendMessageTool {
    mailbox: Arc<dyn Mailbox>,
    schema: JsonObject,
}

impl SendMessageTool {
    pub(crate) fn new(mailbox: Arc<dyn Mailbox>) -> Self {
        let schema = json!({
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Address of the recipient (a worker display name or session \
                                    handle).",
                },
                "content": {
                    "type": "string",
                    "description": "The message body to deliver to the recipient.",
                },
                "type": {
                    "type": "string",
                    "description": "Optional neutral category for the message (default \
                                    'message').",
                },
                "request_id": {
                    "type": "string",
                    "description": "Optional correlation id when this message expects a reply.",
                },
                "in_reply_to": {
                    "type": "string",
                    "description": "Optional request_id this message is a response to.",
                },
            },
            "required": ["to", "content"],
        });
        let JsonValue::Object(schema) = schema else {
            unreachable!("the schema is an object")
        };
        Self { mailbox, schema }
    }
}

/// The non-empty string at `field`.
fn text<'a>(input: &'a JsonObject, field: &str) -> Option<&'a str> {
    let value = input.get(field).and_then(JsonValue::as_str);
    value.filter(|value| !value.is_empty())
}

fn recipient(input: &JsonObject) -> &str {
    text(input, "to").unwrap_or("").trim()
}

#[async_trait]
impl Tool for SendMessageTool {
    fn name(&self) -> &str {
        SEND_MESSAGE_TOOL
    }

    fn description(&self) -> &str {
        DESCRIPTION
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if recipient(&input).is_empty() {
            return Err(ToolOutputError::new(
                "'to' must be a non-empty recipient address",
            ));
        }
        if text(&input, "content").is_none() {
            return Err(ToolOutputError::new("'content' must be a non-empty string"));
        }
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Send message to {}", recipient(input))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let to = recipient(&input).to_owned();
        let content = text(&input, "content").unwrap_or("");
        let mut message = MailboxMessage::new(ctx.session_id.as_str(), to.clone(), content);
        if let Some(kind) = text(&input, "type") {
            message.kind = kind.to_owned();
        }
        message.request_id = text(&input, "request_id").map(str::to_owned);
        message.in_reply_to = text(&input, "in_reply_to").map(str::to_owned);
        match self.mailbox.send(message).await {
            Ok(()) => Ok(ToolOutput::new(format!("Message delivered to '{to}'."))),
            Err(error) => Err(ToolOutputError::new(format!(
                "The message to '{to}' could not be delivered: {error}"
            ))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::coordination::InMemoryMailbox;
    use crate::ids::{RunId, SessionId};
    use tokio_util::sync::CancellationToken;

    fn input(value: JsonValue) -> JsonObject {
        value.as_object().unwrap().clone()
    }

    #[tokio::test]
    async fn a_message_goes_from_the_calling_session_with_its_fields() {
        let mailbox = Arc::new(InMemoryMailbox::new());
        let tool = SendMessageTool::new(mailbox.clone());
        for (raw, error) in [
            (
                json!({"to": " ", "content": "x"}),
                "'to' must be a non-empty recipient address",
            ),
            (
                json!({"to": "a", "content": ""}),
                "'content' must be a non-empty string",
            ),
        ] {
            assert_eq!(tool.validate(input(raw)).unwrap_err().message, error);
        }
        let raw = json!({"to": " bob ", "content": "hi", "type": "plan", "request_id": "r1",
                         "in_reply_to": ""});
        let raw = tool.validate(input(raw)).unwrap();
        assert_eq!(tool.summarize(&raw), "Send message to bob");
        let session = SessionId::new("alice");
        let token = CancellationToken::new();
        let ctx = ToolContext::new(session, RunId::new("r"), "t1", None, token, None);
        let output = tool.execute(raw, ctx).await.unwrap();
        assert_eq!(output.value, "Message delivered to 'bob'.");
        let sent = mailbox.drain("bob").await.unwrap();
        assert_eq!(
            (sent[0].sender.as_str(), sent[0].content.as_str()),
            ("alice", "hi")
        );
        assert_eq!(sent[0].kind, "plan");
        assert_eq!(sent[0].request_id.as_deref(), Some("r1"));
        assert_eq!(sent[0].in_reply_to, None, "an empty string is no value");
    }
}
