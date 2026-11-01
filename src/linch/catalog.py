"""Private durable prefix snapshots and catalog notifications."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .types import CatalogChangeBlock, Message, SystemBlock


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def snapshot_for(session: Any) -> dict[str, Any] | None:
    raw = getattr(session, "meta", {}).get("linch.prefix")
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    if not isinstance(raw.get("system"), list) or not isinstance(raw.get("tools"), list):
        return None
    if any(
        not isinstance(catalog, dict)
        or set(catalog) != {"tools", "skills", "subagents"}
        or not isinstance(catalog["tools"], dict)
        or not isinstance(catalog["skills"], dict)
        for catalog in (raw.get("base"), raw.get("notified"))
    ):
        return None
    return raw


def snapshot_skill_listing(snapshot: dict[str, Any]) -> str | None:
    start, end = "<linch-skill-listing>\n", "\n</linch-skill-listing>"
    for item in snapshot.get("system", ()):
        text = str(item.get("text", "")) if isinstance(item, dict) else ""
        if text.startswith(start) and text.endswith(end):
            return text[len(start) : -len(end)]
    return None


def snapshot_enabled(session: Any, config: Any) -> bool:
    if (
        getattr(config, "_values", None) is not None
        or getattr(session, "tools_override", None) is not None
    ):
        return False
    system = getattr(config, "system_blocks_override", None)
    if system is None:
        system = getattr(session, "system_blocks_override", None)
    if system is not None:
        return False
    catalog = getattr(config, "context", {}).get("request_catalog")
    return catalog is None or not bool(getattr(catalog, "active", True))


def _schemas(registry: Any) -> list[dict[str, Any]]:
    return copy.deepcopy(list(registry.schemas()))


def _skills(agent: Any) -> dict[str, str]:
    return {
        str(name): _digest(
            {
                "name": str(name),
                "frontmatter": str(getattr(skill, "frontmatter", "")),
                "description": str(getattr(skill.frontmatter, "description", "")),
                "body": str(getattr(skill, "body", "")),
            }
        )
        for name, skill in sorted(getattr(agent, "skills", {}).items())
    }


def _subagent_digest(registry: Any) -> str | None:
    tool = registry.get("Subagent")
    return _digest(str(tool.description)) if tool is not None else None


def _base_system(session: Any, config: Any, registry: Any) -> list[SystemBlock]:
    names = [tool.name for tool in registry.list()]
    blocks = list(
        config._build_system_blocks(
            names, tool_sections=registry.system_prompt_sections(active_names=names)
        )
    )
    listing = session.agent.skill_listing_text
    if listing:
        index = next(
            (
                i
                for i, block in enumerate(blocks)
                if str(getattr(block, "text", "")).startswith("Runtime:\n")
            ),
            len(blocks) - 1,
        )
        blocks.insert(index + 1, SystemBlock(text=str(listing), cacheable=True))
    return blocks


def _snapshot(session: Any, config: Any) -> dict[str, Any]:
    system = _base_system(session, config, config.tools)
    schemas, listing = _schemas(config.tools), session.agent.skill_listing_text
    rows = []
    for block in system:
        text = str(block.text)
        if listing and text == listing:
            text = f"<linch-skill-listing>\n{text}\n</linch-skill-listing>"
        rows.append({"text": text, "cacheable": bool(getattr(block, "cacheable", False))})
    base = {
        "tools": {str(item.get("name", "")): _digest(item) for item in schemas},
        "skills": _skills(session.agent),
        "subagents": _subagent_digest(config.tools),
    }
    return {
        "version": 1,
        "system": rows,
        "tools": schemas,
        "base": base,
        "notified": copy.deepcopy(base),
    }


def _notification_text(
    added: list[str],
    removed: list[str],
    changed: list[str],
    skills_added: list[str],
    skills_removed: list[str],
    skills_changed: list[str],
) -> str:
    lines = ["The available catalog changed during this session."]
    for label, values in (
        ("Added tools", added),
        ("Removed tools", removed),
        ("Updated tools", changed),
        ("Added skills", skills_added),
        ("Removed skills", skills_removed),
        ("Updated skills", skills_changed),
    ):
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    return "\n".join(lines)


async def reset_catalog_notifications(session: Any, config: Any = None) -> bool:
    if config is not None and not snapshot_enabled(session, config):
        return False
    current = snapshot_for(session)
    if current is None or current["base"] == current["notified"]:
        return False
    updated = copy.deepcopy(current)
    updated["notified"] = copy.deepcopy(current["base"])
    await session.update_meta({"linch.prefix": updated})
    session._catalog_generation = session._catalog_revision_seen = None
    return True


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
        return False
    current = snapshot_for(session)
    if current is None:
        await session.update_meta({"linch.prefix": _snapshot(session, config)})
        session._catalog_generation, session._catalog_revision_seen = generation, revision
        return True
    notice_present = any(
        getattr(block, "type", None) == "catalog_change"
        for message in session.provider_view
        for block in message.content
    )
    if current["base"] != current["notified"] and not notice_present:
        await reset_catalog_notifications(session, config)
        current = snapshot_for(session) or current
    current = copy.deepcopy(current)
    notified = current["notified"]
    live_schemas = _schemas(registry)
    live_by_name = {str(item.get("name", "")): item for item in live_schemas}
    notified_tools = dict(notified["tools"])
    added = [name for name in live_by_name if name not in notified_tools]
    removed = [name for name in notified_tools if name not in live_by_name]
    changed: list[str] = []
    for name, live in live_by_name.items():
        if name in notified_tools and _digest(live) != notified_tools[name]:
            changed.append(
                name
                if getattr(registry.get(name), "is_mcp_tool", False)
                else f"{name}: {live.get('description', '')}"
            )
    current["tools"] = snapshot_tools(current, live_schemas)
    live_skills = _skills(session.agent)
    notified_skills = dict(notified["skills"])
    skill_added = sorted(set(live_skills) - set(notified_skills))
    skill_removed = sorted(set(notified_skills) - set(live_skills))
    skill_changed = sorted(
        name
        for name in set(live_skills) & set(notified_skills)
        if live_skills[name] != notified_skills[name]
    )
    live_subagent = _subagent_digest(registry)
    subagent_changed = live_subagent != notified["subagents"]
    if not any(
        (added, removed, changed, skill_added, skill_removed, skill_changed, subagent_changed)
    ):
        session._catalog_generation, session._catalog_revision_seen = generation, revision
        return False
    current["notified"] = {
        "tools": {name: _digest(item) for name, item in live_by_name.items()},
        "skills": live_skills,
        "subagents": live_subagent,
    }
    from .skills.system_reminder import wrap_in_system_reminder

    block = CatalogChangeBlock(
        text=wrap_in_system_reminder(
            _notification_text(
                added,
                removed,
                changed,
                [
                    f"{name}: {session.agent.skills[name].frontmatter.description}"
                    for name in skill_added
                ],
                skill_removed,
                [
                    f"{name}: {session.agent.skills[name].frontmatter.description}"
                    for name in skill_changed
                ],
            )
        ),
        added_tools=tuple(added),
        removed_tools=tuple(removed),
    )
    await session.append([Message(role="user", content=[block])])
    await session.update_meta({"linch.prefix": current})
    session._catalog_generation, session._catalog_revision_seen = generation, revision
    return True


def snapshot_system(snapshot: dict[str, Any]) -> list[SystemBlock]:
    return [
        SystemBlock(text=str(item.get("text", "")), cacheable=bool(item.get("cacheable", False)))
        for item in snapshot.get("system", ())
        if isinstance(item, dict)
    ]


def snapshot_tools(
    snapshot: dict[str, Any], live_schemas: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    preview = copy.deepcopy([item for item in snapshot.get("tools", ()) if isinstance(item, dict)])
    if live_schemas is None:
        return preview
    positions = {str(item.get("name", "")): i for i, item in enumerate(preview)}
    for live in live_schemas:
        name = str(live.get("name", ""))
        row = positions.get(name)
        if row is None:
            positions[name] = len(preview)
            preview.append(copy.deepcopy(live))
        elif preview[row].get("input_schema") != live.get("input_schema"):
            preview[row] = copy.deepcopy(live)
    return preview
