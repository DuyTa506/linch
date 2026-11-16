# Structured output

[Bản gốc tiếng Anh](../../usage/structured-output.md)

Dùng output schema khi người gọi cần câu trả lời cuối cùng của run dưới dạng dữ
liệu: một JSON object thỏa một JSON Schema do bạn đưa. Run kiểm tra câu trả lời,
gửi câu không đạt trở lại model cùng với lỗi, và kết thúc với object đã parse
trong `ResultEvent.structured_output`.

---

## Đặt schema trên agent

Schema nằm trong `spec.output_schema`; `spec.structured_output_retries` giới hạn
bao nhiêu câu trả lời không đạt được gửi lại cho model (0 khi không có).

```python
from linch_rust import AgentConfig, OutputSchema, Server

invoice: OutputSchema = {
    "name": "invoice",
    "schema": {
        "type": "object",
        "properties": {"total": {"type": "number"}, "currency": {"type": "string"}},
        "required": ["total", "currency"],
        "additionalProperties": False,
    },
}

config: AgentConfig = {
    "namespace": "billing",
    "spec": {
        "model": "claude-sonnet-4-6",
        "output_schema": invoice,
        "structured_output_retries": 2,
    },
    "provider": {"id": "anthropic"},
}

async with Server() as server:
    agent = await server.open_agent(config)
    run = await agent.run("s1", "Bill 3 widgets at 4 EUR each.")
    result = await run.result()
    print(result.structured_output)  # {'total': 12, 'currency': 'EUR'}
```

| Trường của `OutputSchema` | Mặc định | Nó điều khiển gì |
|---|---|---|
| `name` | bắt buộc | Định danh gửi cho provider; không được để trống |
| `schema` | bắt buộc | Một JSON Schema (draft 2020-12) có root là `{"type": "object", ...}` |
| `strict` | `True` | Liệu một provider có thể ép schema chính xác có nên làm vậy không (OpenAI) |
| `description` | không có | Câu trả lời là gì, gửi cho các provider nhận trường này |

`agent.open` từ chối một schema không compile được, một schema có `type` ở root
không phải `object`, và một `name` trống (`OpenError`, code -32011). Schema và
số lần retry là một phần của
[hợp đồng của run](./agent.md#the-run-contract); một agent không có chúng giữ
nguyên hợp đồng nó đã có.

## Cho một run schema riêng

`agent.run(..., output_schema=...)` thay thế schema của agent chỉ cho run đó.
`structured_output_retries` của agent vẫn giới hạn nó, nên một agent mà các run
tự mang schema riêng có thể đặt số lần retry dù chính nó không có schema.

```python
person: OutputSchema = {"name": "person", "schema": {"type": "object", "required": ["name"]}}
run = await agent.run("s2", "Who wrote the Analytical Engine notes?", output_schema=person)
```

Run lưu schema trong event `init` đầu tiên của nó (`InitEvent.output_schema`),
nên một `resume` hoặc `approve` dùng lại nó, từ bất kỳ process nào. Một schema
không compile được, hoặc có root không phải object, kết thúc run bằng
`InvalidOutputSchema` (code -32031) trước khi bất kỳ thứ gì được ghi: không có run
hay session nào tồn tại. Một schema theo run không làm đổi hợp đồng.

## Run kiểm tra gì

Chỉ câu trả lời cuối cùng được kiểm tra: một response không có lời gọi tool, sau
khi mọi subagent chạy nền đã báo cáo. Các lời gọi tool và kết quả của chúng không
bao giờ bị kiểm tra.

1. Văn bản được đọc như JSON; khi nó không phải JSON, block có rào đầu tiên
   (```` ```json ```` hoặc ```` ``` ```` trần) được đọc thay thế.
2. Giá trị phải là một JSON object, rồi thỏa schema.

Một câu trả lời không đạt được giữ trong session, và một event `verification`
(`verifier: "output_schema"`, `action: "retry"`, `attempt` từ 1) ghi lại nó. Sau
đó model nhận nội dung này, trong một message user `<system-reminder>`, và trả lời
lại:

```text
Your previous response failed structured-output validation: <error>
Respond again with ONLY a JSON object matching the required schema.
```

`<error>` là `JSON parse error: ...`, `Expected a JSON object, got array`, hoặc
`Schema validation error:` theo sau là một dòng `- path: message` cho mỗi vi
phạm.

Khi các lần retry đã dùng hết, hoặc với `structured_output_retries` bằng 0, câu
trả lời không đạt kế tiếp kết thúc run: một event `verification` với
`action: "stop"`, một `error` tên `StructuredOutputError`, và một `result` có
subtype `error` mà `structured_error` là lỗi cuối cùng và `final_text` là văn bản
của model. `max_turns` vẫn áp dụng, nên nó có thể kết thúc run trước.

Số lần retry là bền vững: một run đã crash rồi resume đếm các lần retry nó đã dùng
từ các event của nó, nên một lần crash không bao giờ cấp thêm.

## Với callback

Việc kiểm tra chạy trước các callback `stop`, nên chúng chỉ thấy một câu trả lời
thỏa schema, và request của chúng có `structured_output`, object đã parse, bên
cạnh `text`.

| Câu trả lời `stop` | Điều gì xảy ra |
|---|---|
| `allow` | Run kết thúc với câu trả lời đó |
| `rewrite` | Văn bản mới được kiểm tra lại. Một bản viết lại hợp lệ là kết quả; một bản không hợp lệ kết thúc run thất bại với `StructuredOutputError`, mà không hỏi lại model |
| `retry`, `continue` | Model trả lời lại, và câu trả lời đó được kiểm tra từ đầu. Chúng không tiêu `structured_output_retries` |
| `block` | Run thất bại, như khi không có schema |

## Mỗi provider nhận gì

Mọi request của run, trừ lời gọi tóm tắt của một compaction, đều mang schema. Mỗi
adapter gửi nó theo dạng structured-output riêng của vendor, thứ chỉ định hình câu
trả lời dạng text, nên model vẫn có thể gọi tool ở bất kỳ lượt nào:

| Provider | Được gửi dưới dạng |
|---|---|
| `anthropic` | `output_config.format` = `{"type": "json_schema", "schema": ...}` |
| `openai-chat` | `response_format` = `{"type": "json_schema", "json_schema": {name, strict, schema, description?}}` |
| `openai-responses` | `text.format` = `{"type": "json_schema", name, strict, schema, description?}` |
| `gemini` | `generationConfig.responseMimeType` `application/json` và `responseJsonSchema` |
| `deepseek`, `vllm`, `sglang`, `llamacpp` | Bị từ chối trước lời gọi: request thất bại với một event `error` nêu tên adapter |

Các vendor chỉ chấp nhận một phần của JSON Schema trong các dạng này (chẳng hạn
chế độ strict của Anthropic và OpenAI muốn `additionalProperties: false` trên mọi
object). Schema được gửi đúng như bạn viết; run vẫn tự kiểm tra mọi câu trả lời,
nên một provider lệch khỏi schema sẽ nhận retry ở trên. Gemini kết hợp phản hồi
JSON với function calling từ Gemini 3 trở đi. Các dạng request này theo tài liệu
API của từng vendor; chúng chưa được chạy với một API thật.

Một run chuyển sang một model trong `spec.fallback_models` sẽ gửi cho model đó
cùng schema, kiểm tra câu trả lời của nó theo cùng cách, và tiếp tục đếm các lần
retry schema mà run đã dùng.

## Khác với Linch bản Python thế nào

| Linch bản Python | linch_rust |
|---|---|
| Quá số lần retry, run thành công với `structured_error` được đặt | Run thất bại (`result` subtype `error`) với `structured_error` được đặt |
| Số lần retry bắt đầu lại khi resume | Số lần retry được đọc lại từ các event của run |
| `verification` với `action: "exhausted"` | `action: "stop"` |
| Chế độ `compatible` của Anthropic ép một tool cuối | Mọi provider dùng dạng native của nó; không có tool bị ép |
| `final_tool_name`, một `tool_choice` theo agent | Chưa có |

## Các trang liên quan

- [Config của agent](./agent.md#spec): phần còn lại của `spec`
- [Run](./runs.md#errors-arrive-at-the-end-of-the-stream): `InvalidOutputSchema` và các lỗi khác
- [Event](./events.md): `VerificationEvent` và `ResultEvent`
- [Kiến trúc: vòng đời lượt](../architecture/turn-lifecycle.md#the-loop-continues-while-a-response-has-tool-calls): nơi việc kiểm tra chạy và nó ghi gì
