# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Linch Studio** is a local-first visual blueprint editor and deterministic skeleton
exporter for the Linch SDK in the parent directory. It is a *separate package*
(`linch-studio`) nested inside the `agent_kit` repo — the root `../CLAUDE.md` governs the
`linch` SDK and does not cover Studio.

```text
designs/<project>/linch-studio.yaml   → strict v1alpha2 Blueprint (the design contract)
designs/<project>/.linch-studio/layout.json → canvas-only, never affects generated output

React workspace ── HTTP ── FastAPI ── Blueprint ── compiler ── preview/export
```

Studio **never runs, deploys, schedules, or imports back** what it generates. Export is
one-way. Read `docs/architecture.md` (product model, safety invariants) and `docs/usage.md`
(local operation) before editing a subsystem; `ROADMAP.md` tracks release gates and the
permanent product boundaries.

## Commands

Studio depends on the parent SDK, so install Linch first. The repo's venv is `.venv/`.

```bash
python -m pip install -e '../[dev,mcp,anthropic,gemini]'
python -m pip install -e '.[dev]'

pytest -q                                 # ~195 tests, a few seconds
pytest tests/unit/test_compiler.py -k golden   # one test
ruff check src tests && ruff format --check src tests
pyright
python scripts/sync_knowledge.py          # refresh the AI agent's SDK-docs snapshot after ../docs changes
```

Frontend (from `web/`; Node 20 in CI):

```bash
npm install
npm run build      # typecheck + vite build INTO ../src/linch_studio/static/
npm test           # vitest unit
npm run e2e        # playwright; boots a real `linch-studio serve` against the build
npm run dev        # :5173, proxies /api to a `serve` on :8765 you start yourself
npm run api:check  # fails on OpenAPI/TS drift (CI gate)
npm run fixtures:emit   # regenerate cross-language digest fixtures
```

`serve` publishes only what is in `src/linch_studio/static/` (gitignored Vite output,
shipped in wheels via `[tool.hatch.build] artifacts`). Without `npm run build` the API
answers and the page is blank — and **e2e tests a stale build**, not your source.

CI (`../.github/workflows/ci.yml`) runs Studio lint/pyright, pytest on 3.10–3.13, plus
`api:check` → build → vitest → playwright.

## Architecture

### The canonical digest is the spine

`spec/canonical.py` — `SHA-256(json.dumps(model_dump(by_alias=True, exclude_none=False),
sort_keys=True, separators=(",",":")))`. Everything keys off it: compare-and-swap saves,
AI proposal staleness, the export manifest, the semantic diff. Two consequences:

- YAML formatting and layout **cannot** change the digest (layout is deliberately not in
  the model).
- Because defaults are included, **adding a defaulted field to `spec/models.py` changes the
  digest of every blueprint on disk** — stale-digest 409s for clients, permanently
  unacceptable stored proposals. Nothing versions the digest.

`spec/models.py:StudioModel` sets the whole contract: `extra="forbid"`, `frozen`, `strict`,
camelCase aliases. **Python is snake_case; the wire/YAML form is camelCase**, and that
boundary is crossed independently in three places (`spec/models.py`, `server/models.py`,
and `loader.py:_wire_location`).

### Layers

- **`spec/`** — `loader.py` enforces YAML bounds in three passes (preflight token scan
  rejecting aliases/tags/multi-doc, a duplicate-key loader, a post-construction tree walk
  that re-runs *after* migration since a migrator can grow the tree). `migrations.py` does
  in-memory v1alpha1→v1alpha2 only; it never invents working verification (a legacy
  `domainVerifier` becomes a *blocking* `custom_todo`) and drops per-worker limits with a
  `requires_confirmation` warning that **only the CLI gates on**.
- **`compiler/`** — `validate → normalize to CompilerIR → each pack contributes files →
  finalize`. `CompilerIR` is a frozen, fully-detached snapshot (primitives only; nested
  config crosses as pre-serialized JSON) so packs cannot reach back into the spec model.
  Packs are duck-typed (`capability_id` + `contribute(ir)`); the tuple in `compiler/api.py`
  is the entire registry and its order is irrelevant (contributions sort by path).
  `export.py` AST-parses every generated `.py` before writing, stages to a tempdir, and
  refuses overwrite via `O_EXCL|O_NOFOLLOW` / `os.link`; ZIPs are byte-deterministic.
- **`catalog/`** — hand-authored, zero runtime introspection. Three tiers: `runtime_ready`,
  `skeleton_todo` (must emit a visible file, symbol, and blocking TODO), `unsupported`.
  **`export_allowed` on the catalog is a UI label, not an enforcement point** — the real
  block is `has_errors(validate_blueprint(...))`, so an `unsupported` record without a
  matching `semantic.unsupported_capability` error in `spec/validation.py` badges red and
  still exports.
- **`authoring/`** — the AI design agent is a stateless `linch.Agent` whose only
  capabilities are two read-only knowledge tools (`search_docs`/`read_section` over the
  committed snapshot in `authoring/knowledge/`; skills/subagents/mcp/filesystem all off,
  `max_turns=12`). Conversations are staged ask → plan → build turns over a client-held
  transcript (`converse(..., stage=)`); in the `chat` stage the first turn must return
  option-backed clarifying questions and later turns a plan, never a blueprint
  (verifier-enforced), while the `build` stage — entered when the user approves the plan —
  must return one whole candidate Blueprint that passes validation, the guards, a compile
  dry-run (no execution), and a digest-checked accept. Provider reasoning is captured as a
  display-only, untruncated `thinking` field (`LINCH_STUDIO_REASONING`) and also streams
  token-by-token over the SSE variant `POST …/authoring/turns/stream` — the drawer's
  default path; dropping the stream cancels the run. `guards.py` lists
  what AI may never touch even though manual editing can (redaction regexes, MCP stdio
  commands, trusted permissions, executable verifier entry points); the server re-runs
  those guards itself.
- **`server/`** — `FileProjectStore` does digest CAS under a *process-local* lock (two
  `serve` processes on one workspace would race). Path handling rejects symlinks and
  escapes; FastAPI's default `RequestValidationError` body is replaced because it echoes
  rejected input.
- **`cli.py` / `tui.py`** — no separate compile path exists. CLI, TUI, and server all import
  the same `compile_file`/`export_directory`/`export_zip` and render the same
  `CompiledProject.preview()`.

### Frontend (`web/`)

`useStudio(api)` is the entire store — one state object, prop-drilled; no Redux/context.
**The server is the authority.** Every canvas/inspector/palette edit is
`structuredClone → new Blueprint → emitBlueprintYaml → PUT with baseDigest → response
replaces state`. There is no local commit step; an Inspector `onBlur` writes to disk. The
frontend never parses YAML — only the server does.

Layout is a second, independent channel: debounced `PUT /layout` that **must never touch
the digest** (e2e-asserted).

`SaveState` distinguishes two things that must not be conflated: **`draft`** = semantically
invalid but *written to disk* (export blocked), **`buffer`** = structurally invalid, refused
by the server, memory only. Don't "fix" the draft path to block saving.

`model/graph.ts` is the heart: `blueprintToGraph()` stamps each node with its **JSON
pointer**, which is how the Inspector and delete write back. **Scopes** are the central
navigation concept — the canvas never shows the whole blueprint (project map / agent loop /
one workflow / one routine). Provider, memory, MCP, hooks, permissions, and budget are
Inspector rails, never control-flow nodes.

## Trip hazards

- **Two disjoint capability-ID namespaces.** `FileContribution.capability_id`
  (`docs.readme`, `project.core`, …) is *not* a catalog ID. Only `ir.py:_selected_capabilities`
  emits real catalog IDs, and `badges_for_selection` raises `KeyError` on an unknown one.
  Nothing tests that `_selected_capabilities` stays in sync with the catalog — the most
  likely silent break when adding a capability.
- **Diagnostics may never carry the rejected value.** Enforced in four places (`Diagnostic`
  has no value field, `include_input=False`, the custom validation handler,
  `ProposalTelemetry`'s aversion to free-form fields). A "helpful" message interpolating the
  bad value breaks the boundary.
- **Connections pass three gates**: the backend relation matrix ∧ the hardcoded frontend
  `SUPPORTED_RELATION_IDS` ∧ `connectSemanticRelation(...).connected`. Adding a relation to
  the matrix does nothing until the frontend allowlist and a `connect*` branch exist.
- **`runtime.agent.tools` null-vs-`[]` is semantic**, not cosmetic: null/omitted = every
  declared tool, a list = exact allowlist, `[]` = none. See `docs/architecture.md`.
- **Canvas node ids are not blueprint ids** — slugified layout keys, two deliberately legacy
  (`primary_agent`, `loop_<id>`). Renaming them would jump every saved canvas.
- **`types.ts`'s `Blueprint` is hand-written** (OpenAPI can't express the discriminated
  unions) and has no drift guard except `tests/api/test_emitter_contract.py`, which asserts
  the TS emitter and the Pydantic spec agree on the canonical digest — and which **silently
  skips if you forget `npm run fixtures:emit`**. `schema.ts` and `openapi.json` are
  generated; never hand-edit.
- **YAML comments do not survive** a canvas/inspector edit.

## Adding a capability or node type

These must change together: `spec/models.py` → `spec/validation.py` (cross-refs, plus a
skeleton warning or an unsupported *error*) → `compiler/ir.py` (carry into IR **and**
`_selected_capabilities`) → a pack in `compiler/packs/` (register in `compiler/api.py`) →
`catalog/registry.py` (a record, or it has no badge; bump the catalog revision) →
`scripts/dump_openapi.py` to regenerate `web/openapi.json` and the catalog fixture →
`tests/golden/*.yaml` to pick up the compile-everything test.

Every golden blueprint must export to a **fresh** directory and pass Ruff, Pyright,
`compileall`, and its generated pytest.

## Style

Ruff owns formatting (line length 100, py310 target). Match surrounding code; don't refactor
unrelated code in the same change. The parent repo's rules under `../.claude/rules/` (TDD
workflow, surgical changes, docstring style) apply here too.
