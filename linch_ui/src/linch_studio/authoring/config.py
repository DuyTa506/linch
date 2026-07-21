"""Environment-only configuration for optional AI authoring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .errors import AuthoringConfigurationError

SUPPORTED_AUTHORING_PROVIDERS = frozenset(
    {
        "anthropic",
        "deepseek",
        "gemini",
        "llama_cpp",
        "openai_chat",
        "openai_responses",
        "sglang",
        "vllm",
    }
)
_CLOUD_PROVIDERS = frozenset({"anthropic", "deepseek", "gemini", "openai_chat", "openai_responses"})
_LOCAL_PROVIDERS = frozenset({"llama_cpp", "sglang", "vllm"})
_BASE_URL_PROVIDERS = _LOCAL_PROVIDERS | frozenset({"deepseek"})
REASONING_LEVELS = ("off", "low", "medium", "high")


@dataclass(frozen=True, slots=True)
class AuthoringConfig:
    provider: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    project: str | None = None
    location: str = "us-central1"
    context_window: int | None = None
    max_output_tokens: int = 32_768
    token_budget: int = 200_000
    timeout_seconds: float = 120.0
    max_turns: int = 12
    reasoning: str = "medium"
    # JSON-object mode for OpenAI-compatible providers without json_schema
    # support (e.g. DeepSeek); the strict turn schema is still enforced by the
    # loop's text-parse and validation gates.
    json_mode: bool = False

    def __post_init__(self) -> None:
        if self.provider not in SUPPORTED_AUTHORING_PROVIDERS:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_PROVIDER must name a supported built-in provider."
            )
        if not self.model or len(self.model) > 256:
            raise AuthoringConfigurationError("LINCH_STUDIO_MODEL must be a non-empty model ID.")
        if self.provider in _CLOUD_PROVIDERS and not self.api_key:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_API_KEY is required for the selected authoring provider."
            )
        if self.provider in _BASE_URL_PROVIDERS and not self.base_url:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_BASE_URL is required for the selected authoring provider."
            )
        if self.provider == "deepseek" and _is_anthropic_compatibility_url(self.base_url):
            raise AuthoringConfigurationError(
                "DeepSeek Support must use its native OpenAI-compatible endpoint, not /anthropic. "
                "Set LINCH_STUDIO_PROVIDER=deepseek and use the base URL without /anthropic."
            )
        if self.provider == "anthropic" and _is_official_deepseek_anthropic_url(self.base_url):
            raise AuthoringConfigurationError(
                "The configured Anthropic endpoint is DeepSeek compatibility mode, which does not "
                "support Studio's native structured-output contract. Set LINCH_STUDIO_PROVIDER="
                "deepseek and LINCH_STUDIO_BASE_URL=https://api.deepseek.com instead."
            )
        if self.context_window is not None and not 1_024 <= self.context_window <= 10_000_000:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_CONTEXT_WINDOW must be between 1024 and 10000000."
            )
        if not 1 <= self.max_output_tokens <= 1_000_000:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_MAX_OUTPUT_TOKENS must be between 1 and 1000000."
            )
        if not 1 <= self.token_budget <= 1_000_000_000:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_TOKEN_BUDGET must be between 1 and 1000000000."
            )
        if not 0 < self.timeout_seconds <= 3_600:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_TIMEOUT_SECONDS must be greater than 0 and at most 3600."
            )
        if not 1 <= self.max_turns <= 64:
            raise AuthoringConfigurationError("LINCH_STUDIO_MAX_TURNS must be between 1 and 64.")
        if self.reasoning not in REASONING_LEVELS:
            raise AuthoringConfigurationError(
                "LINCH_STUDIO_REASONING must be one of: " + ", ".join(REASONING_LEVELS) + "."
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AuthoringConfig:
        values = os.environ if environ is None else environ
        provider = values.get("LINCH_STUDIO_PROVIDER", "").strip().lower().replace("-", "_")
        model = values.get("LINCH_STUDIO_MODEL", "").strip()
        if not provider:
            raise AuthoringConfigurationError("LINCH_STUDIO_PROVIDER is required.")
        if not model:
            raise AuthoringConfigurationError("LINCH_STUDIO_MODEL is required.")
        return cls(
            provider=provider,
            model=model,
            api_key=_optional(values, "LINCH_STUDIO_API_KEY"),
            base_url=_optional(values, "LINCH_STUDIO_BASE_URL"),
            project=_optional(values, "LINCH_STUDIO_PROJECT"),
            location=_optional(values, "LINCH_STUDIO_LOCATION") or "us-central1",
            context_window=_optional_int(values, "LINCH_STUDIO_CONTEXT_WINDOW"),
            max_output_tokens=_int(values, "LINCH_STUDIO_MAX_OUTPUT_TOKENS", 32_768),
            token_budget=_int(values, "LINCH_STUDIO_TOKEN_BUDGET", 200_000),
            timeout_seconds=_float(values, "LINCH_STUDIO_TIMEOUT_SECONDS", 120.0),
            max_turns=_int(values, "LINCH_STUDIO_MAX_TURNS", 12),
            reasoning=(values.get("LINCH_STUDIO_REASONING", "").strip().lower() or "medium"),
            json_mode=_bool(values, "LINCH_STUDIO_JSON_MODE", False),
        )


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name, "").strip()
    return value or None


def _is_anthropic_compatibility_url(base_url: str | None) -> bool:
    return bool(base_url and urlparse(base_url).path.rstrip("/").endswith("/anthropic"))


def _is_official_deepseek_anthropic_url(base_url: str | None) -> bool:
    if not _is_anthropic_compatibility_url(base_url):
        return False
    return (urlparse(base_url or "").hostname or "").lower() == "api.deepseek.com"


def _int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AuthoringConfigurationError(f"{name} must be an integer.") from exc


def _optional_int(values: Mapping[str, str], name: str) -> int | None:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise AuthoringConfigurationError(f"{name} must be an integer.") from exc


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise AuthoringConfigurationError(f"{name} must be a boolean (true/false).")


def _float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise AuthoringConfigurationError(f"{name} must be a number.") from exc


__all__ = ["REASONING_LEVELS", "SUPPORTED_AUTHORING_PROVIDERS", "AuthoringConfig"]
