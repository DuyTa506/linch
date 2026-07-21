"""Per-component generated implementation and operations documentation."""

from __future__ import annotations

import json

from ..contributions import FileContribution
from ..ir import CompilerIR, VerifierIR
from .common import file


class ComponentDocsPack:
    capability_id = "docs.components"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        files: list[FileContribution] = []
        for tool in ir.tools:
            todo = f"src/{ir.package}/tools/{tool.id}.py:{tool.id}"
            attached_to_primary = ir.primary_tools is None or tool.id in ir.primary_tools
            runtime_mapping = (
                f"Registered in src/{ir.package}/agent.py with scope={tool.scope}."
                if attached_to_primary
                else (
                    f"Generated with scope={tool.scope}, but excluded from the primary runtime "
                    "registry by runtime.agent.tools. Attach it there before expecting the "
                    "primary agent or its invocation tree to execute it."
                )
            )
            files.append(
                _component(
                    tool.id,
                    "Tool",
                    tool.description,
                    runtime=runtime_mapping,
                    todo=todo,
                    security=(
                        "Read-only by declaration; still validate external input and output."
                        if tool.scope == "read"
                        else "Mutating/exec tool: require explicit permissions and idempotency."
                    ),
                    example=f"pytest tests/test_tools.py -k {tool.id}",
                )
            )
        for subagent in ir.subagents:
            files.append(
                _component(
                    subagent.id,
                    "Subagent",
                    subagent.description or subagent.instructions,
                    runtime=(
                        f"Definition in src/{ir.package}/subagents/{subagent.id}.py; inherits the "
                        "runtime turn ceiling and the shared RunBudget for the invocation tree."
                    ),
                    todo="No generated implementation TODO; review instructions and tool scope.",
                    security=(
                        "Its tool set is an allow-list, never an independent permission bypass."
                    ),
                    example="pytest tests/test_agent.py",
                )
            )
        for workflow in ir.workflows:
            files.append(
                _component(
                    workflow.id,
                    "Directed workflow",
                    workflow.description or workflow.display_name,
                    runtime=(
                        f"run_{workflow.id} in src/{ir.package}/workflows/{workflow.id}.py; "
                        "declaration-stable fan-out/fan-in uses Linch workflow journaling."
                    ),
                    todo="No generated implementation TODO; test replay before changing prompts.",
                    security=(
                        "An omitted node tool filter inherits the bound subagent defaults; an "
                        "explicit filter narrows access. Filter changes invalidate journal replay, "
                        "and the agent permission policy remains authoritative."
                    ),
                    example=f"pytest tests/test_workflows.py -k {workflow.id}",
                )
            )
        verifier_by_id: dict[str, VerifierIR] = {}
        for verifier in ir.completion.verifiers:
            verifier_by_id[verifier.id] = verifier
        for routine in ir.routines:
            for verifier in (routine.verify, routine.done_when):
                if verifier is not None:
                    verifier_by_id[verifier.id] = verifier
        for verifier in verifier_by_id.values():
            config = json.loads(verifier.config_json)
            custom = verifier.kind == "custom_todo"
            files.append(
                _component(
                    verifier.id,
                    "Verifier",
                    str(config.get("description") or verifier.kind),
                    runtime=(
                        f"GeneratedVerifier in src/{ir.package}/completion.py; root completion "
                        "skips child sessions and stops on exceptions or exhaustion."
                    ),
                    todo=(
                        f"src/{ir.package}/completion.py:GeneratedVerifier.verify — implement "
                        f"{verifier.id}; the seam blocks until implemented."
                        if custom
                        else "No implementation TODO; review deterministic expected values/schema."
                    ),
                    security="Treat verifier input as untrusted output; never execute it.",
                    example="pytest tests/test_completion.py",
                )
            )
        for routine in ir.routines:
            files.append(
                _component(
                    routine.id,
                    "Routine",
                    routine.display_name,
                    runtime=(
                        f"run_{routine.id}_once in src/{ir.package}/routines/{routine.id}.py; "
                        + (
                            "uses LoopRunner.run_once()."
                            if routine.kind == "agent_tick"
                            else f"calls run_{routine.target}() directly with one shared budget."
                        )
                        + (
                            f" It rejects agents configured above its {routine.max_turns}-turn "
                            "ceiling."
                            if routine.max_turns is not None
                            else ""
                        )
                    ),
                    todo=(
                        "Implement every linked webhook signature verifier in "
                        f"src/{ir.package}/integrations/triggers.py before accepting webhooks."
                    ),
                    security=(
                        "The host owns scheduling, process lifetime, delivery authentication, "
                        "and secrets."
                    ),
                    example=f"pytest tests/test_routines.py -k {routine.id}",
                )
            )
        return tuple(files)


def _component(
    identifier: str,
    kind: str,
    summary: str,
    *,
    runtime: str,
    todo: str,
    security: str,
    example: str,
) -> FileContribution:
    content = f"""\
# {identifier}

Type: {kind}

{summary}

## Runtime mapping

{runtime}

## Implementation order / exact TODO

{todo}

## Security constraints

{security}

## Run example

```bash
{example}
```
"""
    return file(f"docs/components/{identifier}.md", content, "docs.component")


__all__ = ["ComponentDocsPack"]
