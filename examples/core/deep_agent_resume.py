"""Deep agent examples with DeepSeek provider.

Showcases the full deep agent preset:
  1. Planning + planner subagent + durable /memories + cross-process resume
  2. Background worker + <task-notification> delivery
  3. Fork/continue — re-engage a retained worker with full prior context
  4. Coordinator mode — pure orchestrator that delegates all heavy work

Run with:
    DEEPSEEK_API_KEY=sk-... python3 examples/core/deep_agent_resume.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

DEEPSEEK_BASE = "https://api.deepseek.com"
MODEL = "deepseek-v4-pro"


def _api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("DEEPSEEK_API_KEY is not set. Export it and re-run:", file=sys.stderr)
        print("  export DEEPSEEK_API_KEY=sk-...", file=sys.stderr)
        sys.exit(1)
    return key


def _provider():
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions

    return OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=_api_key(), base_url=DEEPSEEK_BASE)
    )


# ── 1. Planning + planner subagent + /memories + resume ──────────────────────


async def run_durable_planning(work_dir: str) -> None:
    """Deep agent creates a plan via the planner subagent, saves it to /memories,
    and can be resumed after an interruption."""
    from linch import create_deep_agent

    print("[deep/planning] first run …")
    agent = create_deep_agent(
        model=MODEL,
        provider=_provider(),
        cwd=work_dir,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session(id="demo-planning")
    run_id = ""

    try:
        async for event in session.run(
            "Use the planner subagent to create a 3-step plan for building "
            "a Python CLI calculator. Save the plan to /memories/calc-plan.md "
            "and return a one-sentence summary."
        ):
            if event.type == "system":
                run_id = event.run_id
                print(f"  run_id={run_id}")
            elif event.type == "tool_call_start":
                label = event.input.get("description", "") or event.input.get("name", "")
                print(f"  → {event.tool_name}({label})")
            elif event.type == "tool_call_end" and event.is_error:
                print(f"  ✗ {event.tool_name}: {event.result[:80]}")
            elif event.type == "result":
                print(f"  result: {event.final_text}")
            elif event.type == "error":
                print(f"  ERROR: {event.error.get('message', '')}")
    finally:
        await agent.close()

    if not run_id:
        return

    # Simulate a process restart: fresh agent instance, same cwd → same SQLite stores.
    print("\n[deep/planning] restarted — resuming …")
    agent2 = create_deep_agent(
        model=MODEL,
        provider=_provider(),
        cwd=work_dir,
        permissions={"mode": "skip-dangerous"},
    )
    try:
        resumed = await agent2.session(id="demo-planning")
        async for event in resumed.resume(run_id):
            if event.type == "tool_call_start":
                print(f"  (resumed) → {event.tool_name}")
            elif event.type == "result":
                print(f"  (resumed) result: {event.final_text}")
    finally:
        await agent2.close()


# ── 2. Background worker + <task-notification> ────────────────────────────────


async def run_background_worker(work_dir: str) -> None:
    """Spawn a researcher subagent in the background.
    The parent turn returns immediately with an ack; a <task-notification>
    is injected into the conversation on the next session.run() call."""
    from linch import create_deep_agent

    agent = create_deep_agent(
        model=MODEL,
        provider=_provider(),
        cwd=work_dir,
        durable=False,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session(id="demo-bg")

    print("[deep/background] turn 1 — spawn background researcher …")
    try:
        async for event in session.run(
            "Call the Subagent tool with subagent_type='researcher' and "
            "run_in_background=True. The researcher's task: explain what "
            "Python pathlib.Path does in exactly 2 sentences. "
            "After spawning, reply 'Background worker started.' and stop."
        ):
            if event.type == "tool_call_end":
                label = "✓" if not event.is_error else "✗"
                print(f"  {label} {event.tool_name}: {str(event.result)[:100]!r}")
            elif event.type == "result":
                print(f"  result: {event.final_text}")
            elif event.type == "error":
                print(f"  ERROR: {event.error.get('message', '')}")

        # Wait for background task to finish before driving the next turn.
        for handle in session.workers.values():
            if handle.task is not None and not handle.task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(handle.task), timeout=60.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    print(f"  worker {handle.worker_id} timed out or was cancelled")
        print(f"  pending notifications: {len(session.pending_notifications)}")

        # Turn 2: the <task-notification> is drained at the top of this turn
        # and injected into provider_view before the model is called.
        print("[deep/background] turn 2 — drain notification …")
        async for event in session.run("Summarise what the background researcher found."):
            if event.type == "user":
                text = "".join(getattr(b, "text", "") for b in event.message.content)
                if "<task-notification>" in text:
                    print("  <task-notification> delivered to provider view")
            elif event.type == "result":
                print(f"  result: {event.final_text}")
            elif event.type == "error":
                print(f"  ERROR: {event.error.get('message', '')}")
    finally:
        await agent.close()


# ── 3. Fork/continue — re-engage a retained worker ───────────────────────────


async def run_fork_continue(work_dir: str) -> None:
    """Spawn a researcher, then continue it with a follow-up question.
    The worker's full prior conversation is preserved across continuations."""
    from linch import create_deep_agent

    agent = create_deep_agent(
        model=MODEL,
        provider=_provider(),
        cwd=work_dir,
        durable=False,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session(id="demo-fork")
    worker_id = ""

    print("[deep/fork] turn 1 — spawn researcher …")
    try:
        async for event in session.run(
            "Use the Subagent tool with subagent_type='researcher' to ask: "
            "'What does asyncio.gather() do in Python? 1 sentence.' "
            "Include the Worker ID from the result in your reply."
        ):
            if event.type == "tool_call_end" and event.tool_name == "Subagent":
                print(f"  Subagent result: {str(event.result)[:120]!r}")
            elif event.type == "result":
                print(f"  result: {event.final_text}")
            elif event.type == "error":
                print(f"  ERROR: {event.error.get('message', '')}")

        if session.workers:
            worker_id = next(iter(session.workers))
            handle = session.workers[worker_id]
            child = agent._sessions.get(handle.child_session_id)
            ctx_msgs = len(child.provider_view) if child else 0
            print(
                f"  retained worker: {worker_id} ({handle.display_name}), context msgs: {ctx_msgs}"
            )

        if worker_id:
            print("[deep/fork] turn 2 — continue the same worker …")
            async for event in session.run(
                f"Use SubagentContinue with to='{worker_id}' and message: "
                "'Now give a one-line code example using asyncio.gather().'"
            ):
                if event.type == "tool_call_end" and event.tool_name == "SubagentContinue":
                    print(f"  SubagentContinue result: {str(event.result)[:120]!r}")
                elif event.type == "result":
                    print(f"  result: {event.final_text}")
                elif event.type == "error":
                    print(f"  ERROR: {event.error.get('message', '')}")
    finally:
        await agent.close()


# ── 4. Coordinator mode ───────────────────────────────────────────────────────


async def run_coordinator(work_dir: str) -> None:
    """Coordinator mode: parent only orchestrates — no Bash/Edit/Write/Read.
    Workers receive full tool access via SubagentTool → build_child_tools."""
    from linch import create_deep_agent

    agent = create_deep_agent(
        model=MODEL,
        provider=_provider(),
        cwd=work_dir,
        durable=False,
        coordinator=True,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session(id="demo-coordinator")

    parent_tools = {t.name for t in agent.tools.list()}
    print(f"[coordinator] parent tools: {sorted(parent_tools)}")

    print("[coordinator] orchestrating research → synthesis → plan …")
    try:
        async for event in session.run(
            "Orchestrate this in two phases:\n"
            "Phase 1 (Research): spawn a researcher to explain Python dataclasses "
            "in 1 sentence.\n"
            "Phase 2 (Plan): spawn a planner to give a 2-step plan for adding a "
            "dataclass to an existing module.\n"
            "Synthesise both results into one short final answer. "
            "Do NOT implement anything yourself."
        ):
            if event.type == "tool_call_start":
                label = event.input.get("description", "") or event.input.get("subagent_type", "")
                print(f"  → {event.tool_name}({label})")
            elif event.type == "tool_call_end" and event.is_error:
                print(f"  ✗ {event.tool_name}: {event.result[:80]}")
            elif event.type == "result":
                print(f"  result:\n{event.final_text}")
            elif event.type == "error":
                print(f"  ERROR: {event.error.get('message', '')}")
    finally:
        await agent.close()


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="linch-deep-demo-") as work_dir:
        sep = "=" * 60

        print(sep)
        print("1. Planning + planner subagent + /memories + resume")
        print(sep)
        await run_durable_planning(work_dir)

        print(f"\n{sep}")
        print("2. Background worker + <task-notification>")
        print(sep)
        await run_background_worker(work_dir)

        print(f"\n{sep}")
        print("3. Fork/continue — re-engage a retained worker")
        print(sep)
        await run_fork_continue(work_dir)

        print(f"\n{sep}")
        print("4. Coordinator mode — pure orchestrator")
        print(sep)
        await run_coordinator(work_dir)


if __name__ == "__main__":
    asyncio.run(main())
