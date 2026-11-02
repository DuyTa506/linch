# Sử dụng Linch

[Bản gốc tiếng Anh](../../usage/README.md)

Dùng hướng dẫn này để chọn hệ thống con và ví dụ Linch phù hợp với ứng dụng của bạn.

Đây là hướng dẫn thực hành xây dựng ứng dụng bằng Linch — cài đặt, kết nối một
agent và sử dụng từng hệ thống con. Nếu mới làm quen với SDK, hãy bắt đầu từ
[Bắt đầu nhanh](./quickstart.md), rồi quay lại đây để xem bản đồ hệ thống con.

Để hiểu *lý do* đằng sau thiết kế (luồng dữ liệu, pipeline của lượt, ranh giới
module), xem [mục lục kiến trúc](../../architecture/README.md).

Linch là một SDK: `Agent` chưa cấu hình có prompt trung lập về miền nghiệp vụ và
không có tool. Workspace lập trình và chính sách tin cậy của nó là những lựa chọn
tường minh tại nơi gọi.

---

## Cài đặt

```bash
# From the repo (development)
pip install -e /path/to/linch

# With common optional extras
pip install -e "/path/to/linch[mcp,anthropic,gemini,postgres,worker]"
```

Với bản phát hành đã công bố, dùng `pip install linch`. Các extra chỉ được cài khi
bạn chọn: `anthropic`, `gemini`, `postgres`, `worker` và `mcp` chỉ kéo vào các
dependency mà tính năng tương ứng cần. API của OpenTelemetry là một phần của bản
cài đặt lõi; ứng dụng chủ thêm SDK và exporter khi muốn thu thập trace hoặc metric.

Chạy `linch docs path` để tìm tài liệu và ví dụ đi kèm.

---

## Agent tối thiểu có thể chạy

```python
import asyncio
from linch import Agent, InMemorySessionStore

async def main():
    agent = Agent(model="gpt-5", session_store=InMemorySessionStore())
    try:
        session = await agent.session()
        async for event in session.run("Say hello in one sentence."):
            if event.type == "result":
                print(event.final_text)
    finally:
        await agent.close()

asyncio.run(main())
```

Ví dụ này dùng provider OpenAI Responses mặc định và yêu cầu `OPENAI_API_KEY`.
Để xem ví dụ chạy ngoại tuyến, xem [Bắt đầu nhanh](./quickstart.md).

---

## Khái niệm cốt lõi

```
Agent ──── long-lived config (model, tools, permissions, system prompt, hooks, deps)
  └── Session ── conversation state (history, run_deps, live workers)
        └── session.run(prompt) ── AsyncIterator[Event]
```

- **`Agent`** được tạo một lần cho mỗi model/cấu hình và được tái sử dụng qua nhiều
  cuộc hội thoại. Nó quản lý provider, registry tool, bộ máy phân quyền, các kho
  lưu trữ và đăng ký plugin.
- **`Session`** là một luồng hội thoại. Mỗi người dùng trong ứng dụng web có session
  riêng nhưng dùng chung `Agent`. Trạng thái thuộc về cuộc hội thoại (lịch sử,
  worker nền đang hoạt động, dependency theo từng run) nằm ở đây.
- **`session.run(prompt)`** trả về một iterator bất đồng bộ gồm các
  [event](./events.md) có kiểu — stream chúng tới UI ngay khi chúng xuất
  hiện. Tiếp nhận luồng cho tới `ResultEvent` để nhận trạng thái run và đầu ra cuối cùng.

Một `Agent`, nhiều `Session` là cấu trúc tiêu chuẩn cho ứng dụng web — xem
[`examples/core/multi_session.py`](../../../examples/core/multi_session.py).

---

## Bản đồ hướng dẫn

| Trang | Nội dung |
|---|---|
| [Tài liệu tham chiếu API](../../usage/api-reference.md) | Các import được hỗ trợ, chữ ký và tóm tắt mã nguồn chính xác |
| [Khắc phục sự cố](./troubleshooting.md) | Thông báo lỗi chính xác từ mã nguồn, nguyên nhân và cách khắc phục cho thiết lập, tool, xác thực và resume |
| [Bắt đầu nhanh](./quickstart.md) | Lộ trình cho lập trình viên mới: cài đặt, kiểm thử nhanh ngoại tuyến, provider trực tuyến, tool đầu tiên, lưu trữ bền vững |
| [CLI khởi tạo dự án](./cli.md) | `linch new` / `linch add tool` — tạo dự án do bạn quản lý, có thể chạy và kiểm thử ngoại tuyến |
| [Kết nối trong môi trường production](./production.md) | Danh sách kiểm tra tối thiểu để nhúng Linch vào dịch vụ: lưu trữ bền vững, streaming, HITL, resume, tắt hệ thống, phạm vi ảnh hưởng |
| [Cụm worker](./production.md#mở-rộng-theo-chiều-ngang) | Điều phối tùy chọn bằng `linch.worker`/PgQueuer, lease, tiếp quản, hoàn tất việc đang chạy và kết nối lại |
| [Kho run](./run-stores.md) | Checkpoint bền vững, Postgres, lease và fencing tùy chọn, kết nối lại theo số thứ tự event |
| [Agent và session](./agent.md) | Kho session, cờ tính năng, system prompt, nén context, budget cho run, khôi phục khi đầu ra bị cắt ngắn |
| [Profile miền nghiệp vụ](./profiles.md) | Vai trò có phiên bản theo từng run, cấu hình plugin, hạn chế tool, giới hạn, phân lớp và cố định phiên bản để resume |
| [Plugin và `agent.use`](../../architecture/kernel.md#plugins) | Gắn plugin có thể hoàn tác, kích hoạt dependency, dịch vụ có phạm vi và giải phóng tài nguyên |
| [Provider](./providers.md) | Chọn provider, danh mục model, khả năng, event suy luận |
| [Event](./events.md) | Luồng event, các trường chi phí, báo cáo run, bộ chấm điểm eval cho run dài |
| [Thống kê lời gọi model](./events.md#modelcallevent--thống-kê-lời-gọi-mô-hình) | Mức sử dụng tích lũy của model chính/nén context, chốt chặn budget, biên nhận và xử lý mức sử dụng không xác định |
| [Danh mục vòng đời và trace ID](./events.md#trace-và-số-liệu-opentelemetry) | Các điểm vòng đời có kiểu, danh mục bên tạo/bên nhận được sinh tự động và liên kết trace của run/tool |
| [Eval và benchmark](./evals.md) | `EvalSuite`, `run_eval_benchmark` và `scripts/eval_benchmark.py` để so sánh trong CI/giữa các provider |
| [Tool](./tools.md) | `@tool`, `FunctionTool`, tool dạng class, bộ lập lịch, timeout/thử lại, backend Bash, dependency, quyền, principal và vai trò theo từng run |
| [Tìm kiếm tool](./tools.md#toolsearchplugin--danh-mục-lớn) | Trì hoãn nạp schema tool và danh sách skill cho danh mục lớn qua `ToolSearchPlugin` |
| [Principal/RBAC](./tools.md#principal-và-rule-role-theo-từng-run) | Danh tính tenant/người dùng/vai trò theo từng run và quy tắc phân quyền có điều kiện theo vai trò |
| [Đầu ra có cấu trúc](./structured-output.md) | `OutputSchema`, thu nhận kết quả từ tool cuối cùng, sửa schema |
| [Đầu ra tool chuẩn](./tools.md#tool-và-tool--tool-tùy-chỉnh) | `ToolOutput` được xác thực bằng JSON Schema, bộ kết xuất, trích dẫn, tệp đính kèm và metadata kết quả được lưu bền vững |
| [Hook](./hooks.md) | Cơ chế mở rộng tiêu chuẩn: điểm kiểm soát, `HookResult`, adapter tích hợp sẵn |
| [Bộ nhớ đệm tool](./tool-cache.md) | Tùy chọn ghi nhớ kết quả gọi tool có phạm vi đọc theo từng run (`tool_cache=`) |
| [Context và bộ nhớ](./context-and-memory.md) | Truy xuất và kiểm tra căn cứ với `RagConnector`, context theo từng lượt, bộ nhớ tệp có phạm vi và cách tích hợp mem0 tùy chọn |
| [RAG và kiểm tra căn cứ](./context-and-memory.md#ragconnector-và-register_grounding_check) | Các đoạn dữ liệu có nguồn, trích dẫn `[doc:<id>]` và kiểm tra căn cứ của câu trả lời cuối cùng |
| [Hệ thống tệp ảo](./filesystem.md) | Chuyển kết quả lớn sang lưu trữ bên ngoài và các tool `ls`/`read_file`/`write_file`/`edit_file` |
| [Workflow](./workflows.md) | Vòng lặp điều phối cụm xác định (`run_workflow`), ghi nhật ký, resume |
| [Trình chạy vòng lặp ngoài](./loop-runner.md) | `LoopSpec` + `LoopRunner.run_once()` cho cron, CI, webhook và công việc định kỳ chạy thủ công |
| [Deep agent](./deep-agent.md) | `create_deep_agent`, worker nền, fork/continue, chế độ điều phối |
| [Điều phối](./coordination.md) | Nhóm nhiều agent với nhắn tin qua mailbox và bắt tay bằng correlator |
| [Skill](./skills.md) | Workflow prompt qua lệnh slash, skill `verify` tích hợp sẵn |
| [Mở rộng](./extending.md) | Tự triển khai điểm tích hợp: `IsolationBackend`, `Mailbox`, lắp ghép prompt và bộ dựng context |
| [Ranh giới `linch.ext.*`](../../architecture/overview.md#extensions) | Các phần mở rộng tường minh cho workflow, eval, deep agent, báo cáo, trình chạy vòng lặp và khởi tạo dự án |
| [Ví dụ](./examples.md) | Mục lục các ví dụ có thể chạy trong `examples/` |

---

## Xem `examples/` để có mã có thể chạy

Nhiều hệ thống con đi kèm bản demo có thể chạy — một số chạy hoàn toàn ngoại tuyến
mà không cần API key. Mục lục đầy đủ nằm trong [examples.md](./examples.md).
