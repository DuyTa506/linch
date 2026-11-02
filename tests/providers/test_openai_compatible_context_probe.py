from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from linch.providers import SGLangProvider, SGLangProviderOptions, VLLMProvider, VLLMProviderOptions
from linch.providers import _context_probe as context_probe
from linch.types import ProviderRequest
from tests.support.providers import OpenAICompatibleFakeClient


class _Models:
    def __init__(self, rows: object = (), error: Exception | None = None) -> None:
        self.rows = rows
        self.error = error
        self.calls = 0

    async def list(self) -> SimpleNamespace:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(**row) for row in self.rows])


def _with_models(provider: object, models: _Models) -> object:
    async def get_client() -> SimpleNamespace:
        return SimpleNamespace(models=models)

    provider._get_client = get_client  # type: ignore[attr-defined]
    return provider


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_probe_matches_served_model_once(provider_cls, options_cls):
    models = _Models(
        [
            {"id": "other", "max_model_len": 2_048},
            {"id": "served", "max_model_len": 65_536},
            {"id": "bad", "max_model_len": 0},
        ]
    )
    provider = _with_models(provider_cls(options_cls(base_url="https://server/v1")), models)

    assert provider.context_window("served") == 128_000
    await provider.prepare()
    await provider.prepare()
    assert provider.context_window("served") == 65_536
    assert provider.context_window("other") == 2_048
    assert provider.context_window("unknown") == 128_000
    assert models.calls == 1


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_explicit_context_skips_probe(provider_cls, options_cls):
    models = _Models()
    provider = _with_models(
        provider_cls(options_cls(base_url="https://server", context_window=12_345)), models
    )
    await provider.prepare()
    assert provider.context_window("served") == 12_345
    assert models.calls == 0


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_probe_uses_the_chat_client_not_a_second_transport(provider_cls, options_cls):
    """Auth, base path, and redirect policy must match the chat transport."""
    provider = provider_cls(options_cls(base_url="https://host/inference", api_key="secret"))
    client = await provider._get_client()
    assert str(client.base_url).rstrip("/") == "https://host/inference"
    assert client.api_key == "secret"
    probe = vars(context_probe)
    assert "urlopen" not in probe and "request" not in probe


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_probe_honors_base_url_from_environment(monkeypatch, provider_cls, options_cls):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env-host/v1")
    models = _Models([{"id": "m", "max_model_len": 9_000}])
    provider = _with_models(provider_cls(options_cls()), models)
    await provider.prepare()
    assert provider.context_window("m") == 9_000


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_probe_never_targets_the_default_openai_endpoint(
    monkeypatch, provider_cls, options_cls
):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    models = _Models([{"id": "m", "max_model_len": 9_000}])
    provider = _with_models(provider_cls(options_cls()), models)
    await provider.prepare()
    assert models.calls == 0
    assert provider.context_window("m") == 128_000


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_probe_timeout_falls_back(provider_cls, options_cls):
    class _Hung(_Models):
        async def list(self) -> SimpleNamespace:
            self.calls += 1
            await asyncio.Event().wait()
            raise AssertionError

    models = _Hung()
    provider = _with_models(
        provider_cls(options_cls(base_url="https://server", context_window_timeout=0.01)), models
    )
    await provider.prepare()
    assert provider.context_window("m") == 128_000


async def test_probe_failure_is_cached_and_auto_off_skips():
    models = _Models(error=OSError("offline"))
    provider = _with_models(
        SGLangProvider(SGLangProviderOptions(base_url="https://server")), models
    )
    await provider.prepare()
    await provider.prepare()
    assert models.calls == 1
    assert provider.context_window("m") == 128_000

    disabled = _with_models(
        SGLangProvider(SGLangProviderOptions(base_url="https://server", auto_context_window=False)),
        models,
    )
    await disabled.prepare()
    assert models.calls == 1


async def test_probe_swallows_client_construction_failure():
    provider = VLLMProvider(VLLMProviderOptions(base_url="https://server"))

    async def broken() -> None:
        raise RuntimeError("no credentials")

    provider._get_client = broken  # type: ignore[method-assign]
    await provider.prepare()
    assert provider.context_window("m") == 128_000


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_single_model_applies_to_alias(provider_cls, options_cls):
    models = _Models([{"id": "/root/models/local", "max_model_len": 262_144}])
    provider = _with_models(provider_cls(options_cls(base_url="https://server/v1")), models)
    await provider.prepare()
    assert provider.context_window("public/alias") == 262_144


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_multi_model_does_not_guess_alias(provider_cls, options_cls):
    models = _Models([{"id": "a", "max_model_len": 1_024}, {"id": "b", "max_model_len": None}])
    provider = _with_models(provider_cls(options_cls(base_url="https://server/v1")), models)
    await provider.prepare()
    assert provider.context_window("alias") == 128_000


@pytest.mark.parametrize(
    "provider_cls,options_cls,flag",
    [
        (VLLMProvider, VLLMProviderOptions, "--enable-prompt-tokens-details"),
        (SGLangProvider, SGLangProviderOptions, "--enable-cache-report"),
    ],
)
async def test_openai_compatible_cache_warning_is_once_per_instance(
    caplog, provider_cls, options_cls, flag
):
    def chunk(usage):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="ok", reasoning_content=None, tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=usage,
        )

    provider = provider_cls(options_cls())
    provider._client = OpenAICompatibleFakeClient(
        [chunk(SimpleNamespace(prompt_tokens=5, completion_tokens=1))]
    )
    with caplog.at_level("WARNING"):
        _ = [
            event
            async for event in provider.stream(
                ProviderRequest(model="m", system=[], tools=[], messages=[])
            )
        ]
        provider._client = OpenAICompatibleFakeClient(
            [chunk(SimpleNamespace(prompt_tokens=5, completion_tokens=1))]
        )
        _ = [
            event
            async for event in provider.stream(
                ProviderRequest(model="m", system=[], tools=[], messages=[])
            )
        ]
    assert sum(flag in record.message for record in caplog.records) == 1

    caplog.clear()
    present = provider_cls(options_cls())
    present._client = OpenAICompatibleFakeClient(
        [
            chunk(
                SimpleNamespace(
                    prompt_tokens=5,
                    completion_tokens=1,
                    prompt_tokens_details={},
                )
            )
        ]
    )
    with caplog.at_level("WARNING"):
        _ = [
            event
            async for event in present.stream(
                ProviderRequest(model="m", system=[], tools=[], messages=[])
            )
        ]
    assert not [record for record in caplog.records if flag in record.message]

    caplog.clear()
    no_usage = provider_cls(options_cls())
    no_usage._client = OpenAICompatibleFakeClient([chunk(None)])
    with caplog.at_level("WARNING"):
        _ = [
            event
            async for event in no_usage.stream(
                ProviderRequest(model="m", system=[], tools=[], messages=[])
            )
        ]
    assert not [record for record in caplog.records if flag in record.message]
