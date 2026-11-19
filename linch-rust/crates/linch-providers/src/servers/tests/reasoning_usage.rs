//! Reasoning tokens in a server's usage object. Linch has no reasoning bucket:
//! `completion_tokens` already includes them, so both servers' shapes must
//! parse and give the same `output_tokens`. The counts are from a recorded
//! SGLang effort run (`completion_tokens` 217, `reasoning_tokens` 213); the
//! envelopes are constructed, not recorded wire bytes.

use linch_core::Usage;

use super::{Server, assemble_fixture};

/// SGLang: the count sits at `usage.reasoning_tokens` and
/// `completion_tokens_details` is null.
const SGLANG: &str = "data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"hmm\"},\"finish_reason\":null}]}\n\n\
data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n\
data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[],\"usage\":{\"prompt_tokens\":307,\"total_tokens\":524,\"completion_tokens\":217,\"prompt_tokens_details\":{\"cached_tokens\":256},\"reasoning_tokens\":213,\"completion_tokens_details\":null}}\n\n\
data: [DONE]\n\n";

/// vLLM: the count sits at `usage.completion_tokens_details.reasoning_tokens`.
const VLLM: &str = "data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"reasoning\":\"hmm\"},\"finish_reason\":null}]}\n\n\
data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\n\
data: {\"id\":\"c\",\"model\":\"m\",\"choices\":[],\"usage\":{\"prompt_tokens\":307,\"total_tokens\":524,\"completion_tokens\":217,\"prompt_tokens_details\":{\"cached_tokens\":256},\"completion_tokens_details\":{\"reasoning_tokens\":213}}}\n\n\
data: [DONE]\n\n";

#[tokio::test]
async fn both_reasoning_count_shapes_give_the_same_usage() {
    let expected = Usage {
        input_tokens: 51,
        output_tokens: 217,
        cache_read_tokens: 256,
        cache_creation_tokens: 0,
    };
    for (server, sse) in [(Server::SgLang, SGLANG), (Server::Vllm, VLLM)] {
        let turn = assemble_fixture(server, sse).await;
        assert_eq!(turn.usage, Some(expected), "{server:?}");
    }
}
