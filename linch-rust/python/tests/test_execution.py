"""`AgentConfig.execution`: where `Bash` runs, end to end against the server."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    DockerExecution,
    OpenError,
    SandboxExecution,
    Server,
    ToolCallEndEvent,
)

TOOLS = Path(__file__).resolve().parents[2] / "crates/linch-tools/src/exec"


def fields_of(source: Path, struct: str) -> set[str]:
    body = source.read_text(encoding="utf-8").split(f"pub struct {struct} {{", 1)[1]
    return set(re.findall(r"^\s*pub (\w+):", body.split("\n}", 1)[0], re.MULTILINE))


@pytest.mark.source
def test_the_typed_backends_name_every_field_the_server_reads() -> None:
    sandbox = fields_of(TOOLS / "sandbox.rs", "SandboxPolicy")
    assert sandbox | {"backend"} == SandboxExecution.__required_keys__ | (
        SandboxExecution.__optional_keys__
    )
    docker = fields_of(TOOLS / "docker.rs", "DockerPolicy")
    assert docker | {"backend"} == DockerExecution.__required_keys__ | (
        DockerExecution.__optional_keys__
    )


async def bash_result(tmp_path: Path, execution: SandboxExecution | None) -> str:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    (tmp_path / "secret").write_text("hidden")
    turns = [tool_turn("c1", "Bash", {"command": "cat ../secret"}), text_turn("done")]
    extra = {"execution": execution} if execution is not None else {}
    cfg = config(turns, mode="skip_dangerous", workspace=str(root), **extra)
    cfg["permissions"] = {"mode": "skip_dangerous", "project_root": str(root)}
    async with Server() as server:
        agent = await server.open_agent(cfg)
        run = await agent.run("s1", "go")
        ends = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
    return "".join(part.get("text", "") for part in ends[0].result["content"])


async def test_a_sandboxed_bash_cannot_read_beside_its_workspace(tmp_path: Path) -> None:
    try:
        confined = await bash_result(tmp_path, {"backend": "sandbox"})
    except OpenError as error:
        pytest.skip(f"the sandbox cannot be enforced here: {error}")
    assert "Permission denied" in confined, confined
    assert "hidden" in await bash_result(tmp_path, None)


async def test_an_unusable_backend_fails_the_open(tmp_path: Path) -> None:
    turns = [text_turn("hi")]
    docker: DockerExecution = {"backend": "docker", "docker_path": "/nonexistent", "image": " "}
    cfg = config(turns, workspace=str(tmp_path), execution=docker)
    async with Server() as server:
        with pytest.raises(OpenError):
            await server.open_agent(cfg)
