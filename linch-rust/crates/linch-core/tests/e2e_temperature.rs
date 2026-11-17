//! `spec.temperature`: it goes with every request, 0 included, and an
//! agent whose value the provider cannot take does not build.

mod common;

use std::sync::Arc;

use async_trait::async_trait;
use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, BuildError, PermissionMode, Provider, ProviderError, ProviderRequest,
    ProviderStream, RunOptions, ScriptedProvider,
};
use tokio_util::sync::CancellationToken;

/// A scripted provider that takes temperatures up to `limit`.
struct Limited {
    inner: Arc<ScriptedProvider>,
    limit: f64,
}

#[async_trait]
impl Provider for Limited {
    fn id(&self) -> &str {
        "limited"
    }

    fn max_temperature(&self) -> f64 {
        self.limit
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.inner.stream(request, cancel).await
    }
}

fn builder(spec: AgentSpec, provider: Arc<dyn Provider>) -> AgentBuilder {
    let stores = Arc::new(Stores::default());
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
}

fn scripted() -> Arc<ScriptedProvider> {
    let turn = TurnBuilder::new(MODEL)
        .text("ok")
        .usage(usage(1, 1))
        .build();
    Arc::new(ScriptedProvider::new([turn.clone(), turn]))
}

fn with_temperature(temperature: Option<f64>) -> AgentSpec {
    AgentSpec {
        temperature,
        ..spec()
    }
}

#[tokio::test]
async fn the_temperature_goes_with_every_request_zero_included() {
    for temperature in [None, Some(0.0), Some(0.7), Some(2.0)] {
        let provider = scripted();
        let agent = builder(with_temperature(temperature), provider.clone())
            .build()
            .unwrap();
        finish(agent.run("hi", RunOptions::new("s1"))).await;
        let requests = provider.requests();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].temperature, temperature);
    }
}

#[tokio::test]
async fn a_temperature_outside_zero_to_two_does_not_build() {
    for temperature in [-0.1, 2.1, f64::NAN, f64::INFINITY] {
        let error = match builder(with_temperature(Some(temperature)), scripted()).build() {
            Ok(_) => panic!("{temperature} built"),
            Err(error) => error,
        };
        assert!(
            matches!(&error, BuildError::Invalid(reason) if reason.contains("temperature")),
            "{temperature}: {error:?}"
        );
    }
}

#[tokio::test]
async fn a_temperature_past_the_providers_own_limit_does_not_build() {
    let limited = |limit| {
        Arc::new(Limited {
            inner: scripted(),
            limit,
        })
    };
    assert!(
        builder(with_temperature(Some(1.0)), limited(1.0))
            .build()
            .is_ok()
    );
    let error = match builder(with_temperature(Some(1.5)), limited(1.0)).build() {
        Ok(_) => panic!("1.5 built on a provider that stops at 1"),
        Err(error) => error,
    };
    assert!(
        matches!(&error, BuildError::Invalid(reason)
            if reason.contains("limited") && reason.contains("at most 1")),
        "{error:?}"
    );
}
