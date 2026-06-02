"""Custom permissions — all modes and rule types.

Run:
    OPENAI_API_KEY=sk-... python examples/02_custom_permissions.py

Demonstrates:
  1. mode="skip-dangerous"         — auto-approve everything (development/testing)
  2. mode="acceptEdits"  — auto-approve file edits; ask for shell/exec
  3. mode="default"      — ask for everything via canUseTool callback
  4. ToolRule            — allow or deny a specific tool by name
  5. PathRule            — restrict file access to certain directories
  6. BashRule            — block specific shell command patterns
  7. canUseTool sync     — simple synchronous approval callback
  8. canUseTool async    — async callback (e.g. calls an approval API)
  9. canUseTool with UI  — interactively prompt the terminal user

The permission system evaluation order:
  1. Rules are checked in order. First match wins.
  2. If no rule matches, the mode default is applied:
       "skip-dangerous"        → allow
       "acceptEdits" → allow edits/reads; deny exec/shell
       "default"     → canUseTool callback (or deny if not set)
"""

from __future__ import annotations

import asyncio
import os

from linch import Agent
from linch.config import FeatureFlags
from linch.permissions import BashRule, PathRule, ToolRule
from linch.sessions import InMemorySessionStore
from linch.tools.registry import default_tools, tools_from_defaults

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"

BASE_CFG = dict(
    model=MODEL,
    openai_api_key=API_KEY,
    session_store=InMemorySessionStore(),
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
)


# ── 1. Yolo mode ─────────────────────────────────────────────────────────────
#
# All tool calls auto-approved. Good for development and trusted environments.


def make_skip_dangerous_agent() -> Agent:
    return Agent(
        **BASE_CFG,
        permissions={"mode": "skip-dangerous"},
    )


# ── 2. acceptEdits mode ──────────────────────────────────────────────────────
#
# Auto-approves Read/Write/Edit/Glob/Grep (file tools).
# Requires explicit approval for Bash and anything with scope="exec".
# In practice: if you have no canUseTool callback, exec calls are denied.


def make_edits_agent() -> Agent:
    return Agent(
        **BASE_CFG,
        permissions={"mode": "acceptEdits"},
    )


# ── 3. ToolRule — allow or deny a named tool ─────────────────────────────────
#
# Block Bash entirely, but allow all other tools.
# Rules are checked in order; first match wins. The mode default applies
# when no rule matches.


def make_no_bash_agent() -> Agent:
    return Agent(
        **BASE_CFG,
        tools=default_tools(),
        permissions={
            "mode": "skip-dangerous",
            "rules": [
                ToolRule(tool="Bash", decision="deny"),
            ],
        },
    )


# ── 4. PathRule — restrict file access ──────────────────────────────────────
#
# Allow file ops only inside /tmp; deny access to everything else.
# PathRule matches against the file_path / target_directory argument.


def make_sandboxed_agent(allowed_dir: str = "/tmp") -> Agent:
    return Agent(
        **BASE_CFG,
        tools=default_tools(),
        cwd=allowed_dir,
        permissions={
            "mode": "skip-dangerous",
            "rules": [
                # Allow the safe dir explicitly
                PathRule(paths=[f"{allowed_dir}/**"], decision="allow"),
                # Deny everything else (no wildcard allow below this)
                PathRule(paths=["/**"], decision="deny"),
            ],
        },
    )


# ── 5. BashRule — block specific command patterns ────────────────────────────
#
# Let Bash run, but block dangerous patterns.
# Patterns are matched as substring or glob against the full command string.


def make_safe_bash_agent() -> Agent:
    return Agent(
        **BASE_CFG,
        tools=default_tools(),
        permissions={
            "mode": "skip-dangerous",
            "rules": [
                BashRule(patterns=["rm -rf*"], decision="deny"),
                BashRule(patterns=["sudo *"], decision="deny"),
                BashRule(patterns=["curl * | sh", "wget * | sh"], decision="deny"),
                BashRule(patterns=["chmod 777*"], decision="deny"),
            ],
        },
    )


# ── 6. canUseTool — synchronous callback ────────────────────────────────────
#
# The simplest custom logic: a plain Python function.
# Return {"behavior": "allow"} or {"behavior": "deny", "message": "..."}


def my_sync_approval(request) -> dict:
    # request.tool.name  — tool being called
    # request.input      — the validated input dict
    # request.tool.scope — "read" | "write" | "exec"

    # Allow all read operations automatically
    if request.tool.scope == "read":
        return {"behavior": "allow"}

    # Block all write/exec tools in this example
    return {
        "behavior": "deny",
        "message": f"Tool '{request.tool.name}' is not permitted in read-only mode.",
    }


def make_readonly_agent() -> Agent:
    return Agent(
        **BASE_CFG,
        tools=tools_from_defaults(exclude={"Write", "Edit"}),
        permissions={
            "mode": "default",
            "canUseTool": my_sync_approval,
        },
    )


# ── 7. canUseTool — async callback ───────────────────────────────────────────
#
# Use async when the approval decision requires I/O (DB lookup, HTTP call).


async def my_async_approval(request) -> dict:
    # Simulate an async policy check (e.g. calling an internal API)
    await asyncio.sleep(0)  # placeholder for real async work

    # Policy: Bash allowed only for non-destructive commands
    if request.tool.name == "Bash":
        cmd = request.input.get("command", "")
        if any(danger in cmd for danger in ["rm", "sudo", "curl", "wget"]):
            return {"behavior": "deny", "message": "Destructive command blocked."}

    return {"behavior": "allow"}


def make_async_callback_agent() -> Agent:
    return Agent(
        **BASE_CFG,
        tools=default_tools(),
        permissions={
            "mode": "default",
            "canUseTool": my_async_approval,  # async functions work directly
        },
    )


# ── 8. canUseTool — interactive terminal prompt ──────────────────────────────
#
# Block nothing by default, but pause the event stream and ask the user before
# running any write or exec tool. This is the "human-in-the-loop" pattern.
#
# In a web app, you'd emit a PermissionRequestEvent over WebSocket instead
# of using input() — but the callback structure is identical.


def make_interactive_agent() -> Agent:
    def interactive_approval(request) -> dict:
        scope = request.tool.scope
        name = request.tool.name
        summary = getattr(request, "summary", str(request.input))[:80]

        if scope == "read":
            return {"behavior": "allow"}  # reads need no confirmation

        print(f"\n[Permission] Tool: {name}  ({scope})")
        print(f"  Action: {summary}")
        answer = input("  Allow? [y/N] ").strip().lower()
        if answer == "y":
            return {"behavior": "allow"}
        return {"behavior": "deny", "message": "Denied by user."}

    return Agent(
        **BASE_CFG,
        tools=default_tools(),
        permissions={
            "mode": "default",
            "canUseTool": interactive_approval,
        },
    )


# ── 9. Combined rules + callback ─────────────────────────────────────────────
#
# Rules handle the obvious cases (fast, no I/O). The callback handles the rest.
# This is the recommended production pattern.


def make_production_agent(project_root: str = "/tmp/myproject") -> Agent:
    async def production_approval(request) -> dict:
        # Called only when no rule matched — these are the "grey area" ops.
        print(f"[Approval needed] {request.tool.name}: {request.input}")
        # In production: emit a PermissionRequestEvent and wait for UI response.
        # Here we auto-deny as a safe default.
        return {"behavior": "deny", "message": "Manual approval required."}

    return Agent(
        **BASE_CFG,
        tools=default_tools(),
        cwd=project_root,
        permissions={
            "mode": "default",
            "rules": [
                # Always allow reads
                ToolRule(tool="Read", decision="allow"),
                ToolRule(tool="Glob", decision="allow"),
                ToolRule(tool="Grep", decision="allow"),
                # Allow edits only within the project root
                PathRule(paths=[f"{project_root}/**"], decision="allow"),
                PathRule(paths=["/**"], decision="deny"),
                # Block dangerous bash patterns
                BashRule(patterns=["rm -rf*", "sudo *", "curl*|*sh"], decision="deny"),
            ],
            "canUseTool": production_approval,
        },
    )


# ── Demo runner ───────────────────────────────────────────────────────────────


async def demo_rules() -> None:
    """Show that ToolRule.deny blocks a tool call."""
    print("\n── Demo: ToolRule blocks Bash ──")
    agent = make_no_bash_agent()
    session = await agent.session()
    async for event in session.run("Run `echo hello` in the shell."):
        if event.type == "tool_call_end" and event.is_error:
            print(f"Tool call denied: {event.result}")
        if event.type == "result":
            print("Final:", event.final_text)


async def demo_path_rule() -> None:
    """Show that PathRule restricts file access."""
    print("\n── Demo: PathRule allows only /tmp ──")
    import tempfile

    with tempfile.NamedTemporaryFile(dir="/tmp", suffix=".txt", delete=False) as f:
        f.write(b"hello from /tmp\n")
        tmp_path = f.name

    agent = make_sandboxed_agent("/tmp")
    session = await agent.session()
    async for event in session.run(f"Read the file {tmp_path} and tell me its content."):
        if event.type == "result":
            print("Final:", event.final_text)

    import os as _os

    _os.unlink(tmp_path)


async def demo_readonly_callback() -> None:
    """canUseTool blocks write/exec, allows reads."""
    print("\n── Demo: sync canUseTool — read-only mode ──")
    agent = make_readonly_agent()
    session = await agent.session()
    # Read should work; write should be denied
    async for event in session.run("Read the file /etc/hostname."):
        if event.type == "result":
            print("Final:", event.final_text)


async def main() -> None:
    if not API_KEY:
        print("Set OPENAI_API_KEY to run this example.")
        print("Showing agent construction only (no live calls).")
        # Show that all constructors work
        for name, fn in [
            ("skip-dangerous", make_skip_dangerous_agent),
            ("no_bash", make_no_bash_agent),
            ("sandboxed", lambda: make_sandboxed_agent()),
            ("safe_bash", make_safe_bash_agent),
            ("readonly", make_readonly_agent),
            ("async_callback", make_async_callback_agent),
        ]:
            a = fn()
            print(f"  {name}: Agent(model={a.model}, rules={len(a.permission_engine.rules)})")
        return

    await demo_rules()
    await demo_path_rule()
    await demo_readonly_callback()


if __name__ == "__main__":
    asyncio.run(main())
