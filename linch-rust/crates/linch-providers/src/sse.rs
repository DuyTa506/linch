//! The one cancellable SSE loop that every streaming adapter runs.
//!
//! An adapter supplies a [`Decoder`] for its wire format; [`events`] frames
//! the response body into SSE events, feeds them to it, stops on
//! cancellation, and scrubs credentials from every error it yields.

use std::collections::VecDeque;
use std::fmt::Display;

use futures::{Stream, StreamExt};
use linch_core::{ProviderError, ProviderEvent, ProviderStream};
use thiserror::Error;
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::api_key::scrub;

/// The most bytes held for one event, counted from the end of the last
/// event and including an unterminated line. The largest event an adapter
/// legitimately gets is the Responses terminal event, which repeats the
/// whole output: 189 KB for the 20 000-word bench stream, and about 2 MiB
/// for a 128 000-token answer with reasoning items and the echoed request.
/// Eight times that leaves room for any real response and still stops a
/// body that never ends an event.
pub(crate) const MAX_EVENT_BYTES: usize = 16 * 1024 * 1024;

/// The most characters of error text a stream may carry: the cut of an
/// HTTP error body.
pub(crate) const MAX_ERROR_CHARS: usize = 500;

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

/// Why a body cannot be framed into events.
#[derive(Debug, Error)]
enum FrameError {
    #[error("an event stream passed {MAX_EVENT_BYTES} bytes without the end of an event")]
    TooLarge,
    #[error("the event stream is not valid UTF-8")]
    NotUtf8,
}

/// The `data:` lines of the event being read.
#[derive(Default)]
struct Pending {
    data: String,
    any: bool,
}

impl Pending {
    /// Read one non-blank line. Only `data` lines carry anything a decoder
    /// reads; comments and the `event`, `id` and `retry` fields are dropped.
    fn line(&mut self, line: &str) {
        let (field, value) = line.split_once(':').unwrap_or((line, ""));
        if field == "data" {
            if self.any {
                self.data.push('\n');
            }
            self.any = true;
            self.data.push_str(value.strip_prefix(' ').unwrap_or(value));
        }
    }

    /// The event a blank line ends, when it has data.
    fn dispatch(&mut self) -> Option<String> {
        std::mem::take(&mut self.any).then(|| std::mem::take(&mut self.data))
    }
}

/// Frames a byte stream into SSE events in one pass: lines end in LF, CRLF
/// or CR, a leading byte order mark is dropped, and a blank line ends an
/// event. The buffer is read through a cursor and compacted once per
/// chunk, so the cost is linear in the bytes, and it never holds more than
/// [`MAX_EVENT_BYTES`] of one event.
#[derive(Default)]
struct Framer {
    buf: Vec<u8>,
    /// Start of the first line not yet read.
    start: usize,
    /// Where the line-end scan stopped, so a long line is scanned once.
    scanned: usize,
    /// `buf` before this is valid UTF-8; an incomplete character at the
    /// end waits for the next chunk.
    valid: usize,
    /// Bytes of the lines read since the last event end.
    event_bytes: usize,
    pending: Pending,
    bom_checked: bool,
    /// The last line ended in CR, so an LF that opens the next chunk is
    /// part of that line end.
    skip_lf: bool,
}

impl Framer {
    const BOM: &'static [u8] = b"\xef\xbb\xbf";

    /// Add one body chunk; the data of each event it ends goes to `out`.
    fn push(&mut self, chunk: &[u8], out: &mut VecDeque<String>) -> Result<(), FrameError> {
        self.buf.extend_from_slice(chunk);
        match std::str::from_utf8(&self.buf[self.valid..]) {
            Ok(_) => self.valid = self.buf.len(),
            Err(err) if err.error_len().is_none() => self.valid += err.valid_up_to(),
            Err(_) => return Err(FrameError::NotUtf8),
        }
        self.read_lines(out)?;
        self.buf.drain(..self.start);
        self.scanned -= self.start;
        self.valid -= self.start;
        self.start = 0;
        if self.event_bytes + self.buf.len() > MAX_EVENT_BYTES {
            return Err(FrameError::TooLarge);
        }
        Ok(())
    }

    /// The body ended: a last line and event with no end are delivered.
    fn finish(&mut self, out: &mut VecDeque<String>) -> Result<(), FrameError> {
        self.push(b"\n\n", out)
    }

    fn read_lines(&mut self, out: &mut VecDeque<String>) -> Result<(), FrameError> {
        if !self.bom_checked {
            let rest = &self.buf[self.start..];
            if rest.len() < Self::BOM.len() && Self::BOM.starts_with(rest) {
                return Ok(());
            }
            if rest.starts_with(Self::BOM) {
                self.start += Self::BOM.len();
                self.scanned = self.start;
            }
            self.bom_checked = true;
        }
        loop {
            if self.skip_lf {
                if self.start == self.buf.len() {
                    return Ok(());
                }
                if self.buf[self.start] == b'\n' {
                    self.start += 1;
                    self.scanned = self.start;
                }
                self.skip_lf = false;
            }
            let Some(at) = self.buf[self.scanned..]
                .iter()
                .position(|byte| matches!(byte, b'\n' | b'\r'))
            else {
                self.scanned = self.buf.len();
                return Ok(());
            };
            let end = self.scanned + at;
            let line =
                std::str::from_utf8(&self.buf[self.start..end]).map_err(|_| FrameError::NotUtf8)?;
            self.event_bytes += line.len() + 1;
            if line.is_empty() {
                self.event_bytes = 0;
                out.extend(self.pending.dispatch());
            } else {
                self.pending.line(line);
            }
            if self.event_bytes > MAX_EVENT_BYTES {
                return Err(FrameError::TooLarge);
            }
            self.skip_lf = self.buf[end] == b'\r';
            self.start = end + 1;
            self.scanned = self.start;
        }
    }
}

struct State<S, D> {
    bytes: S,
    framer: Framer,
    /// The data of events framed but not yet decoded.
    frames: VecDeque<String>,
    /// The byte stream has ended and the framer has been flushed.
    ended: bool,
    decoder: D,
    pending: VecDeque<ProviderEvent>,
    cancel: CancellationToken,
    key: Option<ApiKey>,
    done: bool,
}

/// Drive `decoder` over an SSE byte stream until the response completes,
/// an error or `cancel`. One event may hold [`MAX_EVENT_BYTES`]; a stream
/// that passes it, or that is not UTF-8, fails with a `Transport` error.
/// Error text is scrubbed of `key` and bearer tokens and cut to
/// [`MAX_ERROR_CHARS`], and nothing is yielded after an error.
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
    let state = State {
        bytes: Box::pin(bytes),
        framer: Framer::default(),
        frames: VecDeque::new(),
        ended: false,
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

async fn next<S, D, B, E>(state: &mut State<S, D>) -> Option<Result<ProviderEvent, ProviderError>>
where
    S: Stream<Item = Result<B, E>> + Unpin,
    B: AsRef<[u8]>,
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
        let failure = if let Some(data) = state.frames.pop_front() {
            if data.trim().is_empty() {
                continue;
            }
            match state.decoder.data(&data, &mut state.pending) {
                Ok(done) => {
                    state.done = done;
                    continue;
                }
                Err(err) => err,
            }
        } else if state.ended {
            state.done = true;
            match state.decoder.close(&mut state.pending) {
                Ok(()) => continue,
                Err(err) => err,
            }
        } else {
            match state.cancel.run_until_cancelled(state.bytes.next()).await {
                None => ProviderError::Cancelled,
                Some(None) => {
                    state.ended = true;
                    match state.framer.finish(&mut state.frames) {
                        Ok(()) => continue,
                        Err(err) => transport(err),
                    }
                }
                Some(Some(Err(err))) => transport(err),
                Some(Some(Ok(chunk))) => {
                    match state.framer.push(chunk.as_ref(), &mut state.frames) {
                        Ok(()) => continue,
                        Err(err) => transport(err),
                    }
                }
            }
        };
        let failure = scrubbed(state.key.as_ref(), failure);
        return Some(Err(stop(state, failure)));
    }
}

fn transport(err: impl Display) -> ProviderError {
    ProviderError::Transport {
        message: err.to_string(),
    }
}

/// Server and transport text can echo request headers, and a server can
/// send any amount of it: scrub it, then cut it, so a cut never leaves
/// part of a key behind.
fn scrubbed(key: Option<&ApiKey>, err: ProviderError) -> ProviderError {
    let clean = |message: String| -> String {
        scrub(key, &message).chars().take(MAX_ERROR_CHARS).collect()
    };
    match err {
        ProviderError::Transport { message } => ProviderError::Transport {
            message: clean(message),
        },
        ProviderError::Status {
            status,
            retry_after,
            message,
        } => ProviderError::Status {
            status,
            retry_after,
            message: clean(message),
        },
        ProviderError::ContextLength { status, message } => ProviderError::ContextLength {
            status,
            message: clean(message),
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

#[cfg(test)]
mod tests;
