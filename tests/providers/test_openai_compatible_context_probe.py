from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from linch.providers import SGLangProvider, SGLangProviderOptions, VLLMProvider, VLLMProviderOptions
from linch.types import ProviderRequest
from tests.support.providers import OpenAICompatibleFakeClient


class _Response:
    def __init__(self, value: object) -> None:
        self._value = value

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._value).encode()


@pytest.mark.parametrize(
    "provider_cls,options_cls",
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_openai_compatible_prepare_matches_served_model(
    monkeypatch, provider_cls, options_cls
):
    calls: list[tuple[str, float]] = []

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, timeout))
        return _Response(
            {
                "data": [
                    {"id": "other", "max_model_len": 2_048},
                    {"id": "served", "max_model_len": 65_536},
                    {"id": "bad", "max_model_len": 0},
                ]
            }
        )

    monkeypatch.setattr("linch.providers._context_probe.request.urlopen", fake_urlopen)
    provider = provider_cls(
        options_cls(
            base_url="https://server/v1",
            api_key="secret",
            context_window_timeout=0.25,
        )
    )

    assert provider.context_window("served") == 128_000
    await provider.prepare()
    await provider.prepare()
    assert provider.context_window("served") == 65_536
    assert provider.context_window("unknown") == 128_000
    assert len(calls) == 1
    assert calls[0] == ("https://server/v1/models", 0.25)


@pytest.mark.parametrize(
    "provider_cls,options_cls",
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_openai_compatible_explicit_context_skips_probe(
    monkeypatch, provider_cls, options_cls
):
    def fail_urlopen(req, timeout):
        raise AssertionError("probe should be skipped")

    monkeypatch.setattr("linch.providers._context_probe.request.urlopen", fail_urlopen)
    provider = provider_cls(options_cls(base_url="https://server", context_window=12_345))
    await provider.prepare()
    assert provider.context_window("served") == 12_345


@pytest.mark.parametrize(
    "provider_cls,options_cls",
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_openai_compatible_malformed_probe_url_falls_back(
    monkeypatch, provider_cls, options_cls
):
    provider = provider_cls(options_cls(base_url="http://[bad"))
    await provider.prepare()
    assert provider.context_window("served") == 128_000


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


@pytest.mark.parametrize("base_url", ["https://server", "https://server/v1"])
async def test_openai_compatible_probe_uses_headers_timeout_and_worker_thread(
    monkeypatch, base_url
):
    calls: list[tuple[str, float, str, str]] = []

    def fake_urlopen(req, timeout):
        calls.append(
            (
                req.full_url,
                timeout,
                req.headers.get("Authorization", ""),
                next(
                    (value for key, value in req.headers.items() if key.lower() == "x-test"),
                    "",
                ),
            )
        )
        assert threading.current_thread() is not threading.main_thread()
        return _Response({"data": [{"id": "m", "max_model_len": 77_000}]})

    monkeypatch.setattr("linch.providers._context_probe.request.urlopen", fake_urlopen)
    provider = VLLMProvider(
        VLLMProviderOptions(
            base_url=base_url,
            api_key="secret",
            default_headers={"X-Test": "yes"},
            context_window_timeout=0.75,
        )
    )
    await provider.prepare()
    assert provider.context_window("m") == 77_000
    expected_url = (
        f"{base_url.rstrip('/')}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
    )
    assert calls == [(expected_url, 0.75, "Bearer secret", "yes")]


async def test_openai_compatible_probe_failure_is_cached_and_auto_off_skips(monkeypatch):
    calls = 0

    def fail_urlopen(req, timeout):
        nonlocal calls
        calls += 1
        raise OSError("offline")

    monkeypatch.setattr("linch.providers._context_probe.request.urlopen", fail_urlopen)
    provider = SGLangProvider(SGLangProviderOptions(base_url="https://server"))
    await provider.prepare()
    await provider.prepare()
    assert calls == 1
    assert provider.context_window("m") == 128_000

    disabled = SGLangProvider(
        SGLangProviderOptions(base_url="https://server", auto_context_window=False)
    )
    await disabled.prepare()
    assert calls == 1
    assert disabled.context_window("m") == 128_000


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_openai_compatible_single_model_applies_to_alias(
    monkeypatch, provider_cls, options_cls
):
    def fake_urlopen(req, timeout):
        return _Response({"data": [{"id": "/root/models/local", "max_model_len": 262_144}]})

    monkeypatch.setattr("linch.providers._context_probe.request.urlopen", fake_urlopen)
    provider = provider_cls(options_cls(base_url="https://server/v1"))

    await provider.prepare()
    assert provider.context_window("public/alias") == 262_144


@pytest.mark.parametrize(
    ("provider_cls", "options_cls"),
    [(VLLMProvider, VLLMProviderOptions), (SGLangProvider, SGLangProviderOptions)],
)
async def test_openai_compatible_multi_model_does_not_guess_alias(
    monkeypatch, provider_cls, options_cls
):
    def fake_urlopen(req, timeout):
        return _Response(
            {"data": [{"id": "a", "max_model_len": 1_024}, {"id": "b", "max_model_len": None}]}
        )

    monkeypatch.setattr("linch.providers._context_probe.request.urlopen", fake_urlopen)
    provider = provider_cls(options_cls(base_url="https://server/v1"))

    await provider.prepare()
    assert provider.context_window("alias") == 128_000
