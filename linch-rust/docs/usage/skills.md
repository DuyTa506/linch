# Skills

Use skills when the model should load reusable prompt workflows by name
during a run.

[← Usage guide](./README.md)

A skill is a named markdown prompt. The model loads one with the `Skill`
tool; the tool returns the skill's body, with its arguments filled in, as the
call's result, and the model acts on it in its next turn. Think of skills as
slash commands the agent calls on itself.

---

## Turn skills on

Set `skills: True` in the agent config. The agent then has the `Skill` tool,
the built-in `verify` skill, and every valid skill in
`<config_dir>/skills/<directory>/SKILL.md`.

- `config_dir` is `.linch` when absent. A relative path resolves against the
  `workspace`, or, without one, against the server's working directory, which
  is the directory your Python process runs in (`Server()` starts the server
  there). An absolute path is used as it is.
- Only that directory is scanned: there is no home or global skills
  directory.
- Skills load once, when the agent opens. To pick up changed files, open a
  new agent. Reloading skills into an open agent is coming.

The system prompt lists every skill the model may invoke, one line each:
`- <name>: <description> — <when_to_use> (args: <argument_hint>)`. A line is
cut at 250 characters. The listing may take 1% of the provider's
[context window](./providers.md#context-window) at four characters a token
(5120 characters for 128000 tokens), as in Python Linch. When it passes that,
every description is cut to an equal share; when a share would be under 20
characters, only the names are listed. The listing is part of the cached
prefix of each request.

---

## The built-in `verify` skill

```text
Skill({"skill": "verify", "args": "focus on billing workflow"})
```

`verify` asks the model to plan and run evidence-based checks of completed
work and end with `VERDICT: PASS`, `VERDICT: FAIL` or `VERDICT: PARTIAL`. Its
prompt is Python Linch's. `args` narrows the focus. A project skill named
`verify` (in any case) replaces it; keep the verdict line in its body if your
host parses it.

---

## Write a project skill

`.linch/skills/release-notes/SKILL.md`:

```markdown
---
description: Write release notes from the session's finished tasks.
when_to_use: Before tagging a release.
argument_hint: version, then audience
arguments: [version, audience]
allowed_tools: [TaskList]
---
# Release notes for $version

List the finished tasks with TaskList, then write notes for $audience.
Templates are in ${SKILL_DIR}/templates.
Full request: $ARGUMENTS
```

Run this from the directory that holds `.linch` (`/srv/app` in the output
below). The scripted provider plays the model: it loads the skill, calls
`TaskList`, and answers.

```python
import asyncio

from linch_rust import (
    AgentConfig,
    ContextBuildEvent,
    Server,
    SkillCompletedEvent,
    SkillInvokedEvent,
    ToolCallEndEvent,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    tool_turn("c1", "Skill", {"skill": "release-notes", "args": "1.4.0 'support team'"}),
    tool_turn("c2", "TaskList", {}),
    text_turn("Notes written."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "skills": True,
    "tasks": True,
    "config_dir": ".linch",
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Write the release notes for 1.4.0.")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ContextBuildEvent):
                print("offered:", event.selected_tools)
            elif isinstance(event, SkillInvokedEvent):
                print("skill_invoked:", event.name, event.args)
            elif isinstance(event, SkillCompletedEvent):
                print("skill_completed:", event.name, "failed" if event.is_error else "ok")
            elif isinstance(event, ToolCallEndEvent) and event.tool_name == "Skill":
                print(event.result["content"][0]["text"])


asyncio.run(main())
```

```text
offered: ['TaskCreate', 'TaskList', 'TaskGet', 'TaskUpdate', 'Skill']
skill_invoked: release-notes 1.4.0 'support team'
Skill base directory: /srv/app/.linch/skills/release-notes

# Release notes for 1.4.0

List the finished tasks with TaskList, then write notes for support team.
Templates are in /srv/app/.linch/skills/release-notes/templates.
Full request: 1.4.0 'support team'

skill_completed: release-notes ok
offered: ['TaskList']
offered: ['TaskCreate', 'TaskList', 'TaskGet', 'TaskUpdate', 'Skill']
```

### Frontmatter

| Field | Required | Meaning |
|---|---|---|
| `description` | yes | A non-empty string, shown in the listing |
| `name` | no | The name the model passes; the directory name when absent. It must match `^[a-z0-9][a-z0-9_-]*$`, ignoring case |
| `when_to_use` | no | Added to the listing line |
| `argument_hint` | no | Shown in the listing as `(args: ...)` |
| `arguments` | no | Names for the positional arguments, each matching `^[a-z_][a-z0-9_]*$`; not `ARGUMENTS`, no duplicates |
| `allowed_tools` | no | The only tools offered on the model turn after a load (see [Overlays](#overlays)) |
| `model` | no | The model of the model turn after a load |
| `version` | no | Metadata |
| `disable_model_invocation` | no | `true` leaves the skill out of the listing, and a call to it fails with `Skill is not invokable by the model` |

The frontmatter is read as YAML 1.2: `yes` and `no` are strings, so write
`disable_model_invocation: true`. A repeated key makes the file invalid.

### Placeholders

| In the body | Becomes |
|---|---|
| `$ARGUMENTS` | The `args` string, as given |
| `$<name>` of each `arguments` entry | The token at that position of `args`, split like a shell line (quotes and backslashes are honoured); empty when missing |
| `${SKILL_DIR}`, `${LINCH_SKILL_DIR}` | The skill's directory |
| `${LINCH_SESSION_ID}` | The calling session's id |

Substitution is one pass, longest placeholder first: a substituted value is
never expanded again, and no shell quoting is added, so quote paths that may
hold spaces. The result always starts with a `Skill base directory: <dir>`
line.

A skill can tell the model to run its bundled scripts through `Bash`, for
example `python "${SKILL_DIR}/scripts/check.py"`. That call goes through the
normal permission checks; loading the skill grants nothing. The directory
must exist on the machine that runs the server.

### Skills that do not load

A skill is left out when its directory has no `SKILL.md`, its frontmatter is
invalid, the directory is a symlink, its name is taken by one of the agent's
tools (a host tool, a workspace tool, `TaskList` when `tasks` is on) or by a
skill loaded before it. `Agent.skipped` lists each one left out, with its
path and reason ([Agent config](./agent.md#skipped-definitions)); a call to a
missing skill fails with `Unknown skill: <name>`.

---

## Overlays

A skill with `allowed_tools` or `model` changes the next model turn only, as
the output above shows: the turn after the load offered only `TaskList`, and
the turn after that offered every tool again.

- `allowed_tools` only narrows what is offered. It never allows a call the
  permission policy would ask about. A call to a tool outside the set fails
  with `Tool "<name>" is not registered`.
- `model` switches the model of that turn. When the run's model ends in
  `[1m]` and the skill's does not, the suffix is kept.
- Nothing is stored for an overlay: a resumed turn gets the overlay it was
  sent with.

---

## Events

A session's first run lists the skills in a `SkillsLoadedEvent` right after
`init`: each skill's `name`, `description`, `when_to_use` and
`argument_hint`, sorted by name. Each `Skill` call adds a `SkillInvokedEvent` (`name`, and `args` when given)
just before its `tool_call_start` and a `SkillCompletedEvent` (`name`, and
`is_error` true when the call failed) just after its `tool_call_end`, as in
Python. Both are durable, so `Agent.events(run_id)` replays them. See
[Events](./events.md).

---

## Permissions

The `Skill` tool is read-scoped: it only returns text, so it runs without
approval in every mode and alongside other read calls. A tool rule with a
`skill` condition asks or denies for one skill:

```python
permissions = {
    "mode": "default",
    "project_root": "/",
    "layers": [
        {"name": "host", "rules": [
            {"type": "tool", "tool": "Skill", "skill": "release-notes", "decision": "ask"},
        ]},
    ],
}
```

With this policy a `Skill` call for `release-notes` suspends the run for
approval like any other ask (see [Runs](./runs.md)); other skills load
without asking. `"skill": "*"` matches every skill.

---

## Durability

- Every skill, its body and its directory path included, is part of the
  agent's run contract. Editing a `SKILL.md`, or opening the agent with the
  skills at another path, gives a new contract, and a run started under the
  old one cannot be resumed (`ContractMismatch`). Keep the same files at the
  same path on every server that may resume a Postgres run.
- When compaction summarizes away a skill's result, the skill's body is put
  back in the summary, so the model keeps following it.

---

## What differs from Python Linch

| Python Linch | linch-rust |
|---|---|
| `FeatureFlags(skills=True)` | `skills: True` in the config |
| `agent.reload_skills()` | Not yet; open a new agent |
| The listing is a reminder in the first prompt | A cached block of the system prompt |
| The `Skill` tool is exec-scoped with auto-allow rules | Read-scoped |
| `skills_loaded` once per session object, so again after a restart | Only on the session's first run, and only when the run offers the `Skill` tool |
| PyYAML: `yes` is `true`, a repeated key keeps the last value | YAML 1.2: `yes` is a string, a repeated key is invalid |

---

## Related pages

- [Subagents](./subagents.md): a child sees the listing when it has the
  `Skill` tool
- [Tools](./tools.md): the rest of the tool surface and permission rules
- [Agent configuration](./agent.md): `skills`, `config_dir` and the other
  switches
