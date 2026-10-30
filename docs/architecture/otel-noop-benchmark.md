# WP15: direct OpenTelemetry fast path and interleaved measurements

The owner review of `5dad741` found that the original single-run scale table
missed per-stream-item overhead. This report replaces that table with repeated,
interleaved measurements of the two review cases against `175ea48`.
The measured fix is `e99d25c8aa3d096b8d334074a5b00194b34659a9` on `scale/otel`.

## What changed

- `trace_run` and `in_span` are regular iterator factories. Invalid span contexts
  return the exact inner iterator, with no telemetry async-generator layer.
- `trace_chat` and `chat_stream` are deleted. Primary chat lifecycle and metrics
  use the existing `_accounted_primary_stream` loop and its completed parser
  state, including known usage after a parsing failure and actual response model.
  Chat completion precedes durable receipt settlement.
- Only `span.get_span_context().is_valid` permits activation. A valid sampled-out
  span still propagates; `is_recording()` gates content capture only.
- A metrics-only host still records operation duration and token usage at call
  completion. The default/no-op meter skips store timers and proxy records; the
  deferred global provider is checked again at call boundaries so late host setup
  can activate it. This uses the API package's deferred-provider type, without
  importing the SDK into core.
- Auxiliary deliveries use their existing normalized-response state. Generated
  definitions and custom/built-in standalone compaction retain instrumentation;
  the standalone facade also passes its raw iterator through when unconfigured.
  One shared activation helper owns context restoration and close/cancellation.

No public API or dependency changes were needed. Core remains API-only; hosts
own SDK providers and exporters, resolved through the agent Context. All
`gen_ai.*`/`linch.*` keys remain in the private telemetry module. Content remains
absent unless `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.

## Method

All variants use `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python` with
**3 warmups and 40 measured samples per case, per process**. Both baseline and
current no-SDK processes have the SDK installed but no configured SDK providers.
Each process runs both cases with the unchanged command:

```bash
/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python scripts/benchmark_runtime.py \
  --warmups 3 --samples 40 \
  --case no_tool_stream_200_deltas --case parallel_8_read_tools --json
```

Baseline source was extracted with `git archive 175ea48` into `/tmp`; no branch
was switched and no other worktree was edited. The six no-SDK processes ran in
order **B1, C1, C2, B2, B3, C3**, followed by three SDK processes. Code was held
fixed during this batch; before/after source fingerprints were identical:
`b4708b07b36830c82ecf5531252a2b2ae957dfba9169eaad7245adff35d8817e`.

The enabled variant injects a shared `TracerProvider`, `SimpleSpanProcessor`,
`InMemorySpanExporter`, `MeterProvider`, and `InMemoryMetricReader` through the
Agent Context. It uses no global provider setters and leaves content capture
off. This measures traces **and** metrics; there is no enabled-SDK performance
target. Each SDK process exported 688 spans across its warmup/measured runs and
observed all three histograms. All nine processes passed correctness checks.

These are three run medians of 40 samples each, in milliseconds. Every median
from the final committed-source batch is reported, in chronological order within
each variant:

| Case | Variant | Run 1 | Run 2 | Run 3 |
| --- | --- | ---: | ---: | ---: |
| `no_tool_stream_200_deltas` | 175ea48 baseline | 1.816768 | 1.860934 | 1.870779 |
| `no_tool_stream_200_deltas` | e99d25c no SDK | 1.864819 | 1.877485 | 1.843074 |
| `no_tool_stream_200_deltas` | e99d25c SDK | 3.050605 | 2.981250 | 3.025261 |
| `parallel_8_read_tools` | 175ea48 baseline | 9.922058 | 9.946050 | 9.928294 |
| `parallel_8_read_tools` | e99d25c no SDK | 10.111243 | 10.106555 | 10.055813 |
| `parallel_8_read_tools` | e99d25c SDK | 11.081938 | 11.023267 | 11.141828 |

The comparison statistic is the median of those three run medians:

| Case | Baseline (ms) | No SDK (ms) | No SDK vs baseline | SDK (ms) | SDK vs no SDK |
| --- | ---: | ---: | ---: | ---: | ---: |
| `no_tool_stream_200_deltas` | 1.860934 | 1.864819 | +0.209% | 3.025261 | +62.228% |
| `parallel_8_read_tools` | 9.928294 | 10.106555 | +1.795% | 11.081938 | +9.651% |

Both no-SDK cases meet the owner's **≤3%** target. Each corresponding B/C pair
also meets it; these are the paired changes in process order:

| Case | Pair 1 | Pair 2 | Pair 3 |
| --- | ---: | ---: | ---: |
| `no_tool_stream_200_deltas` | +2.645% | +0.889% | -1.481% |
| `parallel_8_read_tools` | +1.907% | +1.614% | +1.284% |

Small differences remain subject to normal scheduling/timer noise. No speedup is
claimed. The enabled numbers apply to the stated in-memory SDK setup, rather
than predicting a particular OTLP exporter/backend's cost.

[The measurement artifact](./otel-noop-benchmark.json) contains all 40-sample
payloads, run order, medians, correctness results, profile counts, and the exact
SDK harness source. To reproduce the enabled case, extract that harness:

```python
import json
from pathlib import Path

report = json.loads(Path("docs/architecture/otel-noop-benchmark.json").read_text())
Path("/tmp/linch-otel-benchmark.py").write_text(report["sdk_driver_source"])
```

```bash
/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python /tmp/linch-otel-benchmark.py \
  --root "$PWD" --warmups 3 --samples 40
```

## Non-timing evidence and verification

A no-SDK 200-delta run forbids `trace.use_span` in a regression test. Separate
identity assertions require raw iterator pass-through for invalid spans and
no-op runs. Metrics-only recording, valid sampled-out propagation, cancellation,
caller restoration, span trees, tenant isolation and content capture remain
covered. Custom standalone compaction is checked with and without tracing.

A cProfile run of the streaming case (3 warmups, 40 samples) has **0**
`trace.use_span` calls and **0** telemetry async-generator advances. It calls the
regular `in_span` factory 86 times: once per run and once per provider call,
independent of the 200 deltas in each call.

Gates on the committed implementation, using the supplied Postgres DSN:

- `python -m pytest -q`: **2172 passed, 18 skipped**, 30.98 s. Postgres test
  writes used a disposable schema; the supplied Unix socket needed sandbox access.
- `ruff check .`: passed.
- `ruff format --check .`: passed, **413 files**.
- `pyright`: **0 errors**, 3 pre-existing `jsonschema` source-resolution warnings
  in `tools/base.py` and `tools/registry.py`.
- Focused telemetry/compaction tests: **74 passed**, including **31 OTel tests**.

One exploratory run with an added 20-second timeout timed out in the unrelated
`tests/tools/test_builtin_search.py::test_grep_malformed_regex_rg_reports_error`;
it passed immediately in isolation and in the final required full gate.

## Surface and scope

Spans remain `invoke_agent`, `step`, `chat {model}`, `execute_tool {name}`, and
`compaction`. Histograms remain `gen_ai.client.token.usage` (`{token}`),
`gen_ai.client.operation.duration` (`s`), and `linch.store.duration` (`s`, `op`).
Store calls do not create spans. Host-job W3C propagation and subagent ancestry
are unchanged. SDK-backed usage/actual-model reads occur at call completion,
rather than on each primary-stream item.

This review revision changes four runtime files, two test files, and the
benchmark report/artifact, changelog and implementation plan. Shared `agent.py`
and `__init__.py`, dependency metadata, and all WP17-owned paths are unchanged.
Source line delta versus `5dad741`: **+288 / −216 = +72**; tests:
**+258 / −0 = +258**. Cumulative WP15 delta versus `175ea48`: source
**+807 / −1120 = −313**; tests **+1180 / −930 = +250**.

Public names added/removed in this revision: **0 / 0**. The original 16 removals
remain: `BaseObserver`, `LoggingObserver`, `ObserverDispatcher`,
`OpenTelemetryObserver`, `ProviderCallInfo`, `ProviderCallResult`, `RunInfo`,
`RunObserver`, `RunResultInfo`, `RunTelemetryHook`, `Span`, `SpanCollector`,
`ToolInfo`, `ToolResultInfo`, `TurnInfo`, `normalize_observers`. See
[the migration guide](../migration-3.0.md). No spec deviations; no push or
protected-branch changes.
