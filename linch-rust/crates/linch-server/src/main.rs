//! `linch-server`: the Linch sidecar.
//!
//! - `linch-server` speaks the `linch-rpc` protocol on stdin and stdout,
//!   one JSON-RPC message per line, and serves many runs at once on one
//!   thread (file tools still use tokio's blocking threads). It exits
//!   when stdin closes.
//! - `linch-server --config FILE` serves the Unix socket and HTTP
//!   listeners FILE names (see the `linch_server` crate docs), and runs a
//!   worker when FILE has a `worker` section, until SIGINT or SIGTERM,
//!   then ends every connection, waits for the worker's jobs in flight
//!   (at most `worker.drain_timeout_ms`; the rest are handed back), and
//!   exits. A stdio server never runs a worker.
//! - `linch-server --migrate` and `linch-server --check` apply or read the
//!   database schema and exit (`--dsn-env NAME` or `--config FILE`; see
//!   the `linch_server` crate docs for the output and exit status).
//!
//! Provider keys come from its environment and are not passed to the
//! processes it starts (MCP servers, Bash), but they stay in its own
//! environment; on Linux the process is marked not dumpable, so a child
//! running as the same user cannot read `/proc/<pid>/environ` or its
//! memory. No key or token is written to any output.
//!
//! The server's log (the worker's, the scheduler's and the workflow
//! journal's `tracing` records) goes to stderr, never to stdout: warnings
//! and errors by default, more with `LINCH_LOG` (or `RUST_LOG`), such as
//! `LINCH_LOG=info` or `LINCH_LOG=linch_worker=debug,warn`.
//!
//! Exit status: 0 after stdin closes or a stop signal, 1 if I/O fails, 2
//! for a bad command line or config, or a worker database a newer release
//! migrated (the reason goes to stderr). `--migrate` and `--check` add 3
//! (the database holds a migration this binary does not know) and 4 (`--check`:
//! the schema is behind).

#![forbid(unsafe_code)]

use std::path::PathBuf;
use std::process::ExitCode;

use linch_server::{
    BackgroundWorker, Listeners, PATH, SchemaAction, ServerConfig, TokenSource, run_schema,
};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::unix::pipe;
use tokio_util::sync::CancellationToken;

const USAGE: &str = "usage: linch-server [--config FILE]\n       \
                      linch-server (--migrate | --check) (--dsn-env NAME | --config FILE)";

/// What the command line asks for.
enum Command {
    /// Speak on stdio, or serve the listeners and worker `FILE` names.
    Serve(Option<PathBuf>),
    /// Apply or read the database schema, then exit.
    Schema(SchemaAction, DsnSource),
}

/// Where a schema command reads the DSN.
enum DsnSource {
    /// An environment variable.
    Env(String),
    /// The `worker` section of a config file.
    Config(PathBuf),
}

fn parse(args: &[String]) -> Option<Command> {
    let action = |flag: &str| match flag {
        "--migrate" => Some(SchemaAction::Migrate),
        "--check" => Some(SchemaAction::Check),
        _ => None,
    };
    match args {
        [] => Some(Command::Serve(None)),
        [flag, path] if flag == "--config" => Some(Command::Serve(Some(PathBuf::from(path)))),
        [flag, source, value] => {
            let source = match source.as_str() {
                "--dsn-env" => DsnSource::Env(value.clone()),
                "--config" => DsnSource::Config(PathBuf::from(value)),
                _ => return None,
            };
            Some(Command::Schema(action(flag)?, source))
        }
        _ => None,
    }
}

fn main() -> ExitCode {
    #[cfg(target_os = "linux")]
    if let Err(error) =
        rustix::process::set_dumpable_behavior(rustix::process::DumpableBehavior::NotDumpable)
    {
        eprintln!("linch-server: cannot mark the process not dumpable: {error}");
        return ExitCode::FAILURE;
    }
    linch_server::init_logging(|name| std::env::var(name).ok());
    let args: Vec<String> = std::env::args().skip(1).collect();
    let config = match parse(&args) {
        Some(Command::Serve(config)) => config,
        Some(Command::Schema(action, source)) => return schema(action, source),
        None => {
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

/// Apply or read the schema of the database `source` names; the exit
/// status says whether it is current.
fn schema(action: SchemaAction, source: DsnSource) -> ExitCode {
    let fail = |message: String| {
        eprintln!("linch-server: {message}");
        ExitCode::from(2)
    };
    let dsn = match dsn_of(&source) {
        Ok(dsn) => dsn,
        Err(message) => return fail(message),
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build();
    let runtime = match runtime {
        Ok(runtime) => runtime,
        Err(error) => return fail(format!("cannot start the runtime: {error}")),
    };
    let outcome = runtime.block_on(run_schema(action, &dsn));
    if !outcome.stdout.is_empty() {
        println!("{}", outcome.stdout);
    }
    if !outcome.stderr.is_empty() {
        eprintln!("linch-server: {}", outcome.stderr);
    }
    ExitCode::from(outcome.code)
}

fn dsn_of(source: &DsnSource) -> Result<String, String> {
    match source {
        DsnSource::Env(name) => std::env::var(name)
            .map_err(|_| format!("environment variable {name} is unset or not UTF-8")),
        DsnSource::Config(path) => {
            let text = std::fs::read_to_string(path)
                .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
            let config = ServerConfig::from_json(&text).map_err(|error| error.to_string())?;
            let section = (config.worker.as_ref())
                .ok_or("the config has no worker section: pass --dsn-env NAME instead")?;
            match &section.dsn {
                TokenSource::Value(dsn) => Ok(dsn.clone()),
                TokenSource::Env(name) => std::env::var(name)
                    .map_err(|_| format!("environment variable {name} is unset or not UTF-8")),
            }
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
            Ok((listeners, worker)) => {
                let stop = on_signal();
                let working = async {
                    match worker {
                        Some(worker) => worker.serve(stop.clone()).await,
                        None => Ok(()),
                    }
                };
                let (_, worked) = tokio::join!(listeners.serve(stop.clone()), working);
                worked.map_err(|error| std::io::Error::other(error.to_string()))
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

/// Read the config, bind its listeners, reporting where they listen, and
/// start its worker when it has a `worker` section.
async fn listen(path: &PathBuf) -> Result<(Listeners, Option<BackgroundWorker>), ExitCode> {
    let fail = |message: String| {
        eprintln!("linch-server: {message}");
        ExitCode::from(2)
    };
    let text = std::fs::read_to_string(path)
        .map_err(|error| fail(format!("cannot read {}: {error}", path.display())))?;
    let config = ServerConfig::from_json(&text).map_err(|error| fail(error.to_string()))?;
    let env = |name: &str| std::env::var(name).ok();
    let built = config.build(env).map_err(|error| fail(error.to_string()))?;
    let worker = match &config.worker {
        Some(section) => Some(
            (BackgroundWorker::start(section, &built.tenants, &built.server_id, env).await)
                .map_err(|error| fail(error.to_string()))?,
        ),
        None => None,
    };
    let listeners = (Listeners::bind(&config, built.tokens).await)
        .map_err(|error| fail(format!("cannot listen: {error}")))?;
    eprintln!("linch-server: server id {}", built.server_id);
    if let Some(socket) = listeners.socket_path() {
        eprintln!("linch-server: listening on unix:{}", socket.display());
    }
    if let Some(address) = listeners.http_addr() {
        eprintln!("linch-server: listening on ws://{address}{PATH}");
    }
    if let Some(worker) = &worker {
        let roots = worker.roots().join(", ");
        eprintln!("linch-server: worker running the jobs of {roots}");
    }
    Ok((listeners, worker))
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
