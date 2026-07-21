from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from linch_studio.catalog import badges_for_selection
from linch_studio.compiler import CompilerError, compile_file, compile_source, project_fingerprint
from linch_studio.spec import default_blueprint

GOLDEN_DIR = Path(__file__).parents[1] / "golden"
GOLDENS = tuple(sorted(GOLDEN_DIR.glob("*.yaml")))


@pytest.mark.parametrize("blueprint_path", GOLDENS, ids=lambda path: path.stem)
def test_every_golden_blueprint_compiles_to_parseable_public_api_python(
    blueprint_path: Path,
) -> None:
    project = compile_file(blueprint_path)

    assert project.files == tuple(sorted(project.files, key=lambda item: item.path))
    assert project.file("linch-studio.yaml").content
    for contribution in project.files:
        if not contribution.path.endswith(".py"):
            continue
        tree = ast.parse(contribution.content, filename=contribution.path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith("linch."), contribution.path
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("linch.") for alias in node.names)


def test_compiler_output_and_zip_input_are_byte_deterministic() -> None:
    path = GOLDEN_DIR / "workflow_fan.yaml"
    first = compile_file(path)
    second = compile_source(path.read_bytes())

    assert project_fingerprint(first) == project_fingerprint(second)
    assert [(item.path, item.content, item.sha256) for item in first.files] == [
        (item.path, item.content, item.sha256) for item in second.files
    ]


def test_manifest_records_every_non_manifest_file_without_hashing_itself() -> None:
    project = compile_file(GOLDEN_DIR / "external_database.yaml")
    manifest = json.loads(project.file(".linch-studio-manifest.json").content)
    records = manifest["files"]

    assert manifest["blueprintDigest"] == project.blueprint_digest
    assert manifest["targetLinch"] == ">=1.1,<2"
    assert ".linch-studio-manifest.json" not in {record["path"] for record in records}
    assert len(records) == len(project.files) - 1
    for record in records:
        assert record["sha256"] == project.file(record["path"]).sha256


@pytest.mark.parametrize("blueprint_path", GOLDENS, ids=lambda path: path.stem)
def test_selected_capabilities_are_all_known_to_the_catalog(blueprint_path: Path) -> None:
    """The manifest must never claim a capability id the catalog cannot badge."""
    project = compile_file(blueprint_path)
    badges_for_selection(project.selected_capabilities)  # raises KeyError on drift


def test_selected_capabilities_reflect_the_compaction_ladder() -> None:
    """The ladder is independent of ``strategy`` and must be tagged whenever enabled."""
    document = default_blueprint("ladder_project").model_dump(by_alias=True, mode="json")
    document["spec"]["runtime"]["provider"] = {"kind": "openai_responses", "model": "gpt-5"}
    document["spec"]["capabilities"]["compaction"]["ladder"] = {"enabled": True}

    project = compile_source(json.dumps(document))

    assert "compaction.ladder" in project.selected_capabilities
    assert "CompactionLadder(" in project.file("src/ladder_project/reliability.py").content
    badges_for_selection(project.selected_capabilities)


def test_development_guide_links_the_sdk_docs_for_selected_capabilities() -> None:
    project = compile_file(GOLDEN_DIR / "external_database.yaml")
    guide = project.file("DEVELOPMENT.md").content

    assert "## Learn the Linch SDK" in guide
    # A tool-bearing project must point at the tool protocol page, and the
    # database seam's memory/persistence pages, all under the canonical base URL.
    assert "https://github.com/DuyTa506/linch/blob/main/docs/usage/tools.md" in guide
    # The README's first-run pointer lands on Quickstart.
    readme = project.file("README.md").content
    assert "docs/usage/quickstart.md" in readme


@pytest.mark.parametrize("blueprint_path", GOLDENS, ids=lambda path: path.stem)
def test_development_guide_links_only_to_docs_that_exist(blueprint_path: Path) -> None:
    docs_root = Path(__file__).parents[3] / "docs"
    if not docs_root.is_dir():
        pytest.skip("SDK docs tree is not present (installed wheel)")

    guide = compile_file(blueprint_path).file("DEVELOPMENT.md").content
    base = "https://github.com/DuyTa506/linch/blob/main/docs/"
    for line in guide.splitlines():
        marker = line.find(base)
        if marker == -1:
            continue
        relative = line[marker + len(base) :].split(")", 1)[0]
        assert (docs_root / relative).is_file(), relative


def test_semantically_invalid_blueprint_cannot_compile() -> None:
    source = (
        (GOLDEN_DIR / "standard_agent.yaml")
        .read_text(encoding="utf-8")
        .replace("kind: openai_responses", "kind: custom")
        .replace("model: gpt-5", "model: custom-model")
    )
    # A custom provider is an explicit skeleton warning and remains exportable.
    assert compile_source(source)

    missing_provider = source.replace("kind: custom", "kind: null").replace(
        "model: custom-model", "model: null"
    )
    with pytest.raises(CompilerError) as caught:
        compile_source(missing_provider)
    assert {item.code for item in caught.value.diagnostics} >= {
        "semantic.provider_required",
        "semantic.provider_model_required",
    }


def _runtime_tool_blueprint(preset: str, tools: list[str] | None) -> dict[str, object]:
    document = default_blueprint("runtime_tools").model_dump(by_alias=True, mode="json")
    spec = document["spec"]
    assert isinstance(spec, dict)
    runtime = spec["runtime"]
    assert isinstance(runtime, dict)
    runtime["provider"] = {"kind": "openai_responses", "model": "gpt-5"}
    runtime["agent"] = {
        "preset": preset,
        "tools": tools,
        "maxTurns": 8,
        "budget": {"maxTokens": 20_000},
    }
    spec["tools"] = [
        {
            "kind": "function",
            "id": identifier,
            "displayName": identifier.title(),
            "description": f"The {identifier} tool.",
        }
        for identifier in ("allowed_tool", "excluded_tool")
    ]
    if preset == "coordinator":
        spec["subagents"] = [
            {
                "id": "worker",
                "displayName": "Worker",
                "instructions": "Complete the delegated task.",
            }
        ]
    return document


@pytest.mark.parametrize("preset", ["standard_agent", "deep_agent", "coordinator"])
def test_primary_runtime_tool_allowlist_filters_every_agent_preset(preset: str) -> None:
    document = _runtime_tool_blueprint(preset, ["allowed_tool"])

    project = compile_source(json.dumps(document))
    agent_module = project.file("src/runtime_tools/agent.py").content

    assert "from runtime_tools.tools import ALL_TOOLS" in agent_module
    assert 'if tool.name in {"allowed_tool"}' in agent_module
    assert "excluded_tool" not in agent_module
    if preset == "standard_agent":
        assert "registry = empty_tools()" in agent_module
    else:
        assert "registry = default_tools()" in agent_module
    excluded_docs = project.file("docs/components/excluded_tool.md").content
    assert "excluded from the primary runtime registry" in excluded_docs


def test_primary_runtime_tool_policy_distinguishes_default_all_from_explicit_empty() -> None:
    default_project = compile_source(json.dumps(_runtime_tool_blueprint("standard_agent", None)))
    empty_project = compile_source(json.dumps(_runtime_tool_blueprint("standard_agent", [])))

    assert (
        "registry = empty_tools(*ALL_TOOLS)"
        in default_project.file("src/runtime_tools/agent.py").content
    )
    empty_agent = empty_project.file("src/runtime_tools/agent.py").content
    assert "from runtime_tools.tools import ALL_TOOLS" not in empty_agent
    assert "registry = empty_tools()" in empty_agent


def test_workflow_tool_filter_preserves_defaults_unless_explicitly_set() -> None:
    inherited = compile_file(GOLDEN_DIR / "workflow_chain.yaml")
    explicit = compile_file(GOLDEN_DIR / "workflow_routine.yaml")

    inherited_module = inherited.file("src/review_chain/workflows/review_flow.py").content
    explicit_module = explicit.file("src/workflow_routine/workflows/review_flow.py").content

    assert "tools=None" in inherited_module
    assert 'tools=["inspect_repo"]' in explicit_module


def test_tool_class_names_stay_unique_when_class_name_would_collide() -> None:
    """``tool_1`` and ``tool1`` both render to ``Tool1`` under the naive PascalCase split."""
    document = default_blueprint("colliding_tools").model_dump(by_alias=True, mode="json")
    document["spec"]["runtime"]["provider"] = {"kind": "openai_responses", "model": "gpt-5"}
    document["spec"]["tools"] = [
        {
            "kind": "function",
            "id": identifier,
            "displayName": identifier.title(),
            "description": f"The {identifier} tool.",
        }
        for identifier in ("tool_1", "tool1")
    ]

    project = compile_source(json.dumps(document))

    init_tree = ast.parse(project.file("src/colliding_tools/tools/__init__.py").content)
    imports = [node for node in ast.walk(init_tree) if isinstance(node, ast.ImportFrom)]
    imported_symbols = [alias.asname or alias.name for node in imports for alias in node.names]
    assert len(imported_symbols) == len(set(imported_symbols)) == 2

    for identifier in ("tool_1", "tool1"):
        module_tree = ast.parse(project.file(f"src/colliding_tools/tools/{identifier}.py").content)
        defined_classes = {
            node.name for node in ast.walk(module_tree) if isinstance(node, ast.ClassDef)
        }
        imported_symbol = next(
            alias.asname or alias.name
            for node in imports
            if node.module == identifier
            for alias in node.names
        )
        assert imported_symbol in defined_classes
