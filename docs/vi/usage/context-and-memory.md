# Ngữ cảnh và bộ nhớ

[Bản gốc tiếng Anh](../../usage/context-and-memory.md)

Dùng các bộ dựng ngữ cảnh và bộ nhớ dạng tệp để cung cấp dữ liệu truy xuất và những thông tin đã ghi nhớ cho các run.

[← Hướng dẫn sử dụng](./README.md)

Linch dùng chung một điểm mở rộng cho ngữ cảnh của request và bộ nhớ dạng tệp. Một
`ContextBuilder` có thể bổ sung RAG tạm thời hoặc dữ liệu trực tiếp từ ứng dụng vào request
gửi đến provider. Bộ nhớ dạng tệp sử dụng hệ thống tệp ảo: mô hình có thể đọc và ghi
thông tin bằng các tool hệ thống tệp hiện có, còn một chỉ mục nhỏ được nạp một lần
cho mỗi run.

---

<a id="sourced-retrieval-and-grounding"></a>

## `RagConnector` và `register_grounding_check`

Một `RagConnector` triển khai phương thức async `retrieve(query, *, k, principal=None)` và
trả về các giá trị `RetrievedChunk(id, source, text, score, metadata)`. Cài đặt connector
bằng các đăng ký có thể hủy:

```python
from linch import Agent, Principal, RunOptions, register_grounding_check, register_rag_connector

agent = Agent(...)
run_principal = Principal("acme", "user-42", {"reader"})
retrieval = register_rag_connector(agent, connector, k=5, principal=run_principal)
grounding = register_grounding_check(agent)
session = await agent.session()
async for event in session.run("question", RunOptions(principal=run_principal)):
    pass
# Later: await grounding.dispose(); await retrieval.dispose()
```

Chỉ có thể đăng ký một connector trong ngữ cảnh của agent. `k` phải là số
nguyên dương. ID của chunk phải là chuỗi không rỗng, không chứa `]`, ký tự carriage return hoặc
ký tự xuống dòng. Mỗi request đưa `[doc:<id>]`, nguồn và nội dung vào các khối
system tạm thời.
Truy xuất dùng câu hỏi do người dùng gửi trong suốt các lần thử lại để sửa lỗi và khi
resume từ trạng thái lưu bền; một alignment prompt sẽ thay thế câu hỏi đó. Principal đang hoạt động từ
`RunOptions` được truyền qua mà không thay đổi. Nếu đăng ký cung cấp `principal`,
giá trị này chỉ dùng để kiểm tra danh tính dự kiến: nếu principal đang hoạt động bị thiếu hoặc khác
với giá trị này, quá trình sẽ thất bại trước khi connector thực hiện I/O. Connector và ứng dụng chủ chịu trách nhiệm kiểm tra quyền truy cập và
xác định phạm vi tri thức.

Kiểm tra grounding yêu cầu ít nhất một trích dẫn đến ID do lần
truy xuất mới nhất trả về và từ chối mọi ID được trích dẫn nhưng không có trong tập đó. Cơ chế này kiểm tra mọi khối văn bản cuối cùng,
các câu trả lời cuối cùng được buộc tạo ra, và kiểm tra đệ quy các giá trị chuỗi trong đầu vào của terminal tool.
Nó xác minh ID trích dẫn có thuộc tập kết quả hay không, chứ không xác minh nội dung được trích dẫn có hỗ trợ một nhận định hay không.
Kết quả truy xuất rỗng không thể làm căn cứ cho câu trả lời. Lỗi truy xuất được báo rõ ràng và khiến quá trình thất bại.

Một câu trả lời không có căn cứ sẽ nhận phản hồi sửa lỗi ngắn, rồi thất bại sau khi đạt
giới hạn sửa lỗi grounding từ `agent.reliability.max_correction_turns`. Đây là
bộ đếm riêng cho mỗi run, tách biệt với các lần sửa lỗi reliability tích hợp sẵn; mỗi cơ chế
có thể dùng hết giới hạn riêng của mình. `reliability=None` không cho phép lần sửa lỗi grounding nào.
Khi bật checkpoint lưu bền, câu hỏi truy xuất, các ID mới nhất và
bộ đếm grounding đều được lưu vào checkpoint. `required=False` tắt các can thiệp;
khi không có connector đang hoạt động, kiểm tra grounding không làm gì.

Nội dung truy xuất và metadata của chunk không được đưa vào nhật ký session hay các
thông điệp session đã lưu. `ContextBuildEvent.metadata["rag"]` có kiểu xác định chỉ ghi nhận `ids`
và `sources` để kiểm toán, kể cả khi kết quả truy xuất rỗng. Việc cắt bớt ngữ cảnh theo budget không
thay đổi tập ID truy xuất: ID của một chunk đã bị cắt vẫn được phép dùng để trích dẫn
cho đến khi lần truy xuất tiếp theo thay thế tập ID. Các can thiệp grounding phát ra các bản ghi
`VerificationEvent` có kiểu xác định, không chứa nội dung truy xuất. Các bản chụp chính xác đầu vào của mô hình
có thể chứa văn bản truy xuất vì chúng giữ nguyên request được gửi đi.

Đối với các run lưu bền, connector nên cung cấp `resume_policy_id`,
`resume_policy_version` ổn định và `resume_policy_config` tương thích JSON để xác định
phạm vi tri thức cùng phiên bản tập dữ liệu; các giá trị này tham gia vào danh tính resume.
Xem [connector từ khóa trong bộ nhớ](../../../examples/rag/keyword_connector.py)
để có một triển khai tham khảo không cần thư viện phụ thuộc. Hủy đăng ký hoặc đóng agent
sẽ gỡ các hook, listener và trạng thái run tương ứng.

---

<a id="per-turn-context-building-rag"></a>

## `ContextBuilder` và `ContextInjectionHook`

Một `ContextBuilder` là đối tượng theo cơ chế duck typing, có phương thức async `build(turn)`.
Nó nhận các thông điệp hiện tại, `turn.deps`, mô hình, các tool và chỉ số lượt, rồi
trả về một `ContextBuildResult` chứa các thông điệp hoặc khối system chỉ có hiệu lực
trong request. Kết quả này không bao giờ sửa đổi nhật ký session.

```python
from linch import Agent, ContextBuildResult, ContextBudget, ContextInjectionHook, Message, TextBlock


class PolicyContextBuilder:
    async def build(self, turn) -> ContextBuildResult:
        docs = await turn.deps.search(turn.messages[-1])
        if not docs:
            return ContextBuildResult()
        return ContextBuildResult(
            messages=[Message(role="user", content=[TextBlock(text=docs)])],
            budget=ContextBudget(max_tokens=800),
            metadata={"source": "policy"},
        )


agent = Agent(..., hooks=[ContextInjectionHook(PolicyContextBuilder())])
```

`ContextInjectionHook` là điểm kết nối tiêu chuẩn. Các bộ dựng ngữ cảnh chạy cho
mỗi request gửi đến provider, nên dữ liệu thay đổi tự nhiên chỉ tồn tại tạm thời và không
tích lũy trong `session.provider_view`. Các khối chỉ có hiệu lực trong request được giữ bên ngoài
phần tiền tố prompt tĩnh và không được mặc định là có thể lưu vào cache.

Đối tượng dependency dùng chung được cung cấp cho các bộ dựng qua `turn.deps` và cho
các tool qua `ctx.deps`. Ứng dụng chủ nên đặt các client và
backend dành riêng cho request hoặc run ở đó thay vì dùng biến toàn cục của module.

---

<a id="file-memory"></a>

## `FileMemoryContextBuilder`

Gắn `FileMemoryContextBuilder` qua `ContextInjectionHook` để nạp
chỉ mục `/memories/MEMORY.md` của run:

```python
from linch import Agent, ContextInjectionHook, FeatureFlags, FileMemoryContextBuilder

agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    hooks=[ContextInjectionHook(FileMemoryContextBuilder())],
)
```

Tối đa một lần trong mỗi run, bộ dựng đọc `/memories/MEMORY.md` qua
backend hệ thống tệp của run. Nó giữ lại 200 dòng đầu và báo cắt bớt khi
chỉ mục dài hơn. Nếu thiếu chỉ mục, nó vẫn chèn chỉ dẫn cố định để
mô hình biết phải đặt thông tin ghi nhớ ở đâu:

> Thông tin ghi nhớ nằm trong `/memories/`, mỗi tệp chứa một thông tin. `MEMORY.md` là
> chỉ mục, mỗi dòng tương ứng với một tệp. Dùng các tool tệp để đọc và duy trì thông tin ghi nhớ;
> cập nhật hoặc xóa những thông tin đã lỗi thời.

Chỉ mục được lưu vào cache theo run nên giữ nguyên qua các lượt. Bộ dựng không ghi bộ nhớ. Mô hình có thể cập nhật
chỉ mục và các tệp thông tin bằng `write_file` và `edit_file` qua các tool
hệ thống tệp thông thường. Ứng dụng chủ cần xóa một tệp thông tin có thể gọi thao tác
`delete` của backend. Lệnh gọi trực tiếp từ ứng dụng chủ đến backend nằm ngoài luồng gọi tool của mô hình,
nên không tự động được tuần tự hóa, kiểm tra quyền hay ghi nhật ký; hãy dùng
các tool tệp hiện có khi cần những bảo đảm đó. Backend quyết định
các tệp nằm trong bộ nhớ hay được lưu bền qua các session và những lần khởi động lại tiến trình.

### Cô lập theo run

Ứng dụng chủ tạo một backend có phạm vi riêng cho mỗi run và truyền nó qua `RunOptions(deps=...)`.
`ScopedFileBackend` ánh xạ mọi đường dẫn do mô hình cung cấp vào bên dưới thư mục gốc của nó. Nó từ chối
`..` và byte null, chuẩn hóa dấu phân cách cùng các thành phần `.`, và coi một
đường dẫn tuyệt đối từ mô hình là đường dẫn bắt đầu tại thư mục gốc bên trong phạm vi. Thao tác liệt kê trả về các đường dẫn
tương đối trong phạm vi đó.

```python
from linch import RunOptions, ScopedFileBackend, StateFileBackend

inner = StateFileBackend()
tenant_a = ScopedFileBackend(inner, "/tenants/acme/users/alice")
tenant_b = ScopedFileBackend(inner, "/tenants/acme/users/bob")

async for _event in session.run(
    "Remember that I prefer concise updates.",
    opts=RunOptions(deps={"filesystem": tenant_a}),
):
    pass
```

Hai run đồng thời có thể dùng chung một `Agent` trong khi dùng các
backend có phạm vi khác nhau. Mỗi run chỉ thấy cây `/memories/` của riêng mình, kể cả khi
cố truy cập vượt phạm vi bằng đường dẫn. `FileMemoryContextBuilder` lấy backend từ cùng
đối tượng `turn.deps` mà các tool tệp dùng, nên thao tác đọc và ghi không thể âm thầm
dùng một tenant khác.

### Cấu trúc chỉ mục ổn định

Giữ `MEMORY.md` ngắn gọn và rõ ràng. Mỗi dòng trỏ đến một tệp thông tin, ví
dụ:

```text
/memories/favorite-language.md
/memories/meeting-timezone.md
```

Các tệp thông tin chứa nội dung mà mô hình cần ghi nhớ. Khi một thông tin thay đổi,
hãy sửa tệp tương ứng và giữ dòng chỉ mục. Khi thông tin không còn đúng, hãy xóa
tệp thông tin và dòng chỉ mục của nó. Giới hạn 200 dòng áp dụng cho chỉ mục;
backend hệ thống tệp chịu trách nhiệm về kích thước tệp thông tin và chính sách lưu bền.

---

## Gợi nhớ ngữ nghĩa tùy chọn với mem0

Gợi nhớ ngữ nghĩa là một công thức ví dụ, không phải kho lưu trữ lõi hay một extra. Công thức
`examples/memory/mem0_recall.py` chỉ dùng các API ngữ cảnh và
hook công khai của Linch. Bộ dựng ngữ cảnh của nó tìm kiếm qua một client kiểu mem0 `AsyncMemory` bằng
văn bản mới nhất của người dùng và `filters={"user_id": ...}` từ các dependency của run; hook
khi agent dừng chỉ gọi `add(...)` sau một run thành công. Một client giả có
các phương thức async `search` và `add` tương ứng là đủ để kiểm thử công thức mà không cần
cài đặt mem0.

Dùng bộ nhớ dạng tệp cho những thông tin rõ ràng, có thể kiểm tra trực tiếp, và chọn dùng mem0 khi việc gợi nhớ ngữ nghĩa
xứng đáng với việc sử dụng một dịch vụ bộ nhớ bên ngoài. Giữ client mem0 và danh tính người dùng
của nó trong các dependency của run để một tenant không thể tìm kiếm thông tin ghi nhớ của tenant khác.

---

## Các trang liên quan

- [Hook](../../usage/hooks.md) — `ContextInjectionHook` và vòng đời hook
- [Tool](../../usage/tools.md) — các tool hệ thống tệp và đối tượng `deps` dùng chung
- [Hệ thống tệp ảo](./filesystem.md) — `FileBackend`, giới hạn phạm vi và chuyển dữ liệu ra ngoài
- [Kiến trúc](../../architecture/README.md) — các hợp đồng và bất biến của từng phân hệ
