# Providers

[Bản gốc tiếng Anh](../../architecture/providers.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Một provider biến một `ProviderRequest` thành một stream các `ProviderEvent` đã chuẩn
hóa. Hợp đồng nằm trong `linch-core`
(`crates/linch-core/src/provider/`); các adapter HTTP nằm trong
`linch-providers` (`crates/linch-providers/src/`). Dùng trang này khi bạn
đổi một adapter, thêm một adapter, hoặc đụng đến cách một lời gọi model được lắp ghép,
tính giá hoặc xử lý lỗi.

Linch bản Python có cùng ý tưởng
([hợp đồng provider](../../../../docs/architecture/provider-contract.md)). Điểm khác
trong Rust:

- Trait có hai method, `id` và `stream`. Không có
  `context_window`, `capabilities`, `prepare` hay `aclose`; một adapter không
  thể đáp ứng một trường của request sẽ làm lời gọi thất bại với `InvalidRequest`.
- `stream` nhận một `CancellationToken` tường minh.
- Usage không xác định là `None`, không bao giờ là một `Usage` toàn số không.
- Event là một enum đóng, từ chối các trường và tag không biết khi giải mã.

## Hợp đồng

`Provider` (`crates/linch-core/src/provider/client.rs:25`) là
`Send + Sync` và object safe; agent giữ nó dưới dạng `Arc<dyn Provider>`.

| Method | Hợp đồng |
|---|---|
| `fn id(&self) -> &str` | Định danh ổn định (`deepseek`, `openai-chat`, `vllm`, ...). Lớp OpenTelemetry ánh xạ nó sang `gen_ai.provider.name`. |
| `async fn stream(&self, request, cancel) -> Result<ProviderStream, ProviderError>` | Bắt đầu một lời gọi. Một lỗi trước event đầu tiên (kết nối, mã trạng thái HTTP, một request mà adapter từ chối) là `Err`. Stream kết thúc sau `message_end` hoặc sau một mục lỗi. Hủy `cancel` kết thúc nó với `ProviderError::Cancelled`. |
| `fn context_window(&self, model: &str) -> u64` | Cửa sổ ngữ cảnh của model, không có I/O; `DEFAULT_CONTEXT_WINDOW` (128000) trừ khi bị ghi đè. Compaction không có `threshold_tokens` (`crates/linch-core/src/compaction.rs`, `due`), ngân sách danh sách skill và tỷ lệ offload của facade đều suy ra từ nó. |
| `async fn prepare(&self)` | Khám phá một lần, là no-op trừ khi bị ghi đè; không bao giờ thất bại. Facade gọi nó khi mở một agent, trước khi có gì đọc cửa sổ; một host dựng agent bằng mã sẽ gọi nó trước `AgentBuilder::build`. |

`ProviderStream` là `BoxStream<'static, Result<ProviderEvent, ProviderError>>`
(`client.rs:17`), nên stream sở hữu mọi thứ nó cần và sống lâu hơn
phần mượn `self`.

<a id="event-vocabulary"></a>

### Tập event

`ProviderEvent` (`crates/linch-core/src/provider/event.rs:24`) được gắn tag bằng
`type` trên wire. Mọi struct payload đều có `deny_unknown_fields`.

| Variant (`type`) | Trường |
|---|---|
| `MessageStart` (`message_start`) | `model`: model đã phục vụ lời gọi |
| `TextDelta` (`text_delta`) | `text` |
| `ThinkingDelta` (`thinking_delta`) | `text`, `signature?` |
| `RedactedThinking` (`redacted_thinking`) | một block reasoning mã hóa nguyên vẹn |
| `ToolUseStart` (`tool_use_start`) | `id`, `name` |
| `ToolUseInputDelta` (`tool_use_input_delta`) | `id`, `json_delta` |
| `ToolUseEnd` (`tool_use_end`) | `id` |
| `MessageEnd` (`message_end`) | `stop_reason`, `usage?`, `provider_metadata?` |

`provider_metadata` là một đối tượng JSON mờ (opaque) được mang theo vào message đã lắp
ghép. Các adapter dùng nó để phát lại những gì vendor cần nhận lại ở lượt kế tiếp: các
output item của Responses dưới `openai_responses`
(`crates/linch-providers/src/responses/stream.rs:26`), các part thô của Gemini dưới
`gemini_parts` (`crates/linch-providers/src/gemini.rs:51`).

### Quy tắc stream

`Assembler` (`crates/linch-core/src/provider/assembler.rs:84`) là bên tiêu thụ duy nhất
của stream và thực thi các quy tắc ở mỗi lần push; `assemble`
(`assembler.rs:344`) chạy nó trên một danh sách đã thu thập. Các quy tắc
(`assembler.rs:46-62`):

- đúng một `message_start` ở đầu, đúng một `message_end` ở cuối, không có gì
  sau nó;
- mỗi `tool_use_start` có một id không rỗng, duy nhất trên cả stream, và một
  tên không rỗng;
- một input delta hoặc `tool_use_end` nêu tên một lời gọi đang mở;
- mọi lời gọi đã mở đều kết thúc trước `message_end`, và input đã nối của nó
  parse được thành một đối tượng JSON;
- `stop_reason` là `tool_use` khi và chỉ khi có ít nhất một lời gọi hoàn tất.

Ngoại lệ duy nhất là một lần dừng `max_tokens`: một lời gọi vẫn đang mở, hoặc có input
không phải một đối tượng trọn vẹn, bị bỏ đi thay vì làm stream thất bại. Lượt khi đó
dừng với `max_tokens` nếu không còn lời gọi nào và `tool_use` nếu còn các lời gọi đã xong.
Một response bị cắt trước khi có bất kỳ văn bản hay lời gọi trọn vẹn nào bị runner từ chối
(`crates/linch-core/src/runner.rs:12-16`).

Gộp (coalescing): các text delta liên tiếp thành một block văn bản; các thinking delta
liên tiếp thành một block thinking cho đến một delta mang chữ ký. Một thinking delta có
chữ ký với văn bản rỗng vẫn cho ra một block, để chữ ký được phát lại (`assembler.rs:64-81`).

### Request

`ProviderRequest` (`crates/linch-core/src/provider/request.rs:17`) có bốn trường bắt buộc
và các điều khiển tùy chọn mà một adapter có thể bỏ qua hoặc từ chối:

| Trường | Ghi chú |
|---|---|
| `model`, `system`, `tools`, `messages` | Luôn được runner đặt. |
| `max_output_tokens`, `temperature`, `stop_sequences` | Tùy chọn. |
| `effort` (`ReasoningEffort`), `thinking` (`Thinking`) | `None` giữ mặc định của provider. Mỗi adapter tự ánh xạ chúng. |
| `cache_control` | Được đặt (không có TTL) trên mọi lời gọi chính của một run, như `cache_prompt=True` của Python; một lời gọi tóm tắt compaction không có (`crates/linch-core/src/agent.rs`). |
| `output_schema`, `tool_choice` | Tùy chọn. |
| `appended_tools` | Tên của các `tools` mà một session đã thêm sau prefix đã đóng băng của nó; rỗng ngoài catalog của session. Chỉ các thay đổi catalog gốc của adapter Anthropic và Responses đọc nó. |

Request không có tín hiệu hủy, ngân sách retry hay công tắc catalog gốc; cancel token là
đối số thứ hai của `stream`.

## Lỗi và retry

`ProviderError` (`client.rs:58`) có năm variant. Thông báo do adapter viết và không bao
giờ được chứa thông tin xác thực hay header của request.

| Variant | Ý nghĩa | `retryable()` |
|---|---|---|
| `Transport { message }` | Không có response: kết nối, TLS, timeout, reset, hoặc một stream bị đứt | có |
| `Status { status, retry_after, message }` | Mã trạng thái HTTP không thành công | chỉ 429 và >= 500 |
| `InvalidRequest { message }` | Adapter từ chối request như đã được dựng | không |
| `Stream(StreamError)` | Event stream vi phạm hợp đồng stream | không |
| `Cancelled` | Cancel token đã kích hoạt | không |

Không có gì trong linch-rust retry một lời gọi provider:

- Các adapter không bao giờ retry (`crates/linch-providers/src/lib.rs:4`).
- `retryable()` và `retry_after` là lời khuyên cho bên gọi
  (`client.rs:1-5`).
- `dispatch` của runner (`crates/linch-core/src/runner/turn.rs:221`) kết thúc
  run với trạng thái `failed` cùng một event `Error` ở mọi lỗi trừ `Cancelled`, lỗi này
  abort run (`turn.rs:298-305`). Event lỗi mang `retryable` và
  `status` (`crates/linch-core/src/provider/call.rs:306`), để host có thể
  quyết định bắt đầu một run mới.
- Số `attempt` của một delivery không phải bộ đếm retry. Nó chỉ tăng
  khi một run được resume thấy một lời gọi đã được gửi đi nhưng chưa bao giờ kết thúc và
  gửi lại nó (`Recovery::Redispatch`,
  `crates/linch-core/src/runner/start.rs:221`).

Không có deadline cho cả một lời gọi provider. HTTP client của mỗi adapter có
connect timeout 30 s và một read timeout giữa các lần đọc (bên dưới); hãy hủy
token của run để dừng một lời gọi sớm hơn.

## Cơ chế HTTP dùng chung

Mọi adapter dựng client của nó bằng `transport::client`
(`crates/linch-providers/src/transport.rs:23`):

- **Không redirect.** reqwest chỉ bỏ `Authorization` khi chuyển hướng sang origin khác,
  nên `x-api-key` và `x-goog-api-key` sẽ đi theo một redirect. Một phản hồi 3xx
  trở thành `Status` với một thông báo cố định (`transport.rs:57`); nó không
  retry được.
- **Giới hạn thân lỗi.** Tối đa 8 KiB của một thân không phải 2xx được đọc
  (`transport.rs:16`, `transport.rs:34`). Bản tóm tắt ưu tiên thông báo lỗi của vendor,
  được che dữ liệu nhạy cảm, rồi cắt còn 500 ký tự
  (`crates/linch-providers/src/openai_compat/http.rs:73`).
- **`Retry-After`** được đọc dưới dạng số giây nguyên; một ngày HTTP bị bỏ qua. Gemini
  cũng đọc `retryDelay` của một detail `RetryInfo`
  (`crates/linch-providers/src/gemini/http.rs:68-88`).
- **Lỗi bên trong một stream 200** được ánh xạ sang mã trạng thái HTTP mà cùng lỗi đó
  sẽ có, để chúng retry giống nhau (`openai_compat/http.rs:104`).

Read timeout:

| Adapter | Read timeout | Lý do |
|---|---|---|
| DeepSeek, Anthropic, Gemini | 300 s | Server gửi keep-alive khi nó đang làm việc |
| OpenAI Chat (và các server profile), OpenAI Responses | 600 s | Các model reasoning có thể suy nghĩ hàng phút trước token đầu tiên |

### Vòng lặp SSE duy nhất

Cả năm wire format chạy qua một vòng lặp, `sse::events`
(`crates/linch-providers/src/sse.rs:45`). Một adapter cung cấp một `Decoder` riêng của
crate (`sse.rs:19`): `data` dịch một payload `data:` không rỗng và
cho biết response đã hoàn tất chưa; `close` xử lý một body kết thúc sớm
(một format có dấu kết thúc thì thất bại, format không có thì hoàn tất). Vòng lặp:

- kiểm tra cancel token trước mỗi lần đọc và cho lần đọc chạy đua với nó;
- khi response đã hoàn tất, yield những gì đang xếp hàng kể cả khi một lệnh hủy
  đến, để phần kết thúc và usage của nó không bị mất (`sse.rs:99-103`);
- không yield gì sau một lỗi (`sse.rs:156`);
- xóa API key và mọi `Bearer <token>` khỏi các thông báo transport và status
  (`sse.rs:137`);
- thêm một dòng trống kết thúc để event cuối không có dòng trống theo sau
  vẫn được dispatch (`sse.rs:57-61`).

### Thông tin xác thực

`ApiKey` (`crates/linch-providers/src/api_key.rs:14`) bọc khóa trong một
`Arc<str>`. `Debug` in `ApiKey([redacted])`, không có `Display`, và
một khóa rỗng hoặc toàn khoảng trắng là `None`. Mọi chuỗi đi tới một
`ProviderError` đều qua `scrub` (`api_key.rs:46`), thứ xóa khóa
và mọi token `Bearer`, kể cả với một server cục bộ không có khóa (một proxy có thể thêm
một khóa). Mỗi struct options có `from_env()`, đọc biến của adapter
và trả về `None` khi nó chưa được đặt hoặc rỗng.

<a id="the-adapters"></a>

## Các adapter

Mọi adapter lấy model từ mỗi request, không lấy từ options của nó.

| Kiểu | `id()` | Endpoint | Xác thực | Biến khóa |
|---|---|---|---|---|
| `DeepSeek` | `deepseek` | `{base}/chat/completions` | Bearer | `DEEPSEEK_API_KEY` |
| `OpenAiChat::new` | `openai-chat` | `{base}/chat/completions` | Bearer | `OPENAI_API_KEY` |
| `OpenAiChat::server` | `vllm`, `sglang`, `llamacpp` | `{base}/chat/completions` | Bearer khi có khóa | không có |
| `OpenAiResponses` | `openai-responses` | `{base}/responses` | Bearer | `OPENAI_API_KEY` |
| `Anthropic` | `anthropic` | `{base}/v1/messages` | `x-api-key` | `ANTHROPIC_API_KEY` (và `ANTHROPIC_BASE_URL`) |
| `Gemini` | `gemini` | `{base}/v1beta/models/{model}:streamGenerateContent?alt=sse` | `x-goog-api-key` | `GEMINI_API_KEY` |

Doc comment của mỗi module liệt kê các quy tắc wire mà nó đã được kiểm tra, kèm ngày;
hãy đọc nó trước khi đổi adapter.

### DeepSeek

`crates/linch-providers/src/deepseek.rs`. `thinking` luôn được gửi, vì
DeepSeek mặc định suy nghĩ; `Thinking::Disabled` tắt nó và mọi trạng thái khác
bật nó (`deepseek.rs:129`). Khi thinking bật, mọi lượt assistant trước đó gửi
`reasoning_content`, là `""` khi nó không có, nếu không một request có tool sẽ
thất bại với 400 (`deepseek.rs:147-153`). Một `tool_choice` bắt buộc khi thinking bật,
và mọi `output_schema`, thất bại ngay tại chỗ với `InvalidRequest`.
Effort ánh xạ `medium` và `xhigh` sang `high` (`deepseek.rs:184`).

<a id="openai-chat-completions-and-server-profiles"></a>

### OpenAI Chat Completions và các server profile

Một kiểu duy nhất, `OpenAiChat` (`crates/linch-providers/src/openai.rs:98`), phục vụ
OpenAI và từng server tương thích OpenAI. Điểm khác biệt là một `ChatProfile`
(`crates/linch-providers/src/openai/profile.rs:19`), một bảng tĩnh không có mặc định:
mỗi đích nêu mọi quy tắc, nên một server không bao giờ vô tình kế thừa hành vi của OpenAI.

| Profile | Trường reasoning | Echo reasoning | `tool_choice` có tên | Trường giới hạn đầu ra | Tắt thinking |
|---|---|---|---|---|---|
| OpenAI (`profile.rs:53`) | không có | không | có | `max_completion_tokens` | không gửi |
| vLLM (`servers/vllm.rs:33`) | `delta.reasoning` | có | có | `max_completion_tokens` | `enable_thinking: false` |
| SGLang (`servers/sglang.rs:31`) | `delta.reasoning_content` | có | có | `max_completion_tokens` | `enable_thinking: false`, `thinking: false` |
| llama.cpp (`servers/llamacpp.rs:31`) | `delta.reasoning_content` | có | không (thất bại tại chỗ) | `max_tokens` | `enable_thinking: false` |

Một server được chọn bằng `ServerOptions::new(Server::Vllm, base_url)`
(`crates/linch-providers/src/servers.rs:76`), thứ điền
`chat_template_kwargs` mà tài liệu của server đó nêu để tắt thinking. Khi
một request yêu cầu `Thinking::Disabled`, các kwargs đó được gửi và effort bị
bỏ ra (`profile.rs:144-154`). `output_schema` hiện chưa được chat profile nào hỗ trợ.

### OpenAI Responses

`crates/linch-providers/src/responses.rs`. Các block system trở thành
`instructions`. Function tool được gửi với `strict: false`, để API không
viết lại một schema sang chế độ strict. Các output item của mỗi response,
kể cả reasoning, được giữ trong `provider_metadata` và gửi lại nguyên văn;
không gửi `previous_response_id`, nên mọi request đều mang đầy đủ
lịch sử. `ResponsesOptions::store` tắt sẽ gửi `store: false` và yêu cầu reasoning
được mã hóa. Stop sequence bị từ chối: API không có tham số như vậy.

### Anthropic

`crates/linch-providers/src/anthropic.rs`. `anthropic-version` được ghim ở
`2023-06-01` (`anthropic.rs:44`) vì phiên bản mới hơn bỏ
`temperature`, thứ `ProviderRequest` vẫn mang. `max_tokens` là
bắt buộc và mặc định 8192 (`anthropic.rs:47`). Các block thinking được gửi
lại với chữ ký không đổi. `cache_control` của một request đánh dấu
block system cacheable cuối cùng và block cuối của message user cuối
(`anthropic/payload.rs:39`).
`message_delta.usage` là tích lũy: một trường có mặt thay thế giá trị của
`message_start`. `pause_turn` và các stop reason không biết làm lời gọi thất bại
dưới dạng một lỗi transport có thể retry (`anthropic/stream.rs:311`).

### Gemini

`crates/linch-providers/src/gemini.rs`. Model được percent-encode thành một
path segment, nên `/`, `?`, `#` và `:` trong tên model không thể đổi URL
(`gemini.rs:59`). Các function call không có `id` nhận một id ngẫu nhiên cho mỗi
response và id đó không bao giờ được gửi lại. Một lượt có thought signature hoặc call
id giữ các part thô của nó trong `provider_metadata` và phát lại nguyên văn.
`cache_control` bị bỏ qua (caching là ngầm định). Các finish reason loại an toàn
trở thành `refusal`; những loại không biết thất bại dưới dạng lỗi transport có thể retry
(`gemini/stream.rs:293`). Module ghi chú rằng adapter này chưa chạy
với một khóa thật.

### Cửa sổ ngữ cảnh

Mỗi adapter báo các giá trị của Linch bản Python (`crates/linch-providers/src/context.rs`
chứa các phép tra cứu và thăm dò): Anthropic 200000; Gemini 2000000 cho
`gemini-1.5-pro`, nếu không thì 1048576; Responses và OpenAI Chat có bảng riêng của chúng
(Python giữ hai bảng), nếu không thì 128000; DeepSeek 128000. `DeepSeekOptions`,
`OpenAiChatOptions` và `ServerOptions` nhận một `context_window` được ưu tiên.
Một server profile nêu quy tắc của nó (`ChatProfile::context`): vLLM và SGLang đọc
`max_model_len` của từng model từ `GET /v1/models`, llama.cpp đọc `n_ctx` từ
`GET /props` (rồi đến của gốc), một lần trong `prepare`, trong vòng 2 giây, với
bearer key của lời gọi. Một lần thăm dò thất bại để lại 128000. Không giống Python, một
cửa sổ llama.cpp đã cấu hình bỏ qua lần thăm dò thay vì dùng làm phương án dự phòng của nó.

### Chuẩn hóa usage

`Usage` của Linch (`crates/linch-core/src/usage.rs:13`) đếm input chưa cache,
output, cache read và cache write trong bốn bucket riêng; `input_tokens`
không gồm cả hai bucket cache. Mỗi adapter trừ đi những gì vendor của nó gộp
vào số đếm prompt: DeepSeek trừ các cache hit của nó (`deepseek.rs:193`), OpenAI trừ
token cached và cache-write (`profile.rs:89`), vLLM trừ token cached và token cache được
tạo (`servers/vllm.rs:54`), SGLang và llama.cpp trừ token cached
(`servers.rs:89`).

<a id="pricing"></a>

## Bảng giá

`linch_providers::cost(model, &usage)` (`crates/linch-providers/src/pricing.rs:97`)
tính giá từng bucket theo mức giá riêng từ một bảng khớp theo đúng model id
(`pricing.rs:56`). Bảng chỉ chứa các model Anthropic, ở mức giá công bố
ngày 2026-06-08 với cache write ở bậc 5 phút. Mọi model khác,
kể cả mọi model OpenAI, DeepSeek, Gemini và cục bộ, được tính giá là
`None`. `ModelPricing::cost` (`pricing.rs:33`) tính giá với mức giá mà
host cung cấp.

Tính giá là opt-in trên agent: `AgentBuilder::pricing`
(`crates/linch-core/src/agent/builder.rs:121`) nhận bất kỳ
`Fn(&str, &Usage) -> Option<f64>`. Crate `linch` truyền
`linch_providers::cost` (`crates/linch/src/open.rs:143`). Khi một receipt có
usage, runner tính giá theo model đã phục vụ lời gọi, nếu không thì theo model của
agent (`crates/linch-core/src/runner/write.rs:107`). Một giá bị thiếu
hoặc không hữu hạn vẫn là `None` (`crates/linch-core/src/agent.rs:141`), và tổng
của một run chỉ cộng các lời gọi có giá. Chi phí xuất hiện dưới dạng `cost_usd` trên
receipt và các event `Usage` và `total_cost_usd` trên `Result`. Bảng giá không phải
là một phần của run contract.

## Thêm một provider bằng Rust

Một provider ngoài cây mã triển khai `Provider` trực tiếp; `Decoder` và
`sse::events` là riêng của crate. Một adapter mới trong cây nên dùng lại
`transport::client`, `sse::events` và `api_key::scrub` thay vì tự viết
client, vòng lặp hay phần che dữ liệu riêng.

Danh sách kiểm tra:

1. Không bao giờ retry và không bao giờ đi theo redirect.
2. Tôn trọng cancel token; trả về `ProviderError::Cancelled` cho nó.
3. Ánh xạ mã trạng thái HTTP sang `Status` với `retry_after`, và che mọi thông báo.
4. Phát tập event ở trên, theo thứ tự, và đặt `usage: None` khi vendor
   không báo gì.
5. Từ chối một trường của request mà bạn không thể đáp ứng bằng `InvalidRequest` thay vì
   bỏ nó đi.
6. Nêu từng quy tắc wire trong doc của module cùng nguồn và ngày bạn đã
   kiểm tra nó. Đừng dùng lại profile của adapter khác.

Đoạn này biên dịch và chạy được với `linch-core` ở revision này. Nó kiểm tra một
stream có lời gọi tool bằng `assemble`, rồi điều khiển một run bằng một provider echo có
tính giá:

```rust
use std::sync::Arc;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ToolUseEndEvent, ToolUseInputDeltaEvent,
    ToolUseStartEvent,
};
use linch_core::{
    AgentBuilder, AgentSpec, Event, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent, ProviderRequest,
    ProviderStream, RunOptions, StopReason, Usage, assemble,
};
use tokio_util::sync::CancellationToken;

/// Answers every request by echoing the last message's text.
struct Echo;

#[async_trait]
impl Provider for Echo {
    fn id(&self) -> &str {
        "echo"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let text = request.messages.last().map(|m| m.text()).unwrap_or_default();
        let events = vec![
            ProviderEvent::MessageStart(MessageStartEvent { model: request.model }),
            ProviderEvent::TextDelta(TextDeltaEvent { text }),
            ProviderEvent::MessageEnd(MessageEndEvent {
                stop_reason: StopReason::EndTurn,
                usage: Some(Usage { input_tokens: 3, output_tokens: 2, ..Usage::default() }),
                provider_metadata: None,
            }),
        ];
        Ok(futures::stream::iter(events.into_iter().map(Ok)).boxed())
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    // 1. The assembler checks the stream contract offline.
    let call = vec![
        ProviderEvent::MessageStart(MessageStartEvent { model: "m".into() }),
        ProviderEvent::ToolUseStart(ToolUseStartEvent { id: "c1".into(), name: "Read".into() }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: r#"{"file_path": "a.txt"}"#.into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason: StopReason::ToolUse,
            usage: None,
            provider_metadata: None,
        }),
    ];
    let turn = assemble(call).expect("a valid stream");
    assert_eq!(turn.message.tool_uses().count(), 1);

    // 2. The provider drives a run like any built-in adapter.
    let permissions =
        PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap();
    let agent = AgentBuilder::new(AgentSpec::new("echo-1"))
        .provider(Arc::new(Echo))
        .permissions(permissions)
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("docs")
        .pricing(|_model, usage| Some(usage.output_tokens as f64 * 1e-6))
        .build()
        .unwrap();
    let mut stream = agent.run("hello", RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        if let Event::Result(result) = item.unwrap().event {
            assert_eq!(result.final_text.as_deref(), Some("hello"));
            assert_eq!(result.total_cost_usd, Some(2e-6));
        }
    }
}
```

## Test adapter

- `ScriptedProvider` (`crates/linch-core/src/provider/scripted.rs`) phát lại các lượt cố
  định và ghi lại từng request; `TurnBuilder` viết các lượt văn bản, thinking,
  lời gọi tool và usage. Dùng nó cho test runner, eval và tool.
- Các stream đã ghi nằm trong `crates/linch-providers/tests/fixtures/*.sse`; các test
  stream phát lại chúng qua từng decoder.
- `crates/linch-providers/tests/live_*.rs` gọi API thật và bị bỏ qua khi
  biến khóa chưa được đặt.

---

Quay lại [mục lục kiến trúc](./README.md).
