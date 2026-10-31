# WP7 reliability measurements

Base: `8024c425ed10aa75d16474666a7ff249a2114683` on `scale/reliability`.
Python: the main checkout's `.venv/bin/python`, 3.11.14, Linux x86_64.
Postgres: the supplied local test DSN. All scale measurements acquire
`flock /tmp/wp8-cpu.lock`, shared with WP18.

The baseline source was exported with `git archive` into `/tmp/wp7-base`;
no other worktree or branch was changed. The original benchmark script runs
the `scale_memory_200x4` and `scale_postgres_200x4` cases with fake provider and
tool I/O. These are runtime overhead measurements, not model-quality evidence.

## Initial baseline

| Case | Runs/s | p50 run ms | p99 run ms | Max loop lag ms | Checkpoint bytes/turn | Writes/turn |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| memory 200x4 | 30.636 | 5707.48 | 5987.66 | 1051.99 | 8071.25 | 14.25 |
| Postgres 200x4 | 20.069 | 6730.75 | 9958.05 | 59.26 | 8071.25 | 14.25 |

Both cases completed 200/200 runs with correctness checks passing. Raw baseline
output: `/tmp/wp7-base-benchmark.json`.

## Final interleaved pairs

Measured source: `2b5f9b97e1f2560d01886d9f66e336861d39a16c`.
Per backend, the order was base/default/None/None/default/base, held under
one lock for the pair. Default and None were fresh `git archive` exports of
that commit. The None variant changed only the benchmark's four Agent
constructors to pass `reliability=None`.

| Case | Base 1 | Default 1 | None 1 | None 2 | Default 2 | Base 2 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| memory 200x4, runs/s | 46.786 | 48.416 | 47.012 | 47.108 | 47.636 | 45.469 |
| Postgres 200x4, runs/s | 32.126 | 31.819 | 31.917 | 31.280 | 31.805 | 32.102 |

Rates and latency quantiles below are arithmetic means of the two samples;
lag is the largest sampled lag. RSS is the mean peak RSS increase per session,
using the benchmark's existing import-floor/sampling method.

| Case | Policy | Runs/s | p50 ms | p99 ms | Max lag ms | RSS increase/session KiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| memory | base | 46.128 | 3757.48 | 3927.92 | 767.92 | 125.06 |
| memory | default | 48.026 | 3606.42 | 3794.59 | 729.08 | 114.37 |
| memory | None | 47.060 | 3693.49 | 3877.10 | 728.86 | 112.88 |
| Postgres | base | 32.114 | 4747.25 | 6215.30 | 28.64 | 156.99 |
| Postgres | default | 31.812 | 4768.73 | 6278.69 | 30.42 | 140.67 |
| Postgres | None | 31.598 | 4988.73 | 6314.38 | 19.00 | 135.89 |

Elapsed-time overhead is `mean(default elapsed) / mean(comparator elapsed) - 1`:

| Case | Default vs base | Default vs None |
| --- | ---: | ---: |
| memory | -3.97% | -2.01% |
| Postgres | +0.95% | -0.68% |

The final pairs meet the approximately 2% clean-run regression target. Negative
deltas from these small samples are not evidence of a speedup. Every sample
completed 200/200 runs and passed correctness checks. All variants retained
**14.25 store writes/turn** and **8071.25 checkpoint bytes/turn**. Postgres used
15.321–15.325 application/transaction round trips and 15.475–15.488 protocol
exchanges per turn.

The comparison was repeated because the first pair on `6f55082` was borderline:
memory default elapsed +1.38% vs base/+2.08% vs None; Postgres +2.46% vs base/
-0.03% vs None. Its mean runs/s (base/default/None) were 47.402/46.781/47.733
and 30.619/29.888/29.874. The final commit also skips success-qualification
inspection when no foreground observer exists. The sample variation does not
support attributing the whole difference to that small change. Absolute base
rates varied, so the paired comparisons, rather than the initial baseline,
determine the reported overhead.

Raw final results: `/tmp/wp7-pair.json`; initial pair:
`/tmp/wp7-pair-initial.json`; source-export paths:
`/tmp/wp7-bench-roots.json`. The task-local runner was
`/tmp/wp7-benchmark-pair.py`, invoked with the supplied DSN and:

```bash
flock /tmp/wp8-cpu.lock /home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python \
  /tmp/wp7-benchmark-pair.py
```

It runs each exported `scripts/benchmark_runtime.py --scale --scale-case
scale_memory_200x4 --json` (or `scale_postgres_200x4`) in the order above.

## Correctness and size

- Source: **+700 net lines** versus the base. Policy/runtime modules: 182;
  typed dispatcher: 117; finalization adapters: 64; shared API/loop/tool wiring:
  337. New test files: **1056 lines**; total test delta: +1059.
- The **32-case crash matrix** covers text-envelope, rejected-native,
  completion-text, and completion-final-tool corrections; memory and Postgres;
  before/after feedback append and checkpoint writes. Freshly loaded sessions
  match uninterrupted durable event traces, correction counts, usage, receipts,
  and private history. Only `PromptCacheAdvisoryEvent` is filtered; volatile
  fields use the WP8 normalization pattern.
- Serial and parallel-read batch restart tests verify that a current-batch
  success cannot unlock a prerequisite. Pre-execute denials settle before
  executable calls; executable side-effect order and provider result order stay
  intact. Eligible successful results and their checkpoint evidence commit
  together.
- Invalid text uses the existing accepted primary projection for a fixed
  assistant diagnostic plus user feedback. Native rejection uses its existing
  sanitized receipt. There is no new pending journal, parser version, or text
  rescue. A complete continuation/control proof distinguishes corrections from
  ordinary answers that happen to equal the diagnostic text.
- Explicit host early-success stops retain their documented precedence before
  model-final candidate dispatch; this interaction is documented in usage.
- Independent whole-branch review found two issues, both reproduced and fixed:
  the literal-diagnostic replay collision and validation of a provider-inferred
  terminal tool. The scoped re-review passed with no unresolved findings.

Final gates on the measured source:

- `python -m pytest -q` with the supplied Postgres DSN: **2332 passed,
  18 skipped**, 83.82 seconds.
- `ruff check .`: pass.
- `ruff format --check .`: pass, 424 files already formatted.
- `pyright --pythonpath /home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python`:
  **0 errors**, 5 existing `jsonschema`/`croniter` source-resolution warnings.

All commands used `/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/` from this
worktree. Raw logs: `/tmp/wp7-final-full-{pytest,ruff,format,pyright}.log`.
The ordinary subprocess backend test initially timed out inside the sandbox;
the unchanged test and both complete gate runs passed in the execution context
that permits the local Postgres socket and subprocess backend.
