# Linch Studio local usage

Install Linch from this repository first, then install Studio:

```bash
python -m pip install -e '.[dev,mcp,anthropic,gemini]'
cd linch_ui
python -m pip install -e '.[dev]'
```

Build the browser UI once, then start the local editor:

```bash
cd web && npm install && npm run build && cd ..
linch-studio serve --workspace ./designs
```

`serve` publishes whatever is in `src/linch_studio/static/`; that directory is
Vite's build output, so without `npm run build` the API answers but the page is
empty. It listens only on `127.0.0.1`. A new project contains a semantic
v1alpha2 Blueprint and an independent default canvas layout. Choose one of the
shared starting points (`agent`, `goal_verified`, `directed_workflow`,
`coordinator`, or `routine`):

```bash
linch-studio new nightly-review --template routine --workspace ./designs
```

## Browser workflow

The UI opens on the project picker and presents each design across five tabs:
**design** (scope navigation, palette, canvas, inspector), **yaml**,
**diagnostics**, **files** (generated-code preview), and **export**. Design scope
is explicit: project map, agent loop, one selected directed workflow, or one
selected routine. Adding a step never silently chooses the first workflow.

The homepage also links to the bilingual **Getting Started** page at `/docs`.
It teaches the four-part product model, Tool → Agent/deep-agent attachment,
bound Subagent A2A workflows, outside Routine scheduling, and the supported
connection matrix. It also covers the runtime rails (provider, memory, MCP),
why a Skill or Subagent is not a Tool, and how goal verification, routine
verification, and `doneWhen` answer three different questions. Its three
illustrations and six clips are captured from real Studio UI flows rather than
hand-built mockups. Regenerate them after a material canvas change with:

```bash
cd web
npm run docs:capture
```

The capture command builds the current SPA, starts a real Studio server against
a temporary workspace, drives the recipes with Playwright, and rewrites the
committed 1440×900 documentation images, the six WebM clips, and each clip's
poster. Screenshots are taken with animation disabled; the clips deliberately
keep it on, because the attachment flash and the running dashed edge are what
they exist to show. Clip file names are stable, but WebM bytes are not
byte-deterministic — expect a diff on every capture. The page imports these
assets, so they are committed; a build fails if they are missing. Product
documentation owns `/docs`; FastAPI's Swagger and OpenAPI surfaces live at
`/api/docs` and `/api/openapi.json` respectively.

The YAML is the semantic source of truth. Canvas moves are written to the
separate layout file and never change the Blueprint digest, so rearranging the
canvas cannot alter what gets generated. Editing a node in the inspector, or
adding one from the palette, re-serializes the Blueprint and saves it — which
does change the digest. YAML comments do not survive a canvas edit.

Provider, memory, and MCP have no card on any canvas. They are edited in the
**project configuration rail**, which the inspector shows on the Project map
whenever no card is selected. The rail writes `spec.runtime.provider`,
`spec.capabilities.memory`, `spec.capabilities.context.memoryRecall`, and
`spec.capabilities.extensions`, and it says so after each save: it is runtime
wiring, and it never creates a graph edge. Secrets are never stored — only the
*names* of environment variables. An MCP server supplies tools and resources but
is not a Tool card; a `stdio` server's local command and args are yours to write
by hand, and AI authoring may never invent them. The memory extraction hook is a
skeleton: it exports a seam and a blocking TODO, not working extraction.

Attach project tools to the primary agent through `spec.runtime.agent.tools`.
An omitted or `null` value keeps the compatibility behavior of exposing every
declared project tool — in that state a Tool is already attached, and the
inspector says so instead of offering to attach it. A list exposes only those
referenced tool IDs, while an explicit empty list exposes no declared project
tools. Every referenced ID must exist in `spec.tools`; duplicate attachments are
rejected before export. A newly declared Tool is selected on arrival and offers
an explicit **Attach to primary agent** button; nothing is attached silently.
Attaching a Tool to a Subagent, step, or Skill widens an explicit primary
allowlist automatically, so there is no need to attach it to the primary agent
first.

### Visual composition and magnetic connectors

Whole-card magnetic attachment is available for capability relationships. Drag a
compatible card close to a highlighted port; Studio shows a ghost edge, snaps the
card on a valid drop, and commits the semantic relationship. Dropping near an
incompatible card produces red feedback and does not change the Blueprint. Flow
ordering inside a directed workflow deliberately remains handle-to-handle, so
moving a step cannot accidentally rewrite `dependsOn`.

| Scope | Gesture | Blueprint field |
| --- | --- | --- |
| Agent loop | Tool → runtime agent | `runtime.agent.tools` |
| Directed workflow | Tool → agent step | `workflow.nodes[].tools` |
| Directed workflow | Tool → subagent | `subagents[].tools` |
| Directed workflow | Subagent → agent step | `workflow.nodes[].subagent` |
| Directed workflow | Step handle → step handle | `workflow.nodes[].dependsOn` |
| Routine | Trigger → routine | `routine.triggers` |
| Routine | Routine → workflow/agent loop | `routine.workflow` or `routine.agent` |

The primary runtime and its declared subagents are members of one agent tree;
there is therefore no direct runtime-agent → subagent execution edge to create.
To express agent-to-agent work, open a directed workflow and add Subagents there.
Studio creates a bound `agent_call` step for each new Subagent, then you connect
the step handles to form the explicit A2A order. Tool cards may be attached to
the primary agent, a subagent, or an individual workflow step. Child and step
tool filters must remain subsets of the primary agent's explicit tool pool.

Select `deep_agent` or `coordinator` in the runtime inspector before attaching
tools when those execution presets are wanted. Deep agents keep their built-in
filesystem/task tools in addition to the selected declared-tool allowlist.
Coordinator workers receive the configured worker catalog while the coordinator
itself keeps its lightweight delegation surface.

For scheduled workflows, use **Scheduled workflow** from the palette. The quick
composition creates a `workflow_run` routine plus a UTC cron trigger using
`0 */2 * * *`, then opens the Routine scope. Manual, cron, CI, and webhook
triggers can also be composed there. These are exported host configuration:
Studio does not start a scheduler or own the process lifetime.

Saves are compare-and-swap against the digest the UI loaded. If the file changed
underneath (another editor, the TUI, the CLI), the save is refused and the UI
shows a conflict banner offering to reload from disk rather than clobber it.

Structurally invalid YAML stays in the editor buffer and is never written.
A semantic-invalid Blueprint saves as a draft but cannot export. Capability
badges come from `GET /api/v1/catalog`: **Runtime-ready** `[rt]`,
**Skeleton/TODO** `[td]`, **Unsupported** `[un]`. Unsupported capabilities are
listed for honesty but can never be added to a canvas.

Opening a v1alpha1 Blueprint shows a migration banner and warnings. Studio uses
the migrated v1alpha2 form in memory but does not replace the source YAML. Use
**Save migrated blueprint** only after reviewing the warnings; layout and
semantic IDs remain stable.

The global **Support** drawer is always visible. Without
`LINCH_STUDIO_*` configuration, it explains that documentation-grounded answers
and implementation recipes need a provider; known deterministic pipeline motifs
can still enter their confirmation-and-plan flow. No semantic Blueprint changes
until a reviewed proposal is explicitly accepted. For provider-backed
documentation and implementation turns, the drawer immediately shows bounded
read-only retrieval activity and a provisional answer preview, then replaces it
with the validated final response.

See [`../README.md`](../README.md) for the frontend build, dev-server, and
`api:check` commands.

## Interactive terminal workflow

The Rich terminal companion works entirely against the same local project
files, without a browser or a frontend build:

```bash
linch-studio tui --workspace ./designs
```

Inside the prompt, use `templates`, `list`, `new nightly-review routine`,
`open nightly_review`, `status`, `validate`, `preview [generated-path]`, and
`export dir <path>` or `export zip <path>`. `status` shows a project summary,
migration warnings, and an export-readiness panel. Rich follows TTY capability
and `NO_COLOR`; the TUI intentionally does not imitate the browser canvas.
`edit` opens `$EDITOR` only after a project is open. A structurally invalid
editor save is restored automatically; a semantic-invalid Blueprint remains a
local draft but cannot export.

The command-line compiler offers the same contract as the UI:

```bash
linch-studio validate ./designs/nightly_review/linch-studio.yaml
linch-studio preview ./designs/nightly_review/linch-studio.yaml
linch-studio export ./designs/nightly_review/linch-studio.yaml --out ../nightly-review
linch-studio export ./designs/nightly_review/linch-studio.yaml --zip nightly-review.zip
linch-studio schema --out studio.schema.json
linch-studio migrate ./legacy/linch-studio.yaml --out ./migrated/linch-studio.yaml
```

Export refuses an existing ZIP and every non-empty destination. The generated
directory is intentionally one-way: edit and own the Python project after
export; do not expect Studio to import it back into a graph. Migration likewise
refuses to overwrite its output path.

## Optional Support and pipeline authoring

Manual editing never needs credentials. Open **Support** to ask a documentation
question, request a static implementation recipe, or describe a pipeline. The
first two modes use a read-only `linch.Agent` over the committed SDK-docs
snapshot, audited examples, capability catalog, and an optional bounded view of
the current Blueprint. The response carries compact evidence and reports
whether the recipe is fully documented, partially documented with host-owned
TODOs, or not covered by the corpus. It cannot run code, alter files, call MCP,
or create a project.

### Live Support progress

The browser sends those provider-backed documentation and implementation turns
to `POST /api/v1/support/turns/stream`. The server emits `tool_call_start` and
`tool_call_end` events for the bounded read-only corpus calls, followed by
`response_delta` fragments from the model's final structured response and one
terminal `turn` or `error` event. The drawer shows the compact tool progress and
extracts only a direct answer or recipe title/overview for its in-flight preview;
it never renders the raw structured JSON. That preview is deliberately
provisional. Studio replaces it with the strict-schema, evidence-validated
`SupportTurnResponse` only after the turn completes.

Pipeline creation is intentionally different. An explicit build/create request
first returns a confirmation; opening a project and confirming again starts a
reviewable plan. Common CI-review, scheduled-team, and release-readiness
requests use deterministic planners. Other pipeline requests use the staged
ask → plan → build service: clarify requirements, approve the plan, then review
one candidate v1alpha2 Blueprint. In both paths, acceptance recomputes the
semantic diff and fails on a stale digest. Nothing changes the Blueprint until
you accept the proposal, and Studio never executes the generated code.

Set `LINCH_STUDIO_PROVIDER` and `LINCH_STUDIO_MODEL` together before starting
`linch-studio serve` to enable provider-backed Support and non-template
pipeline authoring. Cloud providers also require `LINCH_STUDIO_API_KEY`; local
OpenAI-compatible providers require `LINCH_STUDIO_BASE_URL`. For DeepSeek, set
`LINCH_STUDIO_PROVIDER=deepseek` with its native OpenAI-compatible base URL
(without `/anthropic`): Studio enables JSON-object mode and maps its reasoning
control automatically. The `/anthropic` compatibility endpoint is intentionally
rejected because it lacks Studio's native structured-output contract. If neither
provider nor model is set, the server still starts and the manual editor and
deterministic pipeline confirmation flow remain available. Invalid partial
configuration prevents startup and never exposes supplied values.

`LINCH_STUDIO_REASONING` controls provider effort (`off`, `low`, `medium` —
the default, or `high`). It maps to OpenAI reasoning effort, Claude adaptive
thinking plus native effort, and DeepSeek thinking/effort; the native DeepSeek
provider preserves `reasoning_content` through tool loops. Support intentionally
does not stream or retain provider reasoning. It streams only bounded retrieval
progress and non-thinking final-response fragments for the provisional preview;
the raw structured response stays hidden, while Studio renders only its
validated final result. Its per-run token budget is also disabled so
cache/reasoning accounting differences cannot abort a valid retrieval loop;
`LINCH_STUDIO_TOKEN_BUDGET` remains relevant only to the staged authoring
service.

After editing the SDK's `docs/` pages, refresh the Support corpus with
`python scripts/sync_knowledge.py` (from `linch_ui/`); a unit test fails while
the committed snapshot is stale. Do not put secret values in the Blueprint,
layouts, diagnostics, conversation messages, generated manifests, or Studio
telemetry. Use environment variable references only.
