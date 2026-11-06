//! `linch-server`: the Linch sidecar.
//!
//! - `linch-server` speaks the `linch-rpc` protocol on stdin and stdout,
//!   one JSON-RPC message per line, and serves many runs at once on one
//!   thread (file tools still use tokio's blocking threads). It exits
//!   when stdin closes.
//! - `linch-server --config FILE` serves the Unix socket and HTTP
//!   listeners FILE names (see the `linch_server` crate docs) until
//!   SIGINT or SIGTERM, then ends every connection and exits.
//!
//! Provider keys come from its environment and are not passed to the
//! processes it starts (MCP servers, Bash), but they stay in its own
//! environment; on Linux the process is marked not dumpable, so a child
//! running as the same user cannot read `/proc/<pid>/environ` or its
//! memory. No key or token is written to any output.
//!
//! Exit status: 0 after stdin closes or a stop signal, 1 if I/O fails, 2
//! for a bad command line or config (the reason goes to stderr).

#![forbid(unsafe_code)]

use std::path::PathBuf;
use std::process::ExitCode;

use linch_server::{Listeners, PATH, ServerConfig};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::unix::pipe;
use tokio_util::sync::CancellationToken;

const USAGE: &str = "usage: linch-server [--config FILE]";

fn main() -> ExitCode {
    #[cfg(target_os = "linux")]
    if let Err(error) =
        rustix::process::set_dumpable_behavior(rustix::process::DumpableBehavior::NotDumpable)
    {
        eprintln!("linch-server: cannot mark the process not dumpable: {error}");
        return ExitCode::FAILURE;
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    let config = match args.as_slice() {
        [] => None,
        [flag, path] if flag == "--config" => Some(PathBuf::from(path)),
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    // One client on stdio: its runs share one thread, so handing an
    // envelope or an answer between tasks wakes no other thread. The
    // listeners serve many clients and keep a thread per core.
    let mut runtime = match config {
        None => tokio::runtime::Builder::new_current_thread(),
        Some(_) => tokio::runtime::Builder::new_multi_thread(),
    };
    match runtime.enable_all().build() {
        Ok(runtime) => runtime.block_on(serve(config)),
        Err(error) => {
            eprintln!("linch-server: cannot start the runtime: {error}");
            ExitCode::FAILURE
        }
    }
}

async fn serve(config: Option<PathBuf>) -> ExitCode {
    let served = match config {
        None => {
            let (reader, writer) = stdio();
            linch_rpc::serve(reader, writer).await
        }
        Some(path) => match listen(&path).await {
            Ok(listeners) => {
                listeners.serve(on_signal()).await;
                Ok(())
            }
            Err(code) => return code,
        },
    };
    match served {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("linch-server: {error}");
            ExitCode::FAILURE
        }
    }
}

/// Stdin and stdout for the stdio transport.
///
/// A pipe, which is what a client that spawns the server gives it, is
/// driven by the runtime in non-blocking mode, so each read and write is
/// one system call on the task's own thread; tokio's `stdin()` and
/// `stdout()` hand each one to a blocking thread and back instead.
///
/// Non-blocking mode is a flag of the open file description, which fds 0
/// and 1 share with every process holding the same pipe (a shell's
/// `producer | { linch-server; next-step; }`, or a supervisor passing on
/// its own stdio), and a dup shares it too. So the server never sets it
/// on fds 0 and 1: on Linux it opens `/proc/self/fd/0` and `/proc/self/fd/1`,
/// which gives it new descriptions of the same pipes that only it holds,
/// and drives those. Nothing needs undoing at exit, so a crash or a kill
/// leaves the pipes as they were too. Anything else (a file, a terminal,
/// a socket, a pipe it may not reopen, another OS, where `/dev/fd` dups)
/// uses tokio's `stdin()` and `stdout()`, which leave the flags alone.
fn stdio() -> (
    Box<dyn AsyncRead + Unpin + Send>,
    Box<dyn AsyncWrite + Unpin + Send>,
) {
    let own = |fd: u8| cfg!(target_os = "linux").then(|| format!("/proc/self/fd/{fd}"));
    // Opened non-blocking, and refused unless it is a pipe.
    let options = pipe::OpenOptions::new();
    let reader: Box<dyn AsyncRead + Unpin + Send> =
        match own(0).map(|path| options.open_receiver(path)) {
            Some(Ok(pipe)) => Box::new(pipe),
            _ => Box::new(tokio::io::stdin()),
        };
    let writer: Box<dyn AsyncWrite + Unpin + Send> =
        match own(1).map(|path| options.open_sender(path)) {
            Some(Ok(pipe)) => Box::new(pipe),
            _ => Box::new(tokio::io::stdout()),
        };
    (reader, writer)
}

/// Read the config and bind its listeners, reporting where they listen.
async fn listen(path: &PathBuf) -> Result<Listeners, ExitCode> {
    let fail = |message: String| {
        eprintln!("linch-server: {message}");
        ExitCode::from(2)
    };
    let text = std::fs::read_to_string(path)
        .map_err(|error| fail(format!("cannot read {}: {error}", path.display())))?;
    let config = ServerConfig::from_json(&text).map_err(|error| fail(error.to_string()))?;
    let tokens = (config.tokens(|name| std::env::var(name).ok()))
        .map_err(|error| fail(error.to_string()))?;
    let listeners = (Listeners::bind(&config, tokens).await)
        .map_err(|error| fail(format!("cannot listen: {error}")))?;
    if let Some(socket) = listeners.socket_path() {
        eprintln!("linch-server: listening on unix:{}", socket.display());
    }
    if let Some(address) = listeners.http_addr() {
        eprintln!("linch-server: listening on ws://{address}{PATH}");
    }
    Ok(listeners)
}

/// A token cancelled on the first SIGINT or SIGTERM.
fn on_signal() -> CancellationToken {
    let stop = CancellationToken::new();
    let cancel = stop.clone();
    tokio::spawn(async move {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("a SIGTERM handler installs");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = terminate.recv() => {}
        }
        cancel.cancel();
    });
    stop
}
