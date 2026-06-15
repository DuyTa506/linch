from __future__ import annotations

from linch.skills.substitute import substitute_skill_body
from linch.skills.types import Skill, SkillFrontmatter


def _skill(body: str, arguments: list[str] | None = None) -> Skill:
    return Skill(
        name="demo",
        dir="/skills/demo",
        frontmatter=SkillFrontmatter(name="demo", description="d", arguments=arguments),
        body=body,
    )


def test_substitute_replaces_linch_builtin_variables() -> None:
    skill = _skill("dir=${LINCH_SKILL_DIR} sid=${LINCH_SESSION_ID}")
    out = substitute_skill_body(skill, "", "sess-1")
    assert "dir=/skills/demo" in out
    assert "sid=sess-1" in out


def test_substitute_leaves_unknown_variables_untouched() -> None:
    # Only the recognized built-ins are substituted; anything else is verbatim.
    skill = _skill("keep=${UNKNOWN_VAR}")
    out = substitute_skill_body(skill, "", "sess-1")
    assert "keep=${UNKNOWN_VAR}" in out


def test_substitute_positional_args_and_arguments_token() -> None:
    skill = _skill("hi $name | all=$ARGUMENTS", arguments=["name"])
    out = substitute_skill_body(skill, "alice extra", "s")
    assert "hi alice" in out
    assert "all=alice extra" in out
