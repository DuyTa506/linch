"""Static directed/replayable-workflow contribution pack."""

from __future__ import annotations

from collections import defaultdict

from ..contributions import FileContribution
from ..ir import CompilerIR, WorkflowIR, WorkflowNodeIR
from .common import file, py


class WorkflowPack:
    capability_id = "execution.directed_workflow"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        if not ir.workflows:
            return ()
        files: list[FileContribution] = []
        exports: list[str] = []
        for workflow in ir.workflows:
            files.append(
                file(
                    f"src/{ir.package}/workflows/{workflow.id}.py",
                    _workflow_module(workflow),
                    f"workflow.{workflow.id}",
                )
            )
            exports.extend([f"flow_{workflow.id}", f"run_{workflow.id}"])
        files.append(
            file(
                f"src/{ir.package}/workflows/__init__.py",
                _init_module(ir.workflows),
                "workflow.registry",
            )
        )
        files.append(
            file(
                "tests/test_workflows.py",
                _workflow_tests(ir),
                "tests.workflow_offline",
            )
        )
        return tuple(files)


def _workflow_module(workflow: WorkflowIR) -> str:
    by_depth: dict[int, list[WorkflowNodeIR]] = defaultdict(list)
    for node in workflow.nodes:
        by_depth[node.depth].append(node)
    blocks: list[str] = []
    for depth in sorted(by_depth):
        nodes = sorted(by_depth[depth], key=lambda item: item.declaration_index)
        phases = [node.phase for node in nodes if node.phase]
        if phases:
            blocks.append(f"    await wf.phase({py(phases[0])})")
        thunk_lines = []
        for node in nodes:
            tools = list(node.tools) if node.tools else None
            if node.depends_on:
                predecessor_expr = "[\n" + "\n".join(
                    f"                        ({py(parent)}, results[{py(parent)}]),"
                    for parent in node.depends_on
                )
                predecessor_expr += "\n                    ]"
            else:
                predecessor_expr = "[]"
            thunk_lines.append(
                "            lambda: wf.agent(\n"
                "                _node_prompt(\n"
                f"                    {py(node.prompt)},\n"
                "                    trigger,\n"
                f"                    {predecessor_expr},\n"
                "                ),\n"
                f"                name={py(node.subagent)},\n"
                f"                label={py(node.label)},\n"
                f"                tools={py(tools)},\n"
                "            ),"
            )
        ids = [node.id for node in nodes]
        thunks = "\n".join(thunk_lines)
        blocks.append(f"    batch = await wf.parallel(\n        [\n{thunks}\n        ]\n    )")
        blocks.append(
            f"    for node_id, value in zip({py(ids)}, batch, strict=True):\n"
            "        results[node_id] = value"
        )
    body = "\n".join(blocks)
    return f'''\
"""{workflow.display_name}: compiled static acyclic agent-call workflow."""

from __future__ import annotations

import json
from typing import Any


def _node_prompt(
    instruction: str,
    trigger: dict[str, Any],
    predecessors: list[tuple[str, Any]],
) -> str:
    envelope = {{
        "trigger": trigger,
        "predecessors": [{{"node": node_id, "result": result}} for node_id, result in predecessors],
    }}
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{{instruction}}\\n\\n<linch-studio-input>\\n{{payload}}\\n</linch-studio-input>"


async def flow_{workflow.id}(wf: Any, trigger: dict[str, Any]) -> Any:
    """Run the compiler-owned directed graph inside a replayable WorkflowContext."""
    results: dict[str, Any] = {{}}
{body}
    return results[{py(workflow.output)}]


async def run_{workflow.id}(
    agent: Any,
    trigger: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    budget: Any = None,
) -> Any:
    """Run via the public Agent.run_workflow entry point."""
    payload = dict(trigger or {{}})

    async def flow(wf: Any) -> Any:
        return await flow_{workflow.id}(wf, payload)

    return await agent.run_workflow(
        flow,
        budget=budget,
        run_id=run_id,
        max_concurrency={workflow.max_concurrency},
    )
'''


def _init_module(workflows: tuple[WorkflowIR, ...]) -> str:
    lines = []
    exports = []
    for workflow in workflows:
        lines.append(f"from .{workflow.id} import flow_{workflow.id}, run_{workflow.id}")
        exports.extend([f"flow_{workflow.id}", f"run_{workflow.id}"])
    return (
        '"""Compiled directed, replayable workflows."""\n\n'
        + "\n".join(lines)
        + f"\n\n__all__ = {py(exports)}\n"
    )


def _workflow_tests(ir: CompilerIR) -> str:
    imports = "\n".join(
        f"from {ir.package}.workflows.{workflow.id} import flow_{workflow.id}"
        for workflow in ir.workflows
    )
    tests: list[str] = []
    for workflow in ir.workflows:
        output_node = next(node for node in workflow.nodes if node.id == workflow.output)
        expected = output_node.label
        execution_order = sorted(
            workflow.nodes,
            key=lambda node: (node.depth, node.declaration_index),
        )
        tool_filters = [list(node.tools) if node.tools else None for node in execution_order]
        tests.append(
            f"""\
async def test_{workflow.id}_order_and_envelopes() -> None:
    wf = FakeWorkflow()
    result = await flow_{workflow.id}(wf, {{"source": "test"}})
    assert result == {py(expected)}
    assert wf.labels == {py([node.label for node in execution_order])}
    assert wf.tool_filters == {py(tool_filters)}
    for prompt in wf.prompts:
        after_marker = prompt.split("<linch-studio-input>\\n", 1)[1]
        payload = after_marker.split("\\n</linch-studio-input>", 1)[0]
        envelope = json.loads(payload)
        assert envelope["trigger"] == {{"source": "test"}}
"""
        )
    tests_text = "\n\n".join(tests)
    return f'''\
"""Offline graph tests cover declaration order and fixed input envelopes."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

{imports}


class FakeWorkflow:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.prompts: list[str] = []
        self.phases: list[str] = []
        self.tool_filters: list[list[str] | None] = []

    async def phase(self, title: str) -> None:
        self.phases.append(title)

    async def agent(
        self,
        prompt: str,
        *,
        name: str | None = None,
        label: str | None = None,
        tools: list[str] | None = None,
    ) -> str:
        self.prompts.append(prompt)
        self.labels.append(label or name or "agent")
        self.tool_filters.append(None if tools is None else list(tools))
        return label or name or "agent"

    async def parallel(self, thunks: list[Callable[[], Awaitable[Any]]]) -> list[Any]:
        return [await thunk() for thunk in thunks]


{tests_text}
'''


__all__ = ["WorkflowPack"]
