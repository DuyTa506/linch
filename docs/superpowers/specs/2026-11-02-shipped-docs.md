# Shipped docs

Status: proposed, not implemented. Base: `master` at `7581ae6` (3.0.1).

## 1. Goal

A coding agent with only `pip install linch` can build a working Linch agent from the docs
installed with the package, using grep and file reads. No repository clone, no extra tool, and
no network.

The skill `build-agent-with-linch` comes after this work and has its own spec. It depends on the
docs paths and the `linch docs path` command defined here.

## 2. Current gaps

Measured on 2026-11-02:

- The wheel packages only `src/linch`, so an installed copy has no docs or examples.
- 113 of 272 names in `linch.__all__` do not appear anywhere in `docs/usage`.
- Usage pages import from paths `docs/versioning.md` calls private: `linch.config`,
  `linch.sessions`, `linch.providers`, `linch.tools.registry`, `linch.types`, and others. Some
  of these names may be missing from `linch.__all__`, which is a contract gap, not a docs typo.
- `docs/usage` is 23 files and about 32,000 words, too long to read whole in one task.
- No test keeps docs imports or relative links correct.

## 3. Decisions

- Grep is the search method. No search tool, index, or MCP server in this work.
- The stdlib-only rule for the CLI still applies. No new dependency.
- Generated reference over hand-written reference: it cannot drift unnoticed.
- `docs/superpowers` and `docs/ROADMAP.md` stay out of the wheel. They are internal planning.
- Release as 3.0.2: no runtime change and no `linch.__all__` change.

## 4. Work packages

### WP1. Package docs

Copy into the wheel, keeping relative layout so existing links keep working:

```text
linch/_docs/docs/usage/
linch/_docs/docs/architecture/
linch/_docs/docs/architecture.md
linch/_docs/docs/usage.md
linch/_docs/docs/migration-3.0.md
linch/_docs/docs/versioning.md
linch/_docs/examples/
```

Add `linch docs path` to the scaffold CLI. It prints the absolute directory that holds `docs/`
and `examples/`, and exits non-zero with an actionable message if neither exists.

Resolve the packaged directory first, then the source checkout. Hatch `force-include` may not
apply to editable installs, so the fallback is required. Verify this rather than assume it.

Tests:

- Build the wheel, install it into a clean venv outside the repo, and assert `linch docs path`
  returns a directory with `docs/usage/quickstart.md`.
- Every relative link in shipped pages resolves inside the shipped tree. Links that must leave
  it, such as `production.md` to `docs/superpowers/measurements`, become absolute GitHub URLs
  pinned to a tag.

### WP2. Generated API reference

Add `scripts/generate_api_reference.py`, following `scripts/generate_event_catalog.py`. Output
is `docs/usage/api-reference.md`, committed, with a test failing when it is stale.

- One `###` heading per name in `linch.__all__` and per name in each `linch.ext.*` `__all__`.
- Each entry has the exact import line, kind, signature, first docstring line, and a link to the
  usage page that covers it, when one exists.
- Names without a docstring are listed by the generator as a work list and get one in this WP.

Keep the generator static where possible, so optional extras are not needed to run it.

### WP3. Resolve the import contract

For each private import path used in docs, take one of these:

1. The name is in `linch.__all__`: change the doc to `from linch import ...`.
2. The name needs an optional extra, such as `linch.sessions.postgres`: list the module as a
   supported import path in `docs/versioning.md`.
3. Otherwise, stop and report it. Adding to `linch.__all__` is an owner decision and would change
   the release version.

Add a test that parses every `python` block in shipped pages, requires each `linch` import to be
in `__all__`, in `linch.ext.*`, or on the supported list, and requires each block to compile.
Running every block is out of scope; `quickstart.md` blocks marked offline do run.

### WP4. Make pages greppable

- Headings use exact symbol names.
- Each usage page starts with one line stating when to use it.
- Add `docs/usage/troubleshooting.md`: exact error text, cause, and fix for the errors a builder
  is likely to hit, including `ConfigError` messages from agent setup, tool contract failures,
  provider auth, and run contract mismatch. The first draft comes from `raise` sites and is
  trimmed by hand. Exact text is required, so a test asserts each quoted message still exists in
  `src/`.
- Do not restructure or rewrite pages beyond these changes.

## 5. Acceptance

Run after WP1 to WP4, with a clean venv, only the built wheel, and a fresh coding agent in an
empty directory. It gets `linch docs path` and the task, and no other context.

| # | Task |
|---|---|
| 1 | Agent with one custom tool, tested offline |
| 2 | Structured output with a Pydantic schema |
| 3 | Pre-tool hook that blocks one command |
| 4 | Project skill, loaded and reloaded during a session |
| 5 | Subagent used by a parent agent |
| 6 | Connect an MCP server over stdio |
| 7 | Compaction and token budget configured |
| 8 | Durable run that resumes after a simulated crash |
| 9 | Custom provider adapter passing the provider contract tests |
| 10 | Migrate a small 2.x snippet using the migration guide |

A task passes if its tests run offline, it uses no import outside the supported surface, and no
person helped.

Pass bar: at least 8 of 10. Record every grep that found nothing. Each miss is fixed in docs or
explained. If several misses are "how do I X" questions that exact-name search cannot answer,
reopen `linch docs search` as a follow-up. Otherwise it stays out.

## 6. Budget

- `src/`: under 80 lines, for the CLI command and path resolution.
- Generator: under 250 lines.
- No runtime behavior change. Provider, loop, and session code are untouched.

## 7. Open points

- Whether hatch includes the docs in sdists as well as wheels. Check in WP1.
- Wheel size growth from `examples/`. Measure in WP1. Keep them unless growth is unreasonable.
- Whether task 9 is too large for the blind test. Replace it after the first run if the failure
  comes from task size and not from docs.
