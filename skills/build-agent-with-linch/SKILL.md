---
name: build-agent-with-linch
description: Design, build, extend, debug, and test AI agents on the Linch Python SDK. Use this skill whenever a task touches Linch or `from linch import`, or asks for an agent with tools, hooks, RAG or context injection, MCP servers, skills, subagents, structured output, memory, compaction, budgets, durable resume, a custom provider, deployment on Linch, or choosing an architecture for an agent pipeline, even if the request only says "make an agent" inside a project that depends on linch.
---

# Build agents with Linch

Linch changes between versions, and what you remember about it may be wrong. The docs for the
exact installed version ship inside the package, so read them instead of guessing.

## Design first when the architecture is open

Most tasks skip this. Read [references/design.md](references/design.md) before building when the
system has several stages or agents, runs for a long time, takes actions that are hard to undo,
or the user is asking how to structure it. For a single agent with a few tools, go to step 1.

## 1. Find the docs

```bash
DOCS="$(linch docs path)"
ls "$DOCS/docs/usage" "$DOCS/examples"
```

If `linch` is not on `PATH`, activate the project's environment or run
`python -m linch.ext.scaffold docs path`. If Linch is not installed, install it before writing
code.

Search with `grep -rn`, not `rg`. Installed docs sit in `site-packages`, and `rg` can skip them
silently because of ignore rules, which looks like the docs have no answer.

```bash
grep -rn "ContextInjectionHook" "$DOCS/docs/usage"
```

- Exact name, signature, or import: `docs/usage/api-reference.md`, one heading per public name.
- An error message: grep its exact text, starting with `docs/usage/troubleshooting.md`.
- How something works inside: `docs/architecture/README.md`.
- A runnable example: `docs/usage/examples.md` is the index.

Read Linch source only when the docs do not answer. Report the gap if that happens.

## 2. Read before you write

Find what the task needs and read those pages before writing code. Most tasks need more than one
row. Paths are relative to `$DOCS`.

| You need | Read |
|---|---|
| First agent, project setup | `docs/usage/quickstart.md`, `docs/usage/cli.md` |
| Agent options, sessions, system prompt, budgets | `docs/usage/agent.md` |
| Custom tools, built-in tools, permissions, MCP servers | `docs/usage/tools.md` |
| Blocking, changing, or observing behavior with hooks | `docs/usage/hooks.md` |
| RAG, context injection, memory | `docs/usage/context-and-memory.md` |
| Skills for the agent you are building | `docs/usage/skills.md` |
| Subagents, teams, mailboxes | `docs/usage/coordination.md`, `docs/usage/deep-agent.md` |
| Typed output | `docs/usage/structured-output.md` |
| Providers, models, a custom adapter | `docs/usage/providers.md`, `docs/architecture/provider-contract.md` |
| Streaming to a UI, reacting to events | `docs/usage/events.md` |
| Files and result offload | `docs/usage/filesystem.md` |
| Long jobs, resume after a crash | `docs/usage/run-stores.md`, `docs/usage/production.md` |
| Multi-step flows and loops | `docs/usage/workflows.md`, `docs/usage/loop-runner.md` |
| Reusable packages and plugins | `docs/usage/extending.md` |
| Scripted tests and evals | `docs/usage/evals.md` |

## 3. Build it the way Linch expects

- Import from `linch`. The other supported paths are `linch.ext.*` and the modules listed in
  `docs/versioning.md`. Other submodules are private and can change in any release.
- For a new project, start from `linch new <name>`. It produces a runnable project with offline
  tests. Use `linch add tool <name>` for each tool.
- Tools are duck-typed: use `@tool` or implement the protocol. There is no base class to inherit.
- Pass `tools=` a registry such as `empty_tools(...)`, not a list.
- Add behavior through hooks, context builders, and registries. Do not wrap or copy the agent loop.
- Call `await agent.close()` when you finish, so MCP connections and other resources are released.
- Give each tool the narrowest scope and permission that does the job.

## 4. Test it like it is real

A test that replaces the part you depend on proves nothing about that part. Linch itself once
shipped an MCP shutdown crash that passed every test because all of them used a fake transport.
So fake only the model, and keep everything else real.

1. Run through the real loop. Use `ScriptedProvider` with `TextTurn` and `ToolUseTurn` from
   `linch.ext.evals`, then drive `session.run(...)`, so tools, hooks, permissions, and events all
   execute. Do not call your tool function directly and stop there.
2. Use real boundaries. Start a real local MCP server subprocess, write real files in a temp
   directory, run a real store, and query a real or local retriever. Cover startup and shutdown as
   well as the happy path, including `await agent.close()`.
3. Test the failure the feature exists for: the hook that blocks must block, a budget must stop a
   run, a resumed run must really be interrupted first, and an empty retrieval must behave well.
4. Assert on what happened, such as events, tool results, and the request the provider received.
   A scripted final answer is text you wrote yourself, so asserting on it alone proves nothing.
5. Run the agent end to end with a real model when credentials or a local endpoint exist. Keep
   that test separate and skip it cleanly without credentials. Check `docs/usage/providers.md` for
   how to configure the endpoint. Never print or commit a key.

## 5. Finish

Before you call it done, run the project's tests and read the output. Then report what you built,
which test commands passed, and what ran against a real model. If the live run did not happen, say
so plainly, because offline tests do not prove the model works with your prompts and tools.

If you hit a place where the docs were wrong or silent, say which page.
