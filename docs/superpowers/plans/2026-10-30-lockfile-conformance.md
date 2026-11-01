# Lockfile Conformance Prerequisite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the checked-in lock reproduce the SDK dependencies already declared in pyproject.toml before reviewed 3.0 feature releases.
**Architecture:** Regenerate only uv.lock from the existing manifest. This changes no runtime policy, dependency floor, public API or package version.
**Tech Stack:** Python>=3.10, uv, pytest, Ruff, Pyright.
**Spec:** ../specs/2026-10-29-sdk-integration-scope.md (feature release and verification gates).

## Global Constraints

- Python >=3.10, async runtime/provider paths, duck-typed protocols.
- Change only uv.lock in this task. Do not change pyproject.toml, source, tests, docs/ROADMAP.md, plans/, or openhuman/.
- No global package installs; caches and verification environments stay under /tmp.
- Public additions are deliberate through linch.__all__ and public API tests. This task adds none.
- Preserve the existing declared core/optional dependency ranges and package version2.0.0.
- Do not stage another worker's documents or operational artifacts.
- No push, tag, PR merge or publish in this implementation task.

## Review Focus

- MCP1.x cannot be admitted when the manifest requires mcp>=2.0.0; locked-environment import metadata and real MCP unit tests must prove this.
- Moving jsonschema from dev-only to core must be reflected in lock metadata; lock consistency is verified by uv lock --check.
- Python3.10 must remain resolvable; create the locked test environment with /usr/bin/python3.
- Optional extras must resolve together; use --all-extras and uv pip check rather than inspecting only the base lock package.
- Existing draft documents must not enter the dependency commit; git show --stat and git diff --name-only must show only uv.lock.

---

### Task 1: Align the lock with the declared SDK dependencies

**Files:**
- Modify: uv.lock
- Verify: pyproject.toml (read-only), existing tests/ and package metadata

**Interfaces:**
- Consumes: existing pyproject.toml dependency ranges and uv lock format.
- Produces: a uv.lock accepted by uv lock --check and a Python3.10 all-extras environment reproducing those ranges. No API changes.

- [ ] **Step 1: Record the failing existing verifier before editing**

Run from the managed integration worktree:
```bash
UV_CACHE_DIR=/tmp/linch-3-uv-cache uv lock --check
```
Expected: nonzero with lockfile-needs-update diagnostic. Also record the pre-edit pyproject.toml digest and git status. The known stale lock admits MCP1.27.2 and puts jsonschema in dev-only metadata.

- [ ] **Step 2: Regenerate only the lock**

Run uv lock against the unchanged manifest, reusing valid locked versions where possible. Use an authorized network context if sandbox DNS prevents resolution. Do not alter dependency floors to obtain a solution.

- [ ] **Step 3: Verify locked resolution and manifests**

Run uv lock --check and create/sync a disposable Python3.10 environment at /tmp/linch-3-locked with --all-extras --locked --no-install-project. Use uv pip check on it. Assert installed MCP major version>=2 and jsonschema major version4 using importlib.metadata/packaging. Verify pyproject.toml digest unchanged and only uv.lock differs among tracked files.

- [ ] **Step 4: Run the current suite and standard checks**

Use the locked environment and /usr/bin/rg first in PATH, setting pytest cache under /tmp. Run the full pytest suite once with --timeout=60 --timeout-method=thread, Ruff lint and format check, and Pyright with Node22 when needed. Record exact commands/results, including baseline missing-jsonschema-source warnings (0 errors/3 warnings); do not suppress them in source. Failure is reported by name; do not make unrelated source fixes.

- [ ] **Step 5: Self-review and commit**

Review the lock diff for unintended manifest/version changes or unrelated paths. Stage uv.lock only and commit:
```bash
git add uv.lock
git commit -m "build: align lockfile with declared SDK dependencies"
```
Write the implementation report with RED/GREEN verifier evidence, locked package versions, full-check outputs and commit SHA. Do not publish anything. Independent spec/code-quality review follows.
