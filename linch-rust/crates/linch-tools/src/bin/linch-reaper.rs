//! `linch-reaper -- PROGRAM [ARG...]`: runs one command and ends everything
//! it starts. See [`linch_tools::reaper`] for what it does and holds.
//!
//! A host that ships this program beside its own executable, or names it in
//! `LINCH_REAPER`, has its local commands run under it; a host that cannot
//! ship a second program runs its own executable in this mode instead.

#![forbid(unsafe_code)]

use std::process::ExitCode;

fn main() -> ExitCode {
    linch_tools::reaper::run(std::env::args_os().skip(1))
}
