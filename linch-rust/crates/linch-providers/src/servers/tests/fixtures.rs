//! Each server's hand-written SSE quirks, run through its translator and
//! the assembler.

use futures::StreamExt;
use linch_core::{ContentBlock, ProviderError, StopReason, Usage};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::{Server, assemble_fixture};
use crate::openai::test_support::bytes;
use crate::openai_compat::stream::{self, Translator};

const VLLM: &str = include_str!("../../../tests/fixtures/vllm_reasoning_tools.sse");
const SGLANG: &str = include_str!("../../../tests/fixtures/sglang_reasoning_tools.sse");
const LLAMACPP: &str = include_str!("../../../tests/fixtures/llamacpp_reasoning_tools.sse");
const LLAMACPP_ERROR: &str = include_str!("../../../tests/fixtures/llamacpp_stream_error.sse");

fn thinking(turn: &linch_core::AssistantAssembly) -> Option<&str> {
    turn.message.content.iter().find_map(|block| match block {
        ContentBlock::Thinking(block) => Some(block.thinking.as_str()),
        _ => None,
    })
}

#[tokio::test]
async fn vllm_reasoning_field_whole_call_and_cache_writes() {
    // `delta.reasoning`, a keep-alive comment, vLLM-only null fields, one
    // call whose name and arguments share a delta, and cache writes.
    let turn = assemble_fixture(Server::Vllm, VLLM).await;
    assert_eq!(turn.model, "Qwen/Qwen3-32B");
    assert_eq!(thinking(&turn), Some("Two cities, two calls."));
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].id, "chatcmpl-tool-a1");
    assert_eq!(calls[0].input, *json!({"city": "Hue"}).as_object().unwrap());
    assert_eq!(
        calls[1].input,
        *json!({"city": "Vinh"}).as_object().unwrap()
    );
    assert_eq!(
        turn.usage.unwrap(),
        Usage {
            input_tokens: 12,
            output_tokens: 60,
            cache_read_tokens: 256,
            cache_creation_tokens: 32,
        }
    );
}

#[tokio::test]
async fn vllm_error_frame_before_done_keeps_its_status() {
    let sse = "data: {\"error\":{\"message\":\"Internal server error\",\"type\":\"InternalServerError\",\"param\":null,\"code\":500}}\n\ndata: [DONE]\n\n";
    let profile = Server::Vllm.profile();
    let translator = Translator::new("m".into(), profile.reasoning_field, profile.usage);
    let results: Vec<_> =
        stream::events(bytes(sse, 64), translator, CancellationToken::new(), None)
            .collect()
            .await;
    assert!(
        matches!(
            results.as_slice(),
            [Err(ProviderError::Status { status: 500, .. })]
        ),
        "{results:?}"
    );
}

#[tokio::test]
async fn sglang_nulls_sglext_chunk_and_cache_report() {
    // `reasoning_content: null` and explicit nulls on every delta, a
    // separate finish chunk, an sglext chunk with no choices, then usage.
    let turn = assemble_fixture(Server::SgLang, SGLANG).await;
    assert_eq!(turn.model, "Qwen/Qwen3-8B");
    assert_eq!(thinking(&turn), Some("The user wants the weather."));
    assert_eq!(turn.message.text(), "");
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let call = turn.message.tool_uses().next().unwrap();
    assert_eq!(call.id, "call_0f1e2d3c4b5a69788796a5b4");
    assert_eq!(call.input, *json!({"city": "Hanoi"}).as_object().unwrap());
    assert_eq!(
        turn.usage.unwrap(),
        Usage {
            input_tokens: 114,
            output_tokens: 52,
            cache_read_tokens: 96,
            cache_creation_tokens: 0,
        }
    );
}

#[tokio::test]
async fn llamacpp_ping_growing_name_and_timings() {
    // An SSE ping comment, a tool name re-sent longer, and `timings` on the
    // usage chunk next to `prompt_tokens_details.cached_tokens`.
    let turn = assemble_fixture(Server::LlamaCpp, LLAMACPP).await;
    assert_eq!(turn.model, "gpt-oss-20b");
    assert_eq!(thinking(&turn), Some("Need the weather tool."));
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].name, "get_weather");
    assert_eq!(calls[0].id, "Rk3uQ0aZ1pL8mN2bV7cX4sD6fG9hJ5kT");
    assert_eq!(
        calls[0].input,
        *json!({"city": "Da Nang"}).as_object().unwrap()
    );
    assert_eq!(turn.usage.unwrap().input_tokens, 64);
    assert_eq!(turn.usage.unwrap().cache_read_tokens, 448);
}

#[tokio::test]
async fn llamacpp_error_frame_without_done_keeps_its_status() {
    let profile = Server::LlamaCpp.profile();
    let translator = Translator::new("m".into(), profile.reasoning_field, profile.usage);
    let mut results: Vec<_> = stream::events(
        bytes(LLAMACPP_ERROR, 13),
        translator,
        CancellationToken::new(),
        None,
    )
    .collect()
    .await;
    let last = results.pop().unwrap().unwrap_err();
    assert!(results.iter().all(Result::is_ok));
    assert!(
        matches!(&last, ProviderError::Status { status: 500, message, .. }
            if message == "stream error: server_error: slot unavailable"),
        "{last:?}"
    );
    assert!(last.retryable());
}

#[tokio::test]
async fn reasoning_in_the_other_field_is_not_read() {
    // SGLang and llama.cpp never send `delta.reasoning`; vLLM never sends
    // `reasoning_content`. Each profile reads only its own field.
    let swapped = SGLANG.replace("\"reasoning_content\":\"", "\"reasoning\":\"");
    let turn = assemble_fixture(Server::SgLang, &swapped).await;
    assert_eq!(thinking(&turn), None);
}
