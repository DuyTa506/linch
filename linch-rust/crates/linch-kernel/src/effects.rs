//! Reversible effects: the teardown primitive the whole kernel is built on.
//!
//! A [`Disposable`] runs one teardown attempt at a time. Successful teardown is
//! final; a failed attempt is reported to every waiter and may be retried. An
//! [`EffectScope`] collects disposers and unwinds them in reverse registration
//! order. Port of `src/linch/kernel/effects.py`.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex, Weak};

use futures::FutureExt;
use futures::future::{BoxFuture, Shared};

use crate::error::KernelError;

type TeardownFn = Arc<dyn Fn() -> BoxFuture<'static, Result<(), KernelError>> + Send + Sync>;
type Attempt = Shared<BoxFuture<'static, Result<(), KernelError>>>;

/// An idempotent, async-aware teardown handle.
///
/// Every `register`/`on`/`effect` call hands one back so that contribution can
/// be reversed. Clones share the same handle. Concurrent `dispose` calls
/// coalesce into one attempt that runs on its own task, so cancelling one
/// waiter never cancels the shared cleanup.
///
/// ```
/// # tokio::runtime::Runtime::new().unwrap().block_on(async {
/// use linch_kernel::Disposable;
/// let d = Disposable::sync(|| Ok(()));
/// d.dispose().await.unwrap();
/// d.dispose().await.unwrap(); // a no-op
/// assert!(d.is_disposed());
/// # });
/// ```
#[derive(Clone)]
pub struct Disposable(Arc<DisposableInner>);

struct DisposableInner {
    state: Mutex<DisposableState>,
}

struct DisposableState {
    teardown: Option<TeardownFn>,
    disposed: bool,
    attempt: Option<Attempt>,
    scopes: Vec<Weak<ScopeInner>>,
}

impl Disposable {
    /// Create a handle around an async teardown. The closure runs once per
    /// attempt; it runs again only if the previous attempt failed.
    pub fn new<F, Fut>(teardown: F) -> Self
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), KernelError>> + Send + 'static,
    {
        Self::from_fn(Some(Arc::new(move || teardown().boxed())))
    }

    /// Create a handle around a synchronous teardown.
    pub fn sync<F>(teardown: F) -> Self
    where
        F: Fn() -> Result<(), KernelError> + Send + Sync + 'static,
    {
        let teardown = Arc::new(teardown);
        Self::new(move || {
            let teardown = teardown.clone();
            async move { teardown() }
        })
    }

    /// A handle with nothing to undo.
    pub fn noop() -> Self {
        Self::from_fn(None)
    }

    fn from_fn(teardown: Option<TeardownFn>) -> Self {
        Self(Arc::new(DisposableInner {
            state: Mutex::new(DisposableState {
                teardown,
                disposed: false,
                attempt: None,
                scopes: Vec::new(),
            }),
        }))
    }

    /// Whether teardown has completed successfully.
    pub fn is_disposed(&self) -> bool {
        self.state().disposed
    }

    /// Run teardown once, coalescing concurrent callers.
    ///
    /// # Errors
    ///
    /// Returns the current attempt's failure to every waiter; the handle stays
    /// retryable.
    pub async fn dispose(&self) -> Result<(), KernelError> {
        let attempt = {
            let mut state = self.state();
            if state.disposed {
                return Ok(());
            }
            match &state.attempt {
                Some(attempt) => attempt.clone(),
                None => {
                    let attempt = spawn_attempt(self.clone());
                    state.attempt = Some(attempt.clone());
                    attempt
                }
            }
        };
        attempt.await
    }

    /// Stable identity of this handle (shared by its clones).
    pub(crate) fn id(&self) -> usize {
        Arc::as_ptr(&self.0) as usize
    }

    fn state(&self) -> std::sync::MutexGuard<'_, DisposableState> {
        self.0
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    async fn run_teardown(self) -> Result<(), KernelError> {
        let teardown = self.state().teardown.clone();
        let result = match teardown {
            Some(teardown) => teardown().await,
            None => Ok(()),
        };
        let scopes = {
            let mut state = self.state();
            state.attempt = None;
            result?;
            state.disposed = true;
            state.teardown = None; // release captured state after success
            std::mem::take(&mut state.scopes)
        };
        for scope in scopes.iter().filter_map(Weak::upgrade) {
            scope.discard(self.id());
        }
        Ok(())
    }
}

/// Start one shielded teardown attempt on its own task.
fn spawn_attempt(handle: Disposable) -> Attempt {
    let task = tokio::spawn(handle.run_teardown());
    async move {
        task.await
            .unwrap_or_else(|join| Err(KernelError::msg(format!("teardown task failed: {join}"))))
    }
    .boxed()
    .shared()
}

/// What an [`EffectScope::effect`] callback may hand back for teardown.
pub trait IntoDisposers {
    /// Convert into the ordered group of disposers to own.
    fn into_disposers(self) -> Vec<Disposable>;
}

impl IntoDisposers for () {
    fn into_disposers(self) -> Vec<Disposable> {
        Vec::new()
    }
}

impl IntoDisposers for Disposable {
    fn into_disposers(self) -> Vec<Disposable> {
        vec![self]
    }
}

impl IntoDisposers for Option<Disposable> {
    fn into_disposers(self) -> Vec<Disposable> {
        self.into_iter().collect()
    }
}

impl IntoDisposers for Vec<Disposable> {
    fn into_disposers(self) -> Vec<Disposable> {
        self
    }
}

/// An ordered collection of [`Disposable`]s unwound in reverse.
///
/// Clones share the same scope.
#[derive(Clone)]
pub struct EffectScope(Arc<ScopeInner>);

struct ScopeInner {
    state: Mutex<ScopeState>,
    teardown: Disposable,
}

#[derive(Default)]
struct ScopeState {
    closed: bool,
    next_seq: u64,
    order: BTreeMap<u64, Disposable>,
    seq_by_id: HashMap<usize, u64>,
}

impl Default for EffectScope {
    fn default() -> Self {
        Self::new()
    }
}

impl EffectScope {
    /// Create an empty, open scope.
    pub fn new() -> Self {
        Self(Arc::new_cyclic(|weak: &Weak<ScopeInner>| {
            let weak = weak.clone();
            ScopeInner {
                state: Mutex::new(ScopeState::default()),
                teardown: Disposable::new(move || {
                    let weak = weak.clone();
                    async move {
                        match weak.upgrade() {
                            Some(scope) => scope.dispose_all().await,
                            None => Ok(()),
                        }
                    }
                }),
            }
        }))
    }

    /// Whether teardown has begun and new effects are rejected.
    pub fn is_closed(&self) -> bool {
        self.0.state().closed
    }

    /// Whether every owned disposer has completed successfully.
    pub fn is_disposed(&self) -> bool {
        self.0.teardown.is_disposed()
    }

    /// Fail with [`KernelError::Closed`] once teardown has begun.
    pub fn ensure_active(&self) -> Result<(), KernelError> {
        if self.is_closed() {
            Err(KernelError::Closed)
        } else {
            Ok(())
        }
    }

    /// Track a disposer so this scope owns its teardown.
    ///
    /// # Errors
    ///
    /// [`KernelError::Closed`] once teardown has begun.
    pub fn add(&self, disposer: Disposable) -> Result<Disposable, KernelError> {
        self.ensure_active()?;
        self.track(&disposer);
        Ok(disposer)
    }

    /// Run `setup` now and own the teardown it returns.
    ///
    /// The returned composite disposes only this effect's group, in reverse.
    ///
    /// # Errors
    ///
    /// [`KernelError::Closed`] once teardown has begun; `setup` is not called.
    pub fn effect<F, R>(&self, setup: F) -> Result<Disposable, KernelError>
    where
        F: FnOnce() -> R,
        R: IntoDisposers,
    {
        self.ensure_active()?;
        let group = setup().into_disposers();
        for disposer in &group {
            self.track(disposer);
        }
        let group = Arc::new(group);
        Ok(Disposable::new(move || {
            dispose_reverse(group.as_ref().clone())
        }))
    }

    /// Close the scope and dispose every owned disposer in reverse order.
    ///
    /// # Errors
    ///
    /// The collected failures; failed entries stay owned for a later retry.
    pub async fn dispose(&self) -> Result<(), KernelError> {
        self.close();
        self.0.teardown.dispose().await
    }

    /// Reject new registrations without starting teardown.
    pub(crate) fn close(&self) {
        self.0.state().closed = true;
    }

    fn track(&self, disposer: &Disposable) {
        if disposer.is_disposed() {
            return;
        }
        {
            let mut state = self.0.state();
            if state.seq_by_id.contains_key(&disposer.id()) {
                return;
            }
            let seq = state.next_seq;
            state.next_seq += 1;
            state.order.insert(seq, disposer.clone());
            state.seq_by_id.insert(disposer.id(), seq);
        }
        disposer.state().scopes.push(Arc::downgrade(&self.0));
    }

    #[cfg(test)]
    fn tracked(&self) -> usize {
        self.0.state().order.len()
    }
}

impl ScopeInner {
    fn state(&self) -> std::sync::MutexGuard<'_, ScopeState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn discard(&self, id: usize) {
        let mut state = self.state();
        if let Some(seq) = state.seq_by_id.remove(&id) {
            state.order.remove(&seq);
        }
    }

    async fn dispose_all(&self) -> Result<(), KernelError> {
        // Successful entries remove themselves; failed ones stay for a retry.
        let snapshot: Vec<Disposable> = self.state().order.values().cloned().collect();
        dispose_reverse(snapshot).await
    }
}

/// Dispose `disposers` last-to-first, attempting every one before failing.
pub(crate) async fn dispose_reverse(disposers: Vec<Disposable>) -> Result<(), KernelError> {
    let mut errors = Vec::new();
    for disposer in disposers.iter().rev() {
        if let Err(error) = disposer.dispose().await {
            errors.push(error);
        }
    }
    KernelError::collect(errors)
}

impl std::fmt::Debug for Disposable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Disposable")
            .field("disposed", &self.is_disposed())
            .finish()
    }
}

impl std::fmt::Debug for EffectScope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EffectScope")
            .field("closed", &self.is_closed())
            .field("disposed", &self.is_disposed())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[tokio::test]
    async fn scope_forgets_disposers_by_identity_after_success() {
        let scope = EffectScope::new();
        let first = scope.add(Disposable::noop()).unwrap();
        let _second = scope.add(Disposable::noop()).unwrap();
        assert_eq!(scope.tracked(), 2);
        first.dispose().await.unwrap();
        assert_eq!(scope.tracked(), 1);
    }

    #[tokio::test]
    async fn scope_drops_disposed_entries_after_repeated_cycles() {
        let scope = EffectScope::new();
        for i in 0..100 {
            let disposer = if i % 2 == 0 {
                scope.effect(Disposable::noop).unwrap()
            } else {
                scope.add(Disposable::noop()).unwrap()
            };
            disposer.dispose().await.unwrap();
        }
        assert_eq!(scope.tracked(), 0);
    }

    #[tokio::test]
    async fn adding_the_same_handle_twice_tracks_it_once() {
        let scope = EffectScope::new();
        let d = Disposable::noop();
        scope.add(d.clone()).unwrap();
        scope.add(d).unwrap();
        assert_eq!(scope.tracked(), 1);
    }

    proptest! {
        /// Any mix of effects unwinds in exact reverse registration order, once.
        #[test]
        fn teardown_is_reverse_ordered_and_runs_once(sizes in prop::collection::vec(0usize..4, 0..12)) {
            let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
            runtime.block_on(async {
                let log = Arc::new(Mutex::new(Vec::new()));
                let scope = EffectScope::new();
                let mut expected = Vec::new();
                let mut next = 0;
                for size in sizes {
                    let ids: Vec<usize> = (next..next + size).collect();
                    next += size;
                    expected.extend(ids.iter().copied());
                    let log = log.clone();
                    scope.effect(move || {
                        ids.into_iter().map(|id| {
                            let log = log.clone();
                            Disposable::sync(move || { log.lock().unwrap().push(id); Ok(()) })
                        }).collect::<Vec<_>>()
                    }).unwrap();
                }
                scope.dispose().await.unwrap();
                scope.dispose().await.unwrap();
                expected.reverse();
                prop_assert_eq!(log.lock().unwrap().clone(), expected);
                Ok(())
            })?;
        }
    }
}
