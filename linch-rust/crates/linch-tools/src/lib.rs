//! Workspace file tools, Bash and the session task tools for Linch.
//!
//! The file tools ([`Read`], [`Write`], [`Edit`], [`Grep`], [`Glob`]) go
//! through one [`ExecutionFs`]; [`LocalFs`] confines them to a root
//! directory with symlinks resolved. `Read`, `Write` and `Edit` share one
//! [`ReadTracker`], so a file is only changed after the model has seen it.
//! [`Bash`] runs a command in its own process group in the same root.
//! [`workspace_tools`] wires all six together.
//!
//! [`vfs`] is the virtual filesystem apart from the workspace: file
//! backends, the `ls`, `read_file`, `write_file` and `edit_file` tools, and
//! the offload of large tool results.
//!
//! [`task_tools`] are the session task tools ([`TaskCreate`], [`TaskList`],
//! [`TaskGet`], [`TaskUpdate`]) over a [`TaskStore`](linch_core::TaskStore).
//!
//! The crate is Unix-only: Bash relies on process groups.

#![forbid(unsafe_code)]

mod bash;
mod edit;
pub mod fs;
mod glob;
mod grep;
mod input;
mod read;
mod tasks;
mod tracker;
pub mod vfs;
mod write;

use std::sync::Arc;

use linch_core::Tool;

pub use bash::Bash;
pub use edit::Edit;
pub use fs::{ExecutionFs, FsError, LocalFs};
pub use glob::Glob;
pub use grep::Grep;
pub use input::READ_FIRST_CODE;
pub use read::Read;
pub use tasks::{TASK_TOOL_NAMES, TaskCreate, TaskGet, TaskList, TaskUpdate, task_tools};
pub use tracker::ReadTracker;
pub use write::Write;

/// `Read`, `Write`, `Edit`, `Grep`, `Glob` and `Bash` over one local
/// workspace, sharing `reads`. The host keeps `reads` to call
/// [`ReadTracker::forget`] when a session ends.
pub fn workspace_tools(fs: Arc<LocalFs>, reads: Arc<ReadTracker>) -> Vec<Arc<dyn Tool>> {
    let bash = Bash::new(fs.root());
    let fs: Arc<dyn ExecutionFs> = fs;
    vec![
        Arc::new(Read::new(fs.clone(), reads.clone())),
        Arc::new(Write::new(fs.clone(), reads.clone())),
        Arc::new(Edit::new(fs.clone(), reads)),
        Arc::new(Grep::new(fs.clone())),
        Arc::new(Glob::new(fs)),
        Arc::new(bash),
    ]
}
