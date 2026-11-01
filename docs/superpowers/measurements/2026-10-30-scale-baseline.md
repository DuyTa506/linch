# Linch 3.0 scale baseline — 2026-10-30

Decision D10 / WP1. Runtime baseline: `584a012`; benchmark revision: `a80f6e831c593ace859214e43d82271f975c1c33`. No `src/linch` changes.

Machine: Intel Core i7-10750H @ 2.60 GHz, 12 logical CPUs; 16,163,348 KiB physical RAM; x86_64 Linux 6.8.0-138-generic, glibc 2.35; CPython 3.11.14. Shared developer machine; these are observational measurements, not performance gates.

Command (run from this worktree):

```bash
/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python scripts/benchmark_runtime.py --scale --json
```

Each case runs once in a fresh child process with one Agent and a shared session/run store pair. SQLite uses two databases in a temporary directory. Each session admits one run: two parallel reads (`rag_search`, `sql_query`), a second `rag_search`, `save_note`, then final text across four provider turns. The 50-turn cases extend the read sequence with distinct inputs. Provider and each tool sleep asynchronously for 2 ms. No network or credentials are used. All cases validate the exact provider/tool counts, successful final text, and overlapping first reads.

| Store | Sessions × turns | Completed | p50 run ms | p99 run ms | Runs/s | Lag p99 ms | Lag max ms | Peak RSS delta KiB/session | Checkpoint bytes/turn | Store writes/turn |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| memory | 200 × 4 | 200 | 4619.491 | 4785.456 | 37.77 | 826.804 | 826.804 | 126.12 | 17,807.21 | 16.50 |
| memory | 1000 × 4 | 1000 | 24976.841 | 25693.009 | 35.18 | 4309.585 | 4309.585 | 114.06 | 17,818.25 | 16.50 |
| sqlite | 200 × 4 | 200 | 6677.695 | 9568.210 | 20.89 | 8.578 | 22.504 | 91.52 | 17,796.39 | 16.50 |
| sqlite | 1000 × 4 | 1000 | 28248.873 | 58010.675 | 17.23 | 16.389 | 68.404 | 31.34 | 17,798.68 | 16.50 |
| memory | 1 × 50 | 1 | 1186.518 | 1186.518 | 0.84 | 16.533 | 16.533 | 3856.00 | 122,520.30 | 16.04 |
| sqlite | 1 × 50 | 1 | 1964.817 | 1964.817 | 0.51 | 15.446 | 21.134 | 5816.00 | 122,520.30 | 16.04 |

At 1,000 sessions, the memory case completed in 28.43 s with 4.31 s maximum loop lag; SQLite completed in 58.05 s with 68.4 ms maximum lag. The memory bursts produced only 9 (200 sessions) and 10 (1,000 sessions) lag observations, so their nearest-rank p99 equals the maximum. These are single instrumented bursts and do not establish a cause for the stalls or a production capacity limit.

Run latency includes session creation and one run; throughput is successful runs divided by wall time for the concurrent burst, excluding Agent construction/close and child interpreter startup. Percentiles use nearest rank. Lag is scheduling delay beyond a 10 ms timer deadline, sampled throughout the burst and once at completion; overdue periods are represented by one observation per wake, not synthetic catch-up samples.

RSS uses the current resident set before Agent/store construction, sampled current RSS during execution, and a new resource high-water mark when it exceeds the initial import high-water mark. The reported delta is the observed peak minus that current baseline, divided by configured sessions. It includes Agent/session caches, both stores, and benchmark instrumentation. Brief peaks below the import high-water mark may fall between current-RSS samples; this is a process RSS observation, not an allocation attribution or a memory bound.

Checkpoint bytes are the sum of UTF-8 JSON payload sizes (`checkpoint_to_dict` and `json.dumps(allow_nan=False)`, matching SQLite encoding), including terminal writes, divided by observed provider turns. Memory-store bytes are the same wire-size estimate; they are not disk bytes. Store writes count successful external mutation API calls to both stores, including session/run creation, event batches, checkpoint saves, and completion. Batches count once; internal method delegation is not counted again. These are not SQL statements, rows, fsyncs, or physical storage growth. Turn `-1` in the raw data is setup; its costs are included in the per-turn totals.

Instrumentation is enabled for all cases: checkpoint payload encoding adds synchronous work, and partial-progress JSON is written at most every 250 ms. These costs are included in run time and loop lag. No warmups or repeated bursts are used for the scale suite; the normal benchmark suite and its JSON schema remain separate.

Checkpoint growth for the single 50-turn run (zero-based provider turn; bytes include every checkpoint write on that turn):

| Store | Turn | Checkpoint writes | Total bytes | Mean bytes/checkpoint | Max bytes/checkpoint |
|---|---:|---:|---:|---:|---:|
| memory | 0 | 6 | 13,944 | 2,324.00 | 2,634 |
| memory | 9 | 6 | 54,375 | 9,062.50 | 9,474 |
| memory | 24 | 6 | 122,545 | 20,424.17 | 20,837 |
| memory | 48 | 6 | 231,516 | 38,586.00 | 39,002 |
| memory | 49 | 3 | 118,009 | 39,336.33 | 39,687 |
| sqlite | 0 | 6 | 13,944 | 2,324.00 | 2,634 |
| sqlite | 9 | 6 | 54,375 | 9,062.50 | 9,474 |
| sqlite | 24 | 6 | 122,545 | 20,424.17 | 20,837 |
| sqlite | 48 | 6 | 231,516 | 38,586.00 | 39,002 |
| sqlite | 49 | 3 | 118,009 | 39,336.33 | 39,687 |

Each child has a 600-second hard deadline enforced by the parent process. All six cases completed within that limit; no failed runs or tool errors were observed.

Validation: the latest full-suite gate completed with **1 failed, 1,869 passed, 26 skipped in 34.46 s**. The failure is the unchanged `tests/tools/test_execution_backend.py::test_local_backend_runs_command_returns_stdout`: an `echo hello` subprocess returned `timed_out=True` after its 10-second deadline. An earlier full-suite gate on the same benchmark revision passed (1,869 passed / 20 skipped in 24.09 s); separate diagnostic reruns also intermittently timed out `test_local_backend_nonzero_returncode`. All 22 benchmark tests passed in the focused run. This intermittent subprocess wakeup/timeout behavior is outside WP1; no `src/linch` or existing execution-backend test was changed. Ruff check and formatting passed (407 Python files); Pyright reported 0 errors / 3 pre-existing jsonschema import warnings. The six-case benchmark completed with no stderr or failed runs.

Raw JSON (unmodified CLI output):

```json
{
  "command": "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python scripts/benchmark_runtime.py --scale --json",
  "machine": {
    "cpu_count": 12,
    "machine": "x86_64",
    "platform": "Linux-6.8.0-138-generic-x86_64-with-glibc2.35",
    "python": "3.11.14 (main, Feb 12 2026, 00:42:50) [Clang 21.1.4 ]"
  },
  "metrics": {
    "scale_memory_1000x4": {
      "backend": "memory",
      "checkpoint_bytes": {
        "by_phase": {
          "assistant_appended": {
            "count": 4000,
            "max": 4882,
            "mean": 3812.5,
            "min": 2527,
            "total": 15250000
          },
          "completed": {
            "count": 1000,
            "max": 4481,
            "mean": 4481,
            "min": 4481,
            "total": 4481000
          },
          "provider_pending": {
            "count": 4000,
            "max": 4229,
            "mean": 3081,
            "min": 1580,
            "total": 12324000
          },
          "started": {
            "count": 1000,
            "max": 1179,
            "mean": 1179,
            "min": 1179,
            "total": 1179000
          },
          "tool_batch_pending": {
            "count": 3000,
            "max": 3686,
            "mean": 2983.6666666666665,
            "min": 2331,
            "total": 8951000
          },
          "tool_executing": {
            "count": 3000,
            "max": 3682,
            "mean": 2979.6666666666665,
            "min": 2327,
            "total": 8939000
          },
          "tool_results_appended": {
            "count": 3000,
            "max": 3798,
            "mean": 3135,
            "min": 2557,
            "total": 9405000
          },
          "turn_complete": {
            "count": 3000,
            "max": 3834,
            "mean": 3186.3333333333335,
            "min": 2637,
            "total": 9559000
          },
          "user_appended": {
            "count": 1000,
            "max": 1185,
            "mean": 1185,
            "min": 1185,
            "total": 1185000
          }
        },
        "by_turn": [
          {
            "count": 2000,
            "max": 1185,
            "mean": 1182,
            "min": 1179,
            "total": 2364000,
            "turn": -1
          },
          {
            "count": 6000,
            "max": 2637,
            "mean": 2326.5,
            "min": 1580,
            "total": 13959000,
            "turn": 0
          },
          {
            "count": 6000,
            "max": 3647,
            "mean": 3113.5,
            "min": 2930,
            "total": 18681000,
            "turn": 1
          },
          {
            "count": 6000,
            "max": 4194,
            "mean": 3779.5,
            "min": 3483,
            "total": 22677000,
            "turn": 2
          },
          {
            "count": 3000,
            "max": 4882,
            "mean": 4530.666666666667,
            "min": 4229,
            "total": 13592000,
            "turn": 3
          }
        ],
        "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; includes terminal checkpoint payloads; -1 is setup",
        "per_provider_turn": 17818.25,
        "total": 71273000
      },
      "child_command": [
        "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python",
        "/home/duy/.codex/worktrees/scale-bench-baseline/agent_kit/scripts/benchmark_runtime.py",
        "--scale-child",
        "{\"name\": \"scale_memory_1000x4\", \"backend\": \"memory\", \"sessions\": 1000, \"turns\": 4}",
        "--scale-progress",
        "/tmp/linch-scale-nesrbkwo/progress.json"
      ],
      "child_wall_s": 28.776034914000775,
      "completed": 1000,
      "correct": true,
      "elapsed_s": 28.425868071999503,
      "errors": [],
      "event_loop_lag_ms": {
        "interval": 10.0,
        "max": 4309.584687000097,
        "p99": 4309.584687000097,
        "samples": 10
      },
      "failed": 0,
      "name": "scale_memory_1000x4",
      "observed_provider_turns": 4000,
      "observed_tool_calls": 4000,
      "parallel_first_read_pairs": 1000,
      "pid": 6,
      "rss": {
        "baseline_current_bytes": 38764544,
        "import_high_water_floor_bytes": 0,
        "initial_high_water_bytes": 38633472,
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
        "peak_delta_bytes": 116797440,
        "peak_delta_bytes_per_session": 116797.44,
        "peak_high_water_bytes": 155287552,
        "peak_rss_bytes": 155561984,
        "sampled_peak_rss_bytes": 155561984
      },
      "run_latency_ms": {
        "p50": 24976.84098300124,
        "p99": 25693.008883999937,
        "samples": 1000,
        "scope": "session creation plus run; successful runs only"
      },
      "runs_per_second": 35.1792246930547,
      "sessions": 1000,
      "store_writes": {
        "attribution": "-1: setup; then latest provider_pending checkpoint turn; checkpoint calls use their own turn; cleanup uses final turn",
        "by_store": {
          "run": {
            "append_events": 33000,
            "create_run": 1000,
            "mark_completed": 1000,
            "save_checkpoint": 22000
          },
          "session": {
            "append_messages": 8000,
            "create": 1000
          }
        },
        "by_turn": [
          {
            "methods": {
              "run.append_events": 2000,
              "run.create_run": 1000,
              "run.save_checkpoint": 2000,
              "session.append_messages": 1000,
              "session.create": 1000
            },
            "total": 7000,
            "turn": -1
          },
          {
            "methods": {
              "run.append_events": 10000,
              "run.save_checkpoint": 6000,
              "session.append_messages": 2000
            },
            "total": 18000,
            "turn": 0
          },
          {
            "methods": {
              "run.append_events": 8000,
              "run.save_checkpoint": 6000,
              "session.append_messages": 2000
            },
            "total": 16000,
            "turn": 1
          },
          {
            "methods": {
              "run.append_events": 8000,
              "run.save_checkpoint": 6000,
              "session.append_messages": 2000
            },
            "total": 16000,
            "turn": 2
          },
          {
            "methods": {
              "run.append_events": 5000,
              "run.mark_completed": 1000,
              "run.save_checkpoint": 2000,
              "session.append_messages": 1000
            },
            "total": 9000,
            "turn": 3
          }
        ],
        "per_provider_turn": 16.5,
        "total": 66000,
        "unit": "successful external mutation API calls, not SQL statements or rows"
      },
      "timed_out": false,
      "timeout_s": 600,
      "turns": 4
    },
    "scale_memory_1x50": {
      "backend": "memory",
      "checkpoint_bytes": {
        "by_phase": {
          "assistant_appended": {
            "count": 50,
            "max": 39687,
            "mean": 21215.02,
            "min": 2524,
            "total": 1060751
          },
          "completed": {
            "count": 1,
            "max": 39284,
            "mean": 39284,
            "min": 39284,
            "total": 39284
          },
          "provider_pending": {
            "count": 50,
            "max": 39038,
            "mean": 20501.82,
            "min": 1580,
            "total": 1025091
          },
          "started": {
            "count": 1,
            "max": 1179,
            "mean": 1179,
            "min": 1179,
            "total": 1179
          },
          "tool_batch_pending": {
            "count": 49,
            "max": 38490,
            "mean": 20331.85714285714,
            "min": 2328,
            "total": 996261
          },
          "tool_executing": {
            "count": 49,
            "max": 38486,
            "mean": 20327.85714285714,
            "min": 2324,
            "total": 996065
          },
          "tool_results_appended": {
            "count": 49,
            "max": 38604,
            "mean": 20451.61224489796,
            "min": 2554,
            "total": 1002129
          },
          "turn_complete": {
            "count": 49,
            "max": 38641,
            "mean": 20491.224489795917,
            "min": 2634,
            "total": 1004070
          },
          "user_appended": {
            "count": 1,
            "max": 1185,
            "mean": 1185,
            "min": 1185,
            "total": 1185
          }
        },
        "by_turn": [
          {
            "count": 2,
            "max": 1185,
            "mean": 1182,
            "min": 1179,
            "total": 2364,
            "turn": -1
          },
          {
            "count": 6,
            "max": 2634,
            "mean": 2324,
            "min": 1580,
            "total": 13944,
            "turn": 0
          },
          {
            "count": 6,
            "max": 3641,
            "mean": 3108,
            "min": 2924,
            "total": 18648,
            "turn": 1
          },
          {
            "count": 6,
            "max": 4188,
            "mean": 3777.1666666666665,
            "min": 3477,
            "total": 22663,
            "turn": 2
          },
          {
            "count": 6,
            "max": 4943,
            "mean": 4532.166666666667,
            "min": 4232,
            "total": 27193,
            "turn": 3
          },
          {
            "count": 6,
            "max": 5698,
            "mean": 5287.166666666667,
            "min": 4987,
            "total": 31723,
            "turn": 4
          },
          {
            "count": 6,
            "max": 6454,
            "mean": 6042.5,
            "min": 5743,
            "total": 36255,
            "turn": 5
          },
          {
            "count": 6,
            "max": 7209,
            "mean": 6797.5,
            "min": 6498,
            "total": 40785,
            "turn": 6
          },
          {
            "count": 6,
            "max": 7964,
            "mean": 7552.5,
            "min": 7253,
            "total": 45315,
            "turn": 7
          },
          {
            "count": 6,
            "max": 8719,
            "mean": 8307.5,
            "min": 8008,
            "total": 49845,
            "turn": 8
          },
          {
            "count": 6,
            "max": 9474,
            "mean": 9062.5,
            "min": 8763,
            "total": 54375,
            "turn": 9
          },
          {
            "count": 6,
            "max": 10234,
            "mean": 9824.166666666666,
            "min": 9520,
            "total": 58945,
            "turn": 10
          },
          {
            "count": 6,
            "max": 10996,
            "mean": 10583.166666666666,
            "min": 10284,
            "total": 63499,
            "turn": 11
          },
          {
            "count": 6,
            "max": 11753,
            "mean": 11340.166666666666,
            "min": 11041,
            "total": 68041,
            "turn": 12
          },
          {
            "count": 6,
            "max": 12510,
            "mean": 12097.166666666666,
            "min": 11798,
            "total": 72583,
            "turn": 13
          },
          {
            "count": 6,
            "max": 13267,
            "mean": 12854.166666666666,
            "min": 12555,
            "total": 77125,
            "turn": 14
          },
          {
            "count": 6,
            "max": 14024,
            "mean": 13611.166666666666,
            "min": 13312,
            "total": 81667,
            "turn": 15
          },
          {
            "count": 6,
            "max": 14781,
            "mean": 14368.166666666666,
            "min": 14069,
            "total": 86209,
            "turn": 16
          },
          {
            "count": 6,
            "max": 15538,
            "mean": 15125.166666666666,
            "min": 14826,
            "total": 90751,
            "turn": 17
          },
          {
            "count": 6,
            "max": 16295,
            "mean": 15882.166666666666,
            "min": 15583,
            "total": 95293,
            "turn": 18
          },
          {
            "count": 6,
            "max": 17052,
            "mean": 16639.166666666668,
            "min": 16340,
            "total": 99835,
            "turn": 19
          },
          {
            "count": 6,
            "max": 17809,
            "mean": 17396.166666666668,
            "min": 17097,
            "total": 104377,
            "turn": 20
          },
          {
            "count": 6,
            "max": 18566,
            "mean": 18153.166666666668,
            "min": 17854,
            "total": 108919,
            "turn": 21
          },
          {
            "count": 6,
            "max": 19323,
            "mean": 18910.166666666668,
            "min": 18611,
            "total": 113461,
            "turn": 22
          },
          {
            "count": 6,
            "max": 20080,
            "mean": 19667.166666666668,
            "min": 19368,
            "total": 118003,
            "turn": 23
          },
          {
            "count": 6,
            "max": 20837,
            "mean": 20424.166666666668,
            "min": 20125,
            "total": 122545,
            "turn": 24
          },
          {
            "count": 6,
            "max": 21594,
            "mean": 21181.166666666668,
            "min": 20882,
            "total": 127087,
            "turn": 25
          },
          {
            "count": 6,
            "max": 22351,
            "mean": 21938.166666666668,
            "min": 21639,
            "total": 131629,
            "turn": 26
          },
          {
            "count": 6,
            "max": 23108,
            "mean": 22695.166666666668,
            "min": 22396,
            "total": 136171,
            "turn": 27
          },
          {
            "count": 6,
            "max": 23865,
            "mean": 23452.166666666668,
            "min": 23153,
            "total": 140713,
            "turn": 28
          },
          {
            "count": 6,
            "max": 24622,
            "mean": 24209.166666666668,
            "min": 23910,
            "total": 145255,
            "turn": 29
          },
          {
            "count": 6,
            "max": 25379,
            "mean": 24966.166666666668,
            "min": 24667,
            "total": 149797,
            "turn": 30
          },
          {
            "count": 6,
            "max": 26136,
            "mean": 25723.166666666668,
            "min": 25424,
            "total": 154339,
            "turn": 31
          },
          {
            "count": 6,
            "max": 26893,
            "mean": 26480.166666666668,
            "min": 26181,
            "total": 158881,
            "turn": 32
          },
          {
            "count": 6,
            "max": 27650,
            "mean": 27237.166666666668,
            "min": 26938,
            "total": 163423,
            "turn": 33
          },
          {
            "count": 6,
            "max": 28407,
            "mean": 27994.166666666668,
            "min": 27695,
            "total": 167965,
            "turn": 34
          },
          {
            "count": 6,
            "max": 29164,
            "mean": 28751.166666666668,
            "min": 28452,
            "total": 172507,
            "turn": 35
          },
          {
            "count": 6,
            "max": 29921,
            "mean": 29508.166666666668,
            "min": 29209,
            "total": 177049,
            "turn": 36
          },
          {
            "count": 6,
            "max": 30678,
            "mean": 30265.166666666668,
            "min": 29966,
            "total": 181591,
            "turn": 37
          },
          {
            "count": 6,
            "max": 31435,
            "mean": 31022.166666666668,
            "min": 30723,
            "total": 186133,
            "turn": 38
          },
          {
            "count": 6,
            "max": 32192,
            "mean": 31779.166666666668,
            "min": 31480,
            "total": 190675,
            "turn": 39
          },
          {
            "count": 6,
            "max": 32949,
            "mean": 32536.166666666668,
            "min": 32237,
            "total": 195217,
            "turn": 40
          },
          {
            "count": 6,
            "max": 33706,
            "mean": 33293.166666666664,
            "min": 32994,
            "total": 199759,
            "turn": 41
          },
          {
            "count": 6,
            "max": 34463,
            "mean": 34050.166666666664,
            "min": 33751,
            "total": 204301,
            "turn": 42
          },
          {
            "count": 6,
            "max": 35220,
            "mean": 34807.166666666664,
            "min": 34508,
            "total": 208843,
            "turn": 43
          },
          {
            "count": 6,
            "max": 35977,
            "mean": 35564.166666666664,
            "min": 35265,
            "total": 213385,
            "turn": 44
          },
          {
            "count": 6,
            "max": 36734,
            "mean": 36321.166666666664,
            "min": 36022,
            "total": 217927,
            "turn": 45
          },
          {
            "count": 6,
            "max": 37491,
            "mean": 37078.166666666664,
            "min": 36779,
            "total": 222469,
            "turn": 46
          },
          {
            "count": 6,
            "max": 38248,
            "mean": 37835.166666666664,
            "min": 37536,
            "total": 227011,
            "turn": 47
          },
          {
            "count": 6,
            "max": 39002,
            "mean": 38586,
            "min": 38293,
            "total": 231516,
            "turn": 48
          },
          {
            "count": 3,
            "max": 39687,
            "mean": 39336.333333333336,
            "min": 39038,
            "total": 118009,
            "turn": 49
          }
        ],
        "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; includes terminal checkpoint payloads; -1 is setup",
        "per_provider_turn": 122520.3,
        "total": 6126015
      },
      "child_command": [
        "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python",
        "/home/duy/.codex/worktrees/scale-bench-baseline/agent_kit/scripts/benchmark_runtime.py",
        "--scale-child",
        "{\"name\": \"scale_memory_1x50\", \"backend\": \"memory\", \"sessions\": 1, \"turns\": 50}",
        "--scale-progress",
        "/tmp/linch-scale-bpx74ers/progress.json"
      ],
      "child_wall_s": 1.4406760630008648,
      "completed": 1,
      "correct": true,
      "elapsed_s": 1.1866220230003819,
      "errors": [],
      "event_loop_lag_ms": {
        "interval": 10.0,
        "max": 16.532875000848435,
        "p99": 16.532875000848435,
        "samples": 85
      },
      "failed": 0,
      "name": "scale_memory_1x50",
      "observed_provider_turns": 50,
      "observed_tool_calls": 50,
      "parallel_first_read_pairs": 1,
      "pid": 7,
      "rss": {
        "baseline_current_bytes": 38805504,
        "import_high_water_floor_bytes": 0,
        "initial_high_water_bytes": 38625280,
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
        "peak_delta_bytes": 3948544,
        "peak_delta_bytes_per_session": 3948544.0,
        "peak_high_water_bytes": 42557440,
        "peak_rss_bytes": 42754048,
        "sampled_peak_rss_bytes": 42754048
      },
      "run_latency_ms": {
        "p50": 1186.518120999608,
        "p99": 1186.518120999608,
        "samples": 1,
        "scope": "session creation plus run; successful runs only"
      },
      "runs_per_second": 0.8427283335526616,
      "sessions": 1,
      "store_writes": {
        "attribution": "-1: setup; then latest provider_pending checkpoint turn; checkpoint calls use their own turn; cleanup uses final turn",
        "by_store": {
          "run": {
            "append_events": 401,
            "create_run": 1,
            "mark_completed": 1,
            "save_checkpoint": 298
          },
          "session": {
            "append_messages": 100,
            "create": 1
          }
        },
        "by_turn": [
          {
            "methods": {
              "run.append_events": 2,
              "run.create_run": 1,
              "run.save_checkpoint": 2,
              "session.append_messages": 1,
              "session.create": 1
            },
            "total": 7,
            "turn": -1
          },
          {
            "methods": {
              "run.append_events": 10,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 18,
            "turn": 0
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 1
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 2
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 3
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 4
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 5
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 6
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 7
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 8
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 9
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 10
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 11
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 12
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 13
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 14
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 15
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 16
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 17
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 18
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 19
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 20
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 21
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 22
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 23
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 24
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 25
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 26
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 27
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 28
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 29
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 30
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 31
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 32
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 33
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 34
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 35
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 36
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 37
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 38
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 39
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 40
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 41
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 42
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 43
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 44
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 45
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 46
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 47
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 48
          },
          {
            "methods": {
              "run.append_events": 5,
              "run.mark_completed": 1,
              "run.save_checkpoint": 2,
              "session.append_messages": 1
            },
            "total": 9,
            "turn": 49
          }
        ],
        "per_provider_turn": 16.04,
        "total": 802,
        "unit": "successful external mutation API calls, not SQL statements or rows"
      },
      "timed_out": false,
      "timeout_s": 600,
      "turns": 50
    },
    "scale_memory_200x4": {
      "backend": "memory",
      "checkpoint_bytes": {
        "by_phase": {
          "assistant_appended": {
            "count": 800,
            "max": 4878,
            "mean": 3809.9875,
            "min": 2526,
            "total": 3047990
          },
          "completed": {
            "count": 200,
            "max": 4477,
            "mean": 4476.97,
            "min": 4473,
            "total": 895394
          },
          "provider_pending": {
            "count": 800,
            "max": 4226,
            "mean": 3079.495,
            "min": 1580,
            "total": 2463596
          },
          "started": {
            "count": 200,
            "max": 1179,
            "mean": 1179,
            "min": 1179,
            "total": 235800
          },
          "tool_batch_pending": {
            "count": 600,
            "max": 3683,
            "mean": 2981.66,
            "min": 2330,
            "total": 1788996
          },
          "tool_executing": {
            "count": 600,
            "max": 3679,
            "mean": 2977.66,
            "min": 2326,
            "total": 1786596
          },
          "tool_results_appended": {
            "count": 600,
            "max": 3795,
            "mean": 3132.9933333333333,
            "min": 2556,
            "total": 1879796
          },
          "turn_complete": {
            "count": 600,
            "max": 3831,
            "mean": 3184.326666666667,
            "min": 2636,
            "total": 1910596
          },
          "user_appended": {
            "count": 200,
            "max": 1185,
            "mean": 1185,
            "min": 1185,
            "total": 237000
          }
        },
        "by_turn": [
          {
            "count": 400,
            "max": 1185,
            "mean": 1182,
            "min": 1179,
            "total": 472800,
            "turn": -1
          },
          {
            "count": 1200,
            "max": 2636,
            "mean": 2325.6666666666665,
            "min": 1580,
            "total": 2790800,
            "turn": 0
          },
          {
            "count": 1200,
            "max": 3645,
            "mean": 3111.6583333333333,
            "min": 2926,
            "total": 3733990,
            "turn": 1
          },
          {
            "count": 1200,
            "max": 4191,
            "mean": 3776.6566666666668,
            "min": 3479,
            "total": 4531988,
            "turn": 2
          },
          {
            "count": 600,
            "max": 4878,
            "mean": 4526.9766666666665,
            "min": 4224,
            "total": 2716186,
            "turn": 3
          }
        ],
        "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; includes terminal checkpoint payloads; -1 is setup",
        "per_provider_turn": 17807.205,
        "total": 14245764
      },
      "child_command": [
        "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python",
        "/home/duy/.codex/worktrees/scale-bench-baseline/agent_kit/scripts/benchmark_runtime.py",
        "--scale-child",
        "{\"name\": \"scale_memory_200x4\", \"backend\": \"memory\", \"sessions\": 200, \"turns\": 4}",
        "--scale-progress",
        "/tmp/linch-scale-0b243s4h/progress.json"
      ],
      "child_wall_s": 5.5453232179988845,
      "completed": 200,
      "correct": true,
      "elapsed_s": 5.2948917930007156,
      "errors": [],
      "event_loop_lag_ms": {
        "interval": 10.0,
        "max": 826.8043360003503,
        "p99": 826.8043360003503,
        "samples": 9
      },
      "failed": 0,
      "name": "scale_memory_200x4",
      "observed_provider_turns": 800,
      "observed_tool_calls": 800,
      "parallel_first_read_pairs": 200,
      "pid": 5,
      "rss": {
        "baseline_current_bytes": 38699008,
        "import_high_water_floor_bytes": 0,
        "initial_high_water_bytes": 38633472,
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
        "peak_delta_bytes": 25829376,
        "peak_delta_bytes_per_session": 129146.88,
        "peak_high_water_bytes": 64454656,
        "peak_rss_bytes": 64528384,
        "sampled_peak_rss_bytes": 64528384
      },
      "run_latency_ms": {
        "p50": 4619.490683000549,
        "p99": 4785.456024999803,
        "samples": 200,
        "scope": "session creation plus run; successful runs only"
      },
      "runs_per_second": 37.77225443291943,
      "sessions": 200,
      "store_writes": {
        "attribution": "-1: setup; then latest provider_pending checkpoint turn; checkpoint calls use their own turn; cleanup uses final turn",
        "by_store": {
          "run": {
            "append_events": 6600,
            "create_run": 200,
            "mark_completed": 200,
            "save_checkpoint": 4400
          },
          "session": {
            "append_messages": 1600,
            "create": 200
          }
        },
        "by_turn": [
          {
            "methods": {
              "run.append_events": 400,
              "run.create_run": 200,
              "run.save_checkpoint": 400,
              "session.append_messages": 200,
              "session.create": 200
            },
            "total": 1400,
            "turn": -1
          },
          {
            "methods": {
              "run.append_events": 2000,
              "run.save_checkpoint": 1200,
              "session.append_messages": 400
            },
            "total": 3600,
            "turn": 0
          },
          {
            "methods": {
              "run.append_events": 1600,
              "run.save_checkpoint": 1200,
              "session.append_messages": 400
            },
            "total": 3200,
            "turn": 1
          },
          {
            "methods": {
              "run.append_events": 1600,
              "run.save_checkpoint": 1200,
              "session.append_messages": 400
            },
            "total": 3200,
            "turn": 2
          },
          {
            "methods": {
              "run.append_events": 1000,
              "run.mark_completed": 200,
              "run.save_checkpoint": 400,
              "session.append_messages": 200
            },
            "total": 1800,
            "turn": 3
          }
        ],
        "per_provider_turn": 16.5,
        "total": 13200,
        "unit": "successful external mutation API calls, not SQL statements or rows"
      },
      "timed_out": false,
      "timeout_s": 600,
      "turns": 4
    },
    "scale_sqlite_1000x4": {
      "backend": "sqlite",
      "checkpoint_bytes": {
        "by_phase": {
          "assistant_appended": {
            "count": 4000,
            "max": 4878,
            "mean": 3808.058,
            "min": 2524,
            "total": 15232232
          },
          "completed": {
            "count": 1000,
            "max": 4477,
            "mean": 4473.918,
            "min": 4473,
            "total": 4473918
          },
          "provider_pending": {
            "count": 4000,
            "max": 4226,
            "mean": 3078.3285,
            "min": 1580,
            "total": 12313314
          },
          "started": {
            "count": 1000,
            "max": 1179,
            "mean": 1179,
            "min": 1179,
            "total": 1179000
          },
          "tool_batch_pending": {
            "count": 3000,
            "max": 3683,
            "mean": 2980.1046666666666,
            "min": 2328,
            "total": 8940314
          },
          "tool_executing": {
            "count": 3000,
            "max": 3679,
            "mean": 2976.1046666666666,
            "min": 2324,
            "total": 8928314
          },
          "tool_results_appended": {
            "count": 3000,
            "max": 3795,
            "mean": 3131.438,
            "min": 2554,
            "total": 9394314
          },
          "turn_complete": {
            "count": 3000,
            "max": 3831,
            "mean": 3182.771333333333,
            "min": 2634,
            "total": 9548314
          },
          "user_appended": {
            "count": 1000,
            "max": 1185,
            "mean": 1185,
            "min": 1185,
            "total": 1185000
          }
        },
        "by_turn": [
          {
            "count": 2000,
            "max": 1185,
            "mean": 1182,
            "min": 1179,
            "total": 2364000,
            "turn": -1
          },
          {
            "count": 6000,
            "max": 2636,
            "mean": 2324.9991666666665,
            "min": 1580,
            "total": 13949995,
            "turn": 0
          },
          {
            "count": 6000,
            "max": 3645,
            "mean": 3110.2365,
            "min": 2925,
            "total": 18661419,
            "turn": 1
          },
          {
            "count": 6000,
            "max": 4191,
            "mean": 3774.4665,
            "min": 3478,
            "total": 22646799,
            "turn": 2
          },
          {
            "count": 3000,
            "max": 4878,
            "mean": 4524.169,
            "min": 4222,
            "total": 13572507,
            "turn": 3
          }
        ],
        "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; includes terminal checkpoint payloads; -1 is setup",
        "per_provider_turn": 17798.68,
        "total": 71194720
      },
      "child_command": [
        "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python",
        "/home/duy/.codex/worktrees/scale-bench-baseline/agent_kit/scripts/benchmark_runtime.py",
        "--scale-child",
        "{\"name\": \"scale_sqlite_1000x4\", \"backend\": \"sqlite\", \"sessions\": 1000, \"turns\": 4}",
        "--scale-progress",
        "/tmp/linch-scale-a93oubig/progress.json"
      ],
      "child_wall_s": 58.40311724499952,
      "completed": 1000,
      "correct": true,
      "elapsed_s": 58.05031041099937,
      "errors": [],
      "event_loop_lag_ms": {
        "interval": 10.0,
        "max": 68.40407500021684,
        "p99": 16.389452999646892,
        "samples": 4400
      },
      "failed": 0,
      "name": "scale_sqlite_1000x4",
      "observed_provider_turns": 4000,
      "observed_tool_calls": 4000,
      "parallel_first_read_pairs": 1000,
      "pid": 13211,
      "rss": {
        "baseline_current_bytes": 38862848,
        "import_high_water_floor_bytes": 0,
        "initial_high_water_bytes": 38633472,
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
        "peak_delta_bytes": 32088064,
        "peak_delta_bytes_per_session": 32088.064,
        "peak_high_water_bytes": 70950912,
        "peak_rss_bytes": 70950912,
        "sampled_peak_rss_bytes": 70795264
      },
      "run_latency_ms": {
        "p50": 28248.87344000126,
        "p99": 58010.67543799945,
        "samples": 1000,
        "scope": "session creation plus run; successful runs only"
      },
      "runs_per_second": 17.226436739441105,
      "sessions": 1000,
      "store_writes": {
        "attribution": "-1: setup; then latest provider_pending checkpoint turn; checkpoint calls use their own turn; cleanup uses final turn",
        "by_store": {
          "run": {
            "append_events": 33000,
            "create_run": 1000,
            "mark_completed": 1000,
            "save_checkpoint": 22000
          },
          "session": {
            "append_messages": 8000,
            "create": 1000
          }
        },
        "by_turn": [
          {
            "methods": {
              "run.append_events": 2000,
              "run.create_run": 1000,
              "run.save_checkpoint": 2000,
              "session.append_messages": 1000,
              "session.create": 1000
            },
            "total": 7000,
            "turn": -1
          },
          {
            "methods": {
              "run.append_events": 10000,
              "run.save_checkpoint": 6000,
              "session.append_messages": 2000
            },
            "total": 18000,
            "turn": 0
          },
          {
            "methods": {
              "run.append_events": 8000,
              "run.save_checkpoint": 6000,
              "session.append_messages": 2000
            },
            "total": 16000,
            "turn": 1
          },
          {
            "methods": {
              "run.append_events": 8000,
              "run.save_checkpoint": 6000,
              "session.append_messages": 2000
            },
            "total": 16000,
            "turn": 2
          },
          {
            "methods": {
              "run.append_events": 5000,
              "run.mark_completed": 1000,
              "run.save_checkpoint": 2000,
              "session.append_messages": 1000
            },
            "total": 9000,
            "turn": 3
          }
        ],
        "per_provider_turn": 16.5,
        "total": 66000,
        "unit": "successful external mutation API calls, not SQL statements or rows"
      },
      "timed_out": false,
      "timeout_s": 600,
      "turns": 4
    },
    "scale_sqlite_1x50": {
      "backend": "sqlite",
      "checkpoint_bytes": {
        "by_phase": {
          "assistant_appended": {
            "count": 50,
            "max": 39687,
            "mean": 21215.02,
            "min": 2524,
            "total": 1060751
          },
          "completed": {
            "count": 1,
            "max": 39284,
            "mean": 39284,
            "min": 39284,
            "total": 39284
          },
          "provider_pending": {
            "count": 50,
            "max": 39038,
            "mean": 20501.82,
            "min": 1580,
            "total": 1025091
          },
          "started": {
            "count": 1,
            "max": 1179,
            "mean": 1179,
            "min": 1179,
            "total": 1179
          },
          "tool_batch_pending": {
            "count": 49,
            "max": 38490,
            "mean": 20331.85714285714,
            "min": 2328,
            "total": 996261
          },
          "tool_executing": {
            "count": 49,
            "max": 38486,
            "mean": 20327.85714285714,
            "min": 2324,
            "total": 996065
          },
          "tool_results_appended": {
            "count": 49,
            "max": 38604,
            "mean": 20451.61224489796,
            "min": 2554,
            "total": 1002129
          },
          "turn_complete": {
            "count": 49,
            "max": 38641,
            "mean": 20491.224489795917,
            "min": 2634,
            "total": 1004070
          },
          "user_appended": {
            "count": 1,
            "max": 1185,
            "mean": 1185,
            "min": 1185,
            "total": 1185
          }
        },
        "by_turn": [
          {
            "count": 2,
            "max": 1185,
            "mean": 1182,
            "min": 1179,
            "total": 2364,
            "turn": -1
          },
          {
            "count": 6,
            "max": 2634,
            "mean": 2324,
            "min": 1580,
            "total": 13944,
            "turn": 0
          },
          {
            "count": 6,
            "max": 3641,
            "mean": 3108,
            "min": 2924,
            "total": 18648,
            "turn": 1
          },
          {
            "count": 6,
            "max": 4188,
            "mean": 3777.1666666666665,
            "min": 3477,
            "total": 22663,
            "turn": 2
          },
          {
            "count": 6,
            "max": 4943,
            "mean": 4532.166666666667,
            "min": 4232,
            "total": 27193,
            "turn": 3
          },
          {
            "count": 6,
            "max": 5698,
            "mean": 5287.166666666667,
            "min": 4987,
            "total": 31723,
            "turn": 4
          },
          {
            "count": 6,
            "max": 6454,
            "mean": 6042.5,
            "min": 5743,
            "total": 36255,
            "turn": 5
          },
          {
            "count": 6,
            "max": 7209,
            "mean": 6797.5,
            "min": 6498,
            "total": 40785,
            "turn": 6
          },
          {
            "count": 6,
            "max": 7964,
            "mean": 7552.5,
            "min": 7253,
            "total": 45315,
            "turn": 7
          },
          {
            "count": 6,
            "max": 8719,
            "mean": 8307.5,
            "min": 8008,
            "total": 49845,
            "turn": 8
          },
          {
            "count": 6,
            "max": 9474,
            "mean": 9062.5,
            "min": 8763,
            "total": 54375,
            "turn": 9
          },
          {
            "count": 6,
            "max": 10234,
            "mean": 9824.166666666666,
            "min": 9520,
            "total": 58945,
            "turn": 10
          },
          {
            "count": 6,
            "max": 10996,
            "mean": 10583.166666666666,
            "min": 10284,
            "total": 63499,
            "turn": 11
          },
          {
            "count": 6,
            "max": 11753,
            "mean": 11340.166666666666,
            "min": 11041,
            "total": 68041,
            "turn": 12
          },
          {
            "count": 6,
            "max": 12510,
            "mean": 12097.166666666666,
            "min": 11798,
            "total": 72583,
            "turn": 13
          },
          {
            "count": 6,
            "max": 13267,
            "mean": 12854.166666666666,
            "min": 12555,
            "total": 77125,
            "turn": 14
          },
          {
            "count": 6,
            "max": 14024,
            "mean": 13611.166666666666,
            "min": 13312,
            "total": 81667,
            "turn": 15
          },
          {
            "count": 6,
            "max": 14781,
            "mean": 14368.166666666666,
            "min": 14069,
            "total": 86209,
            "turn": 16
          },
          {
            "count": 6,
            "max": 15538,
            "mean": 15125.166666666666,
            "min": 14826,
            "total": 90751,
            "turn": 17
          },
          {
            "count": 6,
            "max": 16295,
            "mean": 15882.166666666666,
            "min": 15583,
            "total": 95293,
            "turn": 18
          },
          {
            "count": 6,
            "max": 17052,
            "mean": 16639.166666666668,
            "min": 16340,
            "total": 99835,
            "turn": 19
          },
          {
            "count": 6,
            "max": 17809,
            "mean": 17396.166666666668,
            "min": 17097,
            "total": 104377,
            "turn": 20
          },
          {
            "count": 6,
            "max": 18566,
            "mean": 18153.166666666668,
            "min": 17854,
            "total": 108919,
            "turn": 21
          },
          {
            "count": 6,
            "max": 19323,
            "mean": 18910.166666666668,
            "min": 18611,
            "total": 113461,
            "turn": 22
          },
          {
            "count": 6,
            "max": 20080,
            "mean": 19667.166666666668,
            "min": 19368,
            "total": 118003,
            "turn": 23
          },
          {
            "count": 6,
            "max": 20837,
            "mean": 20424.166666666668,
            "min": 20125,
            "total": 122545,
            "turn": 24
          },
          {
            "count": 6,
            "max": 21594,
            "mean": 21181.166666666668,
            "min": 20882,
            "total": 127087,
            "turn": 25
          },
          {
            "count": 6,
            "max": 22351,
            "mean": 21938.166666666668,
            "min": 21639,
            "total": 131629,
            "turn": 26
          },
          {
            "count": 6,
            "max": 23108,
            "mean": 22695.166666666668,
            "min": 22396,
            "total": 136171,
            "turn": 27
          },
          {
            "count": 6,
            "max": 23865,
            "mean": 23452.166666666668,
            "min": 23153,
            "total": 140713,
            "turn": 28
          },
          {
            "count": 6,
            "max": 24622,
            "mean": 24209.166666666668,
            "min": 23910,
            "total": 145255,
            "turn": 29
          },
          {
            "count": 6,
            "max": 25379,
            "mean": 24966.166666666668,
            "min": 24667,
            "total": 149797,
            "turn": 30
          },
          {
            "count": 6,
            "max": 26136,
            "mean": 25723.166666666668,
            "min": 25424,
            "total": 154339,
            "turn": 31
          },
          {
            "count": 6,
            "max": 26893,
            "mean": 26480.166666666668,
            "min": 26181,
            "total": 158881,
            "turn": 32
          },
          {
            "count": 6,
            "max": 27650,
            "mean": 27237.166666666668,
            "min": 26938,
            "total": 163423,
            "turn": 33
          },
          {
            "count": 6,
            "max": 28407,
            "mean": 27994.166666666668,
            "min": 27695,
            "total": 167965,
            "turn": 34
          },
          {
            "count": 6,
            "max": 29164,
            "mean": 28751.166666666668,
            "min": 28452,
            "total": 172507,
            "turn": 35
          },
          {
            "count": 6,
            "max": 29921,
            "mean": 29508.166666666668,
            "min": 29209,
            "total": 177049,
            "turn": 36
          },
          {
            "count": 6,
            "max": 30678,
            "mean": 30265.166666666668,
            "min": 29966,
            "total": 181591,
            "turn": 37
          },
          {
            "count": 6,
            "max": 31435,
            "mean": 31022.166666666668,
            "min": 30723,
            "total": 186133,
            "turn": 38
          },
          {
            "count": 6,
            "max": 32192,
            "mean": 31779.166666666668,
            "min": 31480,
            "total": 190675,
            "turn": 39
          },
          {
            "count": 6,
            "max": 32949,
            "mean": 32536.166666666668,
            "min": 32237,
            "total": 195217,
            "turn": 40
          },
          {
            "count": 6,
            "max": 33706,
            "mean": 33293.166666666664,
            "min": 32994,
            "total": 199759,
            "turn": 41
          },
          {
            "count": 6,
            "max": 34463,
            "mean": 34050.166666666664,
            "min": 33751,
            "total": 204301,
            "turn": 42
          },
          {
            "count": 6,
            "max": 35220,
            "mean": 34807.166666666664,
            "min": 34508,
            "total": 208843,
            "turn": 43
          },
          {
            "count": 6,
            "max": 35977,
            "mean": 35564.166666666664,
            "min": 35265,
            "total": 213385,
            "turn": 44
          },
          {
            "count": 6,
            "max": 36734,
            "mean": 36321.166666666664,
            "min": 36022,
            "total": 217927,
            "turn": 45
          },
          {
            "count": 6,
            "max": 37491,
            "mean": 37078.166666666664,
            "min": 36779,
            "total": 222469,
            "turn": 46
          },
          {
            "count": 6,
            "max": 38248,
            "mean": 37835.166666666664,
            "min": 37536,
            "total": 227011,
            "turn": 47
          },
          {
            "count": 6,
            "max": 39002,
            "mean": 38586,
            "min": 38293,
            "total": 231516,
            "turn": 48
          },
          {
            "count": 3,
            "max": 39687,
            "mean": 39336.333333333336,
            "min": 39038,
            "total": 118009,
            "turn": 49
          }
        ],
        "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; includes terminal checkpoint payloads; -1 is setup",
        "per_provider_turn": 122520.3,
        "total": 6126015
      },
      "child_command": [
        "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python",
        "/home/duy/.codex/worktrees/scale-bench-baseline/agent_kit/scripts/benchmark_runtime.py",
        "--scale-child",
        "{\"name\": \"scale_sqlite_1x50\", \"backend\": \"sqlite\", \"sessions\": 1, \"turns\": 50}",
        "--scale-progress",
        "/tmp/linch-scale-62op5x7x/progress.json"
      ],
      "child_wall_s": 2.2572153300006903,
      "completed": 1,
      "correct": true,
      "elapsed_s": 1.964962538000691,
      "errors": [],
      "event_loop_lag_ms": {
        "interval": 10.0,
        "max": 21.13368499885837,
        "p99": 15.446166000401718,
        "samples": 166
      },
      "failed": 0,
      "name": "scale_sqlite_1x50",
      "observed_provider_turns": 50,
      "observed_tool_calls": 50,
      "parallel_first_read_pairs": 1,
      "pid": 79214,
      "rss": {
        "baseline_current_bytes": 38809600,
        "import_high_water_floor_bytes": 0,
        "initial_high_water_bytes": 38633472,
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
        "peak_delta_bytes": 5955584,
        "peak_delta_bytes_per_session": 5955584.0,
        "peak_high_water_bytes": 44007424,
        "peak_rss_bytes": 44765184,
        "sampled_peak_rss_bytes": 44765184
      },
      "run_latency_ms": {
        "p50": 1964.8170519994892,
        "p99": 1964.8170519994892,
        "samples": 1,
        "scope": "session creation plus run; successful runs only"
      },
      "runs_per_second": 0.5089155547044064,
      "sessions": 1,
      "store_writes": {
        "attribution": "-1: setup; then latest provider_pending checkpoint turn; checkpoint calls use their own turn; cleanup uses final turn",
        "by_store": {
          "run": {
            "append_events": 401,
            "create_run": 1,
            "mark_completed": 1,
            "save_checkpoint": 298
          },
          "session": {
            "append_messages": 100,
            "create": 1
          }
        },
        "by_turn": [
          {
            "methods": {
              "run.append_events": 2,
              "run.create_run": 1,
              "run.save_checkpoint": 2,
              "session.append_messages": 1,
              "session.create": 1
            },
            "total": 7,
            "turn": -1
          },
          {
            "methods": {
              "run.append_events": 10,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 18,
            "turn": 0
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 1
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 2
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 3
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 4
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 5
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 6
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 7
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 8
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 9
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 10
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 11
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 12
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 13
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 14
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 15
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 16
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 17
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 18
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 19
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 20
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 21
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 22
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 23
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 24
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 25
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 26
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 27
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 28
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 29
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 30
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 31
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 32
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 33
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 34
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 35
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 36
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 37
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 38
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 39
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 40
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 41
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 42
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 43
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 44
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 45
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 46
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 47
          },
          {
            "methods": {
              "run.append_events": 8,
              "run.save_checkpoint": 6,
              "session.append_messages": 2
            },
            "total": 16,
            "turn": 48
          },
          {
            "methods": {
              "run.append_events": 5,
              "run.mark_completed": 1,
              "run.save_checkpoint": 2,
              "session.append_messages": 1
            },
            "total": 9,
            "turn": 49
          }
        ],
        "per_provider_turn": 16.04,
        "total": 802,
        "unit": "successful external mutation API calls, not SQL statements or rows"
      },
      "timed_out": false,
      "timeout_s": 600,
      "turns": 50
    },
    "scale_sqlite_200x4": {
      "backend": "sqlite",
      "checkpoint_bytes": {
        "by_phase": {
          "assistant_appended": {
            "count": 800,
            "max": 4875,
            "mean": 3807.53125,
            "min": 2525,
            "total": 3046025
          },
          "completed": {
            "count": 200,
            "max": 4474,
            "mean": 4473.045,
            "min": 4472,
            "total": 894609
          },
          "provider_pending": {
            "count": 800,
            "max": 4224,
            "mean": 3078.02,
            "min": 1580,
            "total": 2462416
          },
          "started": {
            "count": 200,
            "max": 1179,
            "mean": 1179,
            "min": 1179,
            "total": 235800
          },
          "tool_batch_pending": {
            "count": 600,
            "max": 3681,
            "mean": 2979.693333333333,
            "min": 2329,
            "total": 1787816
          },
          "tool_executing": {
            "count": 600,
            "max": 3677,
            "mean": 2975.693333333333,
            "min": 2325,
            "total": 1785416
          },
          "tool_results_appended": {
            "count": 600,
            "max": 3793,
            "mean": 3131.0266666666666,
            "min": 2555,
            "total": 1878616
          },
          "turn_complete": {
            "count": 600,
            "max": 3829,
            "mean": 3182.36,
            "min": 2635,
            "total": 1909416
          },
          "user_appended": {
            "count": 200,
            "max": 1185,
            "mean": 1185,
            "min": 1185,
            "total": 237000
          }
        },
        "by_turn": [
          {
            "count": 400,
            "max": 1185,
            "mean": 1182,
            "min": 1179,
            "total": 472800,
            "turn": -1
          },
          {
            "count": 1200,
            "max": 2636,
            "mean": 2324.8458333333333,
            "min": 1580,
            "total": 2789815,
            "turn": 0
          },
          {
            "count": 1200,
            "max": 3644,
            "mean": 3109.8566666666666,
            "min": 2926,
            "total": 3731828,
            "turn": 1
          },
          {
            "count": 1200,
            "max": 4189,
            "mean": 3773.8708333333334,
            "min": 3479,
            "total": 4528645,
            "turn": 2
          },
          {
            "count": 600,
            "max": 4875,
            "mean": 4523.376666666667,
            "min": 4223,
            "total": 2714026,
            "turn": 3
          }
        ],
        "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; includes terminal checkpoint payloads; -1 is setup",
        "per_provider_turn": 17796.3925,
        "total": 14237114
      },
      "child_command": [
        "/home/duy/Downloads/duy_dev/agent_kit/.venv/bin/python",
        "/home/duy/.codex/worktrees/scale-bench-baseline/agent_kit/scripts/benchmark_runtime.py",
        "--scale-child",
        "{\"name\": \"scale_sqlite_200x4\", \"backend\": \"sqlite\", \"sessions\": 200, \"turns\": 4}",
        "--scale-progress",
        "/tmp/linch-scale-uz2v6a4l/progress.json"
      ],
      "child_wall_s": 9.82003245099986,
      "completed": 200,
      "correct": true,
      "elapsed_s": 9.572646688000532,
      "errors": [],
      "event_loop_lag_ms": {
        "interval": 10.0,
        "max": 22.50373899914848,
        "p99": 8.57830300083151,
        "samples": 770
      },
      "failed": 0,
      "name": "scale_sqlite_200x4",
      "observed_provider_turns": 800,
      "observed_tool_calls": 800,
      "parallel_first_read_pairs": 200,
      "pid": 8,
      "rss": {
        "baseline_current_bytes": 38727680,
        "import_high_water_floor_bytes": 0,
        "initial_high_water_bytes": 38633472,
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
        "peak_delta_bytes": 18743296,
        "peak_delta_bytes_per_session": 93716.48,
        "peak_high_water_bytes": 57470976,
        "peak_rss_bytes": 57470976,
        "sampled_peak_rss_bytes": 57233408
      },
      "run_latency_ms": {
        "p50": 6677.695270000186,
        "p99": 9568.209801000194,
        "samples": 200,
        "scope": "session creation plus run; successful runs only"
      },
      "runs_per_second": 20.892863438771144,
      "sessions": 200,
      "store_writes": {
        "attribution": "-1: setup; then latest provider_pending checkpoint turn; checkpoint calls use their own turn; cleanup uses final turn",
        "by_store": {
          "run": {
            "append_events": 6600,
            "create_run": 200,
            "mark_completed": 200,
            "save_checkpoint": 4400
          },
          "session": {
            "append_messages": 1600,
            "create": 200
          }
        },
        "by_turn": [
          {
            "methods": {
              "run.append_events": 400,
              "run.create_run": 200,
              "run.save_checkpoint": 400,
              "session.append_messages": 200,
              "session.create": 200
            },
            "total": 1400,
            "turn": -1
          },
          {
            "methods": {
              "run.append_events": 2000,
              "run.save_checkpoint": 1200,
              "session.append_messages": 400
            },
            "total": 3600,
            "turn": 0
          },
          {
            "methods": {
              "run.append_events": 1600,
              "run.save_checkpoint": 1200,
              "session.append_messages": 400
            },
            "total": 3200,
            "turn": 1
          },
          {
            "methods": {
              "run.append_events": 1600,
              "run.save_checkpoint": 1200,
              "session.append_messages": 400
            },
            "total": 3200,
            "turn": 2
          },
          {
            "methods": {
              "run.append_events": 1000,
              "run.mark_completed": 200,
              "run.save_checkpoint": 400,
              "session.append_messages": 200
            },
            "total": 1800,
            "turn": 3
          }
        ],
        "per_provider_turn": 16.5,
        "total": 13200,
        "unit": "successful external mutation API calls, not SQL statements or rows"
      },
      "timed_out": false,
      "timeout_s": 600,
      "turns": 4
    }
  },
  "schema_version": 3,
  "suite": "scale"
}
```
