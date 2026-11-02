# Shipped Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Ship local, greppable docs and examples with Linch, keeping imports and generated API reference checked.

**Architecture:** Hatch includes the maintained docs and examples under `linch/_docs`. The stdlib scaffold CLI locates that tree, with a checkout fallback. Static AST-based generation and fast documentation tests maintain the published contract.

**Tech Stack:** Python 3.11+, stdlib, existing hatchling backend and pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-shipped-docs.md`

## Global Constraints

- `src/`: under 80 lines, for the CLI command and path resolution.
- Generator: under 250 lines.
- No runtime behavior change. Provider, loop, and session code are untouched.
- No new dependency. No changes to `linch.__all__`.
- Implement WP1 to WP4 only, each in a separate commit in order on `docs/shipped-docs`.
- Write tests first and confirm they fail before implementation.
- No version bump, release-heading edits, push, or tag. Add entries under `## Unreleased`.
- Leave unsupported core imports unchanged and record every path and doc location in an explicit test list named unresolved.
- Doc tests must stay fast and need no live service or optional extra.
- Omit and report any fact that cannot be confirmed in code. Stop and report any item that cannot hold as written.
- Gate environment: `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin` first on PATH, `PYTHONPATH=src`.
- Each commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` then `Claude-Session: https://claude.ai/code/session_0122wtPsbyKSBb8eF3cuHvm8`.

## Review Focus

- Installed CLI must find packaged data independently of cwd, environment PYTHONPATH, and the checkout.
- Editable installs must find checkout docs when hatch omits force-included data.
- Markdown relative links, including references and HTML links, must resolve in the shipped tree.
- Static API resolution must follow aliases, conditional imports, re-exports, and extension exports without loading optional integrations.
- Python fences must compile with top-level await supported, and unresolved private imports must be narrowly recorded rather than broadly permitted.

### Task 1: WP1 — Package docs and expose their path

**Files:** `pyproject.toml`, `src/linch/ext/scaffold/__init__.py`, `tests/docs/`, shipped Markdown with escaping links, `docs/usage/cli.md`, `CHANGELOG.md`.

**Interfaces:** Produce a reusable shipped-file inventory in tests and `linch docs path`, printing the absolute directory containing `docs/` and `examples/`.

- [x] Add failing tests for CLI packaged-first/check-out fallback/missing-data behavior and shipped relative links. Add a clean wheel-install test that skips clearly only if the backend is absent, and avoid rebuilding on every normal fast doc test run.
- [x] Run tests and retain failing output before implementation.
- [x] Build a baseline wheel before packaging changes; record compressed bytes. Use available cached build tools if possible without adding a project dependency. Report actual environment limitations.
- [x] Configure filtered Hatch file selection and path remapping for exactly the WP1 layout. Inspect wheel and sdist, excluding planning docs, caches, and private state.
- [x] Implement the small stdlib-only CLI addition, packaged-first then checkout resolution; make missing docs nonzero and actionable.
- [x] Fix relative links escaping the shipped tree using existing verifiable immutable GitHub references. The spec requires tag-pinned URLs: report this item if no tag can be verified; do not invent a tag.
- [x] Actually install a wheel in an external clean venv and exercise the CLI if the environment permits. Actually verify editable fallback. Record skips accurately.
- [x] Run focused tests and style/type checks, append WP1 Unreleased entry, obtain task review, then commit WP1 only (including this plan).

### Task 2: WP2 — Generate the API reference

**Files:** `scripts/generate_api_reference.py`, `docs/usage/api-reference.md`, `tests/docs/test_api_reference.py`, `CHANGELOG.md`. Source docstrings only if truly missing and within constraints.

**Interfaces:** A deterministic static generator with `--check`; one `###` heading per root export and each explicit extension export, with exact import, kind, signature, first docstring line, and usage link when available.

- [x] Write and run failing coverage/staleness/static-resolution tests before the generator exists.
- [x] Implement under 250 lines, following `generate_event_catalog.py`. Resolve source AST rather than importing optional integrations. Preserve alias names and handle conditional re-exports.
- [x] Print a missing-docstring work list. Add confirmed source docstrings only when permitted; report contradictions instead of violating protected source/budget constraints. Constants and type aliases must be described honestly rather than borrowing unrelated builtin docstrings.
- [x] Generate and commit the output; verify every expected export, deterministic check behavior, and representative exact signatures/imports/docstrings.
- [x] Add the reference index link and Unreleased entry. Run focused checks, obtain task review, and commit WP2.

### Task 3: WP3 — Enforce the docs import contract

**Files:** shipped Markdown, `docs/versioning.md`, `tests/docs/test_doc_imports.py`, quickstart offline markers, `CHANGELOG.md`.

**Interfaces:** Python-fence parser over every shipped Markdown page; explicit `unresolved` list of names with file/line context; supported optional module list in versioning.

- [x] Add and run failing tests for imports in every Python fence and compilation, plus execution of explicitly offline quickstart blocks.
- [x] For names already in `linch.__all__`, change imports to top-level. Preserve aliases and mixed-import semantics; never add exports.
- [x] Confirm optional-extra module imports from code and declare only those paths supported in versioning. Extensions remain explicit `linch.ext.*` imports.
- [x] Leave every other import line unchanged and list its qualified name and precise doc location in the temporary explicit `unresolved` test list. Reject new unresolved paths and stale exceptions.
- [x] Make existing incomplete Python blocks compile with minimal syntactic placeholders/top-level await handling; preserve page structure and meaning. Mark and run the existing quickstart offline example with no credentials or optional extras.
- [x] Regenerate WP2 output if usage links depend on updated imports. Run focused checks, add Unreleased entry, obtain review, commit WP3, and report all unresolved names.

### Task 4: WP4 — Make pages greppable

**Files:** `docs/usage/*.md`, `tests/docs/test_troubleshooting.py`, `CHANGELOG.md`, generated reference if required.

**Interfaces:** Each usage page opens with a use-case sentence; headings use exact symbol names; troubleshooting entries contain exact source error text, cause, and fix.

- [x] Write and run failing tests for the troubleshooting message/source contract and usage opening lines before edits.
- [x] Add one opening sentence per usage page and adjust headings with exact code-backed symbol names. Do not restructure or rewrite pages. Keep anchors working.
- [x] Draft troubleshooting from actual raise sites for agent ConfigError, tool contracts, provider auth, and run contract mismatch, then trim by hand. Quote exact existing source text and confirm every cause/fix from code.
- [x] Keep quote/source drift checks fast and offline, including negative coverage showing fabricated messages are rejected. Regenerate API reference if needed.
- [ ] Add Unreleased entry. Run focused checks, full `pytest -q`, `ruff check . && ruff format --check .`, and `pyright` in the required environment. Obtain task and whole-branch review; commit WP4 with all fixes scoped to WP4.
- [ ] Measure final wheel size and added source/generator lines; report four commits, gate counts, packaging checks/skips, all unresolved imports/unconfirmed facts/unfinished items. Do not run section 5.
