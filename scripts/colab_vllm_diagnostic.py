"""Diagnose the usage=0 and reasoning-separation findings against real vLLM.

Uses the RAW openai client (not linch) to characterize server behavior:
  A. chunk structure — where does usage live? does reasoning_content stream?
  B. prefix caching — does vLLM report prompt_tokens_details.cached_tokens?
  C. reasoning — non-stream + stream, with enable_thinking on, is
     message.reasoning_content populated?
This tells us if the linch fix (capture usage on empty-choices chunk) is correct.
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
REPO = "https://github.com/DuyTa506/linch.git"
BRANCH = "feat/vllm-sglang-providers"


def sh(cmd):
    return subprocess.run(cmd, shell=True, text=True)


def log(*a):
    print(*a, flush=True)


t0 = time.time()
sh(f"{sys.executable} -m pip install -q uv")
sh(
    f"{sys.executable} -m uv pip install --system -q "
    "--reinstall-package torch --reinstall-package torchvision "
    "--reinstall-package torchaudio "
    f"vllm==0.23.0 'huggingface_hub[hf_transfer]' 'git+{REPO}@{BRANCH}' "
    "--torch-backend=cu130"
)
log(f"[install] {time.time() - t0:.0f}s")

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
from huggingface_hub import snapshot_download  # noqa: E402

local = snapshot_download(MODEL, ignore_patterns=["*.pt", "*.gguf", "original/*"])
log("[download] done")

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
    "--reasoning-parser",
    "qwen3",
    "--enable-auto-tool-choice",
    "--tool-call-parser",
    "qwen3_xml",
]
logf = open("/content/vllm.log", "w")
proc = subprocess.Popen(serve_cmd, stdout=logf, stderr=subprocess.STDOUT)


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


ts = time.time()
while time.time() - ts < 900:
    if proc.poll() is not None:
        log(f"[serve] DIED rc={proc.returncode}")
        print("".join(open("/content/vllm.log").readlines()[-60:]))
        sys.exit(1)
    if health():
        log(f"[serve] READY in {time.time() - ts:.0f}s")
        break
    time.sleep(3)

import asyncio  # noqa: E402

from openai import AsyncOpenAI  # noqa: E402

client = AsyncOpenAI(api_key="EMPTY", base_url=f"http://127.0.0.1:{PORT}/v1")
out = {}


async def main():
    # A. chunk structure with include_usage + thinking on
    msgs = [
        {"role": "system", "content": "You are a careful math tutor."},
        {"role": "user", "content": "What is 17 * 23? Think step by step."},
    ]
    n = 0
    n_with_choices = 0
    n_empty_choices = 0
    usage_on_empty = None
    usage_on_choice = None
    saw_reasoning_delta = False
    stream = await client.chat.completions.create(
        model=SERVED,
        messages=msgs,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=512,
        temperature=0.0,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    async for ch in stream:
        n += 1
        chs = ch.choices or []
        u = getattr(ch, "usage", None)
        if chs:
            n_with_choices += 1
            if getattr(chs[0].delta, "reasoning_content", None):
                saw_reasoning_delta = True
            if u is not None:
                usage_on_choice = {"prompt": u.prompt_tokens, "completion": u.completion_tokens}
        else:
            n_empty_choices += 1
            if u is not None:
                usage_on_empty = {"prompt": u.prompt_tokens, "completion": u.completion_tokens}
    out["A_chunks"] = {
        "total": n,
        "with_choices": n_with_choices,
        "empty_choices": n_empty_choices,
        "usage_on_empty_choices_chunk": usage_on_empty,
        "usage_on_content_chunk": usage_on_choice,
        "streamed_reasoning_content": saw_reasoning_delta,
    }

    # B. prefix caching — two identical big-prefix calls, read cached_tokens
    big = "You are an assistant. " + "Remember this context carefully. " * 400
    cmsgs = [{"role": "system", "content": big}, {"role": "user", "content": "Reply: OK"}]

    async def once():
        r = await client.chat.completions.create(
            model=SERVED,
            messages=cmsgs,
            max_tokens=4,
            temperature=0.0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        u = r.usage
        det = getattr(u, "prompt_tokens_details", None)
        cached = getattr(det, "cached_tokens", None) if det else None
        return {"prompt": u.prompt_tokens, "cached_tokens": cached}

    out["B_caching"] = {"run1": await once(), "run2": await once()}

    # C. reasoning — non-stream, does message.reasoning_content populate?
    r = await client.chat.completions.create(
        model=SERVED,
        messages=msgs,
        max_tokens=512,
        temperature=0.0,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    m = r.choices[0].message
    rc = getattr(m, "reasoning_content", None)
    out["C_reasoning"] = {
        "reasoning_content_present": bool(rc),
        "reasoning_content_chars": len(rc) if rc else 0,
        "reasoning_head": (rc or "")[:200],
        "content_head": (m.content or "")[:200],
    }
    return out


try:
    res = asyncio.run(main())
    print("===DIAG_JSON===", flush=True)
    print(json.dumps(res, indent=2, default=str), flush=True)
    print("===END_DIAG===", flush=True)
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
    log("[done]")
