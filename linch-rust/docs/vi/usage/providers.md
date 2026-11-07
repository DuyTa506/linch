# Provider

[Bản gốc tiếng Anh](../../usage/providers.md)

Dùng trang này để chọn provider cho model, cấp key cho server, và kiểm thử ngoại
tuyến bằng provider scripted.

[<- Hướng dẫn sử dụng](./README.md)

Trường `provider` của [config agent](./agent.md) chọn provider theo `id`. Bản thân
model là `spec.model`, được gửi kèm mọi request. Các adapter chạy bên trong
server; cách mỗi adapter ánh xạ request, thinking và usage nằm ở
[trang providers của kiến trúc](../architecture/providers.md).

---

## Chọn provider

| `id` | Biến chứa key | Endpoint mặc định | Trường bổ sung |
|---|---|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` | `base_url`, `context_window` |
| `openai-chat` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `base_url`, `context_window` |
| `openai-responses` | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `base_url`, `reasoning_summary`, `store`, `native_catalog_changes` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL`, nếu không thì `https://api.anthropic.com` | `base_url`, `native_catalog_changes` |
| `gemini` | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com` | `base_url` |
| `vllm` | `VLLM_API_KEY`, tùy chọn | không có | `base_url`, bắt buộc; `context_window` |
| `sglang` | `SGLANG_API_KEY`, tùy chọn | không có | `base_url`, bắt buộc; `context_window` |
| `llamacpp` | `LLAMA_API_KEY`, tùy chọn | không có | `base_url`, bắt buộc; `context_window` |
| `scripted` | không có | không có | `turns`, bắt buộc; `context_window` |

`base_url` thay thế endpoint của provider. Mọi trường khác đều bị từ chối lúc mở
bằng một `ProtocolError` (code -32602), nên một config không bao giờ có thể chứa
`api_key`.

### Context window

Mỗi provider biết context window của model trong spec, tính bằng token. Nó quyết
định khi nào compaction chạy nếu không có `threshold_tokens`, ngưỡng offload mà
`filesystem.offload_threshold_fraction` đưa ra, và danh sách skill được dài bao
nhiêu ([Config của agent](./agent.md#compaction)). `context_window` thay thế nó
cho mọi model. Khi không có, nó là giá trị của Python Linch:

| `id` | Cửa sổ |
|---|---|
| `anthropic` | 200000 |
| `gemini` | 2000000 với `gemini-1.5-pro`, còn lại 1048576 |
| `openai-responses` | `gpt-5` 400000, `gpt-4.1` 1000000, `o1` 200000, còn lại 128000 |
| `openai-chat` | `gpt-5.5` và `gpt-5.4` 400000, `o1` 200000, còn lại 128000 |
| `deepseek`, `scripted` | 128000 |
| `vllm`, `sglang` | Được hỏi khi agent mở: `max_model_len` của model trên `GET /v1/models` (server có một model thì trả lời cho mọi tên), nếu không thì 128000 |
| `llamacpp` | Được hỏi khi agent mở: `n_ctx` trên `GET /props`, nếu không thì 128000 |

Server chỉ được hỏi một lần, tối đa 2 giây, với cùng key như các lời gọi của nó;
server nào không trả lời sẽ để lại 128000 và việc mở agent vẫn tiếp tục.

Một provider trực tuyến, ví dụ Anthropic (cần `ANTHROPIC_API_KEY`; việc mở agent
đã được kiểm tra với key giả, lời gọi thì chưa):

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": "claude-sonnet-4-6", "max_output_tokens": 1024},
    "provider": {"id": "anthropic"},
}


async def main() -> None:
    # The server reads ANTHROPIC_API_KEY from the environment it inherits.
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Name one prime number.")
        result = await run.result()
        if isinstance(result, ResultEvent):
            print(result.final_text, result.total_cost_usd)


asyncio.run(main())
```

## Key lấy từ môi trường của server

Server đọc key của provider khi agent được mở. Key bị thiếu hoặc để trống làm việc
mở thất bại với `OpenError` (code -32013) mà `data` của nó nêu tên biến:

```text
provider anthropic needs its key in the environment variable ANTHROPIC_API_KEY (code -32013)
```

`Server()` truyền môi trường của process này cho server. `Server(env=...)` truyền
đúng mapping bạn đưa và không gì khác, giúp giữ key ra khỏi môi trường của process
của bạn:

```python
import asyncio
import os

from linch_rust import AgentConfig, Server


def load_key() -> str:
    # Stand-in for your secret store.
    return "sk-not-real"


config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": "deepseek-chat"},
    "provider": {"id": "deepseek"},
}


async def main() -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DEEPSEEK_API_KEY": load_key()}
    async with Server(env=env) as server:
        agent = await server.open_agent(config)
        print("opened", agent.handle)


asyncio.run(main())
```

Việc mở chỉ dựng client; không có request nào được gửi cho tới khi một run gọi
model, nên key sai sẽ hiện ra dưới dạng event `error` trong run đầu tiên, không
phải lúc mở.

Server giữ key tránh xa các process mà nó khởi động (`Bash`, stdio MCP server) và,
trên Linux, tự đánh dấu là không thể dump để một child chạy cùng user không đọc
được `/proc/<pid>/environ` của nó. Process của bạn không có sự bảo vệ như vậy: key
trong môi trường của chính nó vẫn đọc được với mọi process của cùng user, kể cả
các child của server. Khi agent chạy các lệnh không đáng tin, hãy đọc key từ một
secret store và đưa chúng cho server qua `env`.

## Provider hosted

### `deepseek`

Chat Completions API của DeepSeek. DeepSeek suy nghĩ theo mặc định; đặt
`spec.thinking` thành `{"type": "disabled"}` để tắt.

```python
{"id": "deepseek"}
```

### `openai-chat`

OpenAI Chat Completions. Với `base_url`, nó cũng kết nối được tới một gateway nói
cùng API đó bằng key OpenAI:

```python
{"id": "openai-chat"}
{"id": "openai-chat", "base_url": "https://gateway.example.com/v1"}
```

Server vLLM, SGLang hoặc llama.cpp tự triển khai có `id` riêng bên dưới; đừng trỏ
`openai-chat` vào chúng, vì mỗi server khác OpenAI ở các trường mà nó đọc.

### `openai-responses`

OpenAI Responses API. Mỗi request mang toàn bộ lịch sử; không gửi
`previous_response_id`.

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `reasoning_summary` | không có | Stream bản tóm tắt suy luận như thinking: `auto`, `concise` hoặc `detailed` |
| `store` | `True` | OpenAI có lưu từng phản hồi hay không. `False` gửi `store: false` và yêu cầu suy luận được mã hóa thay vào đó |
| `native_catalog_changes` | `False` | Gửi tool được thêm hoặc bỏ giữa chừng một session trong input dưới dạng `additional_tools`, với lựa chọn `allowed_tools`, để prompt cache được giữ. Chỉ với gpt-5.4 trở lên; chưa được chạy với API thật |

```python
{"id": "openai-responses", "reasoning_summary": "concise", "store": False}
```

Dùng `spec.effort` (`low` đến `max`) cho độ sâu suy luận; các adapter OpenAI bỏ
qua `spec.thinking`.

### `anthropic`

Anthropic Messages API. Endpoint là `base_url`, nếu không thì `ANTHROPIC_BASE_URL`
của server, nếu không thì endpoint công khai. API yêu cầu `max_tokens`, và nó mặc
định là 8192 khi không có `spec.max_output_tokens`.

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `native_catalog_changes` | `False` | Gửi tool được thêm hoặc bỏ giữa chừng một session trong cuộc hội thoại dưới dạng tool block (beta `inline-tools-2026-09-15`), để prompt cache được giữ. Chỉ trên `api.anthropic.com` với các model hỗ trợ; chưa được chạy với API thật |

```python
{"id": "anthropic"}
```

### `gemini`

Gemini `streamGenerateContent` với API key (`GEMINI_API_KEY`). Credential của
Vertex AI không được hỗ trợ. Adapter này chưa được chạy với key thật.

```python
{"id": "gemini"}
```

## Server tự triển khai

vLLM, SGLang và llama.cpp mỗi loại có một profile riêng: trường nào mang suy luận,
dạng `tool_choice` nào hoạt động, trường giới hạn đầu ra nào được gửi và cách tắt
thinking. Một profile không sao chép gì từ profile của OpenAI. Bảng về những gì
mỗi loại hỗ trợ nằm ở
[trang providers của kiến trúc](../architecture/providers.md#openai-chat-completions-and-server-profiles).

`base_url` là bắt buộc và gồm cả `/v1`. Key là tùy chọn: chỉ đặt nó trong môi
trường của server khi inference server được khởi động cùng một key.

```python
{"id": "vllm", "base_url": "http://127.0.0.1:8000/v1"}      # VLLM_API_KEY
{"id": "sglang", "base_url": "http://127.0.0.1:30000/v1"}   # SGLANG_API_KEY
{"id": "llamacpp", "base_url": "http://127.0.0.1:8080/v1"}  # LLAMA_API_KEY
```

`spec.model` là tên mà inference server phục vụ, ví dụ `Qwen/Qwen3-8B`.
`{"type": "disabled"}` trong `spec.thinking` gửi công tắc tắt thinking của server
đó; mọi giá trị khác để lại mặc định của chat template.

<a id="the-scripted-provider"></a>

## Provider scripted

`scripted` phát lại một danh sách các lượt, mỗi lượt cho một lời gọi provider theo
thứ tự gọi, kể cả các lời gọi do subagent thực hiện. Dùng nó trong test và khi
chưa có key. `linch_rust.scripted` dựng các lượt:

| Tên | Là gì |
|---|---|
| `MODEL` | `"scripted-model"`, một tên model cho `spec.model` |
| `text_turn(text, model=MODEL)` | Một lượt trả lời `text`; run kết thúc ở đó |
| `tool_turn(call_id, name, input, model=MODEL)` | Một lượt gọi tool `name` một lần với `input`, dưới dạng lời gọi `call_id` |

```python
from linch_rust import AgentConfig
from linch_rust.scripted import MODEL, text_turn, tool_turn

config: AgentConfig = {
    "namespace": "test",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "lookup", {"email": "ada@example.com"}), text_turn("Found.")],
    },
}
```

Mỗi lượt là `{"events": [...]}`, tức stream event của provider cho một phản hồi
(`message_start`, `text_delta`, `tool_use_start`, `tool_use_input_delta`,
`tool_use_end`, `message_end` và `thinking_delta`); các trường của event nằm ở
[trang providers của kiến trúc](../architecture/providers.md#event-vocabulary).
`model` trong `message_start` của một lượt là model đã phục vụ lời gọi, nên
`text_turn("Hi.", model="claude-sonnet-4-6")` thử được các trường chi phí.

Một lượt cũng có thể kết thúc bằng một `error`, để kiểm thử cách host xử lý một
lời gọi thất bại: `transport` (`message`), `status` (`status`, `message`,
`retry_after_ms?`) hoặc `invalid_request` (`message`). Khi không có `events`, lời
gọi thất bại trước khi có bất kỳ đầu ra nào:

```python
import asyncio

from linch_rust import AgentConfig, ErrorEvent, ResultEvent, Server
from linch_rust.scripted import MODEL

config: AgentConfig = {
    "namespace": "test",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [{"error": {"type": "status", "status": 503, "message": "busy"}}],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        async for envelope in await agent.run("s1", "Hi"):
            if isinstance(envelope.event, ErrorEvent | ResultEvent):
                print(envelope.event)


asyncio.run(main())
```

```text
ErrorEvent(error={'name': 'ProviderError', 'kind': 'status', 'message': 'provider returned HTTP 503: busy', 'retryable': True, 'status': 503})
ResultEvent(subtype='error', stop_reason='error', total_usage={...}, usage_complete=False, ...)
```

Một run cần nhiều lượt hơn số lượt trong script sẽ kết thúc bằng một event `error`
(`scripted provider ran out of turns after N`) và một `result` có subtype `error`.
Script không thuộc hợp đồng của run, nên một test có thể resume một run trên
server mới mà script của nó chỉ chứa các lượt còn lại.

## Mở mọi provider ngoại tuyến

Ví dụ này mở một agent cho mỗi provider với key giả. Nó kiểm tra từng config mà
không gọi mạng:

```python
import asyncio

from linch_rust import AgentConfig, Server

# Fake keys: opening builds each client without a network call.
FAKE_ENV = {
    "DEEPSEEK_API_KEY": "sk-not-real",
    "OPENAI_API_KEY": "sk-not-real",
    "ANTHROPIC_API_KEY": "sk-not-real",
    "GEMINI_API_KEY": "not-real",
}

PROVIDERS = [
    ("deepseek-chat", {"id": "deepseek"}),
    ("gpt-5", {"id": "openai-chat"}),
    ("gpt-5", {"id": "openai-responses", "reasoning_summary": "concise", "store": False}),
    ("claude-sonnet-4-5", {"id": "anthropic"}),
    ("gemini-2.5-flash", {"id": "gemini"}),
    ("Qwen/Qwen3-8B", {"id": "vllm", "base_url": "http://127.0.0.1:8000/v1"}),
    ("Qwen/Qwen3-8B", {"id": "sglang", "base_url": "http://127.0.0.1:30000/v1"}),
    ("local", {"id": "llamacpp", "base_url": "http://127.0.0.1:8080/v1"}),
    ("gpt-5", {"id": "openai-chat", "base_url": "https://gateway.example.com/v1"}),
]


async def main() -> None:
    async with Server(env=FAKE_ENV) as server:
        for model, provider in PROVIDERS:
            config: AgentConfig = {"namespace": "demo", "spec": {"model": model}, "provider": provider}
            agent = await server.open_agent(config)
            print(provider["id"], agent.handle)


asyncio.run(main())
```

## Lỗi và thử lại

Không có gì thử lại một lời gọi provider: không phải adapter, cũng không phải run.
Một lời gọi thất bại (lỗi kết nối, HTTP status, một stream vi phạm quy tắc của
nó) kết thúc run bằng một event `error` và một `result` có subtype `error`. Dict
`error` của event lỗi mang `retryable` và, với lỗi HTTP, cả `status`, để bạn quyết
định có bắt đầu một run mới hay không. Một lời gọi bị hủy kết thúc run ở trạng thái
`aborted`. Mỗi adapter có timeout kết nối 30 giây và timeout đọc giữa các lần đọc
(300 giây hoặc 600 giây); không có hạn chót cho cả một lời gọi.

## Chi phí

Các event usage và result mang `cost_usd`, `cumulative_cost_usd` và
`total_cost_usd` khi model phục vụ có giá. Bảng giá tích hợp sẵn chỉ chứa các
model của Anthropic; mọi model khác báo `None`. Xem
[Event](./events.md#mức-sử-dụng-và-chi-phí).

## Các trang liên quan

- [Config của agent](./agent.md#spec): `thinking`, `effort`, `max_output_tokens`.
- [Event](./events.md): các event `error`, `usage` và `result`.
- [Khắc phục sự cố](./troubleshooting.md#opening-an-agent): lỗi khi mở agent.
