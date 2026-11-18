//! A sliding window of call times, for a tenant's per-minute caps.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// The times of the calls still inside the window.
#[derive(Debug, Default)]
pub(super) struct CallWindow(VecDeque<Instant>);

impl CallWindow {
    /// Count a call at `now`, unless `max` calls already fell within the
    /// last `window`; then count nothing and say so.
    pub(super) fn take(&mut self, now: Instant, max: usize, window: Duration) -> bool {
        while (self.0.front()).is_some_and(|at| now.saturating_duration_since(*at) >= window) {
            self.0.pop_front();
        }
        if self.0.len() >= max {
            return false;
        }
        self.0.push_back(now);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_call_counts_until_the_window_has_passed_it() {
        let start = Instant::now();
        let minute = Duration::from_secs(60);
        let mut window = CallWindow::default();
        let at = |seconds| start + Duration::from_secs(seconds);
        for second in 0..3 {
            assert!(window.take(at(second), 3, minute));
        }
        assert!(!window.take(at(3), 3, minute), "the fourth is refused");
        assert!(!window.take(at(59), 3, minute), "a refusal counts nothing");
        assert!(
            window.take(at(60), 3, minute),
            "the first has left the window"
        );
        assert!(!window.take(at(60), 3, minute));
        assert!(window.take(at(121), 3, minute), "and the others after it");
    }
}
