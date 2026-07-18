"""FastAPI application for the local, file-backed Studio server."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue, ValidationError

from linch_studio.authoring import (
    AuthoringError,
    AuthoringMessage,
    SemanticDiffEntry,
    ToolCallUpdate,
    deterministic_layout_for_new_nodes,
)
from linch_studio.authoring.diff import semantic_diff
from linch_studio.authoring.guards import manual_only_diagnostics
from linch_studio.catalog import catalog_document
from linch_studio.compiler import (
    CompiledProject,
    CompilerError,
    ExportError,
    compile_blueprint,
    deterministic_zip_bytes,
    export_directory,
)
from linch_studio.spec import Blueprint, Diagnostic, blueprint_digest, has_errors, load_blueprint
from linch_studio.support import SupportMessage, SupportTurn, pipeline_intent, resolve_mode
from linch_studio.support.pipeline import (
    PipelineBuildError,
    build_candidate,
    detect_pipeline_motif,
    plan_for,
)

from .authoring import AuthoringService, ProposalDraft, TurnDraft, UnavailableAuthoringService
from .errors import (
    AuthoringFailed,
    AuthoringUnavailable,
    CorruptProject,
    ExportBlocked,
    ExportConflict,
    InvalidLayout,
    InvalidRequest,
    StudioServerError,
    SupportFailed,
    SupportUnavailable,
)
from .models import (
    CapabilityCatalogResponse,
    DirectoryExportRequest,
    DirectoryExportResponse,
    ErrorBody,
    ErrorEnvelope,
    ExportPreviewResponse,
    GeneratedFilePreview,
    LayoutDocument,
    LayoutResponse,
    MigrationWarningResponse,
    ProjectCreateRequest,
    ProjectDocument,
    ProjectListResponse,
    ProjectSummary,
    ProposalListResponse,
    ProposalRequest,
    ProposalResponse,
    SaveBlueprintRequest,
    ServiceInfo,
    SupportEvidence,
    SupportHandoff,
    SupportImplementationIntent,
    SupportPipelineIntent,
    SupportRecipe,
    SupportRecipeCommand,
    SupportRecipeFile,
    SupportRecipeTodo,
    SupportTurnRequest,
    SupportTurnResponse,
    TurnQuestion,
    TurnRequest,
    TurnResponse,
    TurnToolCall,
    ValidateBlueprintRequest,
    ValidationResponse,
)
from .store import FileProjectStore, ProjectSnapshot, StoredProposal
from .support import SupportService, UnavailableSupportService

_logger = logging.getLogger("linch_studio.server")


def _authoring_failure(error: AuthoringError) -> AuthoringFailed:
    """Log an aggregate-only trace of a failed turn and keep its diagnostics.

    Diagnostics and telemetry are value-free by construction; the provider's
    free-form exception message is deliberately not logged. ``error.cause``,
    when set, is only ever an exception class name (e.g. "TimeoutError"), never
    its message — safe to log, and the only lead an unexpected crash gets.
    """

    codes = ",".join(item.code for item in error.diagnostics) or "-"
    cause = error.cause or "-"
    telemetry = error.telemetry
    if telemetry is None:
        _logger.warning("authoring failed: %s diagnostics=[%s] cause=%s", error.code, codes, cause)
    else:
        _logger.warning(
            "authoring failed: %s diagnostics=[%s] cause=%s provider=%s model=%s status=%s "
            "duration_ms=%d tokens in=%d out=%d cache_read=%d cache_creation=%d",
            error.code,
            codes,
            cause,
            telemetry.provider,
            telemetry.model,
            telemetry.status,
            telemetry.duration_ms,
            telemetry.input_tokens,
            telemetry.output_tokens,
            telemetry.cache_read_tokens,
            telemetry.cache_creation_tokens,
        )
    return AuthoringFailed(diagnostics=error.diagnostics)


def _support_failure(error: AuthoringError) -> SupportFailed:
    """Log only closed-vocabulary support telemetry, never a user prompt or code."""

    telemetry = error.telemetry
    if telemetry is None:
        _logger.warning("support failed: %s", error.code)
    else:
        _logger.warning(
            "support failed: %s provider=%s model=%s status=%s duration_ms=%d",
            error.code,
            telemetry.provider,
            telemetry.model,
            telemetry.status,
            telemetry.duration_ms,
        )
    return SupportFailed(diagnostics=error.diagnostics)


def _sse_event(event: str, payload: dict[str, JsonValue]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _stream_error_payload(error: StudioServerError) -> dict[str, JsonValue]:
    """The same value-safe envelope the JSON handler renders, plus its status."""

    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=error.code,
            message=error.message,
            diagnostics=list(error.diagnostics),
            current_digest=error.current_digest,
        )
    )
    payload = envelope.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {"status": error.status_code, **payload}


def _tool_call_stream_payload(update: ToolCallUpdate) -> dict[str, JsonValue]:
    return {
        "toolUseId": update.tool_use_id,
        "toolName": update.tool_name,
        "summary": update.summary,
        "detail": update.detail,
        "isError": update.is_error,
        "durationMs": update.duration_ms,
    }


def create_app(
    workspace: str | Path,
    *,
    static_dir: str | Path | None = None,
    authoring_service: AuthoringService | None = None,
    support_service: SupportService | None = None,
) -> FastAPI:
    """Build the API application without choosing a host, port, or auth policy."""

    store = FileProjectStore(workspace)
    authoring = authoring_service or UnavailableAuthoringService()
    support = support_service or UnavailableSupportService()
    authoring_available = not isinstance(authoring, UnavailableAuthoringService)
    support_available = not isinstance(support, UnavailableSupportService)
    frontend = _static_root(static_dir)
    # Errors are rendered by the handlers below rather than by response_model, so
    # declare the envelope once here to keep it in the generated OpenAPI schema.
    app = FastAPI(
        title="Linch Studio",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        responses={"4XX": {"model": ErrorEnvelope}, "5XX": {"model": ErrorEnvelope}},
    )
    app.state.project_store = store
    app.state.authoring_service = authoring
    app.state.support_service = support

    @app.exception_handler(StudioServerError)
    async def studio_error_handler(
        request: Request,
        exc: StudioServerError,
    ) -> JSONResponse:
        del request
        envelope = ErrorEnvelope(
            error=ErrorBody(
                code=exc.code,
                message=exc.message,
                diagnostics=list(exc.diagnostics),
                current_digest=exc.current_digest,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(
                envelope.model_dump(mode="json", by_alias=True, exclude_none=True)
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default validation payload can contain the rejected input. Keep
        # the public diagnostic fixed and value-free instead.
        del exc
        if request.url.path.endswith("/layout"):
            error: StudioServerError = InvalidLayout()
        else:
            error = InvalidRequest(
                diagnostics=(
                    Diagnostic(
                        code="api.request_invalid",
                        severity="error",
                        path="/",
                        message="The request does not match the Studio API contract.",
                        remediation="Check required fields, types, and documented size limits.",
                    ),
                )
            )
        envelope = ErrorEnvelope(
            error=ErrorBody(
                code=error.code,
                message=error.message,
                diagnostics=list(error.diagnostics),
            )
        )
        return JSONResponse(
            status_code=error.status_code,
            content=jsonable_encoder(envelope.model_dump(mode="json", by_alias=True)),
        )

    @app.get("/api/v1", response_model=ServiceInfo)
    async def service_info() -> ServiceInfo:
        return ServiceInfo(
            authoring_available=authoring_available,
            support_available=support_available,
        )

    @app.get("/api/v1/catalog", response_model=CapabilityCatalogResponse)
    async def capability_catalog() -> dict[str, object]:
        return catalog_document()

    @app.get("/api/v1/schema")
    async def blueprint_schema() -> dict[str, object]:
        return Blueprint.model_json_schema(by_alias=True)

    @app.post("/api/v1/validate", response_model=ValidationResponse)
    async def validate_buffer(request: ValidateBlueprintRequest) -> ValidationResponse:
        result = load_blueprint(request.yaml)
        if result.blueprint is None:
            return ValidationResponse(
                structurally_valid=False,
                export_ready=False,
                diagnostics=list(result.diagnostics),
            )
        return ValidationResponse(
            structurally_valid=True,
            export_ready=not has_errors(result.diagnostics),
            digest=blueprint_digest(result.blueprint),
            diagnostics=list(result.diagnostics),
        )

    @app.get("/api/v1/projects", response_model=ProjectListResponse)
    async def list_projects() -> ProjectListResponse:
        snapshots = store.list_projects()
        return ProjectListResponse(projects=[_project_summary(snapshot) for snapshot in snapshots])

    @app.post(
        "/api/v1/projects",
        response_model=ProjectDocument,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(request: ProjectCreateRequest) -> ProjectDocument:
        def create_document() -> ProjectDocument:
            return _project_document(
                store,
                store.create_project(
                    request.project_id,
                    title=request.title,
                    template=request.template,
                ),
            )

        return create_document()

    @app.get("/api/v1/projects/{project_id}", response_model=ProjectDocument)
    async def open_project(project_id: str) -> ProjectDocument:
        return _project_document(store, store.open_project(project_id))

    @app.put("/api/v1/projects/{project_id}/blueprint", response_model=ProjectDocument)
    async def save_blueprint(project_id: str, request: SaveBlueprintRequest) -> ProjectDocument:
        def save_document() -> ProjectDocument:
            snapshot = store.save_blueprint(
                project_id,
                request.yaml,
                base_digest=request.base_digest,
            )
            return _project_document(store, snapshot)

        return save_document()

    @app.post("/api/v1/projects/{project_id}/validate", response_model=ValidationResponse)
    async def validate_project(project_id: str) -> ValidationResponse:
        snapshot = store.open_project(project_id)
        return ValidationResponse(
            structurally_valid=True,
            export_ready=snapshot.export_ready,
            digest=snapshot.digest,
            diagnostics=list(snapshot.diagnostics),
        )

    @app.get(
        "/api/v1/projects/{project_id}/layout",
        response_model=LayoutResponse,
        response_model_exclude_none=True,
    )
    async def load_layout(project_id: str) -> LayoutResponse:
        layout = store.load_layout(project_id)
        return LayoutResponse(
            id=project_id,
            layout=_validated_layout(layout),
        )

    @app.put(
        "/api/v1/projects/{project_id}/layout",
        response_model=LayoutResponse,
        response_model_exclude_none=True,
    )
    async def save_layout(project_id: str, layout: LayoutDocument) -> LayoutResponse:
        saved = store.save_layout(project_id, layout.model_dump(mode="json", by_alias=True))
        return LayoutResponse(id=project_id, layout=_validated_layout(saved))

    @app.post(
        "/api/v1/projects/{project_id}/export/preview",
        response_model=ExportPreviewResponse,
    )
    async def preview_export(project_id: str) -> ExportPreviewResponse:
        project = _compile_project(store.open_project(project_id))
        return ExportPreviewResponse(
            blueprint_digest=project.blueprint_digest,
            files=[GeneratedFilePreview.model_validate(item) for item in project.preview()],
        )

    @app.post("/api/v1/projects/{project_id}/export/zip")
    async def download_export(project_id: str) -> Response:
        project = _compile_project(store.open_project(project_id))
        archive = deterministic_zip_bytes(project)
        return Response(
            content=archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{project_id}.zip"',
                "X-Linch-Blueprint-Digest": project.blueprint_digest,
            },
        )

    @app.post(
        "/api/v1/projects/{project_id}/export/directory",
        response_model=DirectoryExportResponse,
    )
    async def write_export(
        project_id: str,
        request: DirectoryExportRequest,
    ) -> DirectoryExportResponse:
        try:
            project = _compile_project(store.open_project(project_id))
            target = export_directory(project, request.target)
        except (ExportError, OSError):
            raise ExportConflict from None
        return DirectoryExportResponse(
            target=str(target),
            blueprint_digest=project.blueprint_digest,
        )

    @app.get(
        "/api/v1/projects/{project_id}/proposals",
        response_model=ProposalListResponse,
    )
    async def list_proposals(project_id: str) -> ProposalListResponse:
        proposals = store.list_proposals(project_id)
        return ProposalListResponse(proposals=[_proposal_response(item) for item in proposals])

    @app.post(
        "/api/v1/projects/{project_id}/proposals",
        response_model=ProposalResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_proposal(
        project_id: str,
        request: ProposalRequest,
    ) -> ProposalResponse:
        current = store.open_project(project_id)
        try:
            draft = await authoring.propose(
                current=current.blueprint,
                base_digest=current.digest,
                instruction=request.instruction,
            )
        except StudioServerError:
            raise
        except AuthoringError as error:
            raise _authoring_failure(error) from None
        except Exception:
            raise AuthoringFailed from None
        if not isinstance(draft, ProposalDraft):
            raise AuthoringFailed
        return _proposal_response(_persist_draft(store, project_id, current, draft))

    async def _converse_turn(
        project_id: str,
        request: TurnRequest,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> TurnResponse:
        current = store.open_project(project_id)
        messages = [
            AuthoringMessage(role=item.role, content=item.content) for item in request.messages
        ]
        try:
            turn = await authoring.converse(
                current=current.blueprint,
                base_digest=current.digest,
                messages=messages,
                stage=request.stage,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
            )
        except StudioServerError:
            raise
        except AuthoringError as error:
            raise _authoring_failure(error) from None
        except Exception:
            raise AuthoringFailed from None
        if not isinstance(turn, TurnDraft):
            raise AuthoringFailed
        tool_calls = [
            TurnToolCall(
                tool_use_id=item.tool_use_id,
                tool_name=item.tool_name,
                summary=item.summary,
                result_summary=item.result_summary,
                detail=item.detail,
                is_error=item.is_error,
                duration_ms=item.duration_ms,
            )
            for item in turn.tool_calls
        ]
        if turn.kind == "questions":
            return TurnResponse(
                kind="questions",
                questions=[
                    TurnQuestion(question=item.question, options=list(item.options))
                    for item in turn.questions
                ],
                plan_note=turn.note,
                thinking=turn.thinking,
                tool_calls=tool_calls,
            )
        if turn.kind == "plan":
            return TurnResponse(
                kind="plan",
                plan=turn.plan,
                plan_note=turn.note,
                thinking=turn.thinking,
                tool_calls=tool_calls,
            )
        if turn.proposal is None:
            raise AuthoringFailed
        proposal = _persist_draft(store, project_id, current, turn.proposal)
        return TurnResponse(
            kind="proposal",
            plan_note=turn.note,
            proposal=_proposal_response(proposal),
            thinking=turn.thinking,
            tool_calls=tool_calls,
        )

    @app.post(
        "/api/v1/projects/{project_id}/authoring/turns",
        response_model=TurnResponse,
    )
    async def create_authoring_turn(
        project_id: str,
        request: TurnRequest,
    ) -> TurnResponse:
        return await _converse_turn(project_id, request)

    @app.post(
        "/api/v1/projects/{project_id}/authoring/turns/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "Server-sent events: `thinking` deltas while the model reasons, "
                    "`tool_call_start`/`tool_call_end` around each knowledge-tool call, "
                    "then exactly one terminal `turn` (a TurnResponse) or `error` event."
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
    )
    async def stream_authoring_turn(
        project_id: str,
        request: TurnRequest,
    ) -> StreamingResponse:
        # Configuration and existence problems surface as plain HTTP errors
        # before any stream bytes; only the running turn reports through events.
        if not authoring_available:
            raise AuthoringUnavailable
        store.open_project(project_id)
        queue: asyncio.Queue[tuple[str, dict[str, JsonValue]]] = asyncio.Queue()
        TERMINAL_KINDS = ("turn", "error")

        async def worker() -> None:
            try:
                response = await _converse_turn(
                    project_id,
                    request,
                    on_thinking=lambda text: queue.put_nowait(("thinking", {"text": text})),
                    on_tool_call=lambda update: queue.put_nowait(
                        (f"tool_call_{update.phase}", _tool_call_stream_payload(update))
                    ),
                )
                queue.put_nowait(("turn", response.model_dump(mode="json", by_alias=True)))
            except StudioServerError as error:
                queue.put_nowait(("error", _stream_error_payload(error)))
            except Exception:
                queue.put_nowait(("error", _stream_error_payload(AuthoringFailed())))

        async def events() -> AsyncIterator[str]:
            task = asyncio.create_task(worker())
            try:
                while True:
                    kind, payload = await queue.get()
                    yield _sse_event(kind, payload)
                    if kind in TERMINAL_KINDS:
                        break
            finally:
                # A closed connection cancels the underlying agent run.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(events(), media_type="text/event-stream")

    def _support_recipe_response(recipe: Any) -> SupportRecipe:
        return SupportRecipe(
            title=recipe.title,
            overview=recipe.overview,
            intent=SupportImplementationIntent(
                summary=recipe.intent.summary,
                capabilities=list(recipe.intent.capabilities),
                schedule=recipe.intent.schedule,
                workflow_shape=recipe.intent.workflow_shape,
                constraints=list(recipe.intent.constraints),
            ),
            files=[
                SupportRecipeFile(
                    path=item.path,
                    language=item.language,
                    content=item.content,
                    provenance=item.provenance,
                    evidence=list(item.evidence),
                    explanation=item.explanation,
                )
                for item in recipe.files
            ],
            tests=[
                SupportRecipeFile(
                    path=item.path,
                    language=item.language,
                    content=item.content,
                    provenance=item.provenance,
                    evidence=list(item.evidence),
                    explanation=item.explanation,
                )
                for item in recipe.tests
            ],
            handoff=SupportHandoff(
                start_here=list(recipe.handoff.start_here),
                environment=list(recipe.handoff.environment),
                commands=[
                    SupportRecipeCommand(command=item.command, purpose=item.purpose)
                    for item in recipe.handoff.commands
                ],
                todos=[
                    SupportRecipeTodo(
                        description=item.description,
                        blocking=item.blocking,
                        owner=item.owner,
                    )
                    for item in recipe.handoff.todos
                ],
            ),
        )

    def _support_response(turn: SupportTurn) -> SupportTurnResponse:
        return SupportTurnResponse(
            kind=turn.kind,
            mode=turn.mode,
            answer=turn.answer,
            recipe=_support_recipe_response(turn.recipe) if turn.recipe is not None else None,
            pipeline_intent=(
                SupportPipelineIntent(
                    summary=turn.pipeline_intent.summary,
                    proposed_components=list(turn.pipeline_intent.proposed_components),
                    requires_project=turn.pipeline_intent.requires_project,
                )
                if turn.pipeline_intent is not None
                else None
            ),
            evidence=[
                SupportEvidence(anchor=item.anchor, claim=item.claim, excerpt=item.excerpt)
                for item in turn.evidence
            ],
            coverage=turn.coverage,
            follow_up=turn.follow_up,
        )

    def _pipeline_instruction(request: SupportTurnRequest) -> str:
        """Retain the substantive user requirements across a gated conversation.

        A confirmed pipeline turn ends with a deliberately generic message such as
        ``Proceed with pipeline authoring.``.  Selecting the first user message
        (the old behavior) was also wrong once a developer had asked an unrelated
        documentation question before describing the pipeline.  Keep the user
        supplied requirements, while stripping only the UI's fixed action labels.
        """

        action_labels = {
            "proceed with pipeline authoring.",
            "turn this implementation into a reviewable studio pipeline.",
            "proceed with this plan.",
        }
        requirements = [
            item.content
            for item in request.messages
            if item.role == "user" and item.content.strip().casefold() not in action_labels
        ]
        return "\n\n".join(requirements) if requirements else request.messages[-1].content

    async def _support_turn(request: SupportTurnRequest) -> SupportTurnResponse:
        instruction = _pipeline_instruction(request)
        mode = resolve_mode(request.requested_mode, instruction)
        current = store.open_project(request.project_id) if request.project_id else None

        if mode == "pipeline":
            intent = pipeline_intent(instruction)
            motif = detect_pipeline_motif(instruction)
            if not request.pipeline_confirmed:
                return SupportTurnResponse(
                    kind="mode_confirmation",
                    mode="pipeline",
                    answer=(
                        "This looks like a Studio pipeline request. Confirm before I enter the "
                        "reviewable ask → plan → build flow; no project will change until a "
                        "proposal is explicitly accepted."
                    ),
                    pipeline_intent=SupportPipelineIntent(
                        summary=intent.summary,
                        proposed_components=list(intent.proposed_components),
                        requires_project=intent.requires_project,
                    ),
                    coverage="partial",
                )
            if current is None:
                return SupportTurnResponse(
                    kind="mode_confirmation",
                    mode="pipeline",
                    answer="Open or create a Studio project before planning this pipeline.",
                    pipeline_intent=SupportPipelineIntent(
                        summary=intent.summary,
                        proposed_components=list(intent.proposed_components),
                        requires_project=True,
                    ),
                    coverage="partial",
                    follow_up="Choose a project, then confirm again to start the reviewable plan.",
                )
            if motif is not None:
                plan = plan_for(motif)
                if request.stage == "chat":
                    return SupportTurnResponse(
                        kind="plan",
                        mode="pipeline",
                        plan=plan.text,
                        plan_digest=hashlib.sha256(plan.text.encode("utf-8")).hexdigest(),
                        pipeline_intent=SupportPipelineIntent(
                            summary=intent.summary,
                            proposed_components=list(intent.proposed_components),
                            requires_project=True,
                        ),
                    )
                prior_plan = next(
                    (
                        item.content
                        for item in reversed(request.messages)
                        if item.role == "assistant"
                    ),
                    None,
                )
                expected = (
                    hashlib.sha256(plan.text.encode("utf-8")).hexdigest()
                    if prior_plan == plan.text
                    else None
                )
                if request.approved_plan_digest is None or request.approved_plan_digest != expected:
                    raise InvalidRequest
                try:
                    candidate = build_candidate(current.blueprint, motif)
                except PipelineBuildError as error:
                    raise AuthoringFailed(diagnostics=error.diagnostics) from None
                motif_label = {
                    "ci_review": "CI review",
                    "scheduled_team": "host-cron team",
                    "release_readiness": "release-readiness review",
                }[motif]
                draft = ProposalDraft(
                    candidate=candidate,
                    summary=(
                        "Created a deterministic "
                        + motif_label
                        + " Blueprint draft. Review configuration and handoff TODOs before use."
                    ),
                )
                proposal = _persist_draft(store, request.project_id or "", current, draft)
                return SupportTurnResponse(
                    kind="proposal",
                    mode="pipeline",
                    proposal=_proposal_response(proposal),
                )
            if not authoring_available:
                raise AuthoringUnavailable
            if request.stage == "build":
                prior_plan = next(
                    (
                        item.content
                        for item in reversed(request.messages)
                        if item.role == "assistant"
                    ),
                    None,
                )
                expected = (
                    hashlib.sha256(prior_plan.encode("utf-8")).hexdigest()
                    if prior_plan is not None
                    else None
                )
                if request.approved_plan_digest is None or request.approved_plan_digest != expected:
                    raise InvalidRequest
            messages = [
                AuthoringMessage(role=item.role, content=item.content) for item in request.messages
            ]
            try:
                authored = await authoring.converse(
                    current=current.blueprint,
                    base_digest=current.digest,
                    messages=messages,
                    stage=request.stage,
                )
            except StudioServerError:
                raise
            except AuthoringError as error:
                raise _authoring_failure(error) from None
            except Exception:
                raise AuthoringFailed from None
            if not isinstance(authored, TurnDraft):
                raise AuthoringFailed
            if authored.kind == "questions":
                return SupportTurnResponse(
                    kind="questions",
                    mode="pipeline",
                    questions=[
                        TurnQuestion(question=item.question, options=list(item.options))
                        for item in authored.questions
                    ],
                )
            if authored.kind == "plan":
                plan = authored.plan or ""
                return SupportTurnResponse(
                    kind="plan",
                    mode="pipeline",
                    plan=plan,
                    plan_digest=hashlib.sha256(plan.encode("utf-8")).hexdigest(),
                )
            if authored.proposal is None:
                raise AuthoringFailed
            proposal = _persist_draft(store, request.project_id or "", current, authored.proposal)
            return SupportTurnResponse(
                kind="proposal",
                mode="pipeline",
                proposal=_proposal_response(proposal),
            )

        if not support_available:
            raise SupportUnavailable
        messages = [
            SupportMessage(role=item.role, content=item.content) for item in request.messages
        ]
        try:
            turn = await support.turn(
                messages=messages,
                mode=mode,
                current_blueprint=current.blueprint if current is not None else None,
            )
        except StudioServerError:
            raise
        except AuthoringError as error:
            raise _support_failure(error) from None
        except Exception:
            raise SupportFailed from None
        if not isinstance(turn, SupportTurn):
            raise SupportFailed
        return _support_response(turn)

    @app.post("/api/v1/support/turns", response_model=SupportTurnResponse)
    async def create_support_turn(request: SupportTurnRequest) -> SupportTurnResponse:
        return await _support_turn(request)

    @app.post(
        "/api/v1/support/turns/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "One terminal support `turn` or `error` event; reasoning is never streamed."
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
    )
    async def stream_support_turn(request: SupportTurnRequest) -> StreamingResponse:
        queue: asyncio.Queue[tuple[str, dict[str, JsonValue]]] = asyncio.Queue()

        async def worker() -> None:
            try:
                response = await _support_turn(request)
                queue.put_nowait(("turn", response.model_dump(mode="json", by_alias=True)))
            except StudioServerError as error:
                queue.put_nowait(("error", _stream_error_payload(error)))
            except Exception:
                queue.put_nowait(("error", _stream_error_payload(SupportFailed())))

        async def events() -> AsyncIterator[str]:
            task = asyncio.create_task(worker())
            try:
                kind, payload = await queue.get()
                yield _sse_event(kind, payload)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post(
        "/api/v1/projects/{project_id}/proposals/{proposal_id}/accept",
        response_model=ProjectDocument,
    )
    async def accept_proposal(project_id: str, proposal_id: str) -> ProjectDocument:
        current = store.open_project(project_id)
        proposal = store.get_proposal(project_id, proposal_id)
        snapshot = store.accept_proposal(project_id, proposal_id)
        _place_new_workflow_nodes(store, project_id, current.blueprint, proposal.candidate)
        return _project_document(store, snapshot)

    @app.delete(
        "/api/v1/projects/{project_id}/proposals/{proposal_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_proposal(project_id: str, proposal_id: str) -> Response:
        store.delete_proposal(project_id, proposal_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/projects/{project_id}/proposals/{proposal_id}/reject",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def reject_proposal(project_id: str, proposal_id: str) -> Response:
        store.delete_proposal(project_id, proposal_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        asset = _safe_static_file(frontend, full_path)
        if asset is not None:
            return _static_response(asset)
        index = _safe_static_file(frontend, "index.html")
        if index is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return _static_response(index)

    return app


def _project_summary(snapshot: ProjectSnapshot) -> ProjectSummary:
    return ProjectSummary(
        id=snapshot.project_id,
        title=snapshot.blueprint.metadata.title,
        digest=snapshot.digest,
        export_ready=snapshot.export_ready,
        updated_at=snapshot.updated_at,
        model=snapshot.blueprint.spec.runtime.provider.model,
    )


def _project_document(store: FileProjectStore, snapshot: ProjectSnapshot) -> ProjectDocument:
    return ProjectDocument(
        id=snapshot.project_id,
        yaml=snapshot.yaml,
        blueprint=snapshot.blueprint,
        digest=snapshot.digest,
        diagnostics=list(snapshot.diagnostics),
        export_ready=snapshot.export_ready,
        layout=_validated_layout(store.load_layout(snapshot.project_id)),
        migrated_from=snapshot.migrated_from,
        migration_warnings=[
            MigrationWarningResponse.model_validate(item.to_dict())
            for item in snapshot.migration_warnings
        ],
    )


def _validated_layout(value: object) -> LayoutDocument:
    try:
        return LayoutDocument.model_validate(value)
    except ValidationError:
        raise CorruptProject from None


def _compile_project(snapshot: ProjectSnapshot) -> CompiledProject:
    try:
        return compile_blueprint(snapshot.blueprint)
    except CompilerError as exc:
        raise ExportBlocked(diagnostics=exc.diagnostics) from None


def _persist_draft(
    store: FileProjectStore,
    project_id: str,
    current: ProjectSnapshot,
    draft: ProposalDraft,
) -> StoredProposal:
    """Re-run the manual-only guards, recompute the authoritative diff, and save."""

    protected = manual_only_diagnostics(current.blueprint, draft.candidate)
    if protected:
        raise AuthoringFailed(diagnostics=protected)
    authoritative_diff = tuple(
        cast(
            dict[str, JsonValue],
            item.model_dump(mode="json", by_alias=True),
        )
        for item in semantic_diff(current.blueprint, draft.candidate)
    )
    return store.save_proposal(
        project_id,
        base_digest=current.digest,
        candidate=draft.candidate,
        semantic_diff=authoritative_diff,
        summary=draft.summary,
    )


def _place_new_workflow_nodes(
    store: FileProjectStore,
    project_id: str,
    current: Blueprint,
    candidate: Blueprint,
) -> None:
    """Give accepted AI-introduced workflow nodes deterministic canvas positions.

    Layout is a separate channel that never touches the blueprint digest.
    Placement is cosmetic, so a corrupt layout file never fails the acceptance
    that already happened — reads report it instead.
    """

    if not candidate.spec.workflows:
        return
    try:
        layout = store.load_layout(project_id)
        nodes = layout.get("nodes")
        if not isinstance(nodes, list):
            return
        existing: dict[str, dict[str, JsonValue]] = {}
        for item in nodes:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                existing[item["id"]] = item
        placed = deterministic_layout_for_new_nodes(current, candidate, existing)
        added = False
        for key, entry in placed.items():
            if key in existing:
                continue
            nodes.append({"id": key, "x": entry["x"], "y": entry["y"]})
            added = True
        if added:
            store.save_layout(project_id, layout)
    except CorruptProject:
        return


def _proposal_response(proposal: StoredProposal) -> ProposalResponse:
    return ProposalResponse(
        id=proposal.id,
        project_id=proposal.project_id,
        base_digest=proposal.base_digest,
        candidate_digest=proposal.candidate_digest,
        candidate=proposal.candidate,
        semantic_diff=[SemanticDiffEntry.model_validate(entry) for entry in proposal.semantic_diff],
        diagnostics=list(proposal.diagnostics),
        export_ready=proposal.export_ready,
        created_at=proposal.created_at,
        summary=proposal.summary,
    )


def _static_root(configured: str | Path | None) -> Path:
    if configured is None:
        configured = Path(__file__).resolve().parents[1] / "static"
    return Path(configured).resolve(strict=False)


def _safe_static_file(root: Path, relative: str) -> Path | None:
    if not root.is_dir():
        return None
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _static_response(path: Path) -> Response:
    """Return trusted local static content without Starlette's sync worker pool."""

    media_type, _ = mimetypes.guess_type(path.name)
    return Response(content=path.read_bytes(), media_type=media_type or "application/octet-stream")


__all__ = ["create_app"]
