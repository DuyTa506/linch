from __future__ import annotations

from pathlib import Path

import pytest

from linch_studio.server import FileProjectStore
from linch_studio.server.errors import (
    StaleDigest,
    StructuralBlueprintRejected,
    UnsafeProjectPath,
)
from linch_studio.spec import Blueprint, dump_blueprint, load_blueprint

VALID_DEMO = """\
apiVersion: studio.linch.dev/v1alpha1
kind: LinchProject
metadata:
  name: demo
  title: Demo
  description: Store contract test.
spec:
  package: demo
  provider:
    kind: openai_responses
    model: gpt-5
    apiKeyEnv: OPENAI_API_KEY
  primaryAgent:
    maxTurns: 4
    budget:
      maxTokens: 10000
"""


def _blueprint(source: str = VALID_DEMO) -> Blueprint:
    result = load_blueprint(source)
    assert result.blueprint is not None
    return result.blueprint


def test_store_crud_cas_layout_and_proposals(tmp_path: Path) -> None:
    store = FileProjectStore(tmp_path / "workspace")
    created = store.create_project("demo", title="Demo")
    assert created.export_ready is False
    assert [item.project_id for item in store.list_projects()] == ["demo"]

    saved = store.save_blueprint("demo", VALID_DEMO, base_digest=created.digest)
    assert saved.export_ready is True
    with pytest.raises(StaleDigest) as stale:
        store.save_blueprint("demo", VALID_DEMO, base_digest=created.digest)
    assert stale.value.current_digest == saved.digest

    with pytest.raises(StructuralBlueprintRejected):
        store.save_blueprint("demo", "kind: [", base_digest=saved.digest)
    assert store.open_project("demo").digest == saved.digest

    layout = {
        "nodes": [{"id": "node_a", "x": 1.0, "y": 2.0}],
        "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0},
    }
    assert store.save_layout("demo", layout) == layout
    assert store.load_layout("demo") == layout
    assert store.open_project("demo").digest == saved.digest

    metadata = saved.blueprint.metadata.model_copy(update={"title": "Candidate"})
    candidate = saved.blueprint.model_copy(update={"metadata": metadata})
    proposal = store.save_proposal(
        "demo",
        base_digest=saved.digest,
        candidate=candidate,
        semantic_diff=({"operation": "replace", "path": "/metadata/title"},),
    )
    assert store.get_proposal("demo", proposal.id).candidate == candidate
    assert len(store.list_proposals("demo")) == 1
    accepted = store.accept_proposal("demo", proposal.id)
    assert accepted.blueprint.metadata.title == "Candidate"
    assert store.list_proposals("demo") == ()


def test_store_stale_proposal_remains_pending(tmp_path: Path) -> None:
    store = FileProjectStore(tmp_path / "workspace")
    created = store.create_project("demo")
    candidate = _blueprint()
    proposal = store.save_proposal(
        "demo",
        base_digest=created.digest,
        candidate=candidate,
        semantic_diff=(),
    )

    metadata = created.blueprint.metadata.model_copy(update={"title": "Direct Edit"})
    direct = created.blueprint.model_copy(update={"metadata": metadata})
    store.save_blueprint("demo", dump_blueprint(direct), base_digest=created.digest)

    with pytest.raises(StaleDigest):
        store.accept_proposal("demo", proposal.id)
    assert store.get_proposal("demo", proposal.id).id == proposal.id
    assert store.open_project("demo").blueprint.metadata.title == "Direct Edit"


def test_store_rejects_symlink_workspace_and_project_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked_workspace = tmp_path / "linked-workspace"
    linked_workspace.symlink_to(target, target_is_directory=True)
    with pytest.raises(UnsafeProjectPath):
        FileProjectStore(linked_workspace)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo").symlink_to(target, target_is_directory=True)
    store = FileProjectStore(workspace)
    with pytest.raises(UnsafeProjectPath):
        store.open_project("demo")
