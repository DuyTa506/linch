from __future__ import annotations

from .types import Skill

MAX_PER_ENTRY = 250
MIN_DESC_LEN = 20
FALLBACK_BUDGET = 8000
BUDGET_FRACTION = 0.01
CHARS_PER_TOKEN = 4


def build_skill_listing(
    skills: list[Skill],
    context_window_tokens: int | None = None,
) -> str:
    visible = sorted(
        [s for s in skills if not s.frontmatter.disable_model_invocation],
        key=lambda s: s.name.lower(),
    )
    if not visible:
        return ""

    budget = (
        int(context_window_tokens * CHARS_PER_TOKEN * BUDGET_FRACTION)
        if context_window_tokens is not None
        else FALLBACK_BUDGET
    )

    descriptions = [_cap_description(_build_entry_description(s), MAX_PER_ENTRY) for s in visible]

    def _render(name: str, desc: str) -> str:
        return f"- {name}: {desc}"

    full_text = "\n".join(_render(s.name, descriptions[i]) for i, s in enumerate(visible))
    if len(full_text) <= budget:
        return full_text

    overhead_per_entry = sum(len(f"- {s.name}: ") for s in visible)
    newline_overhead = max(len(visible) - 1, 0)
    max_desc_len = (budget - overhead_per_entry - newline_overhead) // len(visible)

    if max_desc_len < MIN_DESC_LEN:
        return "\n".join(f"- {s.name}" for s in visible)

    return "\n".join(
        _render(s.name, _cap_description(descriptions[i], max_desc_len))
        for i, s in enumerate(visible)
    )


def _build_entry_description(s: Skill) -> str:
    desc = s.frontmatter.description
    if s.frontmatter.when_to_use:
        desc += f" — {s.frontmatter.when_to_use}"
    if s.frontmatter.argument_hint:
        desc += f" (args: {s.frontmatter.argument_hint})"
    return desc


def _cap_description(desc: str, limit: int) -> str:
    if len(desc) <= limit:
        return desc
    if limit <= 1:
        return "\u2026"
    return f"{desc[: limit - 1]}\u2026"
