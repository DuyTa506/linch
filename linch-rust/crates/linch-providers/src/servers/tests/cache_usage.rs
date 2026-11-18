//! Cache-read usage from local servers, as Python's adapters read it
//! (`_prompt_cache.openai_cached_tokens`, `sglang._usage_from_chunk`,
//! `llamacpp._llamacpp_cache_read_tokens`).

use linch_core::Usage;
use serde_json::{Value, json};

use super::{Server, assemble_fixture};

fn usage_of(server: Server, usage: Value) -> Usage {
    let wire = serde_json::from_value(usage).unwrap();
    (server.profile().usage)(&wire)
}

fn split(input: u64, read: u64) -> Usage {
    Usage {
        input_tokens: input,
        output_tokens: 7,
        cache_read_tokens: read,
        cache_creation_tokens: 0,
    }
}

/// SGLang: `usage.cached_tokens` stands in only when the server sends no
/// `prompt_tokens_details`.
#[test]
fn sglang_reads_the_top_level_count_when_details_are_absent() {
    let top = json!({"prompt_tokens": 100, "completion_tokens": 7, "cached_tokens": 64});
    assert_eq!(usage_of(Server::SgLang, top), split(36, 64));
    let both = json!({"prompt_tokens": 100, "completion_tokens": 7, "cached_tokens": 90,
        "prompt_tokens_details": {"cached_tokens": 10}});
    assert_eq!(usage_of(Server::SgLang, both), split(90, 10));
}

/// llama.cpp: the generic chain of names, details first, then the usage
/// object itself.
#[test]
fn llamacpp_reads_every_name_python_reads() {
    for usage in [
        json!({"prompt_tokens_details": {"cached_tokens": 64}}),
        json!({"prompt_tokens_details": {"cache_read_tokens": 64}}),
        json!({"prompt_tokens_details": {"prompt_cache_hit_tokens": 64}}),
        json!({"cached_tokens": 64}),
        json!({"cache_read_tokens": 64}),
        json!({"prompt_cache_hit_tokens": 64}),
    ] {
        let mut usage = usage;
        usage["prompt_tokens"] = json!(100);
        usage["completion_tokens"] = json!(7);
        let got = usage_of(Server::LlamaCpp, usage.clone());
        assert_eq!(got, split(36, 64), "{usage}");
    }
}

/// vLLM reads `cached_tokens` and `created_cache_tokens` of the details
/// only, as Python's adapter does.
#[test]
fn vllm_ignores_a_top_level_count() {
    let top = json!({"prompt_tokens": 100, "completion_tokens": 7, "cached_tokens": 64});
    assert_eq!(usage_of(Server::Vllm, top), split(100, 0));
}

const TIMINGS: &str = "data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":\"stop\"}]}\n\n\
data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":7,\"prompt_tokens_details\":null},\"timings\":{\"cache_n\":80}}\n\n\
data: [DONE]\n\n";

/// llama.cpp leaves the details null and reports the reused prefix in
/// `timings.cache_n`; the count applies only when usage reports none.
#[tokio::test]
async fn llamacpp_reads_the_prefix_reuse_from_timings() {
    let turn = assemble_fixture(Server::LlamaCpp, TIMINGS).await;
    assert_eq!(turn.usage, Some(split(20, 80)));
    let reported = TIMINGS.replace(
        "\"prompt_tokens_details\":null",
        "\"prompt_tokens_details\":{\"cached_tokens\":50}",
    );
    let turn = assemble_fixture(Server::LlamaCpp, &reported).await;
    assert_eq!(turn.usage, Some(split(50, 50)));
    for server in [Server::Vllm, Server::SgLang] {
        let turn = assemble_fixture(server, TIMINGS).await;
        assert_eq!(turn.usage, Some(split(100, 0)), "{server:?}");
    }
}
