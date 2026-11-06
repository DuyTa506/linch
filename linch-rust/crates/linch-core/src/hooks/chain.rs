//! One chain of hooks per point: registration, matching, timeouts, fail
//! modes and records. The per-point `run` methods live with each point's
//! types and decide what a response does to the chain.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use crate::policy::{FailMode, HookPoint};
use crate::registration::Registration;

use super::hook::{Hook, HookError, Point, ToolMatcher, can_fail_closed, is_tool_point};

/// What happened when the chain called one hook. Hooks the matcher filtered
/// out are not called and leave no record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookRecord {
    /// The point the hook ran at.
    pub point: HookPoint,
    /// The hook's policy id.
    pub policy_id: Arc<str>,
    /// The hook's policy version.
    pub policy_version: u32,
    /// How the call ended.
    pub status: HookStatus,
}

/// How one hook call ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HookStatus {
    /// The hook answered.
    Answered {
        /// Name of the response (`continue`, `rewrite`, `block`, ...).
        verdict: &'static str,
    },
    /// The hook returned an error, handled by `fail_mode`.
    Failed {
        /// The error text.
        error: String,
        /// How the failure was handled.
        fail_mode: FailMode,
    },
    /// The hook did not answer in time, handled by `fail_mode`.
    TimedOut {
        /// The timeout that elapsed.
        after: Duration,
        /// How the failure was handled.
        fail_mode: FailMode,
    },
}

/// A chain's outcome plus one record per hook it called, in call order, so
/// the loop can emit them.
#[derive(Debug)]
pub struct ChainResult<T> {
    /// What the chain decided.
    pub outcome: T,
    /// The hooks the chain called.
    pub records: Vec<HookRecord>,
}

/// The hooks registered at point `P`, run in registration order.
///
/// Shared across runs. Each `run` works on a snapshot taken when it starts,
/// so a registration made meanwhile applies from the next run on.
pub struct HookChain<P: Point> {
    state: Arc<Mutex<State<P>>>,
}

struct State<P: Point> {
    next_id: u64,
    entries: Arc<[Entry<P>]>,
}

/// One registered hook with its settings read at registration.
pub(super) struct Entry<P: Point> {
    id: u64,
    hook: Arc<dyn Hook<P>>,
    policy_id: Arc<str>,
    policy_version: u32,
    matcher: Option<ToolMatcher>,
    timeout: Option<Duration>,
    fail_mode: FailMode,
}

impl<P: Point> Clone for Entry<P> {
    fn clone(&self) -> Self {
        Self {
            id: self.id,
            hook: Arc::clone(&self.hook),
            policy_id: Arc::clone(&self.policy_id),
            policy_version: self.policy_version,
            matcher: self.matcher.clone(),
            timeout: self.timeout,
            fail_mode: self.fail_mode,
        }
    }
}

/// What one hook call means for the chain.
pub(super) enum Step<R> {
    /// The hook answered.
    Answer(R),
    /// The matcher filtered the hook out, or it failed open.
    Skip,
    /// The hook failed closed; the text says which hook and how.
    Closed(String),
}

impl<P: Point> Default for HookChain<P> {
    fn default() -> Self {
        Self {
            state: Arc::new(Mutex::new(State {
                next_id: 0,
                entries: Arc::from(Vec::new()),
            })),
        }
    }
}

impl<P: Point> HookChain<P> {
    /// Add `hook` at the end of the chain.
    ///
    /// Returns a [`Registration`] whose dispose removes this registration
    /// only; registering the same hook twice gives two independent entries.
    ///
    /// # Errors
    ///
    /// [`HookError::BlankPolicyId`] for a blank policy id,
    /// [`HookError::MatcherNotSupported`] for a matcher at a point that is
    /// not about a tool, and [`HookError::FailClosedNotSupported`] for
    /// [`FailMode::Closed`] at `context_build` or `post_turn`.
    pub fn register(&self, hook: Arc<dyn Hook<P>>) -> Result<Registration, HookError> {
        let policy_id = hook.policy_id();
        if policy_id.trim().is_empty() {
            return Err(HookError::BlankPolicyId);
        }
        let matcher = hook.matcher();
        if matcher.is_some() && !is_tool_point(P::POINT) {
            return Err(HookError::MatcherNotSupported { point: P::POINT });
        }
        let fail_mode = hook
            .fail_mode()
            .unwrap_or_else(|| P::POINT.default_fail_mode());
        if fail_mode == FailMode::Closed && !can_fail_closed(P::POINT) {
            return Err(HookError::FailClosedNotSupported { point: P::POINT });
        }
        let mut entry = Entry {
            id: 0,
            policy_id: Arc::from(policy_id),
            policy_version: hook.policy_version(),
            matcher,
            timeout: hook.timeout(),
            fail_mode,
            hook,
        };

        let mut state = lock(&self.state);
        entry.id = state.next_id;
        state.next_id += 1;
        let id = entry.id;
        let mut entries = state.entries.to_vec();
        entries.push(entry);
        state.entries = entries.into();
        drop(state);

        let weak = Arc::downgrade(&self.state);
        Ok(Registration::new(move || {
            if let Some(state) = weak.upgrade() {
                let mut state = lock(&state);
                if state.entries.iter().any(|entry| entry.id == id) {
                    state.entries = state
                        .entries
                        .iter()
                        .filter(|entry| entry.id != id)
                        .cloned()
                        .collect();
                }
            }
        }))
    }

    /// Whether no hook is registered, so the loop can skip building the
    /// request.
    pub fn is_empty(&self) -> bool {
        lock(&self.state).entries.is_empty()
    }

    /// The current entries; the lock is released before any hook runs.
    pub(super) fn entries(&self) -> Arc<[Entry<P>]> {
        Arc::clone(&lock(&self.state).entries)
    }

    /// `(point, policy_id, version)` of every registered hook.
    pub(super) fn policies(&self) -> Vec<(HookPoint, Arc<str>, u32)> {
        self.entries()
            .iter()
            .map(|entry| {
                let id = Arc::clone(&entry.policy_id);
                (P::POINT, id, entry.policy_version)
            })
            .collect()
    }
}

impl<P: Point> Entry<P> {
    /// Call the hook if its matcher accepts the request, apply its timeout
    /// and fail mode, and record the call.
    pub(super) async fn invoke(
        &self,
        request: &P::Request,
        records: &mut Vec<HookRecord>,
    ) -> Step<P::Response> {
        if let Some(matcher) = &self.matcher
            && !P::tool_name(request).is_some_and(|name| matcher.matches(name))
        {
            return Step::Skip;
        }
        let call = self.hook.call(request);
        let answer = match self.timeout {
            Some(limit) => tokio::time::timeout(limit, call).await.map_err(|_| limit),
            None => Ok(call.await),
        };
        let fail_mode = self.fail_mode;
        let (status, step) = match answer {
            Ok(Ok(response)) => (
                HookStatus::Answered {
                    verdict: P::verdict(&response),
                },
                Step::Answer(response),
            ),
            Ok(Err(error)) => (
                HookStatus::Failed {
                    error: error.to_string(),
                    fail_mode,
                },
                self.failed("failed"),
            ),
            Err(after) => (
                HookStatus::TimedOut { after, fail_mode },
                self.failed("timed out"),
            ),
        };
        records.push(HookRecord {
            point: P::POINT,
            policy_id: Arc::clone(&self.policy_id),
            policy_version: self.policy_version,
            status,
        });
        step
    }

    /// The step for a failed call. The closed text names the hook but not
    /// the error, which stays in the record for the host.
    fn failed(&self, how: &str) -> Step<P::Response> {
        match self.fail_mode {
            FailMode::Open => Step::Skip,
            FailMode::Closed => Step::Closed(format!("hook `{}` {how}", self.policy_id)),
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}
