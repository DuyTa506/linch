//! The SSE loop: framing grammar, the bound on one event, and the cut on
//! in-stream error text.

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use futures::{Stream, StreamExt};
use linch_core::{Message, Provider, ProviderError, ProviderEvent, ProviderRequest};
use tokio_util::sync::CancellationToken;

use super::{Decoder, Framer, KEEP_BYTES, MAX_EVENT_BYTES, events};
use crate::openai_compat::stream::{ReasoningField, Translator};
use crate::request::MAX_ERROR_CHARS;
use crate::test_server::serve_once;
use crate::{ApiKey, OpenAiResponses, ResponsesOptions};

const KIB: usize = 1024;

type Bytes = Result<Vec<u8>, std::io::Error>;

/// Hands every `data:` payload back as a text delta.
struct Echo;

impl Decoder for Echo {
    fn data(
        &mut self,
        data: &str,
        out: &mut std::collections::VecDeque<ProviderEvent>,
    ) -> Result<bool, ProviderError> {
        out.push_back(ProviderEvent::TextDelta(
            linch_core::provider::TextDeltaEvent { text: data.into() },
        ));
        Ok(false)
    }

    fn close(
        &mut self,
        _out: &mut std::collections::VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        Ok(())
    }
}

fn chat() -> Translator {
    Translator::new(
        "m".into(),
        Some(ReasoningField::ReasoningContent),
        crate::deepseek::usage,
    )
}

fn message(err: &ProviderError) -> &str {
    match err {
        ProviderError::Connect { message }
        | ProviderError::Transport { message }
        | ProviderError::Status { message, .. }
        | ProviderError::ContextLength { message, .. }
        | ProviderError::InvalidRequest { message } => message,
        _ => "",
    }
}

fn pieces(body: &[u8], size: usize) -> impl Stream<Item = Bytes> + Send + 'static {
    let chunks: Vec<Bytes> = body.chunks(size).map(|piece| Ok(piece.to_vec())).collect();
    futures::stream::iter(chunks)
}

/// The `data` of each event `body` frames into, read in `size`-byte chunks.
async fn framed(body: &[u8], size: usize) -> Vec<String> {
    let stream = events(
        pieces(body, size),
        Echo,
        CancellationToken::new(),
        None::<ApiKey>,
    );
    stream
        .map(|item| match item {
            Ok(ProviderEvent::TextDelta(delta)) => delta.text,
            other => panic!("unexpected item {other:?}"),
        })
        .collect()
        .await
}

/// `body` frames into `expected` however it is split into chunks.
async fn assert_framed(body: &[u8], expected: &[&str]) {
    for size in [1, 2, 3, 5, 64 * KIB] {
        assert_eq!(framed(body, size).await, expected, "chunks of {size} bytes");
    }
}

#[tokio::test]
async fn lines_end_in_lf_crlf_or_cr() {
    assert_framed(
        b"data: a\n\ndata: b\r\n\r\ndata: c\r\rdata: d\r\n\n",
        &["a", "b", "c", "d"],
    )
    .await;
}

#[tokio::test]
async fn a_cr_ending_a_chunk_and_an_lf_opening_the_next_are_one_line_end() {
    // Chunks of 1 byte put the CR and the LF in different chunks.
    assert_framed(b"data: a\r\ndata: b\r\n\r\n", &["a\nb"]).await;
}

#[tokio::test]
async fn data_lines_join_with_a_newline_and_one_leading_space_is_dropped() {
    assert_framed(b"data:a\ndata:  b\ndata\ndata: \n\n", &["a\n b\n\n"]).await;
}

#[tokio::test]
async fn event_id_retry_and_comment_lines_are_not_data() {
    assert_framed(
        b": keep-alive\nevent: ping\nid: 7\nretry: 1000\nunknown: x\ndata: a\n\n:\n\n",
        &["a"],
    )
    .await;
}

#[tokio::test]
async fn an_event_that_ends_with_the_body_is_still_delivered() {
    assert_framed(b"data: a\n\ndata: b", &["a", "b"]).await;
    assert_framed(b"data: a\n\ndata: b\r", &["a", "b"]).await;
}

#[tokio::test]
async fn a_character_split_across_chunks_is_read_whole() {
    assert_framed(
        "data: h\u{e9}llo \u{1f600}\n\n".as_bytes(),
        &["h\u{e9}llo \u{1f600}"],
    )
    .await;
}

#[tokio::test]
async fn a_leading_byte_order_mark_is_dropped() {
    // Chunks of 1 and 2 bytes split the three-byte mark.
    assert_framed("\u{feff}data: a\n\ndata: b\n\n".as_bytes(), &["a", "b"]).await;
}

#[tokio::test]
async fn a_byte_order_mark_after_the_start_is_data_not_a_mark() {
    assert_framed("data: a\n\n\u{feff}data: b\n\n".as_bytes(), &["a"]).await;
}

#[tokio::test]
async fn a_blank_line_with_no_data_ends_the_event_and_resets_its_size() {
    // Each event stays under the bound; together they pass it.
    let one = format!("data: {}\n", "x".repeat(MAX_EVENT_BYTES / 2));
    let body = format!("{one}\n{one}\n{one}\n");
    let items = framed(body.as_bytes(), 64 * KIB).await;
    assert_eq!(items.len(), 3);
}

/// Pull counts the bytes handed to the loop.
fn counted(
    head: Vec<u8>,
    chunk: Vec<u8>,
    total: usize,
    pulled: Arc<AtomicUsize>,
) -> impl Stream<Item = Bytes> + Send + 'static {
    let chunks = total / chunk.len();
    futures::stream::iter(std::iter::once(head).chain((0..chunks).map(move |_| chunk.clone()))).map(
        move |bytes| {
            pulled.fetch_add(bytes.len(), Ordering::SeqCst);
            Ok(bytes)
        },
    )
}

/// The first item the loop yields over `head` and then `chunk`s up to
/// twice the bound, and how many bytes it had pulled by then.
async fn first_item(
    head: &[u8],
    chunk: &[u8],
) -> (Option<Result<ProviderEvent, ProviderError>>, usize) {
    let pulled = Arc::new(AtomicUsize::new(0));
    let bytes = counted(
        head.to_vec(),
        chunk.to_vec(),
        2 * MAX_EVENT_BYTES,
        Arc::clone(&pulled),
    );
    let mut stream = events(bytes, chat(), CancellationToken::new(), None::<ApiKey>);
    let first = stream.next().await;
    (first, pulled.load(Ordering::SeqCst))
}

/// What the loop may have pulled when it refuses a stream: the bound and
/// the chunk that crosses it.
const PULL_LIMIT: usize = MAX_EVENT_BYTES + 64 * KIB;

#[tokio::test]
async fn an_event_with_no_end_is_refused_at_the_bound() {
    let line = format!("data: {}\n", "x".repeat(1023 - 6));
    let (first, pulled) = first_item(b"", line.repeat(64).as_bytes()).await;
    assert!(matches!(first, Some(Err(_))), "{first:?}");
    assert!(
        pulled <= PULL_LIMIT,
        "pulled {} KiB of one unterminated event before failing",
        pulled / KIB
    );
}

#[tokio::test]
async fn a_line_with_no_end_is_refused_at_the_bound() {
    let (first, pulled) = first_item(b"data: ", &[b'x'; 64 * KIB]).await;
    assert!(matches!(first, Some(Err(_))), "{first:?}");
    assert!(
        pulled <= PULL_LIMIT,
        "pulled {} KiB of one unterminated line before failing",
        pulled / KIB
    );
}

#[tokio::test]
async fn a_byte_that_is_not_utf8_fails_the_call_when_it_is_read() {
    let chunk = "data: {\"choices\":[]}\n\n".repeat(2 * KIB);
    let (first, pulled) = first_item(&[0xff], chunk.as_bytes()).await;
    assert!(matches!(first, Some(Err(_))), "{first:?}");
    assert_eq!(pulled, 1, "pulled {pulled} bytes behind one invalid byte");
}

#[tokio::test]
async fn a_byte_that_is_not_utf8_inside_an_unterminated_line_fails_the_call() {
    let (first, pulled) = first_item(b"data: ab\xffcd", &[b'x'; 64 * KIB]).await;
    assert!(matches!(first, Some(Err(_))), "{first:?}");
    assert_eq!(pulled, 11, "pulled {pulled} bytes behind one invalid byte");
}

#[tokio::test]
async fn a_body_that_ends_inside_a_character_fails_the_call() {
    let body = b"data: a\n\ndata: \xe2\x82";
    let items: Vec<_> = events(
        pieces(body, 64 * KIB),
        Echo,
        CancellationToken::new(),
        None::<ApiKey>,
    )
    .collect()
    .await;
    assert!(matches!(items.last(), Some(Err(_))), "{items:?}");
}

/// One 400 KiB chunk of comment lines (about the largest chunk reqwest
/// hands over on loopback) frames in the time of a linear pass. Framing
/// that copies the rest of the chunk for each line takes a second; a
/// linear pass takes a few milliseconds in a release build and tens in a
/// debug build. The least of several runs, so a busy machine does not
/// decide the result.
#[tokio::test]
async fn framing_one_large_chunk_of_comment_lines_is_linear() {
    let body = b":\n".repeat(200 * KIB);
    let mut least = Duration::MAX;
    for _ in 0..5 {
        let start = Instant::now();
        let stream = events(
            pieces(&body, body.len()),
            Echo,
            CancellationToken::new(),
            None::<ApiKey>,
        );
        assert_eq!(stream.count().await, 0);
        least = least.min(start.elapsed());
    }
    assert!(
        least < Duration::from_millis(300),
        "one 400 KiB chunk of comment lines took {least:?}"
    );
}

#[tokio::test]
async fn a_body_that_starts_with_a_byte_order_mark_does_not_panic() {
    let body = "\u{feff}data: {\"model\":\"m\",\"choices\":[{\"delta\":{\"content\":\"hi\"},\
                \"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n";
    let stream = events(
        pieces(body.as_bytes(), 64 * KIB),
        chat(),
        CancellationToken::new(),
        None::<ApiKey>,
    );
    let items = tokio::spawn(stream.collect::<Vec<_>>())
        .await
        .expect("the stream does not panic");
    assert!(items.iter().all(Result::is_ok), "{items:?}");
}

/// The longest error text a stream may carry, with room for the adapter's
/// own prefix.
const ERROR_TEXT_BOUND: usize = MAX_ERROR_CHARS + 64;

#[tokio::test]
async fn a_chat_error_frame_is_cut_like_an_http_error_body() {
    let frame = serde_json::json!({
        "error": {"type": "server_error", "message": "e".repeat(256 * KIB)}
    });
    let body = format!("data: {frame}\n\n");
    let mut stream = events(
        pieces(body.as_bytes(), 64 * KIB),
        chat(),
        CancellationToken::new(),
        None::<ApiKey>,
    );
    let Some(Err(err)) = stream.next().await else {
        panic!("an error frame fails the call");
    };
    assert!(
        message(&err).len() <= ERROR_TEXT_BOUND,
        "the in-stream error text is {} bytes",
        message(&err).len()
    );
}

#[tokio::test]
async fn an_unknown_finish_reason_is_cut() {
    let frame = serde_json::json!({
        "model": "m",
        "choices": [{"delta": {}, "finish_reason": "r".repeat(256 * KIB)}]
    });
    let body = format!("data: {frame}\n\n");
    let stream = events(
        pieces(body.as_bytes(), 64 * KIB),
        chat(),
        CancellationToken::new(),
        None::<ApiKey>,
    );
    let items: Vec<_> = stream.collect().await;
    let Some(Err(err)) = items.last() else {
        panic!("an unknown finish_reason fails the call");
    };
    assert!(
        message(err).len() <= ERROR_TEXT_BOUND,
        "the finish_reason error text is {} bytes",
        message(err).len()
    );
}

#[tokio::test]
async fn a_cut_error_text_never_holds_part_of_the_key() {
    let key = ApiKey::new("sk-unit-test-key-0123456789").unwrap();
    let filler = "e".repeat(MAX_ERROR_CHARS - 5);
    let frame = serde_json::json!({
        "error": {"type": "server_error", "message": format!("{filler}sk-unit-test-key-0123456789")}
    });
    let body = format!("data: {frame}\n\n");
    let mut stream = events(
        pieces(body.as_bytes(), 64 * KIB),
        chat(),
        CancellationToken::new(),
        Some(key),
    );
    let Some(Err(err)) = stream.next().await else {
        panic!("an error frame fails the call");
    };
    assert!(!message(&err).contains("sk-unit"), "{}", message(&err));
}

fn request(model: &str) -> ProviderRequest {
    ProviderRequest {
        model: model.into(),
        system: vec![],
        tools: vec![],
        appended_tools: vec![],
        context_tail: 0,
        messages: vec![Message::user_text("hi")],
        max_output_tokens: None,
        temperature: None,
        stop_sequences: vec![],
        effort: None,
        thinking: None,
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

#[tokio::test]
async fn a_responses_error_event_is_cut_like_an_http_error_body() {
    let frame = serde_json::json!({
        "type": "error", "code": "server_error", "message": "e".repeat(256 * KIB)
    });
    let body = format!("data: {frame}\n\n");
    let response = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    );
    let (url, _served) = serve_once(response);
    let mut options = ResponsesOptions::new(ApiKey::new("sk-unit-test-key").unwrap());
    options.base_url = url;
    let provider = OpenAiResponses::new(options).unwrap();
    let stream = provider
        .stream(request("gpt-5.5"), CancellationToken::new())
        .await
        .unwrap();
    let items: Vec<_> = stream.collect().await;
    let Some(Err(err)) = items.last() else {
        panic!("an error event fails the call");
    };
    assert!(
        message(err).len() <= ERROR_TEXT_BOUND,
        "the in-stream error text is {} bytes",
        message(err).len()
    );
}

// ---- framing is independent of where chunks end

/// What the framer yields for `pieces`: the data of each event, in order,
/// and the error that ended the framing, if any. Events completed before
/// a failure are kept, as the loop delivers them before the error.
fn framed_until_error(pieces: &[&[u8]]) -> (Vec<String>, Option<String>) {
    let mut framer = Framer::default();
    let mut out = VecDeque::new();
    let mut frames = Vec::new();
    for piece in pieces {
        let result = framer.push(piece, &mut out);
        frames.extend(out.drain(..));
        if let Err(err) = result {
            return (frames, Some(err.to_string()));
        }
    }
    let result = framer.finish(&mut out);
    frames.extend(out.drain(..));
    (frames, result.err().map(|err| err.to_string()))
}

/// `body` whole and cut in two at every position gives one result.
fn assert_same_at_every_split(body: &[u8], step: usize) -> (Vec<String>, Option<String>) {
    let whole = framed_until_error(&[body]);
    for cut in (0..=body.len()).step_by(step) {
        assert_eq!(
            framed_until_error(&[&body[..cut], &body[cut..]]),
            whole,
            "{:?} cut at {cut}",
            String::from_utf8_lossy(body)
        );
    }
    whole
}

#[test]
fn the_events_before_a_bad_byte_are_delivered_however_the_body_is_cut() {
    let (frames, error) = assert_same_at_every_split(b"data: a\n\ndata: b\n\n\xffdata: c\n\n", 1);
    assert_eq!(frames, ["a", "b"]);
    assert!(error.is_some());
    // The bad byte inside a line that has not ended, and at the very end.
    let (frames, error) = assert_same_at_every_split(b"data: a\n\ndata: b\xffc\n\n", 1);
    assert_eq!((frames, error.is_some()), (vec!["a".to_owned()], true));
    let (frames, error) = assert_same_at_every_split(b"data: a\n\n\xff", 1);
    assert_eq!((frames, error.is_some()), (vec!["a".to_owned()], true));
    // A character that the body ends in the middle of.
    let (frames, error) = assert_same_at_every_split(b"data: a\n\ndata: \xe2\x82", 1);
    assert_eq!((frames, error.is_some()), (vec!["a".to_owned()], true));
}

#[test]
fn the_events_before_the_bound_are_delivered_however_the_body_is_cut() {
    let mut body = b"data: a\n\ndata: b\n\ndata: ".to_vec();
    body.resize(body.len() + MAX_EVENT_BYTES, b'x');
    let whole = framed_until_error(&[&body]);
    assert_eq!(whole.0, ["a", "b"]);
    assert!(whole.1.is_some());
    for cut in [0, 1, 8, 9, 10, 17, 18, 24, 1024, 1 << 20, body.len() - 1] {
        assert_eq!(
            framed_until_error(&[&body[..cut], &body[cut..]]),
            whole,
            "cut at {cut}"
        );
    }
}

#[tokio::test]
async fn the_loop_delivers_the_events_before_the_bound_and_then_the_error() {
    let mut body = b"data: a\n\ndata: b\n\ndata: ".to_vec();
    body.resize(body.len() + MAX_EVENT_BYTES, b'x');
    let items: Vec<_> = events(
        pieces(&body, body.len()),
        Echo,
        CancellationToken::new(),
        None::<ApiKey>,
    )
    .collect()
    .await;
    assert_eq!(items.len(), 3, "{items:?}");
    assert!(items[..2].iter().all(Result::is_ok));
    assert!(items[2].is_err());
}

fn chat_body() -> String {
    "data: {\"model\":\"m\",\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n\
     data: {\"model\":\"m\",\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n\
     data: [DONE]\n\n"
        .to_owned()
}

#[tokio::test]
async fn a_complete_response_is_not_failed_by_a_bad_byte_after_its_end_marker() {
    let mut body = chat_body().into_bytes();
    body.push(0xff);
    let bytes = futures::stream::iter([Ok::<_, std::io::Error>(body)]);
    let items: Vec<_> = events(bytes, chat(), CancellationToken::new(), None::<ApiKey>)
        .collect()
        .await;
    assert!(items.iter().all(Result::is_ok), "{items:?}");
    assert!(matches!(
        items.last(),
        Some(Ok(ProviderEvent::MessageEnd(_)))
    ));
}

#[tokio::test]
async fn a_response_gives_the_same_items_wherever_a_bad_byte_chunk_ends() {
    let mut body = chat_body().into_bytes();
    let complete = body.len();
    body.extend_from_slice(b"data: x\n\n\xff");
    let run = |pieces: Vec<Vec<u8>>| async move {
        let bytes = futures::stream::iter(pieces.into_iter().map(Ok::<_, std::io::Error>));
        events(bytes, chat(), CancellationToken::new(), None::<ApiKey>)
            .map(|item| format!("{item:?}"))
            .collect::<Vec<_>>()
            .await
    };
    let whole = run(vec![body.clone()]).await;
    for cut in [1, 20, complete - 1, complete, complete + 5, body.len() - 1] {
        let pieces = vec![body[..cut].to_vec(), body[cut..].to_vec()];
        assert_eq!(run(pieces).await, whole, "cut at {cut}");
    }
}

// ---- every adapter's fixtures frame the same at every split

const FIXTURES: [&str; 12] = [
    include_str!("../../tests/fixtures/anthropic_text_thinking.sse"),
    include_str!("../../tests/fixtures/anthropic_parallel_tools.sse"),
    include_str!("../../tests/fixtures/anthropic_edge.sse"),
    include_str!("../../tests/fixtures/deepseek_text_thinking.sse"),
    include_str!("../../tests/fixtures/deepseek_parallel_tools.sse"),
    include_str!("../../tests/fixtures/openai_chat_tools.sse"),
    include_str!("../../tests/fixtures/responses_text.sse"),
    include_str!("../../tests/fixtures/responses_reasoning_tools.sse"),
    include_str!("../../tests/fixtures/gemini_text_thinking.sse"),
    include_str!("../../tests/fixtures/gemini_parallel_calls.sse"),
    include_str!("../../tests/fixtures/vllm_reasoning_tools.sse"),
    include_str!("../../tests/fixtures/llamacpp_reasoning_tools.sse"),
];

#[test]
fn the_recorded_streams_of_every_wire_frame_the_same_at_every_two_way_split() {
    for (n, fixture) in FIXTURES.iter().enumerate() {
        for (ends, step) in [("\n", 1), ("\r\n", 5), ("\r", 5)] {
            let body = fixture.replace('\n', ends).into_bytes();
            let (frames, error) = assert_same_at_every_split(&body, step);
            assert!(
                error.is_none() && !frames.is_empty(),
                "fixture {n}: {error:?}"
            );
        }
    }
}

// ---- the framer gives memory back

#[test]
fn a_large_event_does_not_keep_its_memory_for_the_rest_of_the_stream() {
    let mut framer = Framer::default();
    let mut out = VecDeque::new();
    let mut body = b"data: ".to_vec();
    body.resize(8 * 1024 * 1024, b'x');
    body.extend_from_slice(b"\n\ndata: small\n\n");
    let mut peak = 0;
    for piece in body.chunks(64 * KIB) {
        framer.push(piece, &mut out).unwrap();
        peak = peak.max(framer.buf.capacity());
    }
    assert_eq!(out.len(), 2);
    assert!(
        framer.buf.capacity() <= KEEP_BYTES,
        "{} bytes held after the event",
        framer.buf.capacity()
    );
    assert!(
        peak >= 8 * 1024 * 1024,
        "the event was held while it was read"
    );
}

#[test]
fn the_buffer_does_not_double_past_the_bound() {
    let mut framer = Framer::default();
    let mut out = VecDeque::new();
    let mut body = b"data: ".to_vec();
    body.resize(MAX_EVENT_BYTES - 2, b'x');
    let mut peak = 0;
    for piece in body.chunks(64 * KIB) {
        framer.push(piece, &mut out).unwrap();
        peak = peak.max(framer.buf.capacity());
    }
    assert!(
        peak <= MAX_EVENT_BYTES + 64 * KIB,
        "{} MiB held for an event that may hold {} MiB",
        peak / (KIB * KIB),
        MAX_EVENT_BYTES / (KIB * KIB)
    );
}

#[test]
fn two_large_events_in_a_row_do_not_add_up() {
    let mut framer = Framer::default();
    let mut out = VecDeque::new();
    let mut event = b"data: ".to_vec();
    event.resize(12 * 1024 * 1024, b'x');
    event.extend_from_slice(b"\n\n");
    let body = [event.clone(), event].concat();
    let mut peak = 0;
    for piece in body.chunks(64 * KIB) {
        framer.push(piece, &mut out).unwrap();
        out.clear();
        peak = peak.max(framer.buf.capacity() + framer.pending.data.capacity());
    }
    // One event is held twice at most: as the unread line and as its data.
    assert!(peak <= 2 * MAX_EVENT_BYTES, "{} MiB", peak / (KIB * KIB));
    assert!(framer.buf.capacity() <= KEEP_BYTES);
}

// ---- the class of a framing failure

#[tokio::test]
async fn a_stream_over_the_bound_fails_with_an_error_that_is_not_retried() {
    let (first, _) = first_item(b"data: ", &[b'x'; 64 * KIB]).await;
    let Some(Err(err)) = first else {
        panic!("{first:?}")
    };
    assert!(matches!(err, ProviderError::Stream(_)), "{err:?}");
    assert!(!err.retryable(), "{err:?}");
}

#[tokio::test]
async fn a_byte_that_is_not_utf8_fails_with_an_error_that_is_not_retried() {
    let (first, _) = first_item(&[0xff], b"data: x\n\n").await;
    let Some(Err(err)) = first else {
        panic!("{first:?}")
    };
    assert!(matches!(err, ProviderError::Stream(_)), "{err:?}");
    assert!(!err.retryable(), "{err:?}");
}

#[tokio::test]
async fn a_transport_failure_of_the_body_stays_retryable() {
    let bytes = futures::stream::iter([Err::<Vec<u8>, _>("connection reset")]);
    let mut stream = events(bytes, Echo, CancellationToken::new(), None::<ApiKey>);
    let Some(Err(err)) = stream.next().await else {
        panic!("the body fails");
    };
    assert!(matches!(err, ProviderError::Transport { .. }), "{err:?}");
    assert!(err.retryable());
}
