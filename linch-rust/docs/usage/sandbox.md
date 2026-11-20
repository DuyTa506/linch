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
| `remote` | On a host your client supplies, not on the server (see [Remote execution](./remote-execution.md)) | A client connection that serves the commands |

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
execs `/bin/sh`, keeping the process group. With the optional `linch-reaper`
helper on (see [What ends with a command](#what-ends-with-a-command)) the
command runs under it, and the helper is inside both restrictions too; for
`linch-server` as its own helper that puts the server executable on the
command's read-and-execute list.

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
- **`/etc`, by allowlist.** What a shell, coreutils, `git`, `python3`
  and `curl` read on Ubuntu (traced with `strace`, then run in the
  sandbox), plus the certificate and configuration paths other
  distributions use (`pki/*` and `crypto-policies` on Fedora,
  `ssl/cert.pem` on Alpine and Arch):
  `alternatives`, `ca-certificates*`, `crypto-policies/back-ends`,
  `debian_version`, `fonts`, `gai.conf`, `gitattributes`, `gitconfig`,
  `gnutls/config`, `group`, `host.conf`, `hosts`, `ld.so.cache`,
  `ld.so.conf`, `ld.so.conf.d`, `ld.so.preload`, `locale.alias`,
  `localtime`, `lsb-release`, `magic`, `magic.mime`, `mime.types`,
  `netconfig`, `networks`, `nsswitch.conf`, `os-release`, `passwd`,
  `pki/ca-trust/extracted`, `pki/tls/cert.pem`, `pki/tls/certs`,
  `pki/tls/openssl.cnf`, `protocols`, `python3*`, `resolv.conf`,
  `selinux/config`, `services`, `ssl/cert.pem`, `ssl/certs`,
  `ssl/openssl.cnf`, `terminfo`, `timezone` and `userdb` (a `*` entry
  stands for every name that starts so). A secret kept elsewhere in `/etc` (an
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
  `perf_event_open` and `userfaultfd`; System V IPC (`shmget`, `shmat`,
  `shmctl`, `msgget`, `msgsnd`, `msgrcv`, `msgctl`, `semget`, `semop`,
  `semtimedop`, `semctl`) and POSIX message queues (`mq_open`, `mq_unlink`,
  `mq_timedsend`, `mq_timedreceive`), whose objects are checked by user id
  and so would let a command reach another program of the server's user;
  and `unshare`, `setns` and `clone`
  with a `CLONE_NEW*` flag, so not even an unprivileged user namespace
  opens. `clone3`, whose flags the filter cannot read, gets `ENOSYS` and
  glibc falls back to `clone`. `io_uring`, whose requests the filter would
  not see, is refused. The filter is a denylist: a syscall it does not name
  is allowed, so Landlock and the refusal to run as root are the boundary,
  and the filter only closes the doors listed here.
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
- System V IPC is refused (see above), so a program that needs it fails with
  `Operation not permitted`: `fakeroot` (`shmget: Operation not permitted`; it
  worked before this was refused), Perl's `IPC::SysV` users and any tool that
  shares memory, queues or semaphores that way. A database that takes a System
  V shared memory segment at start (PostgreSQL does for its interlock) cannot
  start in the sandbox; PostgreSQL was not tested here. Use `local` or `docker`
  for such work.
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

### What ends with a command

A command's own process group does not hold it: a process that calls `setsid`
or `setpgid` leaves the group, and one that double-forks is handed to `init`.
What ends when a command exits, times out, is cancelled or killed, or its
session closes, depends on the platform and on a helper that is **off by
default**:

| | `local` and `sandbox`, default (helper off) | `local` and `sandbox` on Linux, helper on | `docker` |
|---|---|---|---|
| The command's process group | yes | yes | yes (the client's) |
| A descendant that left the group (`setsid`, `setpgid`) | no | yes | yes (the container is removed) |
| A double-forked daemon, a process that ignores `SIGTERM` | no | yes | yes (the container is removed) |
| A process not started by the command (`systemd-run --user`, `at`, `cron`, a D-Bus activation) | no | no | yes (nothing runs on the host) |
| The server dying, even by `SIGKILL` | a background command's group | every command | yes |

Where the default holds a command by its process group, a process that leaves
the group outlives the call, and in the sandbox it counts against the next
command's `max_processes` headroom.

The helper, `linch-reaper`, is a small program that runs each local command,
holds what it leaves behind (a child subreaper, with the children list in
`/proc`) and kills it; it works unprivileged and needs no cgroup or namespace.
Turn it on with `LINCH_REAPER=1` for `linch-server`, which runs itself as the
helper (nothing extra to ship), or `LINCH_REAPER=/path/to/linch-reaper` to name
a program; `LINCH_REAPER=` (empty) turns it off, and a Rust host can turn on its
own helper with `linch_tools::reaper::register`. The path must be absolute; any other value
(`0`, `true`, a relative path) leaves it off with one warning in the log. It is
off by default because
it costs a process per command: a `local` `true` takes a median of 0.6 to
0.8 ms without it, 1.9 to 2.7 ms with the 350 KB `linch-reaper` and 3.5 to
4.2 ms with `linch-server` as its own helper (a 30 MB executable to load); the
sandbox goes from 21.5 to 22.9 ms to 24 to 26 ms and 25 to 28 ms (release
build, 200 spawns a round, CPU at 0.8 to 2.5 GHz on a shared host).

In the `sandbox` the helper's executable must lie outside every directory the
command can write (the workspace root and the sandbox's private homes), because
the server trusts what the helper reports: a helper that lies under them, for
example `linch-server` in a project's `.venv` inside the workspace, is refused
with one warning in the log and commands run without it. Install it outside the
workspace, at an absolute path.

With the helper on, `$PPID` inside a command is the helper, and a command can
kill it (it runs as the same user, in the same Landlock domain, and the seccomp
filter does not deny `kill`): the server then still kills the shell's process
group and ends the call, but what had already left the group survives. On a
kernel without `/proc/<pid>/task/<tid>/children` or `pidfd_open` (before 5.3)
only the process group is killed. The design and the mechanisms not used are
in [Bash](../architecture/tools.md#what-ends-with-a-command).

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
`linch.boot=<host boot id>:<pid namespace>:<uid>:<server pid>:<start time>`;
opening a docker backend removes, in the background, labelled containers
whose server ran on this host boot, in this pid namespace and as this
user, and is gone. A container whose server it cannot see (another host,
another container, another user under `hidepid`) is left alone. A
container a killed client had asked for but not yet started can remain
until its server is gone; it runs nothing.

A container confines what Docker's runtime confines: its own file system,
process tree and network namespace, the limits above. Nothing outside the
workspace persists between commands. The daemon is root's, so a client that
may reach it may do anything; keep its socket away from tenants (the
sandbox backend refuses Unix sockets for that reason).

### Isolation levels

A container shares the host's kernel. The system calls a command makes go to
that kernel, so a bug in it can let a command out of the container. The
`sandbox` backend has the same residual risk (Landlock and seccomp are kernel
features too). For commands that come from untrusted content, or on a server
shared by tenants, run the containers under a stronger runtime. From weakest
to strongest:

| Level | Isolation | Notes |
|---|---|---|
| `local` | None beyond the server's user | Trusted agents only |
| `sandbox` | Landlock, seccomp, rlimits on the host kernel | No daemon; Linux 6.12 or later |
| `docker` with `runc` (Docker's default) | Namespaces, cgroups, dropped capabilities on the host kernel | The residual risk is a kernel or runtime escape |
| `docker` with gVisor (`runsc`) | A user-space kernel answers the container's system calls; few reach the host's | Linux only; see the costs below |
| `docker` with a VM runtime (Kata Containers, Firecracker) | A guest kernel in a virtual machine | The strongest; needs virtualization on the host |

gVisor needs no change to linch-rust, since it is a Docker runtime. Install
`runsc` and register it with the daemon, then either make it the daemon's
default, in `/etc/docker/daemon.json`, so every `docker run` the backend
issues uses it:

```json
{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}, "default-runtime": "runsc"}
```

or leave the default alone and point the policy's `docker_path` at a wrapper
that adds the runtime to `run` only:

```sh
#!/bin/sh
# /usr/local/bin/docker-runsc
if [ "$1" = run ]; then shift; exec docker run --runtime=runsc "$@"; fi
exec docker "$@"
```

```python
execution = {"backend": "docker", "docker_path": "/usr/local/bin/docker-runsc"}
```

On a server, set the same `docker_path` in the tenant's `allow.execution`
entry, which a config must equal.

What gVisor costs: a command that makes many system calls runs slower (reports
put it at tens of percent for such work) and a container takes longer to
start; some system calls and `/proc` and `/sys` files are not implemented, so a
tool that needs one fails; it runs on Linux only. Measure your own commands
before you rely on it. For a VM-level runtime, register it with the daemon the
same way and use it in place of `runsc`.

The docker backend is not verified against a Docker daemon in linch-rust's
CI, and not against gVisor or a VM runtime at all: try your configuration
with a command that must fail (reading a file outside the workspace) before
you trust it.

## Live tests

`python/tests/live/test_ts_backends.py` runs each guarantee below through a
real `linch-server` and a real backend, with a scripted model (no LLM). Each
test name states the guarantee. A case skips with its reason when the host
cannot run it (not Linux, no Landlock ABI 6, no docker access), so CI without
docker stays green.

| Guarantee | `local` | `sandbox` | `docker` |
|---|---|---|---|
| A command returns its output and exit status; the run survives a failing one | yes | yes | yes |
| A command starts in the workspace and its files are the file tools' files | yes | yes | yes |
| No server secret reaches the command's environment | yes | yes | yes |
| The server's own `environ` is unreadable from the command | n/a | yes | yes |
| A write outside the workspace does not reach the host | n/a | yes | yes |
| A host file beside the workspace cannot be read | n/a | yes | yes |
| A forbidden syscall is an error result, not a crash | n/a | yes | n/a |
| A command cannot `ptrace` a process outside the sandbox | n/a | yes | n/a |
| No capabilities, `no-new-privileges`, `mount` refused | n/a | n/a | yes |
| Memory, CPU time and file size limits fail inside the command | n/a | yes | memory only |
| A fork bomb stays bounded and the run survives | n/a | yes | yes |
| The network is off by default | n/a | yes | yes |
| A timeout kills the command's process group (the container for `docker`) | yes | yes | yes |
| A timeout also ends a process that left the group (`setsid`, a daemon) | only with the helper on | only with the helper on | yes |
| A 100 MB stream is capped and the server's memory stays small | yes | yes | yes |
| Cancelling a run leaves no process of the command's group and no container | yes | yes | yes |
| `kill -9` of the server leaves no process (a background command; any command with the helper on) and no container | yes | yes | yes |
| `docker inspect` shows the user, dropped capabilities, limits, read-only root, network and the one mount | n/a | n/a | yes |

The `sandbox` and `local` cases run with `LINCH_SERVER_BIN` set:

```sh
cd linch-rust/python
LINCH_SERVER_BIN=/path/to/linch-server pytest -m live tests/live/test_ts_backends.py
```

The `docker` cases also need `LINCH_LIVE_DOCKER=1`, so they never run by
accident. They use `LINCH_LIVE_DOCKER_IMAGE` (default `alpine:3.20`, pulled when
absent), cap each container (256 MiB, half a CPU, 64 processes), and name them
`linch-live-<id>` through a `docker_path` wrapper, so
`docker ps -a --filter name=linch-live-` finds any leftover. A user who
reaches the daemon through the `docker` group runs them as:

```sh
LINCH_LIVE_DOCKER=1 LINCH_SERVER_BIN=/path/to/linch-server \
  sg docker -c 'pytest -m live tests/live/test_ts_backends.py'
```

Other guarantees (Landlock paths, `/etc` allowlist, namespaces, Unix sockets,
the sandbox with the network on) are covered by `crates/linch-tools/tests/sandbox.rs`
and `python/tests/live/test_ts_sandbox.py`.

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
(`ExecutionBackend` in `crates/linch-tools/src/exec.rs`) with four built-in
backends chosen by config instead of objects; the `remote` backend takes the
place of Python's remote shell by asking the client's `ExecutionHost` to run
each command ([Remote execution](./remote-execution.md)), and the sandbox
backend has no Python counterpart. Python's
`TempDirIsolation` gives each subagent branch its own working copy; the
sandbox's private home is per session, and the workspace stays shared with
the file tools.
