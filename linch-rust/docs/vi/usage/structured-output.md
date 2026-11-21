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

`<error>` là `JSON parse error: ...`, `Expected a JSON object, got array`,
`JSON nests deeper than 64 levels`, hoặc `Schema validation error:` theo sau là
một dòng `- path: message` cho mỗi vi phạm.

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

Với provider có structured output native, mọi request của run, trừ lời gọi tóm
tắt của một compaction, đều mang schema. Mỗi adapter gửi nó theo dạng
structured-output riêng của vendor, thứ chỉ định hình câu trả lời dạng text, nên
model vẫn có thể gọi tool ở bất kỳ lượt nào. Các provider còn lại nhận một
[final tool](#the-final-tool) thay thế:

| Provider | Được gửi dưới dạng |
|---|---|
| `anthropic` | `output_config.format` = `{"type": "json_schema", "schema": ...}` |
| `openai-chat` | `response_format` = `{"type": "json_schema", "json_schema": {name, strict, schema, description?}}` |
| `openai-responses` | `text.format` = `{"type": "json_schema", name, strict, schema, description?}` |
| `gemini` | `generationConfig.responseMimeType` `application/json` và `responseJsonSchema` |
| `deepseek`, `vllm`, `sglang`, `llamacpp` | Không được gửi: run đưa ra một [final tool](#the-final-tool) thay thế |

Các vendor chỉ chấp nhận một phần của JSON Schema trong các dạng này (chẳng hạn
chế độ strict của Anthropic và OpenAI muốn `additionalProperties: false` trên mọi
object). Schema được gửi đúng như bạn viết; run vẫn tự kiểm tra mọi câu trả lời,
nên một provider lệch khỏi schema sẽ nhận retry ở trên. Gemini kết hợp phản hồi
JSON với function calling từ Gemini 3 trở đi. Các dạng request này theo tài liệu
API của từng vendor; chúng chưa được chạy với một API thật.

Một run chuyển sang một model trong `spec.fallback_models` sẽ gửi cho model đó
cùng schema, kiểm tra câu trả lời của nó theo cùng cách, và tiếp tục đếm các lần
retry schema mà run đã dùng.

<a id="the-final-tool"></a>

## Final tool

DeepSeek không có response format JSON-schema (chỉ có `json_object`, thứ không
mang schema), còn các adapter vLLM, SGLang và llama.cpp không gửi gì cả. Trên các
provider này run không gửi schema. Nó đưa ra thêm một tool, đặt tên theo schema
(`name`), có input schema là `schema` của bạn và có mô tả là của bạn hoặc một câu
yêu cầu câu trả lời cuối cùng. Tool này nằm cuối danh sách tool, nên phần prefix
được cache của các tool khác giữ nguyên các byte của nó.

Một response gọi nó kết thúc run với input của lời gọi làm câu trả lời:

- Lời gọi không bao giờ chạy: không có `tool_call_start` hay `tool_call_end`. Tuy vậy
  nó được quyết định như mọi lời gọi: input schema và validate của tool, các callback
  `pre_tool_use` và các luật permission đều áp dụng, và các event `hook` của callback
  được phát ra. Một callback có thể viết lại input (câu trả lời là bản đã viết lại), một
  veto hay luật deny được gửi lại dưới dạng kết quả lỗi của lời gọi, và một input mà
  tool không chấp nhận cũng vậy. Các lần từ chối này dùng `structured_output_retries`,
  nên một lần từ chối không bao giờ được gỡ sẽ kết thúc run `failed` với
  `StructuredOutputError` thay vì lặp mãi, kể cả với `tool_choice` bị ép và `max_turns`
  chưa đặt. Quyết định `Ask` được coi là cho phép: không có gì chạy nên không có gì để
  duyệt.
- Trước khi lời gọi được quyết định, một [subagent nền](./agent.md) vẫn đang chạy, hoặc
  đã kết thúc mà chưa được giao kết quả, được chờ như với một câu trả lời dạng text. Kết
  quả của lời gọi khi đó nói rằng câu trả lời chưa được nhận, thông báo của subagent đi
  chung trong cùng message, và model gọi lại tool.
- Input của nó được kiểm tra như một câu trả lời dạng text: một lần thất bại được
  gửi lại dưới dạng kết quả lỗi của lời gọi (`Call the invoice tool again, ...`),
  theo cùng `structured_output_retries` và các event `verification`, và quá số
  đó run thất bại với `StructuredOutputError`.
- Nó phải là lời gọi duy nhất của response. Một lời gọi đi kèm các lời gọi khác
  là một lần thất bại như vậy, và không lời gọi nào chạy.
- Một input hợp lệ đi tiếp đến các verifier và các callback `stop`, chúng thấy
  nó dưới dạng văn bản JSON; văn bản đó là `final_text` của kết quả, còn object là
  `structured_output` của nó. Một retry từ callback được gửi lại dưới dạng kết quả
  của lời gọi. Một callback `stop` viết lại văn bản sẽ bị kiểm tra lại: nó vẫn phải là
  một object JSON mà output schema (nếu có) và tool chấp nhận, và nó trở thành
  `structured_output`; nếu không, run kết thúc `failed`.
- Session nhận một kết quả cho mọi lời gọi của response trước khi run kết thúc
  (`Final answer captured by the host.` cho một câu trả lời được nhận), nên run
  kế tiếp của session bắt đầu ở trạng thái đã ghép cặp, bất kể điều gì đã kết thúc
  run này. Một crash sau quyết định sẽ hoàn tất run khi resume mà không hỏi lại
  model hay một callback.
- Nó kết thúc run khi không còn gì để nghe: một subagent được khởi động sau lượt cuối
  của model bị hủy, như ở bất kỳ lần kết thúc nào của một run.

Khi final tool là tool duy nhất, nó bị ép bằng một `tool_choice` có tên, hoặc
`required` ở nơi provider từ chối một lựa chọn có tên, như chế độ compatible của
Python. DeepSeek từ chối một lựa chọn bị ép khi nó đang thinking (mặc định), nên
hãy đặt `spec.thinking` thành `{"type": "disabled"}` để ép ở đó; nếu không model
chỉ được đưa ra tool. Một câu trả lời dạng text vẫn được kiểm tra như trên, nên một
model trả lời bằng văn bản JSON thay vào đó cũng kết thúc run. `spec.tool_choice`
của bạn luôn thắng.

Run ghi tên của tool vào event `init` đầu tiên của nó (`InitEvent.final_tool`) và
một lần resume dùng lại nó. Một agent có schema riêng trở thành final tool thì nó
nằm trong [hợp đồng của run](./agent.md#the-run-contract); một agent trên provider
có structured output native giữ nguyên hợp đồng nó đã có. `agent.open` từ chối một
schema có `name` trùng với tên một tool đã đăng ký hoặc không phải tên tool hợp lệ
(1 đến 64 chữ cái, chữ số, `_` hoặc `-`).

<a id="name-your-own-final-tool"></a>

### Đặt tên final tool của riêng bạn

`spec.final_tool_name` đặt tên một tool đã đăng ký mà lời gọi của nó kết thúc run
theo cùng cách, trên bất kỳ provider nào (`final_tool_name` của Python). Input của
nó được kiểm tra theo input schema và `validate` của chính tool, rồi theo output
schema khi có, và sau đó là câu trả lời nguyên như vậy. Tool không bao giờ chạy. `agent.open` từ chối một tên không phải một
tool đã đăng ký.

```python
spec = {"model": "deepseek-v4-flash", "final_tool_name": "submit_report"}
```

<a id="choose-which-tool-the-model-calls"></a>

## Chọn tool mà model gọi

`spec.tool_choice` đặt việc model có phải gọi tool hay không và tool nào, cho mọi
request của các run của agent; `agent.run(..., tool_choice=...)` thay thế nó cho
một run, và run lưu nó vào event `init` đầu tiên (`InitEvent.tool_choice`) để một
lần resume giữ nó.

| `tool_choice` | Model |
|---|---|
| `{"type": "auto"}` | Tự chọn: một tool hoặc một câu trả lời |
| `{"type": "none"}` | Không gọi tool nào |
| `{"type": "required"}` | Gọi một tool nào đó |
| `{"type": "tool", "name": "Read"}` | Gọi `Read` |

Nó được gửi với mọi request trừ ở một lượt không đưa ra tool nào (quy tắc của
Python: ví dụ một skill thu hẹp một lượt về không còn tool nào). Một lựa chọn bị ép giữ ở mọi lượt, nên một run như vậy chỉ có thể kết thúc
qua final tool của nó, hoặc `max_turns` và [loop guard](./agent.md#loop_guard).
Tool có tên được kiểm tra với request của chính từng lượt, sau mọi lần ghi lại của
`before_provider_call`: ở lượt không đưa tool đó ra (một skill đã thu hẹp tool, tool
search đã hoãn nó) request đi với `auto` thay vì bị provider trả 400, và sự kiện
`context_build` của lượt đó có `metadata.tool_choice_dropped` mang tên tool. `required`
chỉ bị bỏ ở lượt mà một hook đã để lại không còn tool nào (khi đó không thể thoả được nó):
request đi mà không có lựa chọn, và `tool_choice_dropped` là `"required"`. Một tool có tên
bị bỏ ở lượt không có tool nào cũng đi mà không có lựa chọn, không phải `auto`.
`agent.open` từ chối một tool có tên mà agent không đưa ra (final tool được tính),
`required` khi agent không đưa ra tool nào, và một lựa chọn mà provider của nó từ chối; lựa chọn riêng của một run bị từ chối
với `InvalidToolChoice` (mã -32034) trước khi có gì được ghi.

| Provider | Từ chối |
|---|---|
| `deepseek` | `required` và một tool có tên khi thinking bật, tức mọi `spec.thinking` trừ `{"type": "disabled"}` ([API reference](https://api-docs.deepseek.com/api/create-chat-completion)) |
| `anthropic` | `required` và một tool có tên với thinking có ngân sách (`{"type": "enabled", "budget_tokens": ...}`) |
| `llamacpp` | Một tool có tên, mà server không tôn trọng (`common/chat.cpp` chỉ parse `auto`, `none` và `required`) |
| `vllm`, `sglang` | Không gì cả ([vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/), [SGLang tool parser](https://docs.sglang.io/advanced_features/tool_parser.html)); một lựa chọn bị ép cần tool-call parser của server |
| `openai-chat`, `openai-responses`, `gemini` | Không gì cả |

`tool_choice` và `final_tool_name` là một phần của hợp đồng run; một agent không
đặt cái nào giữ nguyên hợp đồng nó đã có. Các run của subagent không nhận cái nào.

## Khác với Linch bản Python thế nào

| Linch bản Python | linch_rust |
|---|---|
| Quá số lần retry, run thành công với `structured_error` được đặt | Run thất bại (`result` subtype `error`) với `structured_error` được đặt |
| Số lần retry bắt đầu lại khi resume | Số lần retry được đọc lại từ các event của run |
| `verification` với `action: "exhausted"` | `action: "stop"` |
| Chế độ `compatible` của Anthropic ép một tool cuối | Final tool được đưa ra bởi mọi provider không có structured output native (DeepSeek, vLLM, SGLang, llama.cpp); Anthropic luôn dùng dạng native của nó |
| Final tool được xác nhận, rồi một lần thất bại schema được gửi lại như một message riêng | Một lần thất bại schema là kết quả lỗi của lời gọi cuối |
| Một lời gọi cuối đi kèm các lời gọi khác làm run thất bại | Đó là một lần thất bại schema, được retry theo `structured_output_retries` |
| Một `final_tool_name` theo từng run | Chỉ có `spec.final_tool_name` |

## Các trang liên quan

- [Config của agent](./agent.md#spec): phần còn lại của `spec`
- [Run](./runs.md#lỗi-đến-ở-cuối-stream): `InvalidOutputSchema`, `InvalidToolChoice` và các lỗi khác
- [Event](./events.md): `VerificationEvent` và `ResultEvent`
- [Kiến trúc: vòng đời lượt](../architecture/turn-lifecycle.md#vòng-lặp-tiếp-tục-khi-response-có-lời-gọi-tool): nơi việc kiểm tra chạy và nó ghi gì
