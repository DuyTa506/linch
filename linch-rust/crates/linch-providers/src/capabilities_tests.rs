//! Each adapter's declared capabilities against what the adapter does:
//! every rule it states is checked against the request body it builds or
//! the refusal it gives, so a declaration cannot drift from the code.

use linch_core::provider::{OutputSchema, Thinking, ToolChoice};
use linch_core::{
    ContentBlock, ContextSource, ImageBlock, ImageSource, JsonValue, Message, OutputSchemaMode,
    PromptCache, Provider, ProviderError, ProviderRequest, ReasoningEffort, Role, Support,
    ThinkingState,
};
use serde_json::json;

use crate::openai::test_support::{request, weather_tool};
use crate::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    OpenAiChat, OpenAiChatOptions, OpenAiResponses, ReasoningSummary, ResponsesOptions, Server,
    ServerOptions,
};

type Build = Box<dyn Fn(&ProviderRequest) -> Result<JsonValue, ProviderError>>;

struct Adapter {
    name: &'static str,
    provider: Box<dyn Provider>,
    build: Build,
    model: &'static str,
}

fn key() -> ApiKey {
    ApiKey::new("sk-capabilities-test").unwrap()
}

fn chat(name: &'static str, options: ServerOptions, model: &'static str) -> Adapter {
    let chat = std::sync::Arc::new(OpenAiChat::server(options).unwrap());
    let build = std::sync::Arc::clone(&chat);
    Adapter {
        name,
        provider: Box::new((*chat).clone()),
        build: Box::new(move |request| build.body(request)),
        model,
    }
}

fn adapters() -> Vec<Adapter> {
    let anthropic = Anthropic::new(AnthropicOptions::new(key())).unwrap();
    let gemini = Gemini::new(GeminiOptions::new(key())).unwrap();
    let deepseek = DeepSeek::new(DeepSeekOptions::new(key())).unwrap();
    let options = ResponsesOptions::new(key());
    let responses = OpenAiResponses::new(options.clone()).unwrap();
    let mut summarized = ResponsesOptions::new(key());
    summarized.reasoning_summary = Some(ReasoningSummary::Auto);
    let summarizing = OpenAiResponses::new(summarized.clone()).unwrap();
    let openai = std::sync::Arc::new(OpenAiChat::new(OpenAiChatOptions::new(key())).unwrap());
    let openai_build = std::sync::Arc::clone(&openai);
    let server = |kind| ServerOptions::new(kind, "http://127.0.0.1:9/v1");
    vec![
        Adapter {
            name: "anthropic",
            provider: Box::new(anthropic),
            build: Box::new(|request| crate::anthropic::payload::build(request, false)),
            model: "claude-sonnet-4-6",
        },
        Adapter {
            name: "gemini",
            provider: Box::new(gemini),
            build: Box::new(crate::gemini::payload::build),
            model: "gemini-3-pro",
        },
        Adapter {
            name: "deepseek",
            provider: Box::new(deepseek),
            build: Box::new(crate::deepseek::build_payload),
            model: "deepseek-flash",
        },
        Adapter {
            name: "openai-responses",
            provider: Box::new(responses),
            build: Box::new(move |request| {
                crate::responses::payload::build_payload(request, &options)
            }),
            model: "gpt-5",
        },
        Adapter {
            name: "openai-responses with a summary",
            provider: Box::new(summarizing),
            build: Box::new(move |request| {
                crate::responses::payload::build_payload(request, &summarized)
            }),
            model: "gpt-5",
        },
        Adapter {
            name: "openai-chat",
            provider: Box::new((*openai).clone()),
            build: Box::new(move |request| openai_build.body(request)),
            model: "gpt-5.5",
        },
        chat("vllm", server(Server::Vllm), "qwen"),
        chat("sglang", server(Server::SgLang), "qwen"),
        chat("llamacpp", server(Server::LlamaCpp), "gguf"),
    ]
}

fn base(model: &str) -> ProviderRequest {
    ProviderRequest {
        tools: vec![weather_tool()],
        max_output_tokens: Some(4096),
        ..request(model)
    }
}

fn with(model: &str, change: impl FnOnce(&mut ProviderRequest)) -> ProviderRequest {
    let mut request = base(model);
    change(&mut request);
    request
}

fn state(thinking: Option<&Thinking>) -> Option<ThinkingState> {
    match thinking {
        None => Some(ThinkingState::Unset),
        Some(Thinking::Disabled {}) => None,
        Some(Thinking::Adaptive { .. }) => Some(ThinkingState::Adaptive),
        Some(Thinking::Enabled {
            budget_tokens: None,
            ..
        }) => Some(ThinkingState::Adaptive),
        Some(Thinking::Enabled {
            budget_tokens: Some(_),
            ..
        }) => Some(ThinkingState::Budgeted),
    }
}

fn thinkings() -> Vec<Option<Thinking>> {
    vec![
        None,
        Some(Thinking::Disabled {}),
        Some(Thinking::Adaptive { display: None }),
        Some(Thinking::Enabled {
            budget_tokens: None,
            display: None,
        }),
        Some(Thinking::Enabled {
            budget_tokens: Some(1024),
            display: None,
        }),
    ]
}

#[test]
fn every_adapter_declares_itself_and_its_base_request_builds() {
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model);
        assert!(caps.declared, "{} declares nothing", adapter.name);
        assert_eq!(caps.model, adapter.model);
        assert_eq!(caps.provider, adapter.provider.id(), "{}", adapter.name);
        let built = (adapter.build)(&base(adapter.model));
        assert!(built.is_ok(), "{}: {built:?}", adapter.name);
    }
}

#[test]
fn the_tool_choice_rules_are_the_refusals_and_the_payload_builds() {
    let choices = [
        ToolChoice::Auto {},
        ToolChoice::Disabled {},
        ToolChoice::Required {},
        ToolChoice::Tool {
            name: "get_weather".into(),
        },
    ];
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model).tool_choice;
        for choice in &choices {
            for thinking in thinkings() {
                let context = format!("{} {choice:?} {thinking:?}", adapter.name);
                let refused = (adapter.provider)
                    .tool_choice_refusal(choice, thinking.as_ref())
                    .is_some();
                let request = with(adapter.model, |r| {
                    r.tool_choice = Some(choice.clone());
                    r.thinking = thinking.clone();
                });
                let builds = (adapter.build)(&request).is_ok();
                assert_eq!(!refused, builds, "{context}: refusal and payload differ");
                let forced = matches!(choice, ToolChoice::Required {} | ToolChoice::Tool { .. });
                let declared = match choice {
                    ToolChoice::Auto {} => caps.auto,
                    ToolChoice::Disabled {} => caps.none,
                    ToolChoice::Required {} => caps.required,
                    ToolChoice::Tool { .. } => caps.named,
                } && !(forced
                    && state(thinking.as_ref())
                        .is_some_and(|state| caps.forced_refused_with.contains(&state)));
                assert_eq!(declared, builds, "{context}: the declaration differs");
            }
        }
    }
}

#[test]
fn the_output_schema_rule_is_what_the_payload_does() {
    let schema = OutputSchema {
        name: "answer".into(),
        schema: json!({"type": "object", "additionalProperties": false})
            .as_object()
            .unwrap()
            .clone(),
        strict: true,
        description: None,
    };
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model);
        let request = with(adapter.model, |r| r.output_schema = Some(schema.clone()));
        let builds = (adapter.build)(&request).is_ok();
        let native = caps.output_schema == OutputSchemaMode::Native;
        assert_eq!(native, builds, "{}", adapter.name);
        assert_eq!(
            native,
            adapter.provider.native_output_schema(),
            "{}",
            adapter.name
        );
    }
}

#[test]
fn the_stop_sequence_rule_is_what_the_payload_does() {
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model);
        let request = with(adapter.model, |r| r.stop_sequences = vec!["END".into()]);
        let builds = (adapter.build)(&request).is_ok();
        assert_eq!(caps.stop_sequences, builds, "{}", adapter.name);
    }
}

#[test]
fn the_thinking_controls_change_the_payload_exactly_when_declared() {
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model).thinking;
        let body = |change: &dyn Fn(&mut ProviderRequest)| {
            (adapter.build)(&with(adapter.model, change)).unwrap()
        };
        let plain = body(&|_| {});
        let disabled = body(&|r| r.thinking = Some(Thinking::Disabled {}));
        assert_eq!(caps.disable, disabled != plain, "{} disable", adapter.name);
        let effort = body(&|r| r.effort = Some(ReasoningEffort::Medium));
        assert_eq!(caps.effort, effort != plain, "{} effort", adapter.name);
        let enabled = |budget| {
            body(&move |r| {
                r.thinking = Some(Thinking::Enabled {
                    budget_tokens: budget,
                    display: None,
                });
            })
        };
        assert_eq!(
            caps.budget,
            enabled(Some(2048)) != enabled(None),
            "{} budget",
            adapter.name
        );
    }
}

#[test]
fn explicit_caching_is_the_only_one_a_request_controls() {
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model);
        let plain = (adapter.build)(&base(adapter.model)).unwrap();
        let cached = (adapter.build)(&with(adapter.model, |r| {
            r.cache_control = Some(linch_core::CacheControl::default());
        }))
        .unwrap();
        assert_eq!(
            caps.prompt_cache == PromptCache::Explicit,
            cached != plain,
            "{}",
            adapter.name
        );
        assert_ne!(caps.prompt_cache, PromptCache::None, "{}", adapter.name);
    }
}

#[test]
fn an_adapter_that_takes_images_sends_them() {
    let image = ContentBlock::Image(ImageBlock {
        source: ImageSource::Base64 {
            media_type: "image/png".into(),
            data: "AAAA".into(),
        },
    });
    for adapter in adapters() {
        let caps = adapter.provider.capabilities(adapter.model);
        assert!(
            matches!(caps.vision, Support::Supported | Support::ModelDependent),
            "{}",
            adapter.name
        );
        let request = with(adapter.model, |r| {
            r.messages
                .push(Message::new(Role::User, vec![image.clone()]));
        });
        let body = (adapter.build)(&request).unwrap().to_string();
        let sent = [
            "\"type\":\"image\"",
            "inlineData",
            "input_image",
            "image_url",
        ];
        assert!(
            sent.iter().any(|part| body.contains(part)),
            "{}: {body}",
            adapter.name
        );
    }
}

#[test]
fn a_window_nobody_told_is_a_default_not_a_known_value() {
    let by_name = |name: &str| adapters().into_iter().find(|a| a.name == name).unwrap();
    // Tables: a listed model is a table hit, any other is the default.
    let responses = by_name("openai-responses");
    let known = responses.provider.capabilities("gpt-5");
    assert_eq!(
        (known.context_window, known.context_source),
        (400_000, ContextSource::Table)
    );
    let other = responses.provider.capabilities("gpt-next");
    assert_eq!(
        (other.context_window, other.context_source),
        (128_000, ContextSource::Default)
    );
    let gemini = by_name("gemini").provider.capabilities("gemini-future");
    assert_eq!(gemini.context_source, ContextSource::Default);
    // A server not yet asked: the default, not the server's word.
    let vllm = by_name("vllm").provider.capabilities("qwen");
    assert_eq!(
        (vllm.context_window, vllm.context_source),
        (128_000, ContextSource::Default)
    );
    let deepseek = by_name("deepseek").provider.capabilities("deepseek-flash");
    assert_eq!(deepseek.context_source, ContextSource::Default);
}

#[test]
fn the_summary_option_decides_whether_a_responses_stream_has_reasoning_text() {
    let by_name = |name: &str| adapters().into_iter().find(|a| a.name == name).unwrap();
    let plain = by_name("openai-responses").provider.capabilities("gpt-5");
    let summarizing = by_name("openai-responses with a summary")
        .provider
        .capabilities("gpt-5");
    assert!(!plain.thinking.reasoning_text);
    assert!(summarizing.thinking.reasoning_text);
}

#[test]
fn the_tool_choice_rules_of_the_providers_that_have_one() {
    let by_name = |name: &str| adapters().into_iter().find(|a| a.name == name).unwrap();
    let anthropic = by_name("anthropic").provider.capabilities("claude");
    assert_eq!(
        anthropic.tool_choice.forced_refused_with,
        [ThinkingState::Budgeted]
    );
    let deepseek = by_name("deepseek").provider.capabilities("deepseek-flash");
    assert_eq!(
        deepseek.tool_choice.forced_refused_with,
        [
            ThinkingState::Unset,
            ThinkingState::Adaptive,
            ThinkingState::Budgeted
        ]
    );
    let llamacpp = by_name("llamacpp").provider.capabilities("gguf");
    assert!(llamacpp.tool_choice.required && !llamacpp.tool_choice.named);
    for name in [
        "gemini",
        "openai-chat",
        "openai-responses",
        "vllm",
        "sglang",
    ] {
        let caps = by_name(name).provider.capabilities("m").tool_choice;
        assert!(
            caps.required && caps.named && caps.forced_refused_with.is_empty(),
            "{name}"
        );
    }
}
