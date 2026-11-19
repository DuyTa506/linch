//! An agent with no redaction filter and no verifier sends the requests,
//! writes the checkpoints and events, and has the contract it did before
//! either existed, byte for byte: the digest below was taken on the commit
//! before the features.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use linch_core::events::Envelope;
use linch_core::{
    AgentBuilder, Checkpoint, Commit, Lease, NewRun, PermissionMode, RunId, RunOptions, RunRecord,
    RunStore, ScriptedProvider, StoreError, ToolScope, digest,
};
use serde_json::{Value, json};

/// `value` without its `duration_ms` fields, which read the clock.
fn clockless(mut value: Value) -> Value {
    match &mut value {
        Value::Object(object) => {
            object.remove("duration_ms");
            for field in object.values_mut() {
                *field = clockless(field.take());
            }
        }
        Value::Array(items) => {
            for item in items {
                *item = clockless(item.take());
            }
        }
        _ => {}
    }
    value
}

/// The run store of [`Stores`], keeping every checkpoint it is given.
#[derive(Default)]
struct Kept {
    stores: Stores,
    checkpoints: Mutex<Vec<Checkpoint>>,
}

#[async_trait]
impl RunStore for Kept {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.checkpoints
            .lock()
            .unwrap()
            .push(run.checkpoint.clone());
        RunStore::create(&self.stores, run).await
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.stores.claim(ns, run_id, owner).await
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&self.stores, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        if let Some(checkpoint) = &commit.checkpoint {
            self.checkpoints.lock().unwrap().push(checkpoint.clone());
        }
        self.stores.commit(lease, commit).await
    }
    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.stores.events(ns, run_id, after).await
    }
}

#[tokio::test]
async fn without_guardrails_requests_checkpoints_and_events_are_unchanged() {
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Read")));
    let kept = Arc::new(Kept::default());
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = AgentBuilder::new(spec())
        .provider(provider.clone())
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        // Read calls run in every mode; the default policy keeps the
        // contract the digests were taken with.
        .permissions(policy(PermissionMode::Default))
        .run_store(kept.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    let envelopes = finish(agent.run("go", options)).await;
    let requests = provider.requests();
    let checkpoints = kept.checkpoints.lock().unwrap().clone();
    assert_eq!((requests.len(), checkpoints.len()), (2, 7));
    // Every event the caller saw, deltas included, without the clock.
    let events: Vec<_> = (envelopes.iter())
        .map(|envelope| clockless(json!({"seq": envelope.seq, "event": envelope.event})))
        .collect();
    let all = without_breakdowns(json!({
        "requests": requests,
        "checkpoints": checkpoints,
        "events": events,
        "contract": agent.contract().to_string(),
    }));
    assert_eq!(
        digest(&all).unwrap().to_string(),
        "27e20b0a3abccda3ca124c56bd7a2fed63a7efc29e6d235f11037a18d0855ef1",
        "{all:#}"
    );
}
