"""Resource-bounded, non-executing YAML loader for Studio blueprints."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[reportMissingModuleSource]
from pydantic import ValidationError
from yaml.events import (  # type: ignore[reportMissingModuleSource]
    CollectionEndEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    ScalarEvent,
)
from yaml.nodes import MappingNode  # type: ignore[reportMissingModuleSource]
from yaml.tokens import (  # type: ignore[reportMissingModuleSource]
    AliasToken,
    AnchorToken,
    ScalarToken,
    TagToken,
)

from .diagnostics import Diagnostic, Severity, has_errors, json_path, sort_diagnostics
from .migrations import (
    DEFAULT_MIGRATIONS,
    MigrationError,
    MigrationRegistry,
    MigrationWarning,
)
from .models import API_VERSION, Blueprint
from .validation import validate_blueprint

MAX_INPUT_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_COLLECTIONS = 10_000
MAX_SCALARS = 50_000
MAX_SCALAR_BYTES = 262_144


class _DuplicateKeyError(yaml.YAMLError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that never applies last-key-wins semantics."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise yaml.constructor.ConstructorError(None, None, "mapping required", node.start_mark)
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be hashable",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise _DuplicateKeyError("duplicate mapping key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass(frozen=True, slots=True)
class BlueprintLoadResult:
    """Structural result plus semantic findings for a draft blueprint."""

    blueprint: Blueprint | None
    diagnostics: tuple[Diagnostic, ...]
    migrated_from: str | None = None
    migration_warnings: tuple[MigrationWarning, ...] = ()

    @property
    def structurally_valid(self) -> bool:
        return self.blueprint is not None

    @property
    def export_ready(self) -> bool:
        return self.blueprint is not None and not has_errors(self.diagnostics)

    @property
    def requires_migration_confirmation(self) -> bool:
        return any(item.requires_confirmation for item in self.migration_warnings)


def _diagnostic(
    code: str,
    message: str,
    remediation: str,
    *,
    path: str = "/",
    severity: Severity = "error",
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        path=path,
        message=message,
        remediation=remediation,
    )


def _yaml_syntax_diagnostic(exc: BaseException) -> Diagnostic:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        message = "Blueprint YAML is not well-formed."
    else:
        message = (
            f"Blueprint YAML is not well-formed at line {mark.line + 1}, column {mark.column + 1}."
        )
    return _diagnostic(
        "yaml.syntax",
        message,
        "Correct the YAML syntax and try again.",
    )


def _preflight_yaml(text: str) -> Diagnostic | None:
    try:
        for token in yaml.scan(text, Loader=_UniqueKeyLoader):
            if isinstance(token, (AnchorToken, AliasToken)):
                return _diagnostic(
                    "yaml.alias_unsupported",
                    "YAML anchors and aliases are not allowed in Studio blueprints.",
                    "Write each value explicitly without anchors, aliases, or merge keys.",
                )
            if isinstance(token, TagToken):
                return _diagnostic(
                    "yaml.tag_unsupported",
                    "Explicit YAML tags are not allowed in Studio blueprints.",
                    "Remove the tag and use ordinary YAML scalar, mapping, or sequence values.",
                )
            if isinstance(token, ScalarToken):
                if len(token.value.encode("utf-8")) > MAX_SCALAR_BYTES:
                    return _diagnostic(
                        "yaml.scalar_limit",
                        "A YAML scalar exceeds the Studio size limit.",
                        "Shorten the scalar to at most 262144 UTF-8 bytes.",
                    )

        depth = 0
        collections = 0
        scalars = 0
        documents = 0
        for event in yaml.parse(text, Loader=_UniqueKeyLoader):
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    return _diagnostic(
                        "yaml.multiple_documents",
                        "A blueprint file must contain exactly one YAML document.",
                        "Keep one LinchProject document per blueprint file.",
                    )
            elif isinstance(event, CollectionStartEvent):
                depth += 1
                collections += 1
                if depth > MAX_DEPTH:
                    return _diagnostic(
                        "yaml.depth_limit",
                        "The YAML document exceeds the Studio nesting limit.",
                        "Reduce nesting to at most 64 mapping or sequence levels.",
                    )
                if collections > MAX_COLLECTIONS:
                    return _diagnostic(
                        "yaml.collection_limit",
                        "The YAML document contains too many collections.",
                        "Reduce the document to at most 10000 mappings and sequences.",
                    )
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            elif isinstance(event, ScalarEvent):
                scalars += 1
                if scalars > MAX_SCALARS:
                    return _diagnostic(
                        "yaml.scalar_count_limit",
                        "The YAML document contains too many scalar values.",
                        "Reduce the document to at most 50000 scalar values.",
                    )
    except RecursionError:
        return _diagnostic(
            "yaml.depth_limit",
            "The YAML document exceeds the Studio nesting limit.",
            "Reduce nesting to at most 64 mapping or sequence levels.",
        )
    except yaml.YAMLError as exc:
        return _yaml_syntax_diagnostic(exc)
    return None


def _validate_loaded_tree(value: Any) -> Diagnostic | None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    collections = 0
    scalars = 0
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            collections += 1
            if depth > MAX_DEPTH or collections > MAX_COLLECTIONS:
                return _diagnostic(
                    "yaml.collection_limit",
                    "The loaded document exceeds Studio collection limits.",
                    "Reduce the document's size or nesting.",
                )
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            collections += 1
            if depth > MAX_DEPTH or collections > MAX_COLLECTIONS:
                return _diagnostic(
                    "yaml.collection_limit",
                    "The loaded document exceeds Studio collection limits.",
                    "Reduce the document's size or nesting.",
                )
            stack.extend((item, depth + 1) for item in current)
        else:
            scalars += 1
            if scalars > MAX_SCALARS:
                return _diagnostic(
                    "yaml.scalar_count_limit",
                    "The loaded document contains too many scalar values.",
                    "Reduce the document's size.",
                )
            if isinstance(current, str) and len(current.encode("utf-8")) > MAX_SCALAR_BYTES:
                return _diagnostic(
                    "yaml.scalar_limit",
                    "A loaded scalar exceeds the Studio size limit.",
                    "Shorten the scalar to at most 262144 UTF-8 bytes.",
                )
            if isinstance(current, float) and not math.isfinite(current):
                return _diagnostic(
                    "yaml.nonfinite_float",
                    "Non-finite floating-point values are not allowed.",
                    "Replace the value with a finite JSON-compatible number.",
                )
    return None


def _wire_location(location: tuple[Any, ...], *, drop_last: bool = False) -> str:
    parts = list(location[:-1] if drop_last and location else location)
    wire_parts: list[str | int] = []
    for part in parts:
        if isinstance(part, int):
            wire_parts.append(part)
        elif isinstance(part, str):
            head, *tail = part.split("_")
            wire_parts.append(head + "".join(piece[:1].upper() + piece[1:] for piece in tail))
    return json_path(*wire_parts)


def _structural_diagnostics(exc: ValidationError) -> tuple[Diagnostic, ...]:
    findings: list[Diagnostic] = []
    messages: dict[str, tuple[str, str, str]] = {
        "extra_forbidden": (
            "schema.extra_field",
            "The blueprint contains a field not defined by v1alpha2.",
            "Remove the unsupported field or migrate to a schema version that defines it.",
        ),
        "missing": (
            "schema.required",
            "A required blueprint field is missing.",
            "Add the required field using the v1alpha2 schema.",
        ),
        "literal_error": (
            "schema.literal",
            "A field does not use one of its allowed values.",
            "Choose a value allowed by the v1alpha2 schema.",
        ),
        "string_pattern_mismatch": (
            "schema.pattern",
            "A string does not match the required identifier format.",
            "Use the documented lowercase ID or uppercase environment-name format.",
        ),
        "union_tag_invalid": (
            "schema.discriminator",
            "A tagged object uses an unsupported discriminator.",
            "Choose a supported object kind or type.",
        ),
        "union_tag_not_found": (
            "schema.discriminator",
            "A tagged object is missing its discriminator.",
            "Add the required kind or type field.",
        ),
    }
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        error_type = str(error.get("type", ""))
        code, message, remediation = messages.get(
            error_type,
            (
                "schema.invalid",
                "A blueprint field does not satisfy the v1alpha2 schema.",
                "Correct the field using the generated Studio JSON Schema.",
            ),
        )
        location = tuple(error.get("loc", ()))
        findings.append(
            _diagnostic(
                code,
                message,
                remediation,
                path=_wire_location(location, drop_last=error_type == "extra_forbidden"),
            )
        )
    return sort_diagnostics(findings)


def load_blueprint(
    source: str | bytes,
    *,
    migrations: MigrationRegistry | None = None,
    include_semantic: bool = True,
) -> BlueprintLoadResult:
    """Parse untrusted YAML without executing it or leaking rejected values."""

    if isinstance(source, bytes):
        raw_bytes = source
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError:
            diagnostic = _diagnostic(
                "yaml.encoding",
                "Blueprint YAML must be valid UTF-8.",
                "Save the blueprint as UTF-8 and try again.",
            )
            return BlueprintLoadResult(None, (diagnostic,))
    else:
        text = source
        try:
            raw_bytes = text.encode("utf-8")
        except UnicodeEncodeError:
            diagnostic = _diagnostic(
                "yaml.encoding",
                "Blueprint YAML must be valid UTF-8.",
                "Save the blueprint as UTF-8 and try again.",
            )
            return BlueprintLoadResult(None, (diagnostic,))
    if len(raw_bytes) > MAX_INPUT_BYTES:
        diagnostic = _diagnostic(
            "yaml.input_limit",
            "The blueprint exceeds the 1 MiB input limit.",
            "Reduce the blueprint to at most 1048576 UTF-8 bytes.",
        )
        return BlueprintLoadResult(None, (diagnostic,))

    preflight = _preflight_yaml(text)
    if preflight is not None:
        return BlueprintLoadResult(None, (preflight,))
    try:
        loaded = yaml.load(text, Loader=_UniqueKeyLoader)
    except _DuplicateKeyError:
        diagnostic = _diagnostic(
            "yaml.duplicate_key",
            "Duplicate mapping keys are not allowed.",
            "Keep each mapping key exactly once.",
        )
        return BlueprintLoadResult(None, (diagnostic,))
    except RecursionError:
        diagnostic = _diagnostic(
            "yaml.depth_limit",
            "The YAML document exceeds the Studio nesting limit.",
            "Reduce nesting to at most 64 mapping or sequence levels.",
        )
        return BlueprintLoadResult(None, (diagnostic,))
    except yaml.YAMLError as exc:
        return BlueprintLoadResult(None, (_yaml_syntax_diagnostic(exc),))

    if not isinstance(loaded, dict):
        diagnostic = _diagnostic(
            "schema.root_mapping",
            "A blueprint document must be a YAML mapping.",
            "Use a mapping with apiVersion, kind, metadata, and spec fields.",
        )
        return BlueprintLoadResult(None, (diagnostic,))
    tree_diagnostic = _validate_loaded_tree(loaded)
    if tree_diagnostic is not None:
        return BlueprintLoadResult(None, (tree_diagnostic,))

    version = loaded.get("apiVersion")
    if not isinstance(version, str) or not version:
        diagnostic = _diagnostic(
            "schema.api_version_required",
            "A blueprint requires a string apiVersion field.",
            "Set apiVersion to the current Studio blueprint version.",
            path="/apiVersion",
        )
        return BlueprintLoadResult(None, (diagnostic,))

    registry = migrations or DEFAULT_MIGRATIONS
    migrated_from: str | None = None
    migration_diagnostics: list[Diagnostic] = []
    migration_warnings: tuple[MigrationWarning, ...] = ()
    if version != API_VERSION:
        try:
            loaded, migrated_versions, migration_warnings = registry.migrate(
                loaded, target=API_VERSION
            )
        except MigrationError:
            diagnostic = _diagnostic(
                "schema.unsupported_api_version",
                "The blueprint apiVersion is unsupported and has no explicit migrator.",
                "Use the current apiVersion or install a registered migration path.",
                path="/apiVersion",
            )
            return BlueprintLoadResult(None, (diagnostic,))
        migrated_from = version
        if migrated_versions:
            migration_diagnostics.append(
                _diagnostic(
                    "schema.migrated",
                    "The blueprint was migrated through an explicit version path.",
                    "Review and save the migrated blueprint in the current format.",
                    path="/apiVersion",
                    severity="info",
                )
            )
        migration_diagnostics.extend(
            _diagnostic(
                item.code,
                item.message,
                item.remediation,
                path=item.path,
                severity="warning",
            )
            for item in migration_warnings
        )
        tree_diagnostic = _validate_loaded_tree(loaded)
        if tree_diagnostic is not None:
            return BlueprintLoadResult(
                None,
                (tree_diagnostic,),
                migrated_from,
                migration_warnings,
            )

    try:
        blueprint = Blueprint.model_validate(loaded, strict=True)
    except ValidationError as exc:
        return BlueprintLoadResult(
            None,
            sort_diagnostics((*migration_diagnostics, *_structural_diagnostics(exc))),
            migrated_from,
            migration_warnings,
        )

    findings = migration_diagnostics
    if include_semantic:
        findings.extend(validate_blueprint(blueprint))
    return BlueprintLoadResult(
        blueprint,
        sort_diagnostics(findings),
        migrated_from,
        migration_warnings,
    )


def load_blueprint_file(
    path: str | Path,
    *,
    migrations: MigrationRegistry | None = None,
    include_semantic: bool = True,
) -> BlueprintLoadResult:
    """Read at most one byte beyond the input cap, then delegate to the loader."""

    try:
        with Path(path).open("rb") as handle:
            source = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        diagnostic = _diagnostic(
            "io.blueprint_read",
            "The blueprint file could not be read.",
            "Check that the file exists and is readable.",
        )
        return BlueprintLoadResult(None, (diagnostic,))
    return load_blueprint(
        source,
        migrations=migrations,
        include_semantic=include_semantic,
    )


parse_blueprint = load_blueprint


__all__ = [
    "MAX_COLLECTIONS",
    "MAX_DEPTH",
    "MAX_INPUT_BYTES",
    "MAX_SCALAR_BYTES",
    "MAX_SCALARS",
    "BlueprintLoadResult",
    "load_blueprint",
    "load_blueprint_file",
    "parse_blueprint",
]
