# Outer loop runner

[← Usage guide](./README.md)

`LoopRunner` is the SDK-native outer-loop primitive for recurring agent work. It is
not a daemon and it does not own webhooks, cron, or process lifetime. A host calls
`run_once()` whenever some external trigger says another tick should run. Tiny
host-owned wrappers for cron, webhook, fixed-interval, and CI-gate triggers live in
[`examples/recipes/runner_recipes.py`](../../examples/recipes/runner_recipes.py).

Each tick creates a fresh session, runs the agent once, writes a `RunReport`, and
persists loop state through files under `domains/<loop_id>/`.

```
domains/<loop_id>/
  README.md
  LOG.md
  artifacts/runs/<run_id>.md
  artifacts/runs/<run_id>.json
```

Fresh sessions are deliberate: recurring work should not depend on hidden
conversation history. Put durable state in artifacts, memory, tools, or your own
stores.

---

## Manual tick

```python
from linch import Agent, LoopRunner, LoopSpec, LoopTrigger

agent = Agent(model="gpt-5", permissions={"mode": "skip-dangerous"})

spec = LoopSpec(
    id="docs-maintenance",
    charter="Keep the documentation accurate and runnable.",
    prompt="Review the domain artifacts and make one useful improvement.",
)

runner = LoopRunner(agent)
result = await runner.run_once(
    spec,
    LoopTrigger(source="manual", payload="Developer requested a maintenance pass."),
)

print(result.run_id, result.status, result.done)
print(result.artifact_paths)
```

There is a live runnable version at
[`examples/recipes/loop_runner.py`](../../examples/recipes/loop_runner.py). It
loads a project `.env` when present:

```bash
cat > .env <<'EOF'
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
model=deepseek-chat
EOF

python examples/recipes/loop_runner.py
```

The example also accepts explicit names: `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`,
`LOOP_RUNNER_BASE_URL`, and `LOOP_RUNNER_MODEL`. For OpenAI, omit `BASE_URL` and
set `LOOP_RUNNER_MODEL` to an OpenAI chat model such as `gpt-4o-mini`.

`LoopSpec.root` defaults to `"domains"`. Pass another root when a host wants loop
artifacts somewhere else:

```python
spec = LoopSpec(
    id="nightly-evals",
    charter="Run nightly eval triage.",
    prompt="Inspect failures and summarize the next action.",
    root="/var/lib/my-app/linch-domains",
)
```

---

## Completion and verification

The runner does not infer domain completion from marker files. Use caller
predicates over `LoopTickResult` and its `RunReport`.

```python
from linch import LoopTickResult


def verify(result: LoopTickResult) -> bool:
    return result.report.status == "completed" and not result.report.errors


def done(result: LoopTickResult, artifacts) -> bool:
    return "ready for release" in (result.final_text or "").lower()


runner = LoopRunner(agent, verify=verify, done_predicate=done)
result = await runner.run_once(spec)
```

If `verify` raises or returns `False`, the tick result is marked
`verification_failed`, `done` stays `False`, and the failure is appended to
`LOG.md`.

---

## Cron, CI, and webhooks

`LoopRunner` is a one-shot harness, so every host integration has the same shape:

```python
async def tick_from_cron() -> None:
    await runner.run_once(spec, LoopTrigger(source="cron", payload="0 * * * *"))


async def tick_from_ci(commit_sha: str) -> None:
    await runner.run_once(
        spec,
        LoopTrigger(source="ci", payload=commit_sha, metadata={"commit": commit_sha}),
    )


async def tick_from_webhook(body: str, delivery_id: str) -> None:
    await runner.run_once(
        spec,
        LoopTrigger(source="webhook", id=delivery_id, payload=body),
    )
```

The runner always has an in-process per-loop lock. If two calls for the same
`LoopSpec.id` overlap in one process, the second raises `ConfigError`.

For cron, CI workers, webhook handlers, or multiple app processes, add a durable
lease store:

```python
from linch import FileLoopLeaseStore

runner = LoopRunner(
    agent,
    leases=FileLoopLeaseStore(root="domains"),
    lease_owner="worker-1",
    lease_ttl_s=300,
)
```

`FileLoopLeaseStore` writes `domains/<loop_id>/.lock.json` with an expiry time.
Another process cannot acquire the same loop until the holder releases the lease
or the TTL expires. Use the same root as `LoopSpec.root` when you override it.

---

## Scheduling integration

`SchedulerLoop` can trigger the host call, but it does not own execution. Keep the
scheduler responsible for due times and the runner responsible for the agent tick.

```python
from linch import InMemoryScheduleStore, LoopTrigger, SchedulerLoop

store = InMemoryScheduleStore()
scheduler = SchedulerLoop(store, session)

for schedule in await scheduler.tick():
    await runner.run_once(
        spec,
        LoopTrigger(
            source="schedule",
            id=schedule.id,
            payload=schedule.payload,
            metadata={"name": schedule.name},
        ),
    )
```

This keeps trigger ownership in the host application. A future daemon or webhook
adapter can wrap the same `run_once()` primitive without changing loop artifacts or
run reports.

---

## Live test

The integration test uses the same project `.env` loader but requires an explicit
opt-in flag so normal local and CI checks do not spend API quota:

```bash
LINCH_RUN_LIVE_LOOP_TESTS=1 pytest tests/integration/test_live_loop_runner.py -v
```

The test accepts the generic DeepSeek-style `.env` keys (`API_KEY`, `BASE_URL`,
`model`) as well as explicit `DEEPSEEK_API_KEY` or `OPENAI_API_KEY`, then verifies
a real provider-backed tick, the `done_predicate`, and the generated `README.md`,
`LOG.md`, Markdown report, and JSON report under the loop domain.
