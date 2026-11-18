//! Background commands: the per-agent registry of the processes `Bash`
//! started with `run_in_background`, keyed by session.
//!
//! Each process has one supervisor task that owns the child, pumps its
//! output into bounded buffers and waits for it, so the shell is always
//! reaped by the task that started it, whatever ends it: the command
//! exiting (its process group is killed then, as in the foreground),
//! its timeout, `BashKill`, the session closing, or the registry dropping.

use std::collections::hash_map::RandomState;
use std::collections::{BTreeMap, HashMap};
use std::hash::{BuildHasher, Hasher};
use std::io;
use std::mem;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use linch_core::SessionId;
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

use super::capture::Capture;
use super::process::{self, End};

/// Most processes of one session that may run at once.
pub const MAX_RUNNING: usize = 4;

/// Most finished processes of one session kept for a last read; past it
/// the oldest finished one is dropped.
const MAX_FINISHED: usize = 16;

/// How long `kill` waits for the supervisor to reap the shell.
const REAPED: Duration = Duration::from_secs(5);

/// The background processes `Bash` started, per session. One registry
/// belongs to one agent (it is shared by its `Bash`, `BashOutput` and
/// `BashKill`); there is no process-global state.
///
/// A process is killed and reaped when its command times out, on
/// `BashKill`, on [`Processes::close_session`] for its session (the host
/// calls it when the session ends), on [`Processes::close`], and when the
/// registry drops with its agent. Nothing is durable: after a restart a
/// process id names nothing, and the tools say the process no longer
/// exists.
#[derive(Debug, Default)]
pub struct Processes {
    sessions: Mutex<HashMap<SessionId, Vec<Arc<Proc>>>>,
    started: AtomicU64,
}

/// One background process.
#[derive(Debug)]
pub(super) struct Proc {
    pub(super) id: String,
    pub(super) pid: Option<u32>,
    pub(super) timeout: Duration,
    kill: CancellationToken,
    state: Mutex<State>,
    /// `true` once the supervisor has reaped the shell.
    done: watch::Receiver<bool>,
}

#[derive(Debug, Default)]
struct State {
    stdout: Capture,
    stderr: Capture,
    end: Option<End>,
}

/// Output not read yet, and how the process ended, if it has.
#[derive(Debug)]
pub(super) struct Unread {
    pub(super) stdout: Capture,
    pub(super) stderr: Capture,
    pub(super) end: Option<End>,
}

/// Why a process could not start.
#[derive(Debug)]
pub(super) enum StartError {
    /// The session already runs [`MAX_RUNNING`] processes.
    Full,
    /// The shell could not be started.
    Spawn(io::Error),
}

impl Processes {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Kill every process of `session` and forget them; their supervisors
    /// reap them. Sync, so a session-close callback can call it.
    pub fn close_session(&self, session: &SessionId) {
        let procs = self.lock().remove(session).unwrap_or_default();
        for proc in procs {
            proc.kill.cancel();
        }
    }

    /// Kill every process of every session and forget them.
    pub fn close(&self) {
        let sessions = mem::take(&mut *self.lock());
        for proc in sessions.into_values().flatten() {
            proc.kill.cancel();
        }
    }

    /// How many processes of `session` are running.
    pub fn running(&self, session: &SessionId) -> usize {
        let sessions = self.lock();
        let procs = sessions.get(session).map_or(&[][..], Vec::as_slice);
        procs.iter().filter(|proc| proc.is_running()).count()
    }

    /// Start `command` for `session` and supervise it on the current Tokio
    /// runtime until it ends or `timeout` passes.
    pub(super) fn start(
        &self,
        session: &SessionId,
        command: &str,
        cwd: &Path,
        env: &BTreeMap<String, String>,
        timeout: Duration,
    ) -> Result<Arc<Proc>, StartError> {
        let mut sessions = self.lock();
        let procs = sessions.entry(session.clone()).or_default();
        if procs.iter().filter(|proc| proc.is_running()).count() >= MAX_RUNNING {
            return Err(StartError::Full);
        }
        let child = process::spawn(command, cwd, env, true).map_err(StartError::Spawn)?;
        let (finished, done) = watch::channel(false);
        let proc = Arc::new(Proc {
            id: self.fresh_id(),
            pid: child.id(),
            timeout,
            kill: CancellationToken::new(),
            state: Mutex::default(),
            done,
        });
        let supervised = Arc::clone(&proc);
        tokio::spawn(async move {
            let (out, err) = (Arc::clone(&supervised), Arc::clone(&supervised));
            let end = process::supervise(
                child,
                supervised.timeout,
                &supervised.kill,
                |chunk| out.lock().stdout.push(chunk),
                |chunk| err.lock().stderr.push(chunk),
            )
            .await;
            // A failed wait leaves the shell to Tokio's reaper; it was
            // killed with its group.
            supervised.lock().end = Some(end.unwrap_or(End::Aborted));
            let _ = finished.send(true);
        });
        procs.push(Arc::clone(&proc));
        prune(procs);
        Ok(proc)
    }

    /// The process `id` of `session`, if the registry has it.
    pub(super) fn get(&self, session: &SessionId, id: &str) -> Option<Arc<Proc>> {
        let sessions = self.lock();
        let procs = sessions.get(session)?;
        procs.iter().find(|proc| proc.id == id).cloned()
    }

    /// A process id no other process of this registry has had, and that a
    /// restarted process will not hand out again: 48 random bits.
    fn fresh_id(&self) -> String {
        let n = self.started.fetch_add(1, Ordering::Relaxed);
        let mut hasher = RandomState::new().build_hasher();
        hasher.write_u64(n);
        format!("bash_{:012x}", hasher.finish() & 0xffff_ffff_ffff)
    }

    fn lock(&self) -> MutexGuard<'_, HashMap<SessionId, Vec<Arc<Proc>>>> {
        self.sessions.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

impl Drop for Processes {
    /// The agent is gone: so are its processes.
    fn drop(&mut self) {
        self.close();
    }
}

/// Drop the oldest finished processes past [`MAX_FINISHED`].
fn prune(procs: &mut Vec<Arc<Proc>>) {
    let finished = procs.iter().filter(|proc| !proc.is_running()).count();
    let mut excess = finished.saturating_sub(MAX_FINISHED);
    procs.retain(|proc| {
        let drop = excess > 0 && !proc.is_running();
        excess -= usize::from(drop);
        !drop
    });
}

impl Proc {
    /// Whether the shell has not been reaped yet.
    pub(super) fn is_running(&self) -> bool {
        !*self.done.borrow()
    }

    /// Wait until the process ends or `wait` passes, whichever is first.
    pub(super) async fn wait(&self, wait: Duration) {
        let mut done = self.done.clone();
        let _ = tokio::time::timeout(wait, done.wait_for(|done| *done)).await;
    }

    /// Kill the process group and wait for the supervisor to reap it.
    pub(super) async fn kill(&self) {
        self.kill.cancel();
        self.wait(REAPED).await;
    }

    /// Take the output not read yet, with the end if there is one.
    pub(super) fn take(&self) -> Unread {
        let mut state = self.lock();
        Unread {
            stdout: mem::take(&mut state.stdout),
            stderr: mem::take(&mut state.stderr),
            end: state.end,
        }
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
}
