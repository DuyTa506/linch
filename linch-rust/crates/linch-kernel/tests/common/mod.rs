//! Shared helpers for kernel integration tests.
#![allow(dead_code)]

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use tokio::sync::Notify;

/// A one-shot latch, the analogue of `asyncio.Event` in the Python tests.
#[derive(Default)]
pub struct Gate {
    open: AtomicBool,
    notify: Notify,
}

impl Gate {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn open(&self) {
        self.open.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }

    pub fn is_open(&self) -> bool {
        self.open.load(Ordering::SeqCst)
    }

    pub async fn wait(&self) {
        loop {
            let notified = self.notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.is_open() {
                return;
            }
            notified.await;
        }
    }
}

/// An ordered, shareable log of strings.
#[derive(Clone, Default)]
pub struct Log(Arc<Mutex<Vec<String>>>);

impl Log {
    pub fn push(&self, item: impl Into<String>) {
        self.0.lock().unwrap().push(item.into());
    }

    pub fn items(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }
}

/// A shareable counter.
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

/// Yield to the scheduler a couple of times, like the Python `_settle()`.
pub async fn settle() {
    for _ in 0..4 {
        tokio::task::yield_now().await;
    }
}
