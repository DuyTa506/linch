# CLI

Use the `linch-rust` command to scaffold a project, add host tools to it,
find the documentation installed with the package, and install the skill
that teaches a coding agent to use it.

[<- Usage guide](./README.md)

Installing the `linch-rust` wheel installs a `linch-rust` console script. The
code it writes is plain `linch_rust` usage that you own and edit; nothing in
the project depends on the CLI. From a checkout without the wheel, run the
same commands as `python -m linch_rust.cli`.

---

## `linch-rust new <name>`

Writes a project that runs and tests offline on the scripted provider:

```bash
linch-rust new demo-agent
```

```text
created demo-agent/pyproject.toml
created demo-agent/README.md
created demo-agent/.gitignore
created demo-agent/src/demo_agent/__init__.py
created demo-agent/src/demo_agent/agent.py
created demo-agent/src/demo_agent/tools/__init__.py
created demo-agent/src/demo_agent/tools/greet.py
created demo-agent/tests/test_agent.py
created demo-agent/tests/test_greet.py
created demo-agent/.claude/skills/build-agent-with-linch-rust
created demo-agent/.agents/skills/build-agent-with-linch-rust

Next steps:
  cd demo-agent
  pip install -e '.[dev]'
  python -m demo_agent.agent   # runs offline, no API key needed
  pytest
```

```text
demo-agent/
├── pyproject.toml          # hatchling, src layout, pytest configured, depends on linch-rust
├── README.md
├── .gitignore
├── .claude/skills/build-agent-with-linch-rust/   # the coding-agent skill, for Claude Code
├── .agents/skills/build-agent-with-linch-rust/   # the same, for Codex
├── src/demo_agent/
│   ├── __init__.py
│   ├── agent.py            # build_config(), ask(), and TOOLS
│   └── tools/
│       ├── __init__.py
│       └── greet.py        # an example @tool(scope="read")
└── tests/
    ├── test_agent.py       # runs the agent on scripted turns
    └── test_greet.py       # checks the tool's declaration and answer
```

`python -m demo_agent.agent` prints the offline reply:

```text
Hello from demo-agent (offline scripted reply).
```

and `pytest` runs the generated tests against a real sidecar, still offline:

```text
5 passed
```

What `agent.py` does:

- `build_config(turns=None)` returns the `AgentConfig`. With `turns` it uses
  the scripted provider on them. Without, it uses OpenAI's Responses API when
  `OPENAI_API_KEY` is set (model `LINCH_MODEL`, default `gpt-5`) and a
  scripted offline reply otherwise.
- `ask(prompt, config=None)` starts a `Server`, opens the agent with `TOOLS`,
  runs the prompt in session `main` and returns the final text.
- `TOOLS` lists the host tools the agent gets.

Edit `build_config` for another provider ([Providers](./providers.md)),
stores or permissions ([Agent config](./agent.md)).

Options and rules:

- `--dir DIR` makes the project under `DIR` instead of the current directory;
  the next steps then `cd DIR/<name>`.
- The name may be kebab-case (`demo-agent`); the package is its snake-case form
  (`demo_agent`). It starts with a lowercase letter, uses lowercase letters and
  digits with single `-` or `_` separators, and is not a Python keyword or one
  of `linch`, `linch_rust`, `src`, `test`, `tests`.
- The target directory must not exist or be empty.
- The project gets the `build-agent-with-linch-rust` skill for Claude Code and
  Codex, as `linch-rust skill install` writes it ([below](#linch-rust-skill-install)).

## `linch-rust add tool <name>`

Run it inside a project (or pass `--dir`). It writes a host tool stub and its
test:

```bash
cd demo-agent
linch-rust add tool web-search
```

```text
created src/demo_agent/tools/web_search.py
created tests/test_web_search.py
```

```python
"""The web_search host tool."""

from linch_rust import tool


@tool(scope="read")
async def web_search(query: str) -> str:
    """TODO: describe web_search; the model reads this."""
    # TODO: implement web_search.
    return f"web_search results for: {query}"
```

Give it to the agent by adding it to `TOOLS` in `agent.py`. The stub is
`read`-scoped; change the scope to `write` or `exec` if the tool changes
anything ([Tools](./tools.md#scopes)).

The command walks up from the directory to the nearest `pyproject.toml` whose
`[project].dependencies` names `linch-rust`, then picks the package under
`src/` (the one named like the project when there are several). It never
overwrites a file.

## `linch-rust docs path`

Prints the directory of the documentation installed with the package, these
usage pages and the [architecture pages](../architecture/README.md). It works
offline, from any directory:

```bash
linch-rust docs path
# /path/to/site-packages/linch_rust/_docs
grep -rn "NotSuspended" "$(linch-rust docs path)/usage"
```

It looks in the installed package first, then in the checkout the client runs
from.

## `linch-rust skill install`

Copies the `build-agent-with-linch-rust` skill into a project, so a coding agent
working there finds these docs and builds on `linch_rust` the way they
describe. The skill teaches the Python client: where the docs are and how to
search them, the main objects, testing on the scripted provider, debugging,
and when to run more than one sidecar. Its `references/design.md` helps choose
an architecture before building.

```bash
linch-rust skill install                    # .claude/skills and .agents/skills
linch-rust skill install --target linch     # .linch/skills
linch-rust skill install --force            # replace an installed copy after upgrading
```

```text
created .claude/skills/build-agent-with-linch-rust
created .agents/skills/build-agent-with-linch-rust
```

| `--target` | Directory | For |
|---|---|---|
| `claude` | `.claude/skills/build-agent-with-linch-rust` | Claude Code |
| `codex` | `.agents/skills/build-agent-with-linch-rust` | Codex |
| `linch` | `.linch/skills/build-agent-with-linch-rust` | An agent with `skills: True` and the default `config_dir` ([Skills](./skills.md)) |

- `--target` can be repeated; without it the skill goes to `claude` and
  `codex`.
- `--dir DIR` installs into the project at `DIR` instead of the current
  directory.
- An installed copy is never overwritten: the command stops before copying
  anything, with code 1. `--force` replaces each copy, dropping files an
  older version had.
- The skill is the one shipped with the installed package, so it matches its
  docs. It works offline, from the package first, then from the checkout the
  client runs from.

## Exit codes

| Code | Meaning | Example message |
|---|---|---|
| 0 | Success, and `--help` | |
| 2 | Usage error: an unknown command, a missing argument, an invalid name or target | `linch-rust: error: invalid project name 'My_Agent': use lowercase letters and digits with single '-' or '_' separators, starting with a letter` |
| 1 | The files on disk are in the way: a non-empty target, no project, an ambiguous package, a file or skill that exists, missing docs or skill | `linch-rust: error: target demo-agent already exists and is not empty` |

Other messages with code 1:

```text
linch-rust: error: no linch-rust project found from . upward (run it inside a project made by 'linch-rust new'; its pyproject.toml must depend on 'linch-rust')
linch-rust: error: refusing to overwrite existing file .../src/demo_agent/tools/web_search.py
linch-rust: error: docs not found: reinstall linch-rust from a wheel, or use a checkout that has linch-rust/docs
linch-rust: error: .claude/skills/build-agent-with-linch-rust already exists; pass --force to replace it
linch-rust: error: skill not found: reinstall linch-rust from a wheel, or use a checkout that has linch-rust/skills
```

## Related pages

- [Quickstart](./quickstart.md): the same shapes, by hand.
- [Tools](./tools.md): `@tool`, scopes and approvals.
- [Providers](./providers.md#the-scripted-provider): scripted turns for tests.
- [Skills](./skills.md): skills for the agents you build, a different thing
  from this coding-agent skill.
