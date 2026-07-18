"""Explicit, deterministic blueprint-version migration registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from .models import API_VERSION, LEGACY_API_VERSION

RawDocument: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class MigrationWarning:
    """A value-affecting migration decision that the user must review."""

    code: str
    path: str
    message: str
    remediation: str
    requires_confirmation: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "remediation": self.remediation,
            "requiresConfirmation": self.requires_confirmation,
        }


@dataclass(frozen=True, slots=True)
class MigrationStepResult:
    document: RawDocument
    warnings: tuple[MigrationWarning, ...] = ()


Migrator: TypeAlias = Callable[[RawDocument], RawDocument | MigrationStepResult]


class MigrationError(ValueError):
    """Raised when no safe, explicit migration path reaches the target version."""


@dataclass(slots=True)
class MigrationRegistry:
    """Registry with at most one explicit successor for each source version.

    A single successor avoids ambiguous paths and makes migration output stable.
    Migrators receive a defensive deep copy and must return a new mapping whose
    ``apiVersion`` matches the registered target.
    """

    _steps: dict[str, tuple[str, Migrator]] = field(default_factory=dict)

    def register(self, source: str, target: str, migrator: Migrator) -> None:
        if not source or not target or source == target:
            raise ValueError("migration versions must be non-empty and different")
        if source in self._steps:
            raise ValueError("a migrator is already registered for the source version")
        if not callable(migrator):
            raise TypeError("migrator must be callable")
        self._steps[source] = (target, migrator)

    def registered_steps(self) -> Mapping[str, str]:
        """A read-only snapshot useful for schema/debug output."""

        return {source: target for source, (target, _) in sorted(self._steps.items())}

    def migrate(
        self,
        document: RawDocument,
        *,
        target: str = API_VERSION,
    ) -> tuple[RawDocument, tuple[str, ...], tuple[MigrationWarning, ...]]:
        source = document.get("apiVersion")
        if not isinstance(source, str) or not source:
            raise MigrationError("blueprint apiVersion is missing or invalid")
        if source == target:
            return deepcopy(document), (), ()

        current = source
        migrated = deepcopy(document)
        visited: set[str] = set()
        versions: list[str] = []
        warnings: list[MigrationWarning] = []
        while current != target:
            if current in visited:
                raise MigrationError("migration path contains a cycle")
            visited.add(current)
            step = self._steps.get(current)
            if step is None:
                raise MigrationError("no explicit migration path is registered")
            next_version, migrator = step
            try:
                outcome = migrator(deepcopy(migrated))
            except Exception as exc:
                raise MigrationError("registered migrator failed") from exc
            if isinstance(outcome, MigrationStepResult):
                candidate = outcome.document
                warnings.extend(outcome.warnings)
            else:
                candidate = outcome
            if not isinstance(candidate, dict):
                raise MigrationError("registered migrator returned a non-mapping document")
            if candidate.get("apiVersion") != next_version:
                raise MigrationError("registered migrator returned the wrong apiVersion")
            migrated = candidate
            current = next_version
            versions.append(current)
        return migrated, tuple(versions), tuple(warnings)


DEFAULT_MIGRATIONS = MigrationRegistry()


def migrate_v1alpha1_to_v1alpha2(document: RawDocument) -> MigrationStepResult:
    """Move the legacy execution fields without inventing enforced worker limits."""

    migrated = deepcopy(document)
    spec = migrated.get("spec")
    if not isinstance(spec, dict):
        # Structural validation after migration will report the precise shape error.
        migrated["apiVersion"] = API_VERSION
        return MigrationStepResult(migrated)

    provider = spec.pop("provider", {})
    primary_value = spec.pop("primaryAgent", {})
    primary = primary_value if isinstance(primary_value, dict) else None

    capabilities = spec.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    structured = capabilities.get("structuredOutput")
    domain_verifier = False
    if isinstance(structured, dict):
        domain_verifier = structured.pop("domainVerifier", False) is True

    completion: dict[str, Any]
    if domain_verifier:
        completion = {
            "mode": "verifier_gated",
            "maxRetries": 2,
            "verifiers": [
                {
                    "kind": "custom_todo",
                    "id": "domain_verifier",
                    "description": (
                        "TODO: implement the migrated domain verifier; this verifier must block "
                        "until implemented."
                    ),
                }
            ],
        }
    else:
        completion = {"mode": "agent_judged", "maxRetries": 2, "verifiers": []}
    if primary is not None:
        primary["preset"] = primary.pop("mode", "standard_agent")
        primary["completion"] = completion
    spec["runtime"] = {
        "provider": provider,
        "agent": primary if primary is not None else primary_value,
    }

    warnings: list[MigrationWarning] = []
    subagents = spec.get("subagents", [])
    if isinstance(subagents, list):
        for index, subagent in enumerate(subagents):
            if not isinstance(subagent, dict):
                continue
            max_turns = subagent.pop("maxTurns", None)
            budget = subagent.pop("budget", None)
            bounded_budget = isinstance(budget, dict) and (
                budget.get("maxTokens") is not None or budget.get("maxCostUsd") is not None
            )
            if max_turns is not None or bounded_budget:
                warnings.append(
                    MigrationWarning(
                        code="migration.worker_limits_dropped",
                        path=f"/spec/subagents/{index}",
                        message=(
                            "Legacy subagent turn or budget limits were removed because Linch "
                            "never enforced worker-specific limits."
                        ),
                        remediation=(
                            "Confirm the migration and review the shared runtime maxTurns and "
                            "RunBudget limits."
                        ),
                    )
                )

    workflows = spec.get("workflows", [])
    if isinstance(workflows, list):
        for workflow in workflows:
            if not isinstance(workflow, dict):
                continue
            workflow["kind"] = "directed"
            nodes = workflow.get("nodes", [])
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if isinstance(node, dict) and node.get("type", "agent_call") == "agent_call":
                    node.setdefault("tools", [])

    legacy_loops = spec.pop("loops", [])
    routines: list[Any] = []
    if isinstance(legacy_loops, list):
        for legacy_loop in legacy_loops:
            if not isinstance(legacy_loop, dict):
                routines.append(legacy_loop)
                continue
            routine = deepcopy(legacy_loop)
            target = routine.get("target", "primary_agent")
            if target == "primary_agent":
                routine["kind"] = "agent_tick"
                routine["target"] = "runtime_agent"
            else:
                routine["kind"] = "workflow_run"
                routine["target"] = target
                routine.pop("charter", None)
                routine.pop("prompt", None)
            routines.append(routine)
    spec["routines"] = routines
    migrated["apiVersion"] = API_VERSION
    return MigrationStepResult(migrated, tuple(warnings))


DEFAULT_MIGRATIONS.register(LEGACY_API_VERSION, API_VERSION, migrate_v1alpha1_to_v1alpha2)


__all__ = [
    "DEFAULT_MIGRATIONS",
    "MigrationError",
    "MigrationRegistry",
    "MigrationStepResult",
    "MigrationWarning",
    "Migrator",
    "RawDocument",
    "migrate_v1alpha1_to_v1alpha2",
]
