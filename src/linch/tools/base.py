from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, cast

from jsonschema import Draft202012Validator

from .._json import walk_json
from .._tracing import trace_id_for

if TYPE_CHECKING:
    from ..permissions import Principal

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
RenderOutput: TypeAlias = Callable[[JsonValue], str]

ToolScope = Literal["read", "write", "exec"]
ResourceMode = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    """Describe a resource a tool reads or writes.

    Attributes:
        resource: Stable resource identifier used for conflict detection.

    Args:
        resource: Stable resource identifier used for conflict checks.
        mode: Access mode requested by the tool.
    """

    resource: str
    mode: ResourceMode = "read"


@dataclass(slots=True)
class Citation:
    """Attach a source citation to a tool result.

    Attributes:
        source: Locator or document identifier from which the citation came.
        chunk: Optional excerpt supporting the citation.
        metadata: Additional citation details retained for consumers.

    Args:
        id: Stable citation identifier.
        source: Source locator or document identifier.
        label: Optional display label.
        chunk: Optional cited text chunk.
        score: Optional retrieval score.
        metadata: Additional citation metadata.
    """

    id: str
    source: str
    label: str | None = None
    chunk: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolContext:
    """Provide runtime capabilities to a tool invocation.

    Attributes:
        deps: Run-scoped application dependencies.
        filesystem: Optional virtual filesystem backend.
        idempotency_key: Stable key for this run/tool-call pair.
        execution: Optional unified execution capability.

    Args:
        cwd: Working directory for the invocation.
        session_id: Session owning the invocation.
        run_id: Run executing the invocation.
        session_store: Session-store capability.
        signal: Optional cancellation signal.
        file_read_tracker: Optional tracker for read-before-write policy.
        emit: Optional progress sink.
        deps: Run-scoped application dependencies.
        filesystem: Optional virtual filesystem backend.
        idempotency_key: Stable key for this invocation.
        execution: Optional unified execution capability.
        _principal: Optional principal captured for this invocation.
    """

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

    _trace_id: str | None = field(init=False, default=None, repr=False)
    _run_config: Any = field(default=None, repr=False, kw_only=True)
    """Private originating configuration captured before detached execution."""
    _principal: Principal | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self) -> None:
        self._trace_id = trace_id_for(self.run_id)

    @property
    def trace_id(self) -> str | None:
        """Return the immutable trace correlation value for this tool call.

        Returns:
            Trace identifier, or ``None`` when tracing is unavailable.
        """
        return self._trace_id

    @property
    def principal(self) -> Principal | None:
        """Return the immutable principal captured for this tool call.

        Returns:
            Principal associated with the call, or ``None``.
        """
        return self._principal

    def report_progress(self, message: str, data: dict[str, Any] | None = None) -> None:
        """Report transient execution progress to event-stream observers.

        Progress is deliberately best-effort and cannot fail tool execution:
        observer/sink exceptions are swallowed. Calls are a no-op when the
        scheduler did not install a sink (for example in direct unit tests).

        Args:
            message: Human-readable progress message.
            data: Optional progress payload passed to observers.

        Raises:
            TypeError: If ``message`` is not text or ``data`` is not a mapping.
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


@dataclass(slots=True)
class ToolResult:
    """Represent the legacy text projection of a tool invocation.

    Attributes:
        metadata: Additional result details for host consumers.
        citations: Source references associated with the result.
        attachments: Host-defined attachments without an enforced reference shape.
        recovery_hint: Guidance the loop can append after an all-error tool batch.

    Args:
        content: Provider-facing result text.
        summary: Short human-readable result summary.
        is_error: Whether the invocation failed.
        metadata: Additional result metadata.
        citations: Source citations associated with the result.
        attachments: External attachments associated with the result.
        duration_ms: Invocation duration in milliseconds.
        truncated: Whether content was truncated.
        recovery_hint: Optional guidance for recovering from an error.
    """

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

    Attributes:
        reference: JSON-safe locator used to retrieve the external payload.

    Args:
        reference: JSON-safe locator for the external payload.
        name: Optional attachment name.
        media_type: Optional MIME type.
        metadata: JSON-safe attachment metadata.
    """

    reference: JsonValue
    name: str | None = None
    media_type: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class ToolOutput:
    """Canonical successful output from a V2 tool.

    Attributes:
        value: Primary JSON-safe result value before rendering for a provider.

    Args:
        value: JSON-safe primary result value.
        attachments: External payload references.
        metadata: JSON-safe output metadata.
    """

    value: JsonValue
    attachments: list[ToolAttachment] = field(default_factory=list)
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class ToolOutputError:
    """Canonical expected failure from a V2 tool.

    Error outputs intentionally bypass the tool's success ``output_schema``.
    ``message`` is the provider-facing error text; structured error details can
    be retained separately without forcing them into that text projection.

    Attributes:
        details: Optional JSON-safe structured information about the failure.

    Args:
        message: Provider-facing error text.
        code: Optional machine-readable error code.
        details: Optional JSON-safe structured details.
        attachments: External payload references.
        metadata: JSON-safe error metadata.
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

    Args:
        value: Value to validate and isolate.

    Returns:
        An isolated JSON-compatible value.

    Raises:
        ValueError: If ``value`` contains an unsupported object, key, cycle, or
            non-finite float.
    """

    return cast(
        JsonValue,
        walk_json(value, check_node=_check_tool_json_node, check_key=_check_tool_json_key),
    )


def _check_tool_json_node(value: Any, path: str, active: set[int], depth: int) -> int | None:
    if value is None or isinstance(value, str | bool | int):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return None
    if not isinstance(value, list | dict):
        raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")
    if id(value) in active:
        raise ValueError(f"{path} contains a cycle")
    return depth + 1


def _check_tool_json_key(key: Any, path: str) -> None:
    if not isinstance(key, str):
        raise ValueError(f"{path} contains a non-string object key")


def normalize_tool_output(output: CanonicalToolOutput | JsonValue) -> CanonicalToolOutput:
    """Normalize raw V2 output and every attachment/reference without coercion.

    Args:
        output: Canonical output or JSON value returned by a tool.

    Returns:
        Isolated canonical output.

    Raises:
        ValueError: If the output or attachment data is not strict JSON.
    """

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
    """Validate a canonical success output with JSON Schema Draft 2020-12.

    Args:
        output: Canonical output to validate.
        schema: JSON Schema applied to successful output values.

    Raises:
        jsonschema.ValidationError: If a successful value violates ``schema``.
    """

    if isinstance(output, ToolOutputError):
        return
    Draft202012Validator(schema).validate(output.value)


def default_render_output(value: JsonValue) -> str:
    """Render JSON deterministically for the provider-facing legacy projection.

    Args:
        value: JSON-safe value to render.

    Returns:
        Deterministic string representation.
    """

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
    """Render a canonical output and enforce the synchronous string contract.

    Args:
        output: Canonical output to render.
        renderer: Optional synchronous renderer for successful values.

    Returns:
        Provider-facing result text.

    Raises:
        TypeError: If the renderer is asynchronous or does not return text.
    """

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
    """Define the duck-typed contract implemented by executable tools.

    Attributes:
        name: Stable tool name exposed to the model.
        description: Human-readable tool description.
        input_schema: JSON Schema for accepted inputs.
        scope: Access scope used for scheduling and policy.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    scope: ToolScope

    @property
    def parallel(self) -> bool | Callable[[dict[str, Any]], bool]:
        """Return whether this input may run in parallel with peers.

        Returns:
            A static flag or input-aware predicate.
        """

        ...

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize model-supplied input.

        Args:
            raw: Untrusted input mapping from the provider.

        Returns:
            Normalized input accepted by :meth:`execute`.
        """

        ...

    async def execute(
        self, input: dict[str, Any], ctx: ToolContext
    ) -> ToolResult | CanonicalToolOutput | JsonValue:
        """Execute the tool with validated input and runtime context.

        Args:
            input: Normalized tool arguments.
            ctx: Runtime capabilities for this invocation.

        Returns:
            Legacy text result, canonical output, or a JSON value.
        """

        ...

    def summarize(self, input: dict[str, Any]) -> str:
        """Summarize a validated call for logs and event streams.

        Args:
            input: Normalized tool arguments.

        Returns:
            Human-readable invocation summary.
        """

        ...


def require_str(raw: dict[str, Any], key: str) -> str:
    """Read a required non-empty string from an input mapping.

    Args:
        raw: Input mapping to inspect.
        key: Field name that must contain text.

    Returns:
        The validated string.

    Raises:
        ValueError: If the field is missing, empty, or not a string.
    """

    value = raw.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{key} must be a non-empty string")
    return value
