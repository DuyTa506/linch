//! `linch-server`: the Linch sidecar.
//!
//! - `linch-server` speaks the `linch-rpc` protocol on stdin and stdout,
//!   one JSON-RPC message per line, and serves many runs at once. It
//!   exits when stdin closes.
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
use tokio_util::sync::CancellationToken;

const USAGE: &str = "usage: linch-server [--config FILE]";

#[tokio::main]
async fn main() -> ExitCode {
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
    let served = match config {
        None => linch_rpc::serve(tokio::io::stdin(), tokio::io::stdout()).await,
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
