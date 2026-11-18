//! Live vLLM model list, capabilities and reasoning effort. Skips when
//! `LINCH_TEST_VLLM_URL` (the server's `/v1` URL) is unset;
//! `LINCH_TEST_VLLM_MODEL` names the model it serves.
//!
//! `LINCH_TEST_VLLM_URL=http://127.0.0.1:8201/v1 LINCH_TEST_VLLM_MODEL=qwen3.6-35b-a3b \
//!  cargo test -p linch-providers --test live_vllm -- --nocapture`

use futures::StreamExt;
use linch_core::provider::Thinking;
use linch_core::{
    Assembler, ContextSource, Message, OutputSchemaMode, Provider, ProviderRequest, ReasoningEffort,
};
use linch_providers::{OpenAiChat, Server, ServerOptions};
use tokio_util::sync::CancellationToken;

fn vllm(test: &str) -> Option<(OpenAiChat, String)> {
    let Ok(url) = std::env::var("LINCH_TEST_VLLM_URL") else {
        eprintln!("{test}: skipped, LINCH_TEST_VLLM_URL is not set");
        return None;
    };
    let model = std::env::var("LINCH_TEST_VLLM_MODEL").unwrap_or_else(|_| "qwen3.6-35b-a3b".into());
    let options = ServerOptions::new(Server::Vllm, url);
    Some((
        OpenAiChat::server(options).expect("HTTP client builds"),
        model,
    ))
}

#[tokio::test]
async fn a_live_server_lists_its_model_with_the_window() {
    let Some((provider, model)) = vllm("a_live_server_lists_its_model_with_the_window") else {
        return;
    };
    let models = provider.list_models().await.expect("the list");
    eprintln!("list: {models:?}");
    let info = provider.model_info(&model).await.expect("the model");
    eprintln!("info: {info:?}");
    assert_eq!(info.id, model);
    assert!(info.context_window.is_some_and(|window| window > 0));
}

#[tokio::test]
async fn a_live_servers_capabilities_name_its_window_as_the_servers() {
    let Some((provider, model)) = vllm("a_live_servers_capabilities_name_its_window") else {
        return;
    };
    provider.prepare().await;
    let caps = provider.capabilities(&model);
    eprintln!("capabilities: {}", serde_json::to_string(&caps).unwrap());
    assert_eq!(caps.context_source, ContextSource::Server);
    let info = provider.model_info(&model).await.expect("the model");
    assert_eq!(Some(caps.context_window), info.context_window);
    // The server takes a `response_format`, but with tools in the same request
    // its guided decoding binds the answer to the schema and no tool is
    // called, so a run offers its final tool instead.
    assert_eq!(caps.output_schema, OutputSchemaMode::FinalTool);
    assert!(caps.declared);
}

/// Wraps a provider and records when each call began and when its stream
/// ended, as offsets from `origin`.
struct Spans<P> {
    inner: P,
    origin: std::time::Instant,
    spans: std::sync::Arc<std::sync::Mutex<Vec<(std::time::Duration, std::time::Duration)>>>,
}

#[async_trait::async_trait]
impl<P: Provider> Provider for Spans<P> {
    fn id(&self) -> &str {
        self.inner.id()
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<linch_core::ProviderStream, linch_core::ProviderError> {
        let begun = self.origin.elapsed();
        let index = {
            let mut spans = self.spans.lock().unwrap();
            spans.push((begun, begun));
            spans.len() - 1
        };
        let (spans, origin) = (self.spans.clone(), self.origin);
        let stream = self.inner.stream(request, cancel).await?;
        Ok(stream
            .inspect(move |_| spans.lock().unwrap()[index].1 = origin.elapsed())
            .boxed())
    }
}

/// Two runs at once through an agent whose gate holds `cap` calls; the
/// recorded spans of the two provider calls.
async fn two_runs(
    provider: OpenAiChat,
    model: &str,
    cap: Option<u32>,
) -> Vec<(std::time::Duration, std::time::Duration)> {
    use std::sync::Arc;

    use linch_core::{
        AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
        PermissionMode, PermissionPolicy, ProviderGate, RunOptions, Thinking, ToolRegistry,
    };

    let spans = Arc::new(std::sync::Mutex::new(Vec::new()));
    let provider = Spans {
        inner: provider,
        origin: std::time::Instant::now(),
        spans: spans.clone(),
    };
    let mut spec = AgentSpec::new(model);
    spec.system_prompt = "Answer with one word.".into();
    spec.max_turns = Some(1);
    spec.max_output_tokens = Some(64);
    spec.thinking = Some(Thinking::Disabled {});
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let mut builder = AgentBuilder::new(spec)
        .provider(Arc::new(provider))
        .tools(Arc::new(ToolRegistry::new()))
        .permissions(PermissionEngine::new(policy).expect("valid policy"))
        .run_store(Arc::new(MemoryRunStore::new()))
        .session_store(Arc::new(MemorySessionStore::new()))
        .namespace("live-gate");
    if let Some(cap) = cap {
        builder = builder.provider_gate(ProviderGate::new(cap.try_into().unwrap()));
    }
    let agent = builder.build().expect("valid agent");
    let mut runs = Vec::new();
    for session in ["a", "b"] {
        let mut stream = agent.run("Name a colour.", RunOptions::new(session));
        runs.push(tokio::spawn(async move {
            while let Some(item) = stream.next().await {
                item.expect("the run continues durably");
            }
        }));
    }
    for run in runs {
        run.await.unwrap();
    }
    let spans = spans.lock().unwrap().clone();
    eprintln!("spans (begin, end): {spans:?}");
    spans
}

/// With a gate of one, two concurrent runs make their provider calls one
/// after the other (two requests of at most 64 tokens).
#[tokio::test]
async fn a_gate_of_one_serialises_two_live_runs() {
    let Some((provider, model)) = vllm("a_gate_of_one_serialises_two_live_runs") else {
        return;
    };
    let mut spans = two_runs(provider, &model, Some(1)).await;
    spans.sort();
    assert_eq!(spans.len(), 2);
    assert!(spans[0].1 <= spans[1].0, "the calls overlapped: {spans:?}");
}

/// Every effort level is accepted by a Qwen3 template, `high` and `max`
/// included: they go out as `xhigh`.
#[tokio::test]
async fn every_effort_level_is_accepted() {
    let Some((provider, model)) = vllm("every_effort_level_is_accepted") else {
        return;
    };
    for effort in [
        ReasoningEffort::Low,
        ReasoningEffort::Medium,
        ReasoningEffort::High,
        ReasoningEffort::Xhigh,
        ReasoningEffort::Max,
    ] {
        let request = ProviderRequest {
            model: model.clone(),
            system: vec![],
            tools: vec![],
            appended_tools: vec![],
            messages: vec![Message::user_text("Say hi.")],
            context_tail: 0,
            max_output_tokens: Some(16),
            temperature: None,
            stop_sequences: vec![],
            effort: Some(effort),
            thinking: Some(Thinking::Enabled {
                budget_tokens: None,
                display: None,
            }),
            cache_control: None,
            output_schema: None,
            tool_choice: None,
        };
        let mut stream = provider
            .stream(request, CancellationToken::new())
            .await
            .unwrap_or_else(|error| panic!("{effort:?} refused: {error}"));
        let mut assembler = Assembler::new();
        while let Some(event) = stream.next().await {
            assembler
                .push(event.unwrap_or_else(|error| panic!("{effort:?}: {error}")))
                .expect("valid stream");
        }
        let turn = assembler.finish().expect("complete stream");
        eprintln!(
            "{effort:?}: stop={:?} usage={:?}",
            turn.stop_reason, turn.usage
        );
    }
}
