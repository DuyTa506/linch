from __future__ import annotations

from linch_studio.templates import (
    TEMPLATE_IDS,
    build_template,
    build_template_data,
    template_summaries,
)


def test_template_registry_has_the_shared_product_templates_in_stable_order() -> None:
    assert TEMPLATE_IDS == (
        "agent",
        "goal_verified",
        "directed_workflow",
        "coordinator",
        "routine",
    )
    assert [item[0] for item in template_summaries()] == list(TEMPLATE_IDS)


def test_every_template_is_fresh_v1alpha2_and_preserves_the_project_identity() -> None:
    first = build_template_data("agent", "nightly-review")
    second = build_template_data("agent", "nightly-review")

    assert first == second
    assert first is not second
    assert first["apiVersion"] == "studio.linch.dev/v1alpha2"
    assert first["metadata"]["name"] == "nightly_review"
    assert first["spec"]["package"] == "nightly_review"
    first["metadata"]["title"] = "Changed"
    assert second["metadata"]["title"] == "Nightly Review"


def test_templates_encode_the_four_independent_product_axes() -> None:
    goal = build_template_data("goal_verified", "goal")
    workflow = build_template_data("directed_workflow", "flow")
    coordinator = build_template_data("coordinator", "team")
    routine = build_template_data("routine", "maintenance")

    completion = goal["spec"]["runtime"]["agent"]["completion"]
    assert completion["mode"] == "verifier_gated"
    assert completion["verifiers"][0]["kind"] == "custom_todo"
    assert workflow["spec"]["workflows"][0]["kind"] == "directed"
    assert coordinator["spec"]["runtime"]["agent"]["preset"] == "coordinator"
    assert "maxTurns" not in coordinator["spec"]["subagents"][0]
    assert routine["spec"]["triggers"][0]["cron"] == "0 */2 * * *"
    assert routine["spec"]["routines"][0]["kind"] == "agent_tick"
    assert routine["spec"]["routines"][0]["maxTurns"] == 6
    assert {tool["scope"] for tool in routine["spec"]["tools"]} == {"read"}


def test_every_template_matches_the_active_strict_blueprint_schema() -> None:
    for template_id in TEMPLATE_IDS:
        blueprint = build_template(template_id, f"sample_{template_id}")
        assert blueprint.api_version == "studio.linch.dev/v1alpha2"
