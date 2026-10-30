# WP15: direct OpenTelemetry and no-op measurements

D11 is implemented on `scale/otel`. Baseline: `175ea48`; measured implementation:
`36aec4a`. No SDK was configured in either benchmark process. The SDK is a
`dev` dependency; core depends only on `opentelemetry-api`. The lock was refreshed
with uv using cached metadata, without upgrading unrelated packages.

## Runtime contract

| Span | Boundary / parent |
| --- | --- |
| `invoke_agent` | Each run/resumption; caller's current span (including extracted W3C context) |
| `step` | Each turn; run span |
| `chat {model}` | Each physical primary/auxiliary provider delivery; step or compaction span |
| `execute_tool {name}` | Each tool execution; current turn; subagent runs inherit this span |
| `compaction` | Each actual compaction attempt; current turn/caller |

The histograms are `gen_ai.client.token.usage` (`{token}`, input/output split by
`gen_ai.token.type`), `gen_ai.client.operation.duration` (`s`, one per chat), and
`linch.store.duration` (`s`, attribute `op`). Store calls produce no spans.
Store timers cover loop append/checkpoint/event/batch/status/load/snapshot calls,
durable inbox commit/enqueue, and compaction skill-context persistence. All
`gen_ai.*`/`linch.*` telemetry keys are constants in `_telemetry.py`. Usage includes
input/output and cache read/creation buckets; spans carry operation, provider,
request/response model, finish reasons, conversation/tool identifiers, run ID,
turn index, resume marker and compaction strategy where applicable.

`error.type` and error status describe failures without exception messages or
exception events. Known paid usage survives parser failure. Prompt, assistant,
and tool arguments/results are captured only when
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Async iterator context
is restored before every consumer yield; close/cancellation finishes telemetry
before the session becomes idle. Providers/instruments are resolved through the
agent Context with an agent-local cache. Hosts own SDK shutdown. Host-job W3C
propagation is documented and tested; subagent task propagation is automatic.

## No-SDK benchmark

Run before changes and after final runtime fixes:

```bash
/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python scripts/benchmark_runtime.py --scale
```

All six cases completed correctly with no timeout. These are single observations
on the same machine, not a statistical estimate; scheduling/allocation/I/O noise
is visible. In particular, the 200-session memory case increased while the
1,000-session memory case decreased. There is no claim of a speed improvement.
SQLite cases remain in this worktree's pre-WP17 benchmark only; no SQLite code
was added or modified. [Machine-readable results](./otel-noop-benchmark.json)
include all measured deltas and correctness counts.

Latency in milliseconds:

| Case | Before p50 | After p50 | Delta | Before p99 | After p99 | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| memory_200x4 | 4590.97 | 5092.49 | +10.92% | 4748.51 | 5281.57 | +11.23% |
| memory_1000x4 | 25355.34 | 24823.20 | -2.10% | 25912.54 | 25691.64 | -0.85% |
| memory_1x50 | 1134.72 | 1117.03 | -1.56% | 1134.72 | 1117.03 | -1.56% |
| sqlite_200x4 | 6023.22 | 5867.90 | -2.58% | 8497.91 | 8259.75 | -2.80% |
| sqlite_1000x4 | 22854.22 | 23163.03 | +1.35% | 42472.50 | 43205.27 | +1.73% |
| sqlite_1x50 | 1887.61 | 1915.55 | +1.48% | 1887.61 | 1915.55 | +1.48% |

Other scale measurements, before → after:

| Case | Max loop lag (ms) | Peak RSS increase/session (KiB) | Checkpoint bytes/turn | Store writes/turn |
| --- | ---: | ---: | ---: | ---: |
| memory_200x4 | 844.10 → 899.19 | 136.80 → 139.28 | 18125.745 → 18125.700 | 16.50 → 16.50 |
| memory_1000x4 | 4308.35 → 4330.18 | 118.20 → 122.42 | 18136.750 → 18136.750 | 16.50 → 16.50 |
| memory_1x50 | 16.68 → 10.34 | 4264.00 → 4176.00 | 122852.760 → 122852.760 | 16.04 → 16.04 |
| sqlite_200x4 | 27.49 → 21.00 | 106.86 → 108.46 | 18114.755 → 18114.748 | 16.50 → 16.50 |
| sqlite_1000x4 | 44.42 → 43.10 | 32.54 → 32.69 | 18114.806 → 18114.882 | 16.50 → 16.50 |
| sqlite_1x50 | 20.23 → 16.29 | 6476.00 → 6148.00 | 122852.760 → 122852.760 | 16.04 → 16.04 |

## Surface, scope and verification

Public names added: none. Public names removed (16): `BaseObserver`,
`LoggingObserver`, `ObserverDispatcher`, `OpenTelemetryObserver`,
`ProviderCallInfo`, `ProviderCallResult`, `RunInfo`, `RunObserver`, `RunResultInfo`,
`RunTelemetryHook`, `Span`, `SpanCollector`, `ToolInfo`, `ToolResultInfo`,
`TurnInfo`, `normalize_observers`. Each removal and the removed package/extra/
example/parameter has a line in [the migration guide](../migration-3.0.md).
Native lifecycle hooks remain available; their stop result is now private.

Line delta against baseline: `src/linch` +733 / −1118 = **−385**;
`tests` +922 / −930 = **−8**. Changes cover dependency metadata/lock, the private
telemetry module, loop/provider/tool/compaction source calls, observer deletion,
SDK tests, examples, usage/migration/changelog and this measurement report.
`session.py` gained one private run-state field; `subagents/generator.py` changed
to cover its direct provider call. Shared `agent.py`/`__init__.py` changes are
limited to observability. WP17-owned paths are unchanged. No spec deviations.

Final gates using the prescribed venv and supplied Postgres DSN:

- `python -m pytest -q`: **2165 passed, 18 skipped**, 31.10 s. Postgres tests
  used a disposable schema. Unix-socket access required sandbox escalation.
- `ruff check .`: passed.
- `ruff format --check .`: passed, **412 files** formatted.
- `pyright`: **0 errors**, 3 existing environment warnings about resolving
  `jsonschema` source in `tools/base.py` and `tools/registry.py`.
- Focused telemetry/compaction coverage: **67 passed**, including **24 OTel
  tests**, with injected SDK exporters/readers and no global provider mutation.

Review findings about close errors, delayed public close, successful-call error
labels, scoped-provider cache inheritance, known usage after parse failure, and
unclosed auxiliary iterators were fixed with regression coverage. No deferred
material findings. The branch is retained for architecture-owner integration;
no push or protected-branch changes were made.

## Changed files (62)

- `CHANGELOG.md`
- `README.md`
- `docs/architecture/otel-noop-benchmark.json`
- `docs/architecture/otel-noop-benchmark.md`
- `docs/migration-3.0.md`
- `docs/superpowers/plans/2026-10-31-scale-otel.md`
- `docs/usage/README.md`
- `docs/usage/agent.md`
- `docs/usage/events.md`
- `docs/usage/examples.md`
- `docs/usage/hooks.md`
- `docs/usage/production.md`
- `examples/observability/custom_observer.py`
- `examples/observability/observability_agent.py`
- `pyproject.toml`
- `src/linch/__init__.py`
- `src/linch/_telemetry.py`
- `src/linch/agent.py`
- `src/linch/compaction.py`
- `src/linch/hooks/__init__.py`
- `src/linch/hooks/adapters.py`
- `src/linch/hooks/contexts.py`
- `src/linch/loop/checkpoint.py`
- `src/linch/loop/finalize.py`
- `src/linch/loop/provider_turn.py`
- `src/linch/loop/resume.py`
- `src/linch/loop/runner.py`
- `src/linch/loop/runtime.py`
- `src/linch/loop/startup.py`
- `src/linch/loop/streaming.py`
- `src/linch/loop/terminals.py`
- `src/linch/loop/tool_batch.py`
- `src/linch/model_calls.py`
- `src/linch/observability/__init__.py`
- `src/linch/observability/dispatcher.py`
- `src/linch/observability/otel.py`
- `src/linch/observability/protocol.py`
- `src/linch/observability/reference.py`
- `src/linch/scheduler.py`
- `src/linch/session.py`
- `src/linch/subagents/generator.py`
- `tests/loop/test_compaction_provider_agnostic.py`
- `tests/loop/test_compaction_response_chaining.py`
- `tests/loop/test_hardening.py`
- `tests/loop/test_phase_state_recovery.py`
- `tests/loop/test_run_resume.py`
- `tests/observability/conftest.py`
- `tests/observability/test_observability.py`
- `tests/observability/test_otel_propagation.py`
- `tests/observability/test_otel_runtime.py`
- `tests/observability/test_telemetry.py`
- `tests/test_compaction_accounting.py`
- `tests/test_hooks.py`
- `tests/test_public_api.py`
- `tests/tools/test_maximal_batching.py`
- `tests/tools/test_middleware.py`
- `tests/tools/test_pipeline.py`
- `tests/tools/test_scheduler_security_v2.py`
- `tests/tools/test_scheduler_tool_output_v2.py`
- `tests/tools/test_scheduler_v2.py`
- `tests/tools/test_tool_reliability.py`
- `uv.lock`
