# linch

[Bản gốc tiếng Anh](README.md)

Linch 3.0 là một kernel agent Python không có giao diện. Ứng dụng nhúng vòng lặp
agent bất đồng bộ của Linch, cung cấp tool và quyền, đồng thời tiếp nhận các event
qua streaming. Ứng dụng chủ quản lý HTTP API và việc triển khai.

Yêu cầu Python 3.11+. Linch 3.0 không tương thích với 2.x. Xem
[hướng dẫn chuyển đổi sang 3.0](docs/migration-3.0.md).

## Tính năng

| Mảng | Cách triển khai |
|---|---|
| Runtime | `Agent`, `Session`, luồng event có kiểu, lập lịch tool, nén context và budget cho run |
| Plugin | [`agent.use(plugin, config)`](docs/vi/architecture/kernel.md#plugin) gắn plugin có phạm vi, kích hoạt dependency và cho phép tháo gỡ để hoàn tác |
| Profile | [Profile miền nghiệp vụ có phiên bản](docs/vi/usage/profiles.md) kết hợp cấu hình và cố định profile của mỗi run để resume |
| Tìm kiếm tool | [Khám phá tool và skill theo cơ chế trì hoãn](docs/vi/usage/tools.md#toolsearchplugin--danh-mục-lớn) tải schema khi cần, khi vượt các ngưỡng có thể cấu hình |
| Run bền vững | Kho lưu trữ session/run dùng Postgres, lease có cơ chế fencing theo epoch, checkpoint trước các tác động phụ được bảo vệ và phát lại event theo số thứ tự |
| Cụm worker | `linch.worker` tùy chọn: điều phối bằng PgQueuer, gia hạn lease, khôi phục sau khi worker ngừng hoạt động hoặc cơ sở dữ liệu tạm thời gián đoạn và dừng tiếp nhận việc mới để hoàn tất việc đang chạy |
| Độ tin cậy | Mặc định sửa phản hồi với số lần giới hạn, tùy chọn điều kiện tiên quyết cho tool và điều kiện hoàn tất, cùng điểm tích hợp dừng lượt chạy tuần tự |
| Quyền | `Principal` cho từng run, quy tắc tool/đường dẫn/shell có điều kiện theo vai trò và quyết định phê duyệt được lưu bền vững |
| Truy xuất và bộ nhớ | `RagConnector`, kiểm tra căn cứ trích dẫn, bộ nhớ tệp có phạm vi và backend hệ thống tệp ảo |
| Provider | OpenAI Responses/Chat Completions, Anthropic, Gemini/Vertex AI qua `google-genai`, DeepSeek, llama.cpp và các endpoint tương thích |
| Phần mở rộng | Các package `linch.ext` tường minh cho workflow, eval, deep agent, báo cáo, trình chạy vòng lặp và khởi tạo dự án; hook, MCP, skill và subagent |
| Telemetry | Trace và metric OpenTelemetry trực tiếp; ứng dụng chủ cấu hình SDK và exporter |
| Vòng đời | [Danh mục vòng đời có kiểu và trace ID](docs/vi/architecture/events.md#danh-mục-vòng-đời-có-kiểu) kết nối hook, điểm tích hợp kernel, event và log của ứng dụng chủ |

Postgres là backend lưu trữ bền vững. Các kho InMemory dành cho kiểm thử và phát triển.
Tool có phạm vi đọc có thể chạy song song. Predicate `parallel` xét đầu vào còn
cho phép gọi subagent chỉ đọc; tool ghi/thực thi dùng cờ boolean vẫn chạy tuần tự.

## Cài đặt

Từ một bản checkout:

```sh
pip install -e .
```

Cài các extra mà ứng dụng của bạn sử dụng:

```sh
pip install -e '.[postgres,worker,anthropic,gemini,mcp]'
```

API của OpenTelemetry được tích hợp trong phần lõi. Cài `opentelemetry-sdk` và
một exporter trong ứng dụng chủ để thu thập telemetry.

## Bắt đầu nhanh

Ví dụ này chạy mà không cần thông tin xác thực hay truy cập mạng. `ScriptedProvider`
là một phần mở rộng eval cung cấp phản hồi cố định.

```python
import asyncio

from linch import Agent, InMemorySessionStore
from linch.ext.evals import ScriptedProvider, TextTurn


async def main() -> None:
    """Run one offline conversation and print its result."""
    agent = Agent(
        model="demo-model",
        provider=ScriptedProvider([TextTurn(text="Hello from Linch.")]),
        session_store=InMemorySessionStore(),
    )
    try:
        session = await agent.session()
        async for event in session.run("Say hello"):
            if event.type == "result":
                print(event.final_text)
    finally:
        await agent.close()


asyncio.run(main())
```

Kết quả dự kiến: `Hello from Linch.` Để dùng provider trực tuyến, tool và
kho lưu trữ bền vững, làm theo [hướng dẫn bắt đầu nhanh](docs/vi/usage/quickstart.md).
Ứng dụng chủ trong môi trường production cấu hình tường minh cả kho session và run;
[hướng dẫn production](docs/vi/usage/production.md) trình bày worker, streaming, resume
và tắt hệ thống.

## Tài liệu

- [Mục lục hướng dẫn sử dụng](docs/vi/usage/README.md): thiết lập và ví dụ cho từng tính năng.
- [Mục lục kiến trúc](docs/vi/architecture/README.md): ranh giới module, hợp đồng và các bất biến.
- [Chuyển đổi từ 2.x](docs/migration-3.0.md): các API đã bị loại bỏ và cách import thay thế.
- [Quản lý phiên bản](docs/versioning.md): API công khai, bản phát hành thử và định dạng lưu trữ bền vững.
- [Ví dụ](examples/): các ví dụ có thể chạy, được nhóm theo hệ thống con.
- [Đóng góp](docs/contributing.md): quy tắc phát triển và review.

Các thành phần kernel được import từ `linch.__all__`. Import phần mở rộng một cách
tường minh, ví dụ `from linch.ext.workflow import run_workflow`; chúng không có
trong các export ở cấp cao nhất của kernel.

## Xây dựng bằng coding agent

Cài skill `build-agent-with-linch` cho Claude Code, Codex hoặc coding agent khác. Skill chỉ cho agent
biết docs đã cài nằm ở đâu và cách build, test trên Linch.

```sh
npx skills add DuyTa506/linch   # cài từ GitHub
linch skill install             # offline, khớp với bản Linch đã cài
```

`linch new` đã kèm sẵn skill này. Xem [hướng dẫn CLI](docs/vi/usage/cli.md).

## Phát triển

```sh
pip install -e '.[dev,postgres,worker,mcp,anthropic,gemini]'
python -m pytest -q
ruff check . && ruff format --check .
pyright
```
