//! One test per payload rule.

use linch_core::provider::ThinkingDisplay;
use linch_core::{
    ContentBlock, ImageBlock, ImageSource, JsonObject, Message, OutputSchema, ProviderError,
    ProviderRequest, ReasoningEffort, Role, SystemBlock, TextBlock, Thinking, ThinkingBlock,
    ToolChoice, ToolDefinition, ToolResultBlock, ToolResultPart, ToolUseBlock,
};
use serde_json::{Value, json};

use super::request;
use crate::gemini::{PARTS_KEY, payload};

fn build(request: &ProviderRequest) -> Value {
    payload::build(request).unwrap()
}

fn invalid(request: &ProviderRequest) -> bool {
    matches!(
        payload::build(request),
        Err(ProviderError::InvalidRequest { .. })
    )
}

fn weather_tool() -> ToolDefinition {
    ToolDefinition {
        name: "get_weather".into(),
        description: "Weather.".into(),
        input_schema: json!({"type": "object"}).as_object().unwrap().clone(),
    }
}

fn call(id: &str, city: &str) -> ContentBlock {
    ContentBlock::ToolUse(ToolUseBlock {
        id: id.into(),
        name: "get_weather".into(),
        input: json!({"city": city}).as_object().unwrap().clone(),
    })
}

fn with_messages(messages: Vec<Message>) -> ProviderRequest {
    ProviderRequest {
        messages,
        ..request()
    }
}

#[test]
fn base_body_has_only_contents() {
    assert_eq!(
        build(&request()),
        json!({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    );
}

#[test]
fn system_blocks_become_instruction_parts() {
    let block = |text: &str| SystemBlock {
        text: text.into(),
        cacheable: true,
    };
    let body = build(&ProviderRequest {
        system: vec![block("a"), block(""), block("b")],
        ..request()
    });
    assert_eq!(
        body["systemInstruction"],
        json!({"parts": [{"text": "a"}, {"text": "b"}]})
    );
}

#[test]
fn tools_and_calling_modes_use_the_native_shapes() {
    for (choice, wire) in [
        (ToolChoice::Auto {}, json!({"mode": "AUTO"})),
        (ToolChoice::Disabled {}, json!({"mode": "NONE"})),
        (ToolChoice::Required {}, json!({"mode": "ANY"})),
        (
            ToolChoice::Tool {
                name: "get_weather".into(),
            },
            json!({"mode": "ANY", "allowedFunctionNames": ["get_weather"]}),
        ),
    ] {
        let body = build(&ProviderRequest {
            tools: vec![weather_tool()],
            tool_choice: Some(choice),
            ..request()
        });
        assert_eq!(body["toolConfig"], json!({"functionCallingConfig": wire}));
        assert_eq!(
            body["tools"],
            json!([{"functionDeclarations": [{"name": "get_weather", "description": "Weather.",
                "parametersJsonSchema": {"type": "object"}}]}])
        );
    }
    let no_tools = build(&ProviderRequest {
        tool_choice: Some(ToolChoice::Required {}),
        ..request()
    });
    assert!(no_tools.get("toolConfig").is_none());
}

#[test]
fn sampling_controls_go_into_generation_config() {
    let body = build(&ProviderRequest {
        max_output_tokens: Some(256),
        temperature: Some(0.3),
        stop_sequences: vec!["END".into()],
        ..request()
    });
    assert_eq!(
        body["generationConfig"],
        json!({"maxOutputTokens": 256, "temperature": 0.3, "stopSequences": ["END"]})
    );
}

#[test]
fn thinking_states_map_to_budget_and_include_thoughts() {
    let config = |thinking| {
        build(&ProviderRequest {
            thinking,
            ..request()
        })["generationConfig"]["thinkingConfig"]
            .clone()
    };
    assert_eq!(config(None), Value::Null);
    assert_eq!(
        config(Some(Thinking::Disabled {})),
        json!({"thinkingBudget": 0})
    );
    assert_eq!(
        config(Some(Thinking::Adaptive {
            display: Some(ThinkingDisplay::Summarized)
        })),
        json!({"includeThoughts": true})
    );
    assert_eq!(
        config(Some(Thinking::Enabled {
            budget_tokens: Some(512),
            display: Some(ThinkingDisplay::Omitted),
        })),
        json!({"thinkingBudget": 512, "includeThoughts": false})
    );
    assert_eq!(
        config(Some(Thinking::Enabled {
            budget_tokens: None,
            display: None,
        })),
        Value::Null
    );
}

#[test]
fn effort_becomes_thinking_level_and_never_meets_a_budget() {
    let with = |effort, thinking| ProviderRequest {
        effort: Some(effort),
        thinking,
        ..request()
    };
    for (effort, level) in [
        (ReasoningEffort::Low, "LOW"),
        (ReasoningEffort::Medium, "MEDIUM"),
        (ReasoningEffort::High, "HIGH"),
        (ReasoningEffort::Xhigh, "HIGH"),
        (ReasoningEffort::Max, "HIGH"),
    ] {
        let body = build(&with(effort, None));
        assert_eq!(
            body["generationConfig"]["thinkingConfig"],
            json!({"thinkingLevel": level})
        );
    }
    let budgeted = Thinking::Enabled {
        budget_tokens: Some(512),
        display: None,
    };
    assert!(invalid(&with(ReasoningEffort::Low, Some(budgeted))));
    let off = build(&with(ReasoningEffort::Low, Some(Thinking::Disabled {})));
    assert_eq!(
        off["generationConfig"]["thinkingConfig"],
        json!({"thinkingBudget": 0})
    );
}

#[test]
fn a_foreign_tool_turn_is_translated_without_ids() {
    let image = |data: &str| ImageBlock {
        source: ImageSource::Base64 {
            media_type: "image/png".into(),
            data: data.into(),
        },
    };
    let failed = ToolResultBlock::text("toolu_b", "no such city", true);
    let chart = ToolResultBlock {
        tool_use_id: "toolu_a".into(),
        content: vec![
            ToolResultPart::Text(TextBlock { text: "18C".into() }),
            ToolResultPart::Image(image("AAAA")),
        ],
        is_error: false,
    };
    let body = build(&with_messages(vec![
        Message::user_text("weather?"),
        Message::new(
            Role::Assistant,
            vec![
                ContentBlock::Thinking(ThinkingBlock {
                    thinking: "other vendor".into(),
                    signature: Some("sig".into()),
                }),
                ContentBlock::text("Checking."),
                call("toolu_a", "Paris"),
                call("toolu_b", "Atlantis"),
            ],
        ),
        Message::new(
            Role::User,
            vec![
                ContentBlock::text("thanks"),
                ContentBlock::ToolResult(chart),
                ContentBlock::ToolResult(failed),
                ContentBlock::Image(ImageBlock {
                    source: ImageSource::Url {
                        url: "https://x/y.png".into(),
                    },
                }),
            ],
        ),
    ]));
    assert_eq!(
        body["contents"][1],
        json!({"role": "model", "parts": [
            {"text": "Checking."},
            {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}},
            {"functionCall": {"name": "get_weather", "args": {"city": "Atlantis"}}},
        ]})
    );
    assert_eq!(
        body["contents"][2],
        json!({"role": "user", "parts": [
            {"functionResponse": {"name": "get_weather", "response": {"output": "18C"},
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]}},
            {"functionResponse": {"name": "get_weather", "response": {"error": "no such city"}}},
            {"text": "thanks"},
            {"fileData": {"fileUri": "https://x/y.png"}},
        ]})
    );
}

#[test]
fn a_kept_gemini_turn_is_replayed_verbatim_and_its_ids_echoed() {
    let parts = json!([
        {"text": "Plan.", "thought": true},
        {"functionCall": {"id": "fc-1", "name": "get_weather", "args": {"city": "Paris"}},
         "thoughtSignature": "c2ln"},
        {"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}},
    ]);
    let mut metadata = JsonObject::new();
    metadata.insert(PARTS_KEY.into(), parts.clone());
    let assistant = Message {
        provider_metadata: Some(metadata),
        ..Message::new(
            Role::Assistant,
            vec![call("fc-1", "Paris"), call("gemini_2_1", "Tokyo")],
        )
    };
    let body = build(&with_messages(vec![
        Message::user_text("weather?"),
        assistant,
        Message::new(
            Role::User,
            vec![
                ContentBlock::ToolResult(ToolResultBlock::text("fc-1", "18C", false)),
                ContentBlock::ToolResult(ToolResultBlock::text("gemini_2_1", "25C", false)),
            ],
        ),
    ]));
    assert_eq!(body["contents"][1]["parts"], parts);
    let responses = &body["contents"][2]["parts"];
    assert_eq!(responses[0]["functionResponse"]["id"], "fc-1");
    assert!(responses[1]["functionResponse"].get("id").is_none());
}

#[test]
fn results_must_answer_the_previous_assistant_turn() {
    let result = |id: &str| {
        Message::new(
            Role::User,
            vec![ContentBlock::ToolResult(ToolResultBlock::text(
                id, "r", false,
            ))],
        )
    };
    let earlier = Message::new(Role::Assistant, vec![call("gemini_0_0", "Paris")]);
    let later = Message::new(Role::Assistant, vec![call("gemini_2_0", "Tokyo")]);
    assert!(invalid(&with_messages(vec![result("x")])));
    assert!(invalid(&with_messages(vec![
        earlier.clone(),
        result("gemini_0_0"),
        later.clone(),
        result("gemini_0_0"),
    ])));
    assert!(!invalid(&with_messages(vec![
        earlier,
        result("gemini_0_0"),
        later,
        result("gemini_2_0"),
    ])));
}

#[test]
fn misplaced_blocks_and_output_schema_fail_locally() {
    let misplaced = [
        Message::new(
            Role::Assistant,
            vec![ContentBlock::ToolResult(ToolResultBlock::text(
                "c", "r", false,
            ))],
        ),
        Message::new(Role::User, vec![call("c", "Paris")]),
    ];
    for message in misplaced {
        assert!(invalid(&with_messages(vec![message])));
    }
    assert!(invalid(&ProviderRequest {
        output_schema: Some(OutputSchema {
            name: "s".into(),
            schema: serde_json::Map::new(),
            strict: true,
            description: None,
        }),
        ..request()
    }));
}
