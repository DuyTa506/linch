"""Does SGLang report prompt-cache hits, and does linch's SGLangProvider see them?

Serves a standard model with RadixAttention (default) + --enable-cache-report,
then on an identical long prefix sent twice measures cached_tokens via:
  A. raw openai client, non-streaming
  B. raw openai client, streaming + stream_options.include_usage
  C. linch SGLangProvider (include_stream_options=False, enable_cache_report=True)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

MODEL = os.environ.get("LINCH_TEST_MODEL", "Qwen/Qwen3-4B")
SERVED = "qwen"
PORT = 30000
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
    f"'sglang[all]' 'huggingface_hub[hf_transfer]' 'git+{REPO}@{BRANCH}' "
    "--torch-backend=cu126"
)
log(f"[install] {time.time() - t0:.0f}s")

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
os.environ["SGLANG_DISABLE_CUDNN_CHECK"] = "1"
from huggingface_hub import snapshot_download  # noqa: E402

local = snapshot_download(MODEL, ignore_patterns=["*.pt", "*.gguf", "original/*"])
log("[download] done")

serve_cmd = [
    sys.executable,
    "-m",
    "sglang.launch_server",
    "--model-path",
    local,
    "--served-model-name",
    SERVED,
    "--host",
    "127.0.0.1",
    "--port",
    str(PORT),
    "--mem-fraction-static",
    "0.85",
    "--context-length",
    "16384",
    "--trust-remote-code",
    "--disable-cuda-graph",
    "--enable-cache-report",  # surfaces cached_tokens in usage.prompt_tokens_details
]
logf = open("/content/sglang.log", "w")
log(f"[serve] {' '.join(serve_cmd)}")
proc = subprocess.Popen(serve_cmd, stdout=logf, stderr=subprocess.STDOUT)


def ready():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health_generate", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


ts = time.time()
ok = False
while time.time() - ts < 900:
    if proc.poll() is not None:
        log(f"[serve] DIED rc={proc.returncode}")
        print("".join(open("/content/sglang.log").readlines()[-60:]))
        sys.exit(1)
    if ready():
        ok = True
        log(f"[serve] READY in {time.time() - ts:.0f}s")
        break
    time.sleep(4)
if not ok:
    sys.exit(1)

import asyncio  # noqa: E402

from openai import AsyncOpenAI  # noqa: E402

from linch import SGLangProvider, SGLangProviderOptions  # noqa: E402
from linch.types import Message, ProviderRequest, SystemBlock, TextBlock  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}/v1"
client = AsyncOpenAI(api_key="EMPTY", base_url=BASE)
BIG = "You are an assistant. " + "Remember this context carefully. " * 500
MSGS = [{"role": "system", "content": BIG}, {"role": "user", "content": "Reply: OK"}]
EB = {"enable_cache_report": True, "chat_template_kwargs": {"enable_thinking": False}}


async def main():
    out = {}

    # A. raw, non-streaming
    async def nonstream():
        r = await client.chat.completions.create(
            model=SERVED, messages=MSGS, max_tokens=4, temperature=0.0, extra_body=EB
        )
        u = r.usage
        det = getattr(u, "prompt_tokens_details", None)
        return {"prompt": u.prompt_tokens, "cached_tokens": getattr(det, "cached_tokens", None)}

    out["A_raw_nonstream"] = {"run1": await nonstream(), "run2": await nonstream()}

    # B. raw, streaming + include_usage
    async def streamed():
        s = await client.chat.completions.create(
            model=SERVED,
            messages=MSGS,
            max_tokens=4,
            temperature=0.0,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=EB,
        )
        usage = None
        async for ch in s:
            if getattr(ch, "usage", None) is not None:
                usage = ch.usage
        if usage is None:
            return {"usage_in_stream": False}
        det = getattr(usage, "prompt_tokens_details", None)
        return {
            "usage_in_stream": True,
            "prompt": usage.prompt_tokens,
            "cached_tokens": getattr(det, "cached_tokens", None),
        }

    out["B_raw_stream"] = {"run1": await streamed(), "run2": await streamed()}

    async def via_linch(prov):
        req = ProviderRequest(
            model=SERVED,
            system=[SystemBlock(text=BIG)],
            tools=[],
            messages=[Message(role="user", content=[TextBlock(text="Reply: OK")])],
            max_output_tokens=4,
            temperature=0.0,
        )
        usage = None
        async for ev in prov.stream(req):
            if ev["type"] == "message_end":
                usage = ev["usage"]
        return {"input": usage.input_tokens, "cache_read": usage.cache_read_tokens}

    # C. linch SGLangProvider with default stream_options OFF + enable_cache_report
    prov_off = SGLangProvider(
        SGLangProviderOptions(
            api_key="EMPTY",
            base_url=BASE,
            context_window=16384,
            enable_cache_report=True,
            include_stream_options=False,
        )
    )
    out["C_linch_stream_options_off"] = {
        "run1": await via_linch(prov_off),
        "run2": await via_linch(prov_off),
    }

    # D. linch SGLangProvider with include_stream_options=True + enable_cache_report
    prov_on = SGLangProvider(
        SGLangProviderOptions(
            api_key="EMPTY",
            base_url=BASE,
            context_window=16384,
            enable_cache_report=True,
            include_stream_options=True,
        )
    )
    out["D_linch_stream_options_on"] = {
        "run1": await via_linch(prov_on),
        "run2": await via_linch(prov_on),
    }
    return out


try:
    res = asyncio.run(main())
    print("===CACHE_JSON===", flush=True)
    print(json.dumps(res, indent=2, default=str), flush=True)
    print("===END_CACHE===", flush=True)
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
    log("[done]")
