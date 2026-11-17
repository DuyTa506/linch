# Agent config

Use this page to write the config an agent opens from, with every field and
its default, and to give the agent host tools and callbacks.

[<- Usage guide](./README.md)

In `linch_rust` an agent is data. You write an `AgentConfig` (a typed dict),
hand it to a running `Server`, and get back an `Agent` handle. Everything a
turn does runs inside the `linch-server` sidecar; it calls back into your
process only for the host tools and callbacks you declared.

```text
Server ─────── one linch-server child process, spoken to over stdio
  └── Agent ── opened from an AgentConfig; has a handle and a contract
        └── session id ── one conversation, a string you choose
              └── Run ── one prompt; iterate it for its envelopes
```

- Models and keys are on [Providers](./providers.md).
- Host tools, the workspace tools, permissions and approvals are on
  [Tools](./tools.md).
- Starting, resuming, approving and cancelling runs is on [Runs](./runs.md).
- Memory and Postgres stores are on [Stores](./stores.md).

---

## Open an agent

```python
import asyncio
import tempfile

from linch_rust import AgentConfig, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn

workspace = tempfile.mkdtemp()

config: AgentConfig = {
    "namespace": "support",
    "spec": {
        "model": MODEL,
        "system_prompt": "You answer support questions. Be brief.",
        "max_turns": 20,
        "max_output_tokens": 2048,
        "call_timeout_ms": 60_000,
        "compaction": {"threshold_tokens": 100_000, "keep_recent_turns": 6},
    },
    "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
    "workspace": workspace,
    "bash": False,
    "permissions": {"mode": "accept_edits", "project_root": workspace},
    "filesystem": {"offload_threshold_tokens": 20_000},
    "tasks": True,
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("ticket-1", "Hi")
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(agent.handle, result.final_text)


asyncio.run(main())
```

```text
agent-1 Hello.
```

`Server(binary=None, *, env=None)` spawns the sidecar. `binary` overrides
where the server binary is found, and `env` is the server's whole environment,
where it reads provider keys (this process's environment when absent). Use it
as an async context manager: leaving the block closes the server's input, the
server drops its runs where they stand and exits, and it is killed if it has
not exited after 10 seconds. `Server.connect(address, token=...)` reaches a
server that is already running for many clients instead
([Server](./server.md#connect-from-python)); everything below works the same
on it.

`server.open_agent(config, tools=(), callbacks=(), *, telemetry=None)` opens
one agent:

| Argument | What it is |
|---|---|
| `config` | The `AgentConfig` below. A plain dict works at run time; the typed dict lets a type checker catch a misspelt field |
| `tools` | Host tools made with `@tool`, run in this process ([Tools](./tools.md#host-tools)) |
| `callbacks` | Intercept callbacks made with `@callback`, answered in this process ([below](#callbacks)) |
| `telemetry` | OpenTelemetry providers for this agent's runs ([Observability](./observability.md)) |

It returns an `Agent` with three attributes: `handle` (the server's name for
it, such as `agent-1`), `contract` (a digest of what its runs depend on; see
[The run contract](#the-run-contract)) and `skipped` ([below](#skipped-definitions)).
One server holds many agents; open them once and reuse them for every
session. `await agent.close()` frees one on the server: its stores and its
MCP servers, which are disconnected and waited for. With memory stores its
runs and sessions are gone; with Postgres stores they stay for another agent
of the same config. A closed agent raises `AgentNotFound`, and closing an
agent while one of its runs is still going raises `RunBusy`.

### Skipped definitions

With `skills` or `subagents` on, a skill directory or subagent file that
cannot be used is left out and the others load. `agent.skipped` lists each
one as a `Skipped(path, reason, detail)`:

```python
import asyncio
import tempfile
from pathlib import Path

from linch_rust import AgentConfig, Server
from linch_rust.scripted import MODEL

root = Path(tempfile.mkdtemp()).resolve()
(root / "skills" / "notes").mkdir(parents=True)  # no SKILL.md
(root / "agents").mkdir()
(root / "agents" / "reviewer.md").write_text("Review the code.")  # no frontmatter

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": []},
    "skills": True,
    "subagents": True,
    "config_dir": str(root),
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        for skipped in agent.skipped:
            print(Path(skipped.path).relative_to(root), skipped.reason)


asyncio.run(main())
```

```text
skills/notes missing_skill_md
agents/reviewer.md missing_frontmatter
```

`reason` is one of `missing_skill_md`, `missing_frontmatter`,
`invalid_frontmatter`, `io_error` (a symlink, or a file that could not be
read), `name_collision_tool`, `name_collision_skill` and `name_collision` (a
second subagent of the same name); `detail` says exactly what is wrong.

The server reads the config strictly. An unknown field at any level is a
`ProtocolError` (code -32602) naming the fields it accepts, so a misspelt or
unsupported option never goes unnoticed. A config never carries a provider
key: the server reads keys from its environment.

## Top-level fields

Only `namespace`, `spec` and `provider` are required. Every switch is off when
absent, except `bash`.

| Field | Default | What it controls |
|---|---|---|
| `namespace` | required | The store key space of this agent's runs and sessions; not blank. Two agents with the same namespace on one Postgres database see the same sessions |
| `spec` | required | The model, instructions and loop limits ([below](#spec)) |
| `provider` | required | The model provider, by `id` ([Providers](./providers.md)) |
| `stores` | `{"type": "memory"}` | Where runs and sessions live ([Stores](./stores.md)) |
| `workspace` | none: no workspace tools | An existing directory, the root of `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash`, `BashOutput` and `BashKill` ([Tools](./tools.md#workspace-tools)). A relative path resolves against the server's working directory, which is this process's |
| `bash` | `True` | Whether the workspace tools include `Bash`, `BashOutput` and `BashKill`. Unused without a workspace |
| `execution` | `local`: `Bash` runs as the server's user | Where `Bash` runs: a `SandboxExecution` (Landlock, seccomp, rlimits) or a `DockerExecution` ([Sandbox](./sandbox.md)). Part of the run contract unless `local` |
| `ask_user` | `False` | Whether the model has `AskUser`, which suspends the run until you answer ([Tools](./tools.md#ask-the-user)) |
| `schedule_tools` | `False` | Whether the model has `CreateSchedule`, `ListSchedules` and `CancelSchedule`, which schedule runs of this agent; needs a tenant with `max_schedules` ([Scheduler](./scheduler.md#let-the-model-schedule)) |
| `permissions` | mode `default`, project root the workspace (or `/`) | Which calls run, ask or are denied ([below](#permissions), [Security](./security.md#permission-modes)) |
| `mcp_servers` | `[]` | MCP servers connected in order when the agent opens ([below](#mcp_servers)) |
| `skills` | `False` | The `Skill` tool, with the built-in `verify` skill and those in `config_dir/skills` ([Skills](./skills.md)) |
| `subagents` | `False` | The `Subagent` tool, with the built-in `verification` subagent and those in `config_dir/agents` ([Subagents](./subagents.md)) |
| `workers` | none | `{"background": bool, "retain": bool}`, both `False` when absent: background workers and kept workers. Needs `subagents` ([Subagents](./subagents.md)) |
| `mailbox` | `False` | A mailbox and the `send_message` tool between sessions of this agent: in process with memory stores, the `linch_mailbox` table with Postgres stores ([Subagents](./subagents.md)) |
| `tool_search` | none | `{"max_tools": int, "max_skills": int}`, 30 and 50 when absent. Past `max_tools` a new session is offered only the tools that always load and `tool_search`, which the model calls to load the rest by keyword or `select:a,b` |
| `config_dir` | `.linch` | Directory holding `skills/` and `agents/`. A relative path resolves against the workspace, or the server's working directory without one. Unused unless `skills` or `subagents` is on |
| `filesystem` | none | The virtual filesystem tools and large-result offload ([below](#filesystem)) |
| `tasks` | `False` | The session task tools `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, which run without approval in every mode ([Tasks](./tasks.md)) |
| `task_ledger` | none | One task list shared by every session, shown with each request: `{"list", "max_chars"?}`; needs `tasks` ([Tasks](./tasks.md#a-ledger-that-outlives-sessions)) |
| `deep_agent` | none | The deep agent preset ([below](#deep_agent)) |
| `profile` | none | A versioned domain role for the agent's runs ([below](#profile)) |
| `max_provider_concurrency` | none: no limit | The most provider calls its runs, and those of agents on the same provider key and endpoint, make at once; the rest wait their turn ([Providers](./providers.md#limit-concurrent-calls)). Not part of the run contract |

## `spec`

| Field | Default | What it controls |
|---|---|---|
| `model` | required | Model id sent with every request; not blank |
| `system_prompt` | `""` | The system prompt; empty sends no system block |
| `system_blocks` | `[]` | Your own system blocks, `{"text": str, "cacheable": bool}` (`cacheable` `False` when absent), sent in order before the system prompt ([below](#system-blocks)) |
| `system` | none | Named system prompt sections, text added after the prompt, and whether the presets' prompt text is left out, `{"sections"?, "append"?, "replace_defaults"?}`, Python's `SystemPromptConfig` ([below](#system-sections)) |
| `context_budget` | none: no cap | A cap on the context the `context_build` callbacks add to a request and what is dropped first, `{"max_tokens", "drop_order"?, "pinned"?}`, Python's `ContextBudget` ([Memory](./memory.md#context-budget)) |
| `max_turns` | none: no limit | Most provider calls one run may make. A run that needs one more ends with a `loop_guard` event (`reason: "max_turns"`) and a `result` of subtype `error`. The [loop guard](#loop_guard) ends a stuck run without it |
| `thinking` | none: the provider's default | `{"type": "disabled"}`, `{"type": "adaptive", "display"?}` or `{"type": "enabled", "budget_tokens"?, "display"?}`; `display` is `summarized` or `omitted`. How each provider maps it is in the [architecture providers page](../architecture/providers.md#the-adapters) |
| `effort` | none | Reasoning effort: `low`, `medium`, `high`, `xhigh` or `max`, for providers that support it |
| `max_output_tokens` | none: the provider's default | Most tokens one response may generate |
| `temperature` | none: the provider's default | Sampling temperature, 0 to 2 (`0` is allowed; `anthropic` takes at most 1). A value outside the range fails `agent.open`. Which providers ignore or refuse it is in [Providers](./providers.md#temperature) |
| `max_tool_concurrency` | `4` | Most read-scope tool calls that run at once; `0` counts as 1 |
| `call_timeout_ms` | none: wait | Longest one tool call may run, in milliseconds, at least 100 (`0` and smaller values fail `agent.open`: every call would time out at once); past it the call ends with a timeout error the model reads |
| `compaction` | none: never compact | When to summarize the older part of the conversation ([below](#compaction)) |
| `budget` | none: no cap | A token and USD cap for each run and its subagents, `{"max_tokens"?, "max_cost_usd"?, "warn_ratio"?}` ([below](#budget)) |
| `provider_retry` | on: 5 attempts, from 1 s doubling to 30 s, jitter 0.2 | Call the same model again after a retryable failure, `{"max_attempts"?, "base_delay_ms"?, "max_delay_ms"?, "jitter"?}`, Python's defaults; `{"max_attempts": 1}` turns it off ([providers](./providers.md#failures-and-retries)) |
| `fallback_models` | `[]` | Models to fail over to, in order, once a model has no retry left; the run keeps the one it fails over to ([providers](./providers.md#failures-and-retries)) |
| `output_schema` | none | A JSON Schema the final answer must satisfy, `{"name", "schema", "strict"?, "description"?}`; the parsed answer is `result.structured_output` ([Structured output](./structured-output.md)) |
| `tool_choice` | none: the provider's default, `auto` | Whether and which tool the model must call, `{"type": "auto" \| "none" \| "required"}` or `{"type": "tool", "name"}`; refused at open when it names a tool the agent does not offer or the provider refuses it ([Structured output](./structured-output.md#choose-which-tool-the-model-calls)) |
| `final_tool_name` | none | A registered tool whose call ends the run, its input the answer, without running ([Structured output](./structured-output.md#name-your-own-final-tool)) |
| `structured_output_retries` | `0` | How many answers that fail `output_schema` go back to the model before the run fails; also bounds a run's own schema, so it may be set without `output_schema` |
| `redaction` | none | Regex rules that scrub responses, tool results and optionally the prompt before anything logs, streams or sees them, `{"rules": [{"pattern", "replacement"?}], "response"?, "tool_results"?, "prompt"?}` ([Security](./security.md#redaction)) |
| `verification` | none | Checks the final answer must pass and the retry budget they share with `stop` callbacks, `{"max_retries"?, "verifiers"?}` ([Security](./security.md#verifiers)) |
| `truncation_recovery` | none: a cut answer is final | Continue a text answer the output limit cut off, `{"max_attempts"?, "feedback"?}` ([Memory](./memory.md#cut-off-answers)) |
| `loop_guard` | on: 3 identical calls with the same result, 10 identical inputs, 3 failed batches, 25 turns without a call | End a stuck run, `{"max_identical_tool_calls"?, "max_identical_inputs"?, "max_consecutive_failures"?, "max_idle_turns"?}`, Python's `LoopGuard` ([below](#loop_guard)) |
| `policy` | none: nothing is checked | Tools that may only run after others succeeded, and what a final answer must satisfy, `{"prerequisites"?, "gates"?, "max_corrections"?}` ([below](#policy)) |

### System blocks

Every request sends the system blocks in this order: `system_blocks` as you
wrote them, then `system_prompt` (never cacheable), then the skill listing
when `skills` is on (cacheable). A session keeps the blocks of its first run
for all its requests, and a subagent gets the same blocks followed by its
definition's instructions. No block may be blank, and the blocks are part of
the [run contract](#the-run-contract), flags included.

Mark a block `cacheable` when it is stable: a provider that takes cache
breakpoints (Anthropic) puts the system breakpoint on the last cacheable block,
so everything up to it may be served from the prompt cache. Blocks after it,
such as the system prompt when no skill listing follows, sit outside that
breakpoint.

```python
spec = {
    "model": "claude-sonnet-4-6",
    "system_prompt": "Answer billing questions.",
    "system_blocks": [
        {"text": "Company policy: ...", "cacheable": True},
        {"text": "Today's promotions: ..."},
    ],
}
```

### System sections

`spec.system` builds the system prompt from named layers, Python Linch's
`SystemPromptConfig`:

| Field | Default | What it controls |
|---|---|---|
| `sections` | `[]` | Named layers, each `{"name", "text", "order"?, "cacheable"?}`. `name` is for operators and is never sent; it is unique and not blank, as is `text`. `order` (default `0`) sorts the sections, lowest first, a tie keeping the order you wrote. `cacheable` defaults to `True` |
| `append` | none | Text sent as the last system block, after the prompt and the skill listing, never cacheable |
| `replace_defaults` | `False` | Leave out the prompt text the presets add to `system_prompt`: the deep agent's instructions, the filesystem tools' notes and a profile's `system_prompt`. Core has no default prompt of its own, so these are the defaults here |

A request sends: the sections by `order`, then `system_blocks`, then
`system_prompt` (with whatever the presets added), then the skill listing when
`skills` is on, then `append`. A subagent gets the same, followed by its
definition's instructions. The sections are part of the [run
contract](#the-run-contract): a run is not resumed by an agent with other
sections, and a spec without them keeps the contract it always had.

A provider that takes one cache breakpoint (Anthropic) puts it on the last
cacheable block, so everything before it is cached too. A section that is not
cacheable therefore may not come before a cacheable section or one of your
`system_blocks`; the agent refuses to open (`BuildError`) rather than cache
text you marked volatile. Give such a section a higher `order`.

Python Linch's `append` replaces `system_prompt`; here it is added, so the
text a preset put in the prompt is not lost. Python's `placement` (before the
defaults, after them, after the environment block) has no counterpart: there
is no environment block, and `order` places a section relative to the other
sections only, which all go before your `system_blocks` and prompt.

```python
spec = {
    "model": "claude-sonnet-4-6",
    "system_prompt": "Answer billing questions.",
    "system": {
        "sections": [
            {"name": "policy", "text": "Company policy: ...", "order": 1},
            {"name": "tone", "text": "Be brief.", "order": 2},
        ],
        "append": "Today's promotions: ...",
    },
}
```

A remote tenant may hold at most 16 sections of at most 16 KiB (names up to
64 bytes) and 64 KiB in all with `append`, since they ride every request
([Server](./server.md#what-admission-refuses-and-why)). The operator can add its own
sections to every agent of a tenant; they are sent with the tenant's, cannot be
removed or renamed, and `replace_defaults` leaves them in. Changing them
changes the contract of the tenant's agents, so their in-flight runs are
refused on resume until they finish.

### `compaction`

| Field | Default | What it controls |
|---|---|---|
| `threshold_tokens` | none: follow the context window | Compact before a turn whose conversation is estimated at this many tokens or more (about four characters per token, 1000 per image); at least 1 |
| `keep_recent_turns` | `10` | Assistant turns kept verbatim after the summary; at least 1 |
| `prompt` | the built-in prompt | Instructions for the summary call |
| `max_output_tokens` | `4096` | Most tokens the summary may use; at least 1 |
| `micro` | `False` | Clear old tool results before summarizing, with no model call ([Memory](./memory.md#micro-compaction)) |
| `max_forced` | `3` | Most summaries one run makes because the provider refused a prompt as longer than the context window; `0` never ([Memory](./memory.md#when-the-prompt-does-not-fit)) |
| `keep_user_tokens` | `15000` | Tokens of the user messages a summary folds that the checkpoint carries verbatim, newest first, copied by the harness; `0` carries none. The verbatim blocks also shrink to fit: the whole checkpoint is smaller than the part it replaces, or the summary is rejected |

Without `threshold_tokens` (`"compaction": {}` turns it on), the run compacts as
Python Linch does: when the estimate plus an output reserve reaches 80% of the
provider's [context window](./providers.md#context-window) for the spec's model.
The reserve is `spec.max_output_tokens` when set, else a fifth of the window,
at least 1 and at most 32768. A `threshold_tokens` replaces that rule and
ignores the window.

Compaction makes one extra provider call on the agent's provider and model,
and the run reports it with a `compaction` [event](./events.md). With
compaction on, a prompt the provider refuses as too long is shortened and
sent again; without it, the refusal ends the run. [Memory](./memory.md)
covers both, and external memory.

### `loop_guard`

The loop guard ends a run that is stuck, before its next provider call,
with `max_turns` unset as well. It is on unless you change it; set only the
fields to change.

| Field | Default | The run ends when |
|---|---|---|
| `max_identical_tool_calls` | `3` | The same call got the same result this many times among the latest 20 results: one tool, the same input, the same result, not an error. The call that reaches it runs first. A call whose result changed (a file read again after an edit) is progress and does not count, and so is one that came long ago in a long healthy run. A result moved to a file to the filesystem (large-result offload) counts as its content, whatever the file's name. `0` turns this check off |
| `max_identical_inputs` | `10` | One tool was called with the same input this many times among the latest 50 calls, whatever the results and whether they failed. Python's rule counts these over the whole run with the limit of `max_identical_tool_calls`; here the count is looser (10, in a window), and a write or exec call that succeeds with a new input (one not among the latest 50 such calls) clears the count of every other call (an edit changes what the next test run finds), so a long fix-and-test run is not cut. The same call, or a cycle of write or exec calls, run again and again with no new edit between still counts. It ends a loop whose result changes every time (a timestamp) or that alternates between a few calls. A tool that polls is not counted. `0` turns this check off; absent from the wire at `10` |
| `max_consecutive_failures` | `3` | This many tool batches in a row had every call fail. A batch with one success starts the count again. `0` turns this check off |
| `max_idle_turns` | `25` | One more turn in a row would end without a provider call, which only a `before_provider_call` callback answering `force_continue` does. At least `1`: this check is always on |

A run that trips it streams a `loop_guard` event (`reason`
`repeated_tool_call`, `repeated_failures` or `idle_turns`), an `error` named
`LoopGuardError` and a `result` of subtype `error`, and ends `failed`. The
counts are read back from the run's log on a resume, so a crash does not
reset them.

```python
spec = {"model": "deepseek-flash", "loop_guard": {"max_identical_tool_calls": 5}}
```

Python Linch counts identical inputs whatever the result, failed calls
included. Here a repeat must also return the same result, a failed call is
the failure count's (Linch tells the model to repeat some refused calls, a
deferred tool). A tool that polls is waited on, not counted as a repeat:
`BashOutput` can read a running background command as often as the model
needs, as long as the answer changes; the same answer `max_idle_turns`
times in a row ends the run (a command that prints nothing and never
ends), and reading a command that has finished again is a repeat at once
(`BashOutput` marks it done in the result's metadata). A [host
tool](./tools.md#host-tools) declares `polls=True`, and an [MCP
server](#mcp_servers) lists its polling tools in `polls`; their results
say a poll is done with the metadata `{"poll_done": true}`. Python's
`force_final_answer` is not available. A remote tenant may not raise
`max_idle_turns` above 25.

The default is not written into the spec, so leaving `loop_guard` out keeps
the [run contract](#the-run-contract) of an agent from before the guard,
and its runs still resume; any other value changes the contract.

What the guard does not catch. It counts one input at most 10 times in the
latest 50 results, so a cycle of more than 5 distinct calls whose results
change every time (each one a different timestamp, say) never reaches 10 in
50 and is not caught: only `max_turns` and the [budget](#budget) end such a
run, and both default to none. An [MCP](#mcp_servers) `polls` tool whose
answer changes on every call (an elapsed time in a status line) is kept out of
every count, so it is bounded by `max_turns` and the budget alone. Set
`max_turns` (and a budget) on a run that must end.

### `policy`

`spec.policy` states what a run must do in order: which tools may only run
after others succeeded (prerequisites), and what a run must have done
before it may end (completion gates). Unset, nothing is checked.

```python
spec = {
    "model": "deepseek-flash",
    "policy": {
        "prerequisites": [{"tool": "Edit", "requires": ["Read"]}],
        "gates": [
            {"name": "tested", "succeeded": ["Test"]},
            {"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}},
        ],
        "max_corrections": 2,
    },
}
```

**Prerequisites.** A call of `tool` is answered with an error result that
names what to run first, and does not run, until every tool in `requires`
has succeeded in an earlier tool batch of the run. Calls of one batch never
unlock each other. A call counts as a success when its result is not an
error: an interrupted call, a denied one and a `run_in_background` start
(which only acknowledges) do not. The refusal comes before the call's
`pre_tool_use` callbacks and before permission, so a refused call asks
nobody for approval. The run records a `policy` event (`rule`
`prerequisite`, `action` `refuse`) once per refused call (per response and
`tool_use_id`, so a provider that reuses an id on a later turn gets a new
event). The message to the model names the tools to run first only when the
run offers them this turn; with one hidden by a skill's `allowed_tools`, or
deferred by tool search, it says only that a prerequisite has not succeeded
(the event's `missing` still lists them).

A prerequisite is an ordering aid for the model, **not a security
boundary**. It guards the calls of one run; a tool that can start other
work (`Bash`, a subagent) can do what a refused tool would. A call counts
by its name and its rendered result alone: a client's host tool of that
name, a `pre_tool_use` callback that answers the call, a `post_tool_use`
callback that replaces its result and a cache hit all count as a success. Keep
[permissions and the sandbox](./security.md) for security. Python Linch's
`ToolPrerequisite` is the same rule; here a subagent's own calls take the
parent's prerequisites over its own run.

A refused batch fails like any other: the [loop guard](#loop_guard) counts a
batch in which every call was refused as a failed batch, so a model that
ignores the message three times in a row ends the run.

**Gates.** A final answer (text, or the input of the final tool) is checked
before the output schema, the verifiers and the `stop` callbacks. A gate
holds when each tool in `succeeded` has succeeded `min_calls` times (1 by
default) in the run, and the answer passes `check`, one of the
[verifier checks](./security.md#verifiers). When a gate fails, the run goes
on: the answer goes back to the model as a system reminder naming what is
missing, one correction turn for the failure, answering every gate that
failed at once. A check gate's default sentence only says the answer
does not satisfy the requirement: it does not show the pattern or the
keywords, which are the operator's to keep. Set `feedback` to tell the
model what to fix; it is shown as written. A correction is
a `policy` event (`rule` `completion`, `action` `correct`, `attempt` the
corrections spent). Once `max_corrections` (2 when absent, in all, across
the gates) are spent, the next failure ends the run `failed`: a `policy`
event with `action` `stop`, an `error` named `PolicyGateError`, and a
`result` of subtype `error` carrying the last answer. A gate never lets the
answer through, unlike a verifier, which accepts it once its retries are
spent. `max_corrections: 0` ends the run at the first failure.

The input of the final tool is judged as its JSON text and as each string
it holds (keys and values, at any depth), which the JSON text escapes: a
`forbids` check fails when it matches any of them, a `matches` or
`keywords` check when it passes on none. When a `stop` callback rewrites
the answer, the gates judge the rewrite again; a failure there ends the
run at once with a `policy` stop event and no correction, as a hook that
rewrote once would rewrite the next answer too.

A `tool_choice` that holds every turn cannot go with rules it keeps from
being met: `none` or a named tool with a prerequisite or a gate that needs
`succeeded` tools, `required` or a named tool with any rule unless the run
has a final tool to end it with (`required` plus the final tool is fine;
the final tool itself only with gates that check the answer). `agent.open`
refuses the agent, and a run that brings its own `tool_choice` fails with
`InvalidToolChoice` before anything is written.

A correction turn has a provider call and runs no tool, so the loop guard
counts it as neither an idle nor a repeated turn; `max_turns` and the
budget bound it like any turn. Gates are not checked on runs that end
another way (the turn limit, the budget, a failure).

`agent.open` refuses a policy that cannot run (`OpenError`, code -32011): a
tool the agent does not have, the final tool (its call never runs, so it
could never succeed), a prerequisite cycle, two entries for one tool, a
gate with neither `succeeded` nor `check`, a repeated or blank gate name, or
a `check` that does not compile. `policy: {}` is allowed and does nothing,
but it is set, so it changes the contract.

Nothing is checkpointed. The successes, the refusals made and the
corrections spent are read back from the run's log on a resume, so a crash
at any write cannot skip a gate, spend a correction twice or report a
refusal twice. A subagent's runs take the prerequisites, not the gates (it
answers its parent in text, with tools of its own). Policy is part of the
[run contract](#the-run-contract): leaving it out keeps the contract of an
agent from before policies, and any policy changes it. A remote tenant
adds to the policy its operator requires; its rules stand, but they bind
the model's call order, not the tenant's host
([Server](./server.md#what-admission-refuses-and-why)).

### `budget`

| Field | Default | What it controls |
|---|---|---|
| `max_tokens` | none: no token cap | Most tokens the run and its subagents may spend, all four usage buckets summed |
| `max_cost_usd` | none: no cost cap | Most USD they may spend, priced from the [built-in table](./events.md#usage-and-cost); the model needs a price (the built-in table or the agent's `pricing`), or `agent.open` fails with an invalid-agent error (`-32011`) that names the model; cap `max_tokens` instead for a model without one |
| `warn_ratio` | `0.9` | Fraction of either cap at which one `budget` event of kind `warning` fires |

As in Python Linch, every provider call is charged once it ends, and the run
checks the budget before its next call (a compaction summary call included).
A run whose spend reached a cap stops there with a `budget` event of kind
`exceeded`, an `error` named `BudgetExceededError` and a `result` of subtype
`error`, `stop_reason` `error`. A subagent's child run charges its parent's
budget, so one budget caps the whole tree, and the warning fires once for it,
in whichever run's call crossed the ratio. With a cap set, a call whose usage
is unknown (it got no answer, or was cut off after output began, before the
provider reported any) means the cap can no longer be kept: the next call,
a [retry](./providers.md#failures-and-retries) included, is refused with an
error named `BudgetAccountingError`. A call that failed before costing
anything (no connection, or a 4xx, 503 or 529 status before any output)
counts as zero and is retried as usual; a 500, 502 or 504 may have been
billed upstream, so it counts as unknown. A run that crashed during a call resumes into
that error. A cost cap needs every call priced: a call that has usage and no
price (the provider served a model the pricing has no rate for, a
[fallback model](./providers.md#failures-and-retries) included) refuses the next
call with a `BudgetAccountingError` that says so. A token cap alone does not care.
The price is looked up under the id the provider reports, else `spec.model`, so
key `pricing` by the ids it reports.

Each run of the agent starts with nothing spent. A resumed run starts from
what its durable record and its children's say was spent, so a crash never
resets it. The budget is part of the [run contract](#the-run-contract).

```python
spec = {"model": "claude-sonnet-4-6", "budget": {"max_tokens": 200_000, "max_cost_usd": 2.0}}
```

## `permissions`

| Field | Default | What it controls |
|---|---|---|
| `mode` | `default` | The decision when no rule matches: `default` (write and exec calls ask), `accept_edits` (writes inside the project root run; exec asks), `skip_dangerous` (every call runs) |
| `project_root` | required when `permissions` is given | An absolute path (a relative one is refused at open, unlike `workspace`) that relative paths and path patterns resolve against |
| `layers` | `[]` | Named lists of rules, from least to most specific |
| `auto_allow` | `[]` | Tool names allowed in every mode (rules still apply first) |
| `path_fields` | built in | Which input field holds the path, per tool, added to the built-in table |
| `command_fields` | built in | Which input field holds a shell command, per tool, added to `Bash` -> `command` |

Read-scope calls run in every mode. Rules and layers are on the
[Tools](./tools.md#permission-rules) page.

## `mcp_servers`

Each entry connects one MCP server when the agent opens. Its tools are named
`mcp__<name>__<tool>`; the tools it has at that moment enter the run contract.
Durations are in milliseconds.

| Field | Default | What it controls |
|---|---|---|
| `name` | required | The `<name>` in `mcp__<name>__<tool>`: ASCII letters, digits, `-`, and single `_` between them |
| `transport` | required | `{"type": "stdio", "command", "args": [], "env": {}}` or `{"type": "http", "url", "headers": {}}` |
| `trust_annotations` | `False` | Whether a tool the server marks read-only gets the `read` scope instead of `exec` |
| `polls` | `[]` | The server's tools, by the names it lists them under, that poll something that runs on: the loop guard waits on their repeated answers, up to `max_idle_turns` identical ones in a row, instead of ending the run at the third. An MCP result has no metadata to say the wait is over |
| `call_timeout_ms` | `300000` | Longest wait for one tool call before it is cancelled |
| `reconnect` | see below | How a lost connection is retried |

`reconnect` fields: `attempts` (`5`; `0` never reconnects), `initial_delay_ms`
(`500`, doubling), `max_delay_ms` (`30000`) and `attempt_timeout_ms` (`30000`,
the first connect included). A stdio server's process sees only `HOME`,
`LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` from the server's environment,
plus its own `env`. Connection, relisting and limits are in the
[architecture MCP page](../architecture/mcp.md). An example is on the
[Tools](./tools.md#mcp-tools) page.

## `filesystem`

`{}` turns on the virtual filesystem: the `ls`, `read_file`, `write_file` and
`edit_file` tools over text files that are not the workspace, and a short text
after the system prompt that teaches the model to use them.

| Field | Default | What it controls |
|---|---|---|
| `offload_threshold_tokens` | none | Move a text tool result estimated above this many tokens (four bytes per token) into the filesystem, leaving its first lines and its path for the model to `read_file` |
| `memory` | `False` | Show the model the instruction to keep memories under `/memories/` and the first 200 lines (at most 16 KiB) of its `MEMORY.md`, fenced as untrusted data and read for each request ([Memory](./memory.md#file-memory)). A deep agent always has it |
| `memory_scope` | `"principal"` | Whose `/memories/` the model sees: `"principal"` gives each principal a directory of its own (`/memories/<key>/MEMORY.md`), `"namespace"` one file for every run of the namespace ([Memory](./memory.md#file-memory)) |
| `offload_threshold_fraction` | none | Offload above this fraction of the provider's [context window](./providers.md#context-window), at least 1000 tokens; Python Linch uses 0.1. Used only without `offload_threshold_tokens`. With neither, results are never offloaded |

With memory stores each session has its own files, gone with the server. With
Postgres stores every session of the namespace shares them. See
[Tools](./tools.md#virtual-filesystem).

## `deep_agent`

The deep agent preset plans with tasks, delegates to a roster of subagents and
keeps notes in the virtual filesystem. Behaviour and profiles are on the
[Deep agent](./deep-agent.md) page.

| Field | Default | What it controls |
|---|---|---|
| `profile` | `balanced` | A built-in profile by name (`balanced`, `coordinator`, `unbounded`) or a full profile dict (`name`, `durable`, `coordinator`, `max_turns`, `max_tokens`, `retain_subagents`, `enable_background_subagents`, all required) |
| `durable` | the profile's (`True` for the built-in ones) | Whether the agent needs Postgres stores; a durable deep agent on memory stores does not open |
| `coordinator` | the profile's | Whether the agent runs as a coordinator |

## `profile`

A domain profile is a versioned role. See [Profiles](./profiles.md).

| Field | Default | What it controls |
|---|---|---|
| `id` | required | Stable profile id; not blank |
| `version` | required | Revision of the profile; not blank |
| `system_prompt` | none | Role instructions, after the agent's system prompt |
| `tool_deny` | none | Tools whose calls are always denied, whatever the mode; the model is still offered them |
| `max_turns` | none | Replaces `spec.max_turns`; at least 1 |

## The run contract

`agent.contract` is a digest of what a run depends on. A run stores it when it
starts, and `resume` and `approve` refuse an agent whose contract differs
(`ContractMismatch`, code -32025). It covers:

- the `spec`, with what `profile`, `deep_agent` and `filesystem` add to it;
  its `system` sections and `context_budget` count when set, and are left off
  the wire (so the digest does not move) when not;
- every tool registered at open, in order (host tools, then workspace, task,
  filesystem and MCP tools), each with its name, description, schema and
  scope;
- each callback's `point`, `policy_id` and `policy_version`, in declaration
  order;
- the skills and subagent definitions loaded at open;
- the `permissions` policy, when it is not the default one: a `mode` other
  than `default`, or any `layers`, `auto_allow`, `path_fields` or
  `command_fields`, and then its `project_root` too. A run therefore never
  continues under a policy that allows more (or less) than the one it
  started with. The default policy adds nothing, whatever its
  `project_root`, so it leaves the contract as it was before the policy
  counted.

It does not cover the namespace, the stores, the provider config, a host
tool's `timeout`, the scripted turns, or a profile's id, version and denied
tools. Turning offload on or off changes it; changing the threshold does not.
To change an agent while runs are in flight, let them finish first, or keep the
old config for resuming them.

## Callbacks

A callback is a function the server asks at one hook point of a run.
Declare it with `@callback(point, policy_id, policy_version, matcher=None,
timeout=None, fail_mode=None)` and pass it to `open_agent(callbacks=...)`.

```python
import asyncio
from typing import Any

from linch_rust import AgentConfig, Server, ToolCallEndEvent, callback, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def lookup(email: str) -> str:
    """Find a customer by email."""
    return "Ada Lovelace"


@callback("pre_tool_use", "no-external-emails", 1, matcher=["lookup"])
async def guard(request: dict[str, Any]) -> dict[str, Any]:
    if not request["input"]["email"].endswith("@example.com"):
        return {"action": "block", "reason": "only example.com customers"}
    return {"action": "continue"}


config: AgentConfig = {
    "namespace": "support",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "lookup", {"email": "eve@other.org"}), text_turn("Sorry.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup], callbacks=[guard])
        run = await agent.run("s1", "Who is eve@other.org?")
        async for envelope in run:
            if isinstance(envelope.event, ToolCallEndEvent):
                print(envelope.event.is_error, envelope.event.result["content"][0]["text"])


asyncio.run(main())
```

```text
True only example.com customers
```

The function takes the request dict and returns the point's answer. Every
request has `session_id` and `run_id`:

| `point` | The request also has | Answers |
|---|---|---|
| `pre_tool_use` | `tool_use_id`, `tool_name`, `input` | `{"action": "continue"}`, `{"action": "rewrite", "input"}`, `{"action": "block", "reason"}`, `{"action": "resolve", "result"}` |
| `permission_request` | `tool_use_id`, `tool_name`, `input`, `reason?`, `principal?` | `{"decision": "allow"}`, `{"decision": "deny", "reason"}` |
| `post_tool_use` | `tool_use_id`, `tool_name`, `input`, `result` | `{"action": "continue"}`, `{"action": "replace", "result"}`, `{"action": "mutate", "content"?, "is_error"?}`; a `replace` is a whole new result, a `mutate` changes only the output value (`content`) or the error flag (`is_error`) and keeps the result's metadata and error code; leave `content` out to keep it, since a `null` is refused as a bad answer (it would read as absent). The next request and the session log carry the mutated result |
| `tool_execute` | `tool_use_id`, `tool_name`, `input`, `attempt`, `phase` (`before` or `after`), `result` (`after` only) | `before`: `{"action": "next", "after"?}` runs what is inside (the later callbacks, then the tool; with `after` the server calls again with the result), `{"action": "veto", "result"}` skips it and uses `result`. `after`: `{"action": "continue"}`, `{"action": "replace", "result"}`. This is Python's `tools/execute` waterfall; the `tool_middleware` decorator hides the two calls behind `async def layer(call, next)` (`await next()` runs the tool and returns its result; returning without it vetoes the call). The first callback declared is the outermost, a layer sees every attempt of a retried call, and the input is the one permission already allowed: rewrite inputs in `pre_tool_use`. A callback that is not declared costs nothing |
| `user_prompt_submit` | `content` (content blocks) | `{"action": "continue"}`, `{"action": "rewrite", "content"}`, `{"action": "block", "reason"}` |
| `context_build` | `messages` (the conversation the model will see) | `{"blocks"?: [{"text"}], "metadata"?: {...}}`; `metadata` is for observers and evals: it lands on the call's `ContextBuildEvent`, a later callback's key winning, and the model never sees it |
| `before_provider_call` | `turn`, `request` (the full provider request about to be sent) | `{"action": "continue"}`, `{"action": "rewrite", "request"}`, `{"action": "force_continue", "feedback"}`, `{"action": "block", "reason"}`; a rewrite is what is sent and what `context_build` records, and a block ends the run before the provider is called. `force_continue` skips the call: `feedback` goes to the model in a `<system-reminder>` and the run takes another turn, bounded by `max_turns` and by the [loop guard](#loop_guard)'s `max_idle_turns` (25 turns in a row by default). `retry` is refused here (Python ignores it) |
| `after_provider_call` | `turn`, `assembly` (`message`, `stop_reason`, `usage?`, `model`: what the provider call just assembled) | `{"action": "continue"}`, `{"action": "rewrite", "assembly"}`, `{"action": "retry", "feedback"}`, `{"action": "block", "reason"}`; a rewrite is what gets staged and appended to the session, and a block ends the run before the response is staged. A retry (`"force_continue"` is the same, as in Python) keeps the response, runs none of its tool calls, and sends `feedback` to the model before another turn: in a `<system-reminder>`, or, when the response called tools, as the error result of the first call (the others read "Tool not executed; revise and resubmit your final answer."). Only `max_turns` bounds it |
| `stop` | `text` (the final-answer text so far, the model's or a prior rewrite) | `{"action": "allow"}`, `{"action": "continue", "feedback"}`, `{"action": "retry", "feedback"}`, `{"action": "rewrite", "text"}`, `{"action": "block", "reason"}`; a rewrite replaces the run's final-answer text (not the assistant message, already appended to the session by the time `stop` runs), and a block ends the run failed. This is also where Python's `before_final_answer` lives: there is no separate point. `continue` (or `"force_continue"`, Python's name for it at `stop`) sends `feedback` in a `<system-reminder>` and takes another turn, bounded only by `max_turns`. `retry` is Python's `before_final_answer` retry: honoured once per run call (with `spec.verification`, up to its `max_retries` per run, each a `verification` event; see [Security](./security.md#a-verifier-in-your-process)), after which it counts as `allow` and the callbacks after it decide. Declare a retrying callback before the others, as Python runs `before_final_answer` before `stop`. A Python `before_final_answer` hook that answers `force_continue` must answer `retry` here: `force_continue` at `stop` is the unbounded `continue` |
| `post_turn` | `turn`, `stop_reason` | `{}` |

A tool result in a request or an answer is `{"output": {"value",
"metadata"?}}` or `{"error": {"message", "code"?, "details"?}}`.

Thirteen more points only observe: the callback cannot change anything they
report, so the answer is always `{}` and none of them can fail closed.

| `point` | The request also has |
|---|---|
| `agent_start` | `model`, `prompt`, `tools` |
| `agent_stop` | `status`, `stop_reason` |
| `turn_start` | `turn` |
| `provider_call_start` | `turn`, `model` |
| `provider_call_stop` | `turn`, `model`, `disposition`, `usage?` (absent when unknown; zero for a failed call that cost nothing), `duration_ms` |
| `tool_use_start` | `tool_use_id`, `tool_name`, `input` |
| `tool_use_stop` | `tool_use_id`, `tool_name`, `is_error`, `duration_ms` |
| `post_tool_use_failure` | `tool_use_id`, `tool_name`, `input`, `error` |
| `pre_compact` | `turn`, `messages`, `tokens`, `strategy` |
| `post_compact` | `turn`, `messages_before`, `messages_after`, `tokens_before`, `tokens_after`, `strategy` |
| `subagent_start` | `child_session_id`, `subagent_run_id`, `subagent_type`, `display_name`, `prompt` |
| `subagent_stop` | `child_session_id`, `subagent_run_id`, `subagent_type`, `display_name`, `subtype`, `text`, `error?` |
| `event_emit` | `event` (the event about to be delivered to the caller) |

`tool_use_start`/`tool_use_stop` bracket a call that actually runs (not one
served or blocked by `pre_tool_use`); `post_tool_use_failure` fires beside
`post_tool_use` when the result is an error. `subagent_start`/`subagent_stop`
fire on the parent's callbacks, not the child's. `event_emit` fires once per
event, in delivery order, but only for an event that is both streamed and
durably logged: never for a hook's own record (it would otherwise recurse)
and never for a stream-only delta or tool-progress event.

| Argument | Default | What it controls |
|---|---|---|
| `point` | required | One of the twenty-three points above |
| `policy_id`, `policy_version` | required | Enter the run contract. Bump the version whenever the behaviour, matcher, timeout or fail mode changes |
| `matcher` | every tool | Tool-name globs (`*`, `?`), only at the tool points (`pre_tool_use`, `permission_request`, `post_tool_use`, `tool_execute`); a miss never reaches this process |
| `timeout` | none | How long the server waits for an answer; absent, it waits until the callback answers, the run is cancelled or the connection ends |
| `fail_mode` | `closed` at `pre_tool_use` and `permission_request`, `open` elsewhere | What an exception, a bad answer or a timeout does: `closed` gives the point's blocking answer, `open` skips the callback. `context_build` and `post_turn` cannot fail closed |

Callbacks at one point run in declaration order. A `pre_tool_use` rewrite
feeds the next callback and is checked against the tool's schema again. At
`permission_request` a deny stops the chain and an allow does not, so a later
deny wins; with no answer the call asks the host as usual
([approvals](./tools.md#approvals)). A blocked `user_prompt_submit` ends the
run with `PromptBlocked` before anything is written. Each call shows up as a
stream-only `hook` [event](./events.md). Python's `retry` and
`force_continue` are answers only at `after_provider_call` and `stop`, and
`force_continue` also at `before_provider_call`; every other point refuses
them as a bad answer, handled by the fail mode.

The feedback of a `retry`, `continue` or `force_continue` is logged in the
session as the next user message, and arrives as a `user` event with subtype
`notification`. Python marks the same message `prompt`; here `prompt` stays
the run's own prompt, which the OpenTelemetry observer relies on.

```python
@callback("stop", "cite-sources", 1)
async def cite(request: dict[str, Any]) -> dict[str, Any]:
    if "http" not in request["text"]:
        return {"action": "retry", "feedback": "Cite a source for each claim."}
    return {"action": "allow"}
```

## Sessions

A session is any string id. The first run on an id creates it; later runs on
the same id continue the conversation with its full history. A session has at
most one run that is not finished ([Runs](./runs.md#one-live-run-per-session)).
A run left waiting for an approval or an answer holds the session until it
is approved, answered, or ended with `await agent.cancel(run_id)`
([Runs](./runs.md#cancel-a-suspended-run)), which answers its pending calls
as aborted so the next run of the session can start.

`await agent.close_session(session_id)` tells the agent you are done with a
session for now: the workspace tools forget what it read (so a later write
must read the file again), and with memory stores its virtual files are
dropped. Nothing durable is deleted; the session and its runs stay.

## Related pages

- [Quickstart](./quickstart.md): the same shapes, step by step.
- [Providers](./providers.md): every `provider` config.
- [Tools](./tools.md): host tools, workspace tools, permissions, approvals.
- [Runs](./runs.md): start, stream, resume, approve, cancel.
