# Bắt đầu nhanh

[Bản gốc tiếng Anh](../../usage/quickstart.md)

Dùng lộ trình này để chạy agent offline đầu tiên, sau đó thêm provider, tool và cơ chế lưu bền vững.

[<- Hướng dẫn sử dụng](./README.md)

Lộ trình này dành cho nhà phát triển mới muốn chạy một Linch agent trước, rồi tìm hiểu các hệ thống con. Bắt đầu bằng script offline, sau đó chuyển sang provider live khi đã hiểu cách event loop hoạt động.

## 1. Cài đặt

Với bản phát hành đã công bố:

```bash
pip install linch
```

Từ một checkout cục bộ:

```bash
pip install -e '.[dev,mcp,anthropic,gemini]'
```

Linch yêu cầu Python 3.11+. Các extra tùy chọn được tách có chủ ý theo tính năng; bạn chỉ cần extra của provider khi dùng provider tương ứng.

Muốn có một project sẵn? `linch new my-agent` tạo khung một project agent có thể chạy và kiểm thử offline — xem [CLI tạo khung](./cli.md). Phần còn lại của trang này tự xây dựng cấu trúc tương tự.

## 2. Chạy smoke test offline

Điều này xác minh cách dùng SDK mà không cần API key hoặc lệnh gọi mạng.

Lưu thành `quickstart_offline.py`:

<!-- linch:offline -->

```python
import asyncio

from linch import Agent
from linch import FeatureFlags
from linch.ext.evals import ScriptedProvider, TextTurn
from linch import InMemorySessionStore


agent = Agent(
    model="demo-model",
    provider=ScriptedProvider([TextTurn(text="Hello from Linch.")]),
    session_store=InMemorySessionStore(),
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    result_offload=None,
)


async def main() -> None:
    session = await agent.session()
    async for event in session.run("Say hello"):
        if event.type == "result":
            print(event.final_text)
    await agent.close()


asyncio.run(main())
```

Chạy:

```bash
python3 quickstart_offline.py
```

Đầu ra dự kiến:

```text
Hello from Linch.
```

## 3. Chạy với provider live

Với provider OpenAI mặc định, export API key:

```bash
export OPENAI_API_KEY="..."
```

Lưu thành `quickstart_live.py`:

```python
import asyncio
import os

from linch import Agent
from linch import InMemorySessionStore


agent = Agent(
    model="gpt-5",
    openai_api_key=os.environ["OPENAI_API_KEY"],
    session_store=InMemorySessionStore(),
)


async def main() -> None:
    session = await agent.session()
    async for event in session.run("Explain Linch in one paragraph."):
        if event.type == "result":
            print(event.final_text)
    await agent.close()


asyncio.run(main())
```

`session.run(...)` là một async event stream. Hãy xây dựng CLI, web socket, server-sent event, log và giao diện progress từ các event đó thay vì chờ một giá trị trả về duy nhất theo kiểu blocking.

## 4. Thêm một tool

Tool là các object Python thuần. Decorator `@tool` là lộ trình ngắn nhất:

```python
from linch import Agent, tool
from linch import InMemorySessionStore
from linch import empty_tools


@tool(description="Return the current status for a service.", scope="read")
async def service_status(name: str) -> str:
    return f"{name}: healthy"


agent = Agent(
    model="gpt-5",
    tools=empty_tools(service_status),
    session_store=InMemorySessionStore(),
    permissions={"mode": "skip-dangerous"},
)
```

Trong production, hãy thay `skip-dangerous` bằng các rule permission tường minh trước khi cung cấp tool ghi, shell, filesystem hoặc external-service.

## 5. Giữ trạng thái khi tiến trình khởi động lại

Dùng Postgres khi lịch sử hội thoại hoặc resume run là quan trọng. Môi trường phát triển lưu bền vững cục bộ dùng cùng backend như production. Cài `linch[postgres]`, rồi khởi động database cục bộ (thông tin xác thực trong ví dụ dành cho phát triển cục bộ):

```bash
docker run --name linch-postgres -e POSTGRES_PASSWORD=linch -e POSTGRES_DB=linch \
  -p 5432:5432 -d postgres:16
export LINCH_POSTGRES_DSN='postgresql://postgres:linch@localhost:5432/linch'
```

Ứng dụng của bạn đọc biến môi trường rồi truyền DSN một cách tường minh. Thư viện không tự tìm DSN hoặc tạo durable store từ biến môi trường.

```python
import os
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

dsn = os.environ["LINCH_POSTGRES_DSN"]
agent = Agent(
    model="gpt-5",
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
)
# At application shutdown:
await agent.close()
```

Xem [Kết nối production](./production.md) về việc host sở hữu vòng đời và [Kho run](./run-stores.md) về yêu cầu lưu bền vững và schema.

`session_store` lưu trạng thái của hội thoại. `run_store` lưu checkpoint và event, nhờ đó cho phép resume durable và báo cáo vận hành.

## 6. Đọc tiếp ở đâu

| Mục tiêu | Đọc tiếp |
|---|---|
| Nhúng Linch vào web service | [Kết nối production](./production.md) |
| Hiểu Agent và Session | [Agent và session](./agent.md) |
| Thêm tool tùy chỉnh an toàn | [Tools](./tools.md) |
| Stream event tới UI | [Events](./events.md) |
| Thêm RAG/context | [Context và memory](./context-and-memory.md) |
| Chọn provider | [Providers](./providers.md) |
| Xây dựng worker chạy dài | [Trình chạy loop ngoài](./loop-runner.md) |
| Validation adapter mở rộng | [Mở rộng](./extending.md) |
| Tìm các example có thể chạy | [Ví dụ](./examples.md) |
