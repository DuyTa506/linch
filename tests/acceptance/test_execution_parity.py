from __future__ import annotations

import asyncio
import os
import shlex
import socket
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


class _SpillProvider(ScriptedProvider):
    async def stream(self, request: Any):
        if len(self.requests) == 1:
            text = " ".join(
                str(getattr(b, "content", "")) for m in request.messages for b in m.content
            )
            if "[stdout_path: " in text:
                path = text.split("[stdout_path: ", 1)[1].split("]", 1)[0]
                self._turns[1] = ToolUseTurn("Read", {"file_path": path})
        async for event in super().stream(request):
            yield event


@pytest.fixture
def model() -> Any:
    def factory(turns: list[Any], prompt: str) -> dict[str, Any]:
        provider = (
            _SpillProvider(turns) if prompt.startswith("Run 5 MB") else ScriptedProvider(turns)
        )
        return {"provider": provider, "model": "test-model"}

    return factory


def _agent(
    root: Path, model: Any, turns: list[Any], prompt: str, tools: Any = None, **opts: Any
) -> Any:
    from linch.tools.registry import workspace_tools

    cfg = dict(model(turns, prompt))
    cfg.update(cwd=str(root), tools=tools or workspace_tools(), **opts)
    return make_agent(cfg.pop("provider", None), **cfg)


async def _run(
    root: Path, model: Any, turns: list[Any], prompt: str, tools: Any = None, **opts: Any
) -> list[Any]:
    agent = _agent(root, model, turns, prompt, tools, **opts)
    try:
        events = []
        provider = agent.provider
        async for event in (await agent.session()).run(prompt):
            events.append(event)
            if event.type != "tool_call_end" or not isinstance(provider, ScriptedProvider):
                continue
            for turn in provider._turns[provider._index :]:
                if not isinstance(turn, ToolUseTurn):
                    continue
                if turn.tool_input.get("id") == "<process-id>" and event.tool_name == "Bash":
                    process_id = event.tool_result.metadata.get("process_id")
                    if process_id:
                        turn.tool_input["id"] = process_id
                if (
                    turn.tool_input.get("command") == "<skill-command>"
                    and event.tool_name == "Skill"
                ):
                    turn.tool_input["command"] = event.tool_result.content.partition("\n\n")[2]
        return events
    finally:
        await agent.close()


async def _collect(session: Any, prompt: str) -> list[Any]:
    return [e async for e in session.run(prompt)]


def _ends(events: list[Any], name: str) -> list[Any]:
    return [e for e in events if e.type == "tool_call_end" and e.tool_name == name]


def _turns(*specs: tuple[str, Any]) -> list[Any]:
    return [TextTurn(v) if n == "text" else ToolUseTurn(n, v) for n, v in specs]


def _write(root: Path, files: dict[str, str]) -> None:
    for name, value in files.items():
        (root / name).write_text(value)


def _port() -> int:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except PermissionError:
        pytest.skip("sandbox denies local sockets")


def _case(case: str, root: Path) -> tuple[list[Any], str, Any]:
    py = shlex.quote(sys.executable)
    if case == "edit":
        _write(
            root,
            {
                "calc.py": "def add(a,b): return a-b+0\n",
                "test_calc.py": "from calc import add\ndef test_add(): assert add(2,3)==5\n",
            },
        )
        cmd = f"{py} -m pytest -q test_calc.py"
        return (
            _turns(
                ("Bash", {"command": cmd}),
                ("Read", {"file_path": "calc.py"}),
                ("Edit", {"file_path": "calc.py", "old_string": "a-b+0", "new_string": "a+b"}),
                ("Bash", {"command": cmd}),
                ("text", "green"),
            ),
            "Run the failing test, Read calc.py, Edit it, and rerun pytest.",
            None,
        )
    if case == "long":
        _write(root, {"test_slow.py": "import time\ndef test_slow(): time.sleep(.2)\n"})
        cmd = f"{py} -m pytest -q test_slow.py"
        return (
            _turns(
                ("Bash", {"command": cmd, "timeout_ms": 20}),
                ("BashProcess", {"action": "wait", "id": "<process-id>", "wait_ms": 1000}),
                ("text", "done"),
            ),
            "Run slow pytest, then wait with BashProcess.",
            None,
        )
    if case == "server":
        port = _port()
        probe = (
            "import time, urllib.request\n"
            "deadline = time.monotonic() + 5\n"
            "while True:\n"
            " try:\n"
            f"  response = urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=1)\n"
            "  print(response.status); break\n"
            " except OSError:\n"
            "  if time.monotonic() >= deadline: raise\n"
        )
        return (
            _turns(
                (
                    "Bash",
                    {"command": f"{py} -m http.server {port} --bind 127.0.0.1", "timeout_ms": 500},
                ),
                ("Bash", {"command": f"{py} -c {shlex.quote(probe)}"}),
                ("BashProcess", {"action": "kill", "id": "<process-id>"}),
                ("text", "stopped"),
            ),
            "Start the HTTP server, request it, then stop its process.",
            None,
        )
    if case in ("spill", "flood"):
        size = "5M" if case == "spill" else "300M"
        cmd = f"{{ printf 'HEAD-E9\\n'; yes | head -c {size}; printf '\\nTAIL-E9\\n'; }}"
        if case == "flood":
            return _turns(("Bash", {"command": cmd}), ("text", "done")), "Flood stdout.", None
        return (
            _turns(("Bash", {"command": cmd}), ("text", "read spill"), ("text", "done")),
            "Run 5 MB, inspect its tail, and Read its stdout spill file.",
            None,
        )
    if case == "skill":
        from linch.skills.types import Skill, SkillFrontmatter
        from linch.tools.registry import workspace_tools
        from linch.tools.skill import SkillTool

        directory = root / "skill with spaces"
        (directory / "scripts").mkdir(parents=True)
        (directory / "scripts" / "x.py").write_text(
            "from pathlib import Path\nPath('skill-ran.txt').write_text('ran')\n"
        )
        skill = Skill(
            "demo",
            str(directory),
            SkillFrontmatter("demo", "run script"),
            f'{py} "${{SKILL_DIR}}/scripts/x.py"',
        )
        tools = workspace_tools()
        tools.register(SkillTool({"demo": skill}))
        command = "<skill-command>"
        return (
            _turns(("Skill", {"skill": "demo"}), ("Bash", {"command": command}), ("text", "ran")),
            "Invoke demo and run its script through SKILL_DIR.",
            tools,
        )
    _write(
        root,
        {
            "ask.py": (
                "import sys\nif '--yes' not in sys.argv:\n input('Continue? ')\n"
                "print('recovered')\n"
            )
        },
    )
    command = f"{py} ask.py"
    return (
        _turns(
            ("Bash", {"command": command}),
            ("Bash", {"command": f"{command} --yes"}),
            ("text", "recovered"),
        ),
        "Run ask.py, then retry after EOF with --yes.",
        None,
    )


def _check(case: str, events: list[Any], root: Path) -> None:
    if case == "edit":
        runs = _ends(events, "Bash")
        assert runs[0].is_error and "1 failed" in runs[0].result
        assert not _ends(events, "Edit")[0].is_error
        assert not runs[1].is_error and "1 passed" in runs[1].result
        assert "a+b" in (root / "calc.py").read_text()
    elif case == "long":
        first, wait = _ends(events, "Bash")[0], _ends(events, "BashProcess")[0]
        assert first.tool_result.metadata.get("process_id")
        assert not wait.is_error and "1 passed" in wait.result
    elif case == "server":
        bash = _ends(events, "Bash")
        assert not bash[1].is_error and "200" in bash[1].result
        assert not _ends(events, "BashProcess")[0].is_error
    elif case == "spill":
        bash = _ends(events, "Bash")[0]
        assert "HEAD-E9" in bash.result and "TAIL-E9" in bash.result
        assert bash.tool_result.truncated and bash.tool_result.metadata.get("stdout_path")
        read = _ends(events, "Read")
        assert read and not read[0].is_error and "HEAD-E9" in read[0].result
        assert len(read[0].result) < 100_000
    elif case == "flood":
        bash = _ends(events, "Bash")[0]
        assert "HEAD-E9" in bash.result and "TAIL-E9" in bash.result
        assert bash.tool_result.truncated and not bash.tool_result.metadata.get("stdout_path")
    elif case == "skill":
        skill = _ends(events, "Skill")[0]
        assert not skill.is_error and "${SKILL_DIR}" not in skill.result
        assert (root / "skill-ran.txt").read_text() == "ran"
    else:
        runs = _ends(events, "Bash")
        assert runs[0].is_error and "EOF" in runs[0].result
        assert not runs[1].is_error and "recovered" in runs[1].result


CASES = [
    "edit",
    "long",
    "server",
    "spill",
    "flood",
    "stdin",
    "skill",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_execution(case: str, tmp_path: Path, model: Any) -> None:
    turns, prompt, tools = _case(case, tmp_path)
    _check(case, await _run(tmp_path, model, turns, prompt, tools), tmp_path)


_SERVER = "\n".join(
    [
        "import anyio, os",
        "from pathlib import Path",
        "from mcp import types",
        "from mcp.server.lowlevel import Server",
        "from mcp.server.stdio import stdio_server",
        'schema={"type":"object","properties":{"command":{"type":"string"},"hang":{"type":"boolean"},"marker":{"type":"string"}},"required":["marker"]}',
        "async def list_tools(_ctx, _params):",
        ' ann=types.ToolAnnotations(readOnlyHint=True) if os.getenv("ANNOTATED") else None',
        ' return types.ListToolsResult(tools=[types.Tool(name="terminal", inputSchema=schema, annotations=ann)])',  # noqa: E501
        "async def call_tool(_ctx, params):",
        " args=params.arguments",
        ' Path(args["marker"]).write_text("entered")',
        ' if args.get("hang"): await anyio.sleep(30)',
        ' return types.CallToolResult(content=[types.TextContent(text="ran:" + str(args.get("command", "")))])',  # noqa: E501
        "async def main():",
        ' server=Server("fixture",on_list_tools=list_tools,on_call_tool=call_tool)',
        " async with stdio_server() as (reader,writer):",
        "  await server.run(reader,writer,server.create_initialization_options())",
        'anyio.run(main,backend="asyncio")',
    ]
)


async def _wait(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"marker did not appear: {path}")


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["default", "trusted", "allow"])
async def test_mcp_terminal(variant: str, tmp_path: Path, model: Any) -> None:
    pytest.importorskip("mcp")
    from linch import FeatureFlags, McpStdioServerConfig
    from linch.permissions import ToolRule

    name, approvals = "mcp__fixture__terminal", []
    env = {"PATH": os.environ.get("PATH", "")}
    if variant == "trusted":
        env["ANNOTATED"] = "1"
    config = McpStdioServerConfig(
        command=sys.executable,
        args=["-c", _SERVER],
        env=env,
        trust_annotations=variant == "trusted",
    )

    def approve(request: Any) -> dict[str, str]:
        approvals.append(request.tool_name)
        return {"behavior": "allow"}

    cast(Any, approve).resume_policy_id = "acceptance.approve"
    permissions: dict[str, Any] = {"mode": "default", "can_use_tool": approve}
    if variant == "allow":
        permissions["rules"] = [ToolRule("mcp__fixture__*", "allow")]
    marker = tmp_path / f"{variant}.marker"
    args = {
        "command": "hang" if variant == "default" else "touch",
        "hang": variant == "default",
        "marker": str(marker),
    }
    agent = _agent(
        tmp_path,
        model,
        [ToolUseTurn(name, args), TextTurn("done")],
        "Use terminal.",
        mcp_servers={"fixture": config},
        features=FeatureFlags(mcp=True),
        permissions=permissions,
    )
    try:
        async with asyncio.timeout(5):
            session = await agent.session()
        tool = agent.tools.get(name)
        assert tool and (
            (variant == "trusted" and tool.scope == "read" and tool.parallel)
            or (variant != "trusted" and tool.scope == "exec")
        )
        if variant == "default":
            task = asyncio.create_task(_collect(session, "Use terminal and wait."))
            await _wait(marker)
            session.abort()
            events = await task
            assert events[-1].subtype == "aborted" and approvals == [name]
            await agent.close()
            restart = _agent(
                tmp_path,
                model,
                [
                    ToolUseTurn(
                        name, {"command": "recover", "marker": str(tmp_path / "restart.marker")}
                    ),
                    TextTurn("done"),
                ],
                "Recover.",
                mcp_servers={"fixture": config},
                features=FeatureFlags(mcp=True),
                permissions={"mode": "skip-dangerous"},
            )
            try:
                result = await _collect(await restart.session(), "Recover.")
                assert (tmp_path / "restart.marker").exists() and result[-1].subtype == "success"
            finally:
                await restart.close()
            return
        result = await _collect(session, "Use terminal.")
        assert result[-1].subtype == "success" and marker.exists() and not approvals
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_busy_session_rejects_second_run(tmp_path: Path, model: Any) -> None:
    from linch import ConfigError

    agent = _agent(
        tmp_path, model, [ToolUseTurn("Bash", {"command": "sleep 30"}), TextTurn("done")], "first"
    )
    try:
        session = await agent.session()
        task = asyncio.create_task(_collect(session, "first"))
        for _ in range(100):
            if getattr(session, "_processes", None) and session._processes.entries:
                break
            await asyncio.sleep(0.01)
        assert session._processes and session._processes.entries
        with pytest.raises(ConfigError, match="active run"):
            session.run("second")
        session.abort()
        assert (await task)[-1].subtype == "aborted"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_resume_after_version_bump(
    tmp_path: Path, model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import linch._version as version
    from linch import InMemoryRunStore, InMemorySessionStore

    runs, sessions = InMemoryRunStore(), InMemorySessionStore()
    monkeypatch.setattr(version, "get_version", lambda: "3.1.0")
    first = _agent(
        tmp_path, model, [TextTurn("unused")], "go", run_store=runs, session_store=sessions
    )
    try:
        session = await first.session(id="resume")
        iterator = session.run("go")
        event = await anext(iterator)
        await iterator.aclose()
        record = await runs.load_run(event.run_id)
        assert record and record.meta["runtime_version"] == "3.1.0"
    finally:
        await first.close()
    monkeypatch.setattr(version, "get_version", lambda: "3.1.1")
    second = _agent(
        tmp_path, model, [TextTurn("resumed")], "go", run_store=runs, session_store=sessions
    )
    try:
        resumed = await second.session(id="resume")
        events = [e async for e in resumed.resume(event.run_id)]
        assert events[-1].subtype == "success"
    finally:
        await second.close()
