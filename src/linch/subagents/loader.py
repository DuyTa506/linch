from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .types import AgentDefinition, AgentFrontmatter, LoadAgentsResult, SkippedAgent

FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?")
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)


class _FrontmatterError(Exception):
    pass


def normalize_tools(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, list):
        arr = [t for t in v if isinstance(t, str)]
    elif isinstance(v, str):
        arr = [s.strip() for s in v.split(",") if s.strip()]
    else:
        return None
    result = [t.split("(")[0].strip() for t in arr]
    return [t for t in result if t]


async def load_agents_from_dir(config_dir: str) -> LoadAgentsResult:
    agents_root = Path(config_dir) / "agents"
    if not agents_root.is_dir():
        return LoadAgentsResult(agents=[], skipped=[])

    agents: list[AgentDefinition] = []
    skipped: list[SkippedAgent] = []
    seen_names: set[str] = set()

    try:
        entries = sorted(agents_root.iterdir())
    except OSError as exc:
        return LoadAgentsResult(
            agents=[],
            skipped=[
                SkippedAgent(
                    file_path=str(agents_root),
                    reason="io-error",
                    detail=str(exc),
                )
            ],
        )

    for entry in entries:
        file_path = str(entry)
        if entry.is_symlink():
            skipped.append(
                SkippedAgent(
                    file_path=file_path,
                    reason="io-error",
                    detail="symlinks are not followed",
                )
            )
            continue
        if not entry.is_file():
            continue
        if not entry.name.lower().endswith(".md"):
            continue

        try:
            raw = entry.read_text("utf-8")
        except OSError as exc:
            skipped.append(
                SkippedAgent(
                    file_path=file_path,
                    reason="io-error",
                    detail=str(exc),
                )
            )
            continue

        file_base = entry.name[:-3] if entry.name.endswith(".md") else entry.name[: -len(".md")]
        parsed = _parse_frontmatter(raw, file_base)
        if not parsed["ok"]:
            skipped.append(
                SkippedAgent(
                    file_path=file_path,
                    reason=parsed["reason"],
                    detail=parsed["error"],
                )
            )
            continue

        fm, body = parsed["value"]
        key = fm.name.lower()
        if key in seen_names:
            skipped.append(
                SkippedAgent(
                    file_path=file_path,
                    reason="name-collision",
                    detail=f"agent name '{fm.name}' already loaded",
                )
            )
            continue
        seen_names.add(key)

        agents.append(
            AgentDefinition(
                name=fm.name,
                file_path=file_path,
                source="disk",
                frontmatter=fm,
                body=body,
            )
        )

    agents.sort(key=lambda a: a.name)
    return LoadAgentsResult(agents=agents, skipped=skipped)


def _parse_frontmatter(raw: str, file_base: str) -> dict[str, Any]:
    def _fail(reason: str, error: str) -> dict[str, Any]:
        return {"ok": False, "reason": reason, "error": error}

    def _ok(frontmatter: AgentFrontmatter, body: str) -> dict[str, Any]:
        return {"ok": True, "value": (frontmatter, body)}

    match = FRONTMATTER_RE.match(raw)
    if not match:
        return _fail("missing-frontmatter", "missing frontmatter (file must start with `---`)")
    yaml_text = match.group(1) or ""
    body = raw[match.end() :]

    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return _fail("invalid-frontmatter", f"YAML parse error: {exc}")

    if doc is None:
        return _fail("invalid-frontmatter", "frontmatter is empty")
    if not isinstance(doc, dict):
        return _fail(
            "invalid-frontmatter",
            "frontmatter must be a YAML mapping",
        )

    try:
        description = doc.get("description")
        if not isinstance(description, str) or description.strip() == "":
            return _fail(
                "invalid-frontmatter",
                "frontmatter 'description' is required and must be a non-empty string",
            )

        name = file_base
        if "name" in doc:
            raw_name = doc["name"]
            if not isinstance(raw_name, str) or raw_name.strip() == "":
                return _fail(
                    "invalid-frontmatter",
                    "frontmatter 'name' must be a non-empty string",
                )
            name = raw_name

        if not AGENT_NAME_RE.match(name):
            return _fail(
                "invalid-frontmatter",
                f"agent name '{name}' must match /^[a-z0-9][a-z0-9_-]*$/i",
            )

        fm = AgentFrontmatter(name=name, description=description)

        if "tools" in doc:
            normalized = normalize_tools(doc["tools"])
            if normalized is not None:
                fm.tools = normalized

        return _ok(fm, body)
    except Exception as exc:
        return _fail("invalid-frontmatter", str(exc))
