//! A call that waited out the pool's acquire timeout: the one database
//! error that is not about the database but about how many connections
//! the host allowed.

use std::error::Error;
use std::time::Duration;

use thiserror::Error;

/// How long a call waits for a free connection of a [`connect`](crate::connect)
/// pool before it fails.
pub const ACQUIRE_TIMEOUT: Duration = Duration::from_secs(30);

/// Every connection of the pool was busy for the whole [`ACQUIRE_TIMEOUT`].
#[derive(Debug, Error)]
#[error(
    "pool timed out after {}s: raise max_pool_connections or lower concurrent runs; \
     a run already created stays at its last durable state: resume it",
    ACQUIRE_TIMEOUT.as_secs()
)]
pub struct PoolExhausted;

/// `error` as a store reports it: [`PoolExhausted`] for a pool timeout, else
/// `error` itself.
pub(crate) fn backend_source(error: sqlx::Error) -> Box<dyn Error + Send + Sync> {
    match error {
        sqlx::Error::PoolTimedOut => Box::new(PoolExhausted),
        other => Box::new(other),
    }
}

/// Whether `error`, or a cause under it, is [`PoolExhausted`]; a host logs
/// it with the pool's size and use, which only it knows.
pub fn is_pool_exhausted(error: &(dyn Error + 'static)) -> bool {
    let mut current = Some(error);
    while let Some(error) = current {
        if error.is::<PoolExhausted>() {
            return true;
        }
        current = error.source();
    }
    false
}
