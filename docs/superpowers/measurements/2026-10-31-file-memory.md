# WP16 file memory — scale observations

D12 implementation on `scale/file-memory`, based on `4e9ed7a`.
Compare with the [D10 baseline](2026-10-30-scale-baseline.md).

```bash
/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python scripts/benchmark_runtime.py \
  --scale --scale-case scale_memory_200x4 --scale-case scale_memory_1000x4 --json
```

Both bursts completed correctly: 1,200 runs, 4,800 provider turns, 4,800 tool
calls, and 1,200 overlapping first-read pairs; no errors or stderr.

| InMemory sessions × turns | p50 run ms | p99 run ms | Max loop lag ms | Peak RSS delta KiB/session | Checkpoint bytes/turn | Store writes/turn |
|---|---:|---:|---:|---:|---:|---:|
| 200 × 4 | 4,467.35 | 4,622.70 | 798.50 | 135.10 | 17,807.21 | 16.50 |
| 1,000 × 4 | 24,539.39 | 25,197.78 | 4,279.44 | 118.41 | 17,818.25 | 16.50 |

Against the recorded baseline, p50/p99 changed by −3.3%/−3.4% at 200 sessions
and −1.8%/−1.9% at 1,000 sessions. Maximum loop lag changed by −28.30 ms and
−30.14 ms; RSS delta grew by 8.98 and 4.35 KiB/session. Checkpoint bytes and
store calls per turn are unchanged. These single bursts on a shared machine
are observational; the baseline also predates intervening work packages, so
the differences cannot be attributed to WP16.

The standard scale cases leave optional file memory disabled. Its behavior is
covered separately with scoped concurrent runs: each active run reads at most
201 index lines once, injects the first 200 with a truncation notice, keeps the
prefix stable after file edits, and drops the cached index on termination.
The deleted keyword-memory search benchmark was removed with its store.
