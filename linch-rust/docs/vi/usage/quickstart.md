# Bắt đầu nhanh

[Bản gốc tiếng Anh](../../usage/quickstart.md)

Dùng lộ trình này để chạy agent đầu tiên ngoại tuyến, sau đó thêm provider trực
tuyến, host tool, phê duyệt và lưu trữ bền vững bằng Postgres.

[<- Hướng dẫn sử dụng](./README.md)

Bạn viết Python; vòng lặp agent chạy trong `linch-server`, một sidecar Rust mà
client khởi động cho bạn dưới dạng child process. Không có gì trên trang này cần
Rust, trừ việc build từ checkout.

## 1. Cài đặt

Wheel `linch-rust` chứa client Python và binary `linch-server` cho nền tảng của
nó. Cần Python 3.11 trở lên. Với bản phát hành đã công bố:

```bash
pip install linch-rust              # client and server binary
pip install 'linch-rust[otel]'      # with OpenTelemetry spans and metrics
```

Từ một checkout, build wheel (cần Rust toolchain) rồi cài đặt:

```bash
cd linch-rust/python
uv build                            # writes dist/linch_rust-*.whl
pip install dist/linch_rust-*.whl
```

Khi bạn làm việc trực tiếp trên checkout, chỉ cần `cargo build -p linch-server`:
khi chưa cài wheel, client tìm `linch-rust/target/debug/linch-server` (hoặc bản
mới hơn trong hai bản debug và release). Xem
[nơi tìm binary](./troubleshooting.md#the-server-binary).

Muốn có sẵn một dự án? `linch-rust new my-agent` tạo một dự án chạy và kiểm thử
được ngoại tuyến ([CLI](./cli.md)). Phần còn lại của trang này tự xây dựng các
cấu trúc tương tự.

## 2. Chạy ngoại tuyến

Provider scripted phát lại các lượt do bạn viết, nên không cần key và không cần
mạng. Lưu thành `quickstart_offline.py`:

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn

config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Hello from linch-rust.")]},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("session-1", "Say hello")
        async for envelope in run:
            print(envelope.seq, envelope.event.type)
            if isinstance(envelope.event, ResultEvent):
                print(envelope.event.final_text)


asyncio.run(main())
```

```bash
python3 quickstart_offline.py
```

```text
1 init
2 user
3 context_build
4 partial_assistant
6 assistant
7 usage
8 result
Hello from linch-rust.
```

`Server()` khởi động sidecar và dừng nó khi khối lệnh kết thúc. `open_agent` mở
một agent từ config của nó và trả về một handle. `agent.run(session_id, prompt)`
bắt đầu một run và trả về ngay; lặp qua nó sẽ cho ra một `Envelope` cho mỗi event,
kết thúc bằng `result`. Số thứ tự tăng dần nhưng bỏ qua một số giá trị: đó là các
bản ghi kế toán mà client không bao giờ thấy ([Event](./events.md#số-thứ-tự)).

## 3. Chạy với provider trực tuyến

Export một key, rồi trỏ config tới provider. Server đọc key từ môi trường mà nó
kế thừa; config không bao giờ chứa key.

```bash
export OPENAI_API_KEY="..."
```

Ví dụ này cần một key (việc mở agent đã được kiểm tra với key giả, lời gọi thì
chưa). Nó in câu trả lời khi stream:

```python
import asyncio

from linch_rust import AgentConfig, PartialAssistantEvent, ResultEvent, Server

config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": "gpt-5", "system_prompt": "Answer in one paragraph."},
    "provider": {"id": "openai-responses"},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("session-1", "What is a sidecar process?")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, PartialAssistantEvent) and event.delta.get("kind") == "text":
                print(event.delta["text"], end="", flush=True)
            elif isinstance(event, ResultEvent):
                print(f"\n[{event.subtype}]")


asyncio.run(main())
```

Các provider khác (DeepSeek, Anthropic, Gemini, vLLM, SGLang, llama.cpp) chỉ khác
nhau ở dict `provider`; xem [Provider](./providers.md).

## 4. Thêm host tool

Host tool là một hàm Python bất đồng bộ mà server gọi khi model yêu cầu. `scope`
là bắt buộc: nó quyết định lời gọi có cần phê duyệt hay không.

```python
import asyncio

from linch_rust import AgentConfig, ResultEvent, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def service_status(name: str) -> str:
    """Return the current status of a service."""
    return f"{name}: healthy"


config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [
            tool_turn("call_1", "service_status", {"name": "billing"}),
            text_turn("Billing is healthy."),
        ],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[service_status])
        run = await agent.run("session-1", "Is billing up?")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ToolCallEndEvent):
                print(event.tool_name, "->", event.result["content"][0]["text"])
            elif isinstance(event, ResultEvent):
                print(event.final_text)


asyncio.run(main())
```

```text
service_status -> billing: healthy
Billing is healthy.
```

Input schema được suy ra từ type hint và mô tả lấy từ docstring. Xem thêm tại
[Tool](./tools.md#host-tool).

## 5. Phê duyệt một lệnh ghi

Ở permission mode mặc định, lời gọi `read` chạy còn lời gọi `write` và `exec` thì
hỏi. Lời gọi cần hỏi sẽ tạm dừng run: stream của nó kết thúc bằng event
`suspended` liệt kê các lời gọi đang chờ, và `agent.approve` tiếp tục run.

```python
import asyncio

from linch_rust import AgentConfig, Allow, ResultEvent, Server, SuspendedEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn

notes: list[str] = []


@tool(scope="write")
async def save_note(text: str) -> str:
    """Save a note."""
    notes.append(text)
    return "saved"


config: AgentConfig = {
    "namespace": "quickstart",
    "spec": {"model": MODEL},
    "provider": {
        "id": "scripted",
        "turns": [tool_turn("call_1", "save_note", {"text": "buy milk"}), text_turn("Saved.")],
    },
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save_note])
        run = await agent.run("session-1", "Remember to buy milk")
        stopped = await run.result()
        assert isinstance(stopped, SuspendedEvent)
        print("waiting for", stopped.pending_request_ids)

        decisions = {call: Allow() for call in stopped.pending_request_ids}
        run = await agent.approve(run.id, decisions)
        result = await run.result()
        assert isinstance(result, ResultEvent)
        print(result.final_text, notes)


asyncio.run(main())
```

```text
waiting for ['call_1']
Saved. ['buy milk']
```

`Deny("reason")` từ chối lời gọi; model sẽ đọc lý do đó. Xem
[Tool](./tools.md#phê-duyệt) và [Run](./runs.md#approve).

## 6. Giữ trạng thái qua các lần khởi động lại

Với kho memory mặc định, run và session nằm trong agent handle và mất khi server
dừng. Kho Postgres giữ chúng lại: một server mới, hoặc một process khác, tiếp tục
cùng session đó. Server tạo các bảng của nó khi agent được mở.

Ví dụ này đọc DSN từ `DATABASE_URL`, chẳng hạn
`postgres://user:pass@127.0.0.1:5432/linch`:

```python
import asyncio
import os

from linch_rust import AgentConfig, ContextBuildEvent, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn


def config(reply: str) -> AgentConfig:
    return {
        "namespace": "quickstart",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": [text_turn(reply)]},
        "stores": {"type": "postgres", "dsn": os.environ["DATABASE_URL"]},
    }


async def ask(prompt: str, reply: str) -> None:
    # A new server each time, as after a restart.
    async with Server() as server:
        agent = await server.open_agent(config(reply))
        run = await agent.run("session-1", prompt)
        async for envelope in run:
            if isinstance(envelope.event, ContextBuildEvent):
                print("the model sees", envelope.event.messages, "messages")
            elif isinstance(envelope.event, ResultEvent):
                print(envelope.event.final_text)


async def main() -> None:
    await ask("My name is Ada.", "Nice to meet you, Ada.")
    await ask("What is my name?", "Your name is Ada.")


asyncio.run(main())
```

```text
the model sees 1 messages
Nice to meet you, Ada.
the model sees 3 messages
Your name is Ada.
```

Server thứ hai thấy cuộc hội thoại đầu tiên: prompt, câu trả lời và prompt mới.
Xem [Kho lưu trữ](./stores.md).

## Tiếp theo

- [Config của agent](./agent.md): mọi trường, hợp đồng của run, callback.
- [Run](./runs.md): resume sau crash, hủy, replay event.
- [Event](./events.md): mọi event mà client giải mã.
- [Khắc phục sự cố](./troubleshooting.md): các lỗi và cách khắc phục.
