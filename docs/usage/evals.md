# Evals And Benchmarks

Linch evals are deterministic harness runs over normal `Agent` instances. Use
them to compare providers, prompts, recovery settings, memory setups, or runtime
changes without building a separate test runner.

## Library API

```python
import asyncio

from linch import Agent
from linch.evals import (
    EvalBenchmarkTarget,
    EvalCase,
    EvalSuite,
    ScriptedProvider,
    TextTurn,
    run_eval_benchmark,
    text_contains,
)
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools


def agent_for(*answers: str) -> Agent:
    return Agent(
        model="scripted-eval",
        provider=ScriptedProvider([TextTurn(answer) for answer in answers]),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
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

    result = await run_eval_benchmark(
        suite,
        [
            EvalBenchmarkTarget("candidate-a", agent_for("Paris", "Berlin")),
            EvalBenchmarkTarget("candidate-b", agent_for("Paris", "Paris")),
        ],
    )
    print(result.to_markdown())


asyncio.run(main())
```

`run_eval_benchmark()` creates one `EvalResult` per target and records elapsed
wall time. Each eval case still gets a fresh Linch session through `run_eval()`.

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

Supported scorer types map to the built-in `linch.evals` scorers:

| Type | Fields |
|---|---|
| `text_contains` | `substring` or `text` or `contains` |
| `tool_called` | `tool` |
| `schema_valid` | `schema` |
| `cost_under` | `budget_usd` |
| `context_selected_tool` | `tool` |
| `context_not_trimmed` | none |
| `context_metadata_contains` | `key`, optional `expected` |
| `memory_recalled` | `id` or `ids` |
| `recovery_succeeded` | optional `tool` |
| `run_completed` | none |

Scripted turn files may be JSON or YAML and can either be a list or an object
with a `turns` list. A text turn is:

```json
{"type": "text", "text": "done"}
```

A tool-use turn is:

```json
{
  "type": "tool_use",
  "tool_name": "SearchMemory",
  "tool_input": {"query": "PTO rollover"}
}
```

The CLI intentionally wires scripted agents with `empty_tools()` so it is safe
for CI smoke tests. Use the library API when a benchmark target needs a real
tool registry, memory store, filesystem, hooks, or live provider.
