//! Workspace file tools, Bash and the session task tools for Linch.
//!
//! The file tools ([`Read`], [`Write`], [`Edit`], [`Grep`], [`Glob`]) go
//! through one [`ExecutionFs`]; [`LocalFs`] confines them to a root
//! directory with symlinks resolved. `Read`, `Write` and `Edit` share one
//! [`ReadTracker`], so a file is only changed after the model has seen it.
//! [`Bash`] runs a command in its own process group in the same root; with
//! `run_in_background` it starts the command in a per-agent [`Processes`]
//! registry, which [`BashOutput`] reads and [`BashKill`] stops.
//! [`workspace_tools`] wires all eight together over one
//! [`ExecutionBackend`]: `local` (commands run as the process's user),
//! `sandbox` (Landlock, seccomp and rlimits) or `docker` (see [`exec`]).
//!
//! [`vfs`] is the virtual filesystem apart from the workspace: file
//! backends, the `ls`, `read_file`, `write_file` and `edit_file` tools, and
//! the offload of large tool results.
//!
//! [`task_tools`] are the session task tools ([`TaskCreate`], [`TaskList`],
//! [`TaskGet`], [`TaskUpdate`]) over a [`TaskStore`](linch_core::TaskStore).
//!
//! [`schedule_tools`] are the schedule tools ([`CreateSchedule`],
//! [`ListSchedules`], [`CancelSchedule`]) over a [`ScheduleBackend`] the
//! host binds to one agent.
//!
//! The crate is Unix-only: Bash relies on process groups.

#![forbid(unsafe_code)]

mod bash;
mod edit;
pub mod exec;
pub mod fs;
mod glob;
mod grep;
mod input;
mod memory;
mod read;
mod schedules;
mod tasks;
mod tracker;
pub mod vfs;
mod write;

use std::sync::Arc;

use linch_core::Tool;

pub use bash::{Bash, BashKill, BashOutput, MAX_RUNNING, NO_SUCH_PROCESS_CODE, Processes};
pub use edit::Edit;
pub use exec::{DockerPolicy, ExecutionBackend, ExecutionConfig, ExecutionError, SandboxPolicy};
pub use fs::{ExecutionFs, FsError, LocalFs};
pub use glob::Glob;
pub use grep::Grep;
pub use input::READ_FIRST_CODE;
pub use memory::{
    MEMORY_TOOL_NAMES, MemoryDelete, MemorySave, MemoryScope, MemorySearch, NoPrincipal,
    memory_tools,
};
pub use read::Read;
pub use schedules::{
    CancelSchedule, CreateSchedule, ListSchedules, NewSchedule, SCHEDULE_TOOL_NAMES,
    ScheduleBackend, ScheduleEntry, ScheduleTiming, schedule_tools,
};
pub use tasks::{TASK_TOOL_NAMES, TaskCreate, TaskGet, TaskList, TaskUpdate, task_tools};
pub use tracker::ReadTracker;
pub use write::Write;

/// `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash`, `BashOutput` and
/// `BashKill` over one execution backend, sharing `reads`, with `Bash`'s
/// background commands in `processes`. The host keeps `reads`,
/// `processes` and `backend` to call [`ReadTracker::forget`],
/// [`Processes::close_session`] and [`ExecutionBackend::close_session`]
/// when a session ends.
pub fn workspace_tools(
    backend: &ExecutionBackend,
    reads: Arc<ReadTracker>,
    processes: Arc<Processes>,
) -> Vec<Arc<dyn Tool>> {
    let bash = Bash::in_backend(backend).with_processes(Arc::clone(&processes));
    let fs: Arc<dyn ExecutionFs> = backend.fs().clone();
    vec![
        Arc::new(Read::new(fs.clone(), reads.clone())),
        Arc::new(Write::new(fs.clone(), reads.clone())),
        Arc::new(Edit::new(fs.clone(), reads)),
        Arc::new(Grep::new(fs.clone())),
        Arc::new(Glob::new(fs)),
        Arc::new(bash),
        Arc::new(BashOutput::new(Arc::clone(&processes))),
        Arc::new(BashKill::new(processes)),
    ]
}
