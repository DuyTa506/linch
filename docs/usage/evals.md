# Evals And Benchmarks

Use eval suites to compare agent behavior across providers, prompts, and runtime settings.

Linch evals run normal `Agent` instances through a small scoring harness. Use
them to compare providers, prompts, recovery settings, memory setups, or runtime
changes without building a separate test runner; use `ScriptedProvider` for
deterministic offline tests.

<a id="library-api"></a>

## `run_eval_benchmark` and `EvalSuite`

```python
import asyncio

from linch import (
    Agent,
    FeatureFlags,
    InMemorySessionStore,
    empty_tools,
)
from linch.ext.evals import (
    EvalBenchmarkTarget,
    EvalCase,
    EvalSuite,
    ScriptedProvider,
    TextTurn,
    run_eval_benchmark,
    text_contains,
)


def agent_for(*answers: str) -> Agent:
    return Agent(
        model="scripted-eval",
        provider=ScriptedProvider([TextTurn(text=answer) for answer in answers]),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
    )


async def main() -> None:
    suite = EvalSuite(
        name="capitals",
        cases=[
            EvalCase(prompt="Capital of France?", expected="Paris"),
            EvalCase(prompt="Capital of Germany?", expected="Berlin"),
        ],
        scorers=[text_contains("{expected}")],
    )

    targets = [
        EvalBenchmarkTarget("candidate-a", agent_for("Paris", "Berlin")),
        EvalBenchmarkTarget("candidate-b", agent_for("Paris", "Paris")),
    ]
    try:
        result = await run_eval_benchmark(suite, targets)
        print(result.to_markdown())
    finally:
        for target in targets:
            await target.agent.close()


asyncio.run(main())
```

`run_eval_benchmark()` creates one `EvalResult` per target and records elapsed
wall time. Each eval case still gets a fresh Linch session through `run_eval()`.
Cases run sequentially for each target, and `run_eval()` releases each case's
session before starting the next one. A case passes when the run records no
error and every scorer that returns a non-`None` value returns `True`; a
`None` scorer result is treated as unknown. The `run_completed` scorer returns
`True` only for a successful `ResultEvent` and `None` when no result event is
available.

## Scripted turns, tool calls, and child agents

`ScriptedProvider` consumes exactly one turn from its list for each
`provider.stream()` call. `TextTurn` emits one text response. `ToolUseTurn`
emits one tool-use response with its `tool_name`, `tool_input`, and optional
`tool_id`; it does not itself provide the follow-up text after an ordinary tool
call. The loop executes that tool and normally asks the provider for another
turn, so include another scripted turn. Terminal/final-tool paths can stop
without a follow-up call. If a provider call needs a turn after the list is
exhausted, `ScriptedProvider.stream()` raises `RuntimeError` with a message that
reports how many turns were consumed and how many were configured.

The provider's cursor belongs to the provider object, not to a session. A
foreground child run uses the same provider object as its parent because the
child session is owned by the same `Agent`. Therefore parent and child consume
one shared sequence. In this sequence:

1. The parent consumes the `ToolUseTurn` and calls `Subagent`.
2. The child consumes the next `TextTurn` and returns its text as the tool
   result.
3. The parent resumes and consumes the final `TextTurn`.

This small offline example exercises that ordering; its only enabled optional
feature is subagents:

```python
import asyncio

from linch import Agent, FeatureFlags, InMemorySessionStore, empty_tools
from linch.ext.evals import ScriptedProvider, TextTurn, ToolUseTurn


async def main() -> None:
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Subagent",
                tool_input={"description": "helper", "prompt": "Return the child marker."},
            ),
            TextTurn(text="child marker"),
            TextTurn(text="parent marker"),
        ]
    )
    agent = Agent(
        model="scripted-eval",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=True, mcp=False, filesystem=False),
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("Delegate this task.")]
        result = next(event for event in events if event.type == "result")
        assert result.final_text == "parent marker"
        print(result.final_text)
    finally:
        await agent.close()


asyncio.run(main())
```

It prints `parent marker` without credentials or network access. If a child is
run in the background or multiple runs share a provider concurrently, the
interleaving is scheduling-dependent; give each run its own provider when the
turn sequence must be independently reproducible.

## CLI

Create a suite file:

```json
{
  "name": "capitals",
  "cases": [
    {"prompt": "Capital of France?", "expected": "Paris"},
    {"prompt": "Capital of Germany?", "expected": "Berlin"}
  ],
  "scorers": [
    {"type": "text_contains", "substring": "{expected}"},
    {"type": "run_completed"}
  ]
}
```

Create one scripted target:

```json
{
  "turns": [
    {"type": "text", "text": "Paris"},
    {"type": "text", "text": "Berlin"}
  ]
}
```

Run the benchmark:

```bash
python scripts/eval_benchmark.py capitals.json \
  --scripted baseline=baseline-turns.json \
  --format markdown
```

Repeat `--scripted NAME=PATH` to compare multiple deterministic targets:

```bash
python scripts/eval_benchmark.py capitals.json \
  --scripted baseline=baseline-turns.json \
  --scripted candidate=candidate-turns.json \
  --format json \
  --output eval-report.json
```

The script exits with code `1` when any target falls below `--fail-under`
(`1.0` by default). Use `--fail-under 0` for exploratory benchmarking where a
failing candidate should still produce a report artifact.

## Suite Schema

Suite files may be JSON or YAML:

```yaml
name: memory-smoke
cases:
  - prompt: What policy mentions PTO rollover?
    expected: policy-1
    metadata:
      area: memory
scorers:
  - type: text_contains
    substring: "{expected}"
  - type: cost_under
    budget_usd: 0.05
```

Supported scorer types map to the built-in scorers exported from
`linch.ext.evals`:

| Type | Fields |
|---|---|
| `text_contains` | `substring` (defaults to `{expected}` when omitted) |
| `tool_called` | `tool` |
| `schema_valid` | `schema` |
| `cost_under` | `budget_usd` |
| `context_selected_tool` | `tool` |
| `context_not_trimmed` | none |
| `context_metadata_contains` | `key`, optional `expected` |
| `recovery_succeeded` | optional `tool` |
| `run_completed` | none |

`schema_valid` uses the core `jsonschema` dependency. The scorer returns `False`
for invalid output and also fails closed if the validator is unavailable in a
broken installation, so unchecked output cannot silently pass an eval.

Scripted turn files may be JSON or YAML and can either be a list or an object
with a `turns` list. A text turn is:

```json
{"type": "text", "text": "done"}
```

A tool-use turn is:

```json
{
  "type": "tool_use",
  "tool_name": "read_file",
  "tool_input": {"path": "/memories/MEMORY.md"}
}
```

The CLI intentionally wires scripted agents with `empty_tools()` so it is safe
for CI smoke tests. Use the library API when a benchmark target needs a real
tool registry, file-memory dependencies, filesystem, hooks, or live provider.
