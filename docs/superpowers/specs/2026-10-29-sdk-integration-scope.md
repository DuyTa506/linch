# Linch 3.0 integration scope

Date: 2026-10-29

## Authorized outcome

Coordinate the existing reliability planning session with this execution session, integrate the selected OpenHuman-inspired SDK capabilities, release feature by feature, and perform independent code review of every feature and of the integrated branch.

The user authorized integration and delegated orchestration/review. Technical API and sequencing choices below are implementation decisions, not additional user requirements.

## Complete feature scope

1. Auxiliary model calls: usage/cost, shared run budget, cancellation, admission and parent-run/purpose attribution, beginning with compaction.
2. Deferred capability discovery: native tools and registered MCP tools, skill metadata search through the existing Skill loader, host eligibility, durable branch-scoped activation and consistent request/scheduler/fork/compaction/resume behavior.
3. Recoverable result projections: per-tool compression/truncation policy, explicitly opted-in focus, retained original, bounded paging, recovery provenance and preservation of recovery references through caps.
4. Token-budgeted/chunked conversation compaction, retaining valid tool call/result boundaries, recent anchors and immutable full history.
5. Cacheable final request prefix fingerprints and redacted drift telemetry; legitimate host/tool/policy changes remain possible.
6. Scoped bounded memory prefetch/cache with tenant/session/store/version identity and invalidation fences.
7. The other session's 3.0 reliability specification: default-on configurable controls, declared prerequisites/completion, bounded correction, conservative rescue, native malformed-batch rejection, semantic recovery feedback, durable ledger, telemetry and evals.

No feature may be silently omitted from completion. Feature-specific prerequisites and review findings remain work until verified or explicitly changed by the user.

## Boundaries

- Preserve the current neutral identity/persona; no persona expansion.
- Python >=3.10, async runtime/provider paths, duck-typed protocols.
- Provider wire formats stay inside provider adapters.
- Public additions are deliberate through linch.__all__ and public API tests.
- Keep optional discovery/compression/prefetch opt-in; accounting correctness applies to actual auxiliary calls. Reliability's changed defaults belong to major 3.0.
- Implement Python-native contracts independently; do not copy GPL OpenHuman/TinyAgents/TinyJuice implementation into the MIT SDK.
- Preserve the existing session log, provider-view projection, permission ordering, budgets and shared execution backend contracts.
- Do not mutate the original checkout's user-owned docs/ROADMAP.md, plans/ or openhuman/.
- Reliability owns linch.reliability extension state; activation uses its own typed state/contract and does not reuse malformed-call sidecars.
- Mutable shared runtime seams are integrated serially.

## Integration and coordination

Baseline: Linch 2.0.0, commit 1bf0709b54fb481bdf50822e063dc4ba1aec45e0.
Implementation is isolated from the source checkout.
Reliability has a separately reviewed design and shares interface review with the other feature lanes.
Operational paths, agent identities and live process handles belong in ignored local ledgers, not published specifications.

## Feature release and review gates

- Each feature has a precise spec/task brief, test-first implementation, focused regression coverage, self-review, and independent spec-compliance/code-quality review.
- Fix blocking findings and independently re-review the fix.
- Preserve separate feature commits and changelog sections; prerelease milestones target 3.0 rather than introducing changed defaults in 2.x.
- Before each release milestone, run the appropriate full suite, ruff check, ruff format --check and pyright, then build/check distributions and validate the installed wheel in isolation.
- A local commit/build/tag is not proof of a remote release. Verify remote commit/tag/release and intended artifacts after publishing.
- Stable 3.0 requires cross-feature scenario tests and a final independent whole-branch review.
- Remote access/auth and publication capability must be verified when needed; continue unaffected local work if remote access is unavailable.

## Completion evidence

The private integration ledger must link each feature to commits, exact test outputs and review verdicts. It must also record integration scenarios, release versions/artifacts, remote publication evidence and remaining blockers. Missing or indirect evidence is not completion.
