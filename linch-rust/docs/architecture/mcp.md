# MCP client

> Part of the [linch-rust architecture guide](./README.md).

`linch-mcp` (`crates/linch-mcp/src/`) connects to Model Context Protocol
servers and registers their tools in a `ToolRegistry`. One `McpServer` is one
server connection with a background supervisor that keeps the registry in
step with it. Use this page when you change connection handling, the tool
mapping, or a limit.

Python Linch documents its MCP tools for users in
[usage/tools](../../../docs/usage/tools.md#mcp-tools). The Rust client is built
on `rmcp`, the official Rust SDK, and speaks protocol 2026-07-28 with a
fallback to the 2025-11-25 `initialize` handshake. Legacy SSE, resources,
prompts, sampling, elicitation and OAuth are not supported
(`crates/linch-mcp/src/lib.rs:22-24`). The crate is Unix-only: a stdio server
leads its own process group.

## Configuration

`McpServerConfig` (`crates/linch-mcp/src/config.rs:145`):

| Field | Default (`new`) | Meaning |
|---|---|---|
| `name` | required | The `<server>` in `mcp__<server>__<tool>`. ASCII letters, digits, `-` and single `_` between them, so the tool name splits cleanly (`config.rs:191`) |
| `transport` | required | `Stdio { command, args, env }`, `Http { url, headers }` or `Custom(Arc<dyn Connector>)` |
| `trust_annotations` | `false` | Whether a `readOnlyHint` tool gets the `read` scope |
| `call_timeout` | 300 s | Longest wait for one tool call before it is cancelled |
| `reconnect` | `ReconnectPolicy::default()` | 5 attempts, 0.5 s doubling to 30 s, 30 s per attempt (`config.rs:131-141`) |

`validate()` (`config.rs:174`) refuses a bad name and a zero call or attempt
timeout; headers are checked when the transport is built. `Custom` takes a
`Connector` (`crates/linch-mcp/src/connector.rs:110`) for in-process servers
and tests: it opens a transport and runs `Handler::serve` over it.

## Tool mapping

On connect, and on every relist, each listed tool becomes one registration
(`crates/linch-mcp/src/tool.rs:146-174`):

- **Name.** `tool_name(server, tool)` (`tool.rs:36`) is
  `mcp__<server>__<tool>`, with every character of the tool name outside
  `[A-Za-z0-9_-]` replaced by `_` (MCP allows `.`, providers do not). The
  registry refuses a result over 64 characters.
- **Scope.** `exec`, or `read` for a tool whose `readOnlyHint` is true when the
  config sets `trust_annotations`. An `exec` tool asks for approval in the
  default permission mode and never runs in parallel.
- **Schema.** The server's `inputSchema`; a missing `type` or `properties`
  defaults to an empty object schema.
- **Result** (`crates/linch-mcp/src/result.rs:15`). Text blocks join with
  newlines into the output value. Images go to `metadata.images` and leave
  `[image]` in the text; audio, binary resources and other blocks leave a
  placeholder. With no blocks, `structuredContent` is used, else
  `(no content)`. An `isError` result becomes a `ToolOutputError` with code
  `mcp_tool_error` and the same text.

A tool the registry refuses (name too long, name taken, invalid schema) is
left out and reported by `McpServer::skipped()`; it does not fail the connect
(`crates/linch-mcp/src/catalog.rs:10-18`).

### Error codes

Every failure is a `ToolOutputError` the model can read; the code is for the
host (`crates/linch-mcp/src/codes.rs`):

| Code | When |
|---|---|
| `aborted` | `ToolContext::cancel` fired |
| `timeout` | No result within `call_timeout` |
| `mcp_unavailable` | Reconnecting, unavailable or disconnected; the connection dropped during the call; or the request could not be sent (an HTTP error status on its POST) |
| `mcp_tool_error` | The server ran the tool and returned `isError: true` |
| `mcp_protocol_error` | A JSON-RPC error, or a response this client does not support (`input_required`, a task) |

## Lifecycle

`McpServer::connect(config, registry)` (`crates/linch-mcp/src/server.rs:82`)
validates the config, makes one attempt (connect, subscribe, list) within
`attempt_timeout`, routes calls to the new connection, then registers the
tools, so a tool is never visible before it can be called (`server.rs:317`).
A failed first attempt is `McpError::Connect` and registers nothing; it is
not retried.

`ServerStatus` (`server.rs:27`): `Connected`, `Reconnecting`, `Unavailable`
(every reconnect attempt failed; the tools stay registered and fail with
`mcp_unavailable`) and `Closed`. `status()` reads it; `watch_status()`
returns a `watch::Receiver`.

### Relist

The supervisor lists the tools again on `notifications/tools/list_changed`
and after every reconnect (`server.rs:219-252`). From protocol 2026-07-28 that
notification arrives only on a `subscriptions/listen` stream, which the
client opens before it lists, so no change is missed in between
(`crates/linch-mcp/src/listener.rs:65`, `server.rs:344`); older servers send
it unprompted to the `Handler`. A subscription the server ends normally is
renewed after 1 s.

The relist diff touches only what changed (`catalog.rs:33-72`):

- an unchanged tool keeps its registration;
- a changed tool is replaced first and its old registration disposed after,
  so the name never leaves the registry and keeps its position (the prompt
  cache depends on the order);
- a tool the server no longer lists is disposed.

A failed or timed-out relist keeps the current tools; a closed transport
during a relist starts a reconnect.

### Reconnect

A connection counts as lost when the transport closes, or for a stdio server
when a write fails, since its stdin is gone (`tool.rs:94-100`). The status
becomes `Reconnecting`, calls fail with `mcp_unavailable`, and the supervisor
retries per `ReconnectPolicy`: attempt `n` waits
`min(max_delay, initial_delay * 2^n)` (`config.rs:123`). Each attempt
reconnects, resubscribes and relists. After the last failure the status is
`Unavailable` until the server is disconnected. `attempts: 0` never
reconnects.

### Cancel

A call that ends without the server's result is cancelled on the server
(`crates/linch-mcp/src/codes.rs:5-10`): on `ToolContext::cancel`, on
`call_timeout`, and when the caller drops the call's future (the scheduler's
call timeout). The `Abandon` guard (`tool.rs:279-310`) sends
`notifications/cancelled` in a background task, so a server that stopped
reading cannot keep the call from returning; over streamable HTTP rmcp closes
the request's stream instead. The client applies its own timeout rather than
rmcp's, because rmcp's waits for the cancel's send (`tool.rs:228-229`).

### Close

`disconnect()` (`server.rs:154`) disposes every registration at once, then
waits for the transport to close. Dropping the handle without it disposes the
registrations at once and closes the server in the background
(`server.rs:169-173`). A sync that races a close registers nothing: it checks
the close token under the catalog lock (`server.rs:332-337`).

For a stdio server:

- **Process group.** The server leads its own group, so what it starts
  (`npx`, `uvx`, `sh -c`) goes with it (`connector.rs:154-157`).
- **Group kill.** rmcp signals the group only when the server outlives its
  grace period. `KillGroup` sends `SIGKILL` to the whole group as soon as the
  server has exited, so a server that exits on stdin EOF leaves no children
  (`connector.rs:190-232`).
- **Bounded close.** rmcp's own close takes about 6 s at most (3 s after
  stdin closes, 3 s more after `SIGTERM`). When the server has stopped
  reading and a write has filled its stdin, that close would wait forever;
  `BoundedClose` gives up after 10 s and dropping the transport sends
  `SIGKILL` to the group (`connector.rs:31-34`, `connector.rs:163-188`).

## Limits

What one server can make the client hold or wait for
(`crates/linch-mcp/tests/limits.rs`):

| Limit | Value | Where |
|---|---|---|
| `tools/list` pages per listing | 100 | `server.rs:20` |
| Tools kept from one listing | 1000 | `server.rs:23` |
| One connect, resubscribe or relist attempt | `attempt_timeout` (30 s) | `server.rs:241`, `server.rs:285` |
| One tool call | `call_timeout` (300 s) | `tool.rs:264` |
| Cancels waiting on one connection | 64; later ones are dropped | `tool.rs:29` |
| Result text | 30 000 bytes, cut on a character boundary | `result.rs:10` |
| Tool argument summary | 80 characters | `tool.rs:23` |
| Stdio close | 10 s, then `SIGKILL` | `connector.rs:34` |

## Redaction

- **Environment.** A stdio server inherits only `HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM` and `USER` from the host, the same list the MCP Python SDK
  uses; everything else comes from the config's `env`
  (`connector.rs:27-29`, `connector.rs:146-153`).
- **Debug output.** `McpTransport`'s `Debug` prints environment and header
  names, never their values (`config.rs:83-101`), so logging a config does not
  leak a token.

## Example

This compiles at this revision; `main` runs the offline checks, and
`session` needs a running server:

```rust
use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use linch_core::ToolRegistry;
use linch_mcp::{McpError, McpServer, McpServerConfig, McpTransport, ServerStatus, tool_name};

/// A stdio server whose read-only tools may run in parallel.
fn files_server() -> McpServerConfig {
    let args = ["--root".to_owned(), "/srv/data".to_owned()];
    let mut config = McpServerConfig::new("files", McpTransport::stdio("files-mcp", args));
    config.trust_annotations = true;
    config.call_timeout = Duration::from_secs(60);
    config
}

/// A streamable HTTP server with a bearer token from the host's secrets.
fn search_server(token: &str) -> McpServerConfig {
    let mut headers = BTreeMap::new();
    headers.insert("Authorization".to_owned(), format!("Bearer {token}"));
    let url = "https://mcp.example/mcp".to_owned();
    McpServerConfig::new("search", McpTransport::Http { url, headers })
}

/// Connect, report what the registry refused, and disconnect.
async fn session(tools: Arc<ToolRegistry>) -> Result<(), McpError> {
    let server = McpServer::connect(files_server(), Arc::clone(&tools)).await?;
    for skipped in server.skipped() {
        eprintln!("{} left out: {}", skipped.remote_name, skipped.reason);
    }
    if server.status() == ServerStatus::Connected {
        println!("tools: {:?}", server.tools());
    }
    server.disconnect().await; // disposes every registration first
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    assert_eq!(files_server().validate(), Ok(()));
    assert_eq!(search_server("t").validate(), Ok(()));
    assert!(!format!("{:?}", search_server("secret").transport).contains("secret"));
    assert_eq!(tool_name("files", "read.text"), "mcp__files__read_text");
    let _ = session; // needs a running server
}
```

## Tests

- `tests/duplex.rs`: an in-process rmcp server over a duplex pipe; scopes,
  results, relist, reconnect, cancel, timeout, drop and disconnect.
- `tests/stdio.rs`: a real child process (`examples/stdio_server.rs`); clean
  exit, killing a server that stopped reading, killing what it started.
- `tests/http.rs`: streamable HTTP on 127.0.0.1, including an HTTP error on
  one POST failing only that call.
- `tests/limits.rs`: the bounds above.

---

Back to the [architecture index](./README.md).
