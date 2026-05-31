class AgentKitError(Exception):
    kind = "agent_kit"
    retryable = False


class ConfigError(AgentKitError):
    kind = "config"


class AuthError(AgentKitError):
    kind = "auth"


class RateLimitError(AgentKitError):
    kind = "rate_limit"
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ContextLengthError(AgentKitError):
    kind = "context_length"


class PermissionDeniedError(AgentKitError):
    kind = "permission_denied"


class ToolExecutionError(AgentKitError):
    kind = "tool_execution"


class AbortError(AgentKitError):
    kind = "abort"


class ProviderError(AgentKitError):
    kind = "provider"

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class SkillError(AgentKitError):
    kind = "skill"
