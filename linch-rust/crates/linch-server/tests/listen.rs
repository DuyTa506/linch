//! The binary with `--config`: it serves the socket the config names,
//! reads its token from the environment, cannot have its environment
//! read by another process, and stops cleanly on SIGTERM.

mod common;

use std::process::Stdio;
use std::time::Duration;

use common::TOKEN;
use linch_rpc::Client;
use linch_rpc::protocol::{Authenticated, method};
use serde_json::json;
use tokio::io::AsyncReadExt;
use tokio::net::UnixStream;
use tokio::process::Command;
use tokio::time::{sleep, timeout};

const SECRET: &str = "sk-linch-p11c-listen-secret-4242";

fn binary() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_linch-server"));
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    command
}

#[tokio::test]
async fn the_binary_serves_its_config_and_stops_on_sigterm() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("linch.sock");
    let config = json!({"socket": socket, "tenants": {"acme": {
        "tokens": [{"env": "LINCH_P11C_TOKEN"}], "allow": {"scripted_provider": true}}}});
    let file = dir.path().join("server.json");
    std::fs::write(&file, config.to_string()).unwrap();
    let mut child = binary()
        .arg("--config")
        .arg(&file)
        .env("LINCH_P11C_TOKEN", TOKEN)
        .env("DEEPSEEK_API_KEY", SECRET)
        .spawn()
        .unwrap();
    let pid = child.id().unwrap();
    let mut waited = Duration::ZERO;
    while !socket.exists() {
        assert!(
            waited < Duration::from_secs(20),
            "the socket never appeared"
        );
        sleep(Duration::from_millis(20)).await;
        waited += Duration::from_millis(20);
    }

    let (reader, writer) = UnixStream::connect(&socket).await.unwrap().into_split();
    let (client, mut notifications) = Client::connect(reader, writer);
    let auth = json!({"token": TOKEN});
    let who: Authenticated = client.call(method::CONNECTION_AUTH, &auth).await.unwrap();
    assert_eq!(who.tenant, "acme");
    let text = common::run(&client, &mut notifications, "from the binary").await;
    assert_eq!(text, "from the binary");
    #[cfg(target_os = "linux")]
    {
        // Never print what was read: it is the server's environment.
        let read = std::fs::read(format!("/proc/{pid}/environ")).map(|_| ());
        let kind = read.err().map(|error| error.kind());
        assert_eq!(
            kind,
            Some(std::io::ErrorKind::PermissionDenied),
            "environ was readable"
        );
    }

    let killed = Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .await
        .unwrap();
    assert!(killed.success());
    let status = timeout(Duration::from_secs(20), child.wait())
        .await
        .expect("the server stops on SIGTERM")
        .unwrap();
    assert!(status.success(), "{status}");
    assert!(!socket.exists(), "the socket file is removed");
    let mut stderr = String::new();
    let mut pipe = child.stderr.take().unwrap();
    pipe.read_to_string(&mut stderr).await.unwrap();
    assert!(stderr.contains("listening on unix:"), "{stderr}");
    assert!(
        !stderr.contains(TOKEN) && !stderr.contains(SECRET),
        "{stderr}"
    );
}

#[tokio::test]
async fn a_bad_command_line_or_config_exits_2() {
    let dir = tempfile::tempdir().unwrap();
    let unset = dir.path().join("unset.json");
    let config = json!({"http": "127.0.0.1:0", "tenants": {"acme": {
        "tokens": [{"env": "LINCH_P11C_UNSET_TOKEN"}]}}});
    std::fs::write(&unset, config.to_string()).unwrap();
    let missing = dir.path().join("missing.json");
    let cases: [Vec<&std::ffi::OsStr>; 3] = [
        vec!["--bogus".as_ref()],
        vec!["--config".as_ref(), missing.as_os_str()],
        vec!["--config".as_ref(), unset.as_os_str()],
    ];
    for args in cases {
        let output = binary().args(&args).output().await.unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert!(!output.stderr.is_empty(), "{args:?}");
    }
}
