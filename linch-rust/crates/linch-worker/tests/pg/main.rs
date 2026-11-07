//! Worker tests on Postgres. Each skips cleanly without `LINCH_TEST_PG_DSN`.

mod common;
mod exclusive;
mod fixtures;
mod isolation;
mod one_job;
mod retry;
mod schedule;
mod session;
mod suspend;
mod takeover;
