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
fallback to the 2025-11-25 `initialize` handshake. The HTTP+SSE transport, resources,
prompts, sampling, elicitation and OAuth are not supported
(`crates/linch-mcp/src/lib.rs:22-24`). The crate is Unix-only: a stdio server
leads its own process group.

## Configuration

`McpServerConfig` (`crates/linch-mcp/src/config.rs:169`):

| Field | Default (`new`) | Meaning |
|---|---|---|
| `name` | required | The `<server>` in `mcp__<server>__<tool>`. ASCII letters, digits, `-` and single `_` between them, so the tool name splits cleanly (`config.rs:215`) |
| `transport` | required | `Stdio { command, args, env }`, `Http { url, headers }` or `Custom(Arc<dyn Connector>)` |
| `trust_annotations` | `false` | Whether a `readOnlyHint` tool gets the `read` scope |
| `call_timeout` | 300 s | Longest wait for one tool call before it is cancelled |
| `reconnect` | `ReconnectPolicy::default()` | 5 attempts, 0.5 s doubling to 30 s, 30 s per attempt (`config.rs:155-165`) |

`validate()` (`config.rs:198`) refuses a bad name and a zero call or attempt
timeout; headers are checked when the transport is built. `Custom` takes a
`Connector` (`crates/linch-mcp/src/connector.rs:119`) for in-process servers
and tests: it opens a transport and runs `Handler::serve` over it.

The HTTP transport uses a `reqwest` client of the crate's own
(`connector.rs`, `http_client`), built once per connection attempt. It never
follows a redirect: a 3xx answer ends that request as an HTTP error, so the
transport's headers and the session id stay on the configured URL (rmcp's
default client follows same-origin 307 and 308, which would take them to
another path of the origin; the provider adapters refuse every redirect the
same way). It keeps no idle connection, as rmcp's client does, so every call
opens a connection: on loopback, 50 sequential calls took about 0.36 ms each
unpooled and 0.26 ms pooled, but 2 of 9 pooled runs of 50 calls had one call
of 31 to 48 ms (the TCP delayed-ACK stall rmcp's comment names, after a response body
that was not read to its end) against none of 9 unpooled, so pooling stays off.
The proxy environment (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) applies as in
reqwest's default client, as before.

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
- **Result** (`crates/linch-mcp/src/result.rs:38`). Text blocks join with
  newlines into the output value. Images go to `metadata.images` (at most 8, and
  4 MiB of base64 data and MIME types in all, a MIME type of at most 255
  bytes; an image over a limit is left out and the text says which:
  `[image omitted: more than 8 images]`,
  `[image omitted: over 4 MiB of image data]` or
  `[image omitted: MIME type over 255 bytes]`; a later image that fits is
  still kept) and leave `[image]` in the text;
  audio, binary resources and other blocks leave a
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

`McpServer::connect(config, registry)` (`crates/linch-mcp/src/server.rs:97`)
validates the config, makes one attempt (connect, subscribe, list) within
`attempt_timeout`, routes calls to the new connection, then registers the
tools, so a tool is never visible before it can be called (`server.rs:355`).
A failed first attempt is `McpError::Connect` and registers nothing; it is
not retried.

`ServerStatus` (`server.rs:42`): `Connected`, `Reconnecting`, `Unavailable`
(every reconnect attempt failed; the tools stay registered and fail with
`mcp_unavailable`) and `Closed`. `status()` reads it; `watch_status()`
returns a `watch::Receiver`.

### Relist

The supervisor lists the tools again on `notifications/tools/list_changed`
and after every reconnect (`server.rs:234-271`). From protocol 2026-07-28 that
notification arrives only on a `subscriptions/listen` stream, which the
client opens before it lists, so no change is missed in between
(`crates/linch-mcp/src/listener.rs:65`, `server.rs:382`); older servers send
it unprompted to the `Handler`. A subscription the server ends normally is
renewed after 1 s.

The relist diff touches only what changed (`catalog.rs:33-72`):

- an unchanged tool keeps its registration;
- a changed tool is replaced first and its old registration disposed after,
  so the name never leaves the registry and keeps its position (the prompt
  cache depends on the order);
- a tool the server no longer lists is disposed.

Relists are rate limited (`server.rs`, `MIN_RELIST_INTERVAL`): the first
notification after a quiet spell relists at once, and one that arrives within
1 s of the last relist's start, or while a relist runs, waits for the end of
that second; the notifications that arrive meanwhile share the one relist that
follows, so a server that sends a burst costs at most one listing a second
(a reconnect relists at once, as before). A listing is
cut at 1000 tools (`MAX_TOOLS`) or 4 MiB of tools as the server sent them
(`MAX_LIST_BYTES`: the JSON of each tool, so the title, the output schema, the
icons and `_meta` count with the name, the description and the input schema):
the tools before the bound are kept in order, the rest are left out, no further
page is asked for, and a `tracing` warning (target `linch_mcp`, with the server
name, the tools kept and their bytes) says so. A listing whose first tool alone
is over 4 MiB is refused.

A failed, refused or timed-out relist keeps the current tools; a closed
transport during a relist starts a reconnect.

### Reconnect

A connection counts as lost when the transport closes, or for a stdio server
when a write fails, since its stdin is gone (`tool.rs:94-100`). The status
becomes `Reconnecting`, calls fail with `mcp_unavailable`, and the supervisor
retries per `ReconnectPolicy`: attempt `n` waits
`min(max_delay, initial_delay * 2^n)` (`config.rs:147`). Each attempt
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

`disconnect()` (`server.rs:169`) disposes every registration at once, then
waits for the transport to close. Dropping the handle without it disposes the
registrations at once and closes the server in the background
(`server.rs:184-188`). A sync that races a close registers nothing: it checks
the close token under the catalog lock (`server.rs:370-375`).

For a stdio server:

- **Process group.** The server leads its own group, so what it starts
  (`npx`, `uvx`, `sh -c`) goes with it (`connector.rs:163-166`).
- **Group kill.** rmcp signals the group only when the server outlives its
  grace period. `KillGroup` sends `SIGKILL` to the whole group as soon as the
  server has exited, so a server that exits on stdin EOF leaves no children
  (`connector.rs:199-241`).
- **Lifeline.** A small `/bin/sh` watchdog per server reads its own stdin, a
  pipe whose write end only the client holds, and kills the server's group
  when it reaches end of file. The kernel closes that pipe when the client
  dies, `SIGKILL` included, so a server that ignores stdin EOF and `SIGTERM`
  does not outlive it, as with the `Bash` tool's lifeline. The watchdog
  stands outside the server's group, uses only builtins, and is killed when
  the connection is dropped (`connector.rs`, `start_lifeline`).
- **Bounded close.** rmcp's own close takes about 6 s at most (3 s after
  stdin closes, 3 s more after `SIGTERM`). When the server has stopped
  reading and a write has filled its stdin, that close would wait forever;
  `BoundedClose` gives up after 10 s and dropping the transport sends
  `SIGKILL` to the group (`connector.rs:31-34`, `connector.rs:172-197`).

## Limits

What one server can make the client hold or wait for
(`crates/linch-mcp/tests/limits.rs`):

| Limit | Value | Where |
|---|---|---|
| `tools/list` pages per listing | 100 | `server.rs:22` |
| Tools kept from one listing | 1000; the rest are left out, with a `tracing` warning | `server.rs:25` |
| Bytes kept from one listing (each tool as the server sent it, as JSON) | 4 MiB; the tools before the bound are kept in order and the rest are left out, with a `tracing` warning; a first tool alone over it fails the connect with `ConnectError::ListTooLarge`, and a relist then keeps the current tools | `server.rs` (`MAX_LIST_BYTES`) |
| Time between two `list_changed` relists | 1 s, notifications in between share one relist | `server.rs` (`MIN_RELIST_INTERVAL`) |
| One connect, resubscribe or relist attempt | `attempt_timeout` (30 s) | `server.rs:257`, `server.rs:323` |
| One tool call | `call_timeout` (300 s) | `tool.rs:264` |
| Cancels waiting on one connection | 64; later ones are dropped | `tool.rs:29` |
| Result text, and the text of a tool error (a JSON-RPC error's message included) | 30 000 bytes, cut on a character boundary | `result.rs:10`, `tool.rs` (`coded`) |
| Images kept from one result | 8, and 4 MiB of base64 data and MIME types in all; a MIME type of at most 255 bytes; an image over a limit is left out, with a note naming the limit | `result.rs` (`MAX_IMAGES`, `MAX_IMAGE_BYTES`) |
| Tool argument summary | 80 characters | `tool.rs:23` |
| Stdio close | 10 s, then `SIGKILL` | `connector.rs:34` |

## Redaction

- **Environment.** A stdio server inherits only `HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM` and `USER` from the host, the same list the MCP Python SDK
  uses; everything else comes from the config's `env`
  (`connector.rs:27-29`, `connector.rs:155-162`).
- **Debug output.** `McpTransport`'s `Debug` prints environment and header
  names, never their values (`config.rs:87-125`), and shows an HTTP URL with
  its user name, password and query replaced by `redacted` (a string that is
  not a URL is shown as `[redacted]`), so logging a config does not leak a
  token.
- **Errors.** reqwest names the URL in the text of a failed request, and the
  userinfo and query of that URL often hold a token. The HTTP transport wraps
  the client (`connector.rs`, `QuietClient`) so every error it raises leaves the
  URL out (`reqwest::Error::without_url`); this holds for a failed connect, a
  reconnect, and the text of a failed tool call. A JSON-RPC error message in
  `ConnectError::Handshake`, `Listen` or `ListTools` is cut at 30 000 bytes like
  a tool error's, and those variants have no `source`, so the message is not
  repeated in full down the error chain.

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
