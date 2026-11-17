"""Live: subagents (foreground and background, tool scope) and skills."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from live_core_support import PROVIDERS, Live, ask, make_config, retry_once, text_of

from linch_rust import (
    ChildRunStatusEvent,
    InitEvent,
    SkillCompletedEvent,
    SkillInvokedEvent,
    SubagentEvent,
    UserEvent,
)

pytestmark = pytest.mark.live

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
    """A workspace with a fact file, and a config dir with a subagent and a skill."""
    # A fresh directory per call: a retried scenario shares `tmp_path` with its first try.
    root = (tmp_path / uuid.uuid4().hex[:6]).resolve()
    workspace, config_dir = root / "ws", root / "cfg"
    workspace.mkdir(parents=True)
    (workspace / "facts.txt").write_text("the vault code is 8842\n")
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "analyst.md").write_text(ANALYST)
    (config_dir / "skills" / "ship-it").mkdir(parents=True)
    (config_dir / "skills" / "ship-it" / "SKILL.md").write_text(SKILL)
    return workspace, config_dir


def agent_config(live: Live, tmp_path: Path, **top: Any) -> Any:
    workspace, config_dir = project(tmp_path)
    fields: dict[str, Any] = {
        "workspace": str(workspace),
        "bash": False,
        "config_dir": str(config_dir),
        "permissions": {"mode": "skip_dangerous", "project_root": str(workspace)},
    }
    fields.update(top)
    return make_config(live, live.spec(max_turns=8), **fields)


def child_events(trace: Any) -> list[Any]:
    return [s.event for s in trace.of(SubagentEvent)]


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_foreground_subagent_runs_and_its_answer_reaches_the_parent(
    live: Live, tmp_path: Path
) -> None:
    config = agent_config(live, tmp_path, subagents=True)
    trace = await ask(
        live,
        "Call the Subagent tool with subagent_type 'analyst' and prompt 'Read facts.txt and "
        "report it.' Then tell me the vault code.",
        config=config,
    )
    [call] = trace.ends("Subagent")
    assert not call.is_error and "8842" in text_of(call)
    wrapped = trace.of(SubagentEvent)
    assert wrapped
    [child_init] = [e for e in child_events(trace) if isinstance(e, InitEvent)]
    assert child_init.tools == ["Read"]  # the definition's scope, not the parent's
    assert child_init.parent_run_id == trace.of(InitEvent)[0].run_id
    child_reads = [e for e in child_events(trace) if getattr(e, "tool_name", None) == "Read"]
    assert child_reads
    assert "8842" in (trace.result.final_text or "")


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_subagent_cannot_use_tools_outside_its_scope(live: Live, tmp_path: Path) -> None:
    config = agent_config(live, tmp_path, subagents=True)
    workspace = Path(config["workspace"])
    trace = await ask(
        live,
        "Call the Subagent tool with subagent_type 'analyst' and this prompt: 'Use the Write "
        "tool to create hacked.txt containing x. If you have no Write tool, answer CANNOT.' "
        "Then report what the subagent said.",
        config=config,
    )
    [child_init] = [e for e in child_events(trace) if isinstance(e, InitEvent)]
    assert child_init.tools == ["Read"]
    assert not (workspace / "hacked.txt").exists()
    assert not any(
        getattr(e, "tool_name", None) == "Write" and not getattr(e, "is_error", False)
        for e in child_events(trace)
    )


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_background_subagent_delivers_its_result_back(live: Live, tmp_path: Path) -> None:
    config = agent_config(live, tmp_path, subagents=True, workers={"background": True})
    trace = await ask(
        live,
        "Call the Subagent tool with subagent_type 'analyst', run_in_background true and prompt "
        "'Read facts.txt and report it.' When it finishes, tell me the vault code.",
        config=config,
    )
    [ack] = trace.ends("Subagent")
    assert "background" in text_of(ack).lower()
    statuses = [s.status for s in trace.of(ChildRunStatusEvent)]
    assert statuses[:1] == ["running"] and statuses[-1] == "completed"
    # The worker's end is a <task-notification> in a logged user message (text or tool result).
    notes = [str(u.message) for u in trace.of(UserEvent) if "<task-notification>" in str(u.message)]
    assert notes and "8842" in notes[-1] and "<status>completed</status>" in notes[-1]
    assert "8842" in (trace.result.final_text or "")


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_the_model_loads_a_skill_and_follows_it(live: Live, tmp_path: Path) -> None:
    config = agent_config(live, tmp_path, skills=True)
    trace = await ask(
        live,
        "Load the skill 'ship-it' with the Skill tool and then follow its instructions.",
        config=config,
    )
    [invoked] = trace.of(SkillInvokedEvent)
    assert invoked.name == "ship-it"
    [completed] = trace.of(SkillCompletedEvent)
    assert completed.name == "ship-it" and not completed.is_error
    [end] = trace.ends("Skill")
    assert "SKILL-TOKEN-5521" in text_of(end)
    assert "SKILL-TOKEN-5521" in (trace.result.final_text or "")
