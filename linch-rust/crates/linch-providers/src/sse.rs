//! The one cancellable SSE loop that every streaming adapter runs.
//!
//! An adapter supplies a [`Decoder`] for its wire format; [`events`] feeds
//! it the response's SSE events, stops on cancellation, and scrubs
//! credentials from every error it yields.

use std::collections::VecDeque;
use std::fmt::Display;

use eventsource_stream::{Event, EventStreamError, Eventsource};
use futures::{Stream, StreamExt, TryStreamExt};
use linch_core::{ProviderError, ProviderEvent, ProviderStream};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::api_key::scrub;

/// One response's wire format, fed one SSE event at a time.
pub(crate) trait Decoder: Send + 'static {
    /// Translate one non-blank `data:` payload into `out`. Returns `true`
    /// when the response is complete; nothing after it is read.
    fn data(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<bool, ProviderError>;

    /// The byte stream closed before the response was complete. A format
    /// with an end marker fails here; one without finishes the response.
    fn close(&mut self, out: &mut VecDeque<ProviderEvent>) -> Result<(), ProviderError>;
}

struct State<S, D> {
    sse: S,
    decoder: D,
    pending: VecDeque<ProviderEvent>,
    cancel: CancellationToken,
    key: Option<ApiKey>,
    done: bool,
}

/// Drive `decoder` over an SSE byte stream until the response completes,
/// an error or `cancel`. Error text is scrubbed of `key` and bearer
/// tokens, and nothing is yielded after an error.
pub(crate) fn events<D, S, B, E>(
    bytes: S,
    decoder: D,
    cancel: CancellationToken,
    key: impl Into<Option<ApiKey>>,
) -> ProviderStream
where
    D: Decoder,
    S: Stream<Item = Result<B, E>> + Send + 'static,
    B: AsRef<[u8]> + Send + 'static,
    E: Display + Send + 'static,
{
    // eventsource-stream drops an event with no blank line after it at the
    // end of the body; a closing blank line dispatches it.
    let body = bytes
        .map_ok(Piece::Body)
        .chain(futures::stream::once(async { Ok(Piece::End) }));
    let state = State {
        sse: Box::pin(body.eventsource()),
        decoder,
        pending: VecDeque::new(),
        cancel,
        key: key.into(),
        done: false,
    };
    futures::stream::unfold(state, |mut state| async move {
        let item = next(&mut state).await?;
        Some((item, state))
    })
    .boxed()
}

/// A chunk of the response body, or the blank line added after it.
enum Piece<B> {
    Body(B),
    End,
}

impl<B: AsRef<[u8]>> AsRef<[u8]> for Piece<B> {
    fn as_ref(&self) -> &[u8] {
        match self {
            Self::Body(bytes) => bytes.as_ref(),
            Self::End => b"\n\n",
        }
    }
}

async fn next<S, D, E>(state: &mut State<S, D>) -> Option<Result<ProviderEvent, ProviderError>>
where
    S: Stream<Item = Result<Event, EventStreamError<E>>> + Unpin,
    D: Decoder,
    E: Display,
{
    loop {
        // A finished response is queued whole, its end and usage included;
        // a cancel that arrives now does not drop what is already known.
        if state.done {
            return state.pending.pop_front().map(Ok);
        }
        if state.cancel.is_cancelled() {
            return Some(Err(stop(state, ProviderError::Cancelled)));
        }
        if let Some(event) = state.pending.pop_front() {
            return Some(Ok(event));
        }
        let failure = match state.cancel.run_until_cancelled(state.sse.next()).await {
            None => ProviderError::Cancelled,
            Some(None) => {
                state.done = true;
                match state.decoder.close(&mut state.pending) {
                    Ok(()) => continue,
                    Err(err) => err,
                }
            }
            Some(Some(Err(err))) => ProviderError::Transport {
                message: err.to_string(),
            },
            Some(Some(Ok(event))) if event.data.trim().is_empty() => continue,
            Some(Some(Ok(event))) => match state.decoder.data(&event.data, &mut state.pending) {
                Ok(done) => {
                    state.done = done;
                    continue;
                }
                Err(err) => err,
            },
        };
        let failure = scrubbed(state.key.as_ref(), failure);
        return Some(Err(stop(state, failure)));
    }
}

/// Server and transport text can echo request headers; scrub it.
fn scrubbed(key: Option<&ApiKey>, err: ProviderError) -> ProviderError {
    match err {
        ProviderError::Transport { message } => ProviderError::Transport {
            message: scrub(key, &message),
        },
        ProviderError::Status {
            status,
            retry_after,
            message,
        } => ProviderError::Status {
            status,
            retry_after,
            message: scrub(key, &message),
        },
        other => other,
    }
}

/// End the stream after `err`: nothing is yielded after an error.
fn stop<S, D>(state: &mut State<S, D>, err: ProviderError) -> ProviderError {
    state.done = true;
    state.pending.clear();
    err
}
