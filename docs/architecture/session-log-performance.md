# Session-log read performance

Use this page when evaluating projection-read cost or changing a
`SessionLog` reader. The public contract is a detached, immutable sequence of
mutable message graphs: callers may change a returned `Message` or content
block, but that change must not reach the log or a later read.

## Measurement

`SessionLog` eagerly maintains its authoritative entries, `full_history`, and
`provider_view` as messages and projections are appended. The projection
properties return tuples containing `deepcopy` results, so a full projection
read scales with the object graph being copied. `entries` has the same detached
snapshot behavior.

The cheap accessors avoid that whole-projection copy: `visible_count` and
`history_count` return the stored list lengths, while `last_visible()` copies
only the newest provider-view message. `Session.last_provider_message()` is
the corresponding session wrapper.

The following self-contained harness measures the current getters on the local
machine. It deliberately reports measurements instead of embedding a historical
table; Python version, hardware, allocator, and scheduler load all affect the
numbers.

<details>
<summary>Reproducible benchmark script</summary>

```bash
PYTHONPATH=src python - <<'PY'
from __future__ import annotations

import gc
import statistics
import time

from linch import Message, SessionLog, TextBlock, ToolResultBlock, ToolUseBlock


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


def timed(log: SessionLog, getter: str, repetitions: int) -> tuple[float, float]:
    properties = {"provider_view", "full_history", "entries"}
    read = (
        (lambda: getattr(log, getter))
        if getter in properties
        else (lambda: getattr(log, getter)())
    )
    read()
    gc.collect()
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter_ns()
        for _ in range(repetitions):
            if read() is None:
                raise AssertionError
        samples.append((time.perf_counter_ns() - started) / 1e6)
    median_ms = statistics.median(samples)
    return median_ms, min(samples)


for turns in (50, 200):
    log = build(turns)
    repetitions = 20 if turns == 200 else 50
    print(f"turns={turns} messages={log.history_count} repetitions={repetitions}")
    for getter in ("provider_view", "full_history", "entries", "last_visible"):
        median_ms, minimum_ms = timed(log, getter, repetitions)
        print(
            f"  {getter:16s} median={median_ms:.3f}ms "
            f"min={minimum_ms:.3f}ms per_read={median_ms / repetitions * 1000:.3f}us"
        )
PY
```

</details>

The benchmark is an observational tool, not a compatibility test. The
source-backed compatibility checks are in
`tests/session_log/test_session_log.py`: they mutate returned messages, blocks,
metadata, and projection entries and verify that later reads remain unchanged.
`tests/loop/test_history_reads.py` separately verifies that primary model-call
binding does not read `full_history` just to compute its durable identity.

## Why `deepcopy` remains the detached-read implementation

`Message.provider_metadata` and `ToolUseBlock.input` intentionally carry
`Any`-typed values, and the content-block union can grow. The current
implementation therefore uses `deepcopy` for ownership and detached reads,
which preserves the live Python object graph and does not introduce a second
serialization format or cache. A faster serialization-based cache would need a
new compatibility and memory contract; this page does not claim one.

## `SessionLog._history_fingerprint`

`_history_fingerprint()` is a private, lazy SHA-256 fold of the canonical JSON
array represented by the historical messages. It caches the hash prefix and the
number of historical messages already serialized, then serializes only the
unprocessed tail on the next demand. A visible-only message or a projection
entry does not change the audit digest. If serialization of a tail fails, the
previous valid prefix and cursor remain intact.

Primary model-call binding uses `history_count` and this fingerprint rather
than copying and re-fingerprinting the complete audit projection on every
binding. The focused fingerprint and read-count behavior is covered by
`tests/session_log/test_session_log.py` and `tests/loop/test_history_reads.py`.

## Reader inventory

The runtime readers of the projections are intentionally explicit:

- `src/linch/session.py` exposes `Session.provider_view`,
  `Session.full_history`, `Session.message_count`, and
  `Session.last_provider_message()`.
- `src/linch/session_log.py` implements `SessionLog.entries`,
  `SessionLog.full_history`, `SessionLog.provider_view`, the count accessors,
  `SessionLog.last_visible()`, and `_history_fingerprint()`.
- `src/linch/loop/request.py` builds requests from `provider_view`; context
  builders in `src/linch/hooks/adapters.py` receive a detached provider view.
- `src/linch/loop/terminals.py` scans a detached reversed provider view, while
  `src/linch/compaction.py` works on detached projection lists before recording
  a new projection.
- `src/linch/loop/finalize.py`, `src/linch/loop/feedback.py`, and
  `src/linch/loop/resume.py` inspect detached `full_history` snapshots for
  terminal and recovery decisions.
- `src/linch/loop/runtime.py` saves a provider-view snapshot and binds primary
  calls with `history_count` plus `_history_fingerprint()`.
- `src/linch/subagents/runner.py` seeds child sessions from a detached
  provider view. `src/linch/agent.py` and `src/linch/sessions/store.py` restore
  and persist the related provider-view snapshots.

These readers may transform their detached values, but they do not mutate the
log-owned message graphs. Durable `Session.append(...)` remains the session
boundary for persisted history; direct `SessionLog.append(...)` is an in-memory
operation.
