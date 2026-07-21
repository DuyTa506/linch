"""Global Support routes are project-optional and preserve pipeline confirmation gates."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx
import pytest

from linch_studio.server import create_app
from linch_studio.support import Evidence, SupportTurn


class DocsSupport:
    async def turn(self, *, messages, mode, current_blueprint=None, on_tool_call=None):
        del messages, current_blueprint, on_tool_call
        return SupportTurn(
            kind="answer",
            mode=mode,
            answer="Use the documented workflow API.",
            evidence=[
                Evidence(
                    anchor="usage/workflows.md#run_workflow-fn",
                    claim="run_workflow is documented.",
                )
            ],
            coverage="documented",
        )


class RawCrashSupport:
    """Raises a bare, untyped exception, as an unhandled bug would."""

    async def turn(self, *, messages, mode, current_blueprint=None, on_tool_call=None):
        del messages, mode, current_blueprint, on_tool_call
        raise RuntimeError("boom, a raw unhandled crash")


class Client:
    def __init__(self, app: object) -> None:
        self.app = app

    def post(self, path: str, payload: dict[str, object]) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)  # type: ignore[arg-type]
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(path, json=payload)

        return asyncio.run(send())

    def get(self, path: str) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)  # type: ignore[arg-type]
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path)

        return asyncio.run(send())


def test_support_answer_is_global_and_never_exposes_reasoning(tmp_path: Path) -> None:
    client = Client(create_app(tmp_path / "workspace", support_service=DocsSupport()))

    info = client.get("/api/v1")
    assert info.json()["supportAvailable"] is True
    response = client.post(
        "/api/v1/support/turns",
        {
            "messages": [{"role": "user", "content": "How do workflows work?"}],
            "requestedMode": "documentation",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "answer"
    assert body["evidence"][0]["anchor"] == "usage/workflows.md#run_workflow-fn"
    assert "thinking" not in body
    assert "toolCalls" in body and body["toolCalls"] == []


def test_ci_motif_requires_confirmation_then_creates_a_safe_draft(tmp_path: Path) -> None:
    client = Client(create_app(tmp_path / "workspace"))
    created = client.post("/api/v1/projects", {"id": "demo", "title": "Demo"})
    assert created.status_code == 201
    initial = (
        "Build a CI code review pipeline for a PR with security, performance, and style reviewers."
    )
    confirmation = client.post(
        "/api/v1/support/turns",
        {
            "messages": [{"role": "user", "content": initial}],
            "projectId": "demo",
            "requestedMode": "auto",
        },
    )
    assert confirmation.json()["kind"] == "mode_confirmation"

    plan = client.post(
        "/api/v1/support/turns",
        {
            "messages": [
                {"role": "user", "content": initial},
                {"role": "assistant", "content": confirmation.json()["answer"]},
                {"role": "user", "content": "Proceed with pipeline authoring."},
            ],
            "projectId": "demo",
            "requestedMode": "pipeline",
            "pipelineConfirmed": True,
        },
    )
    assert plan.json()["kind"] == "plan"
    assert len(plan.json()["planDigest"]) == 64

    plan_text = plan.json()["plan"]
    proposal = client.post(
        "/api/v1/support/turns",
        {
            "messages": [
                {"role": "user", "content": initial},
                {"role": "assistant", "content": confirmation.json()["answer"]},
                {"role": "user", "content": "Proceed with pipeline authoring."},
                {"role": "assistant", "content": plan_text},
                {"role": "user", "content": "Proceed with this plan."},
            ],
            "projectId": "demo",
            "requestedMode": "pipeline",
            "pipelineConfirmed": True,
            "stage": "build",
            "approvedPlanDigest": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
        },
    )
    assert proposal.status_code == 200
    body = proposal.json()
    assert body["kind"] == "proposal"
    nodes = body["proposal"]["candidate"]["spec"]["workflows"][-1]["nodes"]
    assert [node["id"] for node in nodes] == [
        "security_review",
        "performance_review",
        "style_review",
        "merge_reviews",
    ]
    assert body["proposal"]["exportReady"] is False  # Provider setup remains a safe draft gap.


def test_pipeline_gate_ignores_an_earlier_unrelated_documentation_question(tmp_path: Path) -> None:
    client = Client(create_app(tmp_path / "workspace"))
    assert client.post("/api/v1/projects", {"id": "demo", "title": "Demo"}).status_code == 201
    instruction = "Create a host cron multi-agent review workflow with a planner and reviewer."

    response = client.post(
        "/api/v1/support/turns",
        {
            "messages": [
                {"role": "user", "content": "What is a Linch skill?"},
                {"role": "assistant", "content": "A disk-loaded prompt fragment."},
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": "Please confirm pipeline authoring."},
                {"role": "user", "content": "Proceed with pipeline authoring."},
            ],
            "projectId": "demo",
            "requestedMode": "pipeline",
            "pipelineConfirmed": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "plan"
    assert "host-owned cron" in response.json()["plan"]


def test_support_turn_raw_crash_logs_the_exception_class_without_leaking_its_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = Client(create_app(tmp_path / "workspace", support_service=RawCrashSupport()))

    with caplog.at_level(logging.WARNING, logger="linch_studio.server"):
        response = client.post(
            "/api/v1/support/turns",
            {
                "messages": [{"role": "user", "content": "How do workflows work?"}],
                "requestedMode": "documentation",
            },
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "support.failed"
    record = next(item for item in caplog.records if "unexpected RuntimeError" in item.getMessage())
    assert "support failed" in record.getMessage()
    assert "boom" not in caplog.text
