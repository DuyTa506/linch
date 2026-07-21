"""Auto-routing protects documentation questions from unintended pipeline entry."""

import pytest

from linch_studio.support import resolve_mode


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("What is a workflow_run routine?", "documentation"),
        ("How does a directed workflow run parallel agent steps?", "documentation"),
        ("Show me how to implement a scheduled multi-agent review.", "implementation"),
        (
            "Build a CI-triggered code review pipeline with security and style reviewers.",
            "pipeline",
        ),
        ("Create a scheduled cron multi-agent team workflow.", "pipeline"),
    ],
)
def test_auto_mode_distinguishes_questions_recipes_and_new_pipeline_requests(
    message: str, expected: str
) -> None:
    assert resolve_mode("auto", message) == expected
