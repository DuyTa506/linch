# Session-log read performance

This note records the D6 measurement for `SessionLog` projection reads. The
public contract remains a detached, mutable-message snapshot: callers may
change a returned `Message` or content block, but that change must not reach the
log or a later read.

## Measurement

The benchmark ran with the repository virtual environment and the worktree
source path. The exact command and script are in the details block below.

The script builds 50 or 200 turns. Each turn has four messages (user prompt,
assistant text plus a tool call, tool result, and assistant final) with nested
metadata and representative text payloads. Each row is the median of five
timed samples; each sample repeats a getter 50 times for 50 turns and 20 times
for 200 turns. The reported value is the median sample divided by its inner
repetitions.

<details>
<summary>Exact benchmark command and script</summary>

```bash
PYTHONPATH=src /path/to/linch/.venv/bin/python - <<'PY'
from __future__ import annotations

import gc
import pickle
import statistics
import time

from linch.session_log import SessionLog
from linch.types import Message, TextBlock, ToolResultBlock, ToolUseBlock


def make_turn(turn: int) -> list[Message]:
    return [
        Message(
            role="user",
            content=[TextBlock(text=f"user prompt {turn}")],
            provider_metadata={"turn": turn, "nested": {"source": "client", "tags": list(range(4))}},
        ),
        Message(
            role="assistant",
            content=[
                TextBlock(text=f"thinking/result {turn}"),
                ToolUseBlock(
                    id=f"call-{turn}",
                    name="search",
                    input={"query": f"q{turn}", "limit": 5},
                ),
            ],
            provider_metadata={"request": {"id": f"req-{turn}", "latency_ms": 12.5}},
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=f"call-{turn}",
                    content=f"tool output for turn {turn}; " + ("x" * 120),
                )
            ],
            provider_metadata={"tool": "search", "status": "ok"},
        ),
        Message(
            role="assistant",
            content=[TextBlock(text=f"final answer {turn}; " + ("y" * 80))],
            provider_metadata={
                "finish": {
                    "stop_reason": "end_turn",
                    "usage": {"input": 100, "output": 20},
                }
            },
        ),
    ]


def build(turns: int) -> SessionLog:
    log = SessionLog()
    for turn in range(turns):
        log.append_many(make_turn(turn))
    return log


def timed(log: SessionLog, getter: str, reps: int) -> tuple[float, float]:
    property_getters = {"provider_view", "full_history", "entries"}
    if getter in property_getters:
        read = lambda: getattr(log, getter)
    else:
        read = lambda: getattr(log, getter)()
    read()
    gc.collect()
    samples: list[float] = []
    for _ in range(5):
        start = time.perf_counter_ns()
        for _ in range(reps):
            value = read()
            if value is None:
                raise AssertionError
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return statistics.median(samples), min(samples)


def timed_pickle(log: SessionLog, getter: str, reps: int) -> tuple[float, float]:
    value = {
        "provider_view": tuple(log._derived),  # type: ignore[attr-defined]
        "full_history": tuple(log._full),  # type: ignore[attr-defined]
        "entries": tuple(log._entries),  # type: ignore[attr-defined]
    }[getter]
    blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    samples: list[float] = []
    for _ in range(5):
        start = time.perf_counter_ns()
        for _ in range(reps):
            pickle.loads(blob)
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return statistics.median(samples), min(samples)


for turns in (50, 200):
    log = build(turns)
    reps = 20 if turns == 200 else 50
    print(f"turns={turns} messages={log.history_count} reps={reps}")
    for getter in (
        "provider_view",
        "full_history",
        "entries",
        "last_visible",
    ):
        median_ms, minimum_ms = timed(log, getter, reps)
        print(
            f"  {getter:16s} median={median_ms:.3f}ms "
            f"min={minimum_ms:.3f}ms per_read={median_ms / reps * 1000:.3f}us"
        )
    for getter in ("provider_view", "full_history", "entries"):
        median_ms, minimum_ms = timed_pickle(log, getter, reps)
        print(
            f"  pickle_{getter:11s} median={median_ms:.3f}ms "
            f"min={minimum_ms:.3f}ms per_read={median_ms / reps * 1000:.3f}us"
        )
PY
```

</details>

| Getter | 50 turns (200 messages) | 200 turns (800 messages) |
| --- | ---: | ---: |
| `provider_view` | 3,248 µs/read | 14,785 µs/read |
| `full_history` | 3,245 µs/read | 14,528 µs/read |
| `entries` | 4,626 µs/read | 19,932 µs/read |
| `last_visible()` | 14 µs/read | 14 µs/read |

The current implementation was retained, so the D6 runtime delta is 0. The
cheap count and last-message accessors continue to avoid a whole-projection
copy.

For comparison only, a prototype that pickled one snapshot once and called
`pickle.loads()` for every read measured 308 µs (`provider_view`) and 318 µs
(`full_history`) at 50 turns, and 1,294 µs and 1,311 µs at 200 turns. The
prototype is not the implementation: it did not include the type guard needed
for arbitrary metadata, and therefore is not a compatible replacement.

## Why the faster candidate was rejected

`Message.provider_metadata` and `ToolUseBlock.input` contain `Any`. A safe
pickle fast path would have to allow only exact built-in containers and every
current Linch content dataclass, then fall back to `deepcopy` for everything
else. That guard would need to reject subclasses with custom `__reduce__`,
`__getstate__`, or `__setstate__`; detect cycles in built-in containers; and be
updated whenever a new content-block type is added. Catching a pickle error is
not enough because reducer code can run before the error. It would also change
class identity after a module reload, while `deepcopy` preserves the existing
object type.

The cache would retain extra bytes per session. With the benchmark payload, the
provider and history snapshots were 31,597 bytes each at 50 turns and 126,456
bytes each at 200 turns; the entries snapshots were 34,440 and 137,708 bytes.
Caching all three therefore adds 390,620 bytes (about 381.5 KiB, or 391 kB
decimal) at 200 turns, before the canonical log graph. That changes the default
memory profile when
`max_idle_sessions` is unset. No compatible faster cache was demonstrated, so
the detached `deepcopy` projections remain the source-compatible choice.

## Incremental audit fingerprint (WP8)

The cached `_full` and `_derived` projections remain eager folds of log writes:
append adds the owned message to each selected view, and a projection entry
replaces the provider view. Getters still return detached deep copies. WP8
reduces redundant reads rather than changing that contract or adding a second
serialized snapshot cache.

The private audit fingerprint is a lazy SHA-256 fold of the exact canonical
JSON array previously hashed from `full_history`. It caches the hash prefix
without the closing bracket and the count of historical messages already
folded. A read serializes only the unprocessed historical tail, then closes a
copy of the hash to produce the same digest. Repeated reads without appends
need no message serialization. Provider-only messages and projection entries
do not change the audit digest.

Serialization stays lazy so appending arbitrary metadata does not acquire a
new JSON-encoding requirement. A failed tail fold leaves the valid cached
prefix and cursor unchanged. Primary model-call binding uses this digest and
`history_count` instead of copying and re-fingerprinting the whole audit view.
The focused two-provider-turn probe reduces whole-history reads from two to
zero; the four provider-view reads needed for request construction remain.
See the [WP8 measurements](../superpowers/measurements/2026-10-31-hot-path.md)
for scale results.

Checkpoint recovery also uses journal-backed model-call and loop-guard
watermarks instead of copying their growing receipt/call-count maps into every
checkpoint. Private loop-guard delta facts carry the recovery tail and require
the wire-version-3 codec; they are never yielded by the run iterator. They do
occupy durable event sequence numbers, so persisted reconnect cursors can
advance differently from the yielded event stream.

## Reader inventory

Runtime readers of `SessionLog` projections are grouped below. Readers that
need to transform a projection first materialize their own list or otherwise
operate on the detached return value; no production reader mutates log-owned
objects.

* `src/linch/session.py` exposes the `Session` wrappers and
  `last_provider_message()`.
* `src/linch/session_log.py` implements `entries`, `full_history`,
  `provider_view`, and `last_visible()`.
* `src/linch/loop/request.py` scans `provider_view` for skill reminders and
  copies it into `ProviderRequest.messages`.
* `src/linch/loop/runner.py` copies `provider_view` for durable provider
  snapshots.
* `src/linch/loop/runtime.py` binds primary model calls with `history_count`
  and the private incremental audit digest; it no longer reads `full_history`
  for that binding.
* `src/linch/loop/terminals.py` scans a detached reversed `provider_view` for
  the last assistant text.
* `src/linch/compaction.py` copies projections for micro/paid compaction,
  token estimates, source recipes, and recovery fingerprints. Compaction may
  mutate these detached working lists, never the log.
* `src/linch/hooks/adapters.py` passes a detached `provider_view` to context
  builders; file memory reads its run-scoped backend through the same context
  seam and does not inspect session history.
* `src/linch/subagents/runner.py` copies the parent `provider_view` to seed a
  child session.

These files read related but already-derived durable snapshots rather than
`SessionLog` getters: `src/linch/agent.py` restores a
`ProviderViewSnapshot`, `src/linch/sessions/store.py` serializes one, and
`src/linch/model_calls.py` receives the audit count and digest for primary
binding.
Field names and explanatory comments in `src/linch/loop/checkpoint.py`,
`src/linch/coordination/send_message.py`, and `src/linch/scheduler.py` are not
runtime getter reads.

Tests that exercise projection readers are in
`tests/context/test_context_builder.py`, `tests/filesystem/test_offload.py`,
`tests/loop/test_alignment_durability.py`,
`tests/loop/test_compaction_ladder.py`,
`tests/loop/test_compaction_provider_agnostic.py`,
`tests/loop/test_compaction_response_chaining.py`,
`tests/loop/test_model_call_accounting.py`, `tests/loop/test_run_resume.py`,
`tests/loop/test_verification.py`, `tests/mailbox/test_mailbox_tool.py`,
`tests/observability/test_observability.py`,
`tests/providers/test_structured_output.py`,
`tests/storage/test_model_call_journal.py`,
`tests/storage/test_provider_snapshot.py`,
`tests/storage/test_serialization_versioning.py`,
`tests/storage/test_session_fork.py`,
`tests/test_compaction_accounting.py`,
`tests/test_hooks.py`, `tests/test_session_log.py`, and
`tests/tools/test_subagent_tool.py`, `tests/test_idle_sessions.py`, and
`tests/tools/test_subagent_parallel.py`. The session-log tests deliberately mutate
returned messages, blocks, metadata, and projection entries to verify that the
log and later reads stay unchanged. Other tests either inspect, count,
serialize, or mutate their own detached working copies.

Examples `examples/core/deep_agent_resume.py` and
`examples/integrations/multi_agent_isolation.py` only inspect projection sizes;
`examples/tools/rag_tools.py` contains a text description. References in
`docs/architecture/`, `docs/usage/`, and the memory/tool-discovery superpowers
specs are documentation, not additional runtime readers.
