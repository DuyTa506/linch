//! `spec.call_timeout_ms`: a timeout of zero would end every tool call at
//! once, and one under 100 ms ends calls before a tool can start, so an
//! agent with either does not build.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{AgentBuilder, AgentSpec, BuildError, PermissionMode, ScriptedProvider};

fn build(call_timeout_ms: Option<u64>) -> Result<linch_core::Agent, BuildError> {
    let mut spec = AgentSpec::new(MODEL);
    spec.call_timeout_ms = call_timeout_ms;
    let stores = Arc::new(Stores::default());
    let turn = TurnBuilder::new(MODEL)
        .text("ok")
        .usage(usage(1, 1))
        .build();
    AgentBuilder::new(spec)
        .provider(Arc::new(ScriptedProvider::new([turn])))
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
}

#[test]
fn a_call_timeout_below_100_ms_is_refused_at_build() {
    for ms in [0, 1, 99] {
        let Err(BuildError::Invalid(reason)) = build(Some(ms)) else {
            panic!("call_timeout_ms {ms} built");
        };
        assert!(reason.contains("call_timeout_ms"), "{reason}");
        assert!(reason.contains("100"), "{reason}");
    }
}

#[test]
fn a_call_timeout_of_100_ms_or_more_and_none_build() {
    for ms in [None, Some(100), Some(300_000)] {
        assert!(build(ms).is_ok(), "call_timeout_ms {ms:?}");
    }
}
