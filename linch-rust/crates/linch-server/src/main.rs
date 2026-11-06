//! `linch-server`: the Linch sidecar. It speaks the `linch-rpc` protocol on
//! stdin and stdout, one JSON-RPC message per line, and serves many runs at
//! once. It exits when stdin closes. Provider keys come from its
//! environment; it never writes them anywhere.
//!
//! Exit status: 0 after stdin closes, 1 if reading stdin or writing stdout
//! fails (the reason goes to stderr).

#![forbid(unsafe_code)]

use std::process::ExitCode;

#[tokio::main]
async fn main() -> ExitCode {
    match linch_rpc::serve(tokio::io::stdin(), tokio::io::stdout()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("linch-server: {error}");
            ExitCode::FAILURE
        }
    }
}
