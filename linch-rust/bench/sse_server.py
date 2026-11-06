"""Loopback server that replays one recorded-shape SSE stream per vendor.

    python sse_server.py --deltas N [--dump DIR]

Prints `PORT <n>` when it listens on 127.0.0.1. A POST under `/<vendor>/`
(`deepseek`, `openai-chat`, `openai-responses`, `anthropic`, `gemini`) gets
that vendor's stream: a text-only answer of N deltas in the vendor's wire
format, the same bytes every time, so every side decodes identical input.
`--dump` writes each stream and its size and event count (`streams.json`).
Standard library only; it is the "network", so its CPU is never counted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODELS = {
    "deepseek": "deepseek-flash",
    "openai-chat": "gpt-4.1-mini",
    "openai-responses": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.5-flash",
}


def frame(data: dict | str, event: str | None = None) -> str:
    body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {body}\n\n"


def words(n: int) -> list[str]:
    return [f"word{i} " for i in range(n)]


def chat(model: str, n: int) -> list[str]:
    """OpenAI Chat Completions chunks, as DeepSeek and OpenAI send them."""

    def chunk(delta: dict, finish: str | None = None, **extra) -> str:
        choice = {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
        base = {
            "id": "chatcmpl-bench",
            "object": "chat.completion.chunk",
            "created": 1762400000,
            "model": model,
            "choices": [choice],
        }
        return frame({**base, **extra})

    out = [chunk({"role": "assistant", "content": ""})]
    out += [chunk({"content": text}) for text in words(n)]
    out.append(chunk({}, "stop"))
    usage = {"prompt_tokens": 12, "completion_tokens": n, "total_tokens": 12 + n}
    final = {
        "id": "chatcmpl-bench",
        "object": "chat.completion.chunk",
        "created": 1762400000,
        "model": model,
        "choices": [],
        "usage": usage,
    }
    out.append(frame(final))
    out.append(frame("[DONE]"))
    return out


def responses(model: str, n: int) -> list[str]:
    """OpenAI Responses API events."""
    text = "".join(words(n))
    item = {"id": "msg_bench", "type": "message", "status": "in_progress", "role": "assistant"}
    response = {
        "id": "resp_bench",
        "object": "response",
        "created_at": 1762400000,
        "status": "in_progress",
        "model": model,
        "output": [],
    }
    part = {"type": "output_text", "text": "", "annotations": []}
    where = {"item_id": "msg_bench", "output_index": 0, "content_index": 0}
    seq = iter(range(1_000_000))

    def ev(kind: str, **body) -> str:
        return frame({"type": kind, "sequence_number": next(seq), **body}, kind)

    out = [
        ev("response.created", response=response),
        ev("response.in_progress", response=response),
        ev("response.output_item.added", output_index=0, item={**item, "content": []}),
        ev("response.content_part.added", **where, part=part),
    ]
    out += [ev("response.output_text.delta", **where, delta=w) for w in words(n)]
    done_part = {"type": "output_text", "text": text, "annotations": []}
    done_item = {**item, "status": "completed", "content": [done_part]}
    usage = {
        "input_tokens": 12,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": n,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 12 + n,
    }
    out += [
        ev("response.output_text.done", **where, text=text),
        ev("response.content_part.done", **where, part=done_part),
        ev("response.output_item.done", output_index=0, item=done_item),
        ev(
            "response.completed",
            response={**response, "status": "completed", "output": [done_item], "usage": usage},
        ),
    ]
    return out


def anthropic(model: str, n: int) -> list[str]:
    """Anthropic Messages events."""
    message = {
        "id": "msg_bench",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 1},
    }
    out = [
        frame({"type": "message_start", "message": message}, "message_start"),
        frame(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            "content_block_start",
        ),
    ]
    out += [
        frame(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w}},
            "content_block_delta",
        )
        for w in words(n)
    ]
    out += [
        frame({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        frame(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": n},
            },
            "message_delta",
        ),
        frame({"type": "message_stop"}, "message_stop"),
    ]
    return out


def gemini(model: str, n: int) -> list[str]:
    """Gemini `streamGenerateContent?alt=sse` chunks."""
    out = []
    texts = words(n)
    for i, text in enumerate(texts):
        candidate = {"content": {"parts": [{"text": text}], "role": "model"}, "index": 0}
        chunk = {"candidates": [candidate], "modelVersion": model, "responseId": "bench"}
        if i == len(texts) - 1:
            candidate["finishReason"] = "STOP"
            chunk["usageMetadata"] = {
                "promptTokenCount": 12,
                "candidatesTokenCount": n,
                "totalTokenCount": 12 + n,
            }
        out.append(frame(chunk))
    return out


BUILDERS = {
    "deepseek": chat,
    "openai-chat": chat,
    "openai-responses": responses,
    "anthropic": anthropic,
    "gemini": gemini,
}


def streams(n: int) -> dict[str, tuple[bytes, int]]:
    """Each vendor's stream bytes and SSE event count."""
    out = {}
    for vendor, build in BUILDERS.items():
        frames = build(MODELS[vendor], n)
        out[vendor] = ("".join(frames).encode(), len(frames))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deltas", type=int, default=20_000)
    parser.add_argument("--dump", default="")
    args = parser.parse_args()
    data = streams(args.deltas)
    if args.dump:
        dump = Path(args.dump)
        dump.mkdir(parents=True, exist_ok=True)
        info = {}
        for vendor, (body, events) in data.items():
            (dump / f"{vendor}.sse").write_bytes(body)
            info[vendor] = {
                "bytes": len(body),
                "events": events,
                "sha256": hashlib.sha256(body).hexdigest(),
                "model": MODELS[vendor],
                "text_chars": len("".join(words(args.deltas))),
            }
        (dump / "streams.json").write_text(json.dumps(info, indent=2))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            vendor = self.path.strip("/").split("/", 1)[0]
            if vendor not in data:
                self.send_error(404)
                return
            body = data[vendor][0]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A client that fails mid-stream hangs up; its side reports it.
                self.close_connection = True

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"PORT {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
