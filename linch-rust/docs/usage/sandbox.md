# Sandbox: where Bash runs

[Tiếng Việt](../vi/usage/sandbox.md)

Use this page to choose where an agent's `Bash` runs its commands, and to
know what each choice confines and what it does not.

By default `Bash` runs `/bin/sh -c` in the workspace as the server's user,
with only nine host variables in its environment. `execution` in the
agent config picks another backend:

| `backend` | Commands run | Needs |
|---|---|---|
| `local` (default) | Directly as the server's user | Nothing |
| `sandbox` | Under Landlock, a seccomp filter and rlimits, with a private home per session | Linux 6.12 or later (Landlock ABI 6) and `prlimit` (util-linux); no root, no user namespaces, no daemon |
| `docker` | In a fresh `docker run --rm` container per command | A `docker` client and a daemon the server's user can reach |

The choice applies to `Bash` in the foreground and in the background, so to
`BashOutput` and `BashKill` too. The file tools (`Read`, `Write`, `Edit`,
`Grep`, `Glob`) always run in the server, confined to the workspace; every
backend sees the same workspace directory, so a file `Bash` writes is the
file `Read` reads.

```python
from linch_rust import AgentConfig, SandboxExecution

sandbox: SandboxExecution = {"backend": "sandbox"}  # every limit has a default
config: AgentConfig = {
    "namespace": "app",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "workspace": "/srv/app/work",
    "execution": sandbox,
}
```

An agent whose backend cannot confine what its policy says does not open:
`OpenError` (code `-32011`) names the reason, such as a kernel
without Landlock ABI 6, a missing `prlimit`, a server running as root
(sandbox), a missing `docker` or an invalid policy. The backend is opened
only when the agent has `Bash`.

For every backend, a descriptor the server holds open is marked
close-on-exec before a command starts, so none passes to the command.

A backend other than `local` is part of the run contract: `Bash`'s
description ends with one sentence on what it confines, so the model knows,
and a run started with one policy does not resume under another. The local
backend adds nothing, so its contract is the one it always had.

---

## The `sandbox` backend

Each command is forked from a short-lived thread that first restricts
itself with Landlock and a seccomp filter; the child inherits both, and the
server's other threads stay unrestricted. `prlimit` then sets the limits and
execs `/bin/sh`, keeping the process group and the background lifeline.

| Field | Default | What it sets |
|---|---|---|
| `network` | `false` | Whether commands may open network sockets |
| `memory_bytes` | 2 GiB | Address space of each process (`RLIMIT_AS`) |
| `max_processes` | 512 | Processes and threads a command may add to those the server's user already runs (`RLIMIT_NPROC`, set to the user's count at the start plus this) |
| `cpu_seconds` | 1800 | CPU time of each process (`RLIMIT_CPU`): `SIGXCPU` at the limit, `SIGKILL` a second later |
| `file_size_bytes` | 1 GiB | Largest file a process may write (`RLIMIT_FSIZE`) |
| `read_paths` | none | Absolute paths a command may also read and execute (`/opt`, for one) |

The server must not be root: opening a sandbox fails when any of the
server's user ids is 0 or it holds a capability in its effective or
ambient set, since a command would keep them.

### What it confines

- **Files.** A command reads only the workspace, its private home, the
  system directories `/usr`, `/bin`, `/sbin`, `/lib*`, `/proc` and
  `/sys`, the listed entries of `/etc` below, the devices `/dev/null`,
  `/dev/zero`, `/dev/full`, `/dev/random` and `/dev/urandom`, and
  `read_paths`. It writes only the workspace and its home. The server
  user's home (keys, `~/.ssh`, cloud config), the server's terminal, the
  rest of `/tmp`, other sessions' homes, `/opt` and every other entry of
  `/etc` are closed.
- **`/etc`, by allowlist.** Only what a shell, coreutils, `git`, `python3`
  and `curl` read (found by running them in the sandbox under `strace`):
  `alternatives`, `ca-certificates*`, `crypto-policies/back-ends`,
  `debian_version`, `fonts`, `gai.conf`, `gitattributes`, `gitconfig`,
  `gnutls/config`, `group`, `host.conf`, `hosts`, `ld.so.cache`,
  `ld.so.conf`, `ld.so.conf.d`, `ld.so.preload`, `locale.alias`,
  `localtime`, `lsb-release`, `magic`, `magic.mime`, `mime.types`,
  `netconfig`, `networks`, `nsswitch.conf`, `os-release`, `passwd`,
  `pki/ca-trust/extracted`, `pki/tls/cert.pem`, `pki/tls/certs`,
  `pki/tls/openssl.cnf`, `protocols`, `python3*`, `resolv.conf`,
  `selinux/config`, `services`, `ssh/ssh_config`, `ssh/ssh_config.d`,
  `ssh/ssh_known_hosts`, `ssl/cert.pem`, `ssl/certs`, `ssl/openssl.cnf`,
  `terminfo`, `timezone`, `userdb` and `wgetrc` (a `*` entry stands for
  every name that starts so). A secret kept elsewhere in `/etc` (an
  application's config, `/etc/fstab` credentials) stays closed even when
  its permissions let the user read it. A program that needs another
  entry fails with `Permission denied`; name it in `read_paths`.
- **A private home per session.** `HOME` and `TMPDIR` name a directory of
  the session under a scratch directory of the agent. It lasts across the
  session's commands and is removed when the session closes
  (`Agent.close_session`) and with the agent.
- **Environment.** Only `PATH`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`,
  `USER` and `SHELL` pass from the server, with `HOME` and `TMPDIR`
  replaced, so no provider key is there. `/proc/<server pid>/environ` and
  every other process's `environ`, `mem` and `fd` are closed too: Landlock
  forbids tracing a process outside the sandbox.
- **Signals and other processes.** A command cannot signal a process
  outside its sandbox, so it cannot kill the server. Nor can it change
  another process's limits, priority or CPU affinity, though the server
  runs as the same user: `prlimit64` and the `sched_set*` calls take pid 0
  only, `setpriority` and `ioprio_set` the calling process only (`nice`,
  `ionice`, `taskset` and `ulimit` on itself still work).
- **Unix sockets.** `socket(AF_UNIX, ...)` always fails, and connecting to
  an abstract socket made outside the sandbox does too, so a command cannot
  reach the user's session bus, a Docker socket or another local service
  that would start something unconfined. `socketpair` still works.
- **Network off** (the default): every `socket` call fails with `EACCES`,
  TCP or UDP, and Landlock refuses TCP bind and connect as well. With
  `network: true` only `AF_INET` and `AF_INET6` sockets open: netlink,
  vsock, packet, crypto (`AF_ALG`) and every other family stay refused.
  Name resolution works without netlink (glibc's `AI_ADDRCONFIG` falls
  back); `/etc/resolv.conf`'s target is readable.
- **No new privileges or namespaces.** A setuid program gains nothing.
  The seccomp filter refuses with `EPERM` kernel modules, `kexec`,
  `reboot`, swap, every mount call, `pivot_root` and `chroot`; the user's
  keyrings (`keyctl`, `add_key`, `request_key`), `bpf`,
  `perf_event_open` and `userfaultfd`; and `unshare`, `setns` and `clone`
  with a `CLONE_NEW*` flag, so not even an unprivileged user namespace
  opens. `clone3`, whose flags the filter cannot read, gets `ENOSYS` and
  glibc falls back to `clone`. `io_uring`, whose requests the filter would
  not see, is refused.
- **Resources.** Each process is held to `memory_bytes`, `cpu_seconds` and
  `file_size_bytes`, and a command to `max_processes` more processes, so a
  fork bomb or a memory hog fails inside the command. Wall time is `Bash`'s
  timeout, as for every backend.

### What it does not confine

- Commands run as the server's user id: there is no user namespace (an
  AppArmor or sysctl restriction often disables them for unprivileged
  users), so the boundary is Landlock's, not the file permissions'.
- `/proc` and `/sys` are readable: a command sees every process's command
  line and status and the system's devices, as any local user does. Other
  processes' `environ`, `mem`, `fd` and `maps` stay closed (Landlock
  forbids tracing outside the sandbox). `/proc` is kept on purpose:
  without it `ps`, `free`, `uptime`, `nproc`-style reads of
  `/proc/cpuinfo` and `/proc/meminfo`, and anything reading `/proc/self`
  fail; Landlock cannot grant a command its
  own `/proc/self` and not the rest. Do not put secrets on the server's
  command line.
- Python's `multiprocessing` locks need `/dev/shm`, which is closed, so a
  `Pool` fails.
- Limits are per process, not per command: there is no cgroup, so no total
  memory, no CPU share, and `max_processes` is headroom over the user's
  current count, which other work of the same user shares.
- Landlock gates opening files, not looking them up: `stat`, `ls -d` and a
  path's existence stay visible outside the allowed directories.
- `memory_bytes` limits address space, not resident memory: runtimes that
  reserve large ranges up front (the JVM, Go, Node) may fail under a small
  value.
- 32-bit programs are killed: the seccomp filter allows only the server's
  own architecture.
- With `network: true` a command reaches anything the host can over TCP or
  UDP, including services on `localhost`.
- The file tools, MCP stdio servers and the virtual filesystem's disk
  backend run in or from the server as before; the sandbox is for `Bash`.

## The `docker` backend

Each command runs in `docker run --rm` with Python Linch's limits and the
workspace mounted at its own path:

| Field | Default | What it sets |
|---|---|---|
| `image` | `python:3.12-slim` | The image; name a digest for a reproducible world. Must not start with `-` |
| `docker_path` | `docker` on the server's `PATH` | The client |
| `network` | `none` | `--network`: `none`, `bridge` or a network's name; `host` and `container:<name>` are refused (Python Linch leaves Docker's default bridge on) |
| `memory_bytes` | 2 GiB | `--memory` |
| `cpu_millis` | 2000 | `--cpus`, in thousandths of a CPU |
| `pids_limit` | 512 | `--pids-limit` |
| `read_only_root` | `false` | `--read-only` |
| `tmpfs` | none | `--tmpfs` entries |
| `user` | the server's uid and gid | `--user`, so files a command writes belong to the user the file tools run as |
| `allow_root` | `false` | Whether a command may run as root in the container: without it a `user` of `0`, `root` or `0:<gid>`, or a server running as root (whose uid is the default), is refused |

Every capability is dropped and `no-new-privileges` is set. The workspace
is bind-mounted with `--mount type=bind,...` (a workspace path with a
comma or a quote is refused), and `--quiet` keeps an image pull's
progress out of the command's output. The container gets `Bash`'s
variables except `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR`, passed by
name so no value is on a command line. The client also gets the host's
`DOCKER_*` and `XDG_RUNTIME_DIR` to find its daemon.

The container belongs to the daemon, not to the server, so killing the
client's process group does not stop it: a command that times out, is
cancelled or killed, or whose session closes is removed with
`docker rm -f`. Every command, foreground or background, runs under the
lifeline: when the server dies, its watchdog kills the client first (so
a client still creating the container cannot start it) and then removes
the container by name. Each container is labelled
`linch.boot=<host boot id>:<server pid>:<start time>`; opening a docker
backend removes, in the background, labelled containers of this host boot
whose server process is gone. It never removes those of a live server or
of another host sharing the daemon.

A container confines what Docker's runtime confines: its own file system,
process tree and network namespace, the limits above. Nothing outside the
workspace persists between commands. The daemon is root's, so a client that
may reach it may do anything; keep its socket away from tenants (the
sandbox backend refuses Unix sockets for that reason).

## On a server

A remote tenant may use only the backends its server lists, each with the
operator's policy (`allow.execution`, see [Server](./server.md#config)).
A config naming a backend must equal an entry exactly, so a client cannot
pick its own image, network or limits; a config naming none gets the first
entry. So `"execution": [{"backend": "sandbox"}]` holds every agent of the
tenant to the sandbox and refuses `local`. The default is
`[{"backend": "local"}]`, and `Bash` still needs `exec_tools`.

A background run (`job.enqueue` or a schedule's fire) is admitted again by
the server whose worker runs it, at every attempt, so it gets that
server's backend for the tenant. A `local_namespaces` root is trusted as a
stdio client is and keeps its config's choice.

## Python Linch

Python's `ExecutionBackend` pairs a shell and a file backend
(`src/linch/execution/backend.py`), with `LocalExecutionBackend`,
`RemoteExecutionBackend` and the `DockerBackend` shell
(`src/linch/tools/execution.py:229`). linch-rust has the same seam
(`ExecutionBackend` in `crates/linch-tools/src/exec.rs`) with three built-in
backends chosen by config instead of objects; it has no pluggable remote
shell, and the sandbox backend has no Python counterpart. Python's
`TempDirIsolation` gives each subagent branch its own working copy; the
sandbox's private home is per session, and the workspace stays shared with
the file tools.
