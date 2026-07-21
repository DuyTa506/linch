from __future__ import annotations

import json

from linch_studio.spec import (
    blueprint_digest,
    canonical_json,
    default_blueprint,
    dump_blueprint,
    load_blueprint,
)


def test_default_blueprint_normalizes_cli_name_and_is_a_loadable_draft() -> None:
    blueprint = default_blueprint("nightly-review")

    assert blueprint.metadata.name == "nightly_review"
    assert blueprint.metadata.title == "Nightly Review"
    assert blueprint.spec.package == "nightly_review"
    loaded = load_blueprint(dump_blueprint(blueprint))
    assert loaded.blueprint == blueprint
    assert loaded.structurally_valid is True
    assert loaded.export_ready is False


def test_canonical_digest_ignores_yaml_order_and_formatting() -> None:
    source_a = """\
apiVersion: studio.linch.dev/v1alpha1
kind: LinchProject
metadata: {name: demo, title: Demo}
spec:
  package: demo
  provider: {kind: openai_responses, model: gpt-5}
"""
    source_b = """\
kind: LinchProject
metadata:
  title: Demo
  name: demo
apiVersion: studio.linch.dev/v1alpha1
spec: {provider: {model: gpt-5, kind: openai_responses}, package: demo}
"""
    first = load_blueprint(source_a).blueprint
    second = load_blueprint(source_b).blueprint

    assert first is not None and second is not None
    assert blueprint_digest(first) == blueprint_digest(second)
    assert canonical_json(first) == canonical_json(second)
    decoded = json.loads(canonical_json(first))
    assert decoded["spec"]["target"]["linch"] == ">=1.1,<2"


def test_yaml_serializer_is_deterministic_and_emits_no_alias_or_tag_syntax() -> None:
    blueprint = default_blueprint("demo")
    first = dump_blueprint(blueprint)
    second = dump_blueprint(blueprint)

    assert first == second
    assert "&id" not in first
    assert "*id" not in first
    assert "!!" not in first
    assert first.endswith("\n")
