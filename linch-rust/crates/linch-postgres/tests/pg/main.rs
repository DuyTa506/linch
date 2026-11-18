//! Postgres store tests. Each skips cleanly without `LINCH_TEST_PG_DSN`.

mod admission;
mod cancel;
mod claim;
mod common;
mod conformance;
mod crash;
mod exhausted;
mod files;
mod heartbeat;
mod idle;
mod isolation;
mod lease;
mod lease_line;
mod mailbox;
mod memory;
mod migrations;
mod pool;
mod refusals;
mod retention;
mod schema;
mod tasks;
mod tool_cache;
