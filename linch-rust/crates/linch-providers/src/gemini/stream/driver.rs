//! The cancellable SSE loop around [`Translator`].

use std::collections::VecDeque;
use std::fmt::Display;

use eventsource_stream::Eventsource;
use futures::{Stream, StreamExt};
use linch_core::{ProviderError, ProviderEvent, ProviderStream};
use tokio_util::sync::CancellationToken;

use super::{Translator, transport};
use crate::ApiKey;

struct State<S> {
    sse: S,
    translator: Translator,
    pending: VecDeque<ProviderEvent>,
    cancel: CancellationToken,
    key: ApiKey,
    done: bool,
}

/// Drive `translator` over an SSE byte stream until it closes, an error or
/// `cancel`. A clean close ends the response. Transport error text is
/// redacted with `key`.
pub(crate) fn events<S, B, E>(
    bytes: S,
    translator: Translator,
    cancel: CancellationToken,
    key: ApiKey,
) -> ProviderStream
where
    S: Stream<Item = Result<B, E>> + Send + 'static,
    B: AsRef<[u8]> + Send + 'static,
    E: Display + Send + 'static,
{
    let state = State {
        sse: Box::pin(bytes.eventsource()),
        translator,
        pending: VecDeque::new(),
        cancel,
        key,
        done: false,
    };
    futures::stream::unfold(state, |mut state| async move {
        let item = next(&mut state).await?;
        Some((item, state))
    })
    .boxed()
}

async fn next<S, E>(state: &mut State<S>) -> Option<Result<ProviderEvent, ProviderError>>
where
    S: Stream<Item = Result<eventsource_stream::Event, eventsource_stream::EventStreamError<E>>>
        + Unpin,
    E: Display,
{
    loop {
        if state.done && state.pending.is_empty() {
            return None;
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
                match state.translator.finish(&mut state.pending) {
                    Ok(()) => continue,
                    Err(err) => err,
                }
            }
            Some(Some(Err(err))) => transport(&state.key.redact(&err.to_string())),
            Some(Some(Ok(event))) if event.data.trim().is_empty() => continue,
            Some(Some(Ok(event))) => match state.translator.push(&event.data, &mut state.pending) {
                Ok(()) => continue,
                Err(err) => err,
            },
        };
        return Some(Err(stop(state, failure)));
    }
}

/// End the stream after `err`: nothing is yielded after an error.
fn stop<S>(state: &mut State<S>, err: ProviderError) -> ProviderError {
    state.done = true;
    state.pending.clear();
    err
}
