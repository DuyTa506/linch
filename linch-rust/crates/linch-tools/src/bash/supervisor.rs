//! Finding the helper that supervises local commands.
//!
//! A local command runs under the helper (see [`crate::reaper`]) only when
//! this process turned it on, because it costs a process per command; it is
//! held by its process group alone otherwise. The helper is, in this order:
//!
//! 1. `LINCH_REAPER` set and empty: none (it is turned off).
//! 2. `LINCH_REAPER=1`: the command the host named, through
//!    [`crate::reaper::enter`] or [`crate::reaper::register`]; none if it
//!    named none.
//! 3. `LINCH_REAPER=` anything else: the executable it names.
//! 4. `LINCH_REAPER` unset: the command the host turned on with
//!    [`crate::reaper::register`], if any.
//!
//! A candidate that is not an executable file is skipped. The answer is read
//! once per process.

use std::ffi::{OsStr, OsString};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use crate::reaper::{self, ReaperCommand};

/// The environment variable that turns the helper on (`1`), names it (a
/// path) or, empty, turns it off.
pub(crate) const ENV: &str = "LINCH_REAPER";

/// The helper to run commands under, if this process turned one on.
pub(crate) fn program() -> Option<&'static ReaperCommand> {
    static PROGRAM: OnceLock<Option<ReaperCommand>> = OnceLock::new();
    PROGRAM
        .get_or_init(|| match cfg!(target_os = "linux") {
            true => choose(std::env::var_os(ENV), reaper::host()),
            false => None,
        })
        .as_ref()
}

/// The helper for `env` (`LINCH_REAPER`) and the host's command, with
/// whether the host turned it on.
fn choose(env: Option<OsString>, host: Option<(&ReaperCommand, bool)>) -> Option<ReaperCommand> {
    let command = match env.as_deref() {
        Some(value) if value.is_empty() => return None,
        Some(value) if value == OsStr::new("1") => host?.0.clone(),
        Some(path) => ReaperCommand {
            program: PathBuf::from(path),
            args: Vec::new(),
        },
        None => match host? {
            (command, true) => command.clone(),
            (_, false) => return None,
        },
    };
    runs(&command.program).then_some(command)
}

/// Whether `path` is an executable file.
fn runs(path: &Path) -> bool {
    path.metadata()
        .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn command(program: &str) -> ReaperCommand {
        ReaperCommand {
            program: program.into(),
            args: vec!["--mode".into()],
        }
    }

    #[test]
    fn nothing_is_on_unless_something_turns_it_on() {
        assert_eq!(choose(None, None), None);
        // A host's offer (`enter`) is not enough.
        let offered = command("/bin/sh");
        assert_eq!(choose(None, Some((&offered, false))), None);
    }

    #[test]
    fn a_registered_command_is_on_and_the_variable_can_turn_it_off() {
        let host = command("/bin/sh");
        assert_eq!(choose(None, Some((&host, true))), Some(host.clone()));
        assert_eq!(choose(Some(OsString::new()), Some((&host, true))), None);
    }

    #[test]
    fn one_turns_on_what_the_host_offered_and_a_path_names_another() {
        let offered = command("/bin/sh");
        let on = choose(Some("1".into()), Some((&offered, false)));
        assert_eq!(on, Some(offered.clone()));
        assert_eq!(choose(Some("1".into()), None), None);
        let named = choose(Some("/bin/cat".into()), Some((&offered, true))).unwrap();
        assert_eq!(named.program, Path::new("/bin/cat"));
        assert!(named.args.is_empty());
    }

    #[test]
    fn a_helper_that_is_not_an_executable_file_is_skipped() {
        let missing = command("/nonexistent/linch");
        assert_eq!(choose(None, Some((&missing, true))), None);
        assert_eq!(choose(Some("1".into()), Some((&missing, false))), None);
        assert_eq!(choose(Some("/nonexistent/x".into()), None), None);
        assert_eq!(choose(Some("/tmp".into()), None), None);
    }
}
