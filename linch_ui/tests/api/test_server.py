from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from linch_studio.authoring import (
    AuthoringQuestion,
    MalformedProposalError,
    ProposalGenerationError,
    ProposalTelemetry,
    ToolCallRecord,
    ToolCallUpdate,
)
from linch_studio.server import ProposalDraft, TurnDraft, create_app
from linch_studio.spec import Blueprint, Diagnostic, dump_blueprint

VALID_DEMO = """\
apiVersion: studio.linch.dev/v1alpha1
kind: LinchProject
metadata:
  name: demo
  title: Demo
  description: A bounded offline-test blueprint.
spec:
  package: demo
  provider:
    kind: openai_responses
    model: gpt-5
    apiKeyEnv: OPENAI_API_KEY
  primaryAgent:
    mode: standard_agent
    instructions: Be concise.
    maxTurns: 8
    budget:
      maxTokens: 20000
"""

SEMANTIC_INVALID_DEMO = """\
apiVersion: studio.linch.dev/v1alpha1
kind: LinchProject
metadata:
  name: demo
  title: Demo Draft
  description: Provider selection is intentionally incomplete.
spec:
  package: demo
  provider: {}
  primaryAgent: {}
"""


class TitleAuthoringService:
    def __init__(self, title: str = "AI Candidate") -> None:
        self.title = title

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        assert len(base_digest) == 64
        assert instruction
        metadata = current.metadata.model_copy(update={"title": self.title})
        return ProposalDraft(candidate=current.model_copy(update={"metadata": metadata}))


class UnsafeAuthoringService:
    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        del base_digest, instruction
        permissions = current.spec.capabilities.permissions.model_copy(update={"mode": "trusted"})
        capabilities = current.spec.capabilities.model_copy(update={"permissions": permissions})
        spec = current.spec.model_copy(update={"capabilities": capabilities})
        return ProposalDraft(candidate=current.model_copy(update={"spec": spec}))


class RawCrashAuthoringService:
    """Raises a bare, untyped exception — not an AuthoringError subclass.

    Simulates a genuine unhandled bug (as opposed to a typed provider/model
    failure) to exercise the server's catch-all logging path.
    """

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        del current, base_digest, instruction
        raise RuntimeError("boom, a raw unhandled crash")


class LocalApiClient:
    """Small synchronous facade over httpx's native ASGI transport.

    It keeps the API tests independent of Starlette's optional synchronous
    TestClient wrapper while exercising the actual asynchronous application.
    """

    def __init__(self, app: object) -> None:
        self._app = app

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: object) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: object) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        return asyncio.run(self._request(method, path, **kwargs))

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        transport = httpx.ASGITransport(app=self._app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)


def _client(
    tmp_path: Path,
    *,
    authoring_service: object | None = None,
    static_dir: Path | None = None,
) -> LocalApiClient:
    app = create_app(
        tmp_path / "workspace",
        static_dir=static_dir,
        authoring_service=authoring_service,  # type: ignore[arg-type]
    )
    return LocalApiClient(app)


def _create(client: LocalApiClient) -> dict[str, object]:
    response = client.post("/api/v1/projects", json={"id": "demo", "title": "Demo"})
    assert response.status_code == 201, response.text
    return response.json()


def _save_valid(client: LocalApiClient, base_digest: str) -> dict[str, object]:
    response = client.put(
        "/api/v1/projects/demo/blueprint",
        json={"yaml": VALID_DEMO, "baseDigest": base_digest},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _title_yaml(document: dict[str, object], title: str) -> str:
    blueprint = Blueprint.model_validate(document["blueprint"])
    metadata = blueprint.metadata.model_copy(update={"title": title})
    return dump_blueprint(blueprint.model_copy(update={"metadata": metadata}))


def test_project_crud_cas_and_semantic_drafts(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/v1/projects").json() == {"projects": []}

    created = _create(client)
    initial_digest = created["digest"]
    assert created["exportReady"] is False
    assert created["layout"]["nodes"] == []  # type: ignore[index]

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert listed.json()["projects"][0]["id"] == "demo"

    saved = _save_valid(client, str(initial_digest))
    assert saved["exportReady"] is True
    current_digest = saved["digest"]

    stale = client.put(
        "/api/v1/projects/demo/blueprint",
        json={"yaml": VALID_DEMO, "baseDigest": initial_digest},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "blueprint.stale_digest"
    assert stale.json()["error"]["currentDigest"] == current_digest

    marker = "do-not-reflect-this-secret"
    structural = client.put(
        "/api/v1/projects/demo/blueprint",
        json={
            "yaml": f"apiVersion: studio.linch.dev/v1alpha1\nkind: [{marker}\n",
            "baseDigest": current_digest,
        },
    )
    assert structural.status_code == 422
    assert structural.json()["error"]["code"] == "blueprint.structural_invalid"
    assert marker not in structural.text
    assert client.get("/api/v1/projects/demo").json()["digest"] == current_digest

    draft = client.put(
        "/api/v1/projects/demo/blueprint",
        json={"yaml": SEMANTIC_INVALID_DEMO, "baseDigest": current_digest},
    )
    assert draft.status_code == 200
    assert draft.json()["exportReady"] is False
    assert draft.json()["diagnostics"]
    assert client.post("/api/v1/projects/demo/export/preview").status_code == 422

    invalid_id = "../do-not-reflect-this-secret"
    rejected = client.post("/api/v1/projects", json={"id": invalid_id})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "project.invalid_id"
    assert invalid_id not in rejected.text


def test_layout_is_bounded_and_does_not_change_blueprint_digest(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = _create(client)

    layout = {
        "nodes": [{"id": "node_a", "x": 12.5, "y": -8.0, "width": 240.0}],
        "viewport": {"x": 1.0, "y": 2.0, "zoom": 1.25},
    }
    saved = client.put("/api/v1/projects/demo/layout", json=layout)
    assert saved.status_code == 200, saved.text
    assert saved.json()["layout"] == layout
    assert client.get("/api/v1/projects/demo/layout").json()["layout"] == layout
    assert client.get("/api/v1/projects/demo").json()["digest"] == created["digest"]

    rejected_value = 1000001.0
    invalid = client.put(
        "/api/v1/projects/demo/layout",
        json={
            "nodes": [{"id": "node_a", "x": rejected_value, "y": 0.0}],
            "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0},
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "layout.invalid"
    assert str(rejected_value) not in invalid.text
    assert client.get("/api/v1/projects/demo/layout").json()["layout"] == layout


def test_validate_catalog_schema_and_sanitized_request_errors(tmp_path: Path) -> None:
    client = _client(tmp_path)
    marker = "private-editor-buffer"
    invalid = client.post("/api/v1/validate", json={"yaml": f"kind: [{marker}"})
    assert invalid.status_code == 200
    assert invalid.json()["structurallyValid"] is False
    assert marker not in invalid.text

    semantic = client.post("/api/v1/validate", json={"yaml": SEMANTIC_INVALID_DEMO})
    assert semantic.status_code == 200
    assert semantic.json()["structurallyValid"] is True
    assert semantic.json()["exportReady"] is False
    assert len(semantic.json()["digest"]) == 64

    catalog = client.get("/api/v1/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["catalogVersion"]["apiVersion"] == "studio.linch.dev/catalog/v1alpha2"
    assert catalog.json()["capabilities"]
    assert catalog.json()["relationMatrix"]["version"]["blueprintApiVersion"] == (
        "studio.linch.dev/v1alpha2"
    )

    schema = client.get("/api/v1/schema")
    assert schema.status_code == 200
    assert schema.json()["title"] == "Blueprint"
    assert "$defs" in schema.json()

    request_marker = "never-return-request-input"
    malformed = client.post(
        "/api/v1/validate",
        json={"yaml": VALID_DEMO, "unexpected": request_marker},
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "api.request_invalid"
    assert request_marker not in malformed.text


def test_preview_zip_and_no_overwrite_directory_export(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = _create(client)
    saved = _save_valid(client, str(created["digest"]))

    preview = client.post("/api/v1/projects/demo/export/preview")
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["blueprintDigest"] == saved["digest"]
    assert payload["files"]
    assert {"path", "sha256", "size", "capabilityId", "content"} <= set(payload["files"][0])

    first = client.post("/api/v1/projects/demo/export/zip")
    second = client.post("/api/v1/projects/demo/export/zip")
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "application/zip"
    assert first.content == second.content
    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        assert ".linch-studio-manifest.json" in archive.namelist()

    target = tmp_path / "generated" / "demo"
    exported = client.post(
        "/api/v1/projects/demo/export/directory",
        json={"target": str(target)},
    )
    assert exported.status_code == 200, exported.text
    assert (target / ".linch-studio-manifest.json").is_file()
    conflict = client.post(
        "/api/v1/projects/demo/export/directory",
        json={"target": str(target)},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "export.target_unavailable"
    assert str(target) not in conflict.text


def test_static_spa_fallback_does_not_capture_api_routes(tmp_path: Path) -> None:
    static = tmp_path / "frontend"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<main>studio-index</main>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("window.STUDIO = true;", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    client = _client(tmp_path, static_dir=static)

    assert "studio-index" in client.get("/").text
    assert "studio-index" in client.get("/docs").text
    assert "studio-index" in client.get("/projects/demo").text
    assert client.get("/assets/app.js").text == "window.STUDIO = true;"
    assert "swagger-ui" in client.get("/api/docs").text
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/v1/missing").status_code == 404
    escaped = client.get("/..%2Foutside.txt")
    assert "outside-secret" not in escaped.text


def test_proposal_accept_is_all_or_nothing_and_stale_safe(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=TitleAuthoringService())
    created = _create(client)

    proposed = client.post(
        "/api/v1/projects/demo/proposals",
        json={"instruction": "Give the project a clearer title."},
    )
    assert proposed.status_code == 201, proposed.text
    proposal = proposed.json()
    assert proposal["baseDigest"] == created["digest"]
    assert proposal["candidate"]["metadata"]["title"] == "AI Candidate"
    assert proposal["semanticDiff"] == [
        {
            "operation": "replace",
            "path": "/metadata/title",
            "before": "Demo",
            "after": "AI Candidate",
        }
    ]

    direct_yaml = _title_yaml(created, "Direct Edit")
    direct = client.put(
        "/api/v1/projects/demo/blueprint",
        json={"yaml": direct_yaml, "baseDigest": created["digest"]},
    )
    assert direct.status_code == 200

    stale = client.post(f"/api/v1/projects/demo/proposals/{proposal['id']}/accept")
    assert stale.status_code == 409
    assert stale.json()["error"]["currentDigest"] == direct.json()["digest"]
    current = client.get("/api/v1/projects/demo").json()
    assert current["blueprint"]["metadata"]["title"] == "Direct Edit"
    assert len(client.get("/api/v1/projects/demo/proposals").json()["proposals"]) == 1

    rejected = client.post(f"/api/v1/projects/demo/proposals/{proposal['id']}/reject")
    assert rejected.status_code == 204
    assert client.get("/api/v1/projects/demo/proposals").json() == {"proposals": []}

    fresh = client.post(
        "/api/v1/projects/demo/proposals",
        json={"instruction": "Apply the reviewed title."},
    ).json()
    accepted = client.post(f"/api/v1/projects/demo/proposals/{fresh['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["blueprint"]["metadata"]["title"] == "AI Candidate"
    assert client.get("/api/v1/projects/demo/proposals").json() == {"proposals": []}


def test_authoring_default_and_manual_only_guard(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _create(client)
    unavailable = client.post(
        "/api/v1/projects/demo/proposals",
        json={"instruction": "Change the project."},
    )
    assert unavailable.status_code == 501
    assert unavailable.json()["error"]["code"] == "authoring.unavailable"

    guarded = _client(tmp_path / "guarded", authoring_service=UnsafeAuthoringService())
    created = _create(guarded)
    rejected = guarded.post(
        "/api/v1/projects/demo/proposals",
        json={"instruction": "Enable unrestricted execution."},
    )
    assert rejected.status_code == 502
    assert rejected.json()["error"]["diagnostics"][0]["code"] == ("authoring.manual_only_field")
    assert guarded.get("/api/v1/projects/demo").json()["digest"] == created["digest"]


def test_symlink_project_root_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "demo").symlink_to(outside, target_is_directory=True)
    client = LocalApiClient(create_app(workspace))

    opened = client.get("/api/v1/projects/demo")
    assert opened.status_code == 409
    assert opened.json()["error"]["code"] == "project.unsafe_path"


def test_service_info_reports_authoring_unavailable_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path)

    payload = client.get("/api/v1").json()

    assert payload["authoringAvailable"] is False


def test_service_info_reports_authoring_available_when_configured(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=TitleAuthoringService())

    payload = client.get("/api/v1").json()

    assert payload["authoringAvailable"] is True


def test_project_summary_exposes_updated_at_and_model(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _create(client)

    summary = client.get("/api/v1/projects").json()["projects"][0]

    assert summary["model"] is None or isinstance(summary["model"], str)
    # ISO-8601 UTC, parseable and stable across reads.
    datetime.fromisoformat(summary["updatedAt"])


def test_project_create_accepts_the_shared_template_id(tmp_path: Path) -> None:
    client = _client(tmp_path)

    created = client.post(
        "/api/v1/projects",
        json={"id": "verified", "template": "goal_verified"},
    )

    assert created.status_code == 201, created.text
    completion = created.json()["blueprint"]["spec"]["runtime"]["agent"]["completion"]
    assert completion["mode"] == "verifier_gated"
    assert completion["verifiers"][0]["kind"] == "custom_todo"


def test_opening_legacy_yaml_reports_in_memory_migration_without_overwriting(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _create(client)
    layout = {
        "nodes": [{"id": "legacy_node", "x": 25.0, "y": -10.0}],
        "viewport": {"x": 4.0, "y": 8.0, "zoom": 1.25},
    }
    assert client.put("/api/v1/projects/demo/layout", json=layout).status_code == 200
    source = (
        VALID_DEMO
        + """\
  subagents:
    - id: worker
      displayName: Worker
      instructions: Do one task.
      maxTurns: 2
"""
    )
    path = tmp_path / "workspace" / "demo" / "linch-studio.yaml"
    path.write_text(source, encoding="utf-8")

    opened = client.get("/api/v1/projects/demo")

    assert opened.status_code == 200, opened.text
    payload = opened.json()
    assert payload["migratedFrom"] == "studio.linch.dev/v1alpha1"
    assert payload["blueprint"]["apiVersion"] == "studio.linch.dev/v1alpha2"
    assert payload["migrationWarnings"][0]["code"] == "migration.worker_limits_dropped"
    assert payload["migrationWarnings"][0]["requiresConfirmation"] is True
    assert payload["yaml"] == source
    assert payload["layout"]["nodes"][0] | {"width": None, "height": None} == (
        layout["nodes"][0] | {"width": None, "height": None}
    )
    assert payload["layout"]["viewport"] == layout["viewport"]
    assert path.read_text(encoding="utf-8") == source


def test_project_summary_model_tracks_saved_provider(tmp_path: Path) -> None:
    client = _client(tmp_path)
    document = _create(client)
    saved = client.put(
        "/api/v1/projects/demo/blueprint",
        json={"yaml": VALID_DEMO, "baseDigest": document["digest"]},
    )
    assert saved.status_code == 200, saved.text

    summary = client.get("/api/v1/projects").json()["projects"][0]

    assert summary["model"] == "gpt-5"


def test_project_summary_updated_at_advances_after_save(tmp_path: Path) -> None:
    client = _client(tmp_path)
    document = _create(client)
    before = client.get("/api/v1/projects").json()["projects"][0]["updatedAt"]

    time.sleep(0.01)
    client.put(
        "/api/v1/projects/demo/blueprint",
        json={"yaml": VALID_DEMO, "baseDigest": document["digest"]},
    )

    after = client.get("/api/v1/projects").json()["projects"][0]["updatedAt"]
    assert after >= before


def test_proposal_semantic_diff_is_typed(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=TitleAuthoringService())
    _create(client)

    created = client.post("/api/v1/projects/demo/proposals", json={"instruction": "retitle"})

    assert created.status_code == 201, created.text
    entry = created.json()["semanticDiff"][0]
    assert set(entry) <= {"operation", "path", "before", "after"}
    assert entry["operation"] in {"add", "remove", "replace"}
    assert entry["path"].startswith("/")


def test_catalog_is_typed_in_openapi_schema(tmp_path: Path) -> None:
    app = create_app(tmp_path / "workspace")

    schema = app.openapi()
    responses = schema["paths"]["/api/v1/catalog"]["get"]["responses"]
    content = responses["200"]["content"]["application/json"]["schema"]

    assert "$ref" in content or content.get("type") == "object" and "properties" in content


def test_catalog_response_still_matches_catalog_document(tmp_path: Path) -> None:
    from linch_studio.catalog import catalog_document

    client = _client(tmp_path)

    assert client.get("/api/v1/catalog").json() == catalog_document()


class ConversationalAuthoringService:
    """Protocol fake honouring the staged protocol: ask → plan → build."""

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        raise AssertionError("turn tests must use converse")

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del on_thinking, on_tool_call
        assert len(base_digest) == 64
        transcript = list(messages)  # type: ignore[call-overload]
        if stage == "build":
            metadata = current.metadata.model_copy(update={"title": "Conversed Candidate"})
            return TurnDraft(
                kind="proposal",
                note="delivering the approved plan",
                thinking="the nightly cadence settles it",
                proposal=ProposalDraft(
                    candidate=current.model_copy(update={"metadata": metadata}),
                    semantic_diff=({"operation": "remove", "path": "/ignored"},),
                    summary="one agent with a nightly routine",
                ),
            )
        if not any(item.role == "assistant" for item in transcript):
            return TurnDraft(
                kind="questions",
                questions=(
                    AuthoringQuestion(
                        question="Which provider should run it?",
                        options=["openai", "anthropic", "a local model"],
                    ),
                ),
                note="plan: one reviewed agent",
                thinking="considered three designs",
            )
        return TurnDraft(
            kind="plan",
            plan="1. one agent\n2. nightly cron routine",
            note="approve to build",
        )


class UnsafeConversationalService:
    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del base_digest, messages, stage, on_thinking, on_tool_call
        permissions = current.spec.capabilities.permissions.model_copy(update={"mode": "trusted"})
        capabilities = current.spec.capabilities.model_copy(update={"permissions": permissions})
        spec = current.spec.model_copy(update={"capabilities": capabilities})
        return TurnDraft(
            kind="proposal",
            proposal=ProposalDraft(candidate=current.model_copy(update={"spec": spec})),
        )


class StreamingConversationalService:
    """Pushes thinking deltas through the callback before answering."""

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_tool_call
        assert callable(on_thinking)
        on_thinking("considering three ")
        on_thinking("designs")
        return TurnDraft(
            kind="questions",
            questions=(
                AuthoringQuestion(
                    question="Which provider should run it?",
                    options=["openai", "anthropic"],
                ),
            ),
            note="plan: one reviewed agent",
            thinking="considering three designs",
        )


class StreamingFailureService:
    """Streams one delta, then fails with a typed, diagnosable service error."""

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_tool_call
        assert callable(on_thinking)
        on_thinking("half a thought")
        raise MalformedProposalError(
            "the model returned an invalid turn shape",
            diagnostics=(
                Diagnostic(
                    code="authoring.malformed_proposal",
                    severity="error",
                    path="/",
                    message="The turn did not match the required output contract.",
                    remediation="Simplify the request or try again.",
                ),
            ),
        )


def _stream_events(body: str) -> list[tuple[str, dict[str, object]]]:
    import json

    events: list[tuple[str, dict[str, object]]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((event, json.loads(data)))
    return events


def _stream_turn(app: object, body: dict[str, object]) -> tuple[int, str, str]:
    async def scenario() -> tuple[int, str, str]:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post("/api/v1/projects", json={"id": "demo", "title": "Demo"})
            assert created.status_code == 201, created.text
            async with client.stream("POST", TURNS_PATH + "/stream", json=body) as response:
                chunks = [chunk async for chunk in response.aiter_text()]
                return (
                    response.status_code,
                    response.headers.get("content-type", ""),
                    "".join(chunks),
                )

    return asyncio.run(scenario())


def test_streaming_turn_emits_thinking_deltas_then_the_final_turn(tmp_path: Path) -> None:
    app = create_app(tmp_path / "workspace", authoring_service=StreamingConversationalService())  # type: ignore[arg-type]

    status, content_type, body = _stream_turn(
        app, {"messages": [{"role": "user", "content": "build me a review workflow"}]}
    )

    assert status == 200
    assert content_type.startswith("text/event-stream")
    events = _stream_events(body)
    assert [kind for kind, _ in events] == ["thinking", "thinking", "turn"]
    assert events[0][1] == {"text": "considering three "}
    assert events[1][1] == {"text": "designs"}
    turn = events[2][1]
    assert turn["kind"] == "questions"
    assert turn["questions"] == [  # type: ignore[comparison-overlap]
        {"question": "Which provider should run it?", "options": ["openai", "anthropic"]}
    ]
    assert turn["thinking"] == "considering three designs"


def test_streaming_turn_failure_ends_with_a_value_safe_error_event(tmp_path: Path) -> None:
    app = create_app(tmp_path / "workspace", authoring_service=StreamingFailureService())  # type: ignore[arg-type]

    status, content_type, body = _stream_turn(
        app, {"messages": [{"role": "user", "content": "build something"}]}
    )

    assert status == 200
    assert content_type.startswith("text/event-stream")
    events = _stream_events(body)
    assert [kind for kind, _ in events] == ["thinking", "error"]
    payload = events[1][1]
    assert payload["status"] == 502
    error = payload["error"]
    assert error["code"] == "authoring.failed"  # type: ignore[index]
    assert [item["code"] for item in error["diagnostics"]] == [  # type: ignore[index]
        "authoring.malformed_proposal"
    ]
    assert "invalid turn shape" not in body


class ExhaustedConversationalService:
    """Raises the typed service failure whose detail the wire must not swallow."""

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_thinking, on_tool_call
        raise MalformedProposalError(
            "the model kept returning an invalid turn shape",
            telemetry=ProposalTelemetry(
                provider="openai_chat",
                model="live-model",
                duration_ms=120_000,
                input_tokens=90_000,
                output_tokens=8_000,
                cache_read_tokens=60_000,
                cache_creation_tokens=0,
                status="malformed",
            ),
            diagnostics=(
                Diagnostic(
                    code="authoring.malformed_proposal",
                    severity="error",
                    path="/",
                    message="The turn did not match the required output contract.",
                    remediation="Simplify the request or try again.",
                ),
            ),
        )


class CrashingConversationalService:
    """Raises an untyped-cause failure, as an unexpected SDK/provider crash would."""

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_thinking, on_tool_call
        raise ProposalGenerationError(
            "the authoring model could not produce a proposal",
            telemetry=ProposalTelemetry(
                provider="openai_chat",
                model="live-model",
                duration_ms=85_000,
                input_tokens=459_000,
                output_tokens=7_700,
                cache_read_tokens=444_000,
                cache_creation_tokens=0,
                status="runtime_error",
            ),
            cause="RuntimeError",
        )


class WorkflowAuthoringService:
    """Legacy-shaped fake whose candidate introduces a two-node workflow."""

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        del base_digest, instruction
        data = current.model_dump(mode="json", by_alias=True)
        data["spec"]["workflows"] = [
            {
                "id": "review",
                "displayName": "Review",
                "nodes": [
                    {
                        "type": "agent_call",
                        "id": "collect",
                        "label": "Collect",
                        "prompt": "Collect evidence.",
                    },
                    {
                        "type": "agent_call",
                        "id": "summarize",
                        "label": "Summarize",
                        "prompt": "Summarize the evidence.",
                        "dependsOn": ["collect"],
                    },
                ],
                "output": "summarize",
            }
        ]
        return ProposalDraft(candidate=Blueprint.model_validate(data))


TURNS_PATH = "/api/v1/projects/demo/authoring/turns"


def test_authoring_turn_asks_questions_without_persisting_anything(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=ConversationalAuthoringService())
    _create(client)

    response = client.post(
        TURNS_PATH,
        json={"messages": [{"role": "user", "content": "build me a review workflow"}]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "kind": "questions",
        "questions": [
            {
                "question": "Which provider should run it?",
                "options": ["openai", "anthropic", "a local model"],
            }
        ],
        "plan": None,
        "planNote": "plan: one reviewed agent",
        "thinking": "considered three designs",
        "proposal": None,
        "toolCalls": [],
    }
    assert client.get("/api/v1/projects/demo/proposals").json() == {"proposals": []}


def test_authoring_turn_presents_a_plan_before_any_build(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=ConversationalAuthoringService())
    _create(client)

    response = client.post(
        TURNS_PATH,
        json={
            "messages": [
                {"role": "user", "content": "build me a review workflow"},
                {"role": "assistant", "content": "Which provider should run it?"},
                {"role": "user", "content": "Which provider should run it? → openai"},
            ]
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "plan"
    assert payload["plan"] == "1. one agent\n2. nightly cron routine"
    assert payload["planNote"] == "approve to build"
    assert payload["proposal"] is None
    assert client.get("/api/v1/projects/demo/proposals").json() == {"proposals": []}


def test_authoring_turn_proposal_recomputes_diff_and_persists_summary(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=ConversationalAuthoringService())
    _create(client)

    response = client.post(
        TURNS_PATH,
        json={
            "messages": [
                {"role": "user", "content": "build me a review workflow"},
                {"role": "assistant", "content": "plan: one agent + nightly cron"},
                {"role": "user", "content": "Proceed with this plan."},
            ],
            "stage": "build",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "proposal"
    assert payload["questions"] == []
    assert payload["planNote"] == "delivering the approved plan"
    assert payload["thinking"] == "the nightly cadence settles it"
    proposal = payload["proposal"]
    assert proposal["summary"] == "one agent with a nightly routine"
    # The server ignores the fake's junk diff and recomputes the real one.
    assert [(item["operation"], item["path"]) for item in proposal["semanticDiff"]] == [
        ("replace", "/metadata/title")
    ]
    listed = client.get("/api/v1/projects/demo/proposals").json()["proposals"]
    assert [item["id"] for item in listed] == [proposal["id"]]
    assert listed[0]["summary"] == "one agent with a nightly routine"


class ToolCallConversationalService:
    """Carries a completed tool-call trace on the returned draft."""

    async def propose(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        instruction: str,
    ) -> ProposalDraft:
        raise AssertionError("turn tests must use converse")

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_thinking, on_tool_call
        return TurnDraft(
            kind="questions",
            questions=(
                AuthoringQuestion(
                    question="Which provider should run it?",
                    options=["openai", "anthropic", "a local model"],
                ),
            ),
            note="plan: one reviewed agent",
            tool_calls=(
                ToolCallRecord(
                    tool_use_id="call_1",
                    tool_name="search_docs",
                    summary="search_docs: workflow fan-out",
                    result_summary="3 results",
                    detail="usage/workflows.md#fan-out — Fan-out: ...",
                    is_error=False,
                    duration_ms=42,
                ),
            ),
        )


def test_authoring_turn_carries_the_tool_call_trace(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=ToolCallConversationalService())
    _create(client)

    response = client.post(
        TURNS_PATH,
        json={"messages": [{"role": "user", "content": "build me a review workflow"}]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["toolCalls"] == [
        {
            "toolUseId": "call_1",
            "toolName": "search_docs",
            "summary": "search_docs: workflow fan-out",
            "resultSummary": "3 results",
            "detail": "usage/workflows.md#fan-out — Fan-out: ...",
            "isError": False,
            "durationMs": 42,
        }
    ]


class StreamingToolCallService:
    """Streams a start/end tool-call notice through the callback before answering."""

    async def converse(
        self,
        *,
        current: Blueprint,
        base_digest: str,
        messages: object,
        stage: str = "chat",
        on_thinking: object = None,
        on_tool_call: object = None,
    ) -> TurnDraft:
        del current, base_digest, messages, stage, on_thinking
        assert callable(on_tool_call)
        on_tool_call(
            ToolCallUpdate(
                phase="start",
                tool_use_id="call_1",
                tool_name="search_docs",
                summary="search_docs: fan-out",
            )
        )
        record = ToolCallRecord(
            tool_use_id="call_1",
            tool_name="search_docs",
            summary="search_docs: fan-out",
            result_summary="3 results",
            detail="full section text",
            is_error=False,
            duration_ms=42,
        )
        on_tool_call(
            ToolCallUpdate(
                phase="end",
                tool_use_id="call_1",
                tool_name="search_docs",
                summary="3 results",
                detail="full section text",
                is_error=False,
                duration_ms=42,
            )
        )
        return TurnDraft(
            kind="questions",
            questions=(
                AuthoringQuestion(
                    question="Which provider should run it?",
                    options=["openai", "anthropic"],
                ),
            ),
            tool_calls=(record,),
        )


def test_streaming_turn_emits_tool_call_start_and_end_before_the_final_turn(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "workspace", authoring_service=StreamingToolCallService())  # type: ignore[arg-type]

    status, content_type, body = _stream_turn(
        app, {"messages": [{"role": "user", "content": "build me a review workflow"}]}
    )

    assert status == 200
    assert content_type.startswith("text/event-stream")
    events = _stream_events(body)
    assert [kind for kind, _ in events] == ["tool_call_start", "tool_call_end", "turn"]
    assert events[0][1] == {
        "toolUseId": "call_1",
        "toolName": "search_docs",
        "summary": "search_docs: fan-out",
        "detail": None,
        "isError": False,
        "durationMs": 0,
    }
    assert events[1][1] == {
        "toolUseId": "call_1",
        "toolName": "search_docs",
        "summary": "3 results",
        "detail": "full section text",
        "isError": False,
        "durationMs": 42,
    }
    turn = events[2][1]
    assert turn["toolCalls"] == [
        {
            "toolUseId": "call_1",
            "toolName": "search_docs",
            "summary": "search_docs: fan-out",
            "resultSummary": "3 results",
            "detail": "full section text",
            "isError": False,
            "durationMs": 42,
        }
    ]


def test_authoring_turn_transcript_caps_are_rejected_at_the_wire(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=ConversationalAuthoringService())
    _create(client)
    user = {"role": "user", "content": "x"}
    secret_content = "sk-very-secret-" + "y" * 32_760

    for body in (
        {"messages": []},
        {"messages": [user] * 25},
        {"messages": [{"role": "user", "content": secret_content}]},
        {"messages": [{"role": "user", "content": "y" * 32_000}] * 5},
        {"messages": [user, {"role": "assistant", "content": "hello"}]},
    ):
        response = client.post(TURNS_PATH, json=body)
        assert response.status_code == 422, (body.get("messages") or [{}])[0].get("role")
        assert "sk-very-secret" not in response.text


def test_authoring_turn_unavailable_and_manual_only_guard(tmp_path: Path) -> None:
    single_user = {"messages": [{"role": "user", "content": "build something"}]}
    followup = {
        "messages": [
            {"role": "user", "content": "build something"},
            {"role": "assistant", "content": "which?"},
            {"role": "user", "content": "that one"},
        ]
    }

    client = _client(tmp_path)
    _create(client)
    unavailable = client.post(TURNS_PATH, json=single_user)
    assert unavailable.status_code == 501
    assert unavailable.json()["error"]["code"] == "authoring.unavailable"
    # The streaming variant refuses before emitting any stream bytes.
    unavailable_stream = client.post(TURNS_PATH + "/stream", json=single_user)
    assert unavailable_stream.status_code == 501
    assert unavailable_stream.json()["error"]["code"] == "authoring.unavailable"

    guarded = _client(tmp_path / "guarded", authoring_service=UnsafeConversationalService())
    _create(guarded)
    rejected = guarded.post(TURNS_PATH, json={**followup, "stage": "build"})
    assert rejected.status_code == 502
    assert rejected.json()["error"]["diagnostics"][0]["code"] == "authoring.manual_only_field"
    assert guarded.get("/api/v1/projects/demo/proposals").json() == {"proposals": []}


def test_authoring_turn_failure_surfaces_diagnostics_and_logs_aggregates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(tmp_path, authoring_service=ExhaustedConversationalService())
    _create(client)

    with caplog.at_level(logging.WARNING, logger="linch_studio.server"):
        response = client.post(
            TURNS_PATH,
            json={"messages": [{"role": "user", "content": "build something"}]},
        )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "authoring.failed"
    assert [item["code"] for item in error["diagnostics"]] == ["authoring.malformed_proposal"]
    record = next(
        item for item in caplog.records if "authoring.malformed_proposal" in item.getMessage()
    )
    logged = record.getMessage()
    # Aggregate-only: the log names the failure class and token counts, never
    # prompt text or the provider's free-form error message.
    assert "in=90000" in logged
    assert "status=malformed" in logged
    assert "cause=-" in logged
    assert "invalid turn shape" not in caplog.text


def test_authoring_turn_crash_logs_the_exception_class_as_a_diagnostic_lead(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(tmp_path, authoring_service=CrashingConversationalService())
    _create(client)

    with caplog.at_level(logging.WARNING, logger="linch_studio.server"):
        response = client.post(
            TURNS_PATH,
            json={"messages": [{"role": "user", "content": "build something"}]},
        )

    assert response.status_code == 502
    record = next(
        item for item in caplog.records if "authoring.generation_failed" in item.getMessage()
    )
    logged = record.getMessage()
    assert "cause=RuntimeError" in logged


def test_proposal_raw_crash_logs_the_exception_class_without_leaking_its_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(tmp_path, authoring_service=RawCrashAuthoringService())
    _create(client)

    with caplog.at_level(logging.WARNING, logger="linch_studio.server"):
        response = client.post(
            "/api/v1/projects/demo/proposals",
            json={"instruction": "add a greeting tool"},
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "authoring.failed"
    record = next(item for item in caplog.records if "unexpected RuntimeError" in item.getMessage())
    assert "authoring failed" in record.getMessage()
    assert "boom" not in caplog.text


def test_proposal_instruction_cap_matches_the_service_cap(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=TitleAuthoringService())
    _create(client)

    response = client.post(
        "/api/v1/projects/demo/proposals",
        json={"instruction": "x" * 32_769},
    )

    assert response.status_code == 422, response.text


def test_accepting_a_workflow_proposal_places_new_nodes_on_the_canvas(tmp_path: Path) -> None:
    client = _client(tmp_path, authoring_service=WorkflowAuthoringService())
    _create(client)
    created = client.post(
        "/api/v1/projects/demo/proposals",
        json={"instruction": "add a review workflow"},
    )
    assert created.status_code == 201, created.text
    proposal = created.json()

    accepted = client.post(f"/api/v1/projects/demo/proposals/{proposal['id']}/accept")

    assert accepted.status_code == 200, accepted.text
    document = accepted.json()
    assert document["digest"] == proposal["candidateDigest"]
    placed = {node["id"]: node for node in document["layout"]["nodes"]}
    assert placed["wf_review_collect"]["x"] == 0.0
    assert placed["wf_review_collect"]["y"] == 0.0
    assert placed["wf_review_summarize"]["x"] == 280.0
    assert placed["wf_review_summarize"]["y"] == 0.0
    # The layout write is a separate channel: reloading the project keeps both
    # the accepted digest and the placed nodes.
    reloaded = client.get("/api/v1/projects/demo").json()
    assert reloaded["digest"] == proposal["candidateDigest"]
    assert {node["id"] for node in reloaded["layout"]["nodes"]} >= {
        "wf_review_collect",
        "wf_review_summarize",
    }


def test_turn_endpoint_end_to_end_with_scripted_provider(tmp_path: Path) -> None:
    import json

    from linch import ScriptedProvider, TextTurn

    from linch_studio.authoring import AuthoringConfig, LinchProposalService
    from linch_studio.server import LinchAuthoringService

    turn_json = json.dumps(
        {
            "questions": [
                {
                    "question": "Which trigger should start the run?",
                    "options": ["manual", "cron", "webhook"],
                }
            ],
            "plan": None,
            "note": "plan: agent with one routine",
            "blueprint": None,
            "summary": None,
        }
    )
    service = LinchProposalService(
        AuthoringConfig(
            provider="openai_responses",
            model="gpt-5",
            api_key="offline-test-key",
            timeout_seconds=10.0,
        ),
        record_proposals=False,
        provider_factory=lambda _: ScriptedProvider([TextTurn(turn_json)]),
    )
    client = _client(tmp_path, authoring_service=LinchAuthoringService(service))
    _create(client)

    response = client.post(
        TURNS_PATH,
        json={"messages": [{"role": "user", "content": "build a nightly reviewer"}]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "questions"
    assert payload["questions"] == [
        {
            "question": "Which trigger should start the run?",
            "options": ["manual", "cron", "webhook"],
        }
    ]
    assert payload["planNote"] == "plan: agent with one routine"
    assert payload["thinking"] is None
