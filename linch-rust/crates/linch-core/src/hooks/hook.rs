//! The hook contract: the [`Point`] trait, the [`Hook`] trait, and
//! tool-name matchers.

use std::time::Duration;

use async_trait::async_trait;
use thiserror::Error;

use crate::policy::{FailMode, HookPoint};

/// Error a hook callback returns. The chain handles it by the hook's
/// [`FailMode`] and keeps its text in the [`super::HookRecord`].
pub type CallbackError = Box<dyn std::error::Error + Send + Sync>;

/// A hook could not be registered.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum HookError {
    /// [`Hook::policy_id`] was empty or blank.
    #[error("hook policy_id must be a non-blank string")]
    BlankPolicyId,
    /// A [`ToolMatcher`] had no patterns, or a blank one.
    #[error("a tool matcher needs at least one pattern and no blank ones")]
    EmptyMatcher,
    /// A matcher was given at a point that is not about a tool call.
    #[error("{point:?} is not a tool point, so a tool matcher cannot apply")]
    MatcherNotSupported {
        /// The point the hook was registered at.
        point: HookPoint,
    },
    /// [`FailMode::Closed`] was asked for at a point with no blocking verdict.
    #[error("{point:?} has no blocking verdict, so it cannot fail closed")]
    FailClosedNotSupported {
        /// The point the hook was registered at.
        point: HookPoint,
    },
}

pub(crate) mod sealed {
    /// Keeps [`super::Point`] implemented by this module's markers only.
    pub trait Sealed {}
}

/// One hook point's request and response types.
///
/// Implemented only by the marker types in [`crate::hooks`]
/// ([`super::PreToolUse`], [`super::Stop`], ...), one per [`HookPoint`].
pub trait Point: sealed::Sealed + Send + Sync + 'static {
    /// The point this marker stands for.
    const POINT: HookPoint;
    /// What a hook at this point receives.
    type Request: Send + Sync;
    /// What a hook at this point may answer. It can only express what the
    /// point allows.
    type Response: Send;

    /// The tool name a [`ToolMatcher`] tests, at the tool points.
    fn tool_name(request: &Self::Request) -> Option<&str>;

    /// Short name of a response, for [`super::HookStatus::Answered`].
    fn verdict(response: &Self::Response) -> &'static str;
}

/// A callback at one hook point.
///
/// The chain reads [`Self::policy_id`], [`Self::policy_version`],
/// [`Self::matcher`], [`Self::timeout`] and [`Self::fail_mode`] once, at
/// registration; later changes to what they return have no effect. A hook
/// that serves several points implements this trait once per point and is
/// registered on each point's chain.
#[async_trait]
pub trait Hook<P: Point>: Send + Sync {
    /// Stable id of the policy this hook enforces. It enters the run
    /// contract through [`super::Hooks::policy_digest`].
    fn policy_id(&self) -> &str;

    /// Version of the policy. Bump it whenever the hook's behaviour changes,
    /// so a resumed run notices.
    fn policy_version(&self) -> u32;

    /// Tool names this hook applies to, at the tool points. `None` matches
    /// every tool.
    fn matcher(&self) -> Option<ToolMatcher> {
        None
    }

    /// Longest time the chain waits for [`Self::call`]. `None` waits forever.
    fn timeout(&self) -> Option<Duration> {
        None
    }

    /// Failure handling override. `None` uses
    /// [`HookPoint::default_fail_mode`].
    fn fail_mode(&self) -> Option<FailMode> {
        None
    }

    /// Answer one occurrence of the point.
    async fn call(&self, request: &P::Request) -> Result<P::Response, CallbackError>;
}

/// Tool-name globs: `*` matches any run of characters, `?` exactly one.
///
/// Tool names are ASCII (the registry enforces the provider-legal pattern),
/// so matching works on bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolMatcher {
    patterns: Vec<String>,
}

impl ToolMatcher {
    /// A matcher that accepts a tool name matching any of `patterns`.
    ///
    /// # Errors
    ///
    /// [`HookError::EmptyMatcher`] when `patterns` is empty or holds a blank
    /// pattern.
    pub fn new(patterns: impl IntoIterator<Item = impl Into<String>>) -> Result<Self, HookError> {
        let patterns: Vec<String> = patterns.into_iter().map(Into::into).collect();
        if patterns.is_empty() || patterns.iter().any(|p| p.trim().is_empty()) {
            return Err(HookError::EmptyMatcher);
        }
        Ok(Self { patterns })
    }

    /// Whether `tool_name` matches one of the patterns.
    pub fn matches(&self, tool_name: &str) -> bool {
        self.patterns
            .iter()
            .any(|pattern| glob(pattern.as_bytes(), tool_name.as_bytes()))
    }
}

/// Wildcard match with one backtrack point (the last `*` seen), so it runs
/// in `O(pattern * name)` time and never recurses.
fn glob(pattern: &[u8], name: &[u8]) -> bool {
    let (mut p, mut n) = (0, 0);
    let mut star: Option<(usize, usize)> = None;
    while n < name.len() {
        match pattern.get(p) {
            Some(b'*') => {
                star = Some((p, n));
                p += 1;
            }
            Some(&c) if c == b'?' || c == name[n] => {
                p += 1;
                n += 1;
            }
            _ => match star {
                Some((star_p, star_n)) => {
                    p = star_p + 1;
                    n = star_n + 1;
                    star = Some((star_p, star_n + 1));
                }
                None => return false,
            },
        }
    }
    pattern[p..].iter().all(|&c| c == b'*')
}

/// Points whose requests name a tool, so a [`ToolMatcher`] can apply.
pub(super) fn is_tool_point(point: HookPoint) -> bool {
    matches!(
        point,
        HookPoint::PreToolUse | HookPoint::PermissionRequest | HookPoint::PostToolUse
    )
}

/// Points with a verdict that blocks, which a closed failure turns into.
/// `context_build` only adds context and `post_turn` only observes.
pub(super) fn can_fail_closed(point: HookPoint) -> bool {
    !matches!(point, HookPoint::ContextBuild | HookPoint::PostTurn)
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;

    use super::*;

    /// Exponential but obviously correct reference for [`glob`].
    fn reference(pattern: &[u8], name: &[u8]) -> bool {
        match pattern.split_first() {
            None => name.is_empty(),
            Some((b'*', rest)) => (0..=name.len()).any(|i| reference(rest, &name[i..])),
            Some((&c, rest)) => name
                .split_first()
                .is_some_and(|(&n, tail)| (c == b'?' || c == n) && reference(rest, tail)),
        }
    }

    #[test]
    fn globs_match_names() {
        let m = ToolMatcher::new(["Write", "mcp__github__*", "Ed?t"]).unwrap();
        assert!(m.matches("Write"));
        assert!(m.matches("mcp__github__create_issue"));
        assert!(m.matches("mcp__github__"));
        assert!(m.matches("Edit"));
        assert!(!m.matches("Writer"));
        assert!(!m.matches("Read"));
        assert!(!m.matches("Eddit"));
        assert!(ToolMatcher::new(["*"]).unwrap().matches(""));
    }

    #[test]
    fn matcher_rejects_empty_and_blank_patterns() {
        assert_eq!(
            ToolMatcher::new(Vec::<String>::new()),
            Err(HookError::EmptyMatcher)
        );
        assert_eq!(ToolMatcher::new(["ok", " "]), Err(HookError::EmptyMatcher));
    }

    proptest! {
        #[test]
        fn glob_agrees_with_the_reference(
            pattern in "[ab*?]{0,8}",
            name in "[ab]{0,10}",
        ) {
            prop_assert_eq!(
                glob(pattern.as_bytes(), name.as_bytes()),
                reference(pattern.as_bytes(), name.as_bytes())
            );
        }

        #[test]
        fn glob_never_panics(pattern in ".{0,16}", name in ".{0,16}") {
            let _ = glob(pattern.as_bytes(), name.as_bytes());
        }
    }
}
