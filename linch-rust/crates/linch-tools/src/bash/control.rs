//! `BashOutput` and `BashKill`: read and stop a background command.

use std::os::unix::process::ExitStatusExt;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    JsonObject, JsonValue, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;

use super::background::{Proc, Processes, Unread};
use super::fit;
use super::process::End;
use crate::input::{coded, optional_count, schema, string};

/// Longest `wait_ms` a `BashOutput` call may ask for, in milliseconds.
const MAX_WAIT_MS: u64 = 600_000;

/// Error code of a process id the registry does not know.
pub const NO_SUCH_PROCESS_CODE: &str = "no_such_process";

/// Read what a background command printed since the last read. Scope
/// `read`: it only reads the output of a command `Bash` already ran.
///
/// With `wait_ms` it first waits until the command ends or that much time
/// passes. Each stream keeps the first 4000 and last 26000 bytes of what
/// arrived since the last read.
pub struct BashOutput {
    processes: Arc<Processes>,
    schema: JsonObject,
}

/// Stop a background command: its whole process group gets `SIGKILL` and
/// the shell is reaped. Scope `exec`, as `Bash`.
pub struct BashKill {
    processes: Arc<Processes>,
    schema: JsonObject,
}

impl BashOutput {
    /// Read the processes of `processes`, the registry `Bash` starts them in.
    pub fn new(processes: Arc<Processes>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "minLength": 1, "description": "The id Bash returned for the background command."},
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "First wait up to this many milliseconds for the command to end. Default 0, max 600000."
                }
            },
            "required": ["process_id"]
        }));
        Self { processes, schema }
    }
}

impl BashKill {
    /// Stop the processes of `processes`, the registry `Bash` starts them in.
    pub fn new(processes: Arc<Processes>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "minLength": 1, "description": "The id Bash returned for the background command."}
            },
            "required": ["process_id"]
        }));
        Self { processes, schema }
    }
}

#[async_trait]
impl Tool for BashOutput {
    fn name(&self) -> &str {
        "BashOutput"
    }

    fn description(&self) -> &str {
        "Read what a background Bash command printed since the last read, and whether it is \
         still running. wait_ms first waits for it to end (max 10 min)."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let id = input.get("process_id").and_then(JsonValue::as_str);
        format!("BashOutput: {}", id.unwrap_or(""))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let proc = find(&self.processes, &input, &ctx)?;
        let wait_ms = optional_count(&input, "wait_ms")?.unwrap_or(0);
        let wait = fit(
            Duration::from_millis(wait_ms.min(MAX_WAIT_MS)),
            ctx.deadline,
        );
        if wait_ms > 0 && proc.is_running() {
            tokio::select! {
                () = proc.wait(wait) => {}
                () = ctx.cancel.cancelled() => {}
            }
        }
        render(&proc, proc.take())
    }
}

#[async_trait]
impl Tool for BashKill {
    fn name(&self) -> &str {
        "BashKill"
    }

    fn description(&self) -> &str {
        "Stop a background Bash command and everything it started, and read its last output."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let id = input.get("process_id").and_then(JsonValue::as_str);
        format!("BashKill: {}", id.unwrap_or(""))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let proc = find(&self.processes, &input, &ctx)?;
        proc.kill().await;
        render(&proc, proc.take())
    }
}

/// The process the input names in the call's session, or `no_such_process`.
fn find(
    processes: &Processes,
    input: &JsonObject,
    ctx: &ToolContext,
) -> Result<Arc<Proc>, ToolOutputError> {
    let id = string(input, "process_id")?;
    processes.get(&ctx.session_id, id).ok_or_else(|| {
        coded(
            format!(
                "Background process {id} no longer exists in this session: it finished long \
                 ago, its session was closed, or the server restarted since it started \
                 (background processes do not survive a restart). Start it again if you \
                 still need it."
            ),
            NO_SUCH_PROCESS_CODE,
        )
    })
}

/// New output, then the status. A command that ended badly (a non-zero
/// exit, a signal, its timeout) is an error result with the same text;
/// a running one, a clean exit and a kill are not.
fn render(proc: &Proc, unread: Unread) -> ToolResult {
    let Unread {
        stdout,
        stderr,
        end,
    } = unread;
    let (stdout, out_cut) = stdout.render();
    let (stderr, err_cut) = stderr.render();
    let mut metadata = JsonObject::new();
    let (status, failure): (String, Option<(&str, JsonValue)>) = match end {
        None => {
            metadata.insert("status".into(), "running".into());
            let line = format!(
                "[{} still running; read more with BashOutput, stop it with BashKill]",
                proc.id
            );
            (line, None)
        }
        Some(End::Exited(status)) => match (status.code(), status.signal()) {
            (Some(code), _) => {
                metadata.insert("status".into(), "exited".into());
                metadata.insert("exit_code".into(), code.into());
                let failure = (code != 0).then(|| ("exit_status", json!({"exit_code": code})));
                (format!("[exit code {code}]"), failure)
            }
            (None, signal) => {
                metadata.insert("status".into(), "exited".into());
                let signal = signal.unwrap_or(0);
                let failure = Some(("exit_status", json!({"signal": signal})));
                (format!("[killed by signal {signal}]"), failure)
            }
        },
        Some(End::TimedOut) => {
            metadata.insert("status".into(), "timed_out".into());
            let ms = proc.timeout.as_millis();
            let line = format!("[timed out after {ms} ms; process group killed]");
            (line, Some(("timeout", JsonValue::Null)))
        }
        Some(End::Aborted) => {
            metadata.insert("status".into(), "killed".into());
            ("[killed; process group killed]".into(), None)
        }
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
            metadata.insert("process_id".into(), proc.id.clone().into());
            metadata.insert("truncated".into(), (out_cut || err_cut).into());
            output.metadata = metadata;
            Ok(output)
        }
        Some((code, details)) => Err(ToolOutputError {
            message: text,
            code: Some(code.to_owned()),
            details: (!details.is_null()).then_some(details),
        }),
    }
}
