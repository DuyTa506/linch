"""Deterministic, explainable routing for the global Studio support drawer."""

from __future__ import annotations

import re

from .models import PipelineIntent, ResolvedSupportMode, SupportMode

# These are deliberately conservative. Documentation remains the safe default;
# users can always choose a mode explicitly in the UI or API. In particular,
# ``workflow`` is a documented noun, not evidence that the developer wants a
# new Studio Blueprint. A previous broad match turned questions such as "What
# is a workflow_run routine?" into a pipeline confirmation screen.
_PIPELINE_ACTION = re.compile(
    r"\b(?:build|create|make|generate|design|compose|orchestrate|"
    r"tao|tạo|xây|xay|thiết kế)\b",
    re.IGNORECASE,
)
_PIPELINE_OBJECT = re.compile(
    r"\b(?:pipeline|blueprint|workflow|agent system|multi[- ]?agent team|"
    r"fan[- ]?out|fan[- ]?in|luong (?:cong viec|công việc))\b",
    re.IGNORECASE,
)
_CI_REVIEW_REQUEST = re.compile(
    r"\b(?:ci|pull request|pr|github)\b.*\b(?:code review|reviewers?|security|"
    r"performance|style)\b|\b(?:code review|reviewers?)\b.*\b(?:ci|pull request|pr|github)\b",
    re.IGNORECASE,
)
_IMPLEMENTATION = re.compile(
    r"\b(?:implement|implementation|code|sample|example|how (?:do|to) (?:i|we) build|"
    r"schedule|scheduled|scheduler|cron|loop|multi[- ]?agent|recipe|skeleton|"
    r"triển khai|lập trình|mẫu code|viết code|lịch|vòng lặp)\b",
    re.IGNORECASE,
)
_SCHEDULE = re.compile(
    r"\b(?:schedule|scheduled|scheduler|cron|loop|lịch|vòng lặp)\b",
    re.IGNORECASE,
)
_MULTI_AGENT = re.compile(r"\b(?:multi[- ]?agent|subagents?|nhiều agent)\b", re.IGNORECASE)


def resolve_mode(mode: SupportMode, latest_user_message: str) -> ResolvedSupportMode:
    """Resolve an explicit mode or classify the latest user wording locally.

    This classifier is a routing aid, never a claim of documentation support.
    The answer/recipe service still reports evidence coverage after retrieval.
    """

    if mode != "auto":
        return mode
    if _PIPELINE_ACTION.search(latest_user_message) and _PIPELINE_OBJECT.search(
        latest_user_message
    ):
        return "pipeline"
    if _CI_REVIEW_REQUEST.search(latest_user_message) and _PIPELINE_ACTION.search(
        latest_user_message
    ):
        return "pipeline"
    if _IMPLEMENTATION.search(latest_user_message):
        return "implementation"
    return "documentation"


def pipeline_intent(latest_user_message: str) -> PipelineIntent:
    """Make a reviewable bridge description without inventing a Blueprint."""

    components: list[str] = []
    lower = latest_user_message.casefold()
    if _SCHEDULE.search(latest_user_message):
        components.extend(("host-owned schedule or trigger", "bounded routine"))
    if _MULTI_AGENT.search(latest_user_message):
        components.extend(("specialist subagents", "directed workflow"))
    if "ci" in lower or "pull request" in lower or "code review" in lower:
        components.extend(("CI trigger wrapper", "human completion gate"))
    if not components:
        components.append("reviewable Studio Blueprint")
    return PipelineIntent(
        summary=latest_user_message.strip(),
        proposed_components=list(dict.fromkeys(components)),
    )


__all__ = ["pipeline_intent", "resolve_mode"]
