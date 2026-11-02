# Profile theo lĩnh vực
[Bản gốc tiếng Anh](../../usage/profiles.md)

Dùng `Profile` khi các run trên cùng một agent cần những vai trò và cấu hình được quản lý phiên bản khác nhau.

[← Hướng dẫn sử dụng](./README.md)

Dùng `Profile` để chọn vai trò và cấu hình có phiên bản cho từng run trên một
`Agent` hiện có. Các cuộc hội thoại hỗ trợ và thanh toán có thể dùng chung
provider và kho lưu trữ của agent trong khi dùng các chỉ dẫn và tool khác nhau:

```python
import asyncio
from linch import Agent, InMemorySessionStore, Profile, RunOptions, empty_tools

# support_search and billing_lookup are your existing tool implementations.
agent = Agent(
    model="gpt-5",
    session_store=InMemorySessionStore(),
    tools=empty_tools(support_search, billing_lookup),
)
base = Profile(id="base", version="1", max_turns=8)
support = base.layer(Profile(
    id="support", version="2",
    system_prompt="Help customers resolve product issues.",
    tool_allow=(support_search.name,),
))
billing = base.layer(Profile(
    id="billing", version="1",
    system_prompt="Explain invoices using the billing lookup results.",
    tool_allow=(billing_lookup.name,),
    budget={"max_tokens": 20_000},
))

async def answer(profile, question):
    session = await agent.session()
    return [event async for event in session.run(question, RunOptions(profile=profile))]

async def main():
    support_events, billing_events = await asyncio.gather(
        answer(support, "How do I reset access?"),
        answer(billing, "Where is my invoice?"),
    )
    await agent.close()

asyncio.run(main())
```

Mỗi cuộc hội thoại cần một session riêng; mỗi session vẫn chỉ cho phép một run hoạt động.
Các chỉ dẫn của profile được nối vào chỉ dẫn của ứng dụng chủ. Các tool của profile
lấy phần giao với cấu hình ghi đè tool hiện có của session, còn bộ máy phân quyền,
hook và ranh giới tool được cung cấp thông thường vẫn có quyền quyết định. Danh sách cho phép không bao giờ
cấp quyền đã bị ứng dụng chủ từ chối. `tool_allow=()` không cung cấp tool nào, còn
`tool_deny` loại bỏ các tên ngay cả khi chúng cũng được cho phép.

## Các lớp và việc kiểm tra

`base.layer(domain).layer(tenant_patch)` tạo ra một profile bất biến mới. Trường
khác `None` ở lớp sau thay thế trường trước đó; `None` kế thừa giá trị trước. Các mục plugin
được hợp nhất theo khóa ánh xạ ổn định: mục thay thế giữ nguyên vị trí, còn mục mới
được nối vào cuối. Lớp cuối cùng cung cấp id và phiên bản có hiệu lực. Tuple tool
rỗng thay thế danh sách tool; ánh xạ budget và schema thay thế toàn bộ trường.
Schema đầu ra phải cung cấp đối số `name` và `schema` hợp lệ.
`profile.resolve()` trả về từ điển độc lập, an toàn để chuyển thành JSON, phù hợp cho
việc kiểm tra hoặc xuất cấu hình.

Các plugin dùng protocol `Plugin` và vòng đời `agent.use` hiện có:

```python
support = support.layer(Profile(
    id="support-acme", version="3",
    plugins={"knowledge": (knowledge_plugin, {"namespace": "acme-support"})},
    output_schema={
        "name": "answer",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
))
print(support.resolve())
```

Cấu hình plugin, schema đầu ra và giới hạn budget là dữ liệu khai báo;
các ánh xạ và chuỗi lồng bên trong chúng đều bất biến. Plugin vẫn là các
triển khai do ứng dụng chủ cung cấp. Các phụ thuộc đang hoạt động nên nằm trong dịch vụ `Context` của agent hoặc
`RunOptions(deps=...)`, thay vì trong cấu hình profile. Tăng phiên bản plugin
khi triển khai của nó thay đổi.

Run thông thường giải phóng phạm vi plugin khi việc duyệt kết thúc hoặc đóng.
Công việc tách rời giữ phạm vi tồn tại cho đến khi tác vụ cuối cùng hoàn tất. Các
session con được giữ lại vẫn giữ các dịch vụ của chúng để tiếp tục cho đến khi được giải phóng;
giải phóng session con cuối cùng cũng giải phóng phạm vi. `agent.close()` vẫn là
phương án dự phòng để đợi công việc hoàn tất và thử lại việc giải phóng đã thất bại.

`output_schema` chứa các đối số của hàm khởi tạo `OutputSchema` hiện có.
`budget` chứa các đối số giới hạn của `RunBudget` (`max_tokens`, `max_cost_usd`,
`warn_ratio`); mỗi run có bộ tích lũy mới. Schema
hoặc budget được chỉ định rõ trong `RunOptions` ghi đè mặc định của profile. `max_turns` đặt giới hạn
vòng lặp hiện có. Profile không bổ sung một bộ máy thực thi hay đầu ra có cấu trúc khác.

## Resume và triển khai phiên bản

Không có registry profile có thể thay đổi. Bên gọi trực tiếp cung cấp định nghĩa
profile khi khởi chạy và resume. Giữ các định nghĩa đã phát hành trong
ứng dụng chủ chừng nào các run của chúng còn có thể resume:

```python
async for event in session.resume(run_id, RunOptions(profile=support)):
    ...
```

Khi khởi chạy, RunContract lưu `id`, `version` và `content_hash` dưới
`payload.run_options.profile` bên trong `RunRecord.meta["run_contract"]`. Giá trị băm
bao phủ cấu hình đã hợp thành, các mục plugin có thứ tự, danh tính plugin và
cấu hình. Resume và tiếp quản lease hợp thành lại định nghĩa được cung cấp rồi
so sánh toàn bộ hợp đồng trước khi thực thi provider hoặc tool. Profile bị thiếu,
khác phiên bản và nội dung thay đổi dưới cùng một phiên bản đều bị từ chối.
Triển khai một profile mới ảnh hưởng đến các run mới; các run hiện có phải dùng
định nghĩa đã được ghim của chúng. `profile=None` giữ nguyên cấu trúc yêu cầu và hợp đồng hiện có.

## Run trong hàng đợi và worker tiếp quản

Truyền profile và principal đã chọn khi đưa vào hàng đợi, đồng thời cung cấp các định nghĩa
đã phát hành cho từng worker:

```python
from linch import Principal
from linch.worker import RunRequest, Worker, enqueue_run, enqueue_runs

principal = Principal(tenant="acme", user="alice", roles={"support"})
run_id = await enqueue_run(pool, session_id, "Reset my access",
                           profile=support, principal=principal)
await enqueue_runs(pool, [RunRequest(other_session_id, "Find my invoice",
                                    profile=billing, principal=principal)])
worker = Worker(pool, agent_factory, profiles=(support, billing))
await worker.run()
```

Metadata của run đang chờ và payload của job chỉ chứa tham chiếu
`{id, version, content_hash}` của profile và principal đã tuần tự hóa; các đối tượng
plugin và định nghĩa profile ở lại trong tiến trình chủ. Worker phân giải
đúng tham chiếu trước khi thực thi và truyền định nghĩa qua
`RunOptions`. Định nghĩa bị thiếu hoặc nội dung không khớp khiến run thất bại với
`ConfigError` có thông tin để xử lý. Sau khi quá trình khởi chạy ghi RunContract, profile
và principal đã ghim của nó được ưu tiên khi resume và tiếp quản lease. Mọi worker
có thể nhận lại quyền xử lý run đều phải giữ định nghĩa đã phát hành đó. Xem
[cách kết nối trong môi trường production](./production.md#mở-rộng-theo-chiều-ngang) để biết về factory của worker.

`linch.ext.deep_agent.DeepAgentProfile` vẫn tách biệt: nó cấu hình
khả năng lưu bền, hành vi của coordinator và việc giữ lại worker tại thời điểm tạo qua factory. Profile theo lĩnh vực chọn
cấu hình cho từng run trên agent được tạo ra.
