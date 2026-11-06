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

use std::os::fd::AsFd;
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
            Ok(listeners) => listeners.serve(on_signal()).await,
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
/// switched to non-blocking mode and driven by the runtime, so each read
/// and write is one system call on the task's own thread; tokio's
/// `stdin()` and `stdout()` hand each one to a blocking thread and back
/// instead. Anything else (a file, a terminal) uses those.
fn stdio() -> (
    Box<dyn AsyncRead + Unpin + Send>,
    Box<dyn AsyncWrite + Unpin + Send>,
) {
    let stdin = std::io::stdin().as_fd().try_clone_to_owned();
    let reader: Box<dyn AsyncRead + Unpin + Send> =
        match stdin.and_then(pipe::Receiver::from_owned_fd) {
            Ok(pipe) => Box::new(pipe),
            Err(_) => Box::new(tokio::io::stdin()),
        };
    let stdout = std::io::stdout().as_fd().try_clone_to_owned();
    let writer: Box<dyn AsyncWrite + Unpin + Send> =
        match stdout.and_then(pipe::Sender::from_owned_fd) {
            Ok(pipe) => Box::new(pipe),
            Err(_) => Box::new(tokio::io::stdout()),
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
