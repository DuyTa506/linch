from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest


@pytest.fixture
def script_skill(tmp_path: Path):
    from linch.skills.types import Skill, SkillFrontmatter

    skill_dir = tmp_path / "skill with spaces"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "x.py").write_text(
        "from pathlib import Path\n"
        'Path("skill-ran.txt").write_text("executed")\n'
        'print("skill script ran")\n',
        encoding="utf-8",
    )
    return Skill(
        name="script-demo",
        dir=str(skill_dir),
        frontmatter=SkillFrontmatter(name="script-demo", description="Run a bundled script."),
        body=f'{shlex.quote(sys.executable)} "${{SKILL_DIR}}/scripts/x.py"',
    )


async def test_author_quoted_skill_directory_with_spaces_runs_through_bash(
    tmp_path: Path, script_skill
) -> None:
    from linch.tools import ToolContext
    from linch.tools.builtin import BashTool
    from linch.tools.skill import SkillTool

    ctx = ToolContext(cwd=str(tmp_path), session_id="s1", run_id="r1", session_store=None)
    invocation = await SkillTool({script_skill.name: script_skill}).execute(
        {"skill": script_skill.name}, ctx
    )
    marker = tmp_path / "skill-ran.txt"
    assert invocation.is_error is False
    assert not marker.exists()
    command = invocation.content.partition("\n\n")[2]
    bash = BashTool()

    result = await bash.execute(bash.validate({"command": command}), ctx)

    assert result.is_error is False
    assert result.content.splitlines() == ["skill script ran", "", "[Exited 0]"]
    assert marker.read_text() == "executed"


@pytest.mark.parametrize("behavior", ["deny", "allow"])
async def test_outside_workspace_skill_script_obeys_bash_approval(
    tmp_path: Path, script_skill, behavior: str
) -> None:
    from linch.abort import AbortContext
    from linch.events import PermissionRequestEvent, ToolCallEndEvent
    from linch.permissions import CanUseToolRequest, PermissionEngine
    from linch.scheduler import execute_tool_calls
    from linch.skills.substitute import substitute_skill_body
    from linch.tools import ToolRegistry
    from linch.tools.builtin import BashTool
    from linch.types import ToolUseBlock
    from tests.support.scheduler import scheduler_agent, scheduler_session

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands: list[str] = []

    def approve(request: CanUseToolRequest) -> dict[str, str]:
        commands.append(request.input["command"])
        return {"behavior": behavior, "message": "host decision"}

    registry = ToolRegistry()
    registry.register(BashTool())
    agent = scheduler_agent(
        registry,
        cwd=str(workspace),
        permission_engine=PermissionEngine(
            mode="default", can_use_tool=approve, project_root=str(workspace)
        ),
    )
    command = substitute_skill_body(script_skill, "", "s1").partition("\n\n")[2]

    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="script-call", name="Bash", input={"command": command})],
            agent,
            scheduler_session(),
            AbortContext(),
        )
    ]

    expected_command = f'{shlex.quote(sys.executable)} "{script_skill.dir}/scripts/x.py"'
    assert commands == [expected_command]
    assert any(isinstance(event, PermissionRequestEvent) for event in events)
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    marker = workspace / "skill-ran.txt"
    if behavior == "deny":
        assert end.is_error is True
        assert not marker.exists()
    else:
        assert end.is_error is False
        assert end.result.splitlines() == ["skill script ran", "", "[Exited 0]"]
        assert marker.read_text() == "executed"
