"""Deterministic first-release pipeline motifs for vague, common requests.

The builder only overlays approved, static graph shapes on an existing Blueprint.
It never widens permissions, writes host integration code, or invents direct-tool
nodes.  Requests outside these motifs remain on the guarded authoring fallback.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from linch_studio.spec import Blueprint, Diagnostic, validate_blueprint

PipelineMotif = Literal["ci_review", "scheduled_team", "release_readiness"]

_SAFE_DRAFT_CODES = frozenset(
    {
        "semantic.provider_required",
        "semantic.provider_model_required",
        "semantic.headless_limit_required",
        "semantic.headless_permissions",
        "semantic.webhook_signing_secret_required",
    }
)


@dataclass(frozen=True, slots=True)
class PipelinePlan:
    motif: PipelineMotif
    text: str


class PipelineBuildError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        super().__init__("pipeline motif could not produce a safe Studio draft")
        self.diagnostics = diagnostics


def detect_pipeline_motif(instruction: str) -> PipelineMotif | None:
    lowered = instruction.casefold()
    release = any(
        term in lowered
        for term in (
            "release readiness",
            "release-ready",
            "release gate",
            "go/no-go",
            "go no-go",
            "go no go",
            "production release",
            "sẵn sàng phát hành",
            "cổng phát hành",
        )
    )
    review = any(
        term in lowered for term in ("code review", "review pr", "pull request", "reviewer")
    )
    ci = "ci" in lowered or "github" in lowered or "pull request" in lowered or "pr" in lowered
    specialists = sum(
        term in lowered for term in ("security", "performance", "style", "bảo mật", "hiệu năng")
    )
    team = any(
        term in lowered
        for term in ("multi-agent", "multi agent", "subagent", "team", "nhiều agent")
    )
    if release and (team or specialists >= 2):
        return "release_readiness"
    if (review and ci) or specialists >= 2:
        return "ci_review"
    scheduled = any(
        term in lowered for term in ("cron", "schedule", "scheduled", "lập lịch", "lịch")
    )
    if scheduled and team:
        return "scheduled_team"
    return None


def plan_for(motif: PipelineMotif) -> PipelinePlan:
    if motif == "ci_review":
        return PipelinePlan(
            motif=motif,
            text=(
                "1. Add three bounded read-only reviewer subagents: security, performance, and "
                "style.\n"
                "2. Add one CI trigger wrapper. The host fetches the unified diff once and "
                "passes it "
                "as trigger.payload; no fake fetch agent or direct-tool node is created.\n"
                "3. Create a directed workflow where all three reviewers are root nodes, then "
                "merge "
                "their findings with maxConcurrency=3.\n"
                "4. Wrap it in a workflow_run routine with doneWhen: custom_todo so a human "
                "approval "
                "seam controls when the routine is marked done.\n"
                "5. Preserve existing provider, permission, redaction, and unrelated Blueprint "
                "settings."
            ),
        )
    if motif == "release_readiness":
        return PipelinePlan(
            motif=motif,
            text=(
                "1. Add four bounded read-only specialists: security, performance, quality, and "
                "reliability. Each receives the same host-provided release-candidate payload.\n"
                "2. Fan those assessments in to one evidence synthesis step, then a separate risk "
                "triage step so the release brief is traceable to specialist findings.\n"
                "3. Add one CI trigger wrapper. The host supplies candidate metadata, test "
                "results, and artifacts as trigger.payload; Studio does not fetch artifacts or "
                "deploy.\n"
                "4. Use one directed workflow with maxConcurrency=4 for the independent "
                "assessments and a workflow_run routine for each host delivery.\n"
                "5. Gate completion with doneWhen: custom_todo. A human must implement the "
                "explicit go/no-go release decision before the routine is marked done.\n"
                "6. Preserve existing provider, permission, redaction, and unrelated Blueprint "
                "settings."
            ),
        )
    return PipelinePlan(
        motif=motif,
        text=(
            "1. Choose host-owned cron as the repetition owner; the host invokes one bounded "
            "workflow_run routine on its schedule.\n"
            "2. Add planner, worker, and reviewer specialists in one directed fan-out/fan-in "
            "workflow.\n"
            "3. Keep this distinct from LoopRunner: this motif does not claim a durable agent "
            "loop or "
            "deploy a scheduler. The generated trigger wrapper is the host integration seam.\n"
            "4. Use a conservative hourly cron placeholder that the developer must review before "
            "use.\n"
            "5. Preserve existing provider, permission, redaction, and unrelated Blueprint "
            "settings."
        ),
    )


def build_candidate(current: Blueprint, motif: PipelineMotif) -> Blueprint:
    data = deepcopy(current.model_dump(mode="json", by_alias=True))
    spec = data["spec"]
    occupied = {
        str(item["id"])
        for key in ("tools", "subagents", "skills", "workflows", "routines", "triggers")
        for item in spec.get(key, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    def identifier(base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in occupied:
            candidate = f"{base}_{suffix}"
            suffix += 1
        occupied.add(candidate)
        return candidate

    if motif == "ci_review":
        _add_ci_review(spec, identifier)
    elif motif == "scheduled_team":
        _add_scheduled_team(spec, identifier)
    else:
        _add_release_readiness(spec, identifier)
    candidate = Blueprint.model_validate(data, strict=True)
    hard = hard_diagnostics(candidate)
    if hard:
        raise PipelineBuildError(hard)
    return candidate


def hard_diagnostics(candidate: Blueprint) -> tuple[Diagnostic, ...]:
    """Keep only errors that cannot be accepted as a configuration draft."""

    return tuple(
        item
        for item in validate_blueprint(candidate)
        if item.severity == "error" and item.code not in _SAFE_DRAFT_CODES
    )


def readiness_blockers(candidate: Blueprint) -> tuple[Diagnostic, ...]:
    """Known post-design configuration gaps that a human can complete safely."""

    return tuple(
        item
        for item in validate_blueprint(candidate)
        if item.severity == "error" and item.code in _SAFE_DRAFT_CODES
    )


def _add_ci_review(spec: dict[str, object], identifier: object) -> None:
    next_id = identifier  # Typed as object only because JSON-compatible dicts are dynamic.
    assert callable(next_id)
    reviewers = []
    nodes = []
    for base, label, focus in (
        ("security_reviewer", "Security Reviewer", "security"),
        ("performance_reviewer", "Performance Reviewer", "performance"),
        ("style_reviewer", "Style Reviewer", "style"),
    ):
        subagent_id = next_id(base)
        node_id = next_id(base.replace("_reviewer", "_review"))
        reviewers.append(
            {
                "id": subagent_id,
                "displayName": label,
                "description": f"Review a host-provided unified diff for {focus} issues.",
                "instructions": (
                    "Read the raw unified diff from the workflow trigger envelope payload. "
                    f"Report concrete {focus} findings with file/line context when present."
                ),
                "tools": [],
            }
        )
        nodes.append(
            {
                "type": "agent_call",
                "id": node_id,
                "label": label,
                "prompt": (
                    "Review the raw unified diff supplied once by the host in trigger.payload for "
                    f"{focus} concerns."
                ),
                "dependsOn": [],
                "subagent": subagent_id,
                "tools": [],
            }
        )
    merge_id = next_id("merge_reviews")
    nodes.append(
        {
            "type": "agent_call",
            "id": merge_id,
            "label": "Merge Review Report",
            "prompt": "Combine all predecessor reviewer findings into one concise report.",
            "dependsOn": [item["id"] for item in nodes],
            "tools": [],
        }
    )
    workflow_id = next_id("review_workflow")
    trigger_id = next_id("on_pull_request")
    routine_id = next_id("run_review_workflow")
    spec.setdefault("subagents", []).extend(reviewers)  # type: ignore[union-attr]
    spec.setdefault("workflows", []).append(  # type: ignore[union-attr]
        {
            "kind": "directed",
            "id": workflow_id,
            "displayName": "Pull Request Review",
            "description": "Host payload fan-out to three review specialists, then one merge.",
            "nodes": nodes,
            "output": merge_id,
            "maxConcurrency": 3,
        }
    )
    spec.setdefault("triggers", []).append(  # type: ignore[union-attr]
        {
            "kind": "ci",
            "id": trigger_id,
            "displayName": "On Pull Request",
            "provider": "github_actions",
        }
    )
    spec.setdefault("routines", []).append(  # type: ignore[union-attr]
        {
            "kind": "workflow_run",
            "id": routine_id,
            "displayName": "Run Pull Request Review",
            "triggers": [trigger_id],
            "target": workflow_id,
            "doneWhen": {
                "kind": "custom_todo",
                "id": next_id("human_review_gate"),
                "description": (
                    "TODO: implement the human approval decision before marking the review done."
                ),
            },
        }
    )


def _add_scheduled_team(spec: dict[str, object], identifier: object) -> None:
    next_id = identifier
    assert callable(next_id)
    planner_id = next_id("scheduled_planner")
    worker_id = next_id("scheduled_worker")
    reviewer_id = next_id("scheduled_reviewer")
    spec.setdefault("subagents", []).extend(  # type: ignore[union-attr]
        [
            {
                "id": planner_id,
                "displayName": "Scheduled Planner",
                "description": "Plan the current host-provided scheduled task.",
                "instructions": (
                    "Turn the trigger payload into a bounded task plan and identify risks."
                ),
                "tools": [],
            },
            {
                "id": worker_id,
                "displayName": "Scheduled Worker",
                "description": "Perform the bounded analysis described by the plan.",
                "instructions": (
                    "Use predecessor plan context and return a concise result; do not claim "
                    "deployment."
                ),
                "tools": [],
            },
            {
                "id": reviewer_id,
                "displayName": "Scheduled Reviewer",
                "description": "Review scheduled-task output for gaps and next actions.",
                "instructions": (
                    "Review predecessor results and return a bounded, actionable report."
                ),
                "tools": [],
            },
        ]
    )
    plan_node = next_id("plan_scheduled_task")
    work_node = next_id("run_scheduled_task")
    review_node = next_id("review_scheduled_task")
    workflow_id = next_id("scheduled_team_workflow")
    trigger_id = next_id("hourly_host_cron")
    routine_id = next_id("run_scheduled_team")
    spec.setdefault("workflows", []).append(  # type: ignore[union-attr]
        {
            "kind": "directed",
            "id": workflow_id,
            "displayName": "Scheduled Team Workflow",
            "description": "One host-owned cron invocation of a bounded directed team workflow.",
            "nodes": [
                {
                    "type": "agent_call",
                    "id": plan_node,
                    "label": "Plan Scheduled Task",
                    "prompt": "Plan the task supplied by the host trigger envelope.",
                    "dependsOn": [],
                    "subagent": planner_id,
                    "tools": [],
                },
                {
                    "type": "agent_call",
                    "id": work_node,
                    "label": "Run Scheduled Task",
                    "prompt": "Execute the bounded analysis described by the predecessor plan.",
                    "dependsOn": [plan_node],
                    "subagent": worker_id,
                    "tools": [],
                },
                {
                    "type": "agent_call",
                    "id": review_node,
                    "label": "Review Scheduled Result",
                    "prompt": "Review predecessor output and produce the final scheduled report.",
                    "dependsOn": [work_node],
                    "subagent": reviewer_id,
                    "tools": [],
                },
            ],
            "output": review_node,
            "maxConcurrency": 1,
        }
    )
    spec.setdefault("triggers", []).append(  # type: ignore[union-attr]
        {
            "kind": "cron",
            "id": trigger_id,
            "displayName": "Hourly Host Cron (review before use)",
            "cron": "0 * * * *",
            "timezone": "UTC",
        }
    )
    spec.setdefault("routines", []).append(  # type: ignore[union-attr]
        {
            "kind": "workflow_run",
            "id": routine_id,
            "displayName": "Run Scheduled Team",
            "triggers": [trigger_id],
            "target": workflow_id,
            "maxTurns": 12,
            "budget": {"maxTokens": 60_000},
        }
    )


def _add_release_readiness(spec: dict[str, object], identifier: object) -> None:
    """Build a larger fan-out/fan-in graph without pretending Studio deploys a release."""

    next_id = identifier
    assert callable(next_id)
    specialists = []
    nodes = []
    for base, label, focus in (
        ("release_security", "Release Security", "security"),
        ("release_performance", "Release Performance", "performance"),
        ("release_quality", "Release Quality", "test and quality"),
        ("release_reliability", "Release Reliability", "operational reliability"),
    ):
        subagent_id = next_id(f"{base}_specialist")
        node_id = next_id(f"assess_{base.removeprefix('release_')}")
        specialists.append(
            {
                "id": subagent_id,
                "displayName": label,
                "description": f"Assess a host-provided release candidate for {focus} risks.",
                "instructions": (
                    "Read the release-candidate envelope supplied by the host in trigger.payload. "
                    f"Report concrete {focus} evidence, unknowns, and blocking risks."
                ),
                "tools": [],
            }
        )
        nodes.append(
            {
                "type": "agent_call",
                "id": node_id,
                "label": label,
                "prompt": (
                    "Assess the host-provided release candidate in trigger.payload for "
                    f"{focus} risks. Do not claim deployment or fetch external artifacts."
                ),
                "dependsOn": [],
                "subagent": subagent_id,
                "tools": [],
            }
        )
    synthesis_id = next_id("synthesize_release_evidence")
    triage_id = next_id("triage_release_risks")
    nodes.extend(
        [
            {
                "type": "agent_call",
                "id": synthesis_id,
                "label": "Synthesize Release Evidence",
                "prompt": (
                    "Merge all specialist findings into a traceable release-evidence brief, "
                    "retaining uncertainty and conflicts."
                ),
                "dependsOn": [item["id"] for item in nodes],
                "tools": [],
            },
            {
                "type": "agent_call",
                "id": triage_id,
                "label": "Triage Release Risks",
                "prompt": (
                    "Turn the synthesized evidence into a ranked go/no-go recommendation for "
                    "a human release owner; do not mark the release approved."
                ),
                "dependsOn": [synthesis_id],
                "tools": [],
            },
        ]
    )
    workflow_id = next_id("release_readiness_workflow")
    trigger_id = next_id("on_release_candidate")
    routine_id = next_id("run_release_readiness")
    spec.setdefault("subagents", []).extend(specialists)  # type: ignore[union-attr]
    spec.setdefault("workflows", []).append(  # type: ignore[union-attr]
        {
            "kind": "directed",
            "id": workflow_id,
            "displayName": "Release Readiness Review",
            "description": "Four parallel specialists, evidence synthesis, and risk triage.",
            "nodes": nodes,
            "output": triage_id,
            "maxConcurrency": 4,
        }
    )
    spec.setdefault("triggers", []).append(  # type: ignore[union-attr]
        {
            "kind": "ci",
            "id": trigger_id,
            "displayName": "On Release Candidate",
            "provider": "github_actions",
        }
    )
    spec.setdefault("routines", []).append(  # type: ignore[union-attr]
        {
            "kind": "workflow_run",
            "id": routine_id,
            "displayName": "Run Release Readiness",
            "triggers": [trigger_id],
            "target": workflow_id,
            "doneWhen": {
                "kind": "custom_todo",
                "id": next_id("human_release_gate"),
                "description": (
                    "TODO: implement the human go/no-go release decision before marking done."
                ),
            },
        }
    )


__all__ = [
    "PipelineBuildError",
    "PipelineMotif",
    "PipelinePlan",
    "build_candidate",
    "detect_pipeline_motif",
    "hard_diagnostics",
    "plan_for",
    "readiness_blockers",
]
