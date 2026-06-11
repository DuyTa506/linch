from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[reportMissingModuleSource]

from ..errors import ConfigError, ProviderError
from ..types import Message, OutputSchema, ProviderRequest, SystemBlock, TextBlock
from .loader import AGENT_NAME_RE, load_agents_from_dir, normalize_tools
from .registry import AgentRegistry
from .types import AgentDefinition

if TYPE_CHECKING:
    from ..agent import Agent


@dataclass(slots=True)
class GeneratedSubagentDefinition:
    name: str
    description: str
    body: str
    tools: list[str] | None = None


@dataclass(slots=True)
class CreatedSubagentDefinition:
    definition: GeneratedSubagentDefinition
    file_path: str
    agent_definition: AgentDefinition


_GENERATOR_SCHEMA = OutputSchema(
    name="generated_subagent",
    description="Generated Linch subagent definition.",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "description", "body", "tools"],
        "properties": {
            "name": {
                "type": "string",
                "description": "Lowercase agent id such as code-reviewer or test_runner.",
            },
            "description": {
                "type": "string",
                "description": "Concise catalog description for when to use this subagent.",
            },
            "body": {
                "type": "string",
                "description": "Complete system prompt for the subagent.",
            },
            "tools": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Optional Linch tool allowlist. Null means full parent tool access.",
            },
        },
    },
)


_GENERATOR_SYSTEM_PROMPT = """You are an expert Linch subagent architect.

Create one high-quality Linch subagent definition from the user's request.
Linch subagents are Markdown files under `.linch/agents/<name>.md` with YAML
frontmatter and a Markdown body. The frontmatter description is shown in the
Subagent tool catalog. The body is the complete system prompt used for that
subagent.

Design rules:
- Choose a concise lowercase identifier using letters, numbers, hyphens, or
  underscores. Prefer 2-4 words. Avoid generic names like helper or assistant.
- The description must start with an action-oriented phrase that clearly says
  when to use the subagent.
- The body must be a complete autonomous operating manual for the subagent,
  written in second person.
- Include concrete workflow guidance, output expectations, and self-checks
  that are specific to the requested role.
- Mention that subagents start with no parent conversation history and must
  rely only on the prompt they receive and their available tools.
- If the user asks for memory/persistence, include explicit memory behavior in
  the body. Otherwise do not invent persistent memory.
- Return only the structured object requested by the schema.
"""


def render_subagent_markdown(definition: GeneratedSubagentDefinition) -> str:
    """Render a generated subagent as Linch's disk-backed Markdown format."""

    normalized = _normalize_generated_definition(definition)
    frontmatter: dict[str, object] = {
        "name": normalized.name,
        "description": normalized.description,
    }
    if normalized.tools is not None:
        frontmatter["tools"] = normalized.tools

    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=False).strip()
    body = normalized.body.strip()
    return f"---\n{yaml_text}\n---\n{body}\n"


async def generate_subagent_definition(
    agent: Agent,
    request: str,
    *,
    tools: list[str] | None = None,
    existing_names: list[str] | None = None,
    signal: Any = None,
) -> GeneratedSubagentDefinition:
    """Generate a Linch subagent definition from a natural-language request."""

    if not isinstance(request, str) or request.strip() == "":
        raise ConfigError("request must be a non-empty string")

    available_tools = sorted(tool.name for tool in agent.tools.list())
    forced_tools = _normalize_tool_allowlist(tools, available_tools) if tools is not None else None
    names = (
        sorted({name.lower() for name in existing_names})
        if existing_names is not None
        else sorted(await _existing_subagent_names(agent))
    )

    user_text = _build_generation_request(
        request=request,
        available_tools=available_tools,
        forced_tools=forced_tools,
        existing_names=names,
    )
    parsed = await _query_definition(agent, user_text, signal=signal)

    generated = GeneratedSubagentDefinition(
        name=str(parsed.get("name", "")),
        description=str(parsed.get("description", "")),
        body=str(parsed.get("body", "")),
        tools=forced_tools if forced_tools is not None else normalize_tools(parsed.get("tools")),
    )
    generated = _normalize_generated_definition(generated, available_tools=available_tools)

    if generated.name.lower() in names:
        raise ConfigError(f"subagent {generated.name!r} already exists")
    return generated


async def write_subagent_definition(
    definition: GeneratedSubagentDefinition,
    config_dir: str | Path,
    *,
    overwrite: bool = False,
) -> AgentDefinition:
    """Write a generated subagent definition under ``config_dir/agents``."""

    normalized = _normalize_generated_definition(definition)
    agents_dir = Path(config_dir) / "agents"
    path = agents_dir / f"{normalized.name}.md"

    if path.exists() and not overwrite:
        raise ConfigError(f"subagent file already exists: {path}")

    agents_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(render_subagent_markdown(normalized), encoding="utf-8")

    loaded = await load_agents_from_dir(str(config_dir))
    for agent_def in loaded.agents:
        if agent_def.name.lower() == normalized.name.lower():
            return agent_def
    detail = "; ".join(f"{s.file_path}: {s.detail}" for s in loaded.skipped)
    raise ConfigError(
        f"generated subagent {normalized.name!r} could not be loaded"
        + (f": {detail}" if detail else "")
    )


async def create_subagent_definition(
    agent: Agent,
    request: str,
    *,
    tools: list[str] | None = None,
    overwrite: bool = False,
    reload: bool = True,
    signal: Any = None,
) -> CreatedSubagentDefinition:
    """Generate, write, and optionally hot-reload a disk-backed subagent."""

    config_dir = Path(str(getattr(agent, "_config_dir", Path(agent.cwd) / ".linch")))
    existing_names = sorted(await _existing_subagent_names(agent))
    definition = await generate_subagent_definition(
        agent,
        request,
        tools=tools,
        existing_names=[] if overwrite else existing_names,
        signal=signal,
    )
    agent_definition = await write_subagent_definition(
        definition,
        config_dir,
        overwrite=overwrite,
    )
    if reload:
        await agent.reload_subagents()
        if agent.subagent_registry is not None:
            refreshed = agent.subagent_registry.get(definition.name)
            if refreshed is not None:
                agent_definition = refreshed
    return CreatedSubagentDefinition(
        definition=definition,
        file_path=agent_definition.file_path,
        agent_definition=agent_definition,
    )


def _build_generation_request(
    *,
    request: str,
    available_tools: list[str],
    forced_tools: list[str] | None,
    existing_names: list[str],
) -> str:
    tool_text = ", ".join(available_tools) if available_tools else "(no tools)"
    existing_text = ", ".join(existing_names) if existing_names else "(none)"
    if forced_tools is None:
        tool_instruction = (
            "Choose `tools` as null for full parent tool access, [] for no tools, "
            "or a subset of the available tool names."
        )
    else:
        tool_instruction = (
            "Set `tools` exactly to this fixed allowlist: "
            f"{', '.join(forced_tools) if forced_tools else '[]'}."
        )
    return "\n".join(
        [
            f"Create a Linch subagent for this request:\n{request.strip()}",
            "",
            f"Existing subagent names that must not be reused: {existing_text}",
            f"Available Linch tools: {tool_text}",
            tool_instruction,
        ]
    )


async def _query_definition(agent: Agent, user_text: str, *, signal: Any = None) -> dict[str, Any]:
    req = ProviderRequest(
        model=agent.model,
        system=[SystemBlock(text=_GENERATOR_SYSTEM_PROMPT, cacheable=True)],
        tools=[],
        messages=[Message(role="user", content=[TextBlock(text=user_text)])],
        signal=signal,
        max_output_tokens=agent.max_output_tokens,
        max_retries=agent.max_retries,
        cache_ttl=agent.cache_ttl,  # type: ignore[arg-type]
        output_schema=_GENERATOR_SCHEMA,
    )

    text_parts: list[str] = []
    tool_inputs: dict[str, list[str]] = {}
    tool_names: dict[str, str] = {}
    async for event in agent.provider.stream(req):
        typ = event.get("type")
        if typ == "text_delta":
            text_parts.append(str(event.get("text", "")))
        elif typ == "tool_use_start":
            tool_id = str(event["id"])
            tool_names[tool_id] = str(event.get("name", ""))
            tool_inputs[tool_id] = []
        elif typ == "tool_use_input_delta":
            tool_id = str(event["id"])
            tool_inputs.setdefault(tool_id, []).append(str(event.get("json_delta", "")))
        elif typ == "tool_use_end":
            tool_id = str(event["id"])
            if tool_names.get(tool_id) == _GENERATOR_SCHEMA.name:
                raw = "".join(tool_inputs.get(tool_id, []))
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"generated subagent tool JSON was invalid: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise ProviderError("generated subagent tool output must be a JSON object")
                return parsed

    raw_text = "".join(text_parts).strip()
    if raw_text == "":
        raise ProviderError("provider returned no generated subagent definition")
    return _parse_json_object(raw_text)


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{[\s\S]*\}", text)
        if match is None:
            raise ProviderError("provider response did not contain a JSON object") from exc
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider response JSON was invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("generated subagent definition must be a JSON object")
    return parsed


async def _existing_subagent_names(agent: Agent) -> set[str]:
    registry = getattr(agent, "subagent_registry", None)
    if registry is None:
        config_dir = str(getattr(agent, "_config_dir", Path(agent.cwd) / ".linch"))
        loaded = await load_agents_from_dir(config_dir)
        registry = AgentRegistry(
            loaded.agents,
            extra_built_ins=list(getattr(agent, "extra_subagents", []) or []),
        )
    return {definition.name.lower() for definition in registry.list_all()}


def _normalize_generated_definition(
    definition: GeneratedSubagentDefinition,
    *,
    available_tools: list[str] | None = None,
) -> GeneratedSubagentDefinition:
    name = _slugify_agent_name(definition.name)
    if not AGENT_NAME_RE.match(name):
        raise ConfigError(f"subagent name {definition.name!r} must match /^[a-z0-9][a-z0-9_-]*$/i")

    description = definition.description.strip()
    if description == "":
        raise ConfigError("subagent description must be a non-empty string")

    body = definition.body.strip()
    if body == "":
        raise ConfigError("subagent body must be a non-empty string")

    tools = (
        _normalize_tool_allowlist(definition.tools, available_tools)
        if definition.tools is not None
        else None
    )
    return GeneratedSubagentDefinition(
        name=name,
        description=description,
        body=body,
        tools=tools,
    )


def _slugify_agent_name(name: str) -> str:
    lowered = name.strip().lower()
    lowered = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-_")
    return lowered


def _normalize_tool_allowlist(
    tools: list[str] | None,
    available_tools: list[str] | None = None,
) -> list[str]:
    normalized = normalize_tools(tools) if tools is not None else []
    if normalized is None:
        normalized = []
    seen: set[str] = set()
    deduped: list[str] = []
    for tool in normalized:
        if tool in seen:
            continue
        seen.add(tool)
        deduped.append(tool)

    if available_tools is not None:
        available = set(available_tools)
        unknown = [tool for tool in deduped if tool not in available]
        if unknown:
            raise ConfigError(f"unknown subagent tool(s): {', '.join(unknown)}")
    return deduped
