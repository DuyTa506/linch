"""One-shot host-owned routine contribution pack."""

from __future__ import annotations

import json

from ..contributions import FileContribution
from ..ir import CompilerIR, RoutineIR
from .common import file, py


class RoutinePack:
    capability_id = "execution.routines"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        if not ir.routines:
            return ()
        files: list[FileContribution] = []
        for routine in ir.routines:
            content = (
                _agent_tick_module(ir, routine)
                if routine.kind == "agent_tick"
                else _workflow_run_module(ir, routine)
            )
            files.append(
                file(
                    f"src/{ir.package}/routines/{routine.id}.py",
                    content,
                    f"routine.{routine.id}",
                )
            )
        files.extend(
            [
                file(
                    f"src/{ir.package}/routines/__init__.py",
                    _init_module(ir.routines),
                    "routine.registry",
                ),
                file(
                    f"src/{ir.package}/integrations/triggers.py",
                    _trigger_module(ir),
                    "triggers.host_wrappers",
                ),
                file("tests/test_routines.py", _routine_tests(ir), "tests.routines"),
            ]
        )
        return tuple(files)


def _budget_expr(routine: RoutineIR) -> str:
    budget = json.loads(routine.budget_json)
    args = []
    if budget.get("maxTokens") is not None:
        args.append(f"max_tokens={budget['maxTokens']}")
    if budget.get("maxCostUsd") is not None:
        args.append(f"max_cost_usd={budget['maxCostUsd']!r}")
    args.append(f"warn_ratio={budget['warnRatio']!r}")
    return f"RunBudget({', '.join(args)})"


def _verifier_import(ir: CompilerIR, routine: RoutineIR) -> tuple[str, str, str]:
    if routine.verify is None and routine.done_when is None:
        return "", "None", "None"
    import_line = f"from {ir.package}.completion import verify_result\n"
    verify = (
        f"lambda result: verify_result({py(routine.verify.id)}, result)"
        if routine.verify is not None
        else "None"
    )
    done = (
        f"lambda result, artifacts: verify_result({py(routine.done_when.id)}, result)"
        if routine.done_when is not None
        else "None"
    )
    return import_line, verify, done


def _agent_tick_module(ir: CompilerIR, routine: RoutineIR) -> str:
    completion_import, verify_expr, done_expr = _verifier_import(ir, routine)
    completion_block = f"\n{completion_import}\n" if completion_import else "\n"
    return f'''\
"""{routine.display_name}: one bounded agent tick; this module is not a daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from linch import (
    LoopRunner,
    LoopSpec,
    LoopTickResult,
    LoopTrigger,
    RunBudget,
    RunOptions,
)
{completion_block}MAX_TURNS = {py(routine.max_turns)}


def make_spec(root: str | Path = "domains") -> LoopSpec:
    return LoopSpec(
        id={py(routine.id)},
        charter={py(routine.charter)},
        prompt={py(routine.prompt)},
        root=root,
        run_options=RunOptions(budget={_budget_expr(routine)}),
        session_meta={{"linch_studio_target": "runtime_agent"}},
    )


async def run_{routine.id}_once(
    agent: Any,
    trigger: LoopTrigger | None = None,
    *,
    root: str | Path = "domains",
) -> LoopTickResult:
    """Run one tick. The host owns scheduling and repeated invocation."""
    configured = getattr(agent, "max_turns", None)
    if MAX_TURNS is not None and (configured is None or configured > MAX_TURNS):
        raise ValueError(
            f"routine {routine.id} requires agent max_turns <= {{MAX_TURNS}}; "
            "use build_agent(max_turns_override=MAX_TURNS)"
        )
    runner = LoopRunner(agent, verify={verify_expr}, done_predicate={done_expr})
    return await runner.run_once(make_spec(root), trigger or LoopTrigger(source="manual"))
'''


def _workflow_run_module(ir: CompilerIR, routine: RoutineIR) -> str:
    durable = ir.capabilities()["persistence"]["runStore"] != "in_memory"
    verify_id = routine.verify.id if routine.verify is not None else None
    done_id = routine.done_when.id if routine.done_when is not None else None
    completion_import = (
        f"from {ir.package}.completion import verify_result\n"
        if verify_id is not None or done_id is not None
        else ""
    )
    return f'''\
"""{routine.display_name}: one directed-workflow invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from linch import RunBudget

{completion_import}from {ir.package}.workflows.{routine.target} import run_{routine.target}

DURABLE_RUN_STORE = {py(durable)}
MAX_TURNS = {py(routine.max_turns)}


@dataclass(frozen=True, slots=True)
class RoutineInvocationResult:
    value: Any
    verified: bool
    done: bool


async def run_{routine.id}_once(
    agent: Any,
    trigger: dict[str, Any] | None = None,
    *,
    delivery_id: str | None = None,
) -> RoutineInvocationResult:
    """Run the target workflow directly with one tree-shared invocation budget."""
    configured = getattr(agent, "max_turns", None)
    if MAX_TURNS is not None and (configured is None or configured > MAX_TURNS):
        raise ValueError(
            f"routine {routine.id} requires agent max_turns <= {{MAX_TURNS}}; "
            "use build_agent(max_turns_override=MAX_TURNS)"
        )
    run_id = delivery_id if DURABLE_RUN_STORE and delivery_id else None
    value = await run_{routine.target}(
        agent,
        dict(trigger or {{}}),
        run_id=run_id,
        budget={_budget_expr(routine)},
    )
    verified = {f"verify_result({py(verify_id)}, value)" if verify_id else "True"}
    done = {f"verify_result({py(done_id)}, value)" if done_id else "False"}
    if not verified:
        raise RuntimeError("routine result failed verification")
    return RoutineInvocationResult(value=value, verified=verified, done=done)
'''


def _init_module(routines: tuple[RoutineIR, ...]) -> str:
    lines = []
    exports = []
    for routine in routines:
        exports.append(f"run_{routine.id}_once")
        if routine.kind == "agent_tick":
            lines.append(f"from .{routine.id} import make_spec as make_{routine.id}_spec")
            lines.append(f"from .{routine.id} import run_{routine.id}_once")
            exports.append(f"make_{routine.id}_spec")
        else:
            lines.append(f"from .{routine.id} import run_{routine.id}_once")
    return (
        '"""Generated one-shot routines; the host owns process lifetime."""\n\n'
        + "\n".join(lines)
        + f"\n\n__all__ = {py(exports)}\n"
    )


def _trigger_module(ir: CompilerIR) -> str:
    trigger_map = {item.id: item for item in ir.triggers}
    configs = {item.id: json.loads(item.config_json) for item in ir.triggers}
    imports = []
    functions = []
    webhook_todos = []
    for trigger in ir.triggers:
        if trigger.kind == "webhook":
            webhook_todos.append(
                f"""\
def verify_webhook_signature_{trigger.id}(payload: str, signature: str | None) -> bool:
    del payload, signature
    raise NotImplementedError(
        "TODO: implement blocking webhook signature verification using the configured env reference"
    )
"""
            )
    for routine in ir.routines:
        imports.append(f"from {ir.package}.routines.{routine.id} import run_{routine.id}_once")
        for trigger_id in routine.triggers:
            trigger = trigger_map[trigger_id]
            function_name = f"trigger_{routine.id}_from_{trigger.id}"
            webhook_guard = ""
            if trigger.kind == "webhook":
                webhook_guard = f"    verify_webhook_signature_{trigger.id}(payload, signature)\n"
            if routine.kind == "agent_tick":
                call = (
                    f"return await run_{routine.id}_once(\n"
                    "        agent,\n"
                    "        LoopTrigger(\n"
                    f"            source={py(trigger.kind)},\n"
                    "            id=delivery_id,\n"
                    "            payload=payload,\n"
                    "            metadata=dict(metadata or {}),\n"
                    "        ),\n"
                    "    )"
                )
                return_type = "LoopTickResult"
            else:
                call = (
                    f"return await run_{routine.id}_once(\n"
                    "        agent,\n"
                    "        {\n"
                    f'            "source": {py(trigger.kind)},\n'
                    '            "payload": payload,\n'
                    '            "deliveryId": delivery_id,\n'
                    '            "metadata": dict(metadata or {}),\n'
                    f'            "config": TRIGGER_CONFIG[{py(trigger.id)}],\n'
                    "        },\n"
                    "        delivery_id=delivery_id,\n"
                    "    )"
                )
                return_type = "Any"
            functions.append(
                f"""\
async def {function_name}(
    agent: Any,
    payload: str = "",
    *,
    delivery_id: str | None = None,
    metadata: dict[str, object] | None = None,
    signature: str | None = None,
) -> {return_type}:
{webhook_guard}    {call}
"""
            )
    has_agent_tick = any(routine.kind == "agent_tick" for routine in ir.routines)
    linch_import = "from linch import LoopTickResult, LoopTrigger\n\n" if has_agent_tick else ""
    webhook_section = "\n\n" + "\n\n".join(webhook_todos) if webhook_todos else ""
    return f'''\
"""Host wrappers preserve trigger configuration; no scheduler is started here."""

from __future__ import annotations

from typing import Any

{linch_import}{chr(10).join(sorted(set(imports)))}

TRIGGER_CONFIG: dict[str, dict[str, Any]] = {py(configs)}{webhook_section}


{(chr(10) * 2).join(functions)}
'''


def _routine_tests(ir: CompilerIR) -> str:
    def config_literal(config_json: str) -> str:
        config = json.loads(config_json)
        return (
            "{\n"
            + "\n".join(f"        {py(key)}: {py(value)}," for key, value in sorted(config.items()))
            + "\n    }"
        )

    trigger_assertions = (
        "\n".join(
            f"    assert TRIGGER_CONFIG[{py(item.id)}] == " + config_literal(item.config_json)
            for item in ir.triggers
        )
        or "    assert TRIGGER_CONFIG == {}"
    )
    limit_imports = []
    limit_tests = []
    for routine in ir.routines:
        if routine.max_turns is None:
            continue
        limit_imports.append(
            f"from {ir.package}.routines.{routine.id} import "
            f"MAX_TURNS as {routine.id.upper()}_MAX_TURNS\n"
            f"from {ir.package}.routines.{routine.id} import run_{routine.id}_once"
        )
        limit_tests.append(
            f"""\
async def test_{routine.id}_rejects_an_agent_above_its_turn_ceiling() -> None:
    class OverLimitAgent:
        max_turns = {routine.id.upper()}_MAX_TURNS + 1

    with pytest.raises(ValueError, match="max_turns"):
        await run_{routine.id}_once(OverLimitAgent())
"""
        )
    limit_import_block = "\n".join(limit_imports)
    pytest_import = "import pytest\n\n" if limit_tests else ""
    limit_test_block = "\n\n" + "\n\n".join(limit_tests) if limit_tests else ""
    return f'''\
"""Routine wrappers preserve host-owned trigger configuration."""

{pytest_import}from {ir.package}.integrations.triggers import TRIGGER_CONFIG
{limit_import_block}


def test_trigger_configuration_is_preserved() -> None:
{trigger_assertions}
{limit_test_block}
'''


__all__ = ["RoutinePack"]
