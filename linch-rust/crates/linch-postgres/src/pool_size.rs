//! The default size of a [`connect`](crate::connect) pool.

use std::num::NonZeroU32;
use std::sync::LazyLock;

/// The smallest default pool size: sqlx's own default, enough for about
/// five runs committing at once (a run that commits back to back uses
/// about two connections).
pub const MIN_DEFAULT_CONNECTIONS: NonZeroU32 = NonZeroU32::new(10).unwrap();

/// The largest default pool size. Postgres's own `max_connections`, 100
/// unless the database sets it, is shared by every pool of every process
/// on the database, so three processes at this default still fit.
pub const MAX_DEFAULT_CONNECTIONS: NonZeroU32 = NonZeroU32::new(32).unwrap();

static DEFAULT: LazyLock<NonZeroU32> = LazyLock::new(|| {
    // A machine that cannot say gets the smallest default.
    let cores = std::thread::available_parallelism().map_or(1, usize::from);
    size_for(cores)
});

/// The most connections a [`connect`](crate::connect) pool opens unless
/// the host chooses: the machine's cores, at least
/// [`MIN_DEFAULT_CONNECTIONS`] and at most [`MAX_DEFAULT_CONNECTIONS`],
/// computed once per process.
pub fn default_max_connections() -> NonZeroU32 {
    *DEFAULT
}

fn size_for(cores: usize) -> NonZeroU32 {
    let (min, max) = (MIN_DEFAULT_CONNECTIONS.get(), MAX_DEFAULT_CONNECTIONS.get());
    let size = u32::try_from(cores).unwrap_or(max).clamp(min, max);
    NonZeroU32::new(size).unwrap_or(MIN_DEFAULT_CONNECTIONS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_follows_the_cores_between_its_bounds() {
        assert_eq!(size_for(1).get(), 10);
        assert_eq!(size_for(10).get(), 10);
        assert_eq!(size_for(20).get(), 20);
        assert_eq!(size_for(32).get(), 32);
        assert_eq!(size_for(128).get(), 32);
        assert_eq!(
            (MIN_DEFAULT_CONNECTIONS.get(), MAX_DEFAULT_CONNECTIONS.get()),
            (10, 32)
        );
    }

    #[test]
    fn the_default_is_this_machines() {
        let cores = std::thread::available_parallelism().map_or(1, usize::from);
        assert_eq!(default_max_connections(), size_for(cores));
        assert_eq!(default_max_connections(), default_max_connections());
    }
}
