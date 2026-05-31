"""Event streaming — consume, filter, and forward events.

Run:
    OPENAI_API_KEY=sk-... python examples/07_event_streaming.py

Demonstrates:
  1. All event types — print every event to understand the stream
  2. Streaming text delta — progressively print assistant text
  3. Tool call observer — log start/end with timing
  4. Usage tracking — accumulate token counts across turns
  5. SSE format — serialize events for a web frontend
  6. Progress bar — show task progress in the terminal
  7. Abort — cancel a long-running run mid-stream
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from agent_kit import Agent
from agent_kit.config import FeatureFlags, SystemPromptConfig
from agent_kit.events import event_to_dict
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.registry import empty_tools, tools_from_defaults
from agent_kit.types import Usage

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"

BASE = dict(
    model=MODEL,
    openai_api_key=API_KEY,
    session_store=InMemorySessionStore(),
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
)


# ── 1. Print every event ──────────────────────────────────────────────────────

async def demo_all_events() -> None:
    print("\n── 1. All event types ──")
    agent = Agent(
        **BASE,
        tools=tools_from_defaults(exclude={"Bash", "Write", "Edit"}),
        permissions={"mode": "skip-dangerous"},
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="List the Python files in the current directory using Glob.",
        ),
    )
    session = await agent.session()
    async for event in session.run("List all .py files here."):
        typ = event.type
        if typ == "system":
            print(f"  [system]    model={event.model}, tools={event.tools[:3]}…")
        elif typ == "user":
            print(f"  [user]      {len(event.message.content)} content block(s)")
        elif typ == "assistant":
            text = next((b.text[:60] for b in event.message.content if b.type == "text"), "")
            print(f"  [assistant] stop={event.stop_reason} | {text!r}")
        elif typ == "tool_call_start":
            print(f"  [tool▶]     {event.tool_name}({event.summary[:40]})")
        elif typ == "tool_call_end":
            print(f"  [tool■]     {event.tool_name} ok={not event.is_error} {event.duration_ms}ms")
        elif typ == "usage":
            u = event.usage
            print(f"  [usage]     in={u.input_tokens} out={u.output_tokens}")
        elif typ == "result":
            print(f"  [result]    subtype={event.subtype} duration={event.duration_ms}ms")
        elif typ == "error":
            print(f"  [error]     {event.error}")
        elif typ == "partial_assistant":
            pass  # skip — too verbose; see demo 2 below


# ── 2. Progressive text streaming ────────────────────────────────────────────

async def demo_streaming_text() -> None:
    print("\n── 2. Streaming text (partial_assistant events) ──")
    agent = Agent(
        **BASE,
        tools=empty_tools(),
        include_partial_messages=True,  # ← enables PartialAssistantEvent
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Answer in exactly three sentences.",
        ),
    )
    session = await agent.session()
    print("  Response: ", end="", flush=True)
    async for event in session.run("Explain what async/await means in Python."):
        if event.type == "partial_assistant":
            delta = event.delta
            if delta.get("kind") == "text":
                print(delta["text"], end="", flush=True)
        elif event.type == "result":
            print()  # newline after streaming text
            u = event.total_usage
            print(f"  (tokens in={u.input_tokens} out={u.output_tokens})")


# ── 3. Tool call observer ─────────────────────────────────────────────────────

async def demo_tool_observer() -> None:
    print("\n── 3. Tool call observer ──")
    agent = Agent(
        **BASE,
        tools=tools_from_defaults(exclude={"Bash", "Write", "Edit"}),
        permissions={"mode": "skip-dangerous"},
        cwd=os.getcwd(),
    )
    session = await agent.session()

    pending_starts: dict[str, float] = {}

    async for event in session.run("Read the file pyproject.toml and tell me the project name."):
        if event.type == "tool_call_start":
            pending_starts[event.tool_use_id] = time.monotonic()
            print(f"  ▶ {event.tool_name:15} | {event.summary[:50]}")
        elif event.type == "tool_call_end":
            elapsed = time.monotonic() - pending_starts.pop(event.tool_use_id, time.monotonic())
            status = "✓" if not event.is_error else "✗"
            print(f"  {status} {event.tool_name:15} | {elapsed*1000:.0f}ms")
        elif event.type == "result":
            print(f"  Answer: {event.final_text[:120]}")


# ── 4. Usage tracking ─────────────────────────────────────────────────────────

async def demo_usage_tracking() -> None:
    print("\n── 4. Usage tracking across turns ──")
    agent = Agent(
        **BASE,
        tools=empty_tools(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Answer very briefly — one sentence only.",
        ),
    )
    session = await agent.session()
    total = Usage()
    questions = [
        "What is Python?",
        "What is async/await?",
        "What is a decorator?",
    ]
    for q in questions:
        async for event in session.run(q):
            if event.type == "usage":
                total = total.add(event.usage)
            elif event.type == "result":
                print(f"  Q: {q[:30]:30} | out={event.total_usage.output_tokens:3} tokens")

    print(f"  ──── Session total: in={total.input_tokens} out={total.output_tokens} tokens")


# ── 5. SSE serialisation ──────────────────────────────────────────────────────
#
# event_to_dict() + event_from_dict() let you serialise events for SSE / WS.
# In a FastAPI route you'd yield these as text/event-stream data.

def to_sse_line(event) -> str:
    """Format an event as a Server-Sent Events data line."""
    data = json.dumps(event_to_dict(event))
    return f"data: {data}\n\n"


async def demo_sse_format() -> None:
    print("\n── 5. SSE format ──")
    agent = Agent(
        **BASE,
        tools=empty_tools(),
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append="Answer briefly."),
    )
    session = await agent.session()
    sse_lines = []
    async for event in session.run("What is 2 + 2?"):
        if event.type in ("assistant", "result", "usage"):
            line = to_sse_line(event)
            sse_lines.append(line)

    print(f"  Generated {len(sse_lines)} SSE lines:")
    for line in sse_lines[:3]:
        payload = json.loads(line.removeprefix("data: "))
        print(f"    type={payload['type']:12} | {str(payload)[:80]}")

    # ── FastAPI example (not executed here) ──
    # from fastapi import FastAPI
    # from fastapi.responses import StreamingResponse
    #
    # app = FastAPI()
    # agent = make_agent()  # module-level singleton
    #
    # @app.post("/chat")
    # async def chat(body: ChatRequest):
    #     session = await agent.session(id=body.session_id)
    #     async def stream():
    #         async for event in session.run(body.message):
    #             yield to_sse_line(event)
    #             if event.type == "result":
    #                 break
    #     return StreamingResponse(stream(), media_type="text/event-stream")


# ── 6. Progress bar ───────────────────────────────────────────────────────────

async def demo_progress() -> None:
    print("\n── 6. Terminal progress indicator ──")
    agent = Agent(
        **BASE,
        tools=tools_from_defaults(exclude={"Bash", "Write", "Edit"}),
        permissions={"mode": "skip-dangerous"},
        cwd=os.getcwd(),
    )
    session = await agent.session()

    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    current_step = "Starting…"

    print("  ", end="", flush=True)
    async for event in session.run(
        "Read pyproject.toml and list the direct dependencies."
    ):
        if event.type == "tool_call_start":
            current_step = f"{event.tool_name}({event.summary[:25]})"
        elif event.type == "tool_call_end":
            current_step = f"{event.tool_name} done"
        elif event.type == "assistant":
            current_step = "Generating response…"

        if event.type not in ("result", "error"):
            print(f"\r  {spinner[i % len(spinner)]} {current_step[:50]:50}", end="", flush=True)
            i += 1

        if event.type == "result":
            print(f"\r  ✓ Done in {event.duration_ms}ms" + " " * 40)
            print(f"  Answer: {event.final_text[:150]}")


# ── 7. Abort ──────────────────────────────────────────────────────────────────
#
# session.abort() signals the running loop to stop. The current tool call
# and stream are cancelled. A ResultEvent(subtype="aborted") is emitted.

async def demo_abort() -> None:
    print("\n── 7. Abort mid-run ──")
    agent = Agent(
        **BASE,
        tools=empty_tools(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Write a very long essay about the history of computing. Be exhaustive.",
        ),
    )
    session = await agent.session()

    events_seen = []

    async def run_and_abort():
        # Abort after 0.3 seconds
        async def _abort_later():
            await asyncio.sleep(0.3)
            print("  [aborting now]")
            session.abort()

        abort_task = asyncio.create_task(_abort_later())
        async for event in session.run("Write the essay."):
            events_seen.append(event.type)
            if event.type == "result":
                break
        abort_task.cancel()

    await run_and_abort()
    result = next((e for e in events_seen if e == "result"), None)
    print(f"  Events seen: {events_seen}")
    print(f"  Run ended: result event present = {result is not None}")


# ── Main ───────────────────────────────────────────────────────────────────────


async def main() -> None:
    if not API_KEY:
        print("Set OPENAI_API_KEY to run this example.")
        print("Patterns covered:")
        for label in [
            "1. All events — understand the stream shape",
            "2. partial_assistant — stream text progressively",
            "3. tool_call_start/end — observe tool timing",
            "4. usage events — track token consumption",
            "5. event_to_dict — SSE / WebSocket serialisation",
            "6. progress bar — terminal UX",
            "7. session.abort() — cancel a running loop",
        ]:
            print(f"  {label}")
        return

    await demo_streaming_text()
    await demo_tool_observer()
    await demo_usage_tracking()
    await demo_sse_format()
    await demo_all_events()
    await demo_progress()
    await demo_abort()


if __name__ == "__main__":
    asyncio.run(main())
