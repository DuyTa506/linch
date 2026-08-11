class LinchError(Exception):
    kind = "linch"
    retryable = False


class ConfigError(LinchError):
    kind = "config"


class AuthError(LinchError):
    kind = "auth"


class RateLimitError(LinchError):
    kind = "rate_limit"
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ContextLengthError(LinchError):
    kind = "context_length"


class PermissionDeniedError(LinchError):
    kind = "permission_denied"


class ToolExecutionError(LinchError):
    kind = "tool_execution"


class ToolTimeoutError(LinchError):
    kind = "tool_timeout"
    retryable = True


class AbortError(LinchError):
    kind = "abort"


class ProviderError(LinchError):
    kind = "provider"

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class SkillError(LinchError):
    kind = "skill"


class WorkflowError(LinchError):
    """A subagent run inside a workflow ended in an error."""

    kind = "workflow"

    def __init__(self, message: str, *, error: dict[str, str] | None = None):
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
        super().__init__(f"workflow suspended at interrupt {key!r}")
        self.key = key
        self.payload = payload
