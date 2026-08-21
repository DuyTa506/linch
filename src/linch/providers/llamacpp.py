from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit

from linch._prompt_cache import LLAMACPP_PROMPT_CACHE, apply_extra_body_cache
from linch.providers.openai_chat import (
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
    _build_openai_compatible_payload,
)
from linch.types import ModelId, ProviderRequest


@dataclass(slots=True)
class LlamaCppProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    context_window: int = 128_000
    auto_context_window: bool = True
    context_window_timeout: float = 2.0
    json_mode: bool = False
    # On by default so llama.cpp streams token usage in the final chunk; set
    # False for older server builds that reject the OpenAI stream_options field.
    include_stream_options: bool = True
    parallel_tool_calls: bool | None = None
    chat_template_kwargs: dict[str, Any] | None = None
    reasoning_format: str | None = None
    reasoning_control: bool | None = None
    generation_prompt: str | None = None
    parse_tool_calls: bool | None = None
    extra_body: dict[str, Any] | None = None


class LlamaCppProvider(OpenAIChatCompletionsProvider):
    """llama.cpp server provider using its OpenAI-compatible chat endpoint."""

    id = "llamacpp"

    def __init__(self, options: LlamaCppProviderOptions | None = None) -> None:
        opts = options or LlamaCppProviderOptions()
        self._llamacpp_options = opts
        self._context_window_cache: int | None = None
        super().__init__(
            OpenAIChatProviderOptions(
                api_key=opts.api_key,
                base_url=opts.base_url,
                default_headers=opts.default_headers,
                json_mode=False,
                parallel_tool_calls=opts.parallel_tool_calls,
            )
        )

    def context_window(self, model: ModelId) -> int:
        # Immediate, non-blocking lookup: the detected value (populated by
        # prepare()) or the configured default. Never performs network I/O — the
        # /props probe runs off the event loop in prepare() instead.
        if self._context_window_cache is not None:
            return self._context_window_cache
        return self._llamacpp_options.context_window

    async def prepare(self) -> None:
        """Discover the server's context window off the event loop and cache it.

        Coalesced and awaited once before the first run by the agent. Runs the
        blocking ``/props`` probe on the shared blocking bridge so the event loop
        is never stalled. A failed or timed-out probe leaves the cache unset, so
        ``context_window()`` falls back to the configured value. Explicit
        configuration (``auto_context_window=False``) skips the probe entirely.
        """
        opts = self._llamacpp_options
        if self._context_window_cache is not None:
            return
        if not (opts.auto_context_window and opts.base_url):
            return
        from linch._blocking import run_blocking

        detected = await run_blocking(_fetch_llamacpp_context_window, opts)
        if detected is not None:
            self._context_window_cache = detected

    def _build_payload(self, req: ProviderRequest) -> dict[str, Any]:
        return _build_llamacpp_payload(req, self._llamacpp_options)

    def _chunk_cache_read_tokens(self, chunk: Any) -> int | None:
        return _llamacpp_cache_read_tokens(chunk)


def _llamacpp_cache_read_tokens(chunk: Any) -> int | None:
    """Read prefix-cache reuse from llama.cpp's per-response `timings.cache_n`.

    llama.cpp leaves usage.prompt_tokens_details null and instead reports the
    number of prompt tokens served from the KV cache in `timings.cache_n`,
    exposed either as a direct chunk attribute or under `model_extra`.
    """
    timings = getattr(chunk, "timings", None)
    if timings is None:
        extra = getattr(chunk, "model_extra", None)
        if isinstance(extra, dict):
            timings = extra.get("timings")
    if timings is None:
        return None
    cache_n = (
        timings.get("cache_n") if isinstance(timings, dict) else getattr(timings, "cache_n", None)
    )
    if cache_n is None:
        return None
    try:
        return int(cache_n)
    except (TypeError, ValueError):
        return None


def _build_llamacpp_payload(
    req: ProviderRequest, options: LlamaCppProviderOptions | None = None
) -> dict[str, Any]:
    opts = options or LlamaCppProviderOptions()
    extra_body = dict(opts.extra_body or {})
    if opts.chat_template_kwargs is not None:
        extra_body["chat_template_kwargs"] = opts.chat_template_kwargs
    if opts.reasoning_format is not None:
        extra_body["reasoning_format"] = opts.reasoning_format
    if opts.reasoning_control is not None:
        extra_body["reasoning_control"] = opts.reasoning_control
    if opts.generation_prompt is not None:
        extra_body["generation_prompt"] = opts.generation_prompt
    if opts.parse_tool_calls is not None:
        extra_body["parse_tool_calls"] = opts.parse_tool_calls
    apply_extra_body_cache(extra_body, req, wire=LLAMACPP_PROMPT_CACHE)

    # Use the shared OpenAI-compatible builder so structured output retains the
    # nested ``json_schema`` shape expected by llama.cpp's OpenAI endpoint.
    # ``stream_options.include_usage`` is what makes llama.cpp emit token usage
    # in the final stream chunk; older servers can opt out via the option.
    return _build_openai_compatible_payload(
        req,
        json_mode=opts.json_mode,
        include_stream_options=opts.include_stream_options,
        parallel_tool_calls=opts.parallel_tool_calls,
        extra_body=extra_body or None,
    )


def _fetch_llamacpp_context_window(opts: LlamaCppProviderOptions) -> int | None:
    for url in _props_urls(opts.base_url):
        try:
            raw = _read_json(url, opts)
        except (OSError, TimeoutError, ValueError, error.URLError, error.HTTPError):
            continue
        n_ctx = _extract_n_ctx(raw)
        if n_ctx is not None:
            return n_ctx
    return None


def _props_urls(base_url: str | None) -> list[str]:
    if not base_url:
        return []

    base = base_url.rstrip("/")
    urls: list[str] = []

    def add(url: str) -> None:
        if url not in urls:
            urls.append(url)

    add(f"{base}/props")
    split = urlsplit(base)
    path = split.path.rstrip("/")
    if path.endswith("/v1"):
        root_path = path[:-3].rstrip("/")
        add(urlunsplit((split.scheme, split.netloc, f"{root_path}/props", "", "")))
    else:
        add(urlunsplit((split.scheme, split.netloc, f"{path}/v1/props", "", "")))
    return urls


def _read_json(url: str, opts: LlamaCppProviderOptions) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if opts.default_headers:
        headers.update(opts.default_headers)
    if opts.api_key is not None and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {opts.api_key}"

    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=opts.context_window_timeout) as resp:
        body = resp.read()
    data = json.loads(body.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _extract_n_ctx(raw: dict[str, Any]) -> int | None:
    settings = raw.get("default_generation_settings")
    if isinstance(settings, dict):
        n_ctx = settings.get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            return n_ctx

    n_ctx = raw.get("n_ctx")
    if isinstance(n_ctx, int) and n_ctx > 0:
        return n_ctx
    return None
