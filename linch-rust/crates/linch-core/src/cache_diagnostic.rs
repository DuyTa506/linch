//! Why a provider's prompt cache read what it did: the run's
//! [`CacheWatch`] compares each provider call with the run's previous one.
//!
//! It is an observation and nothing more. A call is described by hashes of
//! its system text, its tool definitions and each of its messages, never by
//! content, and the watch is built only for an agent that turned
//! `monitoring.cache_diagnostic` on: without it no request is hashed. The
//! watch lives in memory for one run call, so the first call after a resume
//! has nothing to compare with and says nothing.

use std::hash::{DefaultHasher, Hasher};
use std::io;
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::events::kinds::{CacheDiagnosticEvent, CacheMissReason};
use crate::provider::{CacheTtl, ProviderRequest};
use crate::usage::Usage;

/// Fewest input tokens a call must have for the provider to cache any of
/// them; under it a call says nothing about the cache.
const CACHE_FLOOR: u64 = 1024;

/// The same for OpenAI's own endpoints.
const OPENAI_CACHE_FLOOR: u64 = 2048;

/// How long Anthropic keeps a cache entry that is not read again, with
/// the default lifetime.
const ANTHROPIC_TTL: Duration = Duration::from_secs(300);

/// With `CacheTtl::OneHour`.
const ANTHROPIC_LONG_TTL: Duration = Duration::from_secs(3600);

/// What of one request the watch keeps: hashes and an instant.
#[derive(Debug)]
struct Sent {
    provider: String,
    model: String,
    system: u64,
    tools: u64,
    /// One hash per message, in order.
    messages: Vec<u64>,
    at: Instant,
    /// How long the provider keeps an entry unread, when it says.
    ttl: Option<Duration>,
}

/// A call that was answered, and what the provider reported of it.
#[derive(Debug)]
struct Settled {
    sent: Sent,
    prompt_tokens: u64,
    cached: u64,
}

/// One run's view of its provider calls.
#[derive(Debug, Default)]
pub(crate) struct CacheWatch {
    sent: Option<Sent>,
    last: Option<Settled>,
}

impl CacheWatch {
    /// Note the request about to be sent to `provider`, at `at`.
    pub(crate) fn sending(&mut self, provider: &str, request: &ProviderRequest, at: Instant) {
        self.sent = Some(Sent {
            provider: provider.to_owned(),
            model: request.model.clone(),
            system: hash_of(&request.system),
            tools: tools_hash(request),
            messages: request.messages.iter().map(hash_of).collect(),
            at,
            ttl: ttl(provider, request),
        });
    }

    /// The call noted by [`Self::sending`] was answered with `usage`:
    /// compare it with the previous one and keep it as the new previous.
    ///
    /// Returns:
    ///     The event to send, or `None` for the first call, a call whose
    ///     input is under the provider's caching floor, or one that
    ///     reported no input tokens.
    pub(crate) fn settled(&mut self, usage: &Usage) -> Option<CacheDiagnosticEvent> {
        let sent = self.sent.take()?;
        let prompt_tokens = usage.total_input_tokens();
        if prompt_tokens == 0 {
            return None;
        }
        let cached = usage.cache_read_tokens;
        let event = match &self.last {
            Some(last) if prompt_tokens >= floor(&sent.provider) => {
                Some(diagnose(last, &sent, prompt_tokens, cached))
            }
            _ => None,
        };
        self.last = Some(Settled {
            sent,
            prompt_tokens,
            cached,
        });
        event
    }
}

/// The reason `now` read `cached` tokens of its `prompt_tokens`, against
/// `last`, by the first check that holds.
fn diagnose(last: &Settled, now: &Sent, prompt_tokens: u64, cached: u64) -> CacheDiagnosticEvent {
    let before = &last.sent;
    let common = before.messages.len().min(now.messages.len());
    let first_changed = (0..common)
        .find(|&i| before.messages[i] != now.messages[i])
        .or((now.messages.len() < before.messages.len()).then_some(now.messages.len()));
    let expired = now
        .ttl
        .is_some_and(|ttl| now.at.saturating_duration_since(before.at) > ttl);
    let reason = if before.model != now.model {
        CacheMissReason::ModelChanged
    } else if before.provider != now.provider {
        CacheMissReason::ProviderChanged
    } else if expired {
        CacheMissReason::TtlExpired
    } else if before.system != now.system {
        CacheMissReason::SystemChanged
    } else if before.tools != now.tools {
        CacheMissReason::ToolsChanged
    } else if now.messages.len() < before.messages.len() {
        CacheMissReason::Compaction
    } else if first_changed.is_some() {
        CacheMissReason::PrefixChanged
    } else if cached.saturating_mul(2) < last.prompt_tokens {
        CacheMissReason::LowRead
    } else {
        CacheMissReason::NoMiss
    };
    CacheDiagnosticEvent {
        reason,
        previous_cached: last.cached,
        cached,
        prompt_tokens,
        first_changed_message: first_changed.map(|index| index as u64),
    }
}

/// The input tokens under which `provider` caches nothing.
fn floor(provider: &str) -> u64 {
    if provider.starts_with("openai") {
        OPENAI_CACHE_FLOOR
    } else {
        CACHE_FLOOR
    }
}

/// How long `provider` keeps a cache entry nobody reads, when it says.
/// Anthropic's is the lifetime the request asked for (5 minutes unless it
/// is 1 hour); no other adapter declares one, since OpenAI's depends on the
/// model and retention setting and the rest state none.
fn ttl(provider: &str, request: &ProviderRequest) -> Option<Duration> {
    if provider != "anthropic" {
        return None;
    }
    let long = request
        .cache_control
        .is_some_and(|control| control.ttl == Some(CacheTtl::OneHour));
    Some(if long {
        ANTHROPIC_LONG_TTL
    } else {
        ANTHROPIC_TTL
    })
}

/// The tools the model is offered, their order and the ones appended after
/// the frozen prefix.
fn tools_hash(request: &ProviderRequest) -> u64 {
    let mut hasher = DefaultHasher::new();
    feed(&mut hasher, &request.tools);
    feed(&mut hasher, &request.appended_tools);
    hasher.finish()
}

fn hash_of<T: Serialize>(value: &T) -> u64 {
    let mut hasher = DefaultHasher::new();
    feed(&mut hasher, value);
    hasher.finish()
}

/// Write `value`'s JSON into `hasher` without building the text.
fn feed<T: Serialize>(hasher: &mut DefaultHasher, value: &T) {
    struct Sink<'a>(&'a mut DefaultHasher);
    impl io::Write for Sink<'_> {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0.write(bytes);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    if serde_json::to_writer(Sink(&mut *hasher), value).is_err() {
        // A value that does not encode hashes as one marker, the same each
        // time, so it never reads as a change.
        hasher.write_u8(0xff);
    }
}

#[cfg(test)]
mod tests;
