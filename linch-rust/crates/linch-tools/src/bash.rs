//! The `Bash` tool, and the background commands it starts.

mod background;
mod capture;
mod control;
pub(crate) mod process;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    JsonObject, JsonValue, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;
use tokio::time::Instant;

use crate::exec::{ExecutionBackend, Exit, Launch, LocalShell, Shell};
use crate::input::{coded, flag, optional_count, schema, string};
use background::StartError;
use process::{End, Finished};

pub use background::{MAX_RUNNING, Processes};
pub use control::{BashKill, BashOutput, NO_SUCH_PROCESS_CODE};

/// The status line of a remote command whose end is unknown: its host went
/// quiet, or did not confirm a kill.
pub(crate) const INTERRUPTED: &str = "[interrupted: the remote host stopped answering, so the \
     command's outcome is unknown; it may still be running]";

/// The status line of a remote command that printed more than the server
/// takes (`exec::remote::OUTPUT_BUDGET`) and was killed.
pub(crate) const OUTPUT_LIMIT: &str = "[output limit reached (256 MiB); the command was killed]";

/// Timeout when the call gives none, in milliseconds.
const DEFAULT_TIMEOUT_MS: u64 = 120_000;

/// Longest timeout a call may ask for, in milliseconds.
const MAX_TIMEOUT_MS: u64 = 1_800_000;

/// How long before the call's deadline a local command is killed, so the
/// kill, the pipe drain and the result fit before the scheduler drops the
/// call. A remote command's kill is a round trip: see
/// [`Shell::deadline_margin`].
pub(crate) const DEADLINE_MARGIN: Duration = Duration::from_millis(100);

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
/// cuts its timeout to end 100 ms before it (about 6 s for a `remote`
/// command, whose kill is a round trip to its host), so the output so far
/// reaches the model instead of a generic timeout.
///
/// With [`Bash::with_processes`], `run_in_background` starts the command
/// in that registry and returns its id at once; [`BashOutput`] reads it and
/// [`BashKill`] stops it. A background command keeps its process group
/// until it exits, its `timeout_ms` (30 min when absent) passes, it is
/// killed, its session closes or the registry drops; it also dies with the
/// server process, even on `SIGKILL`. A session runs at most
/// [`MAX_RUNNING`] at once.
///
/// [`Bash::new`] runs commands directly as the process's user;
/// [`Bash::in_backend`] runs them through an [`ExecutionBackend`], whose
/// confinement its description then states.
pub struct Bash {
    cwd: PathBuf,
    env: BTreeMap<String, String>,
    processes: Option<Arc<Processes>>,
    schema: JsonObject,
    shell: Arc<dyn Shell>,
    description: String,
}

/// What the description says before a backend's own sentence.
const DESCRIPTION: &str = "Run a shell command (/bin/sh) in the workspace root with stdin closed. \
     The command and everything it starts are killed on timeout (default 120 s, \
     max 30 min) or cancellation, and background jobs are killed when the \
     command exits. Each output stream keeps its first 4000 and last 26000 bytes. \
     With run_in_background the command keeps running and the call returns its \
     process id: read it with BashOutput, stop it with BashKill (default timeout \
     30 min, at most 4 at once).";

impl Bash {
    /// A `Bash` running commands through `backend`, in its root, with the
    /// allowlisted host variables, read now. Its description ends with what
    /// the backend confines, so the backend is part of the run contract; the
    /// local backend adds nothing.
    pub fn in_backend(backend: &ExecutionBackend) -> Self {
        let mut bash = Self::new(backend.root());
        bash.shell = backend.shell();
        if let Some(confinement) = backend.describe() {
            bash.description = format!("{DESCRIPTION} {confinement}");
        }
        bash
    }

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
                    "description": "Kill the command after this many milliseconds. Default 120000 (1800000 in the background), max 1800000."
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Start the command and return its process id at once; read it with BashOutput, stop it with BashKill."
                }
            },
            "required": ["command"]
        }));
        Self {
            cwd: cwd.into(),
            env,
            processes: None,
            schema,
            shell: Arc::new(LocalShell),
            description: DESCRIPTION.to_owned(),
        }
    }

    /// Start `run_in_background` commands in `processes`, which
    /// [`BashOutput`] and [`BashKill`] read. Without it such a call is
    /// refused.
    pub fn with_processes(mut self, processes: Arc<Processes>) -> Self {
        self.processes = Some(processes);
        self
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
        &self.description
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    /// A `run_in_background` command keeps running after the call, and may
    /// change files a cached read tool answers about.
    fn launches_background(&self, input: &JsonObject) -> bool {
        input.get("run_in_background").and_then(|v| v.as_bool()) == Some(true)
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
        let background = flag(&input, "run_in_background")?;
        let default = if background {
            MAX_TIMEOUT_MS
        } else {
            DEFAULT_TIMEOUT_MS
        };
        let timeout_ms = optional_count(&input, "timeout_ms")?
            .unwrap_or(default)
            .clamp(1, MAX_TIMEOUT_MS);
        if ctx.cancel.is_cancelled() {
            return Err(coded("[aborted before start]", "aborted"));
        }
        if background {
            return self
                .start(command, Duration::from_millis(timeout_ms), &ctx)
                .await;
        }
        let spawned = (self.shell)
            .spawn(self.launch(command, &ctx, false))
            .await
            .map_err(|error| self.spawn_failed(error))?;
        // After the spawn, which a remote host may take seconds to answer:
        // the margin counts from when the command runs.
        let margin = |left| self.shell.deadline_margin(left);
        let timeout = fit(Duration::from_millis(timeout_ms), ctx.deadline, margin);
        let finished = process::run(spawned, timeout, &ctx.cancel)
            .await
            .map_err(|error| self.spawn_failed(error))?;
        render(finished, timeout.as_millis())
    }
}

impl Bash {
    fn launch<'a>(&'a self, command: &'a str, ctx: &'a ToolContext, lifeline: bool) -> Launch<'a> {
        Launch {
            command,
            cwd: &self.cwd,
            env: &self.env,
            session: &ctx.session_id,
            principal: ctx.principal.as_ref(),
            lifeline,
        }
    }

    fn spawn_failed(&self, error: std::io::Error) -> ToolOutputError {
        let program = self.shell.program();
        coded(
            format!("could not start {program}: {error}"),
            "spawn_failed",
        )
    }

    /// Start `command` in the background for the call's session.
    async fn start(&self, command: &str, timeout: Duration, ctx: &ToolContext) -> ToolResult {
        let Some(processes) = &self.processes else {
            let message = "run_in_background is not available: this Bash has no process registry";
            return Err(coded(message, "no_background"));
        };
        let launch = self.launch(command, ctx, true);
        let started = processes
            .start(&ctx.session_id, &*self.shell, launch, timeout)
            .await;
        let proc = match started {
            Ok(proc) => proc,
            Err(StartError::Full) => {
                let message = format!(
                    "{MAX_RUNNING} background processes already run in this session; stop one \
                     with BashKill or wait for one to end first"
                );
                return Err(coded(message, "too_many_processes"));
            }
            Err(StartError::Spawn(error)) => return Err(self.spawn_failed(error)),
        };
        let ms = timeout.as_millis();
        let text = format!(
            "[started background process {}; read its output with BashOutput, stop it with \
             BashKill; it is killed after {ms} ms, when the session closes, or when the \
             server stops]",
            proc.id
        );
        let mut output = ToolOutput::new(text);
        output
            .metadata
            .insert("process_id".into(), proc.id.clone().into());
        output.metadata.insert("status".into(), "running".into());
        if let Some(pid) = proc.pid {
            output.metadata.insert("pid".into(), pid.into());
        }
        Ok(output)
    }
}

/// `timeout`, cut to end `margin` (given the time left) before `deadline`
/// (at least 1 ms).
fn fit(
    timeout: Duration,
    deadline: Option<Instant>,
    margin: impl FnOnce(Duration) -> Duration,
) -> Duration {
    let Some(deadline) = deadline else {
        return timeout;
    };
    let left = deadline.saturating_duration_since(Instant::now());
    let left = left.saturating_sub(margin(left));
    timeout.min(left.max(Duration::from_millis(1)))
}

/// The model-facing result: stdout, then `[stderr]` and stderr, then the
/// status line. Anything but exit code 0 is an error carrying the same text.
///
/// The host-facing facts are `status` (`exited`, `timed_out`, `killed`,
/// `output_limit` or `interrupted`), `exit_code` or `signal` for a command
/// that ended by itself, and `truncated`: the output's metadata, or the
/// error's details. The call's end event carries either as `metadata`.
fn render(finished: Finished, timeout_ms: u128) -> ToolResult {
    let (stdout, out_cut) = finished.stdout.render();
    let (stderr, err_cut) = finished.stderr.render();
    let mut metadata = JsonObject::new();
    let (status, state, failure): (String, &str, Option<&str>) = match finished.end {
        End::Exited(Exit::Code(code)) => {
            metadata.insert("exit_code".into(), code.into());
            let failure = (code != 0).then_some("exit_status");
            (format!("[exit code {code}]"), "exited", failure)
        }
        End::Exited(Exit::Signal(signal)) => {
            metadata.insert("signal".into(), signal.into());
            let line = format!("[killed by signal {signal}]");
            (line, "exited", Some("exit_status"))
        }
        End::TimedOut => {
            let line = format!("[timed out after {timeout_ms} ms; process group killed]");
            (line, "timed_out", Some("timeout"))
        }
        End::Aborted => {
            let line = "[aborted; process group killed]".to_owned();
            (line, "killed", Some("aborted"))
        }
        End::OutputLimit => (OUTPUT_LIMIT.into(), "output_limit", Some("output_limit")),
        End::Interrupted => (INTERRUPTED.into(), "interrupted", Some("interrupted")),
    };
    metadata.insert("status".into(), state.into());
    metadata.insert("truncated".into(), (out_cut || err_cut).into());
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
            output.metadata = metadata;
            Ok(output)
        }
        Some(code) => Err(ToolOutputError {
            message: text,
            code: Some(code.to_owned()),
            details: Some(JsonValue::Object(metadata)),
        }),
    }
}
