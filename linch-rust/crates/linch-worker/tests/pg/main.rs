//! Worker tests on Postgres. Each skips cleanly without `LINCH_TEST_PG_DSN`.

mod common;
mod drain;
mod exclusive;
mod faults;
mod fixtures;
mod isolation;
mod one_job;
mod retention;
mod retry;
mod schedule;
mod scheduler;
mod schema;
mod session;
mod suspend;
mod takeover;
