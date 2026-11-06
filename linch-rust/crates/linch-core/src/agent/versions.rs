//! [`Versions`]: what an agent holds that a reload replaces, with each
//! reload a reversible registration.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use crate::registration::Registration;

/// A first value and the ones reloads pushed over it: the last one is
/// current. Disposing a push's registration takes exactly that value out
/// again, so a later push stays current.
pub(crate) struct Versions<T> {
    inner: Mutex<Inner<T>>,
}

struct Inner<T> {
    /// Each live value with the id of its push, oldest first.
    stack: Vec<(u64, Arc<T>)>,
    next: u64,
    /// Bumps whenever the current value changes.
    revision: u64,
}

impl<T: Send + Sync + 'static> Versions<T> {
    /// Versions holding `first`, which no registration takes out.
    pub(crate) fn new(first: Arc<T>) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(Inner {
                stack: vec![(0, first)],
                next: 1,
                revision: 0,
            }),
        })
    }

    /// The current value.
    pub(crate) fn current(&self) -> Arc<T> {
        let inner = self.lock();
        let (_, value) = inner.stack.last().expect("the first value stays");
        Arc::clone(value)
    }

    /// A counter that moves whenever [`Self::current`] changes.
    pub(crate) fn revision(&self) -> u64 {
        self.lock().revision
    }

    /// Make `value` current; the returned registration takes it out again,
    /// bringing back the latest value still live.
    pub(crate) fn push(self: &Arc<Self>, value: Arc<T>) -> Registration {
        let id = {
            let mut inner = self.lock();
            let id = inner.next;
            inner.next += 1;
            inner.stack.push((id, value));
            inner.revision += 1;
            id
        };
        let versions = Arc::downgrade(self);
        Registration::new(move || {
            let Some(versions) = versions.upgrade() else {
                return;
            };
            let mut inner = versions.lock();
            let Some(at) = inner.stack.iter().position(|(entry, _)| *entry == id) else {
                return;
            };
            // Bind the removed value so it drops after the lock is released.
            let removed = inner.stack.remove(at);
            if at == inner.stack.len() {
                inner.revision += 1;
            }
            drop(inner);
            drop(removed);
        })
    }

    fn lock(&self) -> MutexGuard<'_, Inner<T>> {
        // Nothing panics while holding the lock.
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_push_is_current_until_its_registration_is_disposed() {
        let versions = Versions::new(Arc::new("first"));
        let second = versions.push(Arc::new("second"));
        let third = versions.push(Arc::new("third"));
        assert_eq!(*versions.current(), "third");
        let at = versions.revision();
        second.dispose();
        assert_eq!(*versions.current(), "third", "a hidden one goes quietly");
        assert_eq!(versions.revision(), at);
        third.dispose();
        third.dispose();
        assert_eq!(*versions.current(), "first");
        assert_eq!(versions.revision(), at + 1);
    }
}
