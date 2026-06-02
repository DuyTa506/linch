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
