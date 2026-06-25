"""Run linch's VLLMProvider against a real vLLM server on a Colab A100.

Single-shot: install cu13 stack + linch(branch) -> download model ->
vllm serve -> drive VLLMProvider for reasoning / tool-calling /
structured-output / prompt-caching -> print JSON results -> teardown.
Everything must happen in one process (Colab runtime is ephemeral per call).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

MODEL = "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"
SERVED = "qwen35"
PORT = 8000
BRANCH = "feat/vllm-sglang-providers"
REPO = "https://github.com/DuyTa506/linch.git"


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, text=True, **kw)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- install
t0 = time.time()
sh(f"{sys.executable} -m pip install -q uv")
rc = sh(
    f"{sys.executable} -m uv pip install --system -q "
    "--reinstall-package torch --reinstall-package torchvision "
    "--reinstall-package torchaudio "
    f"vllm==0.23.0 hf_transfer 'huggingface_hub[hf_transfer]' "
    f"'git+{REPO}@{BRANCH}' "
    "--torch-backend=cu130"
).returncode
log(f"[install] rc={rc} in {time.time() - t0:.0f}s")

import torch  # noqa: E402

log(
    f"[install] torch={torch.__version__} cuda={torch.version.cuda} "
    f"avail={torch.cuda.is_available()}"
)
import vllm._C  # noqa: F401,E402  # gate: cu13 ops load

import linch  # noqa: E402

log(f"[install] linch ok, VLLMProvider={'VLLMProvider' in linch.__all__}")

# ------------------------------------------------------------- parsers
# Validated against vllm 0.23.0: reasoning parser module `qwen3`, tool parser
# `qwen3_xml` is the Qwen3.x choice from the CLI's enforced list.
RP = "qwen3"
TP = "qwen3_xml"
log(f"[parsers] reasoning={RP} tool={TP}")

# ---------------------------------------------------------------- download
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
from huggingface_hub import snapshot_download  # noqa: E402

td = time.time()
local = snapshot_download(MODEL, ignore_patterns=["*.pt", "*.gguf", "original/*"])
log(f"[download] {MODEL} -> {local} in {time.time() - td:.0f}s")

# ---------------------------------------------------------------- serve
serve_cmd = [
    sys.executable,
    "-m",
    "vllm.entrypoints.openai.api_server",
    "--model",
    local,
    "--served-model-name",
    SERVED,
    "--port",
    str(PORT),
    "--gpu-memory-utilization",
    "0.90",
    "--max-model-len",
    "16384",
    "--max-num-seqs",
    "8",
    "--enforce-eager",
    "--trust-remote-code",
]
if RP:
    serve_cmd += ["--reasoning-parser", RP]
if TP:
    serve_cmd += ["--enable-auto-tool-choice", "--tool-call-parser", TP]

logf = open("/content/vllm.log", "w")
log(f"[serve] launching: {' '.join(serve_cmd)}")
proc = subprocess.Popen(serve_cmd, stdout=logf, stderr=subprocess.STDOUT)


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


ts = time.time()
ready = False
while time.time() - ts < 900:
    if proc.poll() is not None:
        log(f"[serve] DIED rc={proc.returncode} after {time.time() - ts:.0f}s")
        break
    if health():
        ready = True
        log(f"[serve] READY in {time.time() - ts:.0f}s")
        break
    time.sleep(3)

if not ready:
    log("[serve] NOT READY — last 80 log lines:")
    logf.flush()
    print("".join(open("/content/vllm.log").readlines()[-80:]), flush=True)
    if proc.poll() is None:
        proc.terminate()
    sys.exit(1)

# ---------------------------------------------------------------- tests
import asyncio  # noqa: E402

from linch import VLLMProvider, VLLMProviderOptions  # noqa: E402
from linch.types import (  # noqa: E402
    Message,
    OutputSchema,
    ProviderRequest,
    SystemBlock,
    TextBlock,
)

BASE = f"http://127.0.0.1:{PORT}/v1"


def provider(extra_body=None):
    return VLLMProvider(
        VLLMProviderOptions(
            api_key="EMPTY", base_url=BASE, context_window=16384, extra_body=extra_body
        )
    )


def req(system, user, **kw):
    return ProviderRequest(
        model=SERVED,
        system=[SystemBlock(text=system)] if system else [],
        tools=kw.pop("tools", []),
        messages=[Message(role="user", content=[TextBlock(text=user)])],
        **kw,
    )


async def drive(prov, request):
    out = {"text": "", "thinking": "", "tools": [], "usage": None, "stop": None}
    async for ev in prov.stream(request):
        t = ev["type"]
        if t == "thinking_delta":
            out["thinking"] += ev["text"]
        elif t == "text_delta":
            out["text"] += ev["text"]
        elif t == "tool_use_start":
            out["tools"].append({"id": ev["id"], "name": ev["name"], "input": ""})
        elif t == "tool_use_input_delta":
            out["tools"][-1]["input"] += ev["json_delta"]
        elif t == "message_end":
            out["usage"] = ev["usage"]
            out["stop"] = ev["stop_reason"]
    return out


THINK_ON = {"chat_template_kwargs": {"enable_thinking": True}}
THINK_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


async def main():
    results = {}

    # T1 reasoning + basic correctness
    r = await drive(
        provider(THINK_ON),
        req(
            "You are a careful math tutor.",
            "What is 17 * 23? Show brief reasoning then the answer.",
            max_output_tokens=1024,
            temperature=0.0,
        ),
    )
    results["reasoning"] = {
        "has_reasoning": len(r["thinking"]) > 0,
        "reasoning_chars": len(r["thinking"]),
        "answer_correct": "391" in r["text"],
        "text_tail": r["text"][-160:],
        "thinking_head": r["thinking"][:160],
        "usage": {
            "input": r["usage"].input_tokens,
            "output": r["usage"].output_tokens,
            "cache_read": r["usage"].cache_read_tokens,
        },
    }

    # T2 tool calling
    tools = [
        {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    r = await drive(
        provider(THINK_OFF),
        req(
            "You can call tools.",
            "Use the get_weather tool for Paris.",
            tools=tools,
            tool_choice="auto",
            max_output_tokens=512,
            temperature=0.0,
        ),
    )
    parsed = None
    if r["tools"]:
        try:
            parsed = json.loads(r["tools"][0]["input"])
        except Exception:
            parsed = r["tools"][0]["input"]
    results["tool_calling"] = {
        "called": bool(r["tools"]),
        "name": r["tools"][0]["name"] if r["tools"] else None,
        "args": parsed,
        "city_is_paris": isinstance(parsed, dict) and parsed.get("city", "").lower() == "paris",
        "stop": r["stop"],
    }

    # T3 structured output
    schema = OutputSchema(
        name="person",
        schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name", "age"],
            "additionalProperties": False,
        },
        strict=True,
    )
    r = await drive(
        provider(THINK_OFF),
        req(
            "Return only JSON.",
            "Create a person named Alice aged 30.",
            output_schema=schema,
            max_output_tokens=256,
            temperature=0.0,
        ),
    )
    obj = None
    try:
        obj = json.loads(r["text"])
    except Exception:
        pass
    results["structured_output"] = {
        "valid_json": obj is not None,
        "matches_schema": isinstance(obj, dict)
        and set(obj) == {"name", "age"}
        and isinstance(obj.get("age"), int),
        "value": obj,
        "raw_tail": r["text"][-120:],
    }

    # T4 prompt caching — identical long prefix twice; 2nd should hit prefix cache
    big = "You are an assistant. " + "Remember this context carefully. " * 400
    p = provider(THINK_OFF)
    r1 = await drive(p, req(big, "Reply with exactly: OK", max_output_tokens=8, temperature=0.0))
    r2 = await drive(p, req(big, "Reply with exactly: OK", max_output_tokens=8, temperature=0.0))
    results["prompt_caching"] = {
        "run1": {"input": r1["usage"].input_tokens, "cache_read": r1["usage"].cache_read_tokens},
        "run2": {"input": r2["usage"].input_tokens, "cache_read": r2["usage"].cache_read_tokens},
        "cache_hit_on_run2": r2["usage"].cache_read_tokens > 0,
    }

    return results


try:
    res = asyncio.run(main())
    print("===RESULTS_JSON===", flush=True)
    print(json.dumps(res, indent=2, default=str), flush=True)
    print("===END_RESULTS===", flush=True)
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
    log("[done] server torn down")
