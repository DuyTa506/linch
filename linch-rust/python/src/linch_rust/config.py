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
    """

    background: bool
    retain: bool


class Filesystem(TypedDict, total=False):
    """The virtual filesystem: `ls`, `read_file`, `write_file` and `edit_file`.

    `{}` turns it on. With memory stores each session has its own files;
    with Postgres stores every session of the namespace shares them.

    Attributes:
        offload_threshold_tokens: Move a text tool result estimated above
            this many tokens (four bytes per token) into the filesystem,
            leaving its first lines and its path. Absent: never offload.
    """

    offload_threshold_tokens: int


class AgentConfig(TypedDict, total=False):
    """An agent as data: what `Server.open_agent` opens.

    Only `namespace`, `spec` and `provider` are required. Every switch is
    off when absent, except `bash`. A config never carries a provider key:
    the server reads it from its environment.

    Attributes:
        namespace: Store key space for this agent's runs and sessions.
        spec: The model, instructions and loop limits, such as
            `{"model": "deepseek-flash", "system_prompt": "Be brief."}`.
        provider: The model provider chosen by `id` (`deepseek`,
            `openai-chat`, `openai-responses`, `anthropic`, `gemini`,
            `vllm`, `sglang`, `llamacpp`, or `scripted` with its `turns`),
            with an optional `base_url`.
        stores: `{"type": "memory"}` (the default: runs live in the agent
            handle) or `{"type": "postgres", "dsn": ...}`.
        workspace: An existing directory, the root of the built-in tools
            `Read`, `Write`, `Edit`, `Grep`, `Glob` and `Bash`. Absent: no
            workspace tools.
        bash: Whether the workspace tools include `Bash`, which runs
            commands as the server's user. On when absent.
        permissions: `{"mode": ..., "project_root": ...}` and rules. Mode
            `default` (read tools run, write and exec tools ask) when absent.
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
        config_dir: Directory holding `skills/` and `agents/`; `.linch`
            when absent. A relative path resolves against the workspace,
            or the server's working directory without one.
        filesystem: The virtual filesystem; see `Filesystem`.
        tasks: Whether the agent has the session task tools (`TaskCreate`,
            `TaskList`, `TaskGet`, `TaskUpdate`), kept in the session store.
            They run without approval in every permission mode.
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
    config_dir: str
    filesystem: Filesystem
    tasks: bool
