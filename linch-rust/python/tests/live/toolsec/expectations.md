# toolsec expectations (written before any run, 2026-11-18)

Area: tools, sandbox, remote execution, MCP, skills/subagents, tenants/admission, provider request
settings/scrubbing, observability. Stack tip f69d97b2 (PRs #144 #147 #159 #161 #162 #165 #166 #167).
Source of each claim: docs under linch-rust/docs/usage (U) and architecture (A), PR bodies (PR).
Servers: debug binary for correctness, release binary for every measurement.

## Brief-versus-docs notes (decided before running)
- B1. The brief says "run the server with the OTLP exporter". The docs (U observability.md, A otel.md) say
  spans and metrics are built by the PYTHON CLIENT (`linch_rust.otel.Telemetry`) from the event stream;
  `linch-server --help` has only `--config` and the server source has no OTLP code. So the OTLP check drives
  the client's `Telemetry` with an exporter that POSTs to my local /v1/traces receiver. Server-side OTLP: NOT RUN
  (does not exist by design). The OTLP exporter/protobuf packages are not installed, so the exporter is a small
  custom SpanExporter that POSTs OTLP/JSON.
- B2. `output_limit` is produced only by the REMOTE backend (A tools.md, remote.rs OUTPUT_BUDGET const 256 MiB;
  PR #161). Local Bash has no such code: its capture keeps head 4000 / tail 26000 bytes. The 256 MiB flood is
  therefore tested through `SubprocessHost`; for local Bash I record what a flood does (expected: truncation
  markers, command ends by itself or by timeout, no `output_limit`).
- B3. No separate cache hit/miss/clear EVENTS exist: the only observable is `tool_call_end.cache_hit` (U events.md:270)
  and a tracing warning "the tool cache stopped for an agent" on a failed clear. I test hit/miss through
  `cache_hit` and the clear through a write tool invalidating.
- B4. The docker backend needs a daemon the user can reach: `docker` gives "permission denied" on the socket here,
  so docker behaviour is NOT RUN. The `allow_root` refusal for a tenant is admission only and needs no daemon.
- B5. No `mcp` package: the MCP server is a minimal JSON-RPC stdio server (extends live/mcp_tiny_server.py).

## Addendum (still before the MCP, tenant and measurement runs)
- A1. 4.3 corrected after reading agent.md#loop_guard: an MCP tool answering the same text repeatedly ends by loop guard
  at the 3rd identical call+result (`max_identical_tool_calls` 3, reason `repeated_tool_call`); listed in `polls` it is waited on
  until `max_idle_turns` (25) identical answers in a row: 12 calls succeed, 40 calls end by loop guard near call 26.
- A2. The scripted provider cannot know a random `process_id`, so BashOutput/BashKill scenarios and everything needing a
  dynamic reply use a local programmable OpenAI-compatible gateway (vLLM wire format, no LLM): `ts_fakemodel.py`.
  The scripted provider is still used for the measurements.
- A3. 3.3: `BashOutput` for an error result carries no metadata to the event stream (events.py docstring), so the
  documented `status: output_limit` is checked through `error_code` and the text only.

## 1. Bash tool (local backend, scripted provider)
| id | expected observable | basis |
|---|---|---|
| 1.1 | `echo hi; pwd` runs in the workspace, result text has output and `[exit code 0]`, not an error | U tools.md Workspace tools |
| 1.2 | `sleep 60` with `timeout_ms` 1500: error code `timeout`, call duration < 6 s, the sleep process is gone | U tools.md; A tools.md:295 |
| 1.3 | A command printing > 30000 bytes: result keeps first 4000 and last 26000 bytes of the stream with a truncation marker between; total result text < 31500 bytes | A tools.md:298 |
| 1.4 | `run_in_background` returns at once with `process_id` matching `bash_[0-9a-f]{12}` and `pid` in metadata; `BashOutput` with `wait_ms` returns the output and `[exit code 0]`; a running one ends with `still running` | U tools.md Background commands |
| 1.5 | `BashKill` kills the process group (the child `sleep` too) | same |
| 1.6 | 5th background process of a session fails with `too_many_processes` | U tools.md |
| 1.7 | `agent.close_session` kills a background process of that session; no orphan under the server | U / A |
| 1.8 | A client connection that drops (client process SIGKILLed) leaves 0 orphan children under the server pid after <= 5 s (`pgrep -P`), for a background `sleep 300`; for a foreground `sleep 300` in flight the docs say a foreground command dies with its call (the run is dropped with the connection) | U server.md Liveness; A tools.md lifeline |
| 1.9 | Server SIGKILL: the background command's group dies within ~5 s (lifeline); a foreground one may survive (documented) | A tools.md:322 |
| 1.10 | Output flood on local Bash (`yes | head -c 300M`): result is truncated (head+tail); code is NOT `output_limit` | B2 |
| 1.11 | Env allowlist: with `LINCH_TEST_PG_DSN` and a canary variable in the SERVER's environment, `env` through Bash lists only PATH, HOME, TERM, LANG, LC_ALL, LC_CTYPE, USER, SHELL, TMPDIR (plus PWD/SHLVL/_ from sh); neither the DSN nor the canary appears in `env`, in `cat /proc/self/environ`, nor in `cat /proc/$PPID/environ` (server is not dumpable) | U server.md "What the server's environment protects" |

## 2. Sandbox backend (Linux, Landlock ABI 6 required; kernel here 7.0)
| id | expected | basis |
|---|---|---|
| 2.1 | In the sandbox: write inside the workspace works; write to a literal outside path (a dir I own, /tmp/<x>/outside.txt, and a server-user home path) fails `Permission denied`; reading the server user's home file is denied; `/proc/<server pid>/environ` unreadable | U sandbox.md What it confines |
| 2.2 | network off: AF_INET socket() raises EACCES (PermissionError); AF_UNIX socket also fails; `socketpair` works | U sandbox.md |
| 2.3 | `network:true`: AF_INET works, AF_NETLINK/AF_PACKET refused | U |
| 2.4 | `clone3` returns ENOSYS (glibc falls back so `sh -c 'true'` still runs); `io_uring_setup` refused; `unshare -r true` fails EPERM; `mount` EPERM | U sandbox.md seccomp paragraph |
| 2.5 | Denylist wording: one harmless syscall the filter does not name (e.g. `getrandom`, `sched_getaffinity`, `membarrier`, `getcpu`) succeeds | U sandbox.md ("a syscall it does not name is allowed") |
| 2.6 | env in sandbox: only PATH, TERM, LANG, LC_*, USER, SHELL plus HOME and TMPDIR replaced by a private dir; no canary | U |
| 2.7 | Opening a sandbox as a server that runs as root is refused (cannot test: not root) -> NOT RUN | U |
| 2.8 | docker backend | NOT RUN (B4) |
| 2.9 | A tenant whose `allow.execution` lists a `docker` entry without `allow_root` cannot choose `allow_root: true` (config differs from the entry -> `NotAllowed` kind `execution`); a tenant with the default list cannot choose sandbox/docker/remote (`kind execution`) | U sandbox.md On a server; PR #144 gaps |

## 3. Remote execution (#166/#147/#161/#167)
| id | expected | basis |
|---|---|---|
| 3.1 | `SubprocessHost` runs `echo; pwd` in `cwd`, `[exit code 0]`; only `Bash/BashOutput/BashKill` in `init.tools` (no file tools) unless `shared_workspace` | U remote-execution.md |
| 3.2 | timeout kills: `Bash sleep 60 timeout_ms 1500` -> code `timeout` and the process group is dead | U |
| 3.3 | a flood past 256 MiB (`yes | head -c 300M`) ends with error code `output_limit` and the text `[output limit reached (256 MiB); the command was killed]`; background: `BashOutput` metadata `status: output_limit`. Time recorded | U/PR #161 |
| 3.4 | a custom host that records every `ExecStart` sees `principal == {tenant,user,roles}` of the run that made the call, per run; absent for a run with none | PR #167 / U |
| 3.5 | client connection dropped with a background command running via SubprocessHost: clean close and agent close -> command dead; client process SIGKILLed -> host cannot act, expect the command to SURVIVE (host's duty, documented); recorded either way | U "bound to its connection" |
| 3.6 | tenant without `allow.execution` listing remote: `open_agent` with remote -> `NotAllowed` kind `execution`; with the exact entry -> works and Bash runs through the client's host; a config differing from the entry (e.g. `shared_workspace`) -> refused | U / PR #166 |
| 3.7 | `schedule`/`enqueue` of a remote-Bash agent refused `-32040 not_background` | U |

## 4. MCP (stdio, tiny server)
| id | expected | basis |
|---|---|---|
| 4.1 | `init.tools` contains `mcp__<name>__<tool>` for each tool of the server; a call returns its text; scope `exec` unless `trust_annotations` and readOnly (so approval asked in `default` mode) | U tools.md MCP tools |
| 4.2 | a tool that returns `isError` yields a `tool_call_end` with `is_error` true and the run continues | A mcp.md |
| 4.3 | `polls` listing: an agent calling the same MCP tool with identical input 12 times in a row does NOT end by loop guard when the tool is in `polls`, and does end by loop guard without it (identical inputs >= 10 in latest 50) | U agent.md loop_guard, mcp_servers.polls |
| 4.4 | closing the agent / the connection: the MCP child is gone | A mcp.md |
| 4.5 | `kill -9` of the `linch-server` (not the child): the MCP child (even one ignoring stdin EOF and SIGTERM, `MCP_STUBBORN`) is dead within ~5 s (lifeline watchdog) | A mcp.md:147-158 |
| 4.6 | a tenant: MCP command not in `mcp_commands` -> `NotAllowed` kind `mcp_command`; env name not listed -> `mcp_env`; entry listing `PATH` -> server config refused at startup, exit 2 | U server.md |

## 5. Skills and subagent with a real model (vLLM, tiny)
| id | expected | basis |
|---|---|---|
| 5.1 | A skill in `<config_dir>/skills/<name>/SKILL.md`: the model calls the `Skill` tool (ends without error) and the answer holds the code word that only the skill body contains | U skills.md |
| 5.2 | A read-only subagent: the parent calls `Subagent`, its result reaches the parent and the answer holds the code word; the subagent cannot call a write tool it was not given | U subagents.md |

## 6. Tenants and admission (two tenants, one server, `allow.*` absent)
Each refusal is `NotAllowed` (-32004) with `.kind`, before anything starts:
`scripted_provider` absent -> `provider`; `provider_request` -> `provider_request`; `execution` -> `execution`;
`workflow` absent -> `kind workflow` at `workflow.start`; `memory` -> `memory`; `task_ledger` -> `task_ledger`;
`schedule_tools`/`create_schedule` without max_schedules -> `schedule_tools` / `schedules`; `base_url` not in
`provider_base_urls` -> `provider_base_url`; no `max_turns` and no budget -> `run_bound`; principal of another
tenant -> `principal`; namespace of another tenant -> `namespace`; permission mode skip_dangerous -> `permission_mode`;
`auto_allow` -> `auto_allow`; `tool_cache` -> `tool_cache`; workspace outside roots -> `workspace`.
Limits: `max_connections` 2 -> third login `LimitReached kind connections` (HTTP 429); `max_workflows` 1 ->
second concurrent `workflow.start` `LimitReached kind workflows`; `max_schedules` 1 -> second schedule `LimitReached schedules`;
`min_schedule_interval_ms` 60000 -> interval 1000 `NotAllowed schedule_interval`; `max_agents` -> `agents`.
Isolation: tenant A guessing tenant B's run id / session / workflow / schedule / job id -> `RunNotFound` /
not found / empty list, never data. Tool cache: same read tool call from two principals in one namespace with
`scope: namespace` is a miss for the second (cache_hit false) and a hit for the same principal's repeat (#162);
`invalidate_on_write` clears: after a write tool a repeat is a miss. Error text: no refusal message contains
the Postgres DSN, a server path, a token, or a header value.

## 7. Provider request settings and scrubbing (#159, #162)
- 7.1 positive control: a local gateway that answers HTTP 400 with its request headers echoed in the body: the
  value of a non-credential 6-byte custom header appears in the run's provider error text (this is the control
  that shows the echo reaches the error text).
- 7.2 `x-gateway-token: <6 bytes>` (credential-named) is absent from the run's error text.
- 7.3 non-credential header (`x-trace-id: <6 bytes>`): documented NOT scrubbed under 8 bytes (U providers.md:83-86);
  an 8+ byte non-credential value IS scrubbed. I record both.
- 7.4 tenant with `provider_request` allowed cannot set `authorization`, `cookie`, `proxy-authorization`,
  `x-forwarded-for`, `forwarded`: refused `NotAllowed provider_request`; `host`: refused at open (NotAllowed or OpenError; recorded); the refusal text names the header, not its value.
- 7.5 tenant without `provider_request` sending `request` -> `NotAllowed provider_request`.
- 7.6 `extra_body: {"tool_choice": "none", "messages": [...], "model": ...}` does not replace the real
  `tool_choice`/`messages` on the wire (the gateway records the request body): the wire body keeps the real messages.
- 7.7 `LINCH_LOG=debug` server stderr contains no header value (unique random values grep) and no key.
- 7.8 default `LINCH_LOG` (warn): a deliberately provoked warning shows on stderr; `RUST_LOG` is read when `LINCH_LOG` unset; an invalid `LINCH_LOG` reports on stderr and uses warn.

## 8. Observability
- 8.1 With `Telemetry` and a custom OTLP/JSON exporter to a local receiver: POST /v1/traces received; span names
  `invoke_agent`, `step`, `chat <model>`, `execute_tool <tool>`; `invoke_agent` and `chat` carry
  `gen_ai.usage.input_tokens`/`output_tokens` equal to the sums of the run's `usage` events; `execute_tool` carries
  `gen_ai.tool.name`; `gen_ai.operation.name` values `invoke_agent`, `chat`, `execute_tool`.
- 8.2 Server stderr default shows `warn` only; `LINCH_LOG=info` shows more; the provoked warning of 7.8.
- 8.3 Server-side OTLP export: NOT RUN (B1).

## Measurements (release binary, load noted)
- M1 tool-call overhead: scripted provider, host no-op tool and Bash `true # i`; 200 calls each, p50/p95 per call
  (from `tool_call_start`/`tool_call_end` event `ts_ms` and total run wall time / calls). No stated number; reported
  as observed. Expectation: a no-op host tool round trip dominated by IPC (< 5 ms p50), Bash spawn p50 < 10 ms on the local backend.
- M2 tool cache: a 200 ms read tool: miss ~ 200+ ms, hit < 20 ms (a hit skips the tool's run).
- M3 admission cost: open 20 agents as tenant vs local stdio, p50 each; no stated number, expect the same order (< 5 ms difference without Postgres).
- M4 server RSS after 100 runs: no stated number; expect bounded growth (a plateau) between run 50 and run 100.
- M5 Bash spawn time p50 of 50: local backend `true`.
