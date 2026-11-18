//! Background commands: the per-agent registry of the processes `Bash`
//! started with `run_in_background`, keyed by session.
//!
//! Each process has one supervisor task that owns the child, pumps its
//! output into bounded buffers and waits for it, so the shell is always
//! reaped by the task that started it, whatever ends it: the command
//! exiting (its process group is killed then, as in the foreground),
//! its timeout, `BashKill`, the session closing, or the registry dropping.

use std::collections::HashMap;
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::io;
use std::mem;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use linch_core::SessionId;
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

use super::capture::Capture;
use super::process::{self, End};
use crate::exec::{Launch, Shell};

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
    /// Held from the count check to the insert, so two starts cannot both
    /// pass [`MAX_RUNNING`] while a backend spawns.
    starting: tokio::sync::Mutex<()>,
    /// The session of the start in flight, and whether it was closed while
    /// its command spawned; always locked after `sessions`.
    in_flight: Mutex<Option<(SessionId, bool)>>,
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
        let procs = {
            let mut sessions = self.lock();
            if let Some((starting, closed)) = &mut *self.in_flight() {
                *closed |= starting == session;
            }
            sessions.remove(session).unwrap_or_default()
        };
        for proc in procs {
            proc.kill.cancel();
        }
    }

    /// Kill every process of every session and forget them.
    pub fn close(&self) {
        let sessions = {
            let mut sessions = self.lock();
            if let Some((_, closed)) = &mut *self.in_flight() {
                *closed = true;
            }
            mem::take(&mut *sessions)
        };
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

    /// Start `launch` with `shell` for `session` and supervise it on the
    /// current Tokio runtime until it ends or `timeout` passes.
    pub(super) async fn start(
        &self,
        session: &SessionId,
        shell: &dyn Shell,
        launch: Launch<'_>,
        timeout: Duration,
    ) -> Result<Arc<Proc>, StartError> {
        let _starting = self.starting.lock().await;
        if self.running(session) >= MAX_RUNNING {
            return Err(StartError::Full);
        }
        *self.in_flight() = Some((session.clone(), false));
        let spawned = shell.spawn(launch).await;
        let spawned = spawned.map_err(|error| {
            self.in_flight().take();
            StartError::Spawn(error)
        })?;
        let (finished, done) = watch::channel(false);
        let proc = Arc::new(Proc {
            id: self.fresh_id(),
            pid: spawned.child.id(),
            timeout,
            kill: CancellationToken::new(),
            state: Mutex::default(),
            done,
        });
        let supervised = Arc::clone(&proc);
        tokio::spawn(async move {
            let (out, err) = (Arc::clone(&supervised), Arc::clone(&supervised));
            let end = process::supervise(
                spawned,
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
        let mut sessions = self.lock();
        let closed = self.in_flight().take().is_some_and(|(_, closed)| closed);
        if closed {
            // Its session closed while it spawned: it ends with the others.
            proc.kill.cancel();
            return Ok(proc);
        }
        let procs = sessions.entry(session.clone()).or_default();
        procs.push(Arc::clone(&proc));
        prune(procs);
        Ok(proc)
    }

    fn in_flight(&self) -> MutexGuard<'_, Option<(SessionId, bool)>> {
        self.in_flight
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
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

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::path::Path;

    use async_trait::async_trait;
    use tokio::sync::Notify;

    use super::*;
    use crate::bash::process::Spawned;
    use crate::exec::LocalShell;

    /// A local shell that spawns only once `go` is notified.
    #[derive(Debug, Default)]
    struct Slow {
        go: Notify,
        waiting: Notify,
    }

    #[async_trait]
    impl Shell for Slow {
        async fn spawn(&self, launch: Launch<'_>) -> io::Result<Spawned> {
            self.waiting.notify_one();
            self.go.notified().await;
            LocalShell.spawn(launch).await
        }
    }

    /// A session closed while its command spawns still loses the command.
    #[tokio::test]
    async fn a_session_closed_mid_start_kills_the_new_process() {
        let processes = Arc::new(Processes::new());
        let shell = Arc::new(Slow::default());
        let session = SessionId::from("s1");
        let env = BTreeMap::new();
        let start = {
            let (processes, shell, session) =
                (Arc::clone(&processes), Arc::clone(&shell), session.clone());
            tokio::spawn(async move {
                let launch = Launch {
                    command: "sleep 30",
                    cwd: Path::new("/"),
                    env: &env,
                    session: &session,
                    lifeline: true,
                };
                let timeout = Duration::from_secs(30);
                processes.start(&session, &*shell, launch, timeout).await
            })
        };
        shell.waiting.notified().await;
        processes.close_session(&session);
        shell.go.notify_one();
        let proc = start.await.unwrap().unwrap();
        proc.wait(Duration::from_secs(5)).await;
        assert!(!proc.is_running(), "the process outlived its session");
        assert_eq!(processes.running(&session), 0);
    }
}
