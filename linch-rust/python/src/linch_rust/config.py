"""`AgentConfig`: the config `Server.open_agent` takes, as typed dicts.

A config is still the JSON object the server decodes, so a plain dict works
at run time; these types let a type checker catch a misspelt field or a
wrong value before the server refuses it. The server is strict: an unknown
field at any level is a `ProtocolError` at open. Nested values this client
does not type (`spec`, `provider`, `stores`, `permissions`, `mcp_servers`)
are documented in the `linch` crate's `AgentConfig`.
"""

# No `from __future__ import annotations`: `TypedDict` sees `Required` only in
# evaluated annotations, and `__required_keys__` must list the three.
from collections.abc import Mapping, Sequence
from typing import Any, Required, TypedDict


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


class AgentConfig(TypedDict, total=False):
    """An agent as data: what `Server.open_agent` opens.

    Only `namespace`, `spec` and `provider` are required. Every switch is
    off when absent, except `bash`. A config never carries a provider key:
    the server reads it from its environment.

    Attributes:
        namespace: Store key space for this agent's runs and sessions.
        spec: The model, instructions and loop limits, such as
            `{"model": "deepseek-flash", "system_prompt": "Be brief."}`;
            `system_blocks` and a token or USD `budget` go here too.
        provider: The model provider chosen by `id` (`deepseek`,
            `openai-chat`, `openai-responses`, `anthropic`, `gemini`,
            `vllm`, `sglang`, `llamacpp`, or `scripted` with its `turns`),
            with an optional `base_url`.
        stores: `{"type": "memory"}` (the default: runs live in the agent
            handle) or `{"type": "postgres", "dsn": ...}`.
        workspace: An existing directory, the root of the built-in tools
            `Read`, `Write`, `Edit`, `Grep`, `Glob` and `Bash`; a relative
            path resolves against the server's working directory. Absent: no
            workspace tools.
        bash: Whether the workspace tools include `Bash`, which runs
            commands as the server's user. On when absent.
        permissions: `{"mode": ..., "project_root": ...}` and rules.
            `project_root` is required and must be an absolute path: path
            rules resolve against it. Mode `default` (read tools run, write
            and exec tools ask) when absent.
        mcp_servers: MCP servers connected in order when the agent opens.
        skills: Whether the agent has skills: the built-in `verify` and
            those in `config_dir/skills`, called through the `Skill` tool.
        subagents: Whether the agent has foreground subagents: the built-in
            `verification` and those in `config_dir/agents`, run through
            the `Subagent` tool on the same provider.
        workers: How subagents keep their workers; see `Workers`.
        mailbox: Whether the agent has an in-process mailbox and the
            `send_message` tool. A message addressed to a session id is
            added to that session's next user message (the next run's
            prompt when it is idle); it is lost if the server stops first.
        tool_search: Whether a large catalog waits for the model to search
            it, and past which sizes; see `ToolSearch`.
        config_dir: Directory holding `skills/` and `agents/`; `.linch`
            when absent. A relative path resolves against the workspace,
            or the server's working directory without one.
        filesystem: The virtual filesystem; see `Filesystem`.
        tasks: Whether the agent has the session task tools (`TaskCreate`,
            `TaskList`, `TaskGet`, `TaskUpdate`), kept in the session store.
            They run without approval in every permission mode.
        deep_agent: The deep agent preset; see `DeepAgent`.
        profile: The domain profile its runs take; see `Profile`.
    """

    namespace: Required[str]
    spec: Required[Mapping[str, Any]]
    provider: Required[Mapping[str, Any]]
    stores: Mapping[str, Any]
    workspace: str
    bash: bool
    permissions: Mapping[str, Any]
    mcp_servers: Sequence[Mapping[str, Any]]
    skills: bool
    subagents: bool
    workers: Workers
    mailbox: bool
    tool_search: ToolSearch
    config_dir: str
    filesystem: Filesystem
    tasks: bool
    deep_agent: DeepAgent
    profile: Profile
