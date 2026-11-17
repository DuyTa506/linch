//! A plain agent with `filesystem.memory` and `max_provider_concurrency` on a
//! live vLLM server: one request (64 output tokens at most) carries the
//! memory context and the model reads it. Skips when `LINCH_TEST_VLLM_URL`
//! (the server's `/v1` URL) is unset; `LINCH_TEST_VLLM_MODEL` names the model.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::StreamExt;
use linch::hooks::{BeforeProviderCall, BeforeProviderCallRequest, BeforeProviderCallResponse};
use linch::{
    AgentConfig, CallbackError, Event, Hook, Hooks, PgPools, Point, RunOptions, ToolRegistry,
};
use serde_json::json;

#[derive(Default)]
struct Requests(Mutex<Vec<String>>);

#[async_trait]
impl Hook<BeforeProviderCall> for Requests {
    fn policy_id(&self) -> &str {
        "requests"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(
        &self,
        request: &BeforeProviderCallRequest,
    ) -> Result<<BeforeProviderCall as Point>::Response, CallbackError> {
        let text = serde_json::to_string(&request.request.messages).unwrap();
        self.0.lock().unwrap().push(text);
        Ok(BeforeProviderCallResponse::Continue)
    }
}

#[tokio::test]
async fn a_live_model_is_told_where_its_memories_live() {
    let Ok(url) = std::env::var("LINCH_TEST_VLLM_URL") else {
        eprintln!("skipped, LINCH_TEST_VLLM_URL is not set");
        return;
    };
    let model = std::env::var("LINCH_TEST_VLLM_MODEL").unwrap_or_else(|_| "qwen3.6-35b-a3b".into());
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "live-memory",
        "spec": {
            "model": model,
            "system_prompt": "Answer in one short line.",
            "max_turns": 1,
            "max_output_tokens": 64,
            "thinking": {"type": "disabled"},
        },
        "provider": {"id": "vllm", "base_url": url},
        "stores": {"type": "memory"},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
        "filesystem": {"memory": true},
        "max_provider_concurrency": 1,
    }))
    .unwrap();
    let requests = Arc::new(Requests::default());
    let hooks = Hooks::default();
    let _ = hooks
        .before_provider_call
        .register(requests.clone())
        .unwrap();
    let opened = config
        .open_with(
            Arc::new(ToolRegistry::new()),
            Arc::new(hooks),
            &PgPools::new(),
        )
        .await
        .unwrap();
    let mut stream = opened.agent().run(
        "In which directory do your saved memories live? Give the path only.",
        RunOptions::new("s1"),
    );
    let mut answer = String::new();
    while let Some(item) = stream.next().await {
        if let Event::Result(done) = item.unwrap().event {
            answer = done.final_text.unwrap_or_default();
        }
    }
    eprintln!("answer: {answer:?}");
    let sent = requests.0.lock().unwrap().clone();
    assert_eq!(sent.len(), 1, "one request");
    assert!(
        sent[0].contains("Memories live under /memories/"),
        "told the file exists"
    );
    assert!(
        answer.contains("/memories"),
        "the model read it: {answer:?}"
    );
}
