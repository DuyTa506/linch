//! The server's log: the `tracing` records of the worker, the scheduler and
//! the workflow journal, written to stderr.
//!
//! stdout carries the protocol on a stdio server, so the log never goes
//! there. Records are filtered by `LINCH_LOG` (or `RUST_LOG` when it is
//! unset) in `tracing-subscriber`'s directive syntax, such as `info` or
//! `linch_worker=debug,warn`; with neither, only warnings and errors.

use tracing_subscriber::EnvFilter;

/// The filter used when no variable names one.
const DEFAULT_FILTER: &str = "warn";

/// The log filter for the values of `LINCH_LOG` and `RUST_LOG`.
///
/// `LINCH_LOG` wins when it is set and not blank, then `RUST_LOG`, then
/// `warn`. A value that is not a valid filter is reported in the second
/// element, and `warn` is used instead.
///
/// Args:
///     linch_log: The value of `LINCH_LOG`, if set.
///     rust_log: The value of `RUST_LOG`, if set.
///
/// Returns:
///     The filter, and a message naming the variable and why its value was
///     not used, if it was not valid.
pub fn log_filter(linch_log: Option<&str>, rust_log: Option<&str>) -> (EnvFilter, Option<String>) {
    let chosen = [("LINCH_LOG", linch_log), ("RUST_LOG", rust_log)]
        .into_iter()
        .find_map(|(name, value)| value.filter(|v| !v.trim().is_empty()).map(|v| (name, v)));
    match chosen {
        None => (EnvFilter::new(DEFAULT_FILTER), None),
        Some((name, value)) => match EnvFilter::try_new(value) {
            Ok(filter) => (filter, None),
            Err(error) => {
                let message = format!("{name} is not a valid log filter ({error}); using warn");
                (EnvFilter::new(DEFAULT_FILTER), Some(message))
            }
        },
    }
}

/// Install the process's log: records to stderr, without colors, filtered
/// by `LINCH_LOG` or `RUST_LOG` (see [`log_filter`]). An invalid filter is
/// reported on stderr and `warn` is used.
///
/// Args:
///     env: Reads an environment variable.
pub fn init_logging(env: impl Fn(&str) -> Option<String>) {
    let (filter, problem) = log_filter(env("LINCH_LOG").as_deref(), env("RUST_LOG").as_deref());
    if let Some(problem) = problem {
        eprintln!("linch-server: {problem}");
    }
    // A second install (a test that runs the server twice in one process)
    // keeps the first.
    let _ = tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(false)
        .with_env_filter(filter)
        .try_init();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn directives(filter: &EnvFilter) -> String {
        filter.to_string()
    }

    #[test]
    fn the_default_is_warn() {
        let (filter, problem) = log_filter(None, None);
        assert_eq!((directives(&filter).as_str(), problem), ("warn", None));
    }

    #[test]
    fn linch_log_wins_over_rust_log_and_a_blank_one_is_unset() {
        let (filter, _) = log_filter(Some("info"), Some("debug"));
        assert_eq!(directives(&filter), "info");
        let (filter, _) = log_filter(Some("  "), Some("debug"));
        assert_eq!(directives(&filter), "debug");
        let (filter, _) = log_filter(None, Some("linch_worker=trace,warn"));
        assert!(directives(&filter).contains("linch_worker=trace"));
    }

    #[test]
    fn an_invalid_filter_is_named_and_falls_back_to_warn() {
        let (filter, problem) = log_filter(Some("linch_worker=loud"), Some("debug"));
        assert_eq!(directives(&filter), "warn");
        let message = problem.expect("the problem is reported");
        assert!(
            message.starts_with("LINCH_LOG is not a valid log filter"),
            "{message}"
        );
    }
}
