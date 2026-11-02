# Hợp đồng provider

[Bản gốc tiếng Anh](../../architecture/provider-contract.md)

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Các provider tích hợp sẵn kế thừa `BaseProvider`, lớp định nghĩa hai phương thức trừu tượng
và cách triển khai mặc định cụ thể cho `capabilities()`. Provider tùy chỉnh có thể đáp ứng
giao diện runtime thông qua cấu trúc; runtime không yêu cầu `isinstance(provider, BaseProvider)`:

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from linch import ProviderCapabilities, ProviderRequest


class BaseProvider(ABC):
    id: str

    @abstractmethod
    def context_window(self, model: str) -> int: ...

    @abstractmethod
    def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]: ...

    def capabilities(self, model: str) -> ProviderCapabilities:
        # Uses ProviderCapabilities defaults; override unsupported features.
        return ProviderCapabilities(context_window=self.context_window(model))
```

`stream()` trả về một bộ lặp bất đồng bộ; cách triển khai cụ thể có thể dùng
`async def stream(...)` với `yield` để tạo bộ sinh bất đồng bộ. Nó phát ra
**dict đã chuẩn hóa** — không bao giờ phát ra đối tượng API thô. Các khóa bắt buộc theo loại event:

| Giá trị `type` | Các khóa bổ sung bắt buộc |
|---|---|
| `"message_start"` | `model: str` |
| `"text_delta"` | `text: str` |
| `"tool_use_start"` | `id: str`, `name: str` |
| `"tool_use_input_delta"` | `id: str`, `json_delta: str` |
| `"tool_use_end"` | `id: str` |
| `"thinking_delta"` | `text: str`, `signature?: str` |
| `"redacted_thinking"` | `data?: object` (được vòng lặp chuyển thành chuỗi) |
| `"message_end"` | `stop_reason: StopReason`, `usage: Usage`, `provider_metadata?: dict[str, object] \| None` |

Vòng lặp lắp ghép các event này — nó không bao giờ import kiểu dữ liệu thô của bất kỳ provider nào. Để thêm provider mới, bạn chỉ cần triển khai hợp đồng dict này.

Tập loại event là một ranh giới nghiêm ngặt: adapter phải phát ra
`type` đã khai báo và các trường bắt buộc với cấu trúc chuẩn hóa đã được mô tả.
Event sai cấu trúc là lỗi provider; vòng lặp không âm thầm chấp nhận đối tượng
SDK thô, suy ra ID tool còn thiếu, hay biến event có cấu trúc không xác định thành
câu trả lời thành công của assistant. `redacted_thinking` là dấu chỉ nội dung duy nhất
mà hệ thống chủ ý không diễn giải phần nội dung bên trong; payload tùy chọn của nó được chuyển thành khối
redacted-thinking đã chuẩn hóa. Hãy chuẩn hóa định dạng event của nhà cung cấp bên trong adapter.

Một luồng phải bắt đầu bằng đúng một `message_start` và kết thúc bằng một
`message_end`, không có event nào sau đó. ID và tên tool không được rỗng;
mọi lời gọi tool đã bắt đầu đều phải kết thúc trước `message_end`.

`message_end.usage` phải là một instance `linch.Usage` thực sự, không phải mapping hay
đối tượng SDK của provider, với các nhóm token là số nguyên không âm. Lý do dừng và
lời gọi tool phải nhất quán: `"tool_use"` yêu cầu ít nhất một lời gọi tool đã hoàn tất,
trong khi mọi lý do dừng khác đều không cho phép lời gọi tool đã hoàn tất trong thông điệp đó.
Các giá trị lý do dừng được chấp nhận là các giá trị literal `StopReason` công khai.

## Tạo request cho provider

Adapter tùy chỉnh nhận kiểu `ProviderRequest` công khai. Các trường bắt buộc
là `model`, `system`, `tools` và `messages`; các điều khiển tùy chọn có giá trị mặc định:

```python
from linch import Message, ProviderRequest, SystemBlock, TextBlock

request = ProviderRequest(
    model="my-model",
    system=[SystemBlock(text="Answer concisely.")],
    tools=[],
    messages=[Message(role="user", content=[TextBlock(text="Hello")])],
    max_output_tokens=256,
)

async for event in provider.stream(request):
    print(event)
```

## Các hook vòng đời tùy chọn

Ngoài ba phương thức cốt lõi, provider **có thể** cung cấp hai phương thức vòng đời
tùy chọn theo cơ chế duck typing. Cả hai được phát hiện bằng `getattr`/`hasattr`, nên
provider không có chúng vẫn giữ hành vi hiện tại — không phương thức nào là bắt buộc trên
`BaseProvider`.

- **`async def prepare(self) -> None`** — các lời gọi được gộp và được await một lần trước
  run đầu tiên, dành cho bước khởi động bất đồng bộ một lần không được chạy trong quá trình
  khởi tạo đối tượng. Provider llama.cpp dùng phương thức này để thăm dò `/props` ngoài event loop và
  cache cửa sổ ngữ cảnh phát hiện được; `context_window()` của nó chỉ tra cứu
  giá trị đã cache/được cấu hình và không bao giờ thực hiện I/O mạng. Nếu thăm dò thất bại,
  nó dùng giá trị cấu hình làm phương án dự phòng.
- **`async def aclose(self) -> None`** — đóng HTTP transport do provider sở hữu.
  Provider Anthropic và OpenAI Responses triển khai phương thức này để quá trình giải phóng agent
  giải phóng socket một cách xác định. Phương thức phải có tính lũy đẳng.

Bạn có thể kiểm tra tính tuân thủ mà không dùng mạng bằng `assert_provider_contract` từ
`linch`:

```python
from linch import assert_provider_contract

await assert_provider_contract(lambda: MyProvider(), model="my-model")
```

Hàm này kiểm tra ID provider, cửa sổ ngữ cảnh dương, cấu trúc khả năng
(bao gồm `caps.context_window == context_window(model)`), việc `prepare()` có thể
được gọi nếu hiện diện, và tính lũy đẳng khi đóng hai lần. Provider không được
khai báo khả năng mà bộ dựng request của nó bỏ qua. Hàm hỗ trợ này chỉ kiểm tra
cấu trúc provider và vòng đời; nó không đọc `stream()` hay kiểm tra event của luồng.
Hãy kiểm thử riêng chuỗi event theo hợp đồng chuẩn hóa ở trên.

## Kết xuất danh mục theo cơ chế gốc

`ProviderCapabilities.catalog_changes` có kiểu `Literal["append_only", "positional"]`,
mặc định là `"append_only"`, kể cả trong `full_capabilities`.
`AnthropicProviderOptions.native_catalog_changes` và
`OpenAIResponsesProviderOptions.native_catalog_changes` đều mặc định là `False`.
**Hành vi theo cơ chế gốc CHƯA ĐƯỢC XÁC MINH với endpoint thật**; kiểm thử dùng
transport giả. Các điều kiện cho phép mô tả cách triển khai, không đảm bảo
endpoint có hỗ trợ. Bedrock, Google Cloud và các cấu hình không được hỗ trợ dùng
cách kết xuất chỉ nối thêm. Khi tùy chọn tắt, cách kết xuất chỉ nối thêm được sử dụng, bao gồm
cả các khối danh mục trong lịch sử có chứa định nghĩa. Các điều kiện chính xác về mô hình và endpoint
được liệt kê trong
[hướng dẫn sử dụng provider](../usage/providers.md#thay-đổi-danh-mục-theo-cơ-chế-gốc-cần-bật).

Khi cấu hình snapshot danh mục cho session, việc xét điều kiện dùng mô hình
thực sự có hiệu lực theo từng lượt, bao gồm ghi đè từ skill, và
đòi hỏi tiền tố tool của snapshot không rỗng, không có việc thu hẹp tool theo run/ngữ cảnh hay
bỏ qua snapshot. Một quyết định request nội bộ được giữ lại khi phát lại chính xác đầu vào mô hình;
request buộc trả lời cuối cùng có loại bỏ tool cũng chặn các định nghĩa gốc trong lịch sử.
Khi chuyển sang phương án dự phòng chỉ nối thêm, các định nghĩa đã đóng băng trong lịch sử đủ điều kiện
được mở rộng vào danh sách tool, giữ nguyên các mục hiện có và ranh giới của tập tool được cung cấp.

Văn bản nhắc về danh mục luôn giữ vai trò user. Chỉ các cấu trúc tool theo cơ chế gốc của provider
nhận vai trò system/developer:

- Anthropic đệm các khối `tool_addition`/`tool_removal` trong một thông điệp system chỉ chứa tool
  ngay trước thông điệp assistant tiếp theo hoặc ở cuối mảng,
  không bao giờ đặt ở đầu hay giữa lời gọi tool và các kết quả user đã gộp của nó.
  Định nghĩa mới dùng `{"type": "tool_definition", "definition": ...}` bên trong
  `tool_addition.tool`; việc khôi phục/xóa tool thuộc tiền tố đã khai báo dùng
  `{"type": "tool_reference", "name": ...}`. Việc theo dõi tên đang hoạt động bỏ qua
  các lần bổ sung trùng lặp và xóa tên không hoạt động. Việc xóa tool được đưa vào bằng giá trị chỉ thể hiện bằng văn bản, và khôi phục
  không gửi định nghĩa lần thứ hai. Thông tin nguồn gốc lịch sử của tool được đưa vào bằng giá trị giữ cho việc xóa
  chỉ thể hiện bằng văn bản sau khi nâng cấp schema; khi việc thu gọn ngữ cảnh loại bỏ định nghĩa đó khỏi
  phần dữ liệu provider nhìn thấy, tên đã được nâng cấp vào tiền tố có thể dùng tham chiếu đã khai báo của nó.
  Chỉ thông điệp system chứa tool chưa hoàn tất ở cuối mới có thể đổi vị trí sau khi hủy và
  có prompt user tiếp theo; vị trí của lịch sử đã hoàn tất được giữ cố định. Thông điệp
  user cuối cùng giữ điểm ngắt cache. `pause_turn` kết thúc lượt Linch
  và không được resume.
- Mọi request Anthropic theo vị trí đều bao gồm `inline-tools-2026-09-15`, được gộp
  với các beta header hiện có mà không phân biệt chữ hoa/chữ thường, ngay cả khi không có khối tool.
  JSON của định nghĩa nội tuyến đã chuyển đổi được tính bằng byte UTF-8 trên toàn request;
  vượt quá 4.194.304 byte sẽ gây lỗi `ConfigError` có hướng xử lý trước khi gửi.
- Responses phát ra `{"type": "additional_tools", "role": "developer", "tools": ...}`
  tại vị trí của mỗi khối danh mục, không có `prompt_cache_breakpoint`. Các tên đang hoạt động
  và định nghĩa trùng lặp được tính từ toàn bộ lịch sử trước khi
  cắt lấy phần đuôi theo `previous_response_id`. Chỉ các bổ sung trong phần đuôi đó được gửi;
  tên đã được nâng cấp vào tiền tố tool bỏ qua các định nghĩa bổ sung đã lỗi thời. Khi tool
  thay đổi và lựa chọn là tự động, `tool_choice` trở thành
  `{"type": "allowed_tools", "mode": "auto", "tools": [...]}` theo thứ tự tên ổn định.
  Các lựa chọn bắt buộc và `"none"` được giữ nguyên.

Việc sửa schema thay thế các định nghĩa tool được cung cấp; adapter không tự đặt ra ngữ nghĩa
định nghĩa lại theo cơ chế gốc. Việc thu gọn ngữ cảnh giữ lại tiền tố, đặt lại các digest thông báo,
và đồng bộ danh mục đã hợp nhất. Lịch sử kiểm toán giữ lại
thông tin nguồn gốc theo cơ chế gốc để so sánh schema và kiểm tra hợp đồng bền vững ngay cả khi
phần dữ liệu đã nén mà provider nhìn thấy không còn thông báo ban đầu.

## Cơ sở thiết kế

- **Chỉ ba phương thức.** Provider chỉ cần trả lời “cửa sổ lớn bao nhiêu”,
  “streaming request này” và “bạn hỗ trợ những gì”. Việc giữ giao diện nhỏ gọn
  giúp provider mới (hoặc máy chủ llama.cpp cục bộ) chỉ cần một tệp nhỏ, độc lập.
- **Dict đã chuẩn hóa, không bao giờ là đối tượng API thô.** `stream()` phát ra một tập loại event
  cố định, nên vòng lặp không bao giờ import kiểu dữ liệu từ SDK của nhà cung cấp. Thay đổi của nhà cung cấp
  được giới hạn trong một tệp provider thay vì lan vào vòng lặp cốt lõi.
- **`capabilities()` + điều chỉnh request thay cho các nhánh riêng cho từng provider trong
  vòng lặp.** Provider *khai báo* khả năng hỗ trợ; `apply_provider_capabilities` loại bỏ
  các trường không được hỗ trợ trước mỗi lời gọi. Vòng lặp không có nhánh `if provider == "openai"`
  — khác biệt về tính năng là dữ liệu, được xử lý tại một nơi.
- **Cơ chế truyền đầu ra có cấu trúc được khai báo tường minh.** `structured_output` cho biết
  provider có thể trả về kết quả JSON đã được kiểm tra; `structured_output_terminal_tool`
  chỉ là true khi kết quả đó được biểu diễn bằng một lời gọi tool kết thúc do hệ thống tạo ra.
  Provider dùng JSON-schema và JSON-object gốc kết thúc qua quy trình phân tích response thông thường,
  tránh vô tình trùng tên tool.
- **Giá trị mặc định của khả năng phải khớp với adapter.** `capabilities()` của lớp cơ sở lấy
  cửa sổ ngữ cảnh và dùng các giá trị mặc định của `ProviderCapabilities`:
  `parallel_tool_calls=True`, `structured_output=True`, `tool_choice=True`,
  `structured_output_terminal_tool=False`, `prompt_cache=False`,
  `can_disable_thinking=False` và `catalog_changes="append_only"`. Hãy ghi đè
  tường minh các khả năng không được hỗ trợ, và chỉ bật cache hoặc đầu ra qua tool kết thúc
  khi adapter có triển khai chúng.

---

Quay lại [mục lục kiến trúc](./README.md).
