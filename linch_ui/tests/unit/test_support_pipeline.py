"""Deterministic support motifs prevent vague requests from producing fake graph nodes."""

from __future__ import annotations

from linch_studio.compiler import compile_blueprint
from linch_studio.spec import Blueprint, default_blueprint, validate_blueprint
from linch_studio.support.pipeline import (
    build_candidate,
    detect_pipeline_motif,
    hard_diagnostics,
    plan_for,
)


def test_ci_review_motif_uses_host_payload_parallel_roots_and_human_done_gate() -> None:
    motif = detect_pipeline_motif(
        "Làm team review PR gồm security, performance và style rồi gom báo cáo; "
        "người duyệt xong mới done."
    )
    assert motif == "ci_review"
    candidate = build_candidate(default_blueprint("demo"), motif)

    workflow = candidate.spec.workflows[-1]
    routine = candidate.spec.routines[-1]
    assert [node.id for node in workflow.nodes] == [
        "security_review",
        "performance_review",
        "style_review",
        "merge_reviews",
    ]
    assert all(node.depends_on == [] for node in workflow.nodes[:3])
    assert workflow.nodes[-1].depends_on == [
        "security_review",
        "performance_review",
        "style_review",
    ]
    assert workflow.max_concurrency >= 3
    assert all("trigger.payload" in node.prompt for node in workflow.nodes[:3])
    assert routine.kind == "workflow_run"
    assert routine.done_when is not None
    assert routine.done_when.kind == "custom_todo"
    assert "fetch_diff" not in {node.id for node in workflow.nodes}
    assert not hard_diagnostics(candidate)
    assert {item.code for item in validate_blueprint(candidate)} >= {
        "semantic.provider_required",
        "semantic.provider_model_required",
    }
    assert "host fetches the unified diff once" in plan_for(motif).text


def test_scheduled_team_motif_chooses_host_cron_not_an_invented_looprunner_stack() -> None:
    motif = detect_pipeline_motif("Create a scheduled cron multi-agent team that runs a review.")
    assert motif == "scheduled_team"
    candidate = build_candidate(default_blueprint("demo"), motif)

    routine = candidate.spec.routines[-1]
    trigger = candidate.spec.triggers[-1]
    assert trigger.kind == "cron"
    assert routine.kind == "workflow_run"
    assert "host-owned cron" in plan_for(motif).text
    assert "distinct from LoopRunner" in plan_for(motif).text
    assert not hard_diagnostics(candidate)


def test_release_readiness_motif_draws_parallel_specialists_and_a_human_gate() -> None:
    motif = detect_pipeline_motif(
        "Build a complex multi-agent release-readiness pipeline: fan out to security, "
        "performance, quality, and reliability specialists; synthesize evidence, triage risks, "
        "and require a human go/no-go gate."
    )
    assert motif == "release_readiness"
    candidate = build_candidate(default_blueprint("demo"), motif)

    workflow = candidate.spec.workflows[-1]
    routine = candidate.spec.routines[-1]
    assert [node.id for node in workflow.nodes] == [
        "assess_security",
        "assess_performance",
        "assess_quality",
        "assess_reliability",
        "synthesize_release_evidence",
        "triage_release_risks",
    ]
    assert all(node.depends_on == [] for node in workflow.nodes[:4])
    assert workflow.nodes[4].depends_on == [
        "assess_security",
        "assess_performance",
        "assess_quality",
        "assess_reliability",
    ]
    assert workflow.nodes[5].depends_on == ["synthesize_release_evidence"]
    assert workflow.max_concurrency == 4
    assert len(candidate.spec.subagents) == 4
    assert all("trigger.payload" in node.prompt for node in workflow.nodes[:4])
    assert routine.kind == "workflow_run"
    assert routine.done_when is not None
    assert routine.done_when.kind == "custom_todo"
    assert routine.done_when.id == "human_release_gate"
    assert "maxConcurrency=4" in plan_for(motif).text
    assert "does not fetch artifacts or deploy" in plan_for(motif).text
    assert not hard_diagnostics(candidate)


def test_generated_project_explains_the_ci_host_and_human_completion_seams() -> None:
    candidate = build_candidate(default_blueprint("demo"), "ci_review")
    document = candidate.model_dump(mode="json", by_alias=True)
    runtime = document["spec"]["runtime"]
    assert isinstance(runtime, dict)
    runtime["provider"] = {"kind": "openai_responses", "model": "gpt-5"}
    project = compile_blueprint(Blueprint.model_validate(document))
    guide = project.file("DEVELOPMENT.md").content

    assert "## Workflow and trigger handoff" in guide
    assert "The host supplies the raw trigger input as `payload`" in guide
    assert "Studio does not fetch CI diffs" in guide
    assert "human_review_gate" in guide
    assert "routine returns `done=False`" in guide
