"""MCP, skills, subagents, and external-database skeleton pack."""

from __future__ import annotations

import yaml  # type: ignore[reportMissingModuleSource]

from ..contributions import FileContribution
from ..ir import CompilerIR
from .common import file, py


class ExtensionPack:
    capability_id = "extensions"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        files: list[FileContribution] = []
        for subagent in ir.subagents:
            frontmatter: dict[str, object] = {
                "name": subagent.id,
                "description": subagent.description or subagent.display_name,
            }
            if subagent.tools:
                frontmatter["tools"] = list(subagent.tools)
            header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
            content = f"---\n{header}\n---\n{subagent.instructions.strip()}\n"
            files.append(
                file(
                    f".linch/agents/{subagent.id}.md",
                    content,
                    f"subagent.{subagent.id}",
                )
            )
        for skill in ir.skills:
            frontmatter = {
                "name": skill.id,
                "description": skill.description or skill.display_name,
            }
            if skill.allowed_tools:
                frontmatter["allowed_tools"] = list(skill.allowed_tools)
            header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
            content = f"---\n{header}\n---\n{skill.instructions.strip()}\n"
            files.append(
                file(
                    f".linch/skills/{skill.id}/SKILL.md",
                    content,
                    f"skill.{skill.id}",
                )
            )

        caps = ir.capabilities()
        if caps["extensions"]["mcpServers"]:
            files.append(
                file(
                    f"src/{ir.package}/integrations/mcp.py",
                    _mcp_module(caps["extensions"]["mcpServers"]),
                    "extensions.mcp",
                )
            )
        if caps["externalDatabase"]["enabled"]:
            files.append(
                file(
                    f"src/{ir.package}/integrations/database.py",
                    _database_module(caps["externalDatabase"]["dsnEnv"]),
                    "integration.external_database.skeleton",
                )
            )
            files.append(
                file(
                    "tests/test_database.py",
                    _database_test(ir),
                    "tests.external_database",
                )
            )
        for adapter in caps["externalDatabase"]["adapterTemplates"]:
            files.append(
                file(
                    f"src/{ir.package}/integrations/{adapter}.py",
                    _adapter_module(adapter),
                    f"adapter.{adapter}.skeleton",
                )
            )
        if caps["externalDatabase"]["vectorGuidance"]:
            files.append(
                file(
                    f"src/{ir.package}/integrations/VECTOR_GUIDANCE.md",
                    _vector_guidance(caps["externalDatabase"]["vectorGuidance"]),
                    "memory.vector.guidance",
                )
            )
        return tuple(files)


def _mcp_module(servers: list[dict[str, object]]) -> str:
    entries: list[str] = []
    for server in servers:
        if server["kind"] == "stdio":
            env_pairs = server.get("env", {})
            assert isinstance(env_pairs, dict)
            env_expr = (
                "{"
                + ", ".join(
                    f"{name!r}: os.environ.get({reference!r}, '')"
                    for name, reference in sorted(env_pairs.items())
                )
                + "}"
            )
            expression = (
                "McpStdioServerConfig("
                f"command={server['command']!r}, args={py(server['args'])}, env={env_expr})"
            )
        else:
            token_env = server.get("tokenEnv")
            headers = (
                f"{{'Authorization': 'Bearer ' + os.environ.get({token_env!r}, '')}}"
                if token_env
                else "None"
            )
            expression = f"McpHttpServerConfig(url={server['url']!r}, headers={headers})"
        entries.append(f"{server['id']!r}: {expression}")
    return f'''\
"""Explicit MCP server configuration. No live discovery occurs."""

import os

from linch import McpHttpServerConfig, McpStdioServerConfig


def build_mcp_servers() -> dict[str, object]:
    return {{{", ".join(entries)}}}
'''


def _database_module(dsn_env: str | None) -> str:
    env = dsn_env or "DATABASE_URL"
    return f'''\
"""External database seam with an offline fake and explicit production TODO."""

from __future__ import annotations

import os
from typing import Any, Protocol


class DatabaseClient(Protocol):
    async def read(self, operation: str, arguments: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def write(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


async def create_database_client() -> DatabaseClient:
    dsn = os.environ.get({py(env)})
    if not dsn:
        raise RuntimeError({py(env)} + " is required for the external database")
    raise NotImplementedError(
        "TODO: construct the production DatabaseClient and define pool ownership"
    )


class FakeDatabaseClient:
    """Deterministic fake for offline tool and lifecycle tests."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.writes: list[tuple[str, dict[str, Any], str]] = []

    async def read(self, operation: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        self.reads.append((operation, dict(arguments)))
        return []

    async def write(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.writes.append((operation, dict(arguments), idempotency_key))
        return {{"accepted": True, "idempotency_key": idempotency_key}}

    async def aclose(self) -> None:
        return None
'''


def _database_test(ir: CompilerIR) -> str:
    return f'''\
"""The fake database remains offline and records idempotency keys."""

from {ir.package}.integrations.database import FakeDatabaseClient


async def test_fake_database_is_offline_and_deterministic() -> None:
    client = FakeDatabaseClient()
    assert await client.read("lookup", {{"id": "one"}}) == []
    result = await client.write("save", {{"id": "one"}}, idempotency_key="run:call")
    assert result["idempotency_key"] == "run:call"
    assert client.writes == [("save", {{"id": "one"}}, "run:call")]
'''


def _adapter_module(adapter: str) -> str:
    contract = {
        "memory_store": "assert_memory_store_contract",
        "file_backend": "assert_file_backend_contract",
        "schedule_store": "assert_schedule_store_contract",
        "mailbox": "assert_mailbox_contract",
        "session_store": "SessionStore protocol",
        "run_store": "RunStore protocol",
    }[adapter]
    return f'''\
"""TODO: implement an external {adapter} adapter.

Validate it against `{contract}` where a public contract helper is available.
Do not claim durability until lifecycle, concurrency, and close ownership are tested.
"""
'''


def _vector_guidance(backends: list[str]) -> str:
    items = "\n".join(
        f"- `{backend}`: implement the public `MemoryStore` seam." for backend in backends
    )
    return f"""\
# Vector memory adapter guidance

No vector SDK is bundled. Selected guidance:

{items}

Preserve namespace/filter semantics, deterministic IDs, bounded result counts,
async lifecycle, and run `assert_memory_store_contract` with a fresh test store.
"""


__all__ = ["ExtensionPack"]
