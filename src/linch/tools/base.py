from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, cast

from jsonschema import Draft202012Validator

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
RenderOutput: TypeAlias = Callable[[JsonValue], str]

ToolScope = Literal["read", "write", "exec"]
ResourceMode = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    resource: str
    mode: ResourceMode = "read"


@dataclass(slots=True)
class Citation:
    id: str
    source: str
    label: str | None = None
    chunk: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolContext:
    cwd: str
    session_id: str
    run_id: str
    session_store: Any
    signal: Any = None
    file_read_tracker: Any = None
    emit: Callable[[str, dict[str, Any] | None], None] | None = None
    """Runtime-owned progress sink.

    Tools should call :meth:`report_progress` instead of invoking this field
    directly. The sink is scoped to one ``execute()`` call and ignores reports
    made after that call settles.
    """
    deps: Any = None
    """Application-state dependency object injected via ``Agent(deps=...)``
    or ``RunOptions(deps=...)``.  Use this to share a vector-store client,
    database connection, or any other per-agent / per-run resource across
    all tool calls without requiring ``__init__``-closure injection."""

    filesystem: Any = None
    """Per-session virtual :class:`~linch.filesystem.backend.FileBackend`,
    when the filesystem subsystem is enabled (``Agent(filesystem=...)`` or
    ``Agent(result_offload=...)``).  The ls / read_file / write_file / edit_file
    tools read and write through this, and the scheduler offloads oversized
    results here.  ``None`` when the subsystem is off."""

    idempotency_key: str = ""
    """Stable key for this ``(run, tool call)`` pair, ``f"{run_id}:{tool_use_id}"``.
    Both parts survive a crash and are reused on resume, so a tool whose external
    side effect ran *before* its completion record became durable can key on this
    to deduplicate or reconcile the interrupted intent when it re-executes.
    Durable execution is at-least-once, not exactly-once — Linch supplies the
    stable key; the integration owns the deduplication.  Empty string when the
    context is built outside the loop (e.g. direct unit tests)."""

    execution: Any = None
    """The agent's unified execution world (``shell`` + ``fs``), when one is
    configured. Custom tools can consume this capability directly without
    reaching back through the Agent or global state. ``None`` on the legacy
    default path. Kept last for positional-constructor compatibility."""

    @property
    def sessionId(self) -> str:
        return self.session_id

    @property
    def runId(self) -> str:
        return self.run_id

    @property
    def sessionStore(self) -> Any:
        return self.session_store

    @property
    def fileReadTracker(self) -> Any:
        return self.file_read_tracker

    @property
    def idempotencyKey(self) -> str:
        return self.idempotency_key

    def report_progress(self, message: str, data: dict[str, Any] | None = None) -> None:
        """Report transient execution progress to event-stream observers.

        Progress is deliberately best-effort and cannot fail tool execution:
        observer/sink exceptions are swallowed. Calls are a no-op when the
        scheduler did not install a sink (for example in direct unit tests).
        """
        if not isinstance(message, str):
            raise TypeError("progress message must be a string")
        if data is not None and not isinstance(data, dict):
            raise TypeError("progress data must be a dict or None")
        sink = self.emit
        if sink is None:
            return
        try:
            sink(message, data)
        except Exception:
            return

    @property
    def reportProgress(self) -> Callable[[str, dict[str, Any] | None], None]:
        return self.report_progress


@dataclass(slots=True)
class ToolResult:
    content: str
    summary: str = ""
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    attachments: list[Any] = field(default_factory=list)
    duration_ms: int = 0
    truncated: bool = False
    recovery_hint: str = ""


@dataclass(slots=True)
class ToolAttachment:
    """A durable, JSON-only reference to output stored outside the model context.

    ``reference`` is deliberately not a bytes payload. Hosts can use a URI, an
    object-store key, or a JSON object containing their own stable locator.
    """

    reference: JsonValue
    name: str | None = None
    media_type: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class ToolOutput:
    """Canonical successful output from a V2 tool."""

    value: JsonValue
    attachments: list[ToolAttachment] = field(default_factory=list)
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class ToolOutputError:
    """Canonical expected failure from a V2 tool.

    Error outputs intentionally bypass the tool's success ``output_schema``.
    ``message`` is the provider-facing error text; structured error details can
    be retained separately without forcing them into that text projection.
    """

    message: str
    code: str | None = None
    details: JsonValue = None
    attachments: list[ToolAttachment] = field(default_factory=list)
    metadata: dict[str, JsonValue] = field(default_factory=dict)


CanonicalToolOutput: TypeAlias = ToolOutput | ToolOutputError


class ToolOutputContractError(TypeError):
    """Non-retryable violation of a V2 tool's output contract."""


def normalize_json_value(value: Any) -> JsonValue:
    """Return an isolated, strict JSON value or raise ``ValueError``.

    Unlike ``json.dumps(default=str)``, this never coerces foreign objects or
    mapping keys. It also rejects cycles and non-finite floats, which JSON's
    default Python encoder otherwise accepts as non-standard tokens.
    """

    active: set[int] = set()

    def visit(item: Any, path: str) -> JsonValue:
        if item is None or isinstance(item, str | bool):
            return cast(JsonValue, item)
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite float")
            return item
        if isinstance(item, list):
            identity = id(item)
            if identity in active:
                raise ValueError(f"{path} contains a cycle")
            active.add(identity)
            try:
                return [visit(child, f"{path}[{index}]") for index, child in enumerate(item)]
            finally:
                active.remove(identity)
        if isinstance(item, dict):
            identity = id(item)
            if identity in active:
                raise ValueError(f"{path} contains a cycle")
            active.add(identity)
            try:
                normalized: dict[str, JsonValue] = {}
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} contains a non-string object key")
                    normalized[key] = visit(child, f"{path}.{key}")
                return normalized
            finally:
                active.remove(identity)
        raise ValueError(f"{path} contains non-JSON value {type(item).__name__}")

    return visit(value, "$")


def normalize_tool_output(output: CanonicalToolOutput | JsonValue) -> CanonicalToolOutput:
    """Normalize raw V2 output and every attachment/reference without coercion."""

    if isinstance(output, ToolOutputError):
        if not isinstance(output.message, str):
            raise ValueError("ToolOutputError.message must be a string")
        if output.code is not None and not isinstance(output.code, str):
            raise ValueError("ToolOutputError.code must be a string or None")
        return ToolOutputError(
            message=output.message,
            code=output.code,
            details=normalize_json_value(output.details),
            attachments=_normalize_attachments(output.attachments),
            metadata=_normalize_metadata(output.metadata, "ToolOutputError.metadata"),
        )
    if isinstance(output, ToolOutput):
        return ToolOutput(
            value=normalize_json_value(output.value),
            attachments=_normalize_attachments(output.attachments),
            metadata=_normalize_metadata(output.metadata, "ToolOutput.metadata"),
        )
    return ToolOutput(value=normalize_json_value(output))


def _normalize_attachments(attachments: Any) -> list[ToolAttachment]:
    if not isinstance(attachments, list | tuple):
        raise ValueError("tool output attachments must be a list")
    normalized: list[ToolAttachment] = []
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, ToolAttachment):
            raise ValueError(f"tool output attachments[{index}] must be ToolAttachment")
        if attachment.name is not None and not isinstance(attachment.name, str):
            raise ValueError(f"tool output attachments[{index}].name must be a string or None")
        if attachment.media_type is not None and not isinstance(attachment.media_type, str):
            raise ValueError(
                f"tool output attachments[{index}].media_type must be a string or None"
            )
        normalized.append(
            ToolAttachment(
                reference=normalize_json_value(attachment.reference),
                name=attachment.name,
                media_type=attachment.media_type,
                metadata=_normalize_metadata(
                    attachment.metadata, f"tool output attachments[{index}].metadata"
                ),
            )
        )
    return normalized


def _normalize_metadata(metadata: Any, label: str) -> dict[str, JsonValue]:
    normalized = normalize_json_value(metadata)
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} must be a JSON object")
    return normalized


def validate_tool_output(output: CanonicalToolOutput, schema: dict[str, Any]) -> None:
    """Validate a canonical success output with JSON Schema Draft 2020-12."""

    if isinstance(output, ToolOutputError):
        return
    Draft202012Validator(schema).validate(output.value)


def default_render_output(value: JsonValue) -> str:
    """Render JSON deterministically for the provider-facing legacy projection."""

    normalized = normalize_json_value(value)
    if isinstance(normalized, str):
        return normalized
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def render_tool_output(
    output: CanonicalToolOutput,
    renderer: RenderOutput | None = None,
) -> str:
    """Render a canonical output and enforce the synchronous string contract."""

    if isinstance(output, ToolOutputError):
        return output.message
    render = renderer or default_render_output
    rendered = render(output.value)
    if inspect.isawaitable(rendered):
        if inspect.iscoroutine(rendered):
            rendered.close()
        raise TypeError("tool output renderer must be synchronous")
    if not isinstance(rendered, str):
        raise TypeError("tool output renderer must return str")
    return rendered


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]
    scope: ToolScope
    parallel: bool

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    async def execute(
        self, input: dict[str, Any], ctx: ToolContext
    ) -> ToolResult | CanonicalToolOutput | JsonValue: ...

    def summarize(self, input: dict[str, Any]) -> str: ...


def require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{key} must be a non-empty string")
    return value
