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
//! 3. `LINCH_REAPER=/path`: the executable it names. The path must be
//!    absolute: a relative one would resolve in the command's own directory.
//! 4. `LINCH_REAPER` unset: the command the host turned on with
//!    [`crate::reaper::register`], if any.
//!
//! Any other value (`0`, `true`, a relative path) and a candidate that is not
//! an executable file leave the helper off with one warning. The answer is
//! read once per process. The sandbox backend further refuses a helper whose
//! executable the command can write (see `exec::sandbox`).

use std::ffi::{OsStr, OsString};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use crate::reaper::{self, ReaperCommand};

/// The environment variable that turns the helper on (`1`), names it (a
/// path) or, empty, turns it off.
pub(crate) const ENV: &str = "LINCH_REAPER";

/// The helper to run commands under, if this process turned one on. A
/// setting that cannot be honoured (not `1`, empty or an absolute path, or
/// not an executable file) is warned about, once, and leaves the helper off.
pub(crate) fn program() -> Option<&'static ReaperCommand> {
    static PROGRAM: OnceLock<Option<ReaperCommand>> = OnceLock::new();
    PROGRAM
        .get_or_init(|| match cfg!(target_os = "linux") {
            true => match choose(std::env::var_os(ENV), reaper::host()) {
                Ok(helper) => helper,
                Err(reason) => {
                    tracing::warn!("{reason}; commands run without the helper");
                    None
                }
            },
            false => None,
        })
        .as_ref()
}

/// The helper for `env` (`LINCH_REAPER`) and the host's command, with
/// whether the host turned it on; the reason when a setting cannot be
/// honoured.
fn choose(
    env: Option<OsString>,
    host: Option<(&ReaperCommand, bool)>,
) -> Result<Option<ReaperCommand>, String> {
    let command = match env.as_deref() {
        Some(value) if value.is_empty() => return Ok(None),
        Some(value) if value == OsStr::new("1") => match host {
            Some((command, _)) => command.clone(),
            None => return Err(format!("{ENV}=1, but this host offers no helper")),
        },
        Some(path) if Path::new(path).is_absolute() => ReaperCommand {
            program: PathBuf::from(path),
            args: Vec::new(),
        },
        Some(value) => {
            return Err(format!(
                "{ENV}={value:?} is not 1, empty or an absolute path"
            ));
        }
        None => match host {
            Some((command, true)) => command.clone(),
            _ => return Ok(None),
        },
    };
    match runs(&command.program) {
        true => Ok(Some(command)),
        false => Err(format!(
            "the helper {} is not an executable file",
            command.program.display()
        )),
    }
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
        assert_eq!(choose(None, None), Ok(None));
        // A host's offer (`enter`) is not enough.
        let offered = command("/bin/sh");
        assert_eq!(choose(None, Some((&offered, false))), Ok(None));
    }

    #[test]
    fn a_registered_command_is_on_and_the_variable_can_turn_it_off() {
        let host = command("/bin/sh");
        assert_eq!(choose(None, Some((&host, true))), Ok(Some(host.clone())));
        assert_eq!(choose(Some(OsString::new()), Some((&host, true))), Ok(None));
    }

    #[test]
    fn one_turns_on_what_the_host_offered_and_a_path_names_another() {
        let offered = command("/bin/sh");
        let on = choose(Some("1".into()), Some((&offered, false)));
        assert_eq!(on, Ok(Some(offered.clone())));
        assert!(choose(Some("1".into()), None).is_err());
        let named = choose(Some("/bin/cat".into()), Some((&offered, true)));
        let named = named.unwrap().unwrap();
        assert_eq!(named.program, Path::new("/bin/cat"));
        assert!(named.args.is_empty());
    }

    #[test]
    fn a_setting_that_cannot_be_honoured_is_an_error_not_silence() {
        // Not `1`, empty or an absolute path: a typo, or a relative path the
        // command's own directory could answer.
        for value in ["0", "true", "yes", "linch-reaper", "./linch-reaper"] {
            assert!(choose(Some(value.into()), None).is_err(), "{value}");
        }
        let missing = command("/nonexistent/linch");
        assert!(choose(None, Some((&missing, true))).is_err());
        assert!(choose(Some("1".into()), Some((&missing, false))).is_err());
        assert!(choose(Some("/nonexistent/x".into()), None).is_err());
        assert!(choose(Some("/tmp".into()), None).is_err());
    }
}
