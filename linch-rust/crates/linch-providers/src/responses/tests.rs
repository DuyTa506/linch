use std::time::Duration;

use linch_core::provider::{OutputSchema, Thinking};
use linch_core::{
    ContentBlock, ImageBlock, ImageSource, JsonObject, Message, ProviderError, ProviderRequest,
    ReasoningEffort, Role, StopReason, SystemBlock, TextBlock, ThinkingBlock, ToolChoice,
    ToolResultBlock, ToolResultPart, ToolUseBlock,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use super::*;
use crate::openai::test_support::{
    assemble_ok, error_response, request, serve_once, sse_response, weather_tool,
};
use crate::test_server::{assert_redirect_refused, redirect_response};

const SECRET: &str = "sk-unit-test-responses-8e7d";
const MODEL: &str = "gpt-5.5";
const TOOLS: &str = include_str!("../../tests/fixtures/responses_reasoning_tools.sse");

fn options() -> ResponsesOptions {
    ResponsesOptions::new(ApiKey::new(SECRET).unwrap())
}

fn body(request: &ProviderRequest) -> Value {
    payload::build_payload(request, &options()).unwrap()
}

fn invalid(request: &ProviderRequest) -> ProviderError {
    payload::build_payload(request, &options()).unwrap_err()
}

#[test]
fn base_body_has_input_and_no_server_state_flags() {
    assert_eq!(
        body(&request(MODEL)),
        json!({
            "model": MODEL,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
            "stream": true,
        })
    );
}

#[test]
fn system_blocks_become_instructions() {
    let system = vec![
        SystemBlock {
            text: "a".into(),
            cacheable: true,
        },
        SystemBlock {
            text: "b".into(),
            cacheable: false,
        },
    ];
    let body = body(&ProviderRequest {
        system,
        ..request(MODEL)
    });
    assert_eq!(body["instructions"], "a\n\nb");
}

#[test]
fn tools_are_non_strict_functions_and_tool_choice_uses_the_flat_shape() {
    for (choice, wire) in [
        (ToolChoice::Auto {}, json!("auto")),
        (ToolChoice::Disabled {}, json!("none")),
        (ToolChoice::Required {}, json!("required")),
        (
            ToolChoice::Tool {
                name: "get_weather".into(),
            },
            json!({"type": "function", "name": "get_weather"}),
        ),
    ] {
        let body = body(&ProviderRequest {
            tools: vec![weather_tool()],
            tool_choice: Some(choice),
            ..request(MODEL)
        });
        assert_eq!(
            body["tools"],
            json!([{"type": "function", "name": "get_weather", "description": "Weather.",
                    "parameters": {"type": "object"}, "strict": false}])
        );
        assert_eq!(body["tool_choice"], wire);
    }
}

#[test]
fn limits_effort_and_summary_are_forwarded_and_thinking_is_ignored() {
    let request = ProviderRequest {
        max_output_tokens: Some(2048),
        temperature: Some(1.0),
        effort: Some(ReasoningEffort::Max),
        thinking: Some(Thinking::Disabled {}),
        ..request(MODEL)
    };
    let options = ResponsesOptions {
        reasoning_summary: Some(ReasoningSummary::Detailed),
        ..options()
    };
    let body = payload::build_payload(&request, &options).unwrap();
    assert_eq!(body["max_output_tokens"], 2048);
    assert_eq!(body["temperature"], 1.0);
    assert_eq!(
        body["reasoning"],
        json!({"effort": "max", "summary": "detailed"})
    );
}

#[test]
fn store_off_asks_for_encrypted_reasoning() {
    let options = ResponsesOptions {
        store: false,
        ..options()
    };
    let body = payload::build_payload(&request(MODEL), &options).unwrap();
    assert_eq!(body["store"], false);
    assert_eq!(body["include"], json!(["reasoning.encrypted_content"]));
}

#[test]
fn stop_sequences_and_output_schema_are_rejected() {
    let stop = ProviderRequest {
        stop_sequences: vec!["END".into()],
        ..request(MODEL)
    };
    assert!(matches!(
        invalid(&stop),
        ProviderError::InvalidRequest { .. }
    ));
    let schema = ProviderRequest {
        output_schema: Some(OutputSchema {
            name: "s".into(),
            schema: JsonObject::new(),
            strict: true,
            description: None,
        }),
        ..request(MODEL)
    };
    assert!(matches!(
        invalid(&schema),
        ProviderError::InvalidRequest { .. }
    ));
}

fn call(id: &str) -> ContentBlock {
    ContentBlock::ToolUse(ToolUseBlock {
        id: id.into(),
        name: "get_weather".into(),
        input: json!({"city": "Hue"}).as_object().unwrap().clone(),
    })
}

#[test]
fn a_foreign_assistant_turn_is_rebuilt_without_its_reasoning() {
    let turn = Message::new(
        Role::Assistant,
        vec![
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "other vendor".into(),
                signature: Some("sig".into()),
            }),
            ContentBlock::text("Checking."),
            call("c1"),
        ],
    );
    let body = body(&ProviderRequest {
        messages: vec![turn],
        ..request(MODEL)
    });
    assert_eq!(
        body["input"],
        json!([
            {"type": "message", "role": "assistant", "content": "Checking."},
            {"type": "function_call", "call_id": "c1", "name": "get_weather",
             "arguments": "{\"city\":\"Hue\"}"},
        ])
    );
}

#[test]
fn a_stored_turn_is_replayed_verbatim_and_not_rebuilt() {
    let items = json!([{"id": "rs_1", "type": "reasoning", "summary": []},
                       {"id": "fc_1", "type": "function_call", "call_id": "c1",
                        "name": "get_weather", "arguments": "{}", "status": "completed"}]);
    let mut turn = Message::new(
        Role::Assistant,
        vec![ContentBlock::text("ignored"), call("c1")],
    );
    turn.provider_metadata = json!({stream::METADATA_KEY: {"response_id": "r", "output": items}})
        .as_object()
        .cloned();
    let body = body(&ProviderRequest {
        messages: vec![turn],
        ..request(MODEL)
    });
    assert_eq!(body["input"], items);
}

#[test]
fn tool_outputs_come_before_user_parts_and_images_use_input_image() {
    let image = ImageBlock {
        source: ImageSource::Base64 {
            media_type: "image/png".into(),
            data: "AAAA".into(),
        },
    };
    let with_image = ToolResultBlock {
        tool_use_id: "c2".into(),
        content: vec![
            ToolResultPart::Text(TextBlock { text: "see".into() }),
            ToolResultPart::Image(image.clone()),
        ],
        is_error: false,
    };
    let turn = Message::new(
        Role::User,
        vec![
            ContentBlock::text("and then?"),
            ContentBlock::ToolResult(ToolResultBlock::text("c1", "31C", false)),
            ContentBlock::ToolResult(with_image),
        ],
    );
    let input = body(&ProviderRequest {
        messages: vec![turn],
        ..request(MODEL)
    })["input"]
        .clone();
    let picture = json!({"type": "input_image", "image_url": "data:image/png;base64,AAAA",
                         "detail": "auto"});
    assert_eq!(
        input,
        json!([
            {"type": "function_call_output", "call_id": "c1", "output": "31C"},
            {"type": "function_call_output", "call_id": "c2",
             "output": [{"type": "input_text", "text": "see"}, picture]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "and then?"}]},
        ])
    );
}

#[test]
fn misplaced_blocks_are_rejected() {
    let user_call = Message::new(Role::User, vec![call("c1")]);
    let err = invalid(&ProviderRequest {
        messages: vec![user_call],
        ..request(MODEL)
    });
    assert!(err.to_string().contains("tool_use"), "{err}");
}

fn provider(base_url: String) -> OpenAiResponses {
    OpenAiResponses::new(ResponsesOptions {
        base_url,
        ..options()
    })
    .unwrap()
}

#[test]
fn defaults_and_debug_output_never_show_the_key() {
    let options = options();
    assert!(options.store && options.reasoning_summary.is_none());
    let provider = OpenAiResponses::new(options.clone()).unwrap();
    assert_eq!(provider.id(), "openai-responses");
    for text in [format!("{options:?}"), format!("{provider:?}")] {
        assert!(!text.contains(SECRET), "{text}");
    }
}

#[tokio::test]
async fn http_round_trip_and_replay_of_the_assembled_turn() {
    let (url, server) = serve_once(sse_response(TOOLS));
    let provider = provider(url + "/v1");
    let turn = assemble_ok(
        provider
            .stream(request(MODEL), CancellationToken::new())
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let raw = server.join().unwrap();
    assert!(raw.starts_with("POST /v1/responses HTTP/1.1"), "{raw}");
    assert!(
        raw.contains(&format!("authorization: Bearer {SECRET}")),
        "{raw}"
    );
    // The next request sends the reasoning item and both calls back.
    let next = body(&ProviderRequest {
        messages: vec![Message::user_text("weather?"), turn.message],
        ..request(MODEL)
    });
    let input = next["input"].as_array().unwrap();
    assert_eq!(input[1]["type"], "reasoning");
    assert_eq!(input[1]["encrypted_content"], "gAAAAB-enc");
    assert_eq!(input[3]["call_id"], "call_now");
}

#[tokio::test]
async fn http_429_keeps_retry_after_and_never_the_key() {
    let body = format!(
        r#"{{"error":{{"message":"slow down, Bearer {SECRET}","type":"rate_limit_error","param":null,"code":"slow_down"}}}}"#
    );
    let (url, server) = serve_once(error_response(
        "429 Too Many Requests",
        "retry-after: 2\r\n",
        &body,
    ));
    let err = provider(url)
        .stream(request(MODEL), CancellationToken::new())
        .await
        .err()
        .expect("429 fails the call");
    server.join().unwrap();
    assert!(
        matches!(&err, ProviderError::Status { status: 429, retry_after: Some(delay), .. }
            if *delay == Duration::from_secs(2)),
        "{err:?}"
    );
    assert!(!format!("{err} {err:?}").contains(SECRET), "{err:?}");
}

#[tokio::test]
async fn a_redirect_is_refused_and_never_followed() {
    let (url, server) = serve_once(redirect_response());
    let err = provider(url)
        .stream(request(MODEL), CancellationToken::new())
        .await
        .err()
        .expect("a redirect fails the call");
    server.join().unwrap();
    assert_redirect_refused(&err);
}
