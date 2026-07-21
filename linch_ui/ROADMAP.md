# Linch Studio Roadmap

This file tracks work after the `studio.linch.dev/v1alpha2` foundation. Keep it honest: an item
is complete only when its generated code or visible TODO seam and its acceptance test both exist.

## Shipped in v1alpha2

- [x] Independent runtime preset, directed workflow, completion gate, and routine concepts.
- [x] Strict v1alpha2 schema plus in-memory v1alpha1 migration without overwriting source YAML.
- [x] Migration CLI with no-overwrite output and explicit dropped-worker-limit acknowledgement.
- [x] Deterministic text, JSON Schema, and blocking custom-TODO verifier generation.
- [x] Root-only completion hook; child subagent sessions are not gated.
- [x] Generated verifier tests for pass, retry, retry exhaustion, exception-stop, malformed JSON,
  invalid schema, and root-versus-child behavior.
- [x] Directed workflow generation with stable ordering, dependencies, phases, fan-out/fan-in,
  tool filters, shared budgets, and public `Agent.run_workflow` integration.
- [x] Primary-agent tool allowlists with compatibility-safe `null` semantics, exact empty-list
  semantics, deep/coordinator runtime filtering, reference validation, and generated tests.
- [x] Workflow journal/replay identity includes the effective tool filter, including tools inherited
  from subagent frontmatter, so differently constrained calls cannot replay each other.
- [x] Coordinator worker catalog isolation: the coordinator retains its delegation surface while
  workers receive the configured implementation-tool catalog.
- [x] One-shot agent-tick and workflow-run routines with host-owned process lifetime.
- [x] Full trigger configuration preservation and blocking webhook-signature TODO seams.
- [x] v1alpha2 capability catalog revision 2 and versioned editable relation matrix.
- [x] Per-component generated documentation for tools, subagents, workflows, verifiers, and
  routines.
- [x] Shared `agent`, `goal_verified`, `directed_workflow`, `coordinator`, and `routine`
  templates across CLI, TUI, API, web, and authoring.
- [x] Rich TUI with project summary, migration warnings, and export-readiness reporting.
- [x] Scoped web canvases for project map, agent loop, directed workflows, and routines.
- [x] Semantic connectors, cycle/reference checks, edge selection/deletion/undo, migration banner,
  and sectioned inspector fields.
- [x] Whole-card magnetic capability attachment with compatible-port glow, ghost preview, snap,
  invalid-target feedback, semantic rollback on save failure, and backend relation authority.
- [x] Visual Tool → runtime/deep-agent composition, bound Subagent → workflow-step creation,
  handle-only A2A `dependsOn`, and Tool → subagent/step attachment.
- [x] Scheduled-workflow quick composition plus manual, cron, CI, and webhook trigger palette items;
  the Routine scope owns Trigger → Routine → Target configuration outside the workflow graph.
- [x] Bilingual in-app Getting Started page at `/docs`, linked from Home and the editor, with
  step-by-step Tool, deep-agent, A2A, and scheduled-workflow recipes plus real Playwright-captured
  screenshots and a reproducible `docs:capture` command.
- [x] Generated OpenAPI client contract, deterministic frontend emit fixtures, and one-way export
  boundaries.
- [x] Conversational AI authoring: staged ask → plan → build turn protocol over a client-held
  transcript (`POST …/authoring/turns` with a `stage` field), option-backed clarifying questions
  with a UI-owned free-form answer, a user-approved plan gate before any build, a `linch.Agent`
  design agent with two read-only knowledge tools over a committed, staleness-tested SDK-docs
  snapshot plus the live catalog, compile dry-run verification without execution, proposal
  summaries, display-only untruncated reasoning traces (`LINCH_STUDIO_REASONING`), a chat drawer
  with in-place proposal cards and value-carrying diffs, and deterministic canvas placement of
  accepted workflow nodes.
- [x] Live, Claude-Code-style tool-call visibility in the chat drawer: each knowledge-tool call
  streams as a start/end notice over `POST …/authoring/turns/stream`, renders as one compact
  plain-text line while the turn is in flight and once the turn lands, and expands in place for
  the full bounded result — no separate panel, no emoji.
- [x] Global Support drawer for documentation-grounded answers, static implementation recipes,
  and explicit pipeline creation. Support has bounded read-only retrieval, evidence coverage,
  no filesystem/MCP/subagents, no run token budget, and a confirmation → plan → proposal path
  that keeps deterministic common pipeline motifs provider-independent while preserving
  digest-checked user acceptance.

## P0 — Release hardening

- [ ] Add generated journal/replay tests using a durable run store, including fan-out/fan-in replay
  and delivery-ID behavior. A trigger delivery ID must become `run_id` only for durable stores.
- [ ] Add focused React component tests for transient magnetic glow/ghost animation and edge
  keyboard selection. Relation geometry, invalid targets, semantic persistence, and complete browser
  gestures already have model and Playwright coverage.
- [ ] Add the manual-versus-AI canonical-byte equivalence test for all five templates, including
  proposal acceptance and stale CAS behavior.
- [ ] Add dedicated project-level inspector rails for provider, memory, MCP, hooks, permissions,
  loop guard, persistence, and observability. These must remain configuration rails, never
  control-flow nodes.
- [ ] Add explicit UI acknowledgement state for every `requiresConfirmation` migration warning and
  preserve that acknowledgement in the save audit trail.
- [ ] Add a screenshot-freshness CI job that regenerates the in-app documentation assets and fails
  when a material canvas change leaves committed walkthrough images stale.

## P1 — Authoring and lifecycle depth

- [ ] Replace raw JSON text areas for verifier lists with discriminated-union editors for
  `text_contains`, `json_schema`, and `custom_todo`.
- [ ] Add structured editors for tool resources, permission rules, budgets, webhook environment
  references, and routine `verify`/`doneWhen` fields.
- [ ] Add one-click CI and webhook *routine presets* beyond the existing trigger cards, including
  security review copy and complete host-integration examples.
- [ ] Add workflow phase lanes, fan-out/fan-in visual grouping, and replay/journal inspection without
  implying that Studio executes the workflow.
- [ ] Add keyboard-first connector composition, reduced-motion equivalents, and screen-reader
  announcements for the shipped magnetic attachment cards.
- [ ] Add migration preview/diff before materializing v1alpha2 YAML from the web and TUI.
- [ ] Add template/version metadata so future schema revisions can migrate template instances
  independently from hand-authored projects.
- [ ] Add scope-aware help links, template comparison cards, and non-blocking first-run callouts that
  deep-link to the relevant section of the shipped Getting Started page.

## P2 — Scale and polish

- [ ] Split the web bundle by editor surface and lazy-load CodeMirror and React Flow.
- [ ] Add visual-regression snapshots for light/dark themes, Vietnamese copy, narrow inspectors,
  large workflows, and reduced-motion mode.
- [ ] Add property-based schema/migration tests for identifier collisions, reference preservation,
  and layout-key stability.
- [ ] Add large-graph performance budgets for layout, relation validation, YAML emission, and
  deterministic ZIP generation.
- [ ] Publish a versioned migration guide and generated-project upgrade guide for each future API
  version.

## Permanent product boundaries

- Studio does not run or deploy generated projects.
- Studio does not own daemons, schedulers, process lifetime, or secrets.
- Studio does not infer a graph from Python.
- Studio never overwrites an exported project or imports it back as a Blueprint.
- Unsupported catalog capabilities stay non-exportable; skeleton/TODO capabilities must emit a
  visible file, symbol, and blocking TODO.

## Last verified release gate — 2026-07-18

- Studio: 225 pytest tests; Ruff check/format and Pyright all clean.
- Generated projects: all 11 golden Blueprints pass Ruff check/format, `compileall`, pytest, and
  Pyright after export to fresh directories.
- Web: 106 Vitest tests and 28 Playwright tests pass; OpenAPI drift, TypeScript, and production build
  are clean. `npm run docs:capture` re-captures 3 screenshots and 6 clips from real Studio flows.
- Linch SDK: 1,100 pytest tests pass and 19 skip; Ruff check/format and Pyright are clean for
  `src/` and `tests/`.

LocalBackend/BashTool tests spawn real shell processes and therefore need a subprocess-enabled test
environment. They pass outside restricted execution sandboxes.

## Acceptance commands

From `linch_ui/`:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
cd web
npm run typecheck
npm test
PATH=../.venv/bin:$PATH npm run api:check
npm run build
npx playwright test
```

For every compiler golden, also export to a fresh directory and run Ruff check/format, Pyright,
`compileall`, and generated pytest. Never use an existing export directory as the target.
