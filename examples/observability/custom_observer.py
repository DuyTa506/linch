"""Custom RunObserver — latency tracking and error counting without OTel.

Run:
    python3 examples/observability/custom_observer.py

Requires OPENAI_API_KEY for the live agent run (prints report even when
the run finishes in under a millisecond due to no tool calls).

Demonstrates:
  1. Subclassing BaseObserver — override only the hooks you need.
  2. Per-tool latency and error rate tracking.
  3. Provider call latency (wall-clock time waiting for the LLM).
  4. Token usage summary from on_run_end.
  5. Combining multiple observers (MetricsObserver + LoggingObserver).
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path

from linch.observability import (
    BaseObserver,
    LoggingObserver,
    ProviderCallResult,
    RunInfo,
    RunResultInfo,
    ToolResultInfo,
)

ROOT = Path(__file__).resolve().parents[2]


class MetricsObserver(BaseObserver):
    """Collects per-tool and per-provider latency + error counts for one run."""

    def __init__(self) -> None:
        self._tool_latencies: dict[str, list[int]] = defaultdict(list)
        self._tool_errors: dict[str, int] = defaultdict(int)
        self._provider_latencies: list[int] = []

    # ── span hooks ────────────────────────────────────────────────────────────

    def on_run_start(self, info: RunInfo) -> None:
        print(f"[metrics] run started  run_id={info.run_id}  model={info.model}")

    def on_provider_call_end(self, info: ProviderCallResult) -> None:
        self._provider_latencies.append(info.duration_ms)

    def on_tool_end(self, info: ToolResultInfo) -> None:
        self._tool_latencies[info.tool_name].append(info.duration_ms)
        if info.is_error:
            self._tool_errors[info.tool_name] += 1

    def on_run_end(self, info: RunResultInfo) -> None:
        self._print_report(info)

    # ── report ────────────────────────────────────────────────────────────────

    def _print_report(self, run: RunResultInfo) -> None:
        print("\n══ Metrics Report ══════════════════════════════")
        print(f"  run_id   : {run.run_id}")
        print(f"  outcome  : {run.subtype}")
        print(f"  duration : {run.duration_ms} ms")
        tokens = run.total_usage
        print(f"  tokens   : {tokens.input_tokens} in / {tokens.output_tokens} out")

        if self._provider_latencies:
            avg = sum(self._provider_latencies) / len(self._provider_latencies)
            print(f"  provider : {len(self._provider_latencies)} call(s), avg {avg:.0f} ms")

        if self._tool_latencies:
            print("  tools:")
            for name in sorted(self._tool_latencies):
                lats = self._tool_latencies[name]
                avg = sum(lats) / len(lats)
                errs = self._tool_errors.get(name, 0)
                err_note = f"  [{errs} error(s)]" if errs else ""
                print(f"    {name}: {len(lats)} call(s), avg {avg:.0f} ms{err_note}")

        print("════════════════════════════════════════════════\n")


def load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


async def main() -> None:
    load_project_env()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set — set it to run the live demo.")
        return

    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    metrics = MetricsObserver()
    logger = LoggingObserver()

    agent = Agent(
        model="gpt-5-nano-2025-08-07",
        openai_api_key=api_key,
        tools=tools_from_defaults(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        observers=[metrics, logger],
    )
    session = await agent.session()

    # Two turns — second turn reuses the same session so usage accumulates.
    async for _event in session.run("List the files in the current directory."):
        pass
    async for _event in session.run("How many files are there?"):
        pass


if __name__ == "__main__":
    asyncio.run(main())
