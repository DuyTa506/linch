"""Strict blueprint schema, hardened codecs, migration, and validation."""

from .canonical import (
    blueprint_digest,
    canonical_blueprint_json,
    canonical_bytes,
    canonical_data,
    canonical_digest,
    canonical_json,
)
from .defaults import create_default_blueprint, default_blueprint
from .diagnostics import Diagnostic, has_errors, json_path, sort_diagnostics
from .loader import (
    MAX_COLLECTIONS,
    MAX_DEPTH,
    MAX_INPUT_BYTES,
    MAX_SCALAR_BYTES,
    MAX_SCALARS,
    BlueprintLoadResult,
    load_blueprint,
    load_blueprint_file,
    parse_blueprint,
)
from .migrations import (
    DEFAULT_MIGRATIONS,
    MigrationError,
    MigrationRegistry,
    MigrationStepResult,
    MigrationWarning,
    migrate_v1alpha1_to_v1alpha2,
)
from .models import API_VERSION, KIND, LEGACY_API_VERSION, Blueprint
from .serialization import dump_blueprint, dump_blueprint_yaml
from .validation import RESERVED_MODULE_NAMES, semantic_diagnostics, validate_blueprint

__all__ = [
    "API_VERSION",
    "KIND",
    "LEGACY_API_VERSION",
    "MAX_COLLECTIONS",
    "MAX_DEPTH",
    "MAX_INPUT_BYTES",
    "MAX_SCALARS",
    "MAX_SCALAR_BYTES",
    "RESERVED_MODULE_NAMES",
    "Blueprint",
    "BlueprintLoadResult",
    "DEFAULT_MIGRATIONS",
    "Diagnostic",
    "MigrationError",
    "MigrationRegistry",
    "MigrationStepResult",
    "MigrationWarning",
    "blueprint_digest",
    "canonical_blueprint_json",
    "canonical_bytes",
    "canonical_data",
    "canonical_digest",
    "canonical_json",
    "create_default_blueprint",
    "default_blueprint",
    "dump_blueprint",
    "dump_blueprint_yaml",
    "has_errors",
    "json_path",
    "load_blueprint",
    "load_blueprint_file",
    "migrate_v1alpha1_to_v1alpha2",
    "parse_blueprint",
    "semantic_diagnostics",
    "sort_diagnostics",
    "validate_blueprint",
]
