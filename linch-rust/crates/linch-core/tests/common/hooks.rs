//! A hook driven by a closure, for the hook end-to-end tests.

use std::marker::PhantomData;
use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{CallbackError, FailMode, Hook, Point};

type Answer<P> = Box<
    dyn Fn(&<P as Point>::Request) -> Result<<P as Point>::Response, CallbackError> + Send + Sync,
>;

/// A hook at point `P` that answers with `answer`.
pub struct FnHook<P: Point> {
    id: &'static str,
    fail_mode: Option<FailMode>,
    answer: Answer<P>,
    point: PhantomData<fn() -> P>,
}

/// A hook with policy `id`, the point's default fail mode and `answer`.
pub fn hook<P: Point>(
    id: &'static str,
    answer: impl Fn(&P::Request) -> Result<P::Response, CallbackError> + Send + Sync + 'static,
) -> FnHook<P> {
    FnHook {
        id,
        fail_mode: None,
        answer: Box::new(answer),
        point: PhantomData,
    }
}

impl<P: Point> FnHook<P> {
    /// The same hook with an explicit fail mode.
    pub fn fail_mode(mut self, mode: FailMode) -> Self {
        self.fail_mode = Some(mode);
        self
    }

    /// The hook as the chains take it.
    pub fn arc(self) -> Arc<dyn Hook<P>> {
        Arc::new(self)
    }
}

#[async_trait]
impl<P: Point> Hook<P> for FnHook<P> {
    fn policy_id(&self) -> &str {
        self.id
    }
    fn policy_version(&self) -> u32 {
        1
    }
    fn fail_mode(&self) -> Option<FailMode> {
        self.fail_mode
    }
    async fn call(&self, request: &P::Request) -> Result<P::Response, CallbackError> {
        (self.answer)(request)
    }
}
