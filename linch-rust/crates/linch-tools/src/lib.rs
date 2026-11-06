//! Workspace file tools and Bash for Linch.
//!
//! The file tools (`Read`, `Write`, `Edit`, `Grep`, `Glob`) go through one
//! [`ExecutionFs`]; [`LocalFs`] confines them to a root directory with
//! symlinks resolved. `Read`, `Write` and `Edit` share one [`ReadTracker`],
//! so a file is only changed after the model has seen it.
//!
//! The crate is Unix-only.

#![forbid(unsafe_code)]

mod edit;
pub mod fs;
mod glob;
mod grep;
mod input;
mod read;
mod tracker;
mod write;

pub use edit::Edit;
pub use fs::{ExecutionFs, FsError, LocalFs};
pub use glob::Glob;
pub use grep::Grep;
pub use input::READ_FIRST_CODE;
pub use read::Read;
pub use tracker::ReadTracker;
pub use write::Write;
