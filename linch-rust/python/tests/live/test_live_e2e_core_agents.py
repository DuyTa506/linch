"""Live e2e (core): a skill, a subagent, and read-scoped subagents in parallel."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from e2e_core_support import (
    attempts,
    config,
    dump,
    fwd,  # noqa: F401  (fixture)
    needs_e2e,
    opened,
    run_to_end,
    spec,
)
from live_core_support import text_of

from linch_rust import (
    InitEvent,
    SkillCompletedEvent,
    SkillInvokedEvent,
    SubagentEvent,
)

__all__ = ["fwd"]

pytestmark = [pytest.mark.live, needs_e2e]

ANALYST = """\
---
name: analyst
description: Reads one file and reports its content.
tools: [Read]
---
You read the file the prompt names with the Read tool, then answer with its content only,
prefixed by "ANALYST:". Never write anything.
"""

SKILL = """\
---
description: Ship checklist; load it when asked to ship.
when_to_use: When the user asks to ship.
---
# Shipping

Reply with exactly this token and nothing else: SKILL-TOKEN-5521
"""


def project(tmp_path: Path) -> tuple[Path, Path]:
    root = (tmp_path / uuid.uuid4().hex[:6]).resolve()
    workspace, config_dir = root / "ws", root / "cfg"
    workspace.mkdir(parents=True)
    (workspace / "facts.txt").write_text("the vault code is 8842\n")
    (workspace / "more.txt").write_text("the bunker code is 1357\n")
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "analyst.md").write_text(ANALYST)
    (config_dir / "skills" / "ship-it").mkdir(parents=True)
    (config_dir / "skills" / "ship-it" / "SKILL.md").write_text(SKILL)
    return workspace, config_dir


def agent_cfg(fwd: Any, tmp_path: Path, **top: Any) -> Any:
    workspace, config_dir = project(tmp_path)
    fields: dict[str, Any] = {
        "workspace": str(workspace),
        "bash": False,
        "config_dir": str(config_dir),
        "permissions": {"mode": "skip_dangerous", "project_root": str(workspace)},
    }
    fields.update(top)
    return config(fwd, spec(max_turns=8), **fields)


@attempts("12a skill")
async def test_12a_the_model_loads_a_skill_and_follows_it(fwd: Any, tmp_path: Path) -> None:
    cfg = agent_cfg(fwd, tmp_path, skills=True)
    async with opened(cfg) as (_s, agent):
        trace = await run_to_end(
            "12a-skill",
            agent,
            "Load the skill 'ship-it' with the Skill tool and then follow its instructions.",
            fwd=fwd,
        )
    [invoked] = trace.of(SkillInvokedEvent)
    [completed] = trace.of(SkillCompletedEvent)
    [end] = trace.ends("Skill")
    assert invoked.name == "ship-it" and completed.name == "ship-it" and not completed.is_error
    assert "SKILL-TOKEN-5521" in text_of(end)
    assert "SKILL-TOKEN-5521" in (trace.result.final_text or "")


@attempts("12b subagent")
async def test_12b_a_foreground_subagent_answers_the_parent(fwd: Any, tmp_path: Path) -> None:
    cfg = agent_cfg(fwd, tmp_path, subagents=True)
    async with opened(cfg) as (_s, agent):
        trace = await run_to_end(
            "12b-subagent",
            agent,
            "Call the Subagent tool with subagent_type 'analyst' and prompt 'Read facts.txt and "
            "report "
            "it.' Then tell me the vault code.",
            fwd=fwd,
        )
    call = trace.ends("Subagent")[0]
    children = [s.event for s in trace.of(SubagentEvent)]
    [child_init] = [e for e in children if isinstance(e, InitEvent)]
    assert not call.is_error and "8842" in text_of(call)
    assert (
        child_init.tools == ["Read"] and child_init.parent_run_id == trace.of(InitEvent)[0].run_id
    )
    assert "8842" in (trace.result.final_text or "")


@attempts("12c parallel read subagents")
async def test_12c_read_scoped_subagents_run_in_parallel(fwd: Any, tmp_path: Path) -> None:
    cfg = agent_cfg(fwd, tmp_path, subagents=True)
    async with opened(cfg) as (_s, agent):
        trace = await run_to_end(
            "12c-parallel",
            agent,
            "In ONE response make two Subagent calls at once: subagent_type 'analyst' with prompt "
            "'Read facts.txt and report it.', and subagent_type 'analyst' with prompt 'Read "
            "more.txt "
            "and report it.'. Then tell me both codes.",
            fwd=fwd,
        )
    first = [b for b in trace.assistants()[0].message["content"] if b["type"] == "tool_use"]
    assert [b["name"] for b in first] == ["Subagent", "Subagent"], [b["name"] for b in first]
    span: dict[str, list[int]] = {}
    for sub in trace.of(SubagentEvent):
        span.setdefault(sub.child_run_id, []).append(sub.ts_ms)
    windows = sorted((min(v), max(v)) for v in span.values())
    dump("12c-parallel", {"children": len(windows), "windows_ms": windows})
    assert len(windows) == 2
    (a0, a1), (b0, b1) = windows
    assert b0 < a1, f"child lifetimes did not overlap: {windows}"
    final = trace.result.final_text or ""
    assert "8842" in final and "1357" in final
