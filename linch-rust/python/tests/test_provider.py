"""Provider capabilities and model lists, asked of a real `linch-server`
before any agent is open, with a local mock of a vLLM server."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from scripted import config

from linch_rust import (
    ModelInfo,
    ModelNotFound,
    ModelsUnsupported,
    ProviderCapabilities,
    ProviderInfoError,
    ProviderRequestFailed,
    Server,
)
from linch_rust.errors import error_from

MODELS = {
    "object": "list",
    "data": [
        {"id": "qwen", "object": "model", "max_model_len": 262144},
        {"id": "small", "object": "model", "max_model_len": 8192},
    ],
}


@contextmanager
def mock(status: int, body: dict[str, Any]) -> Iterator[str]:
    """A server answering every GET with `status` and `body`; yields its `/v1` URL."""
    payload = json.dumps(body).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def closed_url() -> str:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}/v1"


def clean_env() -> dict[str, str]:
    """No provider key in the server's environment."""
    return {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}


async def test_a_client_asks_what_a_provider_supports_before_opening_an_agent() -> None:
    with mock(200, MODELS) as url:
        agent = config([], provider={"id": "vllm", "base_url": url}, spec={"model": "qwen"})
        async with Server(env=clean_env()) as server:
            caps = await server.provider_capabilities(agent)
            again = await server.provider_capabilities(agent["provider"], "small")

    assert isinstance(caps, ProviderCapabilities)
    assert (caps.provider, caps.model, caps.declared) == ("vllm", "qwen", True)
    assert (caps.context_window, caps.context_source) == (262144, "server")
    assert not caps.native_output_schema
    assert caps.tool_choice.named and caps.tool_choice.forced_refused_with == ()
    assert caps.thinking.disable and caps.thinking.effort and caps.thinking.reasoning_text
    assert not caps.thinking.budget
    assert (caps.vision, caps.prompt_cache, caps.stop_sequences) == (
        "model_dependent",
        "automatic",
        True,
    )
    assert again.model == "small"


async def test_a_cloud_provider_answers_capabilities_without_a_key() -> None:
    async with Server(env=clean_env()) as server:
        caps = await server.provider_capabilities({"id": "anthropic"}, "claude-sonnet-4-6")
        deepseek = await server.provider_capabilities({"id": "deepseek"}, "deepseek-flash")

    assert caps.prompt_cache == "explicit" and caps.tool_choice.forced_refused_with == ("budgeted",)
    assert caps.native_output_schema and caps.thinking.budget
    assert deepseek.tool_choice.forced_refused_with == ("unset", "adaptive", "budgeted")
    assert not deepseek.native_output_schema
    assert deepseek.context_source == "default"


async def test_the_model_list_and_one_models_info() -> None:
    with mock(200, MODELS) as url:
        provider = {"id": "vllm", "base_url": url}
        async with Server(env=clean_env()) as server:
            models = await server.list_models(provider)
            small = await server.model_info(provider, "small")
            with pytest.raises(ModelNotFound) as missing:
                await server.model_info(provider, "nope")
            with pytest.raises(ModelsUnsupported) as unsupported:
                await server.list_models({"id": "scripted", "turns": []})

    assert models == [
        ModelInfo(id="qwen", context_window=262144),
        ModelInfo(id="small", context_window=8192),
    ]
    assert small == ModelInfo(id="small", context_window=8192)
    assert missing.value.model == "nope"
    assert unsupported.value.provider == "scripted"


async def test_a_failed_list_is_typed_and_names_neither_key_nor_url() -> None:
    body = {"error": {"message": "rejected sk-python-secret-5"}}
    with mock(401, body) as url:
        async with Server(env={**clean_env(), "VLLM_API_KEY": "sk-python-secret-5"}) as server:
            with pytest.raises(ProviderRequestFailed) as refused:
                await server.list_models({"id": "vllm", "base_url": url})
    host = url.removeprefix("http://").removesuffix("/v1")
    assert (refused.value.kind, refused.value.status) == ("status", 401)
    assert "sk-python-secret-5" not in str(refused.value)
    assert host not in str(refused.value)

    dead = closed_url()
    async with Server(env=clean_env()) as server:
        with pytest.raises(ProviderRequestFailed) as unreachable:
            await server.list_models({"id": "vllm", "base_url": dead})
    assert unreachable.value.kind == "connect" and unreachable.value.status is None
    assert dead.removeprefix("http://").removesuffix("/v1") not in str(unreachable.value)


def test_the_new_codes_map_to_their_classes() -> None:
    for code, cls in ((-32035, ModelsUnsupported), (-32036, ModelNotFound)):
        assert type(error_from({"code": code, "message": "m"})) is cls
    failed = error_from({"code": -32037, "message": "m", "data": {"kind": "status", "status": 502}})
    assert isinstance(failed, ProviderRequestFailed) and failed.status == 502
    assert isinstance(failed, ProviderInfoError)
