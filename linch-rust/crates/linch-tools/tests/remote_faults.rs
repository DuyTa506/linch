//! The `remote` backend against hosts that misbehave: answers that prove
//! less than they seem to, floods, and slow kills. `Fake` is a host that
//! answers from a script and counts what it was asked.

mod common;

use std::future::pending;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use common::{Workspace, ctx, input};
use linch_core::{Tool, ToolResult};
use linch_tools::{
    Bash, BashOutput, ExecutionBackend, ExecutionConfig, Exit, Processes, RemoteChunk,
    RemoteCommand, RemoteError, RemoteExec, RemotePolicy,
};
use serde_json::json;

/// How a [`Fake`] answers a start.
#[derive(Clone)]
enum StartMode {
    Ok,
    Fail(RemoteError),
    /// Never answers.
    Hang,
}

/// How a [`Fake`] answers a read.
#[derive(Clone)]
enum ReadMode {
    /// Waits as asked, then answers empty.
    Wait,
    Fail(RemoteError),
    /// Answers at once with this chunk, every time.
    Now(RemoteChunk),
}

struct Fake {
    start: StartMode,
    read: ReadMode,
    kill: Result<RemoteChunk, RemoteError>,
    kill_takes: Duration,
    starts: AtomicUsize,
    reads: AtomicUsize,
    kills: AtomicUsize,
}

impl std::fmt::Debug for Fake {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Fake")
    }
}

impl Fake {
    fn new(start: StartMode, read: ReadMode, kill: Result<RemoteChunk, RemoteError>) -> Self {
        Self {
            start,
            read,
            kill,
            kill_takes: Duration::ZERO,
            starts: AtomicUsize::new(0),
            reads: AtomicUsize::new(0),
            kills: AtomicUsize::new(0),
        }
    }
}

fn count(counter: &AtomicUsize) -> usize {
    counter.load(Ordering::SeqCst)
}

#[async_trait]
impl RemoteExec for Fake {
    async fn start(&self, _: RemoteCommand) -> Result<(), RemoteError> {
        self.starts.fetch_add(1, Ordering::SeqCst);
        match &self.start {
            StartMode::Ok => Ok(()),
            StartMode::Fail(error) => Err(error.clone()),
            StartMode::Hang => pending().await,
        }
    }

    async fn read(&self, _: &str, wait: Duration) -> Result<RemoteChunk, RemoteError> {
        self.reads.fetch_add(1, Ordering::SeqCst);
        match &self.read {
            ReadMode::Wait => {
                tokio::time::sleep(wait).await;
                Ok(RemoteChunk::default())
            }
            ReadMode::Fail(error) => Err(error.clone()),
            ReadMode::Now(chunk) => Ok(chunk.clone()),
        }
    }

    async fn kill(&self, _: &str) -> Result<RemoteChunk, RemoteError> {
        self.kills.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(self.kill_takes).await;
        self.kill.clone()
    }
}

fn signalled() -> Result<RemoteChunk, RemoteError> {
    Ok(RemoteChunk {
        exit: Some(Exit::Signal(9)),
        ..RemoteChunk::default()
    })
}

fn bash_on(ws: &Workspace, host: &Arc<Fake>) -> (Bash, Arc<Processes>) {
    let remote: Arc<dyn RemoteExec> = host.clone();
    let config = ExecutionConfig::Remote(RemotePolicy::default());
    let backend = ExecutionBackend::open_with_host(ws.fs.clone(), &config, Some(remote)).unwrap();
    let processes = Arc::new(Processes::new());
    let bash = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    (bash, processes)
}

async fn run(tool: &Bash, command: &str, timeout_ms: u64) -> ToolResult {
    let value = json!({"command": command, "timeout_ms": timeout_ms});
    tool.execute(input(value), ctx("s1")).await
}

/// A start the host may have done, or not, must not look like a command
/// that never ran: the model would retry it, and it would run twice, the
/// first one unwatched. Only an explicit refusal proves it did not start.
#[tokio::test(start_paused = true)]
async fn a_start_that_may_have_happened_is_interrupted_and_killed() {
    let modes = [
        StartMode::Fail(RemoteError::Protocol("a half-written answer".into())),
        StartMode::Fail(RemoteError::Closed),
        StartMode::Fail(RemoteError::Unresponsive),
        StartMode::Hang,
    ];
    for mode in modes {
        let ws = Workspace::new();
        let host = Arc::new(Fake::new(mode, ReadMode::Wait, signalled()));
        let (bash, _) = bash_on(&ws, &host);
        let error = run(&bash, "make deploy", 10_000).await.unwrap_err();
        assert_eq!(
            error.code.as_deref(),
            Some("interrupted"),
            "{}",
            error.message
        );
        assert!(!error.message.contains("could not start"), "{error:?}");
        assert_eq!(count(&host.starts), 1);
        assert_eq!(count(&host.kills), 1, "the command was left unwatched");
        assert_eq!(count(&host.reads), 0, "nothing was confirmed");
    }
}

#[tokio::test(start_paused = true)]
async fn a_background_start_that_may_have_happened_reads_as_interrupted() {
    let ws = Workspace::new();
    let error = RemoteError::Protocol("unreadable".into());
    let host = Arc::new(Fake::new(
        StartMode::Fail(error),
        ReadMode::Wait,
        signalled(),
    ));
    let (bash, processes) = bash_on(&ws, &host);
    let call = json!({"command": "sleep 30", "run_in_background": true});
    let started = bash.execute(input(call), ctx("s1")).await.unwrap();
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    let output = BashOutput::new(Arc::clone(&processes));
    let read = json!({"process_id": id, "wait_ms": 10_000});
    let error = output.execute(input(read), ctx("s1")).await.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("interrupted"));
    assert_eq!(count(&host.kills), 1);
}

/// A kill answer is a confirmation only with an exit in it.
#[tokio::test(start_paused = true)]
async fn a_kill_answer_without_an_exit_is_not_a_confirmation() {
    let ws = Workspace::new();
    let answer = RemoteChunk {
        stdout: "tail".into(),
        ..RemoteChunk::default()
    };
    let host = Arc::new(Fake::new(StartMode::Ok, ReadMode::Wait, Ok(answer)));
    let (bash, _) = bash_on(&ws, &host);
    let error = run(&bash, "sleep 30", 300).await.unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some("interrupted"),
        "{}",
        error.message
    );
    assert!(error.message.starts_with("tail\n[interrupted"), "{error:?}");
}

/// After a lost read, a kill that reports a code is still `interrupted`:
/// what the command did before is unknown.
#[tokio::test(start_paused = true)]
async fn a_lost_read_stays_interrupted_even_when_the_kill_reports_a_code() {
    let ws = Workspace::new();
    let answer = RemoteChunk {
        stdout: "tail".into(),
        exit: Some(Exit::Code(0)),
        ..RemoteChunk::default()
    };
    let lost = ReadMode::Fail(RemoteError::Protocol("unreadable".into()));
    let host = Arc::new(Fake::new(StartMode::Ok, lost, Ok(answer)));
    let (bash, _) = bash_on(&ws, &host);
    let error = run(&bash, "sleep 30", 10_000).await.unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some("interrupted"),
        "{}",
        error.message
    );
    assert!(error.message.starts_with("tail\n[interrupted"), "{error:?}");
    assert!(!error.message.contains("exit code"), "{error:?}");
}

/// A host that answers reads at once, empty, cannot make the server spin
/// one request after another.
#[tokio::test]
async fn a_host_that_answers_reads_at_once_with_nothing_is_paced() {
    let ws = Workspace::new();
    let host = Arc::new(Fake::new(
        StartMode::Ok,
        ReadMode::Now(RemoteChunk::default()),
        signalled(),
    ));
    let (bash, _) = bash_on(&ws, &host);
    let error = run(&bash, "sleep 30", 400).await.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("timeout"), "{}", error.message);
    let reads = count(&host.reads);
    assert!(reads < 40, "{reads} reads in 400 ms");
}

/// A host cannot make a command's output run on forever: past a budget the
/// command is killed and the call is interrupted.
#[tokio::test]
async fn a_host_that_sends_endless_output_is_cut_off() {
    let ws = Workspace::new();
    let chunk = RemoteChunk {
        stdout: "x".repeat(1 << 20),
        ..RemoteChunk::default()
    };
    let host = Arc::new(Fake::new(StartMode::Ok, ReadMode::Now(chunk), signalled()));
    let (bash, _) = bash_on(&ws, &host);
    let error = run(&bash, "yes", 5_000).await.unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some("interrupted"),
        "{}",
        error.message
    );
    assert_eq!(count(&host.kills), 1);
    assert!(count(&host.reads) < 400);
}

/// The kill round trip to a remote host takes longer than a local kill, so
/// the command ends early enough for the output to reach the model before
/// the scheduler drops the call.
#[tokio::test(start_paused = true)]
async fn a_remote_call_ends_early_enough_for_its_kill_to_fit_the_deadline() {
    let ws = Workspace::new();
    let mut fake = Fake::new(StartMode::Ok, ReadMode::Wait, signalled());
    fake.kill_takes = Duration::from_secs(2);
    let host = Arc::new(fake);
    let (bash, _) = bash_on(&ws, &host);
    let begun = tokio::time::Instant::now();
    let context = ctx("s1").with_deadline(Some(begun + Duration::from_secs(10)));
    let value = json!({"command": "sleep 30", "timeout_ms": 600_000});
    let error = bash.execute(input(value), context).await.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("timeout"), "{}", error.message);
    assert!(
        begun.elapsed() < Duration::from_secs(10),
        "{:?}",
        begun.elapsed()
    );
}

/// What a host says when it refuses a start reaches the model: bounded,
/// and as plain text.
#[tokio::test]
async fn a_hosts_refusal_is_bounded_plain_text() {
    let ws = Workspace::new();
    let text = format!("bad\u{1b}[31m\nline two {}", "x".repeat(100_000));
    let host = Arc::new(Fake::new(
        StartMode::Fail(RemoteError::Rejected(text)),
        ReadMode::Wait,
        signalled(),
    ));
    let (bash, _) = bash_on(&ws, &host);
    let error = run(&bash, "true", 10_000).await.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("spawn_failed"));
    assert!(error.message.len() < 2300, "{}", error.message.len());
    assert!(error.message.contains("bad"), "{}", error.message);
    assert!(
        !error.message.chars().any(char::is_control),
        "{}",
        error.message
    );
    assert_eq!(count(&host.kills), 0, "a refused start is not killed");
}
