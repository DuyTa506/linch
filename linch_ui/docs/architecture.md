# Linch Studio architecture

Linch Studio is a local-first design and export application. It does not run,
deploy, schedule, or observe a production agent. Its persistent project state is
plain files under a user-selected local workspace:

```text
designs/<project>/
├── linch-studio.yaml          # semantic Blueprint
└── .linch-studio/layout.json  # canvas-only layout
```

The Blueprint is the complete design contract. Layout is intentionally separate,
does not affect its digest, and never changes generated output.

```text
React workspace ── HTTP ── FastAPI ── strict v1alpha2 Blueprint ── compiler ── preview/export
                                         │
                                         └── optional bounded AI proposal service
```

## Product model

Studio keeps four concerns independent:

- An **agent loop** is the model-tool cycle. Its preset is `standard_agent`,
  `deep_agent`, or `coordinator`.
- A **directed workflow** is a code-owned DAG with journal/replay behavior.
- **Goal-verified completion** feeds a verifier verdict back to the root agent
  and is the only behavior described as closed-loop.
- A **routine** wraps one bounded agent tick or workflow invocation in a
  manual, cron, CI, or webhook trigger. The host owns process lifetime.

The browser projects this contract through separate scopes. The project map
shows runtime, workflow, and routine ownership; the agent-loop scope shows
capabilities and guardrails; each workflow has its own control-flow graph; and
each routine shows trigger to routine to target. Provider, memory, MCP, hooks,
permissions, budget, persistence, and observability remain inspector settings,
not control-flow nodes.

`runtime.agent.tools` is the primary runtime's declared-tool policy. Omitting
it or setting it to `null` registers every top-level `spec.tools` declaration;
an explicit list is an allow-list, and `[]` registers none of those declared
tools. Capability-provided tools and deep-agent coordination primitives are
separate runtime features. Workflow and subagent filters can only narrow the
tools present in the primary registry; they cannot restore an excluded tool.

## Why the local backend is Python

The frontend owns high-frequency interaction: graph movement, selection, YAML
editing, and layout. The backend handles bounded YAML validation, deterministic
in-memory code generation, local file compare-and-swap writes, and optional
provider requests. These are not throughput bottlenecks in a local single-user
application.

Python keeps Studio on the same public, semver-governed `linch` API used by the
generated projects. A Rust backend would otherwise need to duplicate the strict
Blueprint/compiler contract or add an RPC/FFI bridge back to the Python Linch
runtime. The API boundary is intentionally narrow, so a future profiler-proven
CPU-bound component can be replaced without changing the editor contract.

## Safety invariants

- YAML is size/depth bounded and rejects aliases, custom tags, duplicate keys,
  and unsafe identifiers.
- Blueprint writes use the canonical digest as a compare-and-swap token. Stale
  writes return `409`; the server never silently merges them.
- A v1alpha1 file is migrated only in memory. The original YAML and layout stay
  untouched until the user explicitly saves or runs `linch-studio migrate` to
  a new path after reviewing migration warnings.
- Structural-invalid YAML remains in the editor buffer only. Semantic-invalid
  Blueprints can be drafts but cannot export.
- Layout writes are independently validated and cannot alter the Blueprint
  digest.
- Exports are rendered and parsed in memory before a no-overwrite directory or
  deterministic ZIP write. No generated code is imported, installed, or run.
- Support and pipeline authoring have no filesystem, MCP, skills, or subagents.
  Documentation and implementation requests use only bounded read-only corpus
  tools and return static answers or recipes. A pipeline request must cross an
  explicit confirmation, plan approval, and digest-checked proposal acceptance
  before it can change the semantic Blueprint. Neither path can add executable
  verifier code, MCP commands, secrets, or dangerous permissions.

## Support retrieval and pipeline authoring

The global Support drawer posts a client-held transcript to
`POST /api/v1/support/turns`. For documentation and implementation requests it
constructs a per-turn `linch.Agent` over the committed SDK-documentation
snapshot, audited examples, live catalog, and (when a project is open) a
bounded read-only Blueprint view. The agent can search or read the corpus
iteratively, but has a small retrieval cap and no mutation tools. A strict
response schema separates a direct answer from a static implementation recipe;
each keeps compact corpus evidence, reports whether the request is fully
documented, and leaves host-owned seams as TODOs rather than inventing runtime
behavior. Provider reasoning, system prompts, and raw tool traces are never
returned by Support.

Pipeline creation is a separate, explicit mode. Routing requires an explicit
creation intent (or a user-selected mode), then the server returns a
confirmation rather than a Blueprint. Recognized CI-review, scheduled-team,
and release-readiness motifs use deterministic planners and builders. Other
requirements continue through the staged ask → plan → build authoring service:
questions and a plan precede one candidate Blueprint, and the build request
carries the approved plan digest. In every case, manual-only guards, schema and
semantic validation, and a pure compile dry-run run before a draft is stored.
The server recomputes the semantic diff and accepts the proposal only with the
current Blueprint digest; it never executes generated code or silently writes
the accepted design.
