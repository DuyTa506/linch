# Security guardrails

[Tiếng Việt](../vi/usage/security.md)

Three guardrails decide what an agent may do and what may leave it:
permission modes decide which tool calls run, redaction scrubs sensitive
text before anything stores or streams it, and verifiers check the final
answer before the run completes. All three are config and need no code in
the server. Redaction and verifiers are part of the
[run contract](./agent.md#the-run-contract); an agent without them sends the
same requests and writes the same checkpoints and events as before they
existed.

---

## Permission modes

`permissions.mode` sets what a tool call does when no
[permission rule](./tools.md#permission-rules) matches it:

| Mode | Read tools | Write tools | Exec tools | Python Linch |
|---|---|---|---|---|
| `default` | run | ask | ask | `default` |
| `accept_edits` | run | run, unless the target path is outside `project_root`, which asks | ask | `acceptEdits` |
| `skip_dangerous` | run | run | run | `skip-dangerous` |

A call that asks suspends the run for `agent.approve`, or goes to a
`permission_request` callback ([approvals](./tools.md#approvals)).
`auto_allow` lists tools that run in every mode unless a rule says
otherwise; `tasks` and `deep_agent` put the task tools there.

```python
from linch_rust import AgentConfig, Permissions

permissions: Permissions = {
    "mode": "accept_edits",
    "project_root": "/srv/acme",
    "auto_allow": ["lookup"],
}
config: AgentConfig = {
    "namespace": "acme",
    "spec": {"model": "deepseek-flash", "max_turns": 20},
    "provider": {"id": "deepseek"},
    "workspace": "/srv/acme",
    "permissions": permissions,
}
```

On a remote server a tenant may use only the modes in its
`permission_modes` allowlist, `default` and `accept_edits` unless the server
says otherwise. `auto_allow` entries and rules that allow need
`skip_dangerous` in that list, since each lets a call run without asking
([Server](./server.md)).

Python Linch puts its permission mode and rules in its run contract; here
they are not, so `resume` and `approve` take the policy the agent has now.

## Redaction

`spec.redaction` holds regex rules that replace sensitive text. Linch ships
the mechanism and no patterns, as Python Linch's `RedactionHook` does: you
decide what is sensitive and what replaces it.

```python
from linch_rust import Redaction

redaction: Redaction = {
    "rules": [
        {"pattern": r"sk-[A-Za-z0-9]{20,}", "replacement": "[API_KEY]"},
        {"pattern": r"(?i)[\w.+-]+@([\w-]+\.)+[a-z]{2,}", "replacement": "[EMAIL]"},
    ],
}
config["spec"] = {"model": "deepseek-flash", "redaction": redaction}
```

| Field | Default | What it controls |
|---|---|---|
| `rules` | required | Rules applied in order, each to the output of the one before; at least one |
| `rules[].pattern` | required | A regular expression in the [`regex` crate's syntax](https://docs.rs/regex/latest/regex/#syntax): linear-time matching, no look-around or back-references; `(?i)` for any case |
| `rules[].replacement` | `[REDACTED]` | What replaces a match; `$1` or `${name}` names a group, `$$` is a literal `$` |
| `response` | `True` | Scrub what the model writes |
| `tool_results` | `True` | Scrub what tools return |
| `prompt` | `False` | Scrub a new run's prompt, the host's own input |

What each surface covers:

| Surface | Scrubbed | Not sent while it is on |
|---|---|---|
| `response` | The text and unsigned reasoning of every assistant response, before the `after_provider_call` callbacks see it and before it is logged; a callback's rewrite again; the final answer after a `stop` rewrite; compaction summaries | `partial_assistant` deltas: a match can span two of them |
| `tool_results` | Every string of a tool result (the output's value and metadata, an error's message and details) before the `post_tool_use` callbacks see it, and again after one replaced it, so an offloaded result is stored scrubbed; `AskUser` answers; a background worker's answer; peer messages from the mailbox | `tool_progress` events |
| `prompt` | The text of a new run's prompt, before the `user_prompt_submit` callbacks, and again after a rewrite | nothing |

On each surface that is on, what is scrubbed is what the session logs and
what the model reads back on the next turn: that text never reaches a
store, an event, a callback or the next request in its raw form. A crash at
any write keeps it that way, since nothing holding response text is written
before the redaction. The parts listed below are not scrubbed, and do reach
the `assistant` and `tool_call_start` events and the log as written.

Never changed:

- A tool call's input. The tool runs what is logged; to stop a model from
  passing a secret to a tool, deny the call with a rule or a `pre_tool_use`
  callback.
- Reasoning with a provider signature (Anthropic's thinking). Its signature
  would no longer verify when the response is sent back. Turn thinking off
  when its text must be scrubbed.
- Encrypted reasoning, and object keys in a tool result.

`agent.open` refuses a rule that does not compile, one that matches empty
text (it would insert its replacement between every two characters), no
rules, and every surface off (`OpenError`, code -32011). A remote tenant may
give at most 32 rules of at most 1024 bytes each (`NotAllowed`, `kind`
`redaction`); each compiled pattern is bounded to 1 MiB. Subagents take the
agent's redaction.

## Verifiers

`spec.verification` holds checks the final answer must pass, Python Linch's
`FinalAnswerVerifierHook`. A failed answer goes back to the model with
feedback while retries last.

```python
from linch_rust import Verification

verification: Verification = {
    "max_retries": 2,
    "verifiers": [
        {
            "name": "cites",
            "check": {"type": "matches", "pattern": r"\[\d+\]"},
            "feedback": "Cite a source for each claim as [n].",
        },
        {
            "name": "no_keys",
            "check": {"type": "forbids", "pattern": r"sk-[A-Za-z0-9]{20,}"},
            "on_fail": "stop",
        },
        {
            "name": "topic",
            "check": {"type": "keywords", "keywords": ["refund", "policy"]},
        },
    ],
}
config["spec"] = {"model": "deepseek-flash", "verification": verification}
```

| Field | Default | What it controls |
|---|---|---|
| `max_retries` | 2 | How many failed answers go back to the model per run, the verifiers and `stop` callbacks together |
| `verifiers` | none | Built-in verifiers, run in order; the first failure decides |
| `verifiers[].name` | required | Names it in `verification` events; unique, not `output_schema` |
| `verifiers[].check` | required | `{"type": "matches", "pattern"}`: the pattern must match; `{"type": "forbids", "pattern"}`: it must not; `{"type": "keywords", "keywords", "any"?, "case_sensitive"?}`: each keyword (or one, with `any`) must appear, in any case unless `case_sensitive` |
| `verifiers[].feedback` | a sentence naming the check | What the model is told on a retry |
| `verifiers[].on_fail` | `retry` | `retry`: send the feedback while retries last, then accept the answer; `stop`: fail the run at once |

How a run goes:

1. A text-only answer is checked against the
   [output schema](./structured-output.md) first, if there is one, then
   against the verifiers, in order, then the `stop` callbacks run.
2. A `retry` failure stages a `verification` event (`action` `retry`,
   `attempt` the retries spent, 1 on the first), logs the feedback as the
   next user message in a `<system-reminder>`, and takes another turn.
3. Past `max_retries`, the answer is accepted as it is, with a
   `verification` event of `action` `exhausted`, as in Python Linch.
4. A `stop` failure ends the run failed: a `verification` event of `action`
   `stop`, an `error` event named `VerificationError`, and a result with
   subtype `error` that keeps the answer's text.

The verifiers check the text the session logged, so with redaction on they
see the redacted text, and a resumed run decides the same.

### A verifier in your process

A `stop` callback that answers `retry` is the client form of a verifier.
Without `spec.verification`, a `stop` retry is honoured once per run call.
With it (even with no built-in verifiers, `{"max_retries": 3}`), each
`stop` retry draws on `max_retries`, is a durable `verification` event
named after the callback's `policy_id`, and past the budget counts as an
allow with an `exhausted` event.

```python
from typing import Any

from linch_rust import callback


@callback("stop", "polite", 1)
def polite(request: dict[str, Any]) -> dict[str, Any]:
    if "please" in request["text"]:
        return {"action": "allow"}
    return {"action": "retry", "feedback": "Ask politely."}


config["spec"] = {"model": "deepseek-flash", "verification": {"max_retries": 3}}
agent = await server.open_agent(config, callbacks=[polite])
```

### Budgets

The schema and the verifiers spend separate budgets:

| Budget | Spent by | Past it |
|---|---|---|
| `structured_output_retries` (0 by default) | answers that break the output schema | the run fails with a `StructuredOutputError` |
| `verification.max_retries` (2 by default) | built-in verifiers and `stop` callback retries | the answer is accepted, `exhausted` |

Each retry is a provider call, so `max_turns` and the budget bound both.
Retries are counted from the run's durable events, so a crash and a resume
never grant more. A subagent's runs do not take the agent's verifiers.

`agent.open` refuses a blank, repeated or reserved name, a pattern that does
not compile or matches empty text, and an empty or blank keyword
(`OpenError`, code -32011). A remote tenant may give at most 32 verifiers,
each with a pattern of at most 1024 bytes or at most 32 keywords
(`NotAllowed`, `kind` `verification`).

## Tool order and completion gates

[`spec.policy`](./agent.md#policy) refuses a call until the tools it needs
succeeded, and holds a final answer back until the run did what it must. A
gate fails the run when the model does not comply (a verifier accepts the
answer once its retries are spent). It is a reliability aid: a refused call
asks no approval and runs nothing, but a prerequisite is **not a security
boundary**, since a tool that can start other work can do what a refused one
would, and a call counts by its tool name and its rendered result alone (a
host tool of the same name, a hook that answers the call, or a cache hit
counts too). Permissions, redaction and the [sandbox](./sandbox.md) are
the guardrails.

## Trust boundary

The connection is the trust boundary, and the tenant is what it carries. The
host authenticates its own users and asserts the principal a run acts for;
linch does not authenticate users. The server checks one thing about a
principal: its `tenant` must be the connection's tenant (`admit_principal`,
`linch-rpc/src/access/admit.rs:216-224`). It does not check the `user` or the
`roles`.

What follows from that:

- Any connection of a tenant can `resume`, `approve`, `answer`, `cancel` and
  read the `events` of any run of that tenant, given the run id. `cancel` and
  `events` take no principal at all (`linch-rpc/src/server/run.rs:91-126`,
  `server/agents.rs:136-143`).
- A continuation acts for the principal it names, instead of the run's own
  ([Runs](./runs.md#acting-for-a-principal)). Approving a suspended run as a
  different principal (an admin approving a user's run) is an intended flow.
- A workflow differs: its journal refuses a claim that names another
  principal than the one it was started for (`PrincipalMismatch`,
  `linch-ext/src/workflow/conformance.rs:16`). A run has no such check.

So the host must:

- Map its own user identity to the principals it passes, and never let an end
  user choose the `principal` or the `run_id` of a call to linch.
- Authorize before calling linch: check that this user may touch this run,
  then call. Keep run ids unguessable (a caller-chosen id such as
  `order-42-reply` is guessable); the server does not stop a connection from
  naming another run of its tenant.
- Give each trust domain its own tenant. Two parties that must not see each
  other's runs belong in two tenants, not two principals.

## Untrusted content

A tool result, an MCP result and a subagent's answer go back to the model as
plain text. Only memory recall is fenced: it is wrapped in
`<memory-data untrusted="true">` and its stored text is escaped
(`linch-tools/src/memory_recall.rs:35`). Text that a web page, a file, an
email or another system put into a tool result can therefore tell the model
what to do (prompt injection), and the model may follow it.

**What approval and permissions still stop**

- An injected tool call is decided like any other: by the permission mode and
  rules, per call, not by what the content says ([Permission
  modes](#permission-modes)). A call that asks suspends the run.
- Content cannot change the run's principal, permissions or hooks: they come
  from the run's options, not from the transcript.
- An MCP tool is treated as `exec` scope unless the server is configured with
  `trust_annotations`, because a server's `readOnlyHint` is untrusted
  (`linch-mcp/src/config.rs:152`, default `false` at `:173`). It asks before
  it runs in `default` mode ([Tools](./tools.md#mcp-tools)).

**What they do not stop**

- Steering the model toward any tool the policy already lets run without
  asking: read tools, `auto_allow`, an allow rule, `accept_edits` for edits
  inside the project, and every call in `skip_dangerous`. That includes
  exfiltration through an allowed tool (a read tool that fetches a URL built
  from private data, a write tool that stores it where someone else reads it).
- A `post_tool_use` hook sees the injected result but fails open by default:
  one that crashes or times out lets the result through
  ([Hooks](./agent.md#callbacks)).
- A subagent's answer reaches its parent unlabelled, and the child runs with
  the parent's permissions.
- The kernel wraps its own feedback in `<system-reminder>` tags
  (`linch-core/src/skills.rs:162`). Tool output is not escaped, so a result
  can contain text that imitates such a wrapper. The kernel does not
  neutralise it today. A host that needs it can filter or rewrite results in a
  `post_tool_use` hook with `fail_mode: "closed"`.

**Guidance for the host.** This is guidance, not a guarantee.

- Give each agent the fewest tools that do its job. Do not give one agent
  untrusted input, sensitive data and a way to communicate externally at the
  same time without approval on the last step; if an agent needs all three,
  require approval for the calls that write, execute or reach the network.
- Require approval (a `default` mode, or an `ask` rule) for write, exec and
  network tools that run on data from an untrusted source. Prefer `default` to
  `skip_dangerous` there.
- Set `fail_mode: "closed"` on every hook that guards data.
- Keep secrets out of tool inputs: a tool call's input is never scrubbed by
  [redaction](#redaction) and is captured by [telemetry](#telemetry-content-capture).
  Give a tool its credential through its own configuration, not through the
  model.

## Overload behaviour

What happens when a resource is full, from the code at the time of writing:

| Resource | Default | When full | How long it waits | Error |
|---|---|---|---|---|
| Tenant `max_runs` | 16 (`linch-rpc/src/access/allowlists.rs:18`) | `run`, `resume` or `approve` is rejected; nothing starts | Not at all: `try_acquire_owned` answers at once (`access.rs:115-117`, `:291-299`) | `LimitReached` -32005, `kind: runs` |
| Provider concurrency gate (`max_provider_concurrency`) | No cap | The call joins a first-in-first-out queue | Without a limit; only the run's cancel ends the wait (`linch-core/src/provider/gate.rs:87-92`). The queue has no length limit and sheds nothing | None |
| Provider HTTP | Connect 30 s, read 300 s (`linch-providers/src/clients.rs:132-133`) | The call fails | The timeout; `request.timeout_ms` replaces the read one ([Providers](./providers.md)) | A provider error ([Failures and retries](./providers.md#failures-and-retries)) |
| Postgres pool | 30 s to acquire a connection (`linch_postgres::ACQUIRE_TIMEOUT`) | The store call fails with `pool timed out after 30s: raise max_pool_connections or lower concurrent runs` | 30 s | `RunError` -32020 (-32024 when it ends a journal write) |
| Run wall-clock | No deadline | The run goes on | Until `max_turns`, a budget or a cancel ends it | None |

A run has no wall-clock deadline. Bounds are `max_turns` and the budget, and a
tenant's server refuses an agent with neither (`run_bound`,
[Server](./server.md#what-admission-refuses-and-why)). A run that waits at the gate waits against no
clock, so a saturated provider can make queued runs wait for as long as the
saturation lasts.

Hosts should:

- Set `max_provider_concurrency` (on the agent, or as the tenant's cap) to
  what the provider key allows, so the queue is in front of the gate and not
  in the provider's rate limiter.
- Set `max_turns` and a budget on every agent.
- Enforce a deadline from the host: cancel the run (`agent.cancel`) when it is
  due; cancelling ends a wait at the gate too.
- Queue work above `max_runs` on the host side, since the server rejects it.

## Telemetry content capture

Content is left out of traces by default. `linch-otel` records the prompt, the
answers and tool input and results only when capture is on: set
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` (the switch is read
by `capture_content_from_env`, `linch-otel/src/lib.rs:35-38`; the name is in
`keys.rs:90-92`), or `RunObserver::capture_content` in a library host.

When on, these attributes carry the content: `gen_ai.input.messages`,
`gen_ai.output.messages`, `gen_ai.tool.call.arguments` and
`gen_ai.tool.call.result` ([Observability](./observability.md#content-capture)).

- `linch-otel` applies no redaction and no size cap to them.
- [`spec.redaction`](#redaction) scrubs responses and tool results before they
  are logged, so those attributes carry the scrubbed text when the host turned
  redaction on. A tool call's input is never scrubbed, so
  `gen_ai.tool.call.arguments` can carry a secret the model was given or
  invented into the call, into your traces and whatever stores them.

Keep capture off in production. When you need it, redact at the
OpenTelemetry collector (a processor that drops or masks these attributes) and
cap their size there, and treat the trace backend as holding user data.

## Related pages

- [Tools](./tools.md): permission rules, approvals.
- [Sandbox](./sandbox.md): where `Bash` runs and what each backend confines; a
  fourth guardrail, below the permission modes.
- [Agent config](./agent.md): `spec`, intercept callbacks, the run contract.
- [Structured output](./structured-output.md): the output schema and its retries.
- [Events](./events.md): `verification`, `error`, `partial_assistant`, `tool_progress`.
- [Server](./server.md): tenants and their allowlists.
- [Runs](./runs.md#acting-for-a-principal): the principal a run acts for.
- [Observability](./observability.md#content-capture): what telemetry captures.
