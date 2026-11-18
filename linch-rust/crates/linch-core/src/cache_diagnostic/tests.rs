use std::time::{Duration, Instant};

use serde_json::json;

use super::*;
use crate::events::kinds::CacheMissReason as Reason;
use crate::message::{Message, SystemBlock};
use crate::provider::{CacheControl, ToolDefinition};

fn request(model: &str, messages: &[&str]) -> ProviderRequest {
    ProviderRequest {
        model: model.into(),
        system: vec![SystemBlock {
            text: "be brief".into(),
            cacheable: true,
        }],
        tools: vec![tool("Read")],
        appended_tools: vec![],
        messages: messages
            .iter()
            .map(|text| Message::user_text(*text))
            .collect(),
        context_tail: 0,
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

fn tool(name: &str) -> ToolDefinition {
    ToolDefinition {
        name: name.into(),
        description: "reads".into(),
        input_schema: json!({"type": "object"})
            .as_object()
            .unwrap()
            .clone()
            .into(),
    }
}

/// What the provider reported: `cached` read from the cache, `fresh` not.
fn usage(cached: u64, fresh: u64) -> Usage {
    Usage {
        input_tokens: fresh,
        cache_read_tokens: cached,
        ..Usage::default()
    }
}

/// Two calls through one watch: the first at `t0`, the second `gap` later.
fn second_call(
    provider: &str,
    first: &ProviderRequest,
    second: &ProviderRequest,
    gap: Duration,
    reported: Usage,
) -> Option<CacheDiagnosticEvent> {
    let mut watch = CacheWatch::default();
    let t0 = Instant::now();
    watch.sending(provider, first, t0);
    assert_eq!(
        watch.settled(&usage(0, 5000)),
        None,
        "the first call is alone"
    );
    watch.sending(provider, second, t0 + gap);
    watch.settled(&reported)
}

const SOON: Duration = Duration::from_secs(10);

#[test]
fn the_first_call_says_nothing_and_an_unchanged_prefix_that_was_read_is_no_miss() {
    let first = request("m", &["a"]);
    let second = request("m", &["a", "b"]);
    let event = second_call("deepseek", &first, &second, SOON, usage(4800, 400)).unwrap();
    assert_eq!(event.reason, Reason::NoMiss);
    assert_eq!(
        (event.previous_cached, event.cached, event.prompt_tokens),
        (0, 4800, 5200)
    );
    assert_eq!(event.first_changed_message, None);
}

#[test]
fn a_prefix_that_is_the_same_but_barely_read_is_a_low_read() {
    let first = request("m", &["a"]);
    let second = request("m", &["a", "b"]);
    // Under half of the 5000 tokens the previous call could have left.
    let event = second_call("deepseek", &first, &second, SOON, usage(2000, 3000)).unwrap();
    assert_eq!(event.reason, Reason::LowRead);
    // Exactly half is not low.
    let half = second_call("deepseek", &first, &second, SOON, usage(2500, 2600)).unwrap();
    assert_eq!(half.reason, Reason::NoMiss);
}

#[test]
fn each_reason_is_found_in_its_order() {
    let base = request("m", &["a", "b"]);
    let reported = usage(0, 5000);

    let other_model = request("n", &["a", "b"]);
    let found = second_call("anthropic", &base, &other_model, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::ModelChanged);

    let mut system = base.clone();
    system.system[0].text = "be long".into();
    let found = second_call("anthropic", &base, &system, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::SystemChanged);

    let mut tools = base.clone();
    tools.tools.push(tool("Write"));
    let found = second_call("anthropic", &base, &tools, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::ToolsChanged);
    assert_eq!(found.first_changed_message, None);

    let edited = request("m", &["a", "changed", "c"]);
    let found = second_call("anthropic", &base, &edited, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::PrefixChanged);
    assert_eq!(found.first_changed_message, Some(1));

    // The conversation got shorter, the start replaced: a compaction.
    let compacted = request("m", &["summary"]);
    let found = second_call("anthropic", &base, &compacted, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::Compaction);
    assert_eq!(found.first_changed_message, Some(0));
    let trimmed = request("m", &["a"]);
    let found = second_call("anthropic", &base, &trimmed, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::Compaction);
    assert_eq!(found.first_changed_message, Some(1));
}

#[test]
fn an_earlier_reason_wins_over_a_later_one() {
    let base = request("m", &["a", "b"]);
    let mut all = request("n", &["x"]);
    all.system[0].text = "other".into();
    all.tools.clear();
    let reported = usage(0, 5000);
    let found = second_call("anthropic", &base, &all, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::ModelChanged);
    let mut all = request("m", &["x"]);
    all.system[0].text = "other".into();
    all.tools.clear();
    let found = second_call("anthropic", &base, &all, SOON, reported).unwrap();
    assert_eq!(found.reason, Reason::SystemChanged);
    // Past the TTL beats a changed system prompt.
    let late = Duration::from_secs(301);
    let found = second_call("anthropic", &base, &all, late, reported).unwrap();
    assert_eq!(found.reason, Reason::TtlExpired);
}

#[test]
fn only_a_provider_that_declares_a_ttl_can_expire() {
    let first = request("m", &["a"]);
    let second = request("m", &["a", "b"]);
    let reported = usage(0, 5000);
    let day = Duration::from_secs(86_400);
    // Anthropic: 5 minutes by default, 1 hour when the request asks.
    let found = second_call("anthropic", &first, &second, day, reported).unwrap();
    assert_eq!(found.reason, Reason::TtlExpired);
    let found = second_call(
        "anthropic",
        &first,
        &second,
        Duration::from_secs(300),
        reported,
    );
    assert_ne!(found.unwrap().reason, Reason::TtlExpired);
    let mut long = second.clone();
    long.cache_control = Some(CacheControl {
        ttl: Some(CacheTtl::OneHour),
        uncached_tail: 0,
    });
    let found = second_call(
        "anthropic",
        &first,
        &long,
        Duration::from_secs(1800),
        reported,
    );
    assert_ne!(found.unwrap().reason, Reason::TtlExpired);
    // The others declare none, so no gap is blamed.
    for provider in [
        "openai-chat",
        "openai-responses",
        "deepseek",
        "gemini",
        "vllm",
    ] {
        let found = second_call(provider, &first, &second, day, usage(0, 5000));
        assert_ne!(found.unwrap().reason, Reason::TtlExpired, "{provider}");
    }
}

#[test]
fn a_changed_provider_is_found_after_the_model() {
    let mut watch = CacheWatch::default();
    let t0 = Instant::now();
    let same = request("m", &["a"]);
    watch.sending("deepseek", &same, t0);
    watch.settled(&usage(0, 5000));
    watch.sending("vllm", &same, t0 + SOON);
    let found = watch.settled(&usage(0, 5000)).unwrap();
    assert_eq!(found.reason, Reason::ProviderChanged);
}

#[test]
fn a_call_under_the_floor_or_with_no_report_says_nothing() {
    let mut watch = CacheWatch::default();
    let t0 = Instant::now();
    let first = request("m", &["a"]);
    watch.sending("deepseek", &first, t0);
    watch.settled(&usage(0, 5000));

    // 1023 input tokens: under the floor; nothing said.
    let second = request("m", &["a", "b"]);
    watch.sending("deepseek", &second, t0 + SOON);
    assert_eq!(watch.settled(&usage(500, 523)), None);
    // No input tokens reported: nothing said, and the call is not kept.
    watch.sending("deepseek", &second, t0 + SOON);
    assert_eq!(watch.settled(&Usage::default()), None);
    // 1024 is enough for a provider other than OpenAI.
    watch.sending("deepseek", &second, t0 + SOON);
    assert!(watch.settled(&usage(0, 1024)).is_some());
}

#[test]
fn openai_needs_2048_input_tokens() {
    for (provider, said) in [("openai-chat", false), ("openai-responses", false)] {
        let mut watch = CacheWatch::default();
        let t0 = Instant::now();
        watch.sending(provider, &request("m", &["a"]), t0);
        watch.settled(&usage(0, 3000));
        watch.sending(provider, &request("m", &["a", "b"]), t0 + SOON);
        assert_eq!(watch.settled(&usage(0, 2047)).is_some(), said, "{provider}");
        watch.sending(provider, &request("m", &["a", "b"]), t0 + SOON);
        assert!(watch.settled(&usage(0, 2048)).is_some(), "{provider}");
    }
}

#[test]
fn a_call_that_never_settled_is_not_the_previous_one() {
    let mut watch = CacheWatch::default();
    let t0 = Instant::now();
    watch.sending("deepseek", &request("m", &["a"]), t0);
    watch.settled(&usage(0, 5000));
    // A call that failed: sent, never settled.
    watch.sending("deepseek", &request("m", &["a", "b"]), t0 + SOON);
    // The next one compares with the last answered call, not the failed one.
    watch.sending("deepseek", &request("m", &["a", "b", "c"]), t0 + SOON);
    let found = watch.settled(&usage(4800, 400)).unwrap();
    assert_eq!(found.reason, Reason::NoMiss);
    assert_eq!(found.first_changed_message, None);
}

#[test]
fn the_watch_holds_hashes_not_text() {
    let secret = "SECRET-PROMPT-TEXT";
    let mut watch = CacheWatch::default();
    watch.sending("deepseek", &request("m", &[secret]), Instant::now());
    watch.settled(&usage(0, 5000));
    assert!(!format!("{watch:?}").contains(secret));
}
