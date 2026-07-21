from __future__ import annotations

import json
from copy import deepcopy

import linch_studio.spec.loader as loader_module
from linch_studio.spec import (
    API_VERSION,
    LEGACY_API_VERSION,
    MigrationRegistry,
    load_blueprint,
    migrate_v1alpha1_to_v1alpha2,
)


def _valid_yaml(*, api_version: str = API_VERSION) -> str:
    return f"""\
apiVersion: {api_version}
kind: LinchProject
metadata:
  name: demo
  title: Demo
spec:
  package: demo
  runtime:
    provider:
      kind: openai_responses
      model: gpt-5
"""


def _legacy_yaml(*, worker_limits: bool = False) -> str:
    worker = (
        """
  subagents:
    - id: worker
      displayName: Worker
      instructions: Do one task.
      maxTurns: 3
      budget:
        maxTokens: 5000
"""
        if worker_limits
        else ""
    )
    return f"""\
apiVersion: {LEGACY_API_VERSION}
kind: LinchProject
metadata:
  name: demo
  title: Demo
spec:
  package: demo
  provider:
    kind: openai_responses
    model: gpt-5
  primaryAgent:
    mode: standard_agent
    maxTurns: 8
    budget:
      maxTokens: 20000
{worker}"""


def _codes(result: object) -> set[str]:
    return {item.code for item in result.diagnostics}  # type: ignore[attr-defined]


def test_semantic_invalid_blueprint_remains_loadable() -> None:
    source = _valid_yaml().replace(
        "    provider:\n      kind: openai_responses\n      model: gpt-5\n",
        "    provider: {}\n",
    )
    result = load_blueprint(source)

    assert result.structurally_valid is True
    assert result.export_ready is False
    assert result.blueprint is not None
    assert _codes(result) == {
        "semantic.provider_model_required",
        "semantic.provider_required",
    }


def test_rejects_oversized_input_without_echoing_it(monkeypatch) -> None:
    monkeypatch.setattr(loader_module, "MAX_INPUT_BYTES", 20)
    secret = "super-secret-input-value"
    result = load_blueprint(secret)

    assert result.blueprint is None
    assert _codes(result) == {"yaml.input_limit"}
    assert secret not in result.diagnostics[0].model_dump_json()


def test_rejects_anchors_aliases_and_explicit_tags() -> None:
    anchored = (
        _valid_yaml()
        .replace("name: demo", "name: &project_name demo")
        .replace("package: demo", "package: *project_name")
    )
    tagged = _valid_yaml().replace("title: Demo", "title: !!str Demo")

    assert _codes(load_blueprint(anchored)) == {"yaml.alias_unsupported"}
    assert _codes(load_blueprint(tagged)) == {"yaml.tag_unsupported"}


def test_rejects_duplicate_keys_and_multiple_documents() -> None:
    duplicate = _valid_yaml().replace("kind: LinchProject", "kind: LinchProject\nkind: Other")
    multiple = _valid_yaml() + "---\n{}\n"

    assert _codes(load_blueprint(duplicate)) == {"yaml.duplicate_key"}
    assert _codes(load_blueprint(multiple)) == {"yaml.multiple_documents"}


def test_rejects_depth_collection_scalar_count_and_scalar_size_limits(monkeypatch) -> None:
    monkeypatch.setattr(loader_module, "MAX_DEPTH", 3)
    assert _codes(load_blueprint("a:\n  b:\n    c:\n      d: value\n")) == {"yaml.depth_limit"}

    monkeypatch.setattr(loader_module, "MAX_DEPTH", 64)
    monkeypatch.setattr(loader_module, "MAX_COLLECTIONS", 3)
    assert _codes(load_blueprint("a:\n  - []\n  - []\n")) == {"yaml.collection_limit"}

    monkeypatch.setattr(loader_module, "MAX_COLLECTIONS", 10_000)
    monkeypatch.setattr(loader_module, "MAX_SCALARS", 3)
    assert _codes(load_blueprint("a: [one, two, three, four]\n")) == {"yaml.scalar_count_limit"}

    monkeypatch.setattr(loader_module, "MAX_SCALARS", 50_000)
    monkeypatch.setattr(loader_module, "MAX_SCALAR_BYTES", 3)
    assert _codes(load_blueprint("a: value\n")) == {"yaml.scalar_limit"}


def test_rejects_nonfinite_float_before_pydantic() -> None:
    source = _valid_yaml() + "    agent:\n      budget:\n        maxCostUsd: .nan\n"
    result = load_blueprint(source)

    assert result.blueprint is None
    assert _codes(result) == {"yaml.nonfinite_float"}


def test_unknown_version_requires_an_explicit_migrator_and_does_not_echo_version() -> None:
    old_version = "studio.example.invalid/secret-version"
    result = load_blueprint(_valid_yaml(api_version=old_version))

    assert result.blueprint is None
    assert _codes(result) == {"schema.unsupported_api_version"}
    assert old_version not in result.diagnostics[0].model_dump_json()


def test_explicit_migration_path_can_reach_current_version() -> None:
    old_version = "studio.linch.dev/v0"
    migrations = MigrationRegistry()

    def migrate(document: dict[str, object]) -> dict[str, object]:
        return {**document, "apiVersion": API_VERSION}

    migrations.register(old_version, API_VERSION, migrate)
    result = load_blueprint(_valid_yaml(api_version=old_version), migrations=migrations)

    assert result.blueprint is not None
    assert result.migrated_from == old_version
    assert _codes(result) == {"schema.migrated"}


def test_v1alpha1_migrates_in_memory_without_worker_warning() -> None:
    source = _legacy_yaml()
    result = load_blueprint(source)

    assert result.blueprint is not None
    assert result.migrated_from == LEGACY_API_VERSION
    assert result.migration_warnings == ()
    assert result.requires_migration_confirmation is False
    assert result.blueprint.spec.runtime.agent.preset == "standard_agent"
    assert result.blueprint.spec.runtime.agent.tools is None
    assert result.blueprint.api_version == API_VERSION
    assert _codes(result) == {"schema.migrated"}
    assert f"apiVersion: {LEGACY_API_VERSION}" in source


def test_worker_limits_are_dropped_with_a_confirmation_warning() -> None:
    result = load_blueprint(_legacy_yaml(worker_limits=True))

    assert result.blueprint is not None
    assert result.requires_migration_confirmation is True
    assert len(result.migration_warnings) == 1
    assert result.migration_warnings[0].code == "migration.worker_limits_dropped"
    assert _codes(result) == {"migration.worker_limits_dropped", "schema.migrated"}
    worker = result.blueprint.spec.subagents[0]
    assert worker.id == "worker"
    assert not hasattr(worker, "max_turns")


def test_migration_preserves_semantic_ids_and_creates_a_blocking_verifier_seam() -> None:
    document = {
        "apiVersion": LEGACY_API_VERSION,
        "kind": "LinchProject",
        "metadata": {"name": "demo", "title": "Demo"},
        "spec": {
            "package": "demo",
            "provider": {"kind": "openai_responses", "model": "gpt-5"},
            "primaryAgent": {"mode": "standard_agent", "maxTurns": 4},
            "workflows": [
                {
                    "id": "review_flow",
                    "displayName": "Review Flow",
                    "nodes": [
                        {
                            "type": "agent_call",
                            "id": "review_step",
                            "label": "Review",
                            "prompt": "Review the result.",
                        }
                    ],
                    "output": "review_step",
                }
            ],
            "loops": [
                {
                    "id": "scheduled_review",
                    "displayName": "Scheduled Review",
                    "charter": "Review on schedule.",
                    "prompt": "Review now.",
                    "target": "review_flow",
                }
            ],
            "capabilities": {"structuredOutput": {"enabled": True, "domainVerifier": True}},
        },
    }
    original = deepcopy(document)

    migrated = migrate_v1alpha1_to_v1alpha2(document).document
    result = load_blueprint(json.dumps(document))

    assert document == original
    assert migrated["spec"]["workflows"][0]["id"] == "review_flow"
    assert migrated["spec"]["workflows"][0]["nodes"][0]["id"] == "review_step"
    assert migrated["spec"]["routines"][0]["id"] == "scheduled_review"
    assert migrated["spec"]["routines"][0]["kind"] == "workflow_run"
    assert result.blueprint is not None
    completion = result.blueprint.spec.runtime.agent.completion
    assert completion.mode == "verifier_gated"
    assert completion.verifiers[0].kind == "custom_todo"
    assert completion.verifiers[0].id == "domain_verifier"


def test_migration_preserves_primary_agent_tool_allowlist() -> None:
    document = {
        "apiVersion": LEGACY_API_VERSION,
        "kind": "LinchProject",
        "metadata": {"name": "demo", "title": "Demo"},
        "spec": {
            "package": "demo",
            "provider": {"kind": "openai_responses", "model": "gpt-5"},
            "primaryAgent": {"mode": "standard_agent", "tools": ["lookup"]},
            "tools": [
                {
                    "kind": "function",
                    "id": "lookup",
                    "displayName": "Lookup",
                    "description": "Look up one record.",
                }
            ],
        },
    }

    result = load_blueprint(json.dumps(document))

    assert result.blueprint is not None
    assert result.blueprint.spec.runtime.agent.tools == ["lookup"]


def test_structural_diagnostics_do_not_echo_unknown_field_names_or_values() -> None:
    secret_key = "secretFieldName"
    secret_value = "secret-value-never-return"
    source = _valid_yaml() + f"  {secret_key}: {secret_value}\n"
    result = load_blueprint(source)
    rendered = json.dumps([item.model_dump() for item in result.diagnostics])

    assert result.blueprint is None
    assert _codes(result) == {"schema.extra_field"}
    assert secret_key not in rendered
    assert secret_value not in rendered
    assert result.diagnostics[0].path == "/spec"
