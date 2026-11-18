//! The run's side of the cache diagnostic (see [`crate::cache_diagnostic`]):
//! note each request as it is sent, and stream the comparison when the call
//! is answered. Nothing here runs unless the agent turned
//! `monitoring.cache_diagnostic` on, and the event it makes is stream-only:
//! never staged for a write, so the durable log, its seqs and the run's
//! barriers are what they are without it.

use std::time::Instant;

use super::turn::Runner;
use crate::events::Event;
use crate::provider::ProviderRequest;
use crate::usage::Usage;

impl Runner {
    /// Note `request`, which is about to be sent.
    pub(super) fn cache_sending(&mut self, request: &ProviderRequest) {
        let Some(watch) = &mut self.cache_watch else {
            return;
        };
        watch.sending(self.agent.provider.id(), request, Instant::now());
    }

    /// The call was answered, with `usage` when the provider reported it:
    /// stream what the cache did against the previous call.
    pub(super) fn cache_settled(&mut self, usage: Option<Usage>) {
        let Some(usage) = usage else {
            return;
        };
        let Some(event) = (self.cache_watch.as_mut()).and_then(|watch| watch.settled(&usage))
        else {
            return;
        };
        if let Some(envelope) = self.journal.stage(Event::CacheDiagnostic(event)) {
            self.outbox.push(envelope);
        }
    }
}
