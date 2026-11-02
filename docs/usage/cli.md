# Scaffolding CLI

Use the `linch` CLI to scaffold projects, add tools, and locate installed documentation.

[<- Usage guide](./README.md)

Installing linch registers a `linch` console script that scaffolds agent
projects. It is an *ejector*: the generated code is plain Linch usage that you
own and edit — there is no runtime dependency on the CLI, no lock-in, and no
extra dependencies (the CLI is stdlib-only, so it ships with every install).

---

## `linch new <name>`

Generates a runnable, testable agent project:

```bash
linch new my-agent
cd my-agent
pip install -e '.[dev]'
python -m my_agent.agent   # runs offline — no API key needed
pytest                     # generated tests also run offline
```

```text
my-agent/
├── pyproject.toml            # hatchling, src layout, pytest configured
├── README.md
├── .gitignore
├── src/my_agent/
│   ├── __init__.py
│   ├── agent.py              # build_agent() factory + runnable entry point
│   └── tools/
│       ├── __init__.py
│       └── greet.py          # example @tool
└── tests/
    ├── test_agent.py         # drives the agent with ScriptedProvider
    └── test_greet.py         # assert_tool_contract on the example tool
```

The project is **offline-first**: `build_agent()` falls back to a
`ScriptedProvider` (from `linch.ext.evals`) when `OPENAI_API_KEY` is unset, so the
agent loop and the generated tests work before any credentials exist. Setting
`OPENAI_API_KEY` switches to Linch's default OpenAI provider; `LINCH_MODEL`
overrides the model (default `gpt-5`). See [Providers](./providers.md) to wire
a different provider explicitly.

Project names may be kebab-case (`my-agent`); the Python package uses the
snake-case form (`my_agent`). Names must start with a lowercase letter, use
only lowercase letters/digits with single `-`/`_` separators, and may not be a
Python keyword or a reserved name (`linch`, `src`, `test`, `tests`).

## `linch add tool <name>`

Run from inside a scaffolded project (or pass `--dir`); adds a tool stub plus a
contract test:

```bash
linch add tool web-search
# created src/my_agent/tools/web_search.py
# created tests/test_web_search.py
```

The stub is a `@tool`-decorated async function ready to implement; the test
calls `assert_tool_contract` (from `linch`) so the tool's metadata,
validation, and execution shape are pinned from the first commit. Register the
tool in `build_agent()` by adding it to `empty_tools(...)` — see
[Tools](./tools.md).

The command walks upward to the nearest `pyproject.toml` whose
`[project].dependencies` declares Linch, then locates a Python package under
`src/`. With multiple packages it picks the one matching `[project].name`, or
reports an ambiguity. It refuses to overwrite either generated file.

## `linch docs path`

Print the absolute directory containing the installed `docs/` and `examples/` trees:

```bash
linch docs path
# /path/to/site-packages/linch/_docs
rg 'RunBudget' "$(linch docs path)/docs/usage"
```

The command checks packaged documentation first, then the source checkout for editable
installs. It works from any directory and needs no network. If documentation is missing,
reinstall Linch from a wheel with packaged docs or restore the checkout's `docs/` and
`examples/` directories.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (also `--help`) |
| 2 | Usage error: unknown command, missing argument, invalid name |
| 1 | State error: non-empty target dir, no/ambiguous project, existing file, missing docs |

## Related pages

- [Quickstart](./quickstart.md) — the same shapes, built by hand step by step.
- [Tools](./tools.md) — everything about `@tool`, scopes, and permissions.
- [Evals](./evals.md) — `ScriptedProvider` and offline testing patterns.
