//! Reversible registrations.
//!
//! Every registry operation that adds something returns a [`Registration`].
//! Calling [`Registration::dispose`] undoes exactly that registration; a second
//! call does nothing. Dropping the handle does not undo anything, so a caller
//! that never disposes keeps the registration for the registry's lifetime.

use std::fmt;
use std::sync::Mutex;

type Undo = Box<dyn FnOnce() + Send>;

/// Handle that undoes one registration.
///
/// The undo closure belongs to the registry that created the handle. It must
/// remove only what this registration added, so it never removes a later
/// replacement under the same name.
#[must_use = "keep the handle to undo the registration later"]
pub struct Registration {
    undo: Mutex<Option<Undo>>,
}

impl Registration {
    /// Handle that runs `undo` on the first [`Self::dispose`].
    pub fn new(undo: impl FnOnce() + Send + 'static) -> Self {
        Self {
            undo: Mutex::new(Some(Box::new(undo))),
        }
    }

    /// Handle whose dispose does nothing (an already-undone registration).
    pub fn noop() -> Self {
        Self {
            undo: Mutex::new(None),
        }
    }

    /// Undo the registration. Idempotent.
    pub fn dispose(&self) {
        let undo = self
            .undo
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(undo) = undo {
            undo();
        }
    }

    /// Whether [`Self::dispose`] has already run.
    pub fn is_disposed(&self) -> bool {
        self.undo
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_none()
    }
}

impl fmt::Debug for Registration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Registration")
            .field("disposed", &self.is_disposed())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    #[test]
    fn dispose_runs_once() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let reg = Registration::new(move || {
            counter.fetch_add(1, Ordering::SeqCst);
        });
        assert!(!reg.is_disposed());
        reg.dispose();
        reg.dispose();
        assert!(reg.is_disposed());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn dropping_does_not_undo() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        drop(Registration::new(move || {
            counter.fetch_add(1, Ordering::SeqCst);
        }));
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        assert!(Registration::noop().is_disposed());
    }
}
