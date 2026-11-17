"""Live toolsec: one skill and one read-only subagent with a real model (vLLM through the proxy).

Expectations are in `expectations/toolsec.md` section 5. Tiny prompts, `max_output_tokens` 300.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from live_core_support import VLLM, ask, first, make_config, retry_once, text_of
from ts_support import needs_vllm, record

from linch_rust import (
    InitEvent,
    SkillCompletedEvent,
    SkillInvokedEvent,
    SubagentEvent,
)

pytestmark = pytest.mark.live

LOOKUP = """\
---
name: lookup
description: Reads one file and reports its content.
tools: [Read]
---
Read the file the prompt names with the Read tool and answer with its content only.
"""

SKILL = """\
---
description: Launch checklist; load it when asked to launch.
when_to_use: When the user asks to launch.
---
# Launching

Reply with exactly this token and nothing else: TSKILL-7731
"""


def project(tmp_path: Path) -> tuple[Path, Path]:
    root = (tmp_path / uuid.uuid4().hex[:6]).resolve()
    workspace, config_dir = root / "ws", root / "cfg"
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_text("the beacon number is 3190\n")
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "lookup.md").write_text(LOOKUP)
    (config_dir / "skills" / "launch-it").mkdir(parents=True)
    (config_dir / "skills" / "launch-it" / "SKILL.md").write_text(SKILL)
    return workspace, config_dir


def config_for(tmp_path: Path, **top: Any) -> Any:
    workspace, config_dir = project(tmp_path)
    fields: dict[str, Any] = {
        "workspace": str(workspace),
        "bash": False,
        "config_dir": str(config_dir),
        "permissions": {"mode": "skip_dangerous", "project_root": str(workspace)},
    }
    fields.update(top)
    return make_config(VLLM, VLLM.spec(max_turns=8, max_output_tokens=300), **fields)


@needs_vllm
@retry_once
async def test_the_model_invokes_a_skill(tmp_path: Path) -> None:
    trace = await ask(
        VLLM,
        "Load the skill 'launch-it' with the Skill tool, then follow it.",
        config=config_for(tmp_path, skills=True),
    )
    [invoked] = trace.of(SkillInvokedEvent)
    [completed] = trace.of(SkillCompletedEvent)
    [end] = trace.ends("Skill")
    record(
        "agents",
        "5.1",
        skill=invoked.name,
        is_error=completed.is_error,
        tool_text=text_of(end)[:200],
        final=(trace.result.final_text or "")[:120],
    )
    assert invoked.name == "launch-it" and not completed.is_error
    assert "TSKILL-7731" in text_of(end) and "TSKILL-7731" in (trace.result.final_text or "")


@needs_vllm
@retry_once
async def test_a_read_only_subagent_answers_and_cannot_write(tmp_path: Path) -> None:
    config = config_for(tmp_path, subagents=True)
    workspace = Path(config["workspace"])
    trace = await ask(
        VLLM,
        "Call the Subagent tool with subagent_type 'lookup' and prompt 'Read notes.txt and "
        "report it.' Then tell me the beacon number.",
        config=config,
    )
    call = first(trace.ends("Subagent"), "a Subagent call")
    children = [s.event for s in trace.of(SubagentEvent)]
    child_init = [e for e in children if isinstance(e, InitEvent)]
    record(
        "agents",
        "5.2",
        subagent_text=text_of(call)[:200],
        child_tools=child_init[0].tools if child_init else None,
        final=(trace.result.final_text or "")[:120],
    )
    assert not call.is_error and "3190" in text_of(call)
    assert child_init and child_init[0].tools == ["Read"]
    assert "3190" in (trace.result.final_text or "")
    assert not (workspace / "hacked.txt").exists()
