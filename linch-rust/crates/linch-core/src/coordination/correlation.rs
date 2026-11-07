//! [`Correlator`]: which requests still wait for their response.

use std::collections::HashMap;

use super::MailboxMessage;

/// A pending-to-resolved state machine keyed by `request_id`.
///
/// It never blocks: a turn-based agent opens a request, goes on, and checks
/// [`Self::is_resolved`] on a later turn, after its inbox was drained. What
/// counts as a request or a response is up to the host.
#[derive(Debug, Clone, Default)]
pub struct Correlator {
    /// Open requests, in the order they were opened.
    pending: Vec<String>,
    responses: HashMap<String, MailboxMessage>,
}

impl Correlator {
    /// A correlator with no requests.
    pub fn new() -> Self {
        Self::default()
    }

    /// Open `request_id`, unless it is open or resolved already.
    pub fn open(&mut self, request_id: impl Into<String>) {
        let request_id = request_id.into();
        if !self.responses.contains_key(&request_id) && !self.pending.contains(&request_id) {
            self.pending.push(request_id);
        }
    }

    /// Resolve the open request that `response` answers (its
    /// `in_reply_to`).
    ///
    /// Returns:
    ///     Whether this response resolved a request: false for a response
    ///     with no `in_reply_to`, for one to a request that is not open, and
    ///     for a second response (the first one wins).
    pub fn resolve(&mut self, response: MailboxMessage) -> bool {
        let Some(request_id) = response.in_reply_to.clone() else {
            return false;
        };
        let Some(at) = self.pending.iter().position(|id| *id == request_id) else {
            return false;
        };
        self.pending.remove(at);
        self.responses.insert(request_id, response);
        true
    }

    /// Whether `request_id` has its response.
    pub fn is_resolved(&self, request_id: &str) -> bool {
        self.responses.contains_key(request_id)
    }

    /// The response that resolved `request_id`, if any.
    pub fn response(&self, request_id: &str) -> Option<&MailboxMessage> {
        self.responses.get(request_id)
    }

    /// The requests still waiting, in the order they were opened.
    pub fn pending(&self) -> &[String] {
        &self.pending
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reply(to: Option<&str>, content: &str) -> MailboxMessage {
        let mut message = MailboxMessage::new("lead", "alice", content);
        message.in_reply_to = to.map(str::to_owned);
        message
    }

    #[test]
    fn the_first_response_resolves_an_open_request() {
        let mut correlator = Correlator::new();
        correlator.open("r1");
        correlator.open("r2");
        correlator.open("r1");
        assert_eq!(correlator.pending(), ["r1", "r2"]);
        assert!(!correlator.resolve(reply(None, "no id")));
        assert!(!correlator.resolve(reply(Some("r9"), "unknown")));
        assert!(correlator.resolve(reply(Some("r1"), "approved")));
        assert!(!correlator.resolve(reply(Some("r1"), "again")));
        assert!(correlator.is_resolved("r1"));
        assert!(!correlator.is_resolved("r2"));
        assert_eq!(correlator.response("r1").unwrap().content, "approved");
        assert_eq!(correlator.pending(), ["r2"]);
        correlator.open("r1");
        assert_eq!(
            correlator.pending(),
            ["r2"],
            "a resolved request stays closed"
        );
    }
}
