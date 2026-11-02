# Đầu ra có cấu trúc
[Bản gốc tiếng Anh](../../usage/structured-output.md)

Dùng `OutputSchema` khi agent phải trả về câu trả lời cuối cùng khớp với JSON Schema.

[← Hướng dẫn sử dụng](./README.md)

Khi bạn cần câu trả lời cuối cùng của agent dưới dạng đối tượng có kiểu thay vì văn bản tự do,
hãy gắn `OutputSchema`. Có thể yêu cầu các provider hỗ trợ đầu ra có cấu trúc
giới hạn phản hồi theo JSON Schema của bạn. Đối tượng đã phân tích cú pháp xuất hiện trên
event kết quả dưới dạng `structured_output`. Trang này trình bày schema, hai cách thu nhận
(phân tích văn bản JSON và thu nhận qua final tool gốc), cùng cơ chế
thử lại để sửa đầu ra theo schema theo vòng phản hồi khép kín.

---

## OutputSchema

Định nghĩa cấu trúc bạn muốn bằng `OutputSchema` và truyền nó qua
`Agent(output_schema=...)` (hoặc cho từng run qua `RunOptions(output_schema=...)`):

```python
from linch import Agent, OutputSchema

schema = OutputSchema(
    name="invoice",
    schema={
        "type": "object",
        "properties": {
            "total": {"type": "number"},
            "line_items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["total", "line_items"],
        "additionalProperties": False,
    },
    strict=True,
)

agent = Agent(model="gpt-5", output_schema=schema)
# result.structured_output → {"total": 42.50, "line_items": [...]}
```

Các trường gồm:

| Trường | Ý nghĩa |
|---|---|
| `name` | Định danh schema gửi đến provider (bắt buộc với hầu hết API). |
| `schema` | Một `dict` JSON Schema hợp lệ mô tả cấu trúc đầu ra. |
| `strict` | Bật kiểm tra schema nghiêm ngặt ở cấp provider (được OpenAI hỗ trợ). Mặc định là `True`. |
| `description` | Mô tả schema dành cho người đọc, không bắt buộc. |

Đối tượng đã phân tích cú pháp được chuyển qua `ResultEvent` kết thúc dưới dạng
`structured_output`; khi thất bại, trường đó là `None` và `structured_error`
chứa thông báo phân tích cú pháp/kiểm tra tính hợp lệ. Chỉ riêng lỗi đầu ra có cấu trúc không
bắt buộc kết quả phải có kiểu con lỗi: hãy kiểm tra cả hai trường ngay cả khi `subtype` là
`"success"`. Hook có thể thay đổi kết quả cuối cùng. Xem [Event](./events.md) để biết
toàn bộ cấu trúc kết quả.

---

## Các cách thu nhận

Linch thu nhận đầu ra có cấu trúc từ văn bản cuối cùng hoặc một tool kết thúc. Khả năng
của provider và cấu hình được chỉ định rõ quyết định cách thu nhận.

### Phân tích văn bản JSON (mặc định)

Với provider không hỗ trợ đầu ra có cấu trúc gốc, vòng lặp đọc
văn bản assistant cuối cùng của mô hình, phân tích thành đối tượng JSON và kiểm tra nó theo
schema đã cấu hình. Nếu văn bản thô không phải JSON, bộ phân tích cũng thử một khối code JSON
được bao bằng dấu phân cách. Ràng buộc schema ở phía provider tùy thuộc vào provider; hãy chỉ dẫn
mô hình trả về đối tượng JSON khi dùng provider không có ràng buộc đó.

### Thu nhận qua final tool gốc

Khi provider khai báo `structured_output_terminal_tool` trong
`ProviderCapabilities` (phương án dự phòng tương thích Anthropic được chỉ định rõ), vòng lặp
thiết lập `name` của schema làm **tool kết thúc**. Mô hình "gọi" tool đó
với các đối số khớp schema, rồi vòng lặp thu nhận `final_block.input` làm
`structured_output` mà không thực thi tool. Linch kiểm tra
đầu vào đã thu nhận theo schema đã cấu hình trước khi trả về. Lời gọi tool kết thúc
phải là lời gọi tool duy nhất trong thông điệp assistant của nó. Claude gốc,
OpenAI, Gemini và DeepSeek thay vào đó trả về văn bản JSON trong định dạng
phản hồi riêng và dùng cách phân tích rồi kiểm tra câu trả lời cuối cùng thông thường.

Bạn cũng có thể buộc dùng cách này bằng cách chỉ định rõ `final_tool_name`
(`Agent(final_tool_name=...)` hoặc `RunOptions(final_tool_name=...)`); giá trị được chỉ định rõ
được ưu tiên hơn việc tự thiết lập dựa trên khả năng của provider. Thứ tự phân giải là:
`RunOptions.final_tool_name` → `Agent.final_tool_name` → tên schema khi
provider khai báo rõ đầu ra có cấu trúc qua tool kết thúc.

### Endpoint tương thích OpenAI: `json_mode` (DeepSeek và các dịch vụ tương tự)

`OpenAIChatCompletionsProvider` phục vụ cả OpenAI thực lẫn các endpoint
tương thích OpenAI (DeepSeek, v.v.). Nó có hai chế độ `response_format`, được chọn bằng
`json_mode` trên `OpenAIChatProviderOptions`:

| `json_mode` | `response_format` của yêu cầu |
|---|---|
| `False` *(mặc định)* | `{"type": "json_schema", "json_schema": {...}}` |
| `True` | `{"type": "json_object"}` |

Một số endpoint tương thích OpenAI hỗ trợ chế độ đối tượng JSON nhưng không hỗ trợ chế độ schema.
Chế độ đối tượng JSON không thực thi ràng buộc schema của bạn tại API, vì vậy hãy truyền
`json_mode=True` khi endpoint yêu cầu:

```python
from linch import (
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
)

provider = OpenAIChatCompletionsProvider(
    OpenAIChatProviderOptions(
        api_key=..., base_url="https://api.deepseek.com", json_mode=True
    )
)
```

Với `json_object`, Linch **phân tích văn bản và kiểm tra** phản hồi theo
`OutputSchema.schema` của bạn (cách xử lý văn bản JSON ở trên),
vì vậy bạn vẫn nhận được `structured_output` đã kiểm tra. `AnthropicProvider` gốc dùng
JSON schema qua `output_config.format`. Phương án dự phòng được chỉ định rõ `api_mode="compatible"`
của nó chỉ dùng tool kết thúc được sinh ra cho những endpoint thiếu định dạng gốc đó.

---

## Kiểm tra schema

`jsonschema` là phụ thuộc cốt lõi. Linch luôn kiểm tra JSON đã phân tích cú pháp theo
`OutputSchema.schema` đã cấu hình trước khi trả về đầu ra có cấu trúc thành công;
việc phân tích JSON và kiểm tra đối tượng ở cấp gốc vẫn chạy trước. Lỗi kiểm tra
được ghi vào `structured_error` và có thể kích hoạt các lần thử sửa đã cấu hình.

---

## Sửa đầu ra theo schema bằng vòng phản hồi khép kín

Theo mặc định, câu trả lời cuối cùng không phân tích được hoặc không hợp lệ sẽ kết thúc run
với `structured_error` được đặt. Để cho agent tự sửa đầu ra, hãy bật
cơ chế thử lại để sửa đầu ra theo schema:

```python
agent = Agent(model="gpt-5", output_schema=schema, structured_output_retries=2)
```

Khi câu trả lời cuối cùng không phân tích được hoặc không khớp schema và vẫn còn lần thử lại,
vòng lặp chèn lỗi vào một thông điệp user dạng nhắc nhở hệ thống rồi chạy
thêm một lượt — chỉ dẫn mô hình trả lời lại bằng duy nhất một đối tượng JSON
khớp schema. Mỗi hành động của cổng kiểm tra phát ra một `VerificationEvent`
(`verifier="output_schema"`, `action="retry"` / `"exhausted"`). Với
cách dùng final tool, vòng lặp trước tiên xác nhận lời gọi tool đã thu nhận, rồi thêm
phản hồi sửa lỗi dưới dạng thông điệp user.

Các hành vi cần lưu ý:

- Với giá trị mặc định là `0`, cơ chế sửa đầu ra theo schema không thêm lượt thử lại hoặc event
  xác minh; lỗi vẫn nằm trên kết quả trừ khi hook thay đổi kết quả.
- Các lần thử sửa được tính vào `max_turns` và budget của run, nên schema nghiêm ngặt
  không thể gây vòng lặp vô hạn. Xem budget của run trong [Agent và session](../../usage/agent.md).
- Khi hết số lần thử lại, kết quả giữ `structured_error` và
  `structured_output` là `None`; vòng lặp phát ra
  `VerificationEvent` có `action="exhausted"`, trừ khi hook thay đổi kết quả.
- Bộ đếm thử lại áp dụng cho từng run và không được lưu trong checkpoint — run được resume bắt đầu lại từ đầu.

Cổng sửa đầu ra theo schema là thành phần tích hợp sẵn trong một nhóm rộng hơn gồm
các cổng xác minh theo vòng phản hồi khép kín (`Verifier` tùy chỉnh, `ScorerVerifier` và
các điều kiện `stop_when`). Để tìm hiểu chúng và điểm kiểm soát tập trung `BeforeFinalAnswer`
điều khiển việc xác minh câu trả lời cuối cùng, xem [Hook](../../usage/hooks.md).

---

## Các trang liên quan

- [Event](./events.md) — `ResultEvent.structured_output` và `VerificationEvent`.
- [Hook](../../usage/hooks.md) — các cổng xác minh và điểm kiểm soát tập trung `BeforeFinalAnswer`.
- [Agent và session](../../usage/agent.md) — budget của run giới hạn các lần thử sửa.
- [Provider](./providers.md) — các provider hỗ trợ đầu ra có cấu trúc gốc.
