from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[reportMissingModuleSource]

from .types import Skill, SkillFrontmatter, SkippedSkill

FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?")
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)
ARG_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$", re.IGNORECASE)
TOOL_NAME_RE = re.compile(r"^\S+$")


class _FrontmatterError(Exception):
    pass


def load_skills_from_dir(
    config_dir: str,
    builtin_tool_names: set[str],
) -> tuple[list[Skill], list[SkippedSkill]]:
    skills_root = Path(config_dir) / "skills"
    if not skills_root.is_dir():
        return [], []

    entries: list[Path] = []
    try:
        entries = sorted(skills_root.iterdir())
    except OSError:
        return [], []

    skills: list[Skill] = []
    skipped: list[SkippedSkill] = []
    seen_names: set[str] = set()

    for ent in entries:
        entry_dir = str(ent)
        if ent.is_symlink():
            skipped.append(
                SkippedSkill(
                    dir=entry_dir,
                    reason="io-error",
                    detail="symlinks are not followed",
                )
            )
            continue
        if not ent.is_dir():
            continue

        skill_md_path = ent / "SKILL.md"
        try:
            raw = skill_md_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            skipped.append(
                SkippedSkill(
                    dir=entry_dir,
                    reason="missing-skill-md",
                    detail="SKILL.md not found",
                )
            )
            continue
        except OSError as exc:
            skipped.append(SkippedSkill(dir=entry_dir, reason="io-error", detail=str(exc)))
            continue

        result = _parse_frontmatter(raw, ent.name)
        if not result["ok"]:
            skipped.append(
                SkippedSkill(
                    dir=entry_dir,
                    reason="invalid-frontmatter",
                    detail=result["error"],
                )
            )
            continue

        frontmatter = result["frontmatter"]
        body = result["body"]

        if frontmatter.name in builtin_tool_names:
            skipped.append(
                SkippedSkill(
                    dir=entry_dir,
                    reason="name-collision-tool",
                    detail=f"skill name '{frontmatter.name}' collides with builtin tool",
                )
            )
            continue
        if frontmatter.name in seen_names:
            skipped.append(
                SkippedSkill(
                    dir=entry_dir,
                    reason="name-collision-skill",
                    detail=f"skill name '{frontmatter.name}' already loaded",
                )
            )
            continue
        seen_names.add(frontmatter.name)

        skills.append(
            Skill(
                name=frontmatter.name,
                dir=entry_dir,
                frontmatter=frontmatter,
                body=body,
            )
        )

    skills.sort(key=lambda s: s.name)
    return skills, skipped


def _parse_frontmatter(raw: str, dir_name: str) -> dict:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {"ok": False, "error": "missing frontmatter (file must start with `---`)"}
    yaml_text = match.group(1)
    body = raw[match.end() :]

    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return {"ok": False, "error": f"YAML parse error: {exc}"}

    if doc is None:
        return {"ok": False, "error": "frontmatter is empty"}
    if not isinstance(doc, dict):
        return {"ok": False, "error": "frontmatter must be a YAML mapping"}

    def fail(msg: str) -> None:
        raise _FrontmatterError(msg)

    def optional_str(key: str) -> str | None:
        v = doc.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or v == "":
            fail(f"frontmatter '{key}' must be a non-empty string")
        return v

    def optional_str_array(key: str, item_re: re.Pattern, item_desc: str) -> list[str] | None:
        v = doc.get(key)
        if v is None:
            return None
        if not isinstance(v, list):
            fail(f"frontmatter '{key}' must be an array of strings")
        for item in v:
            if not isinstance(item, str):
                fail(f"frontmatter '{key}' must contain only strings")
            if not item_re.match(item):
                fail(f"frontmatter '{key}' entry '{item}' {item_desc}")
        return v

    try:
        description = doc.get("description")
        if not isinstance(description, str) or description.strip() == "":
            fail("frontmatter 'description' is required and must be a non-empty string")
        assert isinstance(description, str)
        description = description.strip()

        name = dir_name
        raw_name = doc.get("name")
        if raw_name is not None:
            if not isinstance(raw_name, str) or raw_name.strip() == "":
                fail("frontmatter 'name' must be a non-empty string")
            name = raw_name.strip()
        if not SKILL_NAME_RE.match(name):
            fail(f"skill name '{name}' must match /^[a-z0-9][a-z0-9_-]*$/i")

        args = optional_str_array("arguments", ARG_NAME_RE, "must match /^[a-z_][a-z0-9_]*$/i")
        if args:
            seen = set()
            for a in args:
                if a == "ARGUMENTS":
                    fail("frontmatter 'arguments' entry must not be 'ARGUMENTS' (reserved)")
                if a in seen:
                    fail(f"frontmatter 'arguments' contains duplicate '{a}'")
                seen.add(a)

        disable_model_invocation = False
        raw_dmi = doc.get("disable_model_invocation")
        if raw_dmi is not None:
            if not isinstance(raw_dmi, bool):
                fail("frontmatter 'disable_model_invocation' must be a boolean")
            disable_model_invocation = raw_dmi

        frontmatter = SkillFrontmatter(
            name=name,
            description=description,
            disable_model_invocation=disable_model_invocation,
        )

        when_to_use = optional_str("when_to_use")
        if when_to_use is not None:
            frontmatter.when_to_use = when_to_use

        allowed_tools = optional_str_array(
            "allowed_tools", TOOL_NAME_RE, "must look like tool names"
        )
        if allowed_tools is not None:
            frontmatter.allowed_tools = allowed_tools

        if args is not None:
            frontmatter.arguments = args

        argument_hint = optional_str("argument_hint")
        if argument_hint is not None:
            frontmatter.argument_hint = argument_hint

        model = optional_str("model")
        if model is not None:
            frontmatter.model = model

        version = optional_str("version")
        if version is not None:
            frontmatter.version = version

        return {"ok": True, "frontmatter": frontmatter, "body": body}
    except _FrontmatterError as exc:
        return {"ok": False, "error": str(exc)}
