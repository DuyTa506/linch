//! A response body that ends before the vendor's terminal event, over real
//! HTTP, for every adapter: the call fails with a retryable transport error
//! and never finishes as a normal answer. The socket closes cleanly after
//! the last byte sent, or the body is shorter than its `content-length`.

use futures::StreamExt;
use linch_core::{Message, Provider, ProviderError, ProviderEvent, ProviderRequest};
use tokio_util::sync::CancellationToken;

use crate::test_server::serve_once;
use crate::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    OpenAiChat, OpenAiChatOptions, OpenAiResponses, ResponsesOptions, Server, ServerOptions,
};

const SECRET: &str = "sk-cut-stream-test-secret";

fn key() -> ApiKey {
    ApiKey::new(SECRET).unwrap()
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

/// `sse` without its last event: the terminal one (`message_stop`,
/// `[DONE]`, `response.completed`, the chunk with `finishReason`).
fn without_last_event(sse: &str) -> String {
    let sse = sse.replace("\r\n", "\n");
    let events: Vec<&str> = sse.trim_end().split("\n\n").collect();
    let kept = &events[..events.len() - 1];
    format!("{}\n\n", kept.join("\n\n"))
}

struct Case {
    name: &'static str,
    sse: &'static str,
    model: &'static str,
    build: fn(String) -> Box<dyn Provider>,
}

fn cases() -> Vec<Case> {
    vec![
        Case {
            name: "anthropic",
            sse: include_str!("../tests/fixtures/anthropic_text_thinking.sse"),
            model: "claude-sonnet-4-6",
            build: |url| {
                Box::new(
                    Anthropic::new(AnthropicOptions {
                        api_key: key(),
                        base_url: url,
                        native_catalog_changes: false,
                        request: crate::RequestOptions::default(),
                    })
                    .unwrap(),
                )
            },
        },
        Case {
            name: "openai-chat",
            sse: include_str!("../tests/fixtures/openai_chat_tools.sse"),
            model: "gpt-5.5",
            build: |url| {
                Box::new(
                    OpenAiChat::new(OpenAiChatOptions {
                        api_key: key(),
                        base_url: url + "/v1",
                        context_window: None,
                        request: crate::RequestOptions::default(),
                        chat: crate::ChatOptions::default(),
                    })
                    .unwrap(),
                )
            },
        },
        Case {
            name: "openai-responses",
            sse: include_str!("../tests/fixtures/responses_text.sse"),
            model: "gpt-5.5",
            build: |url| {
                Box::new(
                    OpenAiResponses::new(ResponsesOptions {
                        base_url: url + "/v1",
                        ..ResponsesOptions::new(key())
                    })
                    .unwrap(),
                )
            },
        },
        Case {
            name: "gemini",
            sse: include_str!("../tests/fixtures/gemini_text_thinking.sse"),
            model: "models/gemini-3.8-flash",
            build: |url| {
                Box::new(
                    Gemini::new(GeminiOptions {
                        api_key: key(),
                        base_url: url + "/",
                        request: crate::RequestOptions::default(),
                    })
                    .unwrap(),
                )
            },
        },
        Case {
            name: "deepseek",
            sse: include_str!("../tests/fixtures/deepseek_text_thinking.sse"),
            model: "deepseek-v4-flash",
            build: |url| {
                Box::new(
                    DeepSeek::new(DeepSeekOptions {
                        api_key: key(),
                        base_url: url,
                        context_window: None,
                        request: crate::RequestOptions::default(),
                        chat: crate::ChatOptions::default(),
                    })
                    .unwrap(),
                )
            },
        },
        Case {
            name: "vllm",
            sse: include_str!("../tests/fixtures/vllm_reasoning_tools.sse"),
            model: "qwen",
            build: |url| {
                let options = ServerOptions {
                    context_window: Some(8_000),
                    ..ServerOptions::new(Server::Vllm, url + "/v1")
                };
                Box::new(OpenAiChat::server(options).unwrap())
            },
        },
    ]
}

/// The call's items: every event, then the error that ended it.
async fn run(case: &Case, response: String) -> (Vec<ProviderEvent>, ProviderError) {
    let (url, server) = serve_once(response);
    let provider = (case.build)(url);
    let mut stream = provider
        .stream(request(case.model), CancellationToken::new())
        .await
        .unwrap_or_else(|err| panic!("{}: the call failed to start: {err}", case.name));
    let mut events = Vec::new();
    let error = loop {
        match stream.next().await {
            Some(Ok(event)) => events.push(event),
            Some(Err(err)) => break err,
            None => panic!(
                "{}: the stream ended with no error and no message_end: {} events",
                case.name,
                events.len()
            ),
        }
    };
    drop(stream);
    let _ = server.join();
    (events, error)
}

fn assert_failed_not_finished(case: &Case, events: &[ProviderEvent], error: &ProviderError) {
    assert!(
        !events
            .iter()
            .any(|event| matches!(event, ProviderEvent::MessageEnd(_))),
        "{}: a cut stream ended as an answer",
        case.name
    );
    assert!(
        matches!(error, ProviderError::Transport { .. }) && error.retryable(),
        "{}: {error:?}",
        case.name
    );
}

#[tokio::test]
async fn a_body_that_closes_before_its_terminal_event_is_a_retryable_failure() {
    for case in cases() {
        let sse = without_last_event(case.sse);
        let response = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n{sse}"
        );
        let (events, error) = run(&case, response).await;
        assert!(
            !events.is_empty(),
            "{}: nothing streamed first ({error:?})",
            case.name
        );
        assert_failed_not_finished(&case, &events, &error);
    }
}

#[tokio::test]
async fn a_body_shorter_than_its_content_length_is_a_retryable_failure() {
    for case in cases() {
        let sse = without_last_event(case.sse);
        let response = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{sse}",
            sse.len() + 4096
        );
        let (events, error) = run(&case, response).await;
        assert_failed_not_finished(&case, &events, &error);
    }
}

#[tokio::test]
async fn a_body_cut_in_the_middle_of_an_event_is_a_retryable_failure() {
    for case in cases() {
        let cut = case.sse.len() * 6 / 10;
        let sse = &case.sse[..case.sse.floor_char_boundary(cut)];
        let response = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n{sse}"
        );
        let (events, error) = run(&case, response).await;
        assert_failed_not_finished(&case, &events, &error);
    }
}
