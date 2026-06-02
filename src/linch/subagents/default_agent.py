from __future__ import annotations

from .types import AgentDefinition, AgentFrontmatter

DEFAULT_AGENT_TYPE = "_default"

DEFAULT_AGENT = AgentDefinition(
    name=DEFAULT_AGENT_TYPE,
    file_path="<built-in>",
    source="built-in",
    frontmatter=AgentFrontmatter(
        name=DEFAULT_AGENT_TYPE,
        description="General-purpose subagent with full tool access.",
        tools=None,
    ),
    body="\n".join(
        [
            "You are a focused subagent. The parent agent delegated a single task to you.",
            "Use the tools available to complete the task fully. When done, respond with a",
            "concise text summary of what you did and any key findings — the parent will",
            "relay this to the user, so the response only needs the essentials.",
        ]
    ),
)
