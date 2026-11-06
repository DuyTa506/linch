//! Workflow tests on the memory journal store, plus shared fixtures.

mod agent;
mod policy;
mod resume;
mod store;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use linch_core::events::kinds::{WorkflowEvent, WorkflowKind};

use super::*;

pub const NS: &str = "wf";

/// Workflow events in emission order.
#[derive(Clone, Default)]
pub struct Recorder(Arc<Mutex<Vec<WorkflowEvent>>>);

impl Recorder {
    pub fn kinds(&self) -> Vec<WorkflowKind> {
        self.events().iter().map(|event| event.kind).collect()
    }

    pub fn events(&self) -> Vec<WorkflowEvent> {
        self.0.lock().unwrap().clone()
    }
}

/// Options for `id` on `store`, recording events into `recorder`.
pub fn options(store: &Arc<MemoryJournalStore>, id: &str, recorder: &Recorder) -> WorkflowOptions {
    let mut options = WorkflowOptions::new(store.clone(), NS, id);
    let events = recorder.0.clone();
    options.on_event = Some(Arc::new(move |event: &WorkflowEvent| {
        events.lock().unwrap().push(event.clone());
    }));
    options
}

/// How many times a step function ran.
#[derive(Clone, Default)]
pub struct Counter(Arc<AtomicUsize>);

impl Counter {
    pub fn bump(&self) -> usize {
        self.0.fetch_add(1, Ordering::SeqCst) + 1
    }

    pub fn get(&self) -> usize {
        self.0.load(Ordering::SeqCst)
    }
}

/// The stored run of `id`.
pub async fn stored(store: &MemoryJournalStore, id: &str) -> WorkflowRun {
    store
        .load(NS, id)
        .await
        .unwrap()
        .expect("the workflow exists")
}
