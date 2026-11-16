//! An agent without an output schema sends the requests and writes the
//! checkpoints it did before structured output existed, byte for byte: the
//! digest below was taken on the commit before the feature.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use linch_core::events::Envelope;
use linch_core::{
    AgentBuilder, Checkpoint, Commit, Lease, NewRun, PermissionMode, RunId, RunOptions, RunRecord,
    RunStore, ScriptedProvider, StoreError, ToolScope, digest,
};
use serde_json::json;

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
async fn without_a_schema_requests_and_checkpoints_are_unchanged() {
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Read")));
    let kept = Arc::new(Kept::default());
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = AgentBuilder::new(spec())
        .provider(provider.clone())
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(kept.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    finish(agent.run("go", options)).await;
    let requests = provider.requests();
    let checkpoints = kept.checkpoints.lock().unwrap().clone();
    assert_eq!((requests.len(), checkpoints.len()), (2, 7));
    let both = json!({"requests": requests, "checkpoints": checkpoints});
    assert_eq!(
        digest(&both).unwrap().to_string(),
        "cf4547065b65e697910d8f1461c0cf0e4ebb679c02a6a99991b15811f2d54418",
        "{both:#}"
    );
}
