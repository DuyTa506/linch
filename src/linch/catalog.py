"""Private session prefix snapshots and catalog notifications."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from typing import Any

from .types import CatalogChangeBlock, Message, SystemBlock


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_for(session: Any) -> dict[str, Any] | None:
    raw = getattr(session, "meta", {}).get("linch.prefix")
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or not isinstance(raw.get("system"), list)
        or not isinstance(raw.get("tools"), list)
    ):
        return None
    return raw


def snapshot_skill_listing(snapshot: dict[str, Any]) -> str | None:
    for item in snapshot.get("system", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        if text.startswith("<linch-skill-listing>\n") and text.endswith("\n</linch-skill-listing>"):
            return text[len("<linch-skill-listing>\n") : -len("\n</linch-skill-listing>")]
    return None


def snapshot_enabled(session: Any, config: Any) -> bool:
    if getattr(config, "_values", None) is not None:
        return False
    if getattr(session, "tools_override", None) is not None:
        return False
    effective_system = getattr(config, "system_blocks_override", None)
    if effective_system is None:
        effective_system = getattr(session, "system_blocks_override", None)
    if effective_system is not None:
        return False
    context = getattr(config, "context", None)
    request_catalog = context.get("request_catalog") if context is not None else None
    return request_catalog is None or not bool(getattr(request_catalog, "active", True))


def _schemas(registry: Any) -> list[dict[str, Any]]:
    return copy.deepcopy(list(registry.schemas()))


def _skills(agent: Any) -> dict[str, str]:
    return {
        str(name): _digest(
            {"name": str(name), "frontmatter": asdict(skill.frontmatter), "body": skill.body}
        )
        for name, skill in sorted(agent.skills.items())
    }


def _subagent_digest(registry: Any) -> str | None:
    tool = registry.get("Subagent")
    return _digest(str(tool.description)) if tool is not None else None


def _base_system(session: Any, config: Any, registry: Any) -> list[SystemBlock]:
    names = [tool.name for tool in registry.list()]
    sections = registry.system_prompt_sections(active_names=names)
    blocks = list(config._build_system_blocks(names, tool_sections=sections))
    listing = session.agent.skill_listing_text
    if listing:
        skill_block = SystemBlock(text=str(listing), cacheable=True)
        env_index = next(
            (
                index
                for index, block in enumerate(blocks)
                if "Runtime:" in getattr(block, "text", "")
            ),
            len(blocks) - 1,
        )
        blocks.insert(env_index + 1, skill_block)
    return blocks


def _snapshot(session: Any, config: Any) -> dict[str, Any]:
    registry = config.tools
    system = _base_system(session, config, registry)
    schemas = _schemas(registry)
    skills = _skills(session.agent)
    listing = session.agent.skill_listing_text
    system_rows = []
    for block in system:
        text = str(block.text)
        if listing and text == listing:
            text = "<linch-skill-listing>\n" + text + "\n</linch-skill-listing>"
        system_rows.append({"text": text, "cacheable": bool(getattr(block, "cacheable", False))})
    return {
        "version": 1,
        "system": system_rows,
        "tools": schemas,
        "skills": skills,
        "notified": {
            "tools": {str(item.get("name", "")): _digest(item) for item in schemas},
            "subagents": _subagent_digest(registry),
        },
    }


def _notification_text(
    *,
    added_tools: list[str],
    removed_tools: list[str],
    changed_tools: list[str],
    skills_added: list[str],
    skills_removed: list[str],
    skills_changed: list[str],
    subagent_changed: bool,
) -> str:
    lines = ["The available catalog changed during this session."]
    for label, values in (
        ("Added tools", added_tools),
        ("Removed tools", removed_tools),
        ("Updated tools", changed_tools),
        ("Added skills", skills_added),
        ("Removed skills", skills_removed),
        ("Updated skills", skills_changed),
    ):
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    if subagent_changed:
        lines.append("The Subagent description changed.")
    return "\n".join(lines)


async def sync_catalog(session: Any, config: Any, lease: Any = None) -> bool:
    registry = getattr(config, "tools", None)
    if registry is None:
        return False
    generation = getattr(registry, "generation", None)
    revision = getattr(getattr(session, "agent", None), "_catalog_revision", 0)
    if (
        getattr(session, "_catalog_generation", None) == generation
        and getattr(session, "_catalog_revision_seen", None) == revision
    ):
        return False
    if not snapshot_enabled(session, config):
        session._catalog_generation = generation
        session._catalog_revision_seen = revision
        return False
    current = snapshot_for(session)
    if current is None:
        fresh = _snapshot(session, config)
        await session.update_meta({"linch.prefix": fresh})
        session._catalog_generation = generation
        session._catalog_revision_seen = revision
        return True

    current = copy.deepcopy(current)
    live_schemas = _schemas(registry)
    live_by_name = {str(item.get("name", "")): item for item in live_schemas}
    notified = current.get("notified") or {}
    notified_tools = dict(notified.get("tools") or {})
    base_by_name = {str(item.get("name", "")): item for item in current["tools"]}
    pending_base_change = {
        name: _digest(item) for name, item in base_by_name.items()
    } != notified_tools
    notice_present = any(
        getattr(block, "type", None) == "catalog_change"
        for message in session.provider_view
        for block in message.content
    )
    added = [name for name in live_by_name if name not in notified_tools]
    removed = [
        name
        for name in base_by_name
        if name not in live_by_name and (name in notified_tools or not notice_present)
    ]
    changed: list[str] = []
    desc_only: list[str] = []
    for name, live in live_by_name.items():
        if name not in base_by_name:
            continue
        old = base_by_name[name]
        schema_changed = old.get("input_schema") != live.get("input_schema")
        description_changed = old.get("description") != live.get("description")
        if name not in notified_tools:
            continue
        if _digest(live) == notified_tools[name] and not (
            pending_base_change and not notice_present and (schema_changed or description_changed)
        ):
            continue
        if schema_changed:
            changed.append(name)
            for index, item in enumerate(current["tools"]):
                if item.get("name") == name:
                    current["tools"][index] = copy.deepcopy(live)
                    break
        elif description_changed:
            desc_only.append(name)

    current_skills = dict(current.get("skills") or {})
    live_skills = _skills(session.agent)
    skill_added = sorted(set(live_skills) - set(current_skills))
    skill_removed = sorted(set(current_skills) - set(live_skills))
    skill_changed = sorted(
        name
        for name in set(live_skills) & set(current_skills)
        if live_skills[name] != current_skills[name]
    )
    live_subagent = _subagent_digest(registry)
    subagent_changed = live_subagent != notified.get("subagents")
    if not any(
        (
            added,
            removed,
            changed,
            desc_only,
            skill_added,
            skill_removed,
            skill_changed,
            subagent_changed,
        )
    ):
        session._catalog_generation = generation
        session._catalog_revision_seen = revision
        return False

    for name in added:
        live = copy.deepcopy(live_by_name[name])
        for index, item in enumerate(current["tools"]):
            if item.get("name") == name:
                current["tools"][index] = live
                break
        else:
            current["tools"].append(live)
    current["notified"] = {
        "tools": {name: _digest(item) for name, item in live_by_name.items()},
        "subagents": live_subagent,
    }
    text = _notification_text(
        added_tools=added,
        removed_tools=removed,
        changed_tools=sorted(set(changed + desc_only)),
        skills_added=[
            f"{name}: {session.agent.skills[name].frontmatter.description}" for name in skill_added
        ],
        skills_removed=skill_removed,
        skills_changed=[
            f"{name}: {session.agent.skills[name].frontmatter.description}"
            for name in skill_changed
        ],
        subagent_changed=subagent_changed,
    )
    from .skills.system_reminder import wrap_in_system_reminder

    block = CatalogChangeBlock(
        text=wrap_in_system_reminder(text),
        added_tools=tuple(added),
        removed_tools=tuple(removed),
    )
    await session.append([Message(role="user", content=[block])])
    await session.update_meta({"linch.prefix": current})
    session._catalog_generation = generation
    session._catalog_revision_seen = revision
    return True


async def rebuild_catalog_snapshot(session: Any, config: Any) -> bool:
    if not snapshot_enabled(session, config):
        return False
    current = snapshot_for(session)
    if current is None:
        return False
    registry = config.tools
    notified = current.get("notified") or {}
    base_digests = {str(item.get("name", "")): _digest(item) for item in current.get("tools", [])}
    skills_changed = dict(current.get("skills") or {}) != _skills(session.agent)
    subagent_changed = _subagent_digest(registry) != notified.get("subagents")
    if (
        base_digests == dict(notified.get("tools") or {})
        and not skills_changed
        and not subagent_changed
    ):
        return False

    fresh = _snapshot(session, config)
    session._catalog_generation = None
    session._catalog_revision_seen = None
    await session.update_meta({"linch.prefix": fresh})
    session._catalog_generation = getattr(registry, "generation", None)
    session._catalog_revision_seen = getattr(session.agent, "_catalog_revision", 0)
    return True


def snapshot_system(snapshot: dict[str, Any]) -> list[SystemBlock]:
    return [
        SystemBlock(text=str(item.get("text", "")), cacheable=bool(item.get("cacheable", False)))
        for item in snapshot.get("system", [])
        if isinstance(item, dict)
    ]


def snapshot_tools(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return copy.deepcopy([item for item in snapshot.get("tools", []) if isinstance(item, dict)])
