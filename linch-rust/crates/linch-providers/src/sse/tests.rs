//! The SSE loop: framing grammar, the bound on one event, and the cut on
//! in-stream error text.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use futures::{Stream, StreamExt};
use linch_core::{Message, Provider, ProviderError, ProviderEvent, ProviderRequest};
use tokio_util::sync::CancellationToken;

use super::{Decoder, MAX_ERROR_CHARS, MAX_EVENT_BYTES, events};
use crate::openai_compat::stream::{ReasoningField, Translator};
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

#[tokio::test]
async fn a_bound_failure_is_a_transport_error() {
    let (first, _) = first_item(b"data: ", &[b'x'; 64 * KIB]).await;
    assert!(
        matches!(first, Some(Err(ProviderError::Transport { .. }))),
        "{first:?}"
    );
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
