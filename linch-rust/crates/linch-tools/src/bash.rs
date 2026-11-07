//! The `Bash` tool.

mod capture;
mod process;

use std::collections::BTreeMap;
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    JsonObject, JsonValue, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;
use tokio::time::Instant;

use crate::input::{coded, optional_count, schema, string};
use process::{End, Finished};

/// Timeout when the call gives none, in milliseconds.
const DEFAULT_TIMEOUT_MS: u64 = 120_000;

/// Longest timeout a call may ask for, in milliseconds.
const MAX_TIMEOUT_MS: u64 = 1_800_000;

/// How long before the call's deadline the command is killed, so the kill,
/// the pipe drain and the result fit before the scheduler drops the call.
const DEADLINE_MARGIN: Duration = Duration::from_millis(100);

/// Host variables passed through to the shell; nothing else is inherited,
/// so the host's credentials stay out of the command's environment.
const ALLOWED_ENV: [&str; 9] = [
    "PATH", "HOME", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "USER", "SHELL", "TMPDIR",
];

/// Run a shell command in the workspace root. Scope `exec`.
///
/// The command runs under `/bin/sh -c` with stdin closed, as the leader of
/// a new process group. The group is killed with `SIGKILL` when the command
/// times out, when `ToolContext::cancel` fires, when the call's future is
/// dropped, and when the shell exits (so background jobs never outlive the
/// call). Each stream keeps its first 4000 and last 26000 bytes.
///
/// A call with a [`ToolContext::deadline`] (the scheduler's call timeout)
/// cuts its timeout to end 100 ms before it, so the output so far reaches
/// the model instead of a generic timeout.
pub struct Bash {
    cwd: PathBuf,
    env: BTreeMap<String, String>,
    schema: JsonObject,
}

impl Bash {
    /// A `Bash` running in `cwd` (normally [`crate::LocalFs::root`]) with the
    /// allowlisted host variables, read now.
    pub fn new(cwd: impl Into<PathBuf>) -> Self {
        let env = ALLOWED_ENV
            .iter()
            .filter_map(|&key| Some((key.to_owned(), std::env::var(key).ok()?)))
            .collect();
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "description": "Shell command to run."},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Kill the command after this many milliseconds. Default 120000, max 1800000."
                }
            },
            "required": ["command"]
        }));
        Self {
            cwd: cwd.into(),
            env,
            schema,
        }
    }

    /// Also set `vars` in the command's environment, overriding host values.
    pub fn with_env(mut self, vars: impl IntoIterator<Item = (String, String)>) -> Self {
        self.env.extend(vars);
        self
    }
}

#[async_trait]
impl Tool for Bash {
    fn name(&self) -> &str {
        "Bash"
    }

    fn description(&self) -> &str {
        "Run a shell command (/bin/sh) in the workspace root with stdin closed. \
         The command and everything it starts are killed on timeout (default 120 s, \
         max 30 min) or cancellation, and background jobs are killed when the \
         command exits. Each output stream keeps its first 4000 and last 26000 bytes."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    fn validate(&self, mut input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if let Some(timeout) = optional_count(&input, "timeout_ms")? {
            input.insert("timeout_ms".into(), timeout.min(MAX_TIMEOUT_MS).into());
        }
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let command = input.get("command").and_then(|v| v.as_str()).unwrap_or("");
        let short: String = command.chars().take(60).collect();
        format!("Bash: {short}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let command = string(&input, "command")?;
        let timeout_ms = optional_count(&input, "timeout_ms")?
            .unwrap_or(DEFAULT_TIMEOUT_MS)
            .clamp(1, MAX_TIMEOUT_MS);
        if ctx.cancel.is_cancelled() {
            return Err(coded("[aborted before start]", "aborted"));
        }
        let timeout = fit(Duration::from_millis(timeout_ms), ctx.deadline);
        let finished = process::run(command, &self.cwd, &self.env, timeout, &ctx.cancel)
            .await
            .map_err(|error| coded(format!("could not start /bin/sh: {error}"), "spawn_failed"))?;
        render(finished, timeout.as_millis())
    }
}

/// `timeout`, cut to end [`DEADLINE_MARGIN`] before `deadline` (at least
/// 1 ms).
fn fit(timeout: Duration, deadline: Option<Instant>) -> Duration {
    let Some(deadline) = deadline else {
        return timeout;
    };
    let left = deadline.saturating_duration_since(Instant::now());
    let left = left.saturating_sub(DEADLINE_MARGIN);
    timeout.min(left.max(Duration::from_millis(1)))
}

/// The model-facing result: stdout, then `[stderr]` and stderr, then the
/// status line. Anything but exit code 0 is an error carrying the same text.
fn render(finished: Finished, timeout_ms: u128) -> ToolResult {
    let (stdout, out_cut) = finished.stdout.render();
    let (stderr, err_cut) = finished.stderr.render();
    let (status, failure): (String, Option<(&str, JsonValue)>) = match finished.end {
        End::Exited(status) => match (status.code(), status.signal()) {
            (Some(0), _) => ("[exit code 0]".into(), None),
            (Some(code), _) => (
                format!("[exit code {code}]"),
                Some(("exit_status", json!({"exit_code": code}))),
            ),
            (None, signal) => (
                format!("[killed by signal {}]", signal.unwrap_or(0)),
                Some(("exit_status", json!({"signal": signal}))),
            ),
        },
        End::TimedOut => (
            format!("[timed out after {timeout_ms} ms; process group killed]"),
            Some(("timeout", JsonValue::Null)),
        ),
        End::Aborted => (
            "[aborted; process group killed]".into(),
            Some(("aborted", JsonValue::Null)),
        ),
    };
    let mut parts = Vec::new();
    if !stdout.is_empty() {
        parts.push(stdout);
    }
    if !stderr.is_empty() {
        parts.push(format!("[stderr]\n{stderr}"));
    }
    parts.push(status);
    let text = parts.join("\n");
    match failure {
        None => {
            let mut output = ToolOutput::new(text);
            output.metadata.insert("exit_code".into(), 0.into());
            output
                .metadata
                .insert("truncated".into(), (out_cut || err_cut).into());
            Ok(output)
        }
        Some((code, details)) => Err(ToolOutputError {
            message: text,
            code: Some(code.to_owned()),
            details: (!details.is_null()).then_some(details),
        }),
    }
}
