//! A typed, async-first event bus with three dispatch modes.
//!
//! - [`EventBus::emit`]: non-vetoing broadcast; a failing listener is logged and
//!   the rest still run. Sync replies run inline, async replies are spawned.
//! - [`EventBus::serial`]: await listeners in order and stop at the first one
//!   that returns a value ("bail").
//! - [`EventBus::waterfall`]: a middleware chain. Each listener receives a
//!   [`Next`] and delegates inward by running it; not running it vetoes.
//!
//! Each mode has its own point type, so dispatching a point with the wrong mode
//! does not compile. Port of `src/linch/kernel/events.py`.

use std::any::{Any, TypeId};
use std::collections::HashMap;
use std::marker::PhantomData;
use std::sync::{Arc, Mutex};

use futures::FutureExt;
use futures::future::BoxFuture;

use crate::effects::{Disposable, dispose_reverse};
use crate::error::KernelError;

/// How a lifecycle point is dispatched.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DispatchMode {
    /// Fire-and-forget broadcast.
    Emit,
    /// First listener returning a value wins.
    Serial,
    /// Outer-to-inner middleware chain.
    Waterfall,
}

/// A listener's answer: ready now, or a future to await.
pub enum Reply<R> {
    /// Finished without a value.
    Done,
    /// Finished with a value (bails a serial dispatch, ends a waterfall).
    Value(R),
    /// Failed synchronously.
    Failed(KernelError),
    /// Still running; the dispatcher awaits (or, for `emit`, spawns) it.
    Pending(BoxFuture<'static, Result<Option<R>, KernelError>>),
}

impl<R> Reply<R> {
    /// Wrap async listener work that produces no value.
    pub fn later<F>(work: F) -> Self
    where
        F: Future<Output = Result<(), KernelError>> + Send + 'static,
    {
        Reply::Pending(work.map(|result| result.map(|()| None)).boxed())
    }

    /// Wrap async listener work that may produce a value.
    pub fn later_value<F>(work: F) -> Self
    where
        F: Future<Output = Result<Option<R>, KernelError>> + Send + 'static,
    {
        Reply::Pending(work.boxed())
    }

    async fn resolve(self) -> Result<Option<R>, KernelError> {
        match self {
            Reply::Done => Ok(None),
            Reply::Value(value) => Ok(Some(value)),
            Reply::Failed(error) => Err(error),
            Reply::Pending(work) => work.await,
        }
    }
}

macro_rules! point {
    ($(#[$doc:meta])* $name:ident<$($param:ident),*>, $mode:expr) => {
        $(#[$doc])*
        pub struct $name<$($param),*> {
            name: &'static str,
            prepend: bool,
            _payload: PhantomData<fn($($param),*)>,
        }

        impl<$($param),*> $name<$($param),*> {
            /// Name a point; usable in `const` items.
            pub const fn new(name: &'static str) -> Self {
                Self { name, prepend: false, _payload: PhantomData }
            }

            /// The same point, registering listeners before existing ones.
            pub const fn prepend(&self) -> Self {
                Self { name: self.name, prepend: true, _payload: PhantomData }
            }

            /// The event name.
            pub const fn name(&self) -> &'static str {
                self.name
            }

            /// The dispatch mode this point requires.
            pub const fn mode(&self) -> DispatchMode {
                $mode
            }
        }
    };
}

point!(
    /// A broadcast point carrying payload `A`.
    EmitPoint<A>, DispatchMode::Emit
);
point!(
    /// A first-responder point carrying `A` and answering `R`.
    SerialPoint<A, R>, DispatchMode::Serial
);
point!(
    /// A middleware-chain point carrying `A` and producing `R`.
    WaterfallPoint<A, R>, DispatchMode::Waterfall
);

/// The rest of a waterfall chain. Consumed when run, so it runs at most once.
pub struct Next<R>(Box<dyn FnOnce() -> BoxFuture<'static, Result<R, KernelError>> + Send>);

impl<R: 'static> Next<R> {
    /// Wrap the built-in behavior at the center of the chain.
    pub fn new<F, Fut>(run: F) -> Self
    where
        F: FnOnce() -> Fut + Send + 'static,
        Fut: Future<Output = Result<R, KernelError>> + Send + 'static,
    {
        Self(Box::new(move || run().boxed()))
    }

    /// Delegate to the rest of the chain.
    pub async fn run(self) -> Result<R, KernelError> {
        (self.0)().await
    }
}

type CallFn<A, R> = Arc<dyn Fn(A) -> Reply<R> + Send + Sync>;
type WrapFn<A, R> = Arc<dyn Fn(A, Next<R>) -> Reply<R> + Send + Sync>;

struct Entry {
    token: u64,
    listener: Arc<dyn Any + Send + Sync>,
}

struct Bucket {
    signature: TypeId,
    entries: Vec<Entry>,
}

#[derive(Default)]
struct BusState {
    next_token: u64,
    buckets: HashMap<&'static str, Bucket>,
}

/// A name-keyed registry of listeners. Clones share the same bus.
#[derive(Clone, Default)]
pub struct EventBus(Arc<Mutex<BusState>>);

impl EventBus {
    /// Create an empty bus.
    pub fn new() -> Self {
        Self::default()
    }

    /// Register an emit listener; the handle removes exactly this registration.
    ///
    /// Use `point.prepend()` to run it before existing listeners.
    ///
    /// # Errors
    ///
    /// [`KernelError::Config`] if the name already carries another payload type.
    pub fn on<A, F>(&self, point: &EmitPoint<A>, listener: F) -> Result<Disposable, KernelError>
    where
        A: 'static,
        F: Fn(A) -> Reply<()> + Send + Sync + 'static,
    {
        self.insert::<CallFn<A, ()>>(point.name(), Arc::new(listener), point.prepend)
    }

    /// Register a serial listener.
    ///
    /// # Errors
    ///
    /// See [`EventBus::on`].
    pub fn on_serial<A, R, F>(
        &self,
        point: &SerialPoint<A, R>,
        listener: F,
    ) -> Result<Disposable, KernelError>
    where
        A: 'static,
        R: 'static,
        F: Fn(A) -> Reply<R> + Send + Sync + 'static,
    {
        self.insert::<CallFn<A, R>>(point.name(), Arc::new(listener), point.prepend)
    }

    /// Register a waterfall listener. It must run [`Next`] to delegate inward.
    ///
    /// # Errors
    ///
    /// See [`EventBus::on`].
    pub fn on_waterfall<A, R, F>(
        &self,
        point: &WaterfallPoint<A, R>,
        listener: F,
    ) -> Result<Disposable, KernelError>
    where
        A: 'static,
        R: 'static,
        F: Fn(A, Next<R>) -> Reply<R> + Send + Sync + 'static,
    {
        self.insert::<WrapFn<A, R>>(point.name(), Arc::new(listener), point.prepend)
    }

    /// Whether any listener is registered for `name`.
    pub fn has_listeners(&self, name: &str) -> bool {
        self.state()
            .buckets
            .get(name)
            .is_some_and(|b| !b.entries.is_empty())
    }

    /// Snapshot listeners into an independently mutable bus, skipping `exclude`.
    pub fn fork(&self, exclude: &[&str]) -> EventBus {
        let state = self.state();
        let mut forked = BusState::default();
        for (name, bucket) in state.buckets.iter().filter(|(n, _)| !exclude.contains(n)) {
            let entries = bucket
                .entries
                .iter()
                .map(|entry| {
                    forked.next_token += 1;
                    Entry {
                        token: forked.next_token,
                        listener: entry.listener.clone(),
                    }
                })
                .collect();
            forked.buckets.insert(
                name,
                Bucket {
                    signature: bucket.signature,
                    entries,
                },
            );
        }
        EventBus(Arc::new(Mutex::new(forked)))
    }

    /// Broadcast `payload` to every listener, isolating failures.
    ///
    /// Async replies are spawned on the current Tokio runtime; with no runtime
    /// they are dropped and a warning is logged.
    pub fn emit<A: Clone + Send + 'static>(&self, point: &EmitPoint<A>, payload: A) {
        let name = point.name();
        for listener in self.listeners::<CallFn<A, ()>>(name) {
            match listener(payload.clone()) {
                Reply::Done | Reply::Value(()) => {}
                Reply::Failed(error) => {
                    tracing::error!("emit listener for {name:?} failed: {error}")
                }
                Reply::Pending(work) => spawn_listener(name, work),
            }
        }
    }

    /// Await listeners in order and return the first value.
    ///
    /// # Errors
    ///
    /// The first listener failure.
    pub async fn serial<A, R>(
        &self,
        point: &SerialPoint<A, R>,
        payload: A,
    ) -> Result<Option<R>, KernelError>
    where
        A: Clone + 'static,
        R: 'static,
    {
        for listener in self.listeners::<CallFn<A, R>>(point.name()) {
            if let Some(value) = listener(payload.clone()).resolve().await? {
                return Ok(Some(value));
            }
        }
        Ok(None)
    }

    /// Run listeners as a middleware chain around `terminal`.
    ///
    /// The first registered listener is outermost.
    ///
    /// # Errors
    ///
    /// Any failure raised in the chain, or [`KernelError::Config`] when a
    /// listener finishes without a value.
    pub async fn waterfall<A, R>(
        &self,
        point: &WaterfallPoint<A, R>,
        payload: A,
        terminal: Next<R>,
    ) -> Result<R, KernelError>
    where
        A: Clone + Send + 'static,
        R: Send + 'static,
    {
        let listeners: Arc<[WrapFn<A, R>]> = self.listeners(point.name()).into();
        chain(point.name(), listeners, 0, payload, terminal).await
    }

    /// Snapshot a broadcast as a retryable, ordered disposable.
    ///
    /// Disposing it calls each listener in registration order and awaits its
    /// reply; a failed listener is retried on the next dispose, finished ones
    /// are not repeated.
    pub fn notification<A>(&self, point: &EmitPoint<A>, payload: A) -> Disposable
    where
        A: Clone + Send + Sync + 'static,
    {
        let calls: Vec<Disposable> = self
            .listeners::<CallFn<A, ()>>(point.name())
            .into_iter()
            .map(|listener| {
                let payload = payload.clone();
                Disposable::new(move || listener(payload.clone()).resolve().map(|r| r.map(drop)))
            })
            .collect();
        // dispose_reverse runs last-to-first; reverse so listeners run in order.
        let calls: Arc<Vec<Disposable>> = Arc::new(calls.into_iter().rev().collect());
        Disposable::new(move || dispose_reverse(calls.as_ref().clone()))
    }

    fn insert<L: Clone + Send + Sync + 'static>(
        &self,
        name: &'static str,
        listener: L,
        prepend: bool,
    ) -> Result<Disposable, KernelError> {
        let mut state = self.state();
        state.next_token += 1;
        let token = state.next_token;
        let bucket = state.buckets.entry(name).or_insert_with(|| Bucket {
            signature: TypeId::of::<L>(),
            entries: Vec::new(),
        });
        if bucket.entries.is_empty() {
            bucket.signature = TypeId::of::<L>();
        } else if bucket.signature != TypeId::of::<L>() {
            return Err(KernelError::config(format!(
                "event {name:?} is already registered with a different payload or mode"
            )));
        }
        let entry = Entry {
            token,
            listener: Arc::new(listener),
        };
        if prepend {
            bucket.entries.insert(0, entry);
        } else {
            bucket.entries.push(entry);
        }
        let bus = self.clone();
        Ok(Disposable::sync(move || {
            if let Some(bucket) = bus.state().buckets.get_mut(name) {
                bucket.entries.retain(|entry| entry.token != token);
            }
            Ok(())
        }))
    }

    fn listeners<L: Clone + 'static>(&self, name: &str) -> Vec<L> {
        let state = self.state();
        let Some(bucket) = state.buckets.get(name) else {
            return Vec::new();
        };
        bucket
            .entries
            .iter()
            .filter_map(|e| e.listener.downcast_ref::<L>().cloned())
            .collect()
    }

    fn state(&self) -> std::sync::MutexGuard<'_, BusState> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn chain<A, R>(
    name: &'static str,
    listeners: Arc<[WrapFn<A, R>]>,
    index: usize,
    payload: A,
    terminal: Next<R>,
) -> BoxFuture<'static, Result<R, KernelError>>
where
    A: Clone + Send + 'static,
    R: Send + 'static,
{
    async move {
        let Some(listener) = listeners.get(index).cloned() else {
            return terminal.run().await;
        };
        let rest = {
            let (listeners, payload) = (listeners.clone(), payload.clone());
            Next::new(move || chain(name, listeners, index + 1, payload, terminal))
        };
        listener(payload, rest).resolve().await?.ok_or_else(|| {
            KernelError::config(format!("waterfall listener for {name:?} returned no value"))
        })
    }
    .boxed()
}

fn spawn_listener(name: &'static str, work: BoxFuture<'static, Result<Option<()>, KernelError>>) {
    match tokio::runtime::Handle::try_current() {
        Ok(handle) => {
            handle.spawn(async move {
                if let Err(error) = work.await {
                    tracing::error!("async emit listener for {name:?} failed: {error}");
                }
            });
        }
        Err(_) => tracing::warn!("emit listener for {name:?} returned async work with no runtime"),
    }
}
