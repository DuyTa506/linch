# Default Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the approved reliability policy the default for every Linch provider while preserving bounded recovery, correct tool-side-effect ordering, durable resume, and the common model-call receipt ledger as the only usage/budget authority.

**Architecture:** Public policy data lives in `linch.reliability`; run-local decision state and strict rescue parsing live in `loop/reliability`. The runner consumes the pre-landed accounted primary-delivery seam, makes reliability decisions at existing response/tool/final-answer chokepoints, and persists only the versioned `linch.reliability` extension state. The accounting ledger remains responsible for every provider delivery, usage total, cost, budget charge, and malformed-native drain.

**Tech Stack:** Python 3.10+, frozen/slotted dataclasses, existing normalized async provider streams, `RunStore`/`SessionStore`, existing scheduler/finalizers, pytest, Ruff, Pyright.

**Spec:** [default reliability spec](../specs/2026-10-30-default-reliability.md) and [accounted model calls spec](../specs/2026-10-30-accounted-model-calls.md).

## Global Constraints

- This is a reliability plan, not a second accounting plan. The common `_ModelCallLedger` is the sole owner of provider-call usage, pricing, cumulative totals, and `RunBudget` receipt charging. Reliability retains only a receipt delivery-ID reference.
- Do not begin Tasks 2–5 until the separately owned accounting Task 3 primary-delivery wiring is reviewed and landed. It must provide: a valid-`message_end` drain for malformed native JSON/non-object arguments; a sanitized native-rejection fact; an already-settled common receipt with its `delivery_attempt_id`; strict `message_start.model`; and staged durable partial-event behavior that discards rejected raw arguments. Bind to its final private names at dispatch; do not invent a competing field/helper now. RunContract must distinguish accounting terminal_error_v0 from reliability bounded_feedback_v1, so old terminal runs cannot silently resume with correction.
- Reliability must not re-drain a provider stream, parse raw native arguments a second time, create a fake `{}` call or `__invalid_json` sentinel, settle a second receipt, or fall back from a missing/invalid `message_start.model` to the requested model. Other structural stream errors remain existing strict provider errors.
- Use exactly `RunCheckpoint.extension_state["linch.reliability"]`, with a version inside its value. Accounting remains exactly `linch.model_calls.v1`; the runner merges both namespaces and never overwrites inactive/unknown extension namespaces.
- `ReliabilityPolicy()` is the Agent/AgentOptions default. `reliability=None` disables this subsystem. All policy booleans are independent ablations; state and correction budgets are per run, never shared between sessions or inherited by subagents.
- Public additions are only the approved policy dataclasses, `ReliabilityEvent`, and `is_reliability_event`, exported deliberately through `linch.__all__`. Keep provider wire formats, prompts/persona, `CompactionStrategy`, `RunBudget.charge`, transport retry, tool retry, and structured-output repair contracts unchanged.
- A malformed native batch with valid `message_end` preserves its accounted usage but executes zero tools, including otherwise-valid siblings. Neither diagnostics, reliability state/events, SDK-persisted partial events, nor errors may retain raw malformed arguments or provider metadata.
- Validate canonical policy tool names, duplicate edges, self-dependencies, cycles, and terminal-tool evidence after tools finish loading and before the first provider call. Validation/state/contract corruption or unsupported versions fail closed; never silently reset a ledger or counter.
- Preserve normal direct text, refusals, output truncation, cancellation, loop/turn limits, compaction defaults, hooks, and budgets unless a declared reliability mechanism applies. A clean run emits no reliability event, feedback, or extra provider call.
- Every task is implemented by a fresh worker and accepted by a fresh reviewer before the next task. Stage only files owned by that task; preserve other workers' dirty changes. Use TDD and run the named focused checks before committing.
- Do not edit `openhuman/`, other plans, or existing `docs/ROADMAP.md` work. The final release-preparation task may build/inspect local artifacts but must never tag, publish, upload, push, or change a version without explicit root-release authorization.

## Accounting Dependency Gate

The current accounting foundation worker owns `model_calls.py`, `budget.py`, the journal envelope, and its unit tests. The accounting implementation—not this plan—also owns the primary-stream malformed-native drain and receipt settlement. Before a reliability worker starts Task 2, the reviewer must confirm all of the following against the landed accounting change:

- The runner receives a sanitized rejection fact plus the settled `ModelCallReceipt.delivery_attempt_id`; raw native argument text is unavailable to reliability.
- A malformed/mixed native tool batch reaches a structurally valid `message_end`, preserves valid Usage once, has `rejected_native` disposition, executes zero tools, and does not persist raw partial argument deltas.
- The active primary attempt, frozen-request replay, receipt journal, checkpoint, and `RunContract` accounting fingerprints are already reconciled by accounting code.
- Reliability may add its own checkpoint namespace and contract policy payload, but must not modify accounting's receipt codec, budget ownership, or delivery state machine to do so.

If any item is absent, stop Task 2 and return the missing accounting binding to the root coordinator; it is a sequencing dependency, not permission to patch accounting ad hoc.

## Review Focus

- A malformed native batch with one valid sibling must preserve one reported receipt/usage and run zero tools without leaking raw arguments. Task 2 consumes the accounting seam and adds the reliability-side regression.
- A valid-looking tool envelope embedded in prose, a code fence, an oversized string, an unoffered tool, structured output, `tool_choice="none"`, or force-final must never become an executable rescue. Task 2 pins each exclusion.
- A post-hook canonical call with unmet prerequisites must produce an ordinary paired error before permissions/side effects; a background ACK and a failed predecessor must not unlock a dependent call. Task 3 pins this ordering.
- A missing completion gate on text and final-tool paths must be bounded by the shared correction count, must not be bypassed by force-final, and must leave final-tool pairing valid. Task 4 pins it.
- Crashes at each reliability-created multi-message append boundary must reconcile the ordered durable prefix, preserve correction count/receipt reference, and neither reinfer nor duplicate feedback. Task 5 pins all prefix states.

---

### Task 1: Add the public policy, validated run state, event codec, and contract binding

**Files:**

- Create: `src/linch/reliability.py`
- Create: `src/linch/loop/reliability.py`
- Modify: `src/linch/agent.py`
- Modify: `src/linch/events.py`
- Modify: `src/linch/__init__.py`
- Modify: `src/linch/loop/runner.py`
- Modify: `tests/test_public_api.py`
- Modify: `tests/storage/test_run_contract.py`
- Create: `tests/test_reliability.py`
- Create: `tests/loop/test_reliability_state.py`

**Interfaces:**

- Produces public frozen, slotted dataclasses exactly as approved:
  `ToolPrerequisite(tool_name: str, requires_success: tuple[str, ...])`,
  `CompletionGate(required_successful_tools: tuple[str, ...])`, and
  `ReliabilityPolicy(max_correction_turns: int = 2, retry_invalid_responses: bool = True, rescue_text_tool_calls: bool = True, inject_recovery_hints: bool = True, prerequisites: tuple[ToolPrerequisite, ...] = (), completion: CompletionGate | None = None)`.
- Produces `ReliabilityEvent(decision_id, mechanism, action, reason, turn_index, correction_count, type="reliability")` and `is_reliability_event`; its strict event codec round-trips only the approved scalar fields.
- Produces private `validate_reliability_policy(...) -> _ValidatedReliabilityPolicy`, `ReliabilityRuntimeState`, and `RELIABILITY_PARSER_VERSION`. The validator canonicalizes against the loaded tool names, rejects invalid DAG shapes, and rejects a terminal output tool as completion evidence.
- Consumes the final effective tool registry and final tool name after Agent/session initialization, plus the resolved Agent policy. It runs before any provider dispatch on fresh and resumed runs.
- Produces a canonical reliability policy/parser payload in `_build_run_contract`; a changed policy or parser version therefore makes resume fail through existing `RunContract` comparison.

- [ ] **Step 1: Write failing public/configuration tests in `tests/test_reliability.py`.**
  Assert omitted `Agent(reliability=...)` configuration resolves to a fresh default policy, explicit `None` disables it, each boolean can be independently false, the three dataclasses are frozen/slotted, and negative/non-int `max_correction_turns` is rejected.

- [ ] **Step 2: Write failing validation/state/event tests.**
  In `tests/loop/test_reliability_state.py`, cover canonical-name lookup after tool loading, duplicate prerequisite edges, self-dependency, cycles, missing tools, and final-tool evidence rejection. In `tests/storage/test_run_contract.py` and `tests/test_public_api.py`, assert policy/parser mismatch fails resume, state corruption/unknown state version fails closed, `ReliabilityEvent` round-trips, and only the approved reliability names enter `linch.__all__`.

- [ ] **Step 3: Run the focused tests to verify they fail for the missing subsystem.**

  Run: `pytest tests/test_reliability.py tests/loop/test_reliability_state.py tests/storage/test_run_contract.py tests/test_public_api.py -q`

  Expected: FAIL because the policy module, event, validator, and contract fields do not yet exist.

- [ ] **Step 4: Implement the public types and private validation/state boundary.**
  Put only public immutable configuration in `reliability.py`; keep run counters, successful-tool names, pending-action representation, canonical serialization, and parser version private to `loop/reliability.py`. Add `reliability: ReliabilityPolicy | None` to `AgentOptions` and `Agent`, resolving omission to `ReliabilityPolicy()` and retaining `None` exactly. Do not validate tool existence in `Agent.__init__`; have the runner validate the final loaded registry immediately before its first dispatch.

- [ ] **Step 5: Implement strict event/public-export/contract plumbing.**
  Add the event discriminator and serialization branch without changing legacy event wire shapes. Add the lazy export/type-checking entries and public-surface assertions. Include the complete canonical policy mapping and parser version under the existing run-contract policy payload; use the existing fail-closed mismatch path rather than a reliability-specific compatibility escape hatch.

- [ ] **Step 6: Run the focused tests and static checks.**

  Run: `pytest tests/test_reliability.py tests/loop/test_reliability_state.py tests/storage/test_run_contract.py tests/test_public_api.py -q && ruff check src/linch/reliability.py src/linch/loop/reliability.py src/linch/agent.py src/linch/events.py src/linch/loop/runner.py && pyright src/linch`

  Expected: PASS. A policy/state/contract mismatch is rejected, while a default policy with no intervention remains inert.

- [ ] **Step 7: Fresh-review gate.**
  A reviewer checks the exact public surface, frozen/slotted policy objects, fail-closed state/version behavior, terminal-tool restriction, and that this task does not alter accounting ownership or provider behavior.

- [ ] **Step 8: Commit only the owned implementation and tests.**

  ```bash
  git add src/linch/reliability.py src/linch/loop/reliability.py src/linch/agent.py src/linch/events.py src/linch/__init__.py src/linch/loop/runner.py tests/test_reliability.py tests/loop/test_reliability_state.py tests/storage/test_run_contract.py tests/test_public_api.py
  git commit -m "feat: add reliability policy foundation"
  ```

### Task 2: Consume the accounted native-rejection seam and add bounded correction/rescue

**Files:**

- Modify: `src/linch/loop/reliability.py`
- Modify: `src/linch/loop/runner.py`
- Create: `tests/loop/test_reliability_responses.py`
- Modify: `tests/loop/test_provider_stream_contract_v2.py`

**Interfaces:**

- Consumes the reviewed accounting Task 3 binding: a sanitized malformed-native rejection fact associated by the runner with an already-settled `ModelCallReceipt.delivery_attempt_id`. The concrete private field/helper name is finalized from that landed task; reliability adds no second stream parser or receipt operation.
- Produces private strict helpers `parse_rescue_end_turn(...) -> _RescueDecision` and `ReliabilityRuntimeState.decide_response(...) -> _ResponseDecision`. A valid rescue yields one deterministic synthetic `ToolUseBlock` ID derived from the persisted decision ID; an invalid candidate yields no fake call.
- Consumes the final request after request hooks: its offered tool names, output schema, final-tool name, tool choice, and force-final state.
- Produces only canonical reliability-created messages: a fixed Linch-marked assistant diagnostic and bounded feedback asking for resubmission when correction is allowed. The rejected original assistant message and raw provider metadata are never appended.

- [ ] **Step 1: Write failing response-correction tests.**
  Cover an empty `end_turn`, a malformed exact tool envelope, correction budget `0`, and exhaustion after two corrections. Assert the fixed diagnostic/feedback are appended once, the next request is a normal retry, `ReliabilityEvent` has a stable decision ID/count, and a disabled `retry_invalid_responses` path ends with the normal error terminal rather than accepting invalid output.

- [ ] **Step 2: Write failing strict-rescue tests.**
  Assert that only whitespace around exactly one `<tool_call>{"name":"Offered","arguments":{...}}</tool_call>` with strict JSON and exactly the two keys becomes a tool call; valid rescue consumes no correction turn. Assert prose, fenced examples, multiple envelopes, malformed/non-object arguments, over-64-KiB UTF-8 text, unknown/unoffered tools, structured output, `tool_choice="none"`, and force-final do not rescue.

- [ ] **Step 3: Write the accounting-seam regression before implementing runner behavior.**
  Use the landed accounting fake/provider fixture to emit malformed native JSON/non-object plus a valid sibling and valid `message_end` Usage. Assert usage/receipt is already preserved once, zero tools execute, no raw malformed argument appears in SDK session history, `ModelCallEvent`, `ErrorEvent`, or persisted partial-event journal, and reliability consumes the sanitized fact to issue at most one correction. Keep the existing strict-provider-error assertions for missing `message_end`, invalid event order, invalid IDs, invalid Usage, and invalid/missing `message_start.model`.

- [ ] **Step 4: Run the response tests to verify they fail.**

  Run: `pytest tests/loop/test_reliability_responses.py tests/loop/test_provider_stream_contract_v2.py -q`

  Expected: FAIL because no strict rescue/correction decision is integrated at the post-settlement, pre-history response chokepoint.

- [ ] **Step 5: Implement the strict parser and runner response decision.**
  Measure the complete text in UTF-8 before parsing; never scan prose or repair JSON. Use the final request's offered tools, not the registry alone. At the accounting-provided response seam, consume the settled receipt reference first, then either transform a valid rescue through the ordinary scheduler path or create a pending correction decision. Do not edit `streaming.py`, `model_calls.py`, or budget code to reimplement the accounting-owned native drain/settlement path.

- [ ] **Step 6: Preserve bounded/error behavior.**
  Count only true corrective turns globally per run; rescue does not count. When correction is unavailable, do not run an invalid native call and do not synthesize input—terminate through the existing error path. Keep transport retries, tool retries, and structured-output repair separate from reliability correction count.

- [ ] **Step 7: Run focused tests and check formatting/types.**

  Run: `pytest tests/loop/test_reliability_responses.py tests/loop/test_provider_stream_contract_v2.py -q && ruff check src/linch/loop/reliability.py src/linch/loop/runner.py && pyright src/linch`

  Expected: PASS. Valid native-stream Usage is retained by accounting once; malformed batches never execute and rescue remains deliberately narrow.

- [ ] **Step 8: Fresh-review gate.**
  A reviewer verifies the accounting dependency was consumed rather than duplicated, no raw argument/metadata persistence was introduced, no invalid stream contract was relaxed, and no fake tool input/sentinel exists.

- [ ] **Step 9: Commit only this task's files after the accounting dependency is landed.**

  ```bash
  git add src/linch/loop/reliability.py src/linch/loop/runner.py tests/loop/test_reliability_responses.py tests/loop/test_provider_stream_contract_v2.py
  git commit -m "feat: correct invalid provider responses safely"
  ```

### Task 3: Enforce prerequisite ordering and record only foreground successes

**Files:**

- Modify: `src/linch/scheduler.py`
- Modify: `src/linch/loop/reliability.py`
- Modify: `src/linch/loop/runner.py`
- Create: `tests/loop/test_reliability_tools.py`
- Modify: `tests/tools/test_scheduler_v2.py`

**Interfaces:**

- Extends private `execute_tool_calls(...)` with `reliability_precondition: Callable[[ResolvedCall], str | None] | None` and `on_foreground_result: Callable[[ToolCallEndEvent], Awaitable[None]] | None`; neither is public API.
- `reliability_precondition` consumes the immutable success-name snapshot captured before the provider tool batch and runs after final canonical input validation/PreToolUse revalidation but before permission evaluation or execution.
- `on_foreground_result` receives only the final post-hook foreground result, never a background ACK. It lets `ReliabilityRuntimeState.record_success(tool_name)` count a normal `ToolResult.is_error is False` for this run.
- Produces ordinary paired `ToolResult(is_error=True)` failures for prerequisite violations; it never invokes a predecessor automatically.

- [ ] **Step 1: Write failing scheduler-order tests.**
  Assert an unmet prerequisite generates an error result before permission prompting/execution, a post-hook transformed input is validated before the check, and a permitted predecessor is never auto-run. Assert a previous-turn successful foreground result unlocks the dependent call.

- [ ] **Step 2: Write failing batch-isolation/success-semantic tests.**
  Assert two calls from one assistant batch cannot unlock each other even if scheduler lanes are serial; a failed predecessor, denied call, interrupted call, and background ACK never unlock a dependent call; a foreground success after post-tool hooks does. Add a literal tool input containing `{"__invalid_json": "ordinary user data"}` and assert it reaches normal schema validation/execution rather than a sentinel branch.

- [ ] **Step 3: Run the focused tests to verify they fail.**

  Run: `pytest tests/loop/test_reliability_tools.py tests/tools/test_scheduler_v2.py -q`

  Expected: FAIL because the scheduler has no reliability pre-permission seam, foreground-only callback, or sentinel-free behavior.

- [ ] **Step 4: Implement the scheduler seams at the canonical ordering point.**
  Capture the successful-name ledger before `execute_tool_calls` resolves the incoming batch. Run the callback only after PreToolUse has settled and `_canonicalize_hook_input` succeeds, then turn a returned error into the scheduler's normal error-result path before `_evaluate_permissions`. Invoke the foreground callback only after post-tool hooks determine the final result. Remove the obsolete input sentinel branch now that accounting never manufactures it.

- [ ] **Step 5: Implement runner state updates without changing side-effect semantics.**
  The runner updates the per-run ledger only from the foreground callback/final result. It does not inherit this state into subagents, alter scheduler batching, or reinterpret tool content with regexes.

- [ ] **Step 6: Run focused tests and static checks.**

  Run: `pytest tests/loop/test_reliability_tools.py tests/tools/test_scheduler_v2.py -q && ruff check src/linch/scheduler.py src/linch/loop/reliability.py src/linch/loop/runner.py && pyright src/linch`

  Expected: PASS. Prerequisites block before side effects and permission UI, and only the specified foreground success semantics unlock later turns.

- [ ] **Step 7: Fresh-review gate.**
  A reviewer traces canonical validation → prerequisite → permission → execution, verifies background isolation and batch snapshots, and confirms legacy literal `__invalid_json` input is no longer commandeered.

- [ ] **Step 8: Commit only the owned files.**

  ```bash
  git add src/linch/scheduler.py src/linch/loop/reliability.py src/linch/loop/runner.py tests/loop/test_reliability_tools.py tests/tools/test_scheduler_v2.py
  git commit -m "feat: enforce reliability tool prerequisites"
  ```

### Task 4: Add bounded recovery hints and completion gates to both terminal paths

**Files:**

- Modify: `src/linch/loop/reliability.py`
- Modify: `src/linch/loop/runner.py`
- Modify: `src/linch/loop/finalize.py`
- Modify: `src/linch/loop/terminals.py`
- Create: `tests/loop/test_reliability_completion.py`
- Modify: `tests/loop/test_tool_failure_recovery.py`

**Interfaces:**

- Produces `ReliabilityRuntimeState.recovery_hint_text(results) -> str | None`, bounded to 1,024 characters, using only `ToolResult.is_error` and `ToolResult.recovery_hint`.
- Produces `ReliabilityRuntimeState.missing_completion() -> tuple[str, ...]` and `reserve_correction(mechanism, reason, turn_index) -> _CorrectionDecision`; the latter is shared by invalid-response and completion feedback and never resets after progress.
- Extends private `FinalizeCtx` with a reliability completion callback invoked before accepting either text-final or final-tool success, including a loop-guard force-final turn.
- Consumes existing `_gate_retry_tail` for text feedback and `_final_tool_retry_tail` for terminal-tool pairing; final-tool rejection uses the existing paired result mechanism, not an unpaired user text message.

- [ ] **Step 1: Write failing recovery-feedback tests.**
  Update `tests/loop/test_tool_failure_recovery.py` so errors with `recovery_hint` in both all-failed and partially-failed batches produce one capped hint after the `ToolResultBlock`s in the same result message. Assert `inject_recovery_hints=False` changes nothing, no regex is used to classify an error, and hints cause neither an extra inference nor a correction-count increment.

- [ ] **Step 2: Write failing completion-gate tests.**
  In `tests/loop/test_reliability_completion.py`, cover a text final and a `final_tool_name` final blocked until required foreground successes exist; verify the retry feedback, correction counts, budget exhaustion error, `max_correction_turns=0`, two-attempt exhaustion, and force-final non-bypass. Assert rejected final-tool feedback leaves every assistant tool use paired and does not execute the final tool.

- [ ] **Step 3: Run the focused tests to verify they fail.**

  Run: `pytest tests/loop/test_reliability_completion.py tests/loop/test_tool_failure_recovery.py -q`

  Expected: FAIL because the current all-failed-only separate feedback message and terminal finalizers do not implement the approved semantics.

- [ ] **Step 4: Implement in-message hints and bounded feedback.**
  Collect recovery hints from final tool results after hooks, preserve all result blocks, then add one trailing text block to that same user result message. Include errors in a partial batch, cap only supplemental hint text at 1,024 characters, and make it a no-op when no qualifying hint exists or the policy disables it.

- [ ] **Step 5: Implement completion gating before terminal acceptance.**
  Put the completion check before the force-final shortcut and before a success `ResultEvent` is formed in both finalizers. A missing requirement reserves one reliability correction and emits its stable event/feedback; exhausted correction budget emits the existing error terminal. Do not call the model directly or override cancellation, turn limit, loop guard, or budget checks.

- [ ] **Step 6: Run focused tests and static checks.**

  Run: `pytest tests/loop/test_reliability_completion.py tests/loop/test_tool_failure_recovery.py -q && ruff check src/linch/loop/reliability.py src/linch/loop/runner.py src/linch/loop/finalize.py src/linch/loop/terminals.py && pyright src/linch`

  Expected: PASS. Hints are informational and bounded; completion correction is bounded and pairing-safe on both terminal paths.

- [ ] **Step 7: Fresh-review gate.**
  A reviewer confirms partial failure behavior, 1,024-character cap, no secondary inference, final-tool pairing, correction accounting, and force-final ordering against the spec.

- [ ] **Step 8: Commit only the owned files.**

  ```bash
  git add src/linch/loop/reliability.py src/linch/loop/runner.py src/linch/loop/finalize.py src/linch/loop/terminals.py tests/loop/test_reliability_completion.py tests/loop/test_tool_failure_recovery.py
  git commit -m "feat: gate completion and surface recovery hints"
  ```

### Task 5: Make reliability-created append actions crash-safe and replay-safe

**Files:**

- Modify: `src/linch/loop/reliability.py`
- Modify: `src/linch/loop/checkpoint.py`
- Modify: `src/linch/loop/runner.py`
- Modify: `src/linch/reports.py`
- Create: `tests/loop/test_reliability_durability.py`
- Modify: `tests/loop/test_exact_model_input.py`
- Modify: `tests/loop/test_run_resume.py`
- Modify: `tests/test_reports.py`

**Interfaces:**

- Produces strict, versioned `ReliabilityRuntimeState.snapshot()` / `restore(raw)` under exactly `extension_state["linch.reliability"]`; it contains successful-tool names, correction count, parser version, and at most one canonical pending action.
- A pending action contains a stable decision ID, receipt delivery-ID reference when applicable, updated correction count, a durable base boundary/digest, and canonical message suffix descriptors. It never contains raw malformed arguments, provider metadata, Usage, a duplicate budget charge, or an independent receipt.
- Produces `reconcile_pending_reliability_append(session, state) -> list[Message]`, which validates the ordered durable prefix and appends only the missing suffix by structural `message_to_dict` equality.
- Consumes the restored common receipt ledger before replay. On a frozen primary resume, a receipt-keyed native rejection reconstructs its canonical diagnostic/pending feedback without reinference; it does not re-use a delivery ID or settle/charge again.
- Produces run-report reliability counts/decisions from `ReliabilityEvent` without exposing private state or raw payloads.

- [ ] **Step 1: Write failing state/prefix-reconciliation tests.**
  Simulate crashes before any, after the first, and after all messages of a diagnostic-plus-feedback action; assert only the missing suffix is appended. Include a superficially matching last message with a wrong earlier prefix and assert fail-closed, proving an independent last-message comparison is insufficient.

- [ ] **Step 2: Write failing receipt/frozen-replay tests.**
  Resume a run with the accounting Task 3 settled native-rejection receipt and a reliability pending action. Assert it reconstructs the fixed diagnostic from sanitized state, preserves cumulative Usage exactly once, makes no provider call, emits no duplicate feedback/reliability decision, and rejects corrupt/unsupported reliability state or contract mismatch.

- [ ] **Step 3: Write failing interaction/report tests.**
  Cover a crash around a recovery-hint result message and completion feedback; assert successful tools are not re-executed, correction count never resets, and `RunReport` includes decision summaries without raw data. Add cancellation and checkpoint-save-failure cases that stop safely rather than claiming a durable append.

- [ ] **Step 4: Run the focused durability tests to verify they fail.**

  Run: `pytest tests/loop/test_reliability_durability.py tests/loop/test_exact_model_input.py tests/loop/test_run_resume.py tests/test_reports.py -q`

  Expected: FAIL because reliability state is not namespaced/persisted and resume cannot reconcile ordered multi-message prefixes.

- [ ] **Step 5: Implement pending-intent-before-append durability.**
  Before every reliability-created append (diagnostic, correction feedback, completion feedback, rescued canonical assistant message, or result-message hint), write the updated state with its canonical pending intent. On recovery, validate the stored base prefix and present suffix structurally, append only the absent suffix, then clear pending state only after all messages are durable. Do not reuse `_last_message_matches` as the reconciliation algorithm.

- [ ] **Step 6: Integrate resume ordering and receipt references.**
  Restore/reconcile reliability immediately after common-ledger restoration and before the current `provider_pending` replay branch. Merge, rather than replace, `linch.model_calls.v1`, `linch.reliability`, hook, and core extension state. Use the settled receipt delivery ID solely as a reference; keep total usage/cost/`usage_complete` owned by the ledger and terminal accounting plumbing.

- [ ] **Step 7: Deduplicate observability and verify reports.**
  Persist a `ReliabilityEvent` once per stable decision ID before its observable action. On resume, detect the recorded decision and avoid another event/feedback append. Fold only its safe public fields into `RunReport` summaries.

- [ ] **Step 8: Run focused tests and static checks.**

  Run: `pytest tests/loop/test_reliability_durability.py tests/loop/test_exact_model_input.py tests/loop/test_run_resume.py tests/test_reports.py -q && ruff check src/linch/loop/reliability.py src/linch/loop/checkpoint.py src/linch/loop/runner.py src/linch/reports.py && pyright src/linch`

  Expected: PASS. Every crash boundary either completes its canonical suffix once or fails closed; resume never reinfers/rebills a received rejected response.

- [ ] **Step 9: Fresh-review gate.**
  A reviewer independently walks all prefix states, frozen-request ordering, namespace merges, receipt-reference-only data flow, event deduplication, and fail-closed corruption behavior.

- [ ] **Step 10: Commit only the owned files.**

  ```bash
  git add src/linch/loop/reliability.py src/linch/loop/checkpoint.py src/linch/loop/runner.py src/linch/reports.py tests/loop/test_reliability_durability.py tests/loop/test_exact_model_input.py tests/loop/test_run_resume.py tests/test_reports.py
  git commit -m "feat: persist reliability recovery state"
  ```

### Task 6: Prove cross-feature behavior with the required offline matrix and ablations

**Files:**

- Create: `tests/loop/test_reliability_matrix.py`
- Create: `tests/test_reliability_evals.py`
- Modify: `tests/loop/test_compaction_ladder.py`
- Modify: `tests/loop/test_checkpointable_hooks.py`
- Modify: `tests/subagents/test_budget_shared.py`
- Modify: `tests/subagents/test_subagent_isolation.py`
- Modify: `tests/test_evals.py`

**Interfaces:**

- Consumes the completed reliability policy and accounting ledger only through public Agent configuration and normal fake-provider events.
- Produces no new runtime API. It proves default/full policy, `reliability=None`, and each individual boolean ablation against the same deterministic workload.
- Keeps live-model evaluation outside unit CI: live measurements may report completion, extra calls, correction count, tokens, and latency when credentials exist, but scripted output is never evidence of model-quality improvement.

- [ ] **Step 1: Write failing offline matrix tests.**
  Add deterministic fake-provider workloads covering: direct text/refusal/truncation stays normal; valid rescue; all rescue exclusions; malformed native JSON/non-object and mixed batches execute zero tools; literal `__invalid_json` input works; prerequisite missing/same-batch/failed/background cases; completion success/exhaustion/force-final; and partial tool failure with no replayed side effect.

- [ ] **Step 2: Write failing cross-feature tests.**
  Exercise compaction/default `CompactionLadder`, request and tool hooks, cancellation, turn limit, capped and uncapped budgets, unknown-usage fence behavior, exact model-input resume, and parent/subagent independence. Assert subagents use their own reliability state/correction count even when their tree shares one budget object.

- [ ] **Step 3: Write failing ablation/eval assertions.**
  Use the existing deterministic eval harness to run one workload with the default policy, `None`, and each false boolean. Assert only the named mechanism changes; record event/call/usage counts as behavior evidence, not quality claims.

- [ ] **Step 4: Run the matrix to verify it fails before final integration.**

  Run: `pytest tests/loop/test_reliability_matrix.py tests/test_reliability_evals.py tests/loop/test_compaction_ladder.py tests/loop/test_checkpointable_hooks.py tests/subagents/test_budget_shared.py tests/subagents/test_subagent_isolation.py tests/test_evals.py -q`

  Expected: FAIL until all prior slices cooperate at the real runner chokepoints.

- [ ] **Step 5: Make only minimal integration fixes revealed by the matrix.**
  Preserve the approved boundaries: no new provider-specific branch, prompt rewrite, compaction default change, accounting mutation, or new external dependency. If a failing case requires a broader policy/API decision, stop and return it to the root coordinator instead of extending scope.

- [ ] **Step 6: Run the focused matrix, related accounting tests, and full quality gate.**

  Run: `pytest tests/loop/test_reliability_matrix.py tests/test_reliability_evals.py tests/test_model_calls.py -q && pytest && ruff check . && ruff format --check . && pyright`

  Expected: PASS. Offline coverage demonstrates safety/correctness across mechanisms; no live credential is required.

- [ ] **Step 7: Fresh-review gate.**
  A reviewer checks the matrix against every reliability-spec bullet and the accounting interaction rules, with special attention to no raw argument leak, no duplicate accounting, and no default compaction/ladder regression.

- [ ] **Step 8: Commit only files changed to close demonstrated integration gaps.**

  ```bash
  git add tests/loop/test_reliability_matrix.py tests/test_reliability_evals.py tests/loop/test_compaction_ladder.py tests/loop/test_checkpointable_hooks.py tests/subagents/test_budget_shared.py tests/subagents/test_subagent_isolation.py tests/test_evals.py
  git commit -m "test: cover reliability integration matrix"
  ```

### Task 7: Document 3.0 defaults and prepare—without publishing—the prerelease handoff

**Files:**

- Create: `docs/migration-3.0.md`
- Modify: `docs/usage/README.md`
- Modify: `docs/usage/agent.md`
- Modify: `docs/usage/events.md`
- Modify: `docs/architecture/README.md`
- Modify: `docs/architecture/turn-lifecycle.md`
- Modify: `docs/architecture/events.md`
- Modify: `docs/architecture/invariants.md`
- Modify: `docs/architecture/compaction.md`
- Modify: `docs/versioning.md`
- Modify: `CHANGELOG.md`
- Modify only after explicit root-release approval of an exact version: `pyproject.toml`

**Interfaces:**

- Documents the public `ReliabilityPolicy`, `ToolPrerequisite`, `CompletionGate`, `ReliabilityEvent`, and `is_reliability_event` surface; it must not present private receipt/state/parser types as public API.
- Documents the 3.0 default behavior, `reliability=None` migration/ablation choices, bounded correction semantics, prerequisite/completion behavior, event privacy, receipt/usage completeness, and exact durable-resume guarantees/limits.
- Documents accounting's `linch.model_calls.v1` and reliability's exact `linch.reliability` namespace distinction, but does not claim exactly-once external billing, distributed shared-budget recovery, arbitrary custom-strategy projection replay, or model-quality gains.

- [ ] **Step 1: Write/update documentation examples and a failing public-surface check where needed.**
  Ensure examples import only top-level approved names, demonstrate default/disabled/ablation configuration, and identify the breaking default/migration path. Extend `tests/test_public_api.py` only if an approved new top-level name lacks coverage; do not create a fake runtime test solely for prose.

- [ ] **Step 2: Verify current documentation claims against the implementation and specs.**
  Cross-check event ordering, normal primary `UsageEvent` compatibility, additive auxiliary usage metadata, `ResultEvent.usage_complete`, malformed-native privacy, correction limits, compaction accounting, and resume fail-closed behavior. Remove or correct any old claim that the loop alone owns usage/budget charging.

- [ ] **Step 3: Run documentation-adjacent checks before editing release metadata.**

  Run: `pytest tests/test_public_api.py tests/test_reports.py tests/test_model_calls.py tests/loop/test_reliability_matrix.py -q`

  Expected: PASS; examples and terminology match the shipped public surface.

- [ ] **Step 4: Write migration, architecture, usage, versioning, and changelog updates.**
  State plainly that reliability is enabled by default in 3.0, explain `None`/individual ablations, list behavior changes that can affect existing apps, and point durable users to contract mismatch/fail-closed migration behavior. Keep migration guidance scoped to the approved feature; do not alter prompts, providers, or unrelated roadmap content.

- [ ] **Step 5: Perform prerelease preparation only with release-owner authorization.**
  The root release owner alone chooses an exact 3.0 prerelease version. If that approval arrives, update `pyproject.toml`, confirm `linch.__version__` fallback, build local sdist/wheel artifacts, inspect their metadata/contents, and run an isolated local install smoke test. Without approval, leave version files untouched and report the blocker.

- [ ] **Step 6: Run final checks.**

  Run: `pytest && ruff check . && ruff format --check . && pyright`

  Expected: PASS. If a local build is authorized, also run the project’s standard local build command and record artifact names/checksums for the root coordinator.

- [ ] **Step 7: Fresh final-review gate and handoff.**
  A final independent reviewer checks docs/spec/code/test agreement, 3.0 migration accuracy, namespace/receipt wording, public API, version metadata, and build output if authorized. Hand the root coordinator paths, commits, focused/full check outputs, review verdicts, artifact evidence, and unresolved blockers. Do not run `git tag`, `git push`, upload, package publication, or registry commands.

- [ ] **Step 8: Commit only documentation/release-preparation files that were explicitly authorized.**

  ```bash
  git add docs/migration-3.0.md docs/usage/README.md docs/usage/agent.md docs/usage/events.md docs/architecture/README.md docs/architecture/turn-lifecycle.md docs/architecture/events.md docs/architecture/invariants.md docs/architecture/compaction.md docs/versioning.md CHANGELOG.md
  git commit -m "docs: document Linch 3.0 reliability defaults"
  ```

## Coverage Check

| Spec requirement | Owning task |
|---|---|
| Public default/disable/ablation policy, DAG validation, state isolation, event codec, and contract fingerprint | Task 1 |
| Empty/invalid response correction, strict text rescue, accounted malformed-native rejection, no raw data leak | Task 2 |
| Canonical prerequisite ordering, batch snapshot, foreground success definition, no sentinel collision | Task 3 |
| Per-error bounded hints and text/final-tool completion gates with correction exhaustion | Task 4 |
| `linch.reliability` checkpoint state, ordered-prefix recovery, frozen replay, receipt reference, telemetry dedup | Task 5 |
| Offline provider matrix, compaction/hooks/cancellation/budgets/subagents, default versus ablations | Task 6 |
| Defaults, migration, event/accounting documentation, semver/prerelease handoff | Task 7 |
