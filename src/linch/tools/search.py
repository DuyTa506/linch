"""Session-scoped tool and skill discovery through the request-catalog seam."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable
from typing import Any

from ..errors import ConfigError
from ..events import ToolCallEndEvent
from ..kernel import Context, Disposable
from ..lifecycle import TOOL_RESOLVE
from ..loop.turn_stopping import TurnStopping, TurnStoppingDecision
from ..types import Message, TextBlock
from .base import ToolContext, ToolResult
from .registry import ToolRegistry

_KEY = "linch.tool_search"


class ToolSearchPlugin:
    """Defer large tool catalogs and skill listings until the model searches.

    Args:
        max_tools: Largest offered tool count that keeps all schemas inline.
        max_skills: Largest model-invokable skill count that keeps the full listing.

    Raises:
        ValueError: If a threshold is not a non-negative integer.
    """

    name = "tool_search"
    version = "1"
    provides: Iterable[str] = ("request_catalog",)
    requires: Iterable[str] = ("agent",)

    def __init__(self, max_tools: int = 30, max_skills: int = 50) -> None:
        """Create a discovery plugin with catalog-size thresholds.

        Args:
            max_tools: Largest offered tool count that keeps all schemas inline.
            max_skills: Largest model-invokable skill count that keeps the full listing.

        Raises:
            ValueError: If a threshold is not a non-negative integer.
        """
        for value in (max_tools, max_skills):
            if type(value) is not int or value < 0:
                raise ValueError("tool search thresholds must be non-negative integers")
        self.max_tools = max_tools
        self.max_skills = max_skills

    def setup(self, ctx: Context, config: Any = None) -> Disposable:
        """Install discovery, request filtering, and the deferred-call guard.

        Args:
            ctx: Activation scope containing the owning agent.
            config: Reserved; thresholds are supplied to the constructor.

        Returns:
            A disposer that restores the preceding tool and request behavior.
        """
        agent = ctx.agent
        catalog = _SearchCatalog(ctx, agent, self.max_tools, self.max_skills)
        ctx.register("request_catalog", catalog)
        catalog.prepare()
        ctx.effect(lambda: agent._turn_stopping_dispatcher.register(catalog._guard_terminal))
        ctx.on(TOOL_RESOLVE, catalog._guard_call)
        return Disposable()


class _SearchCatalog:
    def __init__(self, scope: Context, agent: Any, max_tools: int, max_skills: int) -> None:
        self.scope = scope
        self.agent = agent
        self.max_tools = max_tools
        self.max_skills = max_skills
        self.search = _SearchTool(self)

    @property
    def active(self) -> bool:
        """Whether the live agent catalog requires discovery in a new request."""
        return (
            sum(not isinstance(tool, _SearchTool) for tool in self.agent.tools.list())
            > self.max_tools
            or len(self._skills()) > self.max_skills
        )

    def prepare(self) -> None:
        """Register discovery only when a live catalog crosses a threshold.

        Raises:
            ConfigError: If an application tool already owns the discovery name.
        """
        if not self.active or self.agent.tools.get(self.search.name) is self.search:
            return
        existing = self.agent.tools.get(self.search.name)
        if existing is not None and not isinstance(existing, _SearchTool):
            raise ConfigError("tool_search is reserved while tool discovery is active")
        self.scope.effect(lambda: self.agent.tools.replace(self.search))

    def _skills(self) -> dict[str, Any]:
        return {
            name: skill
            for name, skill in self.agent.skills.items()
            if not skill.frontmatter.disable_model_invocation
        }

    def select_tools(self, session: Any, registry: ToolRegistry) -> ToolRegistry:
        """Select the schemas offered by this request's already-restricted catalog.

        Args:
            session: Session owning loaded names and the request snapshot.
            registry: Tools admitted by context selection and skill restrictions.

        Returns:
            Tools admitted by the supplied request, optionally including the
            discovery tool and tracking deferred names.
        """
        if not self.active:
            session._extensions.pop(_KEY, None)
            return (
                registry.subset(exclude={"tool_search"})
                if registry.get("tool_search") is self.search
                else registry
            )
        candidates = registry.subset(exclude={"tool_search"})
        defer_tools = len(candidates.list()) > self.max_tools
        skills = self._skills()
        defer_skills = len(skills) > self.max_skills and not session.tools_override
        if not defer_tools and not defer_skills:
            session._extensions.pop(_KEY, None)
            return candidates
        loaded = session.meta.get(_KEY, {})
        loaded_tools = set(loaded.get("tools", ()))
        offered = candidates.select(
            names={
                tool.name
                for tool in candidates.list()
                if not defer_tools
                or getattr(tool, "always_load", False)
                or tool.name in loaded_tools
            }
        )
        search = registry.get("tool_search")
        if search is not None:
            search = copy.copy(search)
            deferred = sorted(
                set(t.name for t in candidates.list()) - set(t.name for t in offered.list())
            )
            skill_names = (
                sorted(set(skills) - set(loaded.get("skills", ()))) if defer_skills else []
            )
            search.description = (
                "Search deferred tools and skills by keywords or select:a,b. "
                "Returns tool schemas and skill descriptions; tools load for the next request."
                f"\nDeferred tools: {', '.join(deferred)}"
                f"\nDeferred skills: {', '.join(skill_names)}"
            )
            offered.register(search)
        session._extensions[_KEY] = {
            "candidates": [t.name for t in candidates.list()],
            "offered": [t.name for t in offered.list()],
            "skills": list(skills),
        }
        return offered

    def skill_listing(self, session: Any, listing: str | None) -> str | None:
        """Build a logged skill reminder using discovered names and deferred names.

        Args:
            session: Session whose searches determine the inline skill listing.
            listing: Existing listing, returned unchanged below the threshold.

        Returns:
            The original listing or descriptions of loaded skills and deferred names.
        """
        from ..skills.listing import build_skill_listing

        skills = self._skills()
        if len(skills) <= self.max_skills or session.tools_override:
            return listing
        loaded = set(session.meta.get(_KEY, {}).get("skills", ()))
        inline = build_skill_listing([s for name, s in skills.items() if name in loaded])
        deferred = ", ".join(sorted(set(skills) - loaded))
        return f"{inline}\nDeferred skills (use tool_search first): {deferred}".lstrip()

    def _guard_call(self, name: str, raw: dict[str, Any], session: Any) -> str | None:
        if session.agent is not self.agent:
            return None
        offering = session._extensions.get(_KEY)
        if offering is not None and name not in offering["offered"]:
            return f"Tool '{name}' is deferred; call tool_search with select:{name} first"
        return None

    def _guard_terminal(self, event: TurnStopping) -> TurnStoppingDecision | None:
        if self.agent.context.get("request_catalog") is not self:
            return None
        block = event.final_block
        offering = event.session._extensions.get(_KEY)
        if (
            block is None
            or offering is None
            or block.name not in offering["candidates"]
            or block.name in offering["offered"]
        ):
            return None
        reason = f"Tool '{block.name}' is deferred; call tool_search first."
        return TurnStoppingDecision(
            "continue",
            feedback=(Message(role="user", content=[TextBlock(text=reason)]),),
            event=ToolCallEndEvent(
                tool_use_id=block.id,
                tool_name=block.name,
                result=reason,
                is_error=True,
            ),
        )


class _SearchTool:
    name = "tool_search"
    description = "Search deferred tools and skills."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"
    parallel = False
    always_load = True

    def __init__(self, catalog: _SearchCatalog) -> None:
        self._catalog = catalog

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate the keyword or explicit-name query.

        Args:
            raw: Model-supplied search arguments.

        Returns:
            The trimmed query.

        Raises:
            ValueError: If query is empty or is not a string.
        """
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return {"query": query.strip()}

    def summarize(self, input: dict[str, Any]) -> str:
        """Summarize a deferred-catalog search.

        Args:
            input: Validated search request.

        Returns:
            Human-readable search summary.
        """

        return f"tool_search({input['query']})"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Return matches and durably load their names for later requests.

        Args:
            input: Validated keyword or select query.
            ctx: Execution context identifying the session.

        Returns:
            Tool schemas and skill descriptions; skill bodies remain with Skill.
        """
        session = self._catalog.agent._sessions[ctx.session_id]
        offering = session._extensions.get(_KEY)
        if offering is None or self.name not in offering["offered"]:
            return ToolResult(content="No deferred catalog is offered on this turn.", is_error=True)
        registry = session.tools_override or self._catalog.agent.tools
        tools = registry.select(names=set(offering["candidates"])).schemas()
        skills = [
            {"name": name, "description": skill.frontmatter.description}
            for name, skill in self._catalog._skills().items()
            if name in offering["skills"]
        ]
        query = input["query"]
        matches = _match(query, tools)
        skill_matches = _match(query, skills)
        loaded = session.meta.get(_KEY, {})
        # Bound state by the current live catalogs, not by the number of searches.
        state = {
            "tools": sorted(
                (set(loaded.get("tools", ())) | {s["name"] for s in matches})
                & {t.name for t in self._catalog.agent.tools.list()}
            ),
            "skills": sorted(
                (set(loaded.get("skills", ())) | {s["name"] for s in skill_matches})
                & set(self._catalog._skills())
            ),
        }
        await session.update_meta({_KEY: state})
        return ToolResult(content=json.dumps({"tools": matches, "skills": skill_matches}))


def _match(query: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if query.startswith("select:"):
        names = {name.strip() for name in query[7:].split(",")}
        return [entry for entry in entries if entry["name"] in names]
    tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = [
        (len(tokens & set(re.findall(r"[a-z0-9]+", f"{e['name']} {e['description']}".lower()))), e)
        for e in entries
    ]
    return [
        entry
        for score, entry in sorted(scored, key=lambda pair: (-pair[0], pair[1]["name"]))[:5]
        if score
    ]
