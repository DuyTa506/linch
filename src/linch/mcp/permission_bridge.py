"""Map MCP tool annotations to permission rules.

MCP servers may annotate a tool as ``readOnlyHint`` or ``destructiveHint``.
``make_mcp_tool`` already turns ``readOnlyHint`` into a read scope (so it is
auto-allowed and parallel-safe). This bridge handles the other tier: a
``destructive`` tool maps to a ``ToolRule(name, "ask")`` so it triggers a
permission prompt regardless of the agent's mode (even ``skip-dangerous`` /
``acceptEdits``, where a plain write tool would be auto-allowed).

Pure mechanism, no ``mcp`` import: it reads the ``destructive`` flag and ``name``
that ``make_mcp_tool`` stamps on each tool, so it is importable without the
optional ``mcp`` dependency.
"""

from __future__ import annotations

from typing import Any

from ..permissions.rules import ToolRule


def mcp_permission_rules(tools: list[Any]) -> list[ToolRule]:
    """Derive permission rules from MCP tool annotations.

    Emits one ``ToolRule(name, "ask")`` per ``destructive`` tool. Read-only and
    plain tools get no rule (their scope already drives the default decision).
    """
    rules: list[ToolRule] = []
    for tool in tools:
        if getattr(tool, "destructive", False):
            rules.append(ToolRule(tool.name, "ask"))
    return rules
