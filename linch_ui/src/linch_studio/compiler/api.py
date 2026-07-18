"""Compiler entry points: validate, normalize, contribute, parse, and manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from linch_studio.spec import (
    Blueprint,
    Diagnostic,
    blueprint_digest,
    dump_blueprint,
    has_errors,
    load_blueprint,
    load_blueprint_file,
    validate_blueprint,
)

from .contributions import ContributionSet
from .export import CompiledProject, finalize_project
from .ir import normalize_blueprint
from .packs.completion import CompletionPack
from .packs.docs import ComponentDocsPack
from .packs.evals import EvalPack
from .packs.extensions import ExtensionPack
from .packs.project import ProjectPack
from .packs.providers import ProviderPack
from .packs.routines import RoutinePack
from .packs.tools import ToolPack
from .packs.workflows import WorkflowPack


@dataclass(frozen=True, slots=True)
class CompilerError(RuntimeError):
    diagnostics: tuple[Diagnostic, ...]

    def __str__(self) -> str:
        codes = ", ".join(item.code for item in self.diagnostics[:5])
        suffix = " ..." if len(self.diagnostics) > 5 else ""
        return f"blueprint is not export-ready: {codes}{suffix}"


_PACKS = (
    ProjectPack(),
    ProviderPack(),
    ToolPack(),
    CompletionPack(),
    WorkflowPack(),
    RoutinePack(),
    ExtensionPack(),
    EvalPack(),
    ComponentDocsPack(),
)


def compile_blueprint(blueprint: Blueprint) -> CompiledProject:
    """Compile one structurally valid, semantically export-ready blueprint."""

    diagnostics = validate_blueprint(blueprint)
    if has_errors(diagnostics):
        raise CompilerError(diagnostics)
    yaml_text = dump_blueprint(blueprint)
    digest = blueprint_digest(blueprint)
    ir = normalize_blueprint(blueprint, yaml_text=yaml_text, digest=digest)
    contributions = ContributionSet()
    for pack in _PACKS:
        contributions.extend(pack.contribute(ir))
    return finalize_project(
        contributions,
        blueprint_digest=digest,
        selected_capabilities=ir.selected_capabilities,
        linch_constraint=ir.linch_constraint,
    )


def compile_source(source: str | bytes) -> CompiledProject:
    result = load_blueprint(source)
    if result.blueprint is None or has_errors(result.diagnostics):
        raise CompilerError(result.diagnostics)
    return compile_blueprint(result.blueprint)


def compile_file(path: str | Path) -> CompiledProject:
    result = load_blueprint_file(path)
    if result.blueprint is None or has_errors(result.diagnostics):
        raise CompilerError(result.diagnostics)
    return compile_blueprint(result.blueprint)


__all__ = ["CompilerError", "compile_blueprint", "compile_file", "compile_source"]
