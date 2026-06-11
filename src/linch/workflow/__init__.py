"""Deterministic workflow engine — closed-loop fleet orchestration.

A workflow is a plain async Python function receiving a
:class:`WorkflowContext` (``wf``): the script owns control flow, subagents do
the work, and every ``wf.agent`` result is journaled for resume.

    async def review(wf):
        await wf.phase("Find")
        findings = await wf.parallel([
            lambda: wf.agent("review for bugs"),
            lambda: wf.agent("review for perf"),
        ])
        return findings

    result = await agent.run_workflow(review, budget=RunBudget(max_tokens=500_000))
"""

from ..errors import WorkflowError
from .context import WorkflowContext
from .engine import run_workflow
from .journal import WorkflowJournal, call_key

__all__ = [
    "WorkflowContext",
    "WorkflowError",
    "WorkflowJournal",
    "call_key",
    "run_workflow",
]
