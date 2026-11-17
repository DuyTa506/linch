# Remote execution: Bash on your machine

[Tiếng Việt](../vi/usage/remote-execution.md)

Use this page when an agent's `Bash` should run its commands somewhere the
server is not: your laptop, a build box, a sandbox service you already
operate. With `"execution": {"backend": "remote"}` the server asks the client
that opened the agent to run each command, over the connection it already
has, and streams back stdout, stderr and the exit code. The other backends
(`local`, `sandbox`, `docker`) are on the [Sandbox](./sandbox.md) page.

```python
from linch_rust import AgentConfig, RemoteExecution, Server, SubprocessHost

remote: RemoteExecution = {"backend": "remote"}
config: AgentConfig = {
    "namespace": "app",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "workspace": "/srv/app/work",   # the server's directory; see "Files"
    "execution": remote,
}

async with Server() as server:
    agent = await server.open_agent(config, execution=SubprocessHost(cwd="/home/me/project"))
```

`SubprocessHost` is the reference host: it runs each command as `/bin/sh -c`
in its own process group with `asyncio.create_subprocess_exec`. To run
commands another way (SSH, a sandbox API), write an `ExecutionHost`: four
methods, `start`, `read`, `kill` and `close`.

## Security: where the commands run

**A remote command runs wherever the host runs it, with the host's
privileges.** The server's sandbox, Landlock, rlimits and container do not
apply; the only isolation is the host's own. Run it where you would be
content to let the model run a shell.

- The server sends the command, the policy's `cwd` and the variables of
  `Bash`'s allowlist (`TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, and any the agent
  added). It leaves out `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR`, which
  describe the server and mean nothing on the host (as the `docker` backend
  does); `SubprocessHost` fills them from its own environment. No provider
  key or other server secret is in that environment.
- The host sees the model's commands and must be trusted with them; the
  server trusts the host's answers about output and exit codes, nothing
  more.
- A tenant of `linch-server` is **denied by default**: `allow.execution` must
  list the remote entry, policy included, and a config may name only an entry
  exactly as listed (as for `sandbox` and `docker`). A tenant whose list
  starts with `remote` gets it even when its config names no backend, so
  every client of such a tenant must serve the host.
- An error shown to the model never carries the server's paths or the
  connection's details. A message the host itself raises when it refuses a
  start is shown as is.

## Who a remote agent can serve

The host is the connection. So an agent with a remote backend:

- **cannot be scheduled or run in a worker.** `schedule.create` and
  `job.enqueue` refuse it with `-32040` (`not_background`, `data.reason`
  `remote_execution`); `schedule_tools` is refused at open; a worker given
  such a definition refuses it. There is no one to run the command.
- **is bound to its connection.** If the connection closes, the commands it
  started are not the server's to stop any more: the host must kill them
  (`SubprocessHost` does, when the connection ends or the agent closes).
- **has no `Bash` without a host.** `AgentConfig.open` outside a connection
  (Rust `open_with_remote(..., None)`, the Python extension) fails with a
  message that says so, and the Python client raises `ValueError` before
  opening.

## Files: the file tools act on the server

Read this before enabling it. `Read`, `Write`, `Edit`, `Grep` and `Glob` run
in the server's process, on its `workspace` directory. A remote host's
commands run in another place, so a file `Bash` writes there is not the file
`Read` reads here. Rather than mix the two worlds silently:

| Policy | Tools the model has |
|---|---|
| `{"backend": "remote"}` | `Bash`, `BashOutput`, `BashKill` only |
| `{"backend": "remote", "shared_workspace": true}` | all eight; the host sees the server's workspace at the same path (a shared mount, or the same machine), commands start there, so one world |

Set `shared_workspace` only when that is true. `workspace` is still
required: it names the server directory (unused by the model without
`shared_workspace`). To give the model files on the host, run them through
`Bash`, or declare host tools.

Policy fields:

| Field | Default | What it sets |
|---|---|---|
| `cwd` | the host's choice | The directory commands start in on the host |
| `shared_workspace` | `false` | Whether the host sees the workspace at the same path (registers the file tools; commands start in the workspace unless `cwd` is set) |

The policy is part of the run contract through `Bash`'s description, as the
other backends' are; an agent without the backend keeps the contract it had.

## How it behaves

Foreground and background commands, `BashOutput` and `BashKill` all go
through the host.

- **Timeout and cancel.** The server enforces them. At the deadline, or when
  the run is cancelled, it sends `exec.kill` and reports `timed out` or
  `aborted` only after the host answered that the command is dead.
- **A host that does not answer is `interrupted`.** If the host stops
  answering reads (6 s), errors, closes the connection, or does not confirm a
  kill (5 s), the call ends with code `interrupted` and `[interrupted: ...
  the command's outcome is unknown; it may still be running]`. The output
  that did arrive is kept; no exit code, signal or timeout is invented. A
  background process reads the same way in `BashOutput` (`status:
  interrupted`).
- **Background commands** have no pid on the server (`metadata` has
  `process_id` only), die with their session (`session.close`), `BashKill`
  and the agent, and, through the host, with the connection.
- Output is bounded as for every backend: the first 4000 and last 26000 bytes
  of each stream reach the model.

## The protocol

Three server requests on the connection that opened the agent (see the
[server](./server.md) page for the wire format); nothing else is declared,
the config's `execution` is the declaration.

| Request | Params | Result |
|---|---|---|
| `exec.start` | `agent, exec_id, command, cwd?, env?, background?, session_id` | `{}` |
| `exec.read` | `agent, exec_id, wait_ms` | `{stdout?, stderr?, exit?}` |
| `exec.kill` | `agent, exec_id` | `{stdout?, stderr?, exit?}` |

`exec.read` is a long poll: answer at once when there is output or the
command ended, else after `wait_ms` (1000) with `{}`. `exit` is `{"code": n}`
or `{"signal": n}`, comes once and after all the output, and the host then
forgets the command. A read must take output only when it answers: the server
cancels a read it gives up on (`$/cancelRequest`) and loses nothing then.
`exec.kill` kills the command's whole process group, waits until it is dead
and returns what it printed since the last read with its `exit`.
The Python `ExecutionHost` carries the same contract.

Differences from Python Linch's `RemoteExecutionBackend`: Python bundles a
remote shell and a remote filesystem and runs both in the host's world; here
the file layer stays on the server and is withheld unless the workspace is
shared. Python's `run(command, cwd, timeout, signal)` is one call; here it is
`start`, `read` and `kill`, so a long command streams and a kill is
confirmed.
