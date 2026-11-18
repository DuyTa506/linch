//! One test per payload rule.

use linch_core::provider::ThinkingDisplay;
use linch_core::{
    CacheControl, CacheTtl, ContentBlock, ImageBlock, ImageSource, Message, OutputSchema,
    ProviderError, ProviderRequest, ReasoningEffort, RedactedThinkingBlock, Role, SystemBlock,
    TextBlock, Thinking, ThinkingBlock, ToolChoice, ToolDefinition, ToolResultBlock,
    ToolResultPart, ToolUseBlock,
};
use serde_json::json;

use super::request;
use crate::anthropic::{DEFAULT_MAX_TOKENS, payload};

fn build(request: &ProviderRequest) -> serde_json::Value {
    payload::build(request, false).unwrap()
}

fn invalid(request: &ProviderRequest) -> bool {
    matches!(
        payload::build(request, false),
        Err(ProviderError::InvalidRequest { .. })
    )
}

fn system(text: &str, cacheable: bool) -> SystemBlock {
    SystemBlock {
        text: text.into(),
        cacheable,
    }
}

fn weather_tool() -> ToolDefinition {
    ToolDefinition {
        name: "get_weather".into(),
        description: "Weather.".into(),
        input_schema: json!({"type": "object"})
            .as_object()
            .unwrap()
            .clone()
            .into(),
    }
}

#[test]
fn base_body_streams_with_a_default_max_tokens() {
    assert_eq!(
        build(&request()),
        json!({
            "model": "claude-sonnet-4-6",
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": true,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        })
    );
}

#[test]
fn thinking_states_map_to_the_native_field() {
    let summarized = Some(ThinkingDisplay::Summarized);
    for (state, wire) in [
        (Thinking::Disabled {}, json!({"type": "disabled"})),
        (
            Thinking::Adaptive {
                display: summarized,
            },
            json!({"type": "adaptive", "display": "summarized"}),
        ),
        (
            Thinking::Enabled {
                budget_tokens: Some(1024),
                display: Some(ThinkingDisplay::Omitted),
            },
            json!({"type": "enabled", "budget_tokens": 1024, "display": "omitted"}),
        ),
        (
            Thinking::Enabled {
                budget_tokens: None,
                display: None,
            },
            json!({"type": "adaptive"}),
        ),
    ] {
        let body = build(&ProviderRequest {
            thinking: Some(state),
            ..request()
        });
        assert_eq!(body["thinking"], wire);
    }
    assert!(build(&request()).get("thinking").is_none());
}

#[test]
fn a_budget_at_or_above_max_tokens_fails_locally() {
    let budget = |budget, max| ProviderRequest {
        thinking: Some(Thinking::Enabled {
            budget_tokens: Some(budget),
            display: None,
        }),
        max_output_tokens: max,
        ..request()
    };
    assert!(invalid(&budget(DEFAULT_MAX_TOKENS, None)));
    assert!(invalid(&budget(2048, Some(2048))));
    assert!(!invalid(&budget(2047, Some(2048))));
}

#[test]
fn forced_tool_choice_fails_only_with_budgeted_thinking() {
    let budgeted = Thinking::Enabled {
        budget_tokens: Some(1024),
        display: None,
    };
    let forced = [
        ToolChoice::Required {},
        ToolChoice::Tool {
            name: "get_weather".into(),
        },
    ];
    for choice in forced {
        let with = |thinking| ProviderRequest {
            tools: vec![weather_tool()],
            tool_choice: Some(choice.clone()),
            thinking,
            ..request()
        };
        assert!(invalid(&with(Some(budgeted.clone()))));
        assert!(!invalid(&with(Some(Thinking::Adaptive { display: None }))));
        assert!(!invalid(&with(None)));
        // The open-time check is the same rule.
        assert!(payload::tool_choice_refusal(&choice, Some(&budgeted)).is_some());
        let adaptive = Thinking::Adaptive { display: None };
        assert!(payload::tool_choice_refusal(&choice, Some(&adaptive)).is_none());
        assert!(payload::tool_choice_refusal(&choice, None).is_none());
    }
    for open in [ToolChoice::Auto {}, ToolChoice::Disabled {}] {
        assert!(payload::tool_choice_refusal(&open, Some(&budgeted)).is_none());
    }
}

#[test]
fn tool_choices_and_tools_use_the_native_shapes() {
    for (choice, wire) in [
        (ToolChoice::Auto {}, json!({"type": "auto"})),
        (ToolChoice::Disabled {}, json!({"type": "none"})),
        (ToolChoice::Required {}, json!({"type": "any"})),
        (
            ToolChoice::Tool { name: "x".into() },
            json!({"type": "tool", "name": "x"}),
        ),
    ] {
        let body = build(&ProviderRequest {
            tools: vec![weather_tool()],
            tool_choice: Some(choice),
            ..request()
        });
        assert_eq!(body["tool_choice"], wire);
        assert_eq!(
            body["tools"],
            json!([{"name": "get_weather", "description": "Weather.", "input_schema": {"type": "object"}}])
        );
    }
}

#[test]
fn effort_goes_into_output_config() {
    for (effort, wire) in [
        (ReasoningEffort::Low, "low"),
        (ReasoningEffort::Medium, "medium"),
        (ReasoningEffort::High, "high"),
        (ReasoningEffort::Xhigh, "xhigh"),
        (ReasoningEffort::Max, "max"),
    ] {
        let body = build(&ProviderRequest {
            effort: Some(effort),
            ..request()
        });
        assert_eq!(body["output_config"], json!({"effort": wire}));
    }
}

#[test]
fn sampling_controls_are_forwarded() {
    let body = build(&ProviderRequest {
        max_output_tokens: Some(256),
        temperature: Some(0.2),
        stop_sequences: vec!["END".into()],
        ..request()
    });
    assert_eq!(body["max_tokens"], 256);
    assert_eq!(body["temperature"], 0.2);
    assert_eq!(body["stop_sequences"], json!(["END"]));
}

#[test]
fn cache_breakpoints_go_on_the_last_cacheable_system_block_and_last_user_block() {
    let conversation = vec![
        Message::user_text("first"),
        Message::assistant_text("ok"),
        Message::new(
            Role::User,
            vec![ContentBlock::text("a"), ContentBlock::text("b")],
        ),
    ];
    let cached = |ttl| ProviderRequest {
        system: vec![
            system("static", true),
            system("", true),
            system("volatile", false),
        ],
        messages: conversation.clone(),
        cache_control: Some(CacheControl {
            ttl,
            ..CacheControl::default()
        }),
        ..request()
    };
    let body = build(&cached(Some(CacheTtl::OneHour)));
    let one_hour = json!({"type": "ephemeral", "ttl": "1h"});
    assert_eq!(
        body["system"],
        json!([
            {"type": "text", "text": "static", "cache_control": one_hour},
            {"type": "text", "text": "volatile"},
        ])
    );
    assert_eq!(body["messages"][2]["content"][1]["cache_control"], one_hour);
    assert!(
        body["messages"][2]["content"][0]
            .get("cache_control")
            .is_none()
    );
    assert!(
        body["messages"][0]["content"][0]
            .get("cache_control")
            .is_none()
    );
    for ttl in [None, Some(CacheTtl::FiveMinutes)] {
        let body = build(&cached(ttl));
        assert_eq!(
            body["system"][0]["cache_control"],
            json!({"type": "ephemeral"})
        );
    }
    let uncached = build(&ProviderRequest {
        cache_control: None,
        ..cached(None)
    });
    assert!(!uncached.to_string().contains("cache_control"));
}

#[test]
fn an_uncached_tail_leaves_the_message_breakpoint_on_the_last_message_before_it() {
    let tail = ProviderRequest {
        messages: vec![
            Message::user_text("first"),
            Message::assistant_text("ok"),
            Message::user_text("shared"),
            Message::user_text("summarize"),
        ],
        cache_control: Some(CacheControl {
            uncached_tail: 1,
            ..CacheControl::default()
        }),
        ..request()
    };
    let body = build(&tail);
    let breakpoint = json!({"type": "ephemeral"});
    assert_eq!(
        body["messages"][2]["content"][0]["cache_control"],
        breakpoint
    );
    assert!(
        body["messages"][3]["content"][0]
            .get("cache_control")
            .is_none()
    );
    // The bytes of the shared prefix equal a request that ends there.
    let ends_there = ProviderRequest {
        messages: tail.messages[..3].to_vec(),
        cache_control: Some(CacheControl::default()),
        ..request()
    };
    assert_eq!(
        build(&ends_there)["messages"],
        json!(body["messages"].as_array().unwrap()[..3])
    );
}

#[test]
fn assistant_turns_replay_signed_thinking_and_drop_unsigned() {
    let call = ToolUseBlock {
        id: "toolu_1".into(),
        name: "get_weather".into(),
        input: json!({"city": "Paris"}).as_object().unwrap().clone(),
        raw_input: None,
    };
    let thinking = |text: &str, signature: Option<&str>| {
        ContentBlock::Thinking(ThinkingBlock {
            thinking: text.into(),
            signature: signature.map(String::from),
        })
    };
    let body = build(&ProviderRequest {
        messages: vec![
            Message::user_text("weather?"),
            Message::new(
                Role::Assistant,
                vec![
                    thinking("", Some("sig-empty")),
                    thinking("foreign", None),
                    ContentBlock::RedactedThinking(RedactedThinkingBlock {
                        data: "opaque".into(),
                    }),
                    ContentBlock::text(""),
                    ContentBlock::text("Checking."),
                    ContentBlock::ToolUse(call),
                ],
            ),
        ],
        ..request()
    });
    assert_eq!(
        body["messages"][1],
        json!({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-empty"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
        ]})
    );
}

#[test]
fn tool_results_lead_the_user_message_with_images_and_error_flags() {
    let image = ImageBlock {
        source: ImageSource::Base64 {
            media_type: "image/png".into(),
            data: "AAAA".into(),
        },
    };
    let result = ToolResultBlock {
        tool_use_id: "toolu_2".into(),
        content: vec![
            ToolResultPart::Text(TextBlock {
                text: "chart".into(),
            }),
            ToolResultPart::Image(image),
        ],
        is_error: false,
    };
    let body = build(&ProviderRequest {
        messages: vec![Message::new(
            Role::User,
            vec![
                ContentBlock::text("and also"),
                ContentBlock::ToolResult(ToolResultBlock::text("toolu_1", "boom", true)),
                ContentBlock::ToolResult(result),
                ContentBlock::ToolResult(ToolResultBlock::text("toolu_3", "", false)),
                ContentBlock::Image(ImageBlock {
                    source: ImageSource::Url {
                        url: "https://x/y.png".into(),
                    },
                }),
            ],
        )],
        ..request()
    });
    assert_eq!(
        body["messages"][0]["content"],
        json!([
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": true,
             "content": [{"type": "text", "text": "boom"}]},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": [
                {"type": "text", "text": "chart"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ]},
            {"type": "tool_result", "tool_use_id": "toolu_3"},
            {"type": "text", "text": "and also"},
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
        ])
    );
}

#[test]
fn an_output_schema_is_native_json_output_beside_effort() {
    let schema = json!({"type": "object", "properties": {"total": {"type": "number"}},
                        "required": ["total"], "additionalProperties": false});
    let output = OutputSchema {
        name: "invoice".into(),
        schema: schema.as_object().unwrap().clone(),
        strict: true,
        description: Some("the bill".into()),
    };
    let body = build(&ProviderRequest {
        output_schema: Some(output.clone()),
        effort: Some(ReasoningEffort::High),
        tools: vec![weather_tool()],
        ..request()
    });
    assert_eq!(
        body["output_config"],
        json!({"effort": "high", "format": {"type": "json_schema", "schema": schema}})
    );
    assert!(body.get("tool_choice").is_none(), "no forced final tool");
    assert_eq!(body["tools"].as_array().unwrap().len(), 1);
    let alone = build(&ProviderRequest {
        output_schema: Some(output),
        ..request()
    });
    assert_eq!(
        alone["output_config"],
        json!({"format": {"type": "json_schema", "schema": schema}})
    );
    assert!(build(&request()).get("output_config").is_none());
}

#[test]
fn misplaced_blocks_fail_locally() {
    let misplaced = [
        Message::new(
            Role::Assistant,
            vec![ContentBlock::ToolResult(ToolResultBlock::text(
                "c", "r", false,
            ))],
        ),
        Message::new(
            Role::User,
            vec![ContentBlock::RedactedThinking(RedactedThinkingBlock {
                data: "d".into(),
            })],
        ),
    ];
    for message in misplaced {
        assert!(invalid(&ProviderRequest {
            messages: vec![message],
            ..request()
        }));
    }
}

/// The system blocks an agent with host blocks sends: the breakpoint lands
/// on the last cacheable host block, before the system prompt, or on the
/// skill listing after it when the agent has skills.
#[tokio::test]
async fn an_agent_s_host_blocks_carry_the_system_breakpoint() {
    use std::sync::Arc;

    use futures::StreamExt;
    use linch_core::{
        AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
        PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, ScriptedTurn, Skill,
    };

    async fn sent(skills: bool) -> ProviderRequest {
        let mut spec = AgentSpec::new("claude-sonnet-4-6");
        spec.system_prompt = "Be brief.".into();
        spec.system_blocks = vec![system("Policy.", true), system("Tone.", false)];
        let reply = ScriptedTurn::builder("claude-sonnet-4-6").text("hi");
        let provider = Arc::new(ScriptedProvider::new([reply.build()]));
        let policy = PermissionPolicy::new(PermissionMode::Default, "/");
        let mut builder = AgentBuilder::new(spec)
            .provider(provider.clone())
            .permissions(PermissionEngine::new(policy).unwrap())
            .run_store(Arc::new(MemoryRunStore::default()))
            .session_store(Arc::new(MemorySessionStore::default()))
            .namespace("ns");
        if skills {
            builder = builder.skills(vec![Skill::new("fmt", "/s/fmt", "Format.", "fix")]);
        }
        let agent = builder.build().unwrap();
        let mut stream = agent.run("go", RunOptions::new("s1"));
        while stream.next().await.is_some() {}
        provider.requests().remove(0)
    }

    let cache = Some(CacheControl::default());
    let body = build(&ProviderRequest {
        cache_control: cache,
        ..sent(false).await
    });
    let breakpoint = json!({"type": "ephemeral"});
    assert_eq!(
        body["system"],
        json!([
            {"type": "text", "text": "Policy.", "cache_control": breakpoint},
            {"type": "text", "text": "Tone."},
            {"type": "text", "text": "Be brief."},
        ])
    );
    let body = build(&ProviderRequest {
        cache_control: cache,
        ..sent(true).await
    });
    let marked: Vec<usize> = (0..4)
        .filter(|&i| body["system"][i].get("cache_control").is_some())
        .collect();
    assert_eq!(marked, [3], "the listing, the last cacheable block");
    assert_eq!(body["system"][2]["text"], "Be brief.");
}

/// The system sections of a spec reach the payload in order: the breakpoint
/// lands on the last cacheable section, so the section that is not
/// cacheable, the prompt and `append` stay outside the cached prefix.
#[tokio::test]
async fn an_agent_s_system_sections_carry_the_system_breakpoint() {
    use std::sync::Arc;

    use futures::StreamExt;
    use linch_core::{
        AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
        PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, ScriptedTurn,
        SystemSection,
    };

    let mut spec = AgentSpec::new("claude-sonnet-4-6");
    spec.system_prompt = "Be brief.".into();
    spec.system.sections = vec![
        SystemSection::new("today", "Today.")
            .order(9)
            .cacheable(false),
        SystemSection::new("tone", "Tone.").order(2),
        SystemSection::new("policy", "Policy.").order(1),
    ];
    spec.system.append = Some("Tail.".into());
    let reply = ScriptedTurn::builder("claude-sonnet-4-6").text("hi");
    let provider = Arc::new(ScriptedProvider::new([reply.build()]));
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("ns")
        .build()
        .unwrap();
    let mut stream = agent.run("go", RunOptions::new("s1"));
    while stream.next().await.is_some() {}
    let sent = provider.requests().remove(0);
    let body = build(&ProviderRequest {
        cache_control: Some(CacheControl::default()),
        ..sent
    });
    let breakpoint = json!({"type": "ephemeral"});
    assert_eq!(
        body["system"],
        json!([
            {"type": "text", "text": "Policy."},
            {"type": "text", "text": "Tone.", "cache_control": breakpoint},
            {"type": "text", "text": "Today."},
            {"type": "text", "text": "Be brief."},
            {"type": "text", "text": "Tail."},
        ])
    );
}

fn cached_messages(messages: Vec<Message>, context_tail: usize) -> ProviderRequest {
    ProviderRequest {
        messages,
        context_tail,
        cache_control: Some(CacheControl::default()),
        ..request()
    }
}

/// Every `cache_control` a message block carries, as `(message, block)`.
fn breakpoints(body: &serde_json::Value) -> Vec<(usize, usize)> {
    let mut found = Vec::new();
    for (m, message) in body["messages"].as_array().unwrap().iter().enumerate() {
        for (b, block) in message["content"].as_array().unwrap().iter().enumerate() {
            if block.get("cache_control").is_some() {
                found.push((m, b));
            }
        }
    }
    found
}

fn without_cache_control(mut value: serde_json::Value) -> serde_json::Value {
    match &mut value {
        serde_json::Value::Object(map) => {
            map.remove("cache_control");
            map.values_mut()
                .for_each(|v| *v = without_cache_control(v.take()));
        }
        serde_json::Value::Array(items) => {
            items
                .iter_mut()
                .for_each(|v| *v = without_cache_control(v.take()));
        }
        _ => {}
    }
    value
}

/// Context the runtime added for this request only is not logged, so the
/// breakpoint goes on the last block before it: the next turn's cached
/// message then still ends there.
#[test]
fn the_cache_breakpoint_stays_before_request_only_context() {
    let messages = vec![
        Message::user_text("first"),
        Message::assistant_text("ok"),
        Message::new(
            Role::User,
            vec![
                ContentBlock::text("question"),
                ContentBlock::text("recalled memory"),
            ],
        ),
    ];
    let body = build(&cached_messages(messages.clone(), 1));
    assert_eq!(breakpoints(&body), [(2, 0)], "on the last logged block");
    // Without context the breakpoint is on the last block, as before.
    let body = build(&cached_messages(messages, 0));
    assert_eq!(breakpoints(&body), [(2, 1)]);
}

/// Tool results lead a user message on the wire, so the logged part is
/// found by what it holds, not by counting from the end of the JSON.
#[test]
fn the_breakpoint_is_after_the_last_tool_result_when_context_follows() {
    let results = Message::new(
        Role::User,
        vec![
            ContentBlock::text("note"),
            ContentBlock::ToolResult(ToolResultBlock::text("t1", "18C", false)),
            ContentBlock::ToolResult(ToolResultBlock::text("t2", "ok", false)),
            ContentBlock::text("recalled"),
        ],
    );
    let messages = vec![Message::user_text("q"), results];
    let body = build(&cached_messages(messages, 1));
    // Wire order: t1, t2, then the logged text "note", then the context.
    assert_eq!(breakpoints(&body), [(1, 2)]);
    assert_eq!(body["messages"][1]["content"][2]["text"], "note");
}

#[test]
fn a_message_that_is_all_context_leaves_the_breakpoint_on_the_last_logged_user_message() {
    let messages = vec![
        Message::user_text("first"),
        Message::assistant_text("ok"),
        Message::new(Role::User, vec![ContentBlock::text("recalled")]),
    ];
    let body = build(&cached_messages(messages, 1));
    assert_eq!(breakpoints(&body), [(0, 0)], "not on the assistant turn");
}

/// A hook may have rewritten the messages; a count past what exists is read
/// as the whole last message and never panics.
#[test]
fn a_context_tail_longer_than_the_message_is_clamped() {
    let messages = vec![
        Message::user_text("first"),
        Message::new(Role::User, vec![ContentBlock::text("a")]),
    ];
    let body = build(&cached_messages(messages, 9));
    assert_eq!(breakpoints(&body), [(0, 0)]);
}

/// What turn 1 caches (everything up to its breakpoint) is the same bytes
/// at the head of turn 2's request, the recall of turn 1 being gone.
#[test]
fn the_cached_prefix_of_one_turn_is_a_prefix_of_the_next() {
    let call = ToolUseBlock {
        id: "toolu_1".into(),
        name: "get_weather".into(),
        input: json!({"city": "Hue"}).as_object().unwrap().clone(),
        raw_input: None,
    };
    let ask = || Message::user_text("what is the weather");
    let mut first_user = ask();
    first_user
        .content
        .push(ContentBlock::text("recall for turn 1"));
    let turn1 = build(&cached_messages(vec![first_user], 1));
    let mut results = Message::new(
        Role::User,
        vec![ContentBlock::ToolResult(ToolResultBlock::text(
            "toolu_1", "18C", false,
        ))],
    );
    results
        .content
        .push(ContentBlock::text("recall for turn 2"));
    let turn2 = build(&cached_messages(
        vec![
            ask(),
            Message::new(Role::Assistant, vec![ContentBlock::ToolUse(call)]),
            results,
        ],
        1,
    ));
    let (message, block) = breakpoints(&turn1)[0];
    let cached = &turn1["messages"].as_array().unwrap()[..=message];
    let mut cached = without_cache_control(json!(cached));
    cached[message]["content"]
        .as_array_mut()
        .unwrap()
        .truncate(block + 1);
    let head = &turn2["messages"].as_array().unwrap()[..=message];
    let mut head = without_cache_control(json!(head));
    head[message]["content"]
        .as_array_mut()
        .unwrap()
        .truncate(block + 1);
    assert_eq!(cached, head, "turn 1's cached prefix is turn 2's head");
}
