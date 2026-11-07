//! Postgres store tests. Each skips cleanly without `LINCH_TEST_PG_DSN`.

mod admission;
mod claim;
mod common;
mod conformance;
mod crash;
mod files;
mod idle;
mod isolation;
mod mailbox;
mod migrations;
mod pool;
mod refusals;
mod tasks;
