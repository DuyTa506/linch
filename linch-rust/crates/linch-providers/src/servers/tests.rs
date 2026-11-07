use linch_core::provider::Thinking;
use linch_core::{
    ContentBlock, Message, Provider, ProviderError, ProviderRequest, ReasoningEffort, Role,
    StopReason, ThinkingBlock, ToolChoice, Usage,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use super::*;
use crate::OpenAiChat;
use crate::openai::profile_payload;
use crate::openai::test_support::{assemble_ok, bytes, request, serve_once, sse_response};
use crate::openai_compat::stream::{self, Translator};

mod fixtures;

const ALL: [Server; 3] = [Server::Vllm, Server::SgLang, Server::LlamaCpp];

fn body(server: Server, request: &ProviderRequest) -> Result<Value, ProviderError> {
    let options = ServerOptions::new(server, "http://unused/v1");
    profile_payload(server.profile(), &options.thinking_off_kwargs, request)
}

/// Run `sse` through `server`'s translator and assemble it.
async fn assemble_fixture(server: Server, sse: &str) -> linch_core::AssistantAssembly {
    let profile = server.profile();
    let translator = Translator::new("requested".into(), profile.reasoning_field, profile.usage);
    assemble_ok(stream::events(
        bytes(sse, 9),
        translator,
        CancellationToken::new(),
        None,
    ))
    .await
}

#[test]
fn ids_and_defaults_are_per_server() {
    let ids: Vec<_> = ALL.iter().map(|server| server.profile().id).collect();
    assert_eq!(ids, ["vllm", "sglang", "llamacpp"]);
    for server in ALL {
        let options = ServerOptions::new(server, "http://localhost/v1");
        assert!(options.api_key.is_none());
        assert_eq!(options.thinking_off_kwargs["enable_thinking"], false);
    }
    let sglang = ServerOptions::new(Server::SgLang, "x").thinking_off_kwargs;
    assert_eq!(sglang["thinking"], false);
}

#[test]
fn every_server_sends_usage_in_the_stream() {
    for server in ALL {
        let body = body(server, &request("m")).unwrap();
        assert_eq!(body["stream_options"], json!({"include_usage": true}));
    }
}

#[test]
fn earlier_reasoning_goes_back_as_reasoning_content() {
    let turn = Message::new(
        Role::Assistant,
        vec![
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "think".into(),
                signature: None,
            }),
            ContentBlock::text("done"),
        ],
    );
    for server in ALL {
        let body = body(
            server,
            &ProviderRequest {
                messages: vec![Message::user_text("hi"), turn.clone()],
                ..request("m")
            },
        )
        .unwrap();
        assert_eq!(
            body["messages"][1]["reasoning_content"], "think",
            "{server:?}"
        );
    }
}

#[test]
fn output_limit_field_follows_each_server() {
    for (server, field) in [
        (Server::Vllm, "max_completion_tokens"),
        (Server::SgLang, "max_completion_tokens"),
        (Server::LlamaCpp, "max_tokens"),
    ] {
        let body = body(
            server,
            &ProviderRequest {
                max_output_tokens: Some(100),
                ..request("m")
            },
        )
        .unwrap();
        assert_eq!(body[field], 100, "{server:?}");
        assert_eq!(body.as_object().unwrap().len(), 5, "{server:?} {body}");
    }
}

#[test]
fn thinking_off_sends_the_template_switch_and_drops_effort() {
    for server in ALL {
        let off = body(
            server,
            &ProviderRequest {
                thinking: Some(Thinking::Disabled {}),
                effort: Some(ReasoningEffort::High),
                ..request("m")
            },
        )
        .unwrap();
        let expected = ServerOptions::new(server, "x").thinking_off_kwargs;
        assert_eq!(off["chat_template_kwargs"], Value::Object(expected));
        assert!(off.get("reasoning_effort").is_none(), "{server:?}");
        let on = body(
            server,
            &ProviderRequest {
                thinking: Some(Thinking::Adaptive { display: None }),
                effort: Some(ReasoningEffort::High),
                ..request("m")
            },
        )
        .unwrap();
        assert!(on.get("chat_template_kwargs").is_none(), "{server:?}");
        assert_eq!(on["reasoning_effort"], "high", "{server:?}");
    }
}

#[test]
fn empty_thinking_off_kwargs_send_nothing() {
    let profile = Server::Vllm.profile();
    let off = ProviderRequest {
        thinking: Some(Thinking::Disabled {}),
        ..request("m")
    };
    let body = profile_payload(profile, &JsonObject::new(), &off).unwrap();
    assert!(body.get("chat_template_kwargs").is_none(), "{body}");
}

#[test]
fn tool_choice_support_is_declared_per_server() {
    let named = ToolChoice::Tool {
        name: "get_weather".into(),
    };
    for (server, named_ok) in [
        (Server::Vllm, true),
        (Server::SgLang, true),
        (Server::LlamaCpp, false),
    ] {
        for choice in [
            ToolChoice::Auto {},
            ToolChoice::Disabled {},
            ToolChoice::Required {},
        ] {
            let request = ProviderRequest {
                tool_choice: Some(choice),
                ..request("m")
            };
            assert!(body(server, &request).is_ok(), "{server:?}");
        }
        let forced = body(
            server,
            &ProviderRequest {
                tool_choice: Some(named.clone()),
                ..request("m")
            },
        );
        match forced {
            Ok(body) => {
                assert!(named_ok && body["tool_choice"]["function"]["name"] == "get_weather")
            }
            Err(err) => {
                assert!(!named_ok, "{server:?}");
                assert!(matches!(err, ProviderError::InvalidRequest { .. }), "{err}");
            }
        }
    }
}

#[test]
fn cached_prefix_is_split_from_input() {
    let wire: WireUsage = serde_json::from_value(json!({
        "prompt_tokens": 100, "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 64}
    }))
    .unwrap();
    let expected = Usage {
        input_tokens: 36,
        output_tokens: 7,
        cache_read_tokens: 64,
        cache_creation_tokens: 0,
    };
    for server in ALL {
        assert_eq!((server.profile().usage)(&wire), expected, "{server:?}");
    }
    // Without a cache report (vLLM and SGLang need a launch flag) the
    // whole prompt reads as uncached.
    let bare: WireUsage = serde_json::from_value(json!({"prompt_tokens": 10})).unwrap();
    assert_eq!(prompt_minus_cached(&bare).input_tokens, 10);
}

#[tokio::test]
async fn keyless_server_sends_no_auth_header() {
    let sse = include_str!("../../tests/fixtures/sglang_reasoning_tools.sse");
    let (url, server) = serve_once(sse_response(sse));
    let provider = OpenAiChat::server(ServerOptions::new(Server::SgLang, url + "/v1")).unwrap();
    assert_eq!(provider.id(), "sglang");
    let turn = assemble_ok(
        provider
            .stream(request("Qwen/Qwen3-8B"), CancellationToken::new())
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let raw = server.join().unwrap();
    assert!(
        raw.starts_with("POST /v1/chat/completions HTTP/1.1"),
        "{raw}"
    );
    assert!(!raw.to_ascii_lowercase().contains("authorization"), "{raw}");
}

#[tokio::test]
async fn server_key_is_sent_as_bearer_and_scrubbed_from_errors() {
    const KEY: &str = "local-server-key-77";
    let body = format!(
        r#"{{"error":{{"code":401,"message":"Invalid API Key {KEY}","type":"authentication_error"}}}}"#
    );
    let response = crate::openai::test_support::error_response("401 Unauthorized", "", &body);
    let (url, server) = serve_once(response);
    let options = ServerOptions {
        api_key: ApiKey::new(KEY),
        ..ServerOptions::new(Server::LlamaCpp, url)
    };
    let err = OpenAiChat::server(options)
        .unwrap()
        .stream(request("m"), CancellationToken::new())
        .await
        .err()
        .unwrap();
    let raw = server.join().unwrap();
    assert!(
        raw.contains(&format!("authorization: Bearer {KEY}")),
        "{raw}"
    );
    assert!(
        matches!(&err, ProviderError::Status { status: 401, message, .. }
            if message == "authentication_error: Invalid API Key [redacted]"),
        "{err:?}"
    );
}
