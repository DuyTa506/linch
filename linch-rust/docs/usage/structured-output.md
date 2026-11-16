# Structured output

[Tiếng Việt](../vi/usage/structured-output.md)

Use an output schema when the caller needs the run's final answer as data: a
JSON object that satisfies a JSON Schema you give. The run checks the answer,
sends a failing one back to the model with the error, and ends with the parsed
object in `ResultEvent.structured_output`.

---

## Set a schema on the agent

The schema goes in `spec.output_schema`; `spec.structured_output_retries`
bounds how many failing answers go back to the model (0 when absent).

```python
from linch_rust import AgentConfig, OutputSchema, Server

invoice: OutputSchema = {
    "name": "invoice",
    "schema": {
        "type": "object",
        "properties": {"total": {"type": "number"}, "currency": {"type": "string"}},
        "required": ["total", "currency"],
        "additionalProperties": False,
    },
}

config: AgentConfig = {
    "namespace": "billing",
    "spec": {
        "model": "claude-sonnet-4-6",
        "output_schema": invoice,
        "structured_output_retries": 2,
    },
    "provider": {"id": "anthropic"},
}

async with Server() as server:
    agent = await server.open_agent(config)
    run = await agent.run("s1", "Bill 3 widgets at 4 EUR each.")
    result = await run.result()
    print(result.structured_output)  # {'total': 12, 'currency': 'EUR'}
```

| `OutputSchema` field | Default | What it controls |
|---|---|---|
| `name` | required | Identifier sent to the provider; not blank |
| `schema` | required | A JSON Schema (draft 2020-12) whose root is `{"type": "object", ...}` |
| `strict` | `True` | Whether a provider that can enforce the schema exactly should (OpenAI) |
| `description` | none | What the answer is, sent to providers that take one |

`agent.open` refuses a schema that does not compile, one whose root `type`
is not `object`, and a blank `name` (`OpenError`, code -32011). The schema and
the retries are part of the [run contract](./agent.md#the-run-contract); an
agent without them keeps the contract it had.

## Give one run its own schema

`agent.run(..., output_schema=...)` replaces the agent's schema for that run
only. The agent's `structured_output_retries` still bound it, so an agent
whose runs bring their own schema may set the retries without a schema of
its own.

```python
person: OutputSchema = {"name": "person", "schema": {"type": "object", "required": ["name"]}}
run = await agent.run("s2", "Who wrote the Analytical Engine notes?", output_schema=person)
```

The run stores the schema in its first `init` event (`InitEvent.output_schema`),
so a `resume` or `approve` uses it again, from any process. A schema that does
not compile, or whose root is not an object, ends the run with
`InvalidOutputSchema` (code -32031) before anything is written: no run or
session exists. A per-run schema does not change the contract.

## What the run checks

Only the final answer is checked: a response without tool calls, after any
background subagents reported. Tool calls and their results are never
checked.

1. The text is read as JSON; when it is not JSON, the first fenced block
   (```` ```json ```` or a bare ```` ``` ````) is read instead.
2. The value must be a JSON object, then satisfy the schema.

A failing answer is kept in the session, and a `verification` event
(`verifier: "output_schema"`, `action: "retry"`, `attempt` from 1) records
it. The model then gets this, in a `<system-reminder>` user message, and
answers again:

```text
Your previous response failed structured-output validation: <error>
Respond again with ONLY a JSON object matching the required schema.
```

`<error>` is `JSON parse error: ...`, `Expected a JSON object, got array`, or
`Schema validation error:` followed by one `- path: message` line per
violation.

When the retries are spent, or with `structured_output_retries` 0, the next
failing answer ends the run: a `verification` event with `action: "stop"`,
an `error` named `StructuredOutputError`, and a `result` of subtype `error`
whose `structured_error` is the last error and whose `final_text` is the
model's text. `max_turns` still applies, so it can end the run first.

The retry count is durable: a run that crashed and resumed counts the retries
it already spent from its events, so a crash never grants more.

## With callbacks

The check runs before the `stop` callbacks, so they only see an answer that
satisfies the schema, and their request has `structured_output`, the parsed
object, beside `text`.

| `stop` answer | What happens |
|---|---|
| `allow` | The run ends with that answer |
| `rewrite` | The new text is checked again. A valid rewrite is the result; an invalid one ends the run failed with `StructuredOutputError`, without asking the model again |
| `retry`, `continue` | The model answers again, and that answer is checked from the start. These do not spend `structured_output_retries` |
| `block` | The run fails, as without a schema |

## What each provider receives

Every request of the run, the summary call of a compaction excluded, carries
the schema. Each adapter sends it in the vendor's own structured-output form,
which shapes only the text answer, so the model can still call tools on any
turn:

| Provider | Sent as |
|---|---|
| `anthropic` | `output_config.format` = `{"type": "json_schema", "schema": ...}` |
| `openai-chat` | `response_format` = `{"type": "json_schema", "json_schema": {name, strict, schema, description?}}` |
| `openai-responses` | `text.format` = `{"type": "json_schema", name, strict, schema, description?}` |
| `gemini` | `generationConfig.responseMimeType` `application/json` and `responseJsonSchema` |
| `deepseek`, `vllm`, `sglang`, `llamacpp` | Refused before the call: the request fails with an `error` event naming the adapter |

Vendors accept only part of JSON Schema in these forms (Anthropic and OpenAI
strict mode want `additionalProperties: false` on every object, for example).
The schema is sent as you wrote it; the run still checks every answer itself,
so a provider that drifts from the schema gets the retry above. Gemini
combines a JSON response with function calling from Gemini 3 on. These
request shapes follow each vendor's API docs; they have not yet been run
against a live API.

A run that fails over to one of `spec.fallback_models` sends the fallback the
same schema, checks its answers the same way, and keeps counting the schema
retries the run already spent.

## How it differs from Python Linch

| Python Linch | linch_rust |
|---|---|
| Past the retries the run succeeds with `structured_error` set | The run fails (`result` subtype `error`) with `structured_error` set |
| The retry count restarts on resume | The retry count is read back from the run's events |
| `verification` with `action: "exhausted"` | `action: "stop"` |
| Anthropic `compatible` mode forces a final tool | Every provider uses its native form; no forced tool |
| `final_tool_name`, a per-agent `tool_choice` | Not yet |

## Related pages

- [Agent config](./agent.md#spec): the rest of `spec`
- [Runs](./runs.md#errors-arrive-at-the-end-of-the-stream): `InvalidOutputSchema` and the other errors
- [Events](./events.md): `VerificationEvent` and `ResultEvent`
- [Architecture: turn lifecycle](../architecture/turn-lifecycle.md#the-loop-continues-while-a-response-has-tool-calls): where the check runs and what it writes
