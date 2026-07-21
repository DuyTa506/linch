"""Shared rendering helpers for compiler packs."""

from __future__ import annotations

import json
import re
from typing import Any

from ..contributions import FileContribution
from ..ir import CompilerIR
from ..serializers import python_literal, toml_string


def file(path: str, content: str, capability: str) -> FileContribution:
    return FileContribution(path=path, content=content, capability_id=capability)


def class_name(identifier: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in identifier.split("_"))


def py(value: Any) -> str:
    return python_literal(value)


def json_value(value: str) -> Any:
    return json.loads(value)


def generated_dependency(ir: CompilerIR) -> str:
    extras: set[str] = set()
    caps = ir.capabilities()
    if ir.provider.kind == "anthropic":
        extras.add("anthropic")
    elif ir.provider.kind == "gemini":
        extras.add("gemini")
    if caps["extensions"]["mcpServers"]:
        extras.add("mcp")
    if caps["observation"]["otel"]:
        extras.add("otel")
    memory_backend = caps["memory"]["backend"]
    if memory_backend == "postgres_keyword":
        extras.add("postgres")
    suffix = f"[{','.join(sorted(extras))}]" if extras else ""
    return f"linch{suffix}{ir.linch_constraint}"


def module_doc(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return repr(clean)


def toml(value: str) -> str:
    return toml_string(value)


__all__ = [
    "class_name",
    "file",
    "generated_dependency",
    "json_value",
    "module_doc",
    "py",
    "toml",
]
