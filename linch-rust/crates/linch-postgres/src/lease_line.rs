//! [`LeaseLine`]: a connection of its own for the writes that keep a claim
//! alive.
//!
//! A lease renewal is one cheap statement, but it is due on time: a run that
//! renews a beat late loses its run to another process, which pays for the
//! model call again. In a pool it queues FIFO behind run work, and when
//! every connection waits on a row lock or a slow disk it waits as long, so
//! the lease runs out under a healthy holder. A line is never shared with
//! run work, and every write on it is bounded by the patience it was given.

use std::fmt;
use std::io;
use std::sync::Arc;
use std::time::Duration;

use sqlx::postgres::{PgArguments, PgConnectOptions};
use sqlx::query::Query;
use sqlx::{Connection, PgConnection, PgPool, Postgres};
use tokio::sync::Mutex;
use tokio::time::{Instant, timeout_at};

/// One connection, opened from a pool's own options on the first write and
/// opened again after any write that broke it.
///
/// Writes take turns on it, each waiting at most the line's patience in all:
/// for its turn, for a connection and for the statement. The server cancels
/// a statement that runs that long too (`statement_timeout`), so a write
/// that waits on a row lock leaves no backend waiting behind it. A write
/// that gives up drops the connection instead of returning it, so the next
/// one starts clean. A store makes one line, so it holds one connection
/// beyond its pool's `max_connections`.
pub struct LeaseLine {
    options: Arc<PgConnectOptions>,
    patience: Duration,
    connection: Mutex<Option<PgConnection>>,
}

impl fmt::Debug for LeaseLine {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LeaseLine")
            .field("patience", &self.patience)
            .finish_non_exhaustive()
    }
}

impl LeaseLine {
    /// A line to `pool`'s database and schema; no connection opens before
    /// the first write.
    ///
    /// Args:
    ///     pool: The pool whose connect options the line reuses.
    ///     patience: The longest one write takes in all, at least 1 ms.
    pub fn new(pool: &PgPool, patience: Duration) -> Self {
        Self {
            options: pool.connect_options(),
            patience: patience.max(Duration::from_millis(1)),
            connection: Mutex::new(None),
        }
    }

    /// Run the statement `query` builds and return the rows it changed.
    ///
    /// Args:
    ///     query: Builds the statement; called again when a connection that
    ///         sat idle turns out to be dead.
    ///
    /// Returns:
    ///     The number of rows the statement affected.
    ///
    /// Raises:
    ///     sqlx::Error: `Io` of kind `TimedOut` when the write did not finish
    ///         within the patience, or the database's or the connection's
    ///         error. A statement the server cancelled for its time is a
    ///         database error and leaves the connection in use.
    pub async fn execute<'q, F>(&self, query: F) -> Result<u64, sqlx::Error>
    where
        F: Fn() -> Query<'q, Postgres, PgArguments>,
    {
        let deadline = Instant::now() + self.patience;
        let mut slot = timeout_at(deadline, self.connection.lock())
            .await
            .map_err(|_| timed_out())?;
        let mut retry = true;
        loop {
            let held = slot.take();
            let reused = held.is_some();
            let attempt = timeout_at(deadline, async {
                let mut connection = match held {
                    Some(connection) => connection,
                    None => self.open().await?,
                };
                let done = query().execute(&mut connection).await;
                Ok::<_, sqlx::Error>((connection, done))
            })
            .await;
            match attempt {
                Err(_elapsed) => return Err(timed_out()),
                Ok(Err(error)) => return Err(error),
                Ok(Ok((connection, done))) => match done {
                    Ok(done) => {
                        *slot = Some(connection);
                        return Ok(done.rows_affected());
                    }
                    Err(error) if !broke(&error) => {
                        *slot = Some(connection);
                        return Err(error);
                    }
                    // A connection that sat idle may have been closed under
                    // us: one more try on a fresh one.
                    Err(_) if reused && retry => retry = false,
                    Err(error) => return Err(error),
                },
            }
        }
    }

    async fn open(&self) -> Result<PgConnection, sqlx::Error> {
        let mut connection = PgConnection::connect_with(&self.options).await?;
        let millis = self.patience.as_millis().max(1).to_string();
        sqlx::query("SELECT set_config('statement_timeout', $1, false)")
            .bind(millis)
            .execute(&mut connection)
            .await?;
        Ok(connection)
    }
}

/// Whether `error` says the connection is gone: not an answer of the
/// database, or a connection exception (class 08) or a shutdown (57P).
fn broke(error: &sqlx::Error) -> bool {
    let Some(error) = error.as_database_error() else {
        return true;
    };
    let code = error.code().unwrap_or_default();
    code.starts_with("08") || code.starts_with("57P")
}

fn timed_out() -> sqlx::Error {
    sqlx::Error::Io(io::Error::new(
        io::ErrorKind::TimedOut,
        "a lease write did not finish in time",
    ))
}
