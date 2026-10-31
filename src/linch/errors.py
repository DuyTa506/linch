from __future__ import annotations

from typing import TYPE_CHECKING

from .kernel.errors import ConfigError as ConfigError
from .kernel.errors import LinchError

if TYPE_CHECKING:
    from .run_store import RunStatus


class RunNotResumableError(LinchError):
    """A run is terminal or has no checkpoint from which to resume.

    Args:
        run_id: Identifier of the run that cannot be resumed.
        status: Current persisted status of the run.

    Attributes:
        run_id: Identifier of the non-resumable run.
        status: Current persisted status of the run.
    """

    kind = "run_not_resumable"

    def __init__(self, run_id: str, status: RunStatus) -> None:
        """Initialize an error for a run that cannot be resumed.

        Args:
            run_id: Identifier of the run that cannot be resumed.
            status: Current persisted status of the run.
        """
        super().__init__(f"Run {run_id!r} cannot be resumed (status: {status})")
        self.run_id = run_id
        self.status = status


class AuthError(LinchError):
    """Authentication or authorization failed while contacting a provider."""

    kind = "auth"


class RateLimitError(LinchError):
    """A provider rejected a request because its rate limit was exceeded.

    Args:
        message: Provider error message describing the rejected request.
        retry_after_seconds: Optional delay suggested before retrying.

    Attributes:
        retry_after_seconds: Suggested retry delay, when the provider supplied
            one.
    """

    kind = "rate_limit"
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        """Initialize a rate-limit error with an optional retry delay.

        Args:
            message: Provider error message describing the rejected request.
            retry_after_seconds: Optional delay suggested before retrying.
        """
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ContextLengthError(LinchError):
    """The provider rejected a request because its context was too large."""

    kind = "context_length"


class PermissionDeniedError(LinchError):
    """Permission policy denied the requested tool or operation."""

    kind = "permission_denied"


class ToolExecutionError(LinchError):
    """A tool failed while executing an otherwise valid tool call."""

    kind = "tool_execution"


class ToolTimeoutError(LinchError):
    """A tool did not finish before its configured timeout."""

    kind = "tool_timeout"
    retryable = True


class AbortError(LinchError):
    """The caller or run signal requested cooperative cancellation."""

    kind = "abort"


class ProviderError(LinchError):
    """A provider request failed without a more specific provider error type.

    Args:
        message: Provider error message describing the failed request.
        status: Optional HTTP-like status code returned by the provider.
        retryable: Whether the failure may be retried by the runtime.

    Attributes:
        status: HTTP-like provider status code, when available.
        retryable: Whether the failure may be retried by the runtime.
    """

    kind = "provider"

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        """Initialize a provider failure with retry metadata.

        Args:
            message: Provider error message describing the failed request.
            status: Optional HTTP-like status code returned by the provider.
            retryable: Whether the failure may be retried by the runtime.
        """
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class SkillError(LinchError):
    """A skill could not be discovered, loaded, or invoked."""

    kind = "skill"


class WorkflowError(LinchError):
    """A subagent run inside a workflow ended in an error.

    Args:
        message: Description of the workflow failure.
        error: Optional structured error metadata from the failed subagent.

    Attributes:
        error: Structured error metadata, when supplied.
    """

    kind = "workflow"

    def __init__(self, message: str, *, error: dict[str, str] | None = None):
        """Initialize a workflow failure with optional structured metadata.

        Args:
            message: Description of the workflow failure.
            error: Optional structured error metadata from the failed subagent.
        """
        super().__init__(message)
        self.error = error


class WorkflowTimeoutError(WorkflowError):
    """A ``wf.agent`` / ``wf.step`` call exceeded its ``timeout_ms``."""

    kind = "workflow_timeout"
    retryable = True


class WorkflowSuspended(BaseException):
    """A ``wf.interrupt`` has no answer yet, so the workflow stopped cleanly.

    This is control flow, not an error: the run is parked at a decision point,
    not broken. It deliberately extends ``BaseException`` (and not
    :class:`LinchError`) so an ``except Exception:`` inside the workflow
    function cannot swallow a suspend. Re-invoke ``run_workflow`` with the same
    ``run_id`` and ``resume={key: value}`` to continue.

    Attributes:
        key: The interrupt's key, as passed to ``wf.interrupt``.
        payload: Whatever context the workflow attached for the decider.
    """

    def __init__(self, key: str, payload: object = None):
        """Initialize a suspended workflow interrupt.

        Args:
            key: Interrupt key used to identify the decision point.
            payload: Context supplied to the host decider.
        """
        super().__init__(f"workflow suspended at interrupt {key!r}")
        self.key = key
        self.payload = payload
