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


class TaskLedger(TypedDict, total=False):
    """A task list every session of the agent shares, shown with each request.

    Needs `tasks`. The task tools then read and write one list instead of
    one per session, so a session that starts with an empty conversation
    finds what an earlier one left, and each request carries the open tasks
    as extra context (never logged). The list is kept like any task: in the
    Postgres tables, or in the server's memory.

    Attributes:
        list: The list's id; stored as an empty session of the agent's
            namespace, so pick an id no run uses as its session id.
        max_chars: Most characters of ledger text a request carries; 4000
            when absent, at least 200. The open tasks past it are named as
            a count. The block's policy id is `linch.tasks.ledger-context`:
            pin it in `spec.context_budget` so a budget never drops it.
    """

    list: Required[str]
    max_chars: int


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
        memory: Show the model its file memory: an instruction to keep
            memories under `/memories/`, and the first 200 lines of
            `/memories/MEMORY.md` when the session's files hold it (Python
            Linch's `FileMemoryContextBuilder`). Both are read again for
            each request and ride its tail, never the session. A
            `deep_agent` always has this. Off when absent. With memory
            stores the file is dropped when its session closes; with
            Postgres stores every session of the namespace shares the
            files, and `memory_scope` says whose `MEMORY.md` it is. The
            index is cut to 200 lines and 16 KiB and shown as untrusted
            data.
        memory_scope: Whose `/memories/` the model sees: `"principal"`
            (the default when absent) gives each run's principal a
            directory of its own, `/memories/<key>/MEMORY.md`, so a run with
            fewer rights cannot plant notes for others (a run without a
            principal shares `/memories/MEMORY.md`); `"namespace"` is that
            one file for every run of the namespace. It separates by path,
            not by permission. Used only with `memory` or a `deep_agent`.
    """

    offload_threshold_tokens: int
    offload_threshold_fraction: float
    memory: bool
    memory_scope: Literal["namespace", "principal"]


class Recall(TypedDict, total=False):
    """Automatic recall, as `LongTermMemory.recall`.

    Attributes:
        k: Most memories one request carries, 1 to 20; 5 when absent.
    """

    k: int


class LongTermMemory(TypedDict, total=False):
    """Long-term memory kept by the server, as `AgentConfig.memory`.

    `{}` gives the model `MemorySave`, `MemorySearch` and `MemoryDelete`
    over a store the server picks from `stores`: Postgres rows found by
    the words they hold (any language, any word of the query matches, more
    matches rank first) with Postgres stores, in-process memory otherwise.
    No callback is involved, so scheduled and background agents have it
    too. The model never names the namespace or the scope. `MemorySave`
    and `MemoryDelete` are write tools: a background run, which cannot
    answer an approval, needs a permission mode that allows them, such as
    `accept_edits`. A remote tenant needs `memory` and `max_memories` in
    its server allowlists.

    What a memory says is untrusted data: the model reads it escaped and,
    in recall, inside a delimiter that says so.

    Attributes:
        scope: `principal` (the default): one memory per tenant and user of
            the run's principal; a run with none has no memory, never the
            shared one. `agent`: every session of the namespace shares one
            memory, so what one user saves reaches every other user; say
            it only for an agent with a single user, such as a local one.
        recall: Search memory with the user's latest message before each
            request and show the model the best matches, at the tail of
            the request. They are never written to the session, so the
            cached prefix does not move. Words too common to select (a few
            English function words, short English words) are not searched
            for, and with two or more searched words a memory needs two of
            them. Off when absent; on, it is part of the run contract.
        max_memories: The most memories the tenant may keep in all its
            namespaces; a save past it fails for the model with
            `limit_reached`. The tenant's `max_memories` allowlist bounds
            it. No cap when absent (a remote tenant always has one).
        max_memories_per_scope: The most one scope (one principal, or the
            shared memory) may keep, so a single user cannot fill
            `max_memories`. A quarter of `max_memories`, at least 1, when
            absent. Needs `max_memories`, and may not pass it.
    """

    scope: str
    recall: Recall
    max_memories: int
    max_memories_per_scope: int


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
        keep_user_tokens: Tokens of the user messages the summary folded
            that it carries verbatim, newest first; 15000 when absent, 0
            carries none.
    """

    threshold_tokens: int
    keep_recent_turns: int
    prompt: str
    max_output_tokens: int
    micro: bool
    max_forced: int
    keep_user_tokens: int


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
    result; here a repeat must also return the same result, within the
    latest 20 results, an error result is the failure streak's, and there is
    no `force_final_answer`. `max_identical_inputs` is Python's rule loosened
    (10 among the latest 50 calls, not 3 in the whole run). A tool that polls
    (`BashOutput`; a host tool or MCP tool declared `polls`) is waited on: it
    ends the run only after
    `max_idle_turns` identical answers in a row, or at once when a finished
    command is read again.

    Attributes:
        max_identical_tool_calls: Calls of one tool with the same input
            and the same result, not an error, that end the run; the one
            that reaches it runs first. A call whose result changed is
            progress. 3 when absent, 0 never.
        max_identical_inputs: Calls of one tool with the same input among
            the latest 50, whatever their results, that end the run (a
            tool that polls is not counted). 10 when absent, 0 never.
        max_consecutive_failures: Tool batches in a row whose every call
            failed that end the run. 3 when absent, 0 never.
        max_idle_turns: Most turns in a row that may end without a provider
            call (a `before_provider_call` `force_continue`); the next one
            ends the run. 25 when absent, at least 1: always on. A remote
            tenant may not raise it above 25.
    """

    max_identical_tool_calls: int
    max_identical_inputs: int
    max_consecutive_failures: int
    max_idle_turns: int


class SystemSection(TypedDict, total=False):
    """One named layer of the system prompt, sent as one system block.

    Attributes:
        name: A name for operators, never sent to the model; unique among
            the sections, not blank. Required.
        text: The text of the block; not blank. Required.
        order: Where it goes among the sections, lowest first; a tie keeps
            the order declared. 0 when absent.
        cacheable: Whether the prompt prefix ending at this block may be
            cached. True when absent. A provider with one cache breakpoint
            (Anthropic) puts it on the last cacheable block, so everything
            before it is cached too: a section that is not cacheable may not
            come before a cacheable block, or the agent does not open.
    """

    name: Required[str]
    text: Required[str]
    order: int
    cacheable: bool


class System(TypedDict, total=False):
    """Named system prompt sections, as `spec.system`.

    Python Linch's `SystemPromptConfig`. The request's system is the
    sections by `order`, then `spec.system_blocks`, then `spec.system_prompt`
    (with the text the presets add to it), then the skill listing when the
    agent has skills, then `append`. The sections are part of the run
    contract: a run is not resumed by an agent with other ones. A remote
    tenant may hold at most 16 sections of 16 KiB each and 64 KiB with
    `append`, and the server's own sections are added to every agent of the
    tenant and cannot be removed or renamed.

    Attributes:
        sections: See `SystemSection`; none when absent.
        append: Text sent as the last system block, not cacheable. It is
            added to the prompt, not a replacement for it (Python's
            `append` replaces `system_prompt`).
        replace_defaults: Whether the presets' own prompt text (the deep
            agent's instructions, the filesystem tools' notes, a profile's
            `system_prompt`) is left out, so the prompt and sections you
            wrote stand alone. Off when absent.
    """

    sections: Sequence[SystemSection]
    append: str
    replace_defaults: bool


class ContextBudget(TypedDict, total=False):
    """A cap on the context the `context_build` callbacks add, as `spec.context_budget`.

    Python Linch's `ContextBudget`. It counts only what `context_build`
    callbacks and hooks add to a request (retrieved memory, file memory,
    anything else they add) at four characters per token, rounded up per
    block; the system prompt, the tools and the logged history are not
    counted (compaction owns the history). When the blocks do not fit, whole
    blocks are dropped, and the run never fails for it. A callback is named
    by its `policy_id` (`retrieval()` sets `memory` unless told otherwise;
    the file memory of a deep agent is
    `linch.deep-agent.file-memory-context`). Dropped first: the blocks of
    callbacks listed in neither list, the later callback's first; then
    those in `drop_order`, the first listed first. Inside one callback the
    last block goes first, so list the best chunk first. A callback in
    `pinned` is never dropped; if the pinned blocks alone are over the cap
    the request goes over and the event's `budget` reports `over_by`.

    The `context_build` event's `budget` reports each request's decision
    (`max_tokens`, `used_tokens`, `remaining_tokens`, `trimmed`, and
    `dropped`: the `source`, `index` among that callback's blocks and
    `tokens` of each); it is recomputed for every request and not stored
    for recovery. The budget is part of the run contract.

    Attributes:
        max_tokens: Most estimated tokens the callbacks may add to one
            request; at least 1. Required.
        drop_order: Policy ids, the one dropped first at the front.
        pinned: Policy ids never dropped. A id is in one list at most.
    """

    max_tokens: Required[int]
    drop_order: Sequence[str]
    pinned: Sequence[str]


class ToolRetry(TypedDict, total=False):
    """When a failed tool call runs again, as `spec.tool_retry`.

    Off when absent. A call is retried only when its tool declares itself
    idempotent (`tool(idempotent=True)` for a host tool; the built-in read
    tools do), the attempt failed with an error whose `code` is in
    `retry_on` (the class the tool reports; `timeout` stands for a call
    that ran past the call timeout), and attempts are left. A tool that is
    not idempotent is never run twice, whatever `tools` lists. Each retry
    is durable before it runs: a `ToolRetryEvent` is written first, and a
    crash leaves the call interrupted, never repeated. A call's retries
    stop together once five minutes have passed since its first attempt,
    and the event's `error` is redacted (with `spec.redaction`) and cut to
    512 bytes. A timed-out host tool call is cancelled with a notification
    the client does not acknowledge, so the next attempt may start while
    the handler is still winding down: an idempotent host tool must
    tolerate two overlapping calls. A remote tenant may ask for at most 5
    attempts and a 60 s wait.

    Attributes:
        max_attempts: Most attempts of one call, the first included; at
            least 2; 3 when absent.
        base_delay_ms: Wait before the second attempt, doubled each retry;
            200 when absent.
        max_delay_ms: Longest wait, at least `base_delay_ms`; 5000 when
            absent.
        retry_on: Error classes retried; `["transient"]` when absent.
        tools: Names that may be retried, narrowing the idempotent tools;
            every idempotent tool when absent.
    """

    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    retry_on: list[str]
    tools: list[str]


class ToolCache(TypedDict, total=False):
    """Which read tools' results are reused, as `spec.tool_cache`.

    Off when absent. A call of a tool named in `tools` with the same
    canonical input, in the same agent namespace and session, is served
    from the cache while its entry lives: permission and the
    `pre_tool_use` and `post_tool_use` hooks still run, and the call's
    `ToolCallEndEvent` has `cache_hit` set. `tool_execute` layers do not
    run on a hit, so a layer that must see every result belongs in
    `post_tool_use`. Only the tool's own successful result is stored,
    after the output schema check and redaction and before
    `post_tool_use`: without `spec.redaction` over tool results it is
    stored as the tool returned it. A result a `tool_execute` layer
    vetoed or replaced is never stored. A write or exec tool's call clears
    the namespace, and a read that began before the clear never stores its
    result. The first call that starts background work (`Bash` with
    `run_in_background`, a background subagent) ends caching for the
    agent. A tool that keeps session state, such as `Read` (which records
    what the session saw so `Edit` may follow), cannot be named. The key
    has no tool version: after a tool changes, old entries live until
    their ttl. With Postgres stores the cache is a table shared by the
    namespace's servers; otherwise it lives in the agent. A remote tenant
    may use it only when its server allows, with redaction over tool
    results, and within a byte cap for all its namespaces together.

    Attributes:
        tools: Names of the cacheable tools; required, never empty.
        ttl_ms: How long an entry lives; 300000 when absent.
        max_entries: Most entries of the namespace; the oldest go first;
            256 when absent.
        max_value_bytes: Results whose JSON is larger are not stored;
            100000 when absent.
        invalidate_on_write: Whether a write or exec tool's call clears the
            namespace's entries, before it starts and after it ends; true
            when absent.
        scope: `"session"` (the default): an entry serves only the session
            that stored it, since a read tool may answer per session.
            `"namespace"`: every session of the namespace shares entries,
            your promise that the tool answers the same for the same input
            whoever asks; a remote tenant needs its server's
            `tool_cache_shared`.
    """

    tools: list[str]
    ttl_ms: int
    max_entries: int
    max_value_bytes: int
    invalidate_on_write: bool
    scope: Literal["session", "namespace"]


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

    A provider without native structured output (`deepseek`, `vllm`,
    `sglang`, `llamacpp`) is not sent the schema: the run offers a final
    tool named `name` whose input schema is `schema`, and its call ends the
    run with the checked input as the answer.

    Attributes:
        name: Identifier sent to the provider, and the final tool's name
            where there is one (letters, digits, `_` and `-`); required.
        schema: The JSON Schema (draft 2020-12) of an object; required.
        strict: Whether a provider that can enforce the schema should
            (OpenAI); true when absent.
        description: What the answer is, for the provider.
    """

    name: Required[str]
    schema: Required[Mapping[str, Any]]
    strict: bool
    description: str


class ToolChoice(TypedDict, total=False):
    """Whether and which tool the model must call.

    Set it as the agent's `spec.tool_choice`, or per run with
    `Agent.run(tool_choice=...)`; it goes with every request of the run
    but on a turn that offers no tool. `{"type": "auto"}` lets the model
    choose, `{"type": "none"}` forbids tools, `{"type": "required"}` forces
    some tool and `{"type": "tool", "name": "Read"}` forces that one. A
    forced choice holds every turn, so such a run ends through its final
    tool or a limit. The agent refuses a named tool it does not offer and a
    choice its provider refuses: DeepSeek refuses a forced one unless
    `spec.thinking` is `{"type": "disabled"}`, llama.cpp a named one.

    Attributes:
        type: `auto`, `none`, `required` or `tool`; required.
        name: The tool to call, with `type` `tool` only.
    """

    type: Required[str]
    name: str


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


class PolicyPrerequisite(TypedDict):
    """A tool that may only run after others succeeded.

    Attributes:
        tool: The tool that is held back.
        requires: Tools that must each have succeeded in an earlier tool
            batch of the run; at least one.
    """

    tool: str
    requires: Sequence[str]


class PolicyGate(TypedDict, total=False):
    """One condition a final answer must meet before the run may end.

    Set `succeeded`, `check`, or both.

    Attributes:
        name: Names the gate in `PolicyEvent`s; unique. Required.
        succeeded: Tools that must have succeeded `min_calls` times in the
            run.
        min_calls: How many successful calls each of `succeeded` needs.
            1 when absent.
        check: A `VerifierCheck` the answer text must pass (for a final
            tool, its input as JSON text and as each string it holds).
        feedback: What the model is told when the gate fails; a sentence
            naming the missing calls, or saying only that the check is not
            met (never its pattern or keywords) when absent.
    """

    name: Required[str]
    succeeded: Sequence[str]
    min_calls: int
    check: VerifierCheck
    feedback: str


class Policy(TypedDict, total=False):
    """Tool order and completion requirements, as `spec.policy`.

    Python Linch's `ReliabilityPolicy` prerequisites and completion gate.
    Unset, nothing is checked. Set, even empty, it is part of the run
    contract.

    A **prerequisite** answers a call with an error result naming what to
    run first, without running the tool, its `pre_tool_use` hooks or the
    permission system (a `PolicyEvent` with `action` `refuse`). Calls of
    one batch never unlock each other, and only a successful foreground
    call counts (not an error, an interrupted call or a `run_in_background`
    start). It is an ordering aid for the model, not a security boundary:
    a call counts by its tool name and result alone, so a host tool of that
    name, a hook that answers the call or a cache hit counts too. Keep
    permissions and the sandbox for security. A subagent's own calls take
    the prerequisites too.

    A **gate** holds a final answer (text, or the final tool's input) back
    until the run did what it must: the answer goes back to the model as
    one correction turn (`action` `correct`), up to `max_corrections` in
    the run, then the run ends failed with a `PolicyGateError` (`action`
    `stop`). Unlike a verifier, a gate never accepts the answer once its
    corrections are spent. A `tool_choice` that holds every turn and keeps
    the rules from being met (`none`, a named tool, or `required` without a
    final tool) is refused with the policy. A correction is neither an idle nor a repeated
    turn for the loop guard. A subagent's runs do not take the gates. The
    tools named must be the agent's own, and not its final tool, or the
    open is refused.

    Attributes:
        prerequisites: See `PolicyPrerequisite`; at most one entry per
            tool, no cycles.
        gates: See `PolicyGate`; all are checked together and one
            correction answers every gate that fails.
        max_corrections: Correction turns the gates may spend in one run, in
            all. 2 when absent; 0 ends the run at the first failure. A
            remote tenant may ask for at most 16, and its policy is added to
            the one its operator requires (`required_policy`), never
            replacing it; a tenant that leaves it out keeps the operator's.
            The operator's policy orders the model's calls, not the
            tenant's host.
    """

    prerequisites: Sequence[PolicyPrerequisite]
    gates: Sequence[PolicyGate]
    max_corrections: int


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
            `temperature` (0 to 2, and at most 1 on `anthropic`; 0 is
            allowed, unset leaves the provider's default; the open fails
            on a value outside the range), `system_blocks`, a token or
            USD `budget`, a `provider_retry` policy (see `ProviderRetry`),
            `fallback_models`, the models to fail over to in order, an `output_schema` (see
            `OutputSchema`) with its `structured_output_retries`, a
            `tool_choice` (see `ToolChoice`), a `final_tool_name` (a
            registered tool whose call ends the run, its input the answer), a
            `redaction` (see `Redaction`), a `verification` (see
            `Verification`), a `loop_guard` (see `LoopGuard`), `system`
            sections (see `System`), a `context_budget` (see
            `ContextBudget`), a `tool_retry` (see `ToolRetry`), a
            `tool_cache` (see `ToolCache`) and a `policy`, the tool order and
            completion gates (see `Policy`), go here too.
        provider: The model provider chosen by `id` (`deepseek`,
            `openai-chat`, `openai-responses`, `anthropic`, `gemini`,
            `vllm`, `sglang`, `llamacpp`, or `scripted` with its `turns`),
            with an optional `base_url`. Every live provider also takes
            `request`: `{"extra_body": {...}, "default_headers": {...},
            "timeout_ms": 60000}`: extra fields merged into the request
            body (not `model`, `stream`, `temperature` or the conversation),
            headers sent with every request (a key header replaces the provider key), and the
            longest silence in milliseconds before a call fails. The
            `deepseek`, `openai-chat`, `vllm`, `sglang` and `llamacpp`
            providers also take `chat`: `{"parallel_tool_calls": false,
            "include_stream_options": false, "chat_template_kwargs": {...},
            "sampling_params": {"top_k": 20}}`. Header values are hidden
            when a config is printed.
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
        task_ledger: A task list shared by every session of the agent, shown
            with each request; see `TaskLedger`. Needs `tasks`.
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
        memory: Long-term memory for the model, in a store that outlives
            sessions; see `LongTermMemory`. Off when absent.
        deep_agent: The deep agent preset; see `DeepAgent`.
        profile: The domain profile its runs take; see `Profile`.
        pricing: Rates by exact model id (see `ModelRates`) that this agent
            prices a model with, in place of the published table or in
            addition to it. A model neither lists costs `None`, as does any
            model a local server (`vllm`, `sglang`, `llamacpp`) serves.
        max_provider_concurrency: The most provider calls its runs, and the
            runs of every agent opened on the same server with the same
            provider key and endpoint, make at once; the rest wait their
            turn, first come first served. A run that waits has dispatched
            nothing, so cancelling it leaves no delivery or receipt.
            Unlimited when absent; at least 1. A remote tenant may ask for
            at most its `max_provider_concurrency` and gets that when it
            asks for none. The limit is the server process's: a worker
            process or a second server counts its own calls. It is not
            part of the run contract.
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
    task_ledger: TaskLedger
    ask_user: bool
    schedule_tools: bool
    memory: LongTermMemory
    deep_agent: DeepAgent
    profile: Profile
    pricing: Mapping[str, ModelRates]
    max_provider_concurrency: int
