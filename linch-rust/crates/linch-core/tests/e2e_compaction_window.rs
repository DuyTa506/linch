//! `compaction.threshold_tokens` against the context window: an agent whose
//! trigger sits at or above a known window does not build, since the
//! provider refuses the prompt before the trigger can fire.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, BuildError, CompactionSpec, PermissionMode, ScriptedProvider,
};

fn provider(window: Option<u64>) -> Arc<ScriptedProvider> {
    let turn = TurnBuilder::new(MODEL)
        .text("ok")
        .usage(usage(1, 1))
        .build();
    let provider = ScriptedProvider::new([turn]);
    Arc::new(match window {
        Some(window) => provider.with_context_window(window),
        None => provider,
    })
}

fn build(compaction: Option<CompactionSpec>, window: Option<u64>) -> Result<(), BuildError> {
    let stores = Arc::new(Stores::default());
    let spec = AgentSpec {
        compaction,
        ..spec()
    };
    AgentBuilder::new(spec)
        .provider(provider(window))
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .map(|_| ())
}

#[test]
fn a_trigger_at_or_above_a_known_window_does_not_build() {
    for threshold in [8000, 8001, 1_000_000] {
        let error = build(Some(CompactionSpec::new(threshold)), Some(8000)).expect_err("built");
        let want = format!(
            "compaction.threshold_tokens ({threshold}) must be below the context window (8000)"
        );
        assert!(
            matches!(&error, BuildError::Invalid(reason) if reason.contains(&want)),
            "{threshold}: {error:?}"
        );
    }
}

#[test]
fn a_trigger_below_a_known_window_builds() {
    assert!(build(Some(CompactionSpec::new(7999)), Some(8000)).is_ok());
    assert!(build(Some(CompactionSpec::new(1)), Some(8000)).is_ok());
}

#[test]
fn an_unknown_window_is_not_checked() {
    // The provider's default window is a guess, so a trigger above it may
    // still be below the model's real one.
    assert!(build(Some(CompactionSpec::new(1_000_000)), None).is_ok());
}

#[test]
fn the_window_derived_trigger_needs_no_check() {
    assert!(build(Some(CompactionSpec::default()), Some(8000)).is_ok());
    assert!(build(None, Some(8000)).is_ok());
}
