# Khắc phục sự cố

[Bản gốc tiếng Anh](../../usage/troubleshooting.md)

Dùng trang này để đối chiếu các lỗi thiết lập, tool, xác thực và resume với nguyên nhân và cách khắc phục.

[← Hướng dẫn sử dụng](./README.md)

Các dòng Nguyên văn trích dẫn toàn bộ thông báo trong mã nguồn. Các dòng Trích
đoạn chỉ trích dẫn phần văn bản cố định trong những thông báo còn chứa chi tiết
động; chúng không phải thông báo lỗi đầy đủ. Đường dẫn mã nguồn chỉ ra mã cần
tìm trong một bản checkout.

## `Agent` và `ConfigError`

Các bước kiểm tra này chạy khi tạo hoặc chuẩn hóa cấu hình agent.

| Loại | Thông báo | Mã nguồn | Nguyên nhân | Cách khắc phục |
|---|---|---|---|---|
| Nguyên văn | `Agent requires a model` | `src/linch/agent.py` | `model` rỗng hoặc có giá trị được coi là sai. | Cung cấp `model=` không rỗng, kể cả khi dùng `ScriptedProvider`. |
| Nguyên văn | `max_retries must be non-negative` | `src/linch/agent.py` | `max_retries` âm. | Đặt bằng không để tắt thử lại, hoặc dùng một giá trị dương. |
| Nguyên văn | `permissions.rules must be a list` | `src/linch/agent.py` | Một từ điển quyền chứa giá trị `rules` không phải danh sách. | Truyền danh sách các đối tượng `ToolRule`, `PathRule` hoặc `BashRule`, hoặc bỏ `rules`. Không chấp nhận từ điển quy tắc làm phần tử. |
| Nguyên văn | `permissions.mode must be one of: default, acceptEdits, skip-dangerous` | `src/linch/agent.py` | Chế độ không khớp với một cách viết được chấp nhận. | Chọn chế độ mong muốn bằng đúng cách viết của nó; việc xử lý phê duyệt thông thường dùng `default`. |
| Nguyên văn | `pass either limiter= or max_provider_concurrency=, not both; max_provider_concurrency is a shortcut that builds a limiter for you` | `src/linch/agent.py` | Cả hai đối số đều khác `None`. | Chỉ giữ một cách cấu hình: bộ giới hạn được truyền vào hoặc tùy chọn rút gọn giới hạn mức đồng thời. |

Xem [cấu hình agent](./agent.md) và [quy tắc quyền](../../usage/tools.md#permissions).

## `ToolRegistry`, `assert_tool_contract` và `ToolOutput`

Quá trình đăng ký kiểm tra cấu trúc tool theo kiểu duck typing trước khi thêm:
bắt buộc có tên không rỗng, `scope` hợp lệ, `input_schema` là từ điển, và các
phương thức `validate`, `execute`, `summarize` có thể gọi được.
`Agent(tools=...)` yêu cầu `ToolRegistry`, không phải danh sách thông thường.
Để tạo một registry tối thiểu:

```python
from linch import Agent, empty_tools

agent = Agent(model="your-model", tools=empty_tools())
```

Hàm hỗ trợ riêng `assert_tool_contract` là hàm bất đồng bộ, nên các bài kiểm thử
phải `await` nó. Hàm phát sinh `AssertionError` khi các bước kiểm tra thất bại;
phần kiểm tra kết quả thực thi hiện chỉ áp dụng cho các tool `ToolResult` kiểu
cũ.

| Loại | Thông báo | Mã nguồn | Nguyên nhân | Cách khắc phục |
|---|---|---|---|---|
| Nguyên văn | `tool.name must be a non-empty string` | `src/linch/tools/registry.py` | Tool được đăng ký không có tên là chuỗi không rỗng. | Định nghĩa `name` trên tool, hoặc dùng `@tool` để tạo siêu dữ liệu của nó. |
| Trích đoạn | ` is missing a callable ` | `src/linch/tools/registry.py` | Một trong các phương thức `validate`, `execute` hoặc `summarize` bị thiếu hoặc không thể gọi; thông báo chứa tên tool và phương thức. | Triển khai phương thức được nêu tên; cả ba đều bắt buộc. Không cần lớp cơ sở. |
| Nguyên văn | `tool.validate(...) must return a dict` | `src/linch/testing/contracts.py` | Hàm hỗ trợ kiểm tra hợp đồng nhận kết quả xác thực không phải từ điển. | Trả về từ điển đầu vào đã chuẩn hóa từ `validate`; phát sinh ngoại lệ nếu đầu vào không hợp lệ. |
| Nguyên văn | `tool.execute(...) must return ToolResult` | `src/linch/testing/contracts.py` | Hàm hỗ trợ kiểm tra hợp đồng kiểu cũ nhận một kiểu kết quả khác. | Trả về `ToolResult(content="...", summary="...")` cho tool kiểu cũ. Bước kiểm tra của hàm hỗ trợ này không mô tả hợp đồng đầu ra chuẩn bên dưới. |
| Trích đoạn | ` declares output_schema and must not return ToolResult` | `src/linch/tools/function.py` | Một tool dạng hàm khai báo `output_schema` nhưng trả về `ToolResult` kiểu cũ; tên của nó được đưa vào thông báo. | Trả về giá trị JSON thô, `ToolOutput` hoặc `ToolOutputError` để có đầu ra tool chuẩn. |
| Nguyên văn | `tool output renderer must be synchronous` | `src/linch/tools/base.py` | Quá trình kết xuất tạo ra một đối tượng có thể await. | Dùng `def render_output(value) -> str` đồng bộ. |
| Nguyên văn | `tool output renderer must return str` | `src/linch/tools/base.py` | Quá trình kết xuất tạo ra giá trị không phải chuỗi. | Trả về văn bản từ bộ kết xuất. |

Bộ kết xuất tùy chỉnh cũng yêu cầu `renderer_id` và `renderer_version` không
rỗng và ổn định. Các dòng này xác định giai đoạn phát hiện lỗi; chúng không đảm
bảo mọi ngoại lệ của tool đều thoát ra khỏi `session.run` mà không bị thay đổi.
Xem [tool và đầu ra chuẩn](../../usage/tools.md#custom-tools).

## `AuthError` và thông tin xác thực provider

| Loại | Thông báo | Mã nguồn | Nguyên nhân | Cách khắc phục |
|---|---|---|---|---|
| Trích đoạn | `Gemini auth error:` | `src/linch/providers/gemini.py` | Bộ chuyển đổi Gemini phân loại mã 401/403, hoặc mã `None`/400 có văn bản nhắc đến khóa API, thông tin xác thực hay quyền truy cập. Ngoại lệ từ phía dịch vụ/SDK theo sau tiền tố. | Chọn chế độ xác thực mong muốn và cấu hình thông tin xác thực cùng quyền truy cập của chế độ đó. |

Để xác thực Gemini bằng khóa API, truyền `GeminiProviderOptions(api_key=...)`
mà không có `project`. Đặt `project` sẽ chọn Vertex AI với thông tin xác thực
mặc định của ứng dụng và `location`; nhánh đó không truyền `api_key` cho client.
Chỉ riêng việc phân loại này không chứng minh khóa không hợp lệ.

OpenAI và Anthropic ánh xạ ngoại lệ thuộc nhóm xác thực hoặc trạng thái 401
sang `AuthError`, chuyển tiếp văn bản lỗi từ phía dịch vụ/SDK. Văn bản động đó
phụ thuộc vào provider/SDK, nên không có thông báo Linch cố định để trích dẫn ở
đây. Kiểm tra endpoint và thông tin xác thực đã chọn:
`OpenAIOptions(api_key=..., base_url=...)` và
`AnthropicProviderOptions(api_key=..., base_url=...)` chuyển tiếp các tùy chọn
đó cho client tương ứng. Xem [cấu hình provider](./providers.md).

## `SessionBusyError` và tiếp nhận run

`SessionBusyError` cung cấp `session_id` và `run_id` hiện có. Run ở trạng thái
`pending`, `running`, `waiting_permission` hoặc `suspended` sở hữu session.
Hãy xếp hàng prompt mới hoặc báo bận. Sau khi task trong process bị hủy, resume
run đó hoặc đối soát tác động bên ngoài rồi gọi
`run_store.mark_failed(run_id, error={"message": ...})`; không có bộ thu hồi run sau sự cố.
Khi client ngắt kết nối, phát tín hiệu `session.abort()` rồi tiếp tục tiêu thụ tới
`ResultEvent` cuối cùng để lưu trạng thái aborted.

| Loại | Thông báo | Mã nguồn | Nguyên nhân | Cách khắc phục |
|---|---|---|---|---|
| Nguyên văn | `Cannot install run admission index: duplicate nonterminal runs exist for a session; resume or mark_failed those runs before restarting.` | `src/linch/run_stores/postgres.py` | Cơ sở dữ liệu phiên bản 3 tương thích chứa các run chưa kết thúc chồng lấn khi cài index tiếp nhận. | Đối soát các run bằng bản triển khai cũ trước khi nâng cấp; store mới không thể khởi tạo xong cho tới khi xử lý các hàng trùng lặp. |

## `RunContractMismatchError` và `ConfigError` khi resume

| Loại | Thông báo | Mã nguồn | Nguyên nhân | Cách khắc phục |
|---|---|---|---|---|
| Trích đoạn | `run contract mismatch ` | `src/linch/run_store.py` | Lời gọi trực tiếp `ensure_run_contract_compatible` phát hiện hợp đồng thực thi được lưu/yêu cầu không tương thích. Dấu vân tay và các khác biệt giữa các trường theo sau tiền tố. | Kiểm tra các khác biệt được báo cáo và khôi phục đầu vào thực thi cùng định danh chính sách cho khớp, hoặc bắt đầu một run mới cho cấu hình đã thay đổi. |
| Trích đoạn | `Cannot safely resume run ` | `src/linch/loop/runner.py` | Quá trình resume session bắt gặp sự không khớp hợp đồng, `TypeError` hoặc `ValueError` và bọc lỗi đó trong `ConfigError`. ID run và lỗi gốc theo sau tiền tố. | Đọc lỗi gốc; khi lỗi chứa trích đoạn không khớp ở trên, áp dụng cách khắc phục của dòng đó. |

Một hợp đồng run bao gồm các model, khối chỉ thị hệ thống, schema/thứ tự các
tool được cung cấp, đầu vào, thiết lập đầu ra, tùy chọn run, budget và chính
sách. Chỉ thay đổi dấu vân tay đã lưu không thể sửa sự không tương thích: phép
so sánh còn tính lại dấu vân tay từ nội dung dữ liệu. Hàm hỗ trợ khi được gọi
trực tiếp phát sinh `RunContractMismatchError`; luồng resume session bọc lỗi
này trong `ConfigError`. Phiên bản định dạng truyền dữ liệu đã lưu bị thiếu
hoặc cũ là một nhóm lỗi riêng. Chỉ thay phiên bản package hoặc mô tả cấp cao nhất
của tool không làm mất hiệu lực hợp đồng còn lại đang khớp. Với 3.1.0, hãy chờ
tất cả run đang hoạt động kết thúc trước triển khai: phiên bản chính sách
Local/Docker là `2`, và schema Bash đã đổi. Các thay đổi đó vẫn từ chối hợp đồng
cũ dù wire generation không đổi. Xem
[hướng dẫn nâng cấp production](./production.md#4-resume-sau-khi-khởi-động-lại) và
[kho lưu trữ run bền vững](./run-stores.md).
