# Linch SDK Roadmap

This file is the live roadmap for Linch as a **pure-mechanism, embeddable agent
runtime SDK**. It should guide what to build next and, just as importantly, what
not to build into core.

---

## North Star

Linch ships the **harness**, not the agent. It provides **mechanisms, protocols,
seams, and primitives**: loop control, tools, permissions, memory, context,
events, durability, scheduling, subagents, hooks, evaluation, and observability.

A coding agent is one application an embedder builds on Linch. A support agent,
research agent, data agent, operations agent, or product-specific assistant
should be equally first-class.

The discriminating rule for every roadmap item:

> Is it a mechanism every embedded agent can use, or behavior/policy specific to
> one kind of agent? Mechanism goes in Linch. Policy stays with the embedder.

When a useful capability is domain-flavored, Linch should expose the generic
seam and let the embedder supply the domain piece. For example,
`ExecutionBackend` is a Linch seam; a git-worktree coding runner is an embedder
implementation.

Hard constraints:

- Every new capability is opt-in and additive.
- With defaults unchanged, the loop stays byte-identical unless the change is a
  domain-neutral safety fix.
- The core dependency graph stays small; heavy integrations belong in examples,
  extras, or separate packages.
- Linch must remain easy to embed inside a host service. It should not become a
  daemon, dashboard, marketplace, or agent product.

---

## Open And Closed Loops

Every agent runs some version of **Discovery -> Planning -> Execution ->
Verification -> Iteration**. The key axis is **open vs closed**, defined by who
authors the path.

| | Closed loop | Open loop |
|---|---|---|
| Path author | Human or host code writes it first | Model discovers it at runtime |
| Shape | Bounded, journaled, repeatable | Exploratory, adaptive, higher variance |
| Cost | Predictable | Needs strict budget controls |
| Quality control | Eval each step | Guardrails, verifiers, loop limits |
| Linch surface | `run_workflow` | `create_deep_agent` / custom orchestration |

Linch should provide the shared control surface for both modes:

- **Budget** via `RunBudget`, shared across an agent tree.
- **Verification** via verifiers, hooks, and eval scorers.
- **Durability** via session stores, run stores, checkpoints, and workflow journals.
- **Observability** via typed events, run reports, and observers.
- **Blast-radius controls** via permissions, loop guards, timeouts, retries, and
  isolation seams.

The SDK should not bake the DPEVI cycle in as mandatory policy. It can ship
presets and examples, but the host decides the workflow.

---

## Current Baseline

Linch already has the main runtime substrate for a strong embedding kit:

- Event-driven `Agent` / `Session` loop with typed event streaming.
- Provider abstraction for OpenAI, Anthropic, Gemini, llama.cpp, vLLM, SGLang,
  and OpenAI-compatible APIs.
- Tool protocol, built-in tools, MCP tool wrapping, execution backends, scheduler,
  resource conflict handling, retries, timeouts, and background tools.
- Permission engine, durable HITL decisions, path/bash/tool rules, read-before-write,
  and MCP destructive-tool prompting.
- Memory stores, context builders, compaction, virtual filesystem, result offload,
  and memory lifecycle hooks.
- Subagents, retained worker sessions, background workers, mailbox coordination,
  scheduling primitives, and a host-called `LoopRunner`.
- Workflow journaling, run checkpoints, run reports, OpenTelemetry observer, and
  deterministic eval harness.
- Extension templates and usage docs for the main seams.

This baseline is enough. The next work should harden and clarify it rather than
add broad new agentic abstractions.

---

## What Linch Actually Needs Now

Linch should stay **thin**: an embedding kit for developers building agent
products, not the product itself. "Strong" now means the SDK makes the hard
runtime parts reliable, observable, testable, and easy to embed while refusing
to accumulate domain policy.

The roadmap should optimize for four developer outcomes:

1. **Confidence to embed.** A host app can wire Linch into an async service, run
   many tenants, stream events, pause for HITL, resume after a restart, and shut
   down without leaked work.
2. **Confidence to extend.** A developer can add a provider, tool package,
   memory backend, filesystem backend, permission policy, hook, mailbox,
   schedule store, or isolation backend without reading loop internals.
3. **Confidence to operate.** A run produces enough structured evidence to debug
   cost, latency, context pressure, permissions, failed tools, compaction,
   fallback, and verifier retries without parsing raw transcripts.
4. **Confidence to control blast radius.** Budgets, loop guards, permissions,
   isolation, retries, timeouts, and verifiers remain first-class controls, but
   their policy is supplied by the embedder.

### Active Roadmap

All eight Active Roadmap priorities are now shipped (status below). New roadmap
items should pass the acceptance test further down before being added here.

| Priority | Need | Status | Shape |
|---|---|---|---|
| 1 | **Embedding quality gates** | ✅ shipped | Expanded regression coverage: hook-failure isolation across lifecycle chokepoints, mid-tool cooperative abort → aborted terminal, 3-level budget inheritance. Existing coverage already strong for resume, concurrency, permission replay, checkpoint compatibility. |
| 2 | **Extension contract tests** | ✅ shipped | `linch.testing` ships `assert_*_contract` helpers for tools, file backends, isolation backends, mailboxes, memory stores, and schedule stores. |
| 3 | **Run diagnostics V1** | ✅ shipped | `RunReport` + `scripts/run_report.py` surface top slow / top failing tools, context-pressure tier, per-tool error counts, retry/fallback/offload counters, and total cost. |
| 4 | **Security/governance seams** | ✅ shipped | `RedactionHook` (policy-free regex scrubbing of tool results / final answer / prompt) + `examples/core/governance_redaction.py`. No default PII/PHI classifiers in core. |
| 5 | **Packaging and docs polish** | ✅ shipped | Small, opt-in extras with explicit `pip install 'linch[...]'` errors; `docs/usage/production.md` minimal production-wiring checklist; examples index refreshed. |
| 6 | **Explicit recovery knobs** | ✅ shipped | `Agent(truncation_recovery=TruncationRecovery(...))` — opt-in, bounded continuation on `max_tokens` truncation; never escalates the output cap implicitly; default byte-identical. |
| 7 | **Host-owned runner recipes** | ✅ shipped | `examples/recipes/runner_recipes.py` wraps `LoopRunner.run_once()` for cron, webhook, fixed-interval, and CI-gate triggers; lifecycle stays host-owned. |
| 8 | **External adapter examples** | ✅ shipped | `examples/integrations/redis_mailbox.py` implements the `Mailbox` protocol over Redis lists and is validated by `assert_mailbox_contract`. Adapters stay out of core. |

---

## What Linch Does Not Need Now

These items are useful for products built on Linch, but they do not belong in
core now. Add them only when a concrete, domain-neutral mechanism emerges.

| Not needed now | Why not | Where it should live |
|---|---|---|
| **Coding-agent product UX** | Todo lists, plan nudges, git conventions, PR behavior, and coding prompts are product policy. | Host app, project skill, or coding-agent package. |
| **Git worktree management in core** | Git is not universal. Linch already has the isolation seam. | External `IsolationBackend` implementation or coding example. |
| **Daemon supervisor** | Process lifetime, restart policy, backoff, deployment target, and locks are host concerns. | Host service, systemd, cron, Kubernetes, Celery, Temporal, or a small example wrapper. |
| **Hosted dashboard or web UI** | UI and storage opinions would turn the SDK into a product. | Separate app consuming events, run reports, and OTel traces. |
| **Marketplace for skills/tools** | Distribution, trust, signing, ranking, and updates are product/ecosystem policy. | External registry or package manager integration. |
| **Default PII/PHI/security classifier** | Classifiers and thresholds are compliance policy and can create false confidence. | Hook examples, verifier examples, or app-specific governance package. |
| **Large built-in tool bundles** | GitHub, Slack, kubectl, browser, DB, cloud, and SaaS SDKs would bloat dependencies. | Optional packages implementing `Tool` or MCP servers. |
| **Domain memory formats as core defaults** | `MEMORY.md`, Obsidian, AgentBrain, CRM notes, and ticket histories have different semantics. | Memory adapters over `MemoryStore` or `ContextBuilder`. |
| **Distributed queue/lease backend in core** | Redis, SQS, Postgres advisory locks, and cloud queues are deployment choices. | Optional store packages behind existing protocols. |
| **Automatic output-token escalation** | Raising caps changes cost and latency without the embedder's policy decision. | Explicit opt-in recovery option. |
| **Prompt/policy presets as defaults** | Defaults would make Linch opinionated toward one agent type. | `create_*_agent` factories, examples, or host configuration. |
| **Vendor-specific observability integrations** | OTel already gives the neutral bridge. | Vendor configuration or thin external observer package. |
| **OAuth/PKCE for every integration** | Auth flows are integration-specific and easy to overfit. | MCP/tool adapter packages unless a small common seam becomes obvious. |
| **Bounded event queues by default** | The async generator's natural backpressure is the current contract. | Optional wrapper if a host needs buffering/drop policy. |
| **Generated API reference site** | Useful polish, not a runtime need. | Documentation pass after the public surface stabilizes further. |

### Reopen Triggers

Deferred ideas can come back when one of these is true:

- Two or more unrelated embedders need the same mechanism and cannot implement it cleanly
  through an existing seam.
- The feature can be expressed as a small protocol, hook, event, report field, or opt-in
  config without adding policy.
- The feature reduces core complexity or risk rather than adding another orchestration layer.
- The feature can be tested deterministically without live services.
- The feature keeps default behavior unchanged.

If a proposal fails these triggers, keep it outside core.

---

## Acceptance Test For New Roadmap Items

A roadmap item belongs here only if the answer is "yes" to all of these:

1. Does it help many kinds of embedded agents, not just coding agents?
2. Can it be expressed as a protocol, hook, store, event, report, example, or opt-in
   factory rather than a default behavior?
3. Does it keep `Agent()` defaults byte-identical or observably safer in a
   domain-neutral way?
4. Can an embedder replace or disable it without forking Linch?
5. Can it be verified with deterministic tests, usually without a live provider?

If an idea fails this test, it should live in an example, external package, host
app, or project skill instead of the SDK core.

---

## Sequencing

The practical order is:

1. **Protect the current runtime.** Regression tests, cleanup, compatibility,
   cancellation, resume, and concurrency guarantees.
2. **Make extension safer.** Compliance helpers, templates, and clearer docs for
   implementers.
3. **Improve diagnostics.** Reports and summaries that make production failures
   explainable.
4. **Add only opt-in recovery.** Recovery features that change cost, duration, or
   model behavior require explicit knobs.
5. **Keep adapters outside core.** Use examples and optional packages to prove
   protocols without bloating `linch`.

This keeps Linch strong where an embedding kit must be strong: runtime
correctness, extension seams, operational evidence, and blast-radius controls.

---

## Docs

Topic-split usage guide under `docs/usage/` covers agent configuration,
providers, events, tools, structured output, hooks, context and memory,
filesystem, workflows, deep-agent, coordination, loop runner, skills, extending,
and examples. `docs/usage.md` is retained as a redirect index.
