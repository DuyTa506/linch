"""`AgentConfig`: the config `Server.open_agent` takes, as typed dicts.

A config is still the JSON object the server decodes, so a plain dict works
at run time; these types let a type checker catch a misspelt field or a
wrong value before the server refuses it. The server is strict: an unknown
field at any level is a `ProtocolError` at open. Nested values this client
does not type (`spec`, `provider`, `stores`, permission rules, `mcp_servers`)
are documented in the `linch` crate's `AgentConfig`.
"""

# No `from __future__ import annotations`: `TypedDict` sees `Required` only in
# evaluated annotations, and `__required_keys__` must list the three.
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Required, TypedDict


class ModelRates(TypedDict, total=False):
    """USD per million tokens for one model, in `AgentConfig.pricing`.

    Every rate must be finite and not negative.

    Attributes:
        input: Uncached input tokens.
        output: Generated output tokens.
        cache_read: Input tokens served from the prompt cache; `input` when
            absent.
        cache_creation: Input tokens written to the prompt cache; `input`
            when absent.
    """

    input: Required[float]
    output: Required[float]
    cache_read: float
    cache_creation: float


class Workers(TypedDict, total=False):
    """How subagents keep their workers. It needs `subagents`.

    Attributes:
        background: Whether a `Subagent` call may run its child in the
            background (`run_in_background`); adds the `SubagentContinue`
            and `TaskStop` tools. Off when absent.
        retain: Whether a foreground worker stays addressable once it
            answered, for `SubagentContinue`, which it adds. Its answer
            ends with its `[Worker ID: ...]`. Off when absent.
        max_background: How many background workers one run may have
            running at once; a `Subagent` call past it gets a tool error.
            4 when absent.
    """

    background: bool
    retain: bool
    max_background: int


class Filesystem(TypedDict, total=False):
    """The virtual filesystem: `ls`, `read_file`, `write_file` and `edit_file`.

    `{}` turns it on. With memory stores each session has its own files;
    with Postgres stores every session of the namespace shares them.

    Attributes:
        offload_threshold_tokens: Move a text tool result estimated above
            this many tokens (four bytes per token) into the filesystem,
            leaving its first lines and its path. Absent, with no
            `offload_threshold_fraction` either: never offload.
        offload_threshold_fraction: Offload above this fraction of the
            provider's context window for the spec's model, at least 1000
            tokens (Python Linch's `threshold_fraction`, 0.1 there). Used
            only when `offload_threshold_tokens` is absent.
    """

    offload_threshold_tokens: int
    offload_threshold_fraction: float


class LocalExecution(TypedDict):
    """The default execution backend: `Bash` runs as the server's user.

    Attributes:
        backend: `"local"`.
    """

    backend: Literal["local"]


class SandboxExecution(TypedDict, total=False):
    """`Bash` under Landlock, a seccomp filter and rlimits.

    Needs Linux 6.12 or later (Landlock ABI 6) and `prlimit`; the agent
    does not open otherwise. A command reads only the workspace, its
    session's private `HOME` (also `TMPDIR`) and system directories, writes
    only the workspace and that home, opens no Unix socket, and cannot
    signal the server. Every field but `backend` has a default.

    Attributes:
        backend: `"sandbox"`.
        network: Whether commands may open network sockets; off when absent.
        memory_bytes: Address space of each process; 2 GiB when absent.
        max_processes: Processes a command may add to those the server's
            user runs; 512 when absent.
        cpu_seconds: CPU time of each process; 1800 when absent.
        file_size_bytes: Largest file a process may write; 1 GiB when absent.
        read_paths: Absolute paths a command may also read and execute.
    """

    backend: Required[Literal["sandbox"]]
    network: bool
    memory_bytes: int
    max_processes: int
    cpu_seconds: int
    file_size_bytes: int
    read_paths: Sequence[str]


class DockerExecution(TypedDict, total=False):
    """`Bash` in a fresh `docker run --rm` container per command.

    The workspace is mounted at its own path. Every field but `backend` has
    a default: Python Linch's limits, with the network off.

    Attributes:
        backend: `"docker"`.
        image: The image; `python:3.12-slim` when absent.
        docker_path: The `docker` client; found on the server's `PATH`
            when absent.
        network: `--network`; `none` when absent. `none`, `bridge` or a
            network's name; `host` and `container:...` are refused.
        memory_bytes: `--memory`; 2 GiB when absent.
        cpu_millis: `--cpus` in thousandths of a CPU; 2000 when absent.
        pids_limit: `--pids-limit`; 512 when absent.
        read_only_root: Whether the container's root is read-only.
        tmpfs: `--tmpfs` entries.
        user: `--user`; the server's uid and gid when absent.
        allow_root: Whether a command may run as root in the container;
            a root `user` (or a root server's default) is refused without.
    """

    backend: Required[Literal["docker"]]
    image: str
    docker_path: str
    network: str
    memory_bytes: int
    cpu_millis: int
    pids_limit: int
    read_only_root: bool
    tmpfs: Sequence[str]
    user: str
    allow_root: bool


Execution = LocalExecution | SandboxExecution | DockerExecution
"""Where `Bash` runs its commands; see `AgentConfig.execution`."""


class ToolSearch(TypedDict, total=False):
    """Past which sizes part of the catalog waits for `tool_search`.

    `{}` turns it on with the defaults. Past `max_tools` a new session is
    offered only the tools that always load and `tool_search`; the model
    finds the rest by keyword or `select:a,b`, and they load for its next
    request.

    Attributes:
        max_tools: The most tools, `tool_search` aside, offered inline; 30
            when absent.
        max_skills: The most skills listed inline; past it the listing only
            names them. 50 when absent.
    """

    max_tools: int
    max_skills: int


class ProviderRetry(TypedDict, total=False):
    """How a provider call that failed with a retryable error is retried, as `spec.provider_retry`.

    Retryable is no connection or no response at all (refused, reset,
    timeout), a 408, a 429 or a 5xx. Retries are on by default, with these
    defaults, which are Python Linch's `RetryOptions`; set only the fields
    to change, and `{"max_attempts": 1}` to turn retries off. Each retry
    waits, then calls the same model again; once a model has had
    `max_attempts` calls in a turn, the run fails over to the next of
    `spec.fallback_models` (keeping it for the rest of the run) or fails
    with the last error.

    Attributes:
        max_attempts: Most calls to one model in one turn, the first
            included; 5 when absent, 1 never retries.
        base_delay_ms: Wait before the first retry; each later one waits
            twice as long. 1000 when absent.
        max_delay_ms: Longest wait; 30000 when absent. A server's
            `Retry-After` is waited as given, and one longer than this ends
            the retries on that model rather than hold the run.
        jitter: Fraction of each doubled wait added or taken away, between
            0 and 1; 0.2 when absent, 0 waits exactly.
    """

    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    jitter: float


class Compaction(TypedDict, total=False):
    """When a run folds the older part of its view into a summary, as `spec.compaction`.

    Absent, a run never compacts, and a prompt the provider refuses as
    longer than the model's context window ends the run. `{}` compacts near
    the provider's context window with every default.

    At a turn's start, a view that is due is first cleared of old tool
    results when `micro` is on, then summarized if still due. When the
    provider refuses a prompt as too long, the run clears old tool results
    (with `micro`), else summarizes, forced, and calls again; that call is
    not a provider retry. The model sees the shorter view; the session keeps
    every message.

    Attributes:
        threshold_tokens: Compact a view estimated at this many tokens or
            more (four characters per token). Absent: once the estimate plus
            an output reserve reaches 80% of the context window.
        keep_recent_turns: Assistant turns kept verbatim; 10 when absent.
        prompt: Instructions for the summary call; a general default when
            absent.
        max_output_tokens: Most tokens a summary may use; 4096 when absent.
        micro: Whether old tool results are cleared to a short placeholder
            before a summary, with no model call (Python Linch's
            micro-compaction). Off when absent.
        max_forced: Most summaries one run makes because the provider
            refused a prompt as too long; 3 when absent, 0 never.
    """

    threshold_tokens: int
    keep_recent_turns: int
    prompt: str
    max_output_tokens: int
    micro: bool
    max_forced: int


DETAILED_COMPACTION_PROMPT = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
    "\n"
    "Create a continuation-safe summary of the earlier conversation. Capture enough detail for "
    "another agent to continue the work without access to the compacted messages.\n"
    "\n"
    "Before the final summary, you may use an <analysis> block to organize details. The final "
    "answer must include a <summary> block with these exact numbered sections:\n"
    "\n"
    "1. Primary Request and Intent: the user's explicit requests and intent.\n"
    "2. Key Information and Artifacts: facts, decisions, data, documents, files, or other "
    "artifacts read, produced, or discussed; include identifiers (paths, URLs, IDs, names) when "
    "known.\n"
    "3. Errors and Fixes: errors, failures, wrong assumptions, and how they were resolved.\n"
    "4. Pending Tasks: outstanding tasks explicitly requested or discovered.\n"
    "5. Current Work: what was being worked on immediately before compaction.\n"
    "6. Next Step: the next concrete action aligned with the latest user request; write 'None' "
    "if the prior task was complete.\n"
    "\n"
    "Be factual and dense. Preserve exact identifiers, values, parameters, and results, along "
    "with any user corrections. Do not invent work that was not done."
)
"""Python Linch's `DetailedCompaction` prompt, for `Compaction.prompt`.

A continuation-safe handoff in six numbered sections, for long runs where
the work must go on from the summary alone; give it a larger
`max_output_tokens` (Python Linch uses 8192)."""


class TruncationRecovery(TypedDict, total=False):
    """How a text answer the output limit cut off is continued, as `spec.truncation_recovery`.

    Python Linch's `TruncationRecovery`. Absent, a cut answer is final.
    With it, the cut part is logged and the model is asked to continue; the
    run's final answer is every part in order, and that whole text is what
    the output schema, the verifiers and `stop` callbacks see.

    Attributes:
        max_attempts: Most cut answers one run continues; 1 when absent.
        feedback: What the model is told; "Your previous response was cut
            off ..." when absent.
    """

    max_attempts: int
    feedback: str


class LoopGuard(TypedDict, total=False):
    """When a stuck run ends, as `spec.loop_guard`.

    Python Linch's `LoopGuard`, on by default with these defaults; set only
    the fields to change. A run that trips it ends failed with a
    `LoopGuardEvent` (its `reason` is `repeated_tool_call`,
    `repeated_failures` or `idle_turns`), an `ErrorEvent` named
    `LoopGuardError`, and a `ResultEvent` of subtype `error`, before its
    next provider call. Python counts identical inputs whatever the
    result; here a repeat must also return the same result, an error result
    is the failure streak's, a built-in tool that polls (`BashOutput`) never
    repeats, and there is no `force_final_answer`.

    Attributes:
        max_identical_tool_calls: Calls of one tool with the same input
            and the same result, not an error, that end the run; the one
            that reaches it runs first. A call whose result changed is
            progress. 3 when absent, 0 never.
        max_consecutive_failures: Tool batches in a row whose every call
            failed that end the run. 3 when absent, 0 never.
        max_idle_turns: Most turns in a row that may end without a provider
            call (a `before_provider_call` `force_continue`); the next one
            ends the run. 25 when absent, at least 1: always on. A remote
            tenant may not raise it above 25.
    """

    max_identical_tool_calls: int
    max_consecutive_failures: int
    max_idle_turns: int


class DeepAgentProfile(TypedDict):
    """A deep agent profile written out in full; every key is required.

    Attributes:
        name: The profile's name.
        durable: Whether the agent must keep its runs and sessions in
            Postgres stores.
        coordinator: Whether the agent runs as a coordinator.
        max_turns: Most provider calls one run may make, unless the spec
            sets its own; `None` sets no limit.
        max_tokens: A token budget across the subagent tree; kept for
            Python Linch's profiles, not enforced.
        retain_subagents: Whether a foreground worker stays addressable.
        enable_background_subagents: Whether a worker may run in the
            background.
    """

    name: str
    durable: bool
    coordinator: bool
    max_turns: int | None
    max_tokens: int | None
    retain_subagents: bool
    enable_background_subagents: bool


class DeepAgent(TypedDict, total=False):
    """The deep agent preset: it plans, delegates, and keeps notes.

    Opening it turns on `tasks`, the `Skill` tool with the built-in skills,
    the `Subagent` tool with the `researcher`, `planner`, `implementer` and
    `verification` roster, the profile's `workers` and a `filesystem` with
    file memory, unless the config sets them itself; its instructions go
    before `spec.system_prompt`. It adds no workspace.

    Attributes:
        profile: A built-in profile by name, `balanced` (the default),
            `coordinator` or `unbounded`, or a `DeepAgentProfile`. The
            built-in ones are durable.
        durable: Overrides the profile's `durable`; a durable deep agent
            needs Postgres stores, or the open fails.
        coordinator: Overrides the profile's `coordinator`.
    """

    profile: str | DeepAgentProfile
    durable: bool
    coordinator: bool


class Profile(TypedDict, total=False):
    """A domain profile: a versioned role the agent's runs take.

    Attributes:
        id: Stable domain or tenant profile id; not blank.
        version: Revision of the profile; not blank.
        system_prompt: Role instructions, after the agent's system prompt.
        tool_deny: Tools whose calls are always denied, whatever the
            permission mode says; the model is still offered them.
        max_turns: Most provider calls one run may make; replaces the
            spec's. At least 1.
    """

    id: Required[str]
    version: Required[str]
    system_prompt: str
    tool_deny: Sequence[str]
    max_turns: int


class Principal(TypedDict, total=False):
    """Who a run acts for, given to `Agent.run`, `resume` and `approve`.

    Permission rules with `role` or `unless_role` match its roles, and a
    `permission_request` callback's request carries it. Without one, a
    rule with `role` never matches and one with `unless_role` always does.

    Attributes:
        tenant: The tenant; not blank. On a remote server it must be the
            connection's own tenant.
        user: The user; not blank.
        roles: The roles the user holds; none when absent.
    """

    tenant: Required[str]
    user: Required[str]
    roles: Sequence[str]


class OutputSchema(TypedDict, total=False):
    """A JSON Schema the final answer of a run must satisfy.

    Set it as the agent's `spec.output_schema` (with an optional
    `spec.structured_output_retries`, 0 by default), or per run with
    `Agent.run(output_schema=...)`. The answer must be a JSON object
    (a fenced ```json block is read too) that satisfies `schema`; the
    parsed object is `ResultEvent.structured_output`. A failing answer goes
    back to the model with the error while retries last, then the run fails
    with `structured_error` set.

    Attributes:
        name: Identifier sent to the provider; required.
        schema: The JSON Schema (draft 2020-12) of an object; required.
        strict: Whether a provider that can enforce the schema should
            (OpenAI); true when absent.
        description: What the answer is, for the provider.
    """

    name: Required[str]
    schema: Required[Mapping[str, Any]]
    strict: bool
    description: str


class RedactionRule(TypedDict, total=False):
    """Replace each match of `pattern` with `replacement`.

    Attributes:
        pattern: A regular expression in the `regex` crate's syntax: no
            look-around or back-references; `(?i)` for any case. One that
            matches empty text is refused. Required.
        replacement: What replaces a match; `$1` or `${name}` name a
            group, `$$` is a literal `$`. `[REDACTED]` when absent.
    """

    pattern: Required[str]
    replacement: str


class Redaction(TypedDict, total=False):
    """Rules that scrub text before it is logged, streamed or shown to a hook, as `spec.redaction`.

    Python Linch's `RedactionHook`: Linch ships no patterns. What is
    scrubbed is what the session logs and the model reads back; the raw
    text never reaches a store or an event. While a surface is scrubbed its
    stream-only events are not sent: `partial_assistant` with `response`,
    `tool_progress` with `tool_results`. A tool call's input and signed
    reasoning are never changed.

    Attributes:
        rules: Applied in order; at least one. Required.
        response: Whether assistant responses (text and unsigned
            reasoning), the final answer after a `stop` rewrite and
            compaction summaries are scrubbed. On when absent.
        tool_results: Whether tool results (every string of the value,
            metadata, error), `AskUser` answers, background workers'
            answers and mailbox peer messages are scrubbed. On when absent.
        prompt: Whether a new run's prompt is scrubbed. Off when absent.
    """

    rules: Required[Sequence[RedactionRule]]
    response: bool
    tool_results: bool
    prompt: bool


class VerifierCheck(TypedDict, total=False):
    """What a verifier checks the final answer text for.

    Attributes:
        type: `matches` (the pattern must match), `forbids` (it must not)
            or `keywords` (each keyword must appear). Required.
        pattern: The regular expression of `matches` and `forbids`, in the
            `regex` crate's syntax.
        keywords: The words of `keywords`, as substrings.
        any: Whether one keyword is enough. Off when absent.
        case_sensitive: Whether keyword case matters. Off when absent.
    """

    type: Required[str]
    pattern: str
    keywords: Sequence[str]
    any: bool
    case_sensitive: bool


class Verifier(TypedDict, total=False):
    """One built-in verifier of the final answer.

    Attributes:
        name: Names it in `verification` events; unique, not
            `output_schema`. Required.
        check: See `VerifierCheck`. Required.
        feedback: What the model is told on a retry; a sentence naming the
            check when absent.
        on_fail: `retry` (the default: feed back while retries last, then
            accept the answer with an `exhausted` event) or `stop` (fail
            the run with a `VerificationError`).
    """

    name: Required[str]
    check: Required[VerifierCheck]
    feedback: str
    on_fail: str


class Verification(TypedDict, total=False):
    """Checks the final answer must pass, as `spec.verification`.

    Python Linch's `FinalAnswerVerifierHook`. The built-in verifiers run
    after the output schema, in order, the first failure deciding. A
    `stop` callback that answers `retry` is the client form of a verifier:
    with this set, its retries draw on `max_retries` too and each one is a
    `verification` event under its `policy_id`. The output schema keeps its
    own `structured_output_retries`.

    Attributes:
        max_retries: How many times a failed answer goes back to the model
            per run, verifiers and `stop` callbacks together. 2 when
            absent.
        verifiers: See `Verifier`; none when only callbacks verify.
    """

    max_retries: int
    verifiers: Sequence[Verifier]


class Permissions(TypedDict, total=False):
    """Which tool calls run, ask or are denied, as `AgentConfig.permissions`.

    Attributes:
        mode: `default` (read tools run, write and exec tools ask),
            `accept_edits` (write tools run too, unless they write outside
            `project_root`; exec tools ask) or `skip_dangerous` (every call
            runs). Python Linch's `default`, `acceptEdits` and
            `skip-dangerous`. `default` when absent.
        project_root: An absolute path that path rules resolve against.
            Required.
        layers: Named layers of rules, from least to most specific; see
            the Tools page of the usage docs.
        auto_allow: Tool names that run in every mode unless a rule says
            otherwise; a remote tenant needs `skip_dangerous` among its
            modes for any.
        path_fields: Where a tool keeps its target path, by tool name.
        command_fields: Where a tool keeps its shell command, by tool name.
    """

    mode: str
    project_root: Required[str]
    layers: Sequence[Mapping[str, Any]]
    auto_allow: Sequence[str]
    path_fields: Mapping[str, Mapping[str, Any]]
    command_fields: Mapping[str, str]


class AgentConfig(TypedDict, total=False):
    """An agent as data: what `Server.open_agent` opens.

    Only `namespace`, `spec` and `provider` are required. Every switch is
    off when absent, except `bash`. A config never carries a provider key:
    the server reads it from its environment.

    Attributes:
        namespace: Store key space for this agent's runs and sessions.
        spec: The model, instructions and loop limits, such as
            `{"model": "deepseek-flash", "system_prompt": "Be brief."}`;
            `system_blocks`, a token or USD `budget`, a `provider_retry`
            policy (see `ProviderRetry`), `fallback_models`, the models to
            fail over to in order, an `output_schema` (see
            `OutputSchema`) with its `structured_output_retries`, a
            `redaction` (see `Redaction`), a `verification` (see
            `Verification`) and a `loop_guard` (see `LoopGuard`) go here
            too.
        provider: The model provider chosen by `id` (`deepseek`,
            `openai-chat`, `openai-responses`, `anthropic`, `gemini`,
            `vllm`, `sglang`, `llamacpp`, or `scripted` with its `turns`),
            with an optional `base_url`.
        stores: `{"type": "memory"}` (the default: runs live in the agent
            handle) or `{"type": "postgres", "dsn": ...}`, with an optional
            `max_connections`: the pool size, by default the server's number
            of cores between 10 and 32. A remote tenant may ask for at most
            its `max_pool_connections`. An optional `lease_ttl_ms` (at
            least 1000, 30000 by default) sets how long a run claim holds
            without a renewal: another server's resume of a run this agent
            drives gets `RunLeased` until the run stops or its lease runs
            out.
        workspace: An existing directory, the root of the built-in tools
            `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Bash`, `BashOutput` and
            `BashKill`; a relative path resolves against the server's working directory. Absent: no
            workspace tools.
        bash: Whether the workspace tools include `Bash`, which runs
            commands through `execution`, and `BashOutput` and `BashKill`
            for its background commands. On when absent.
        execution: Where `Bash` runs its commands: `{"backend": "local"}`,
            as the server's user, when absent; a `SandboxExecution` or a
            `DockerExecution`. A non-local backend is part of the run
            contract. A remote tenant may name only a backend its server
            lists, and one naming none gets the server's first.
        permissions: The mode, `project_root` and rules; see
            `Permissions`. `project_root` is required and must be an
            absolute path: path rules resolve against it. Mode `default`
            (read tools run, write and exec tools ask) when absent.
        mcp_servers: MCP servers connected in order when the agent opens.
        skills: Whether the agent has skills: the built-in `verify` and
            those in `config_dir/skills`, called through the `Skill` tool.
        subagents: Whether the agent has foreground subagents: the built-in
            `verification` and those in `config_dir/agents`, run through
            the `Subagent` tool on the same provider.
        workers: How subagents keep their workers; see `Workers`.
        mailbox: Whether the agent has a mailbox and the `send_message`
            tool. A message addressed to a session id is added to that
            session's next user message (the next run's prompt when it is
            idle). It is in process with memory stores, lost if the server
            stops first; with Postgres stores it waits in a table.
        tool_search: Whether a large catalog waits for the model to search
            it, and past which sizes; see `ToolSearch`.
        config_dir: Directory holding `skills/` and `agents/`; `.linch`
            when absent. A relative path resolves against the workspace,
            or the server's working directory without one.
        filesystem: The virtual filesystem; see `Filesystem`.
        tasks: Whether the agent has the session task tools (`TaskCreate`,
            `TaskList`, `TaskGet`, `TaskUpdate`), kept in the session store.
            They run without approval in every permission mode.
        ask_user: Whether the agent has the `AskUser` tool: a call suspends
            the run with an `input_request` until `Agent.answer`. Off when
            absent.
        schedule_tools: Whether the model has `CreateSchedule`,
            `ListSchedules` and `CancelSchedule`: it schedules runs of this
            same agent in its own session, for the run's principal, under
            the tenant's `max_schedules` and `min_schedule_interval_ms`.
            Needs Postgres stores, no host tools or callbacks, and a tenant
            whose allowlists give it schedules (a worker runs them); a
            local connection is refused. Off when absent.
        deep_agent: The deep agent preset; see `DeepAgent`.
        profile: The domain profile its runs take; see `Profile`.
        pricing: Rates by exact model id (see `ModelRates`) that this agent
            prices a model with, in place of the published table or in
            addition to it. A model neither lists costs `None`, as does any
            model a local server (`vllm`, `sglang`, `llamacpp`) serves.
    """

    namespace: Required[str]
    spec: Required[Mapping[str, Any]]
    provider: Required[Mapping[str, Any]]
    stores: Mapping[str, Any]
    workspace: str
    bash: bool
    execution: Execution
    permissions: Permissions
    mcp_servers: Sequence[Mapping[str, Any]]
    skills: bool
    subagents: bool
    workers: Workers
    mailbox: bool
    tool_search: ToolSearch
    config_dir: str
    filesystem: Filesystem
    tasks: bool
    ask_user: bool
    schedule_tools: bool
    deep_agent: DeepAgent
    profile: Profile
    pricing: Mapping[str, ModelRates]
