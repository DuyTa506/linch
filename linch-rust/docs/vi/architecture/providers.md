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
- Usage không xác định là `None`, không bao giờ là một `Usage` toàn số không; số
  không nghĩa là đã biết chắc không tốn gì ([bên dưới](#retry-và-fallback-model)).
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

`ProviderError` (`client.rs:58`) có sáu variant. Thông báo do adapter viết và không bao
giờ được chứa thông tin xác thực hay header của request.

| Variant | Ý nghĩa | `retryable()` |
|---|---|---|
| `Connect { message }` | Kết nối chưa bao giờ được tạo (bị từ chối, DNS, TLS handshake, connect timeout), nên request chưa bao giờ được gửi đi. Mỗi adapter ánh xạ `is_connect()` của reqwest sang nó trong `transport` (`anthropic/http.rs:66`, `openai_compat/http.rs:66`, `gemini/http.rs:63`) | có |
| `Transport { message }` | Request đã gửi đi nhưng không có response nào về (reset, read timeout), hoặc stream bị đứt sau khi đã bắt đầu | có |
| `Status { status, retry_after, message }` | Mã trạng thái HTTP không thành công | chỉ 408, 429 và >= 500 |
| `InvalidRequest { message }` | Adapter từ chối request như đã được dựng | không |
| `Stream(StreamError)` | Event stream vi phạm hợp đồng stream | không |
| `Cancelled` | Cancel token đã kích hoạt | không |

Các adapter không bao giờ retry (`crates/linch-providers/src/lib.rs:4`); `retryable()`
và `retry_after` là lời khuyên cho bên gọi (`client.rs:1-5`). Runner chính là bên gọi
đó. `dispatch` của nó (`crates/linch-core/src/runner/turn.rs:289`) kết thúc run với
trạng thái `failed` cùng một event `Error` ở mọi lỗi mà nó không retry hay failover
(bên dưới), và abort run ở `Cancelled`. Event lỗi mang `kind`, `retryable` và `status`
(`crates/linch-core/src/provider/call.rs:325`).

### Retry và fallback model

Hai trường của spec biến một lỗi có thể retry thành một lời gọi khác thay vì kết thúc
run (`crates/linch-core/src/reliability.rs`, áp dụng trong
`crates/linch-core/src/runner/retry.rs`). Cả hai đều là một phần của
hợp đồng của run, và được các run con của subagent kế thừa.

| Trường | Mặc định | Tác dụng |
|---|---|---|
| `provider_retry` | bật | `ProviderRetry` (`reliability.rs:46`): `max_attempts` 5 (số lời gọi tới một model trong một turn, kể cả lời gọi đầu), `base_delay_ms` 1000, `max_delay_ms` 30000, `jitter` 0.2, là `RetryOptions` của Python. `max_attempts: 1` tắt nó |
| `fallback_models` | `[]` | Các model để chuyển sang, theo thứ tự. Không để trống, không trùng `model` của spec, không lặp (`reliability.rs:193`) |

`AgentSpec::provider_retry` là một `ProviderRetry`, không phải `Option`: serde điền
giá trị mặc định cho trường thiếu và không đưa giá trị mặc định vào wire
(`ProviderRetry::is_default`, `reliability.rs:80`). Vì vậy một spec không đặt nó, `{}`,
và các giá trị mặc định viết đầy đủ là cùng một spec, với dạng wire, và digest hợp
đồng, của một spec viết trước khi có retry. Một run bắt đầu trước khi có retry được
resume dưới cùng hợp đồng và retry từ đó trở đi; nó không có event `provider_retry`,
nên bộ đếm của turn bắt đầu từ không. `{"max_attempts": 1}` là một hợp đồng khác. `null`
là lỗi decode: tắt được viết là `{"max_attempts": 1}`.

Sau một lời gọi thất bại, `Runner::recover` (`retry.rs:45`) quyết định, theo thứ tự:

1. Một lỗi không `retryable()`, hoặc một budget từ chối lời gọi kế tiếp, kết thúc run
   như trước.
2. **Retry cùng model.** `ProviderRetry::delay` (`reliability.rs:112`) cho thời gian
   chờ trước lần retry `n`: `base_delay_ms * 2^(n-1)`, bị chặn ở `max_delay_ms`, lệch
   đi tối đa `jitter` của chính nó (lấy từ id của delivery thất bại, để các run cùng
   lỗi tản ra), không bao giờ vượt mức chặn. `Retry-After` thay thế nó, không jitter;
   một giá trị dài hơn `max_delay_ms` cho ra không chờ, để run không giữ lease của nó
   trong một khoảng thời gian do server chọn. Model không nhận thêm lời gọi nào khi nó
   đã có `max_attempts` lời gọi trong turn. Mỗi turn bắt đầu với đủ ngân sách của nó.
3. **Failover.** Khi không còn retry, run lấy model kế tiếp trong `fallback_models` và
   gọi nó ngay, với ngân sách retry mới. Nó giữ model đó cho đến hết run, kể cả các
   lời gọi tóm tắt compaction (`retry.rs:98`). Một turn đã failover sẽ gọi model
   fallback ngay cả khi một skill đã nạp chỉ định model; một turn skill sau đó lại
   nhận model của skill, như trong Python (`retry.rs:105`).
4. Nếu không có cả hai, lỗi cuối cùng kết thúc run.

Mỗi lần thử là một dispatch riêng: `Step::Dispatch` với `attempt` kế tiếp dựng lại
request, chạy lại `context_build` và `before_provider_call`, stage một delivery mới và
flush nó cùng `ProviderPending` trước lời gọi. Vì vậy `attempt` của delivery tăng theo
mỗi lần retry hoặc failover, như khi một run được resume gửi lại một lời gọi chưa bao
giờ kết thúc (`Recovery::Redispatch`, `runner/start.rs:227`). Mỗi lần thử kết thúc bằng
một receipt.

Một receipt được chấp nhận bền vững trước khi bất kỳ hook `provider_call_stop` hay
`after_provider_call` nào chạy trên lời gọi của nó. Một lần resume sau khi crash trong
một hook như vậy tìm thấy receipt được chấp nhận mà không có response của nó (một
orphan), giữ usage và chi phí của nó trong các tổng và trong budget, rồi dispatch lại
lời gọi dưới attempt kế tiếp; đó không phải một lần retry, nên không có event
`provider_retry` nào được ghi và các lần retry của lượt không bị đụng tới
([vòng đời của lượt](./turn-lifecycle.md#một-lần-crash-giữa-receipt-và-response-của-nó)).

Usage của một receipt thất bại là số không đã biết khi lời gọi không tốn gì, và không
rõ (`None`) khi nó có thể đã bị tính tiền (`costs_nothing`, `provider/call.rs:257`, áp
dụng ở `call.rs:169`):

| Lỗi | Usage của receipt | `CallFailure::free` |
|---|---|---|
| `Connect`: request chưa bao giờ được gửi đi | `Some(zero)` | true |
| `Status` 4xx (kể cả 408 và 429), 503 hoặc 529 trước bất kỳ event stream nào: provider từ chối nó | `Some(zero)` | true |
| `Status` 500, 502 hoặc 504 trước bất kỳ event stream nào: một gateway có thể gửi nó sau khi model upstream đã chạy và tính tiền | `None` | false |
| Mọi `Status` khác trước bất kỳ event stream nào (501, 505, 520-524, ...) | `None` | false |
| `Transport`: đã gửi, không có gì trả về | `None`, trừ khi một `message_end` báo usage | false |
| Bất cứ gì sau event stream đầu tiên, kể cả `Status` | `None`, trừ khi một `message_end` báo usage | false |

`Option<Usage>` đã phân biệt số không với không rõ, nên định dạng receipt không đổi, và
một binary trước thay đổi này đọc `Some(zero)` như một lời gọi không tốn gì. Một lỗi
free không gửi event `usage` (`runner/turn.rs:397`, và tương tự trong
`runner/compact.rs`), như Python không gửi cho một lời gọi thất bại, và để
`usage_complete` là true. Một `budget` có giới hạn chỉ từ chối lời gọi kế tiếp sau một
usage không rõ (`budget.rs:110`), nên một lỗi free được retry dưới giới hạn; một lỗi có
thể đã bị tính tiền vẫn kết thúc run với lỗi của provider. Vì vậy một 500, 502 hoặc 504 không được retry dưới
giới hạn, dù nó có thể retry và được retry khi không có giới hạn. Python đánh dấu usage của
mọi lời gọi thất bại là `unknown` (`src/linch/loop/streaming.py:412-415`) rồi từ chối
mọi lời gọi dưới giới hạn (`src/linch/model_calls.py:643-650`, kiểm tra trước mỗi delivery, kể cả một lần retry, tại `src/linch/loop/streaming.py:467`), nên dưới giới hạn nó
không bao giờ retry.

Hai event, cả hai đều bền vững:

| Event | Khi nào | Được ghi |
|---|---|---|
| `provider_retry` (`model`, `retry`, `delay_ms`, `error`) | Trước lúc chờ | Trong một lần ghi riêng cùng receipt thất bại, `Usage` của nó (nếu có thể đã tốn gì), và `ProviderPending` được lưu lại ở tổng đã tính chúng (`retry.rs:69`), để bên gọi thấy nó trước lúc chờ |
| `model_fallback` (`from_model`, `to_model`, `error`) | Trước lời gọi của model fallback | Cùng receipt thất bại, trên barrier của dispatch kế tiếp |

`error` là lỗi theo hình dạng của event `error`. `model_fallback` của Python có một
văn bản `reason` thay cho nó, và Python thử các model fallback trước các lần retry cùng
model; Rust dùng hết các lần retry của một model trước.

Resume đọc vị trí của một run từ các event này (`Standing::of`, `reliability.rs:161`,
được gọi trong `runner/start.rs:289`): `model_fallback` cuối cùng cho biết model, và
các event `provider_retry` kể từ `assistant` cuối cùng (hoặc lần failover cuối cùng) là
các lần retry đã dùng của turn. Không có gì được thêm vào checkpoint. Một lần crash
trong lúc chờ resume với lần thử kế tiếp ngay, không chờ nốt phần còn lại; receipt thất
bại của nó đã bền vững, nên nó không bị mất cũng không bị kết thúc hai lần. Một lần
crash trước barrier của một failover để model cũ với các lần retry đã dùng hết, nên run
được resume failover lại; một lần crash sau nó resume trên model fallback. Một lần hủy
trong lúc chờ abort run ngay (`retry.rs:73`).

Khi không có lỗi, một run bật retry và đặt model fallback ghi cùng các event và
checkpoint, và gửi cùng các request, như một run tắt retry và không có fallback (được ghim bởi
`reliability_costs_nothing_when_no_call_fails`,
`crates/linch-core/tests/e2e_provider_retry.rs`).

Phần phục hồi độ dài ngữ cảnh của Python (compaction
bắt buộc và một lần retry khi provider báo prompt quá dài) không được port: các adapter
Rust báo đó là một 400 không thể retry.

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
và mọi `output_schema`, thất bại ngay tại chỗ với `InvalidRequest`:
DeepSeek không có response format JSON-schema, chế độ `json_object` không
mang schema, và việc ép một tool cuối bị từ chối khi thinking bật.
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
bỏ ra (`profile.rs:144-154`). Một `output_schema` được gửi dưới dạng
`response_format: {"type": "json_schema", "json_schema": {name, strict,
schema, description?}}` khi quy tắc `json_schema` của profile bật, mà chỉ
profile của OpenAI bật; vLLM, SGLang và llama.cpp từ chối nó ngay tại chỗ với
`InvalidRequest` cho đến khi từng cái được đối chiếu với tài liệu của server.

### OpenAI Responses

`crates/linch-providers/src/responses.rs`. Các block system trở thành
`instructions`. Function tool được gửi với `strict: false`, để API không
viết lại một schema sang chế độ strict. Các output item của mỗi response,
kể cả reasoning, được giữ trong `provider_metadata` và gửi lại nguyên văn;
không gửi `previous_response_id`, nên mọi request đều mang đầy đủ
lịch sử. `ResponsesOptions::store` tắt sẽ gửi `store: false` và yêu cầu reasoning
được mã hóa. Stop sequence bị từ chối: API không có tham số như vậy. Một `output_schema` được gửi dưới dạng `text.format: {"type":
"json_schema", name, strict, schema, description?}` (`responses/payload.rs`).

### Anthropic

`crates/linch-providers/src/anthropic.rs`. `anthropic-version` được ghim ở
`2023-06-01` (`anthropic.rs:44`) vì phiên bản mới hơn bỏ
`temperature`, thứ `ProviderRequest` vẫn mang. `max_tokens` là
bắt buộc và mặc định 8192 (`anthropic.rs:47`). Các block thinking được gửi
lại với chữ ký không đổi. `cache_control` của một request đánh dấu
block system cacheable cuối cùng và block cuối của message user cuối
(`anthropic/payload.rs:39`).
Một `output_schema` được gửi dưới dạng `output_config.format: {"type": "json_schema",
"schema": ...}`, bên cạnh `output_config.effort` (`anthropic/payload.rs`). Nó
không cần beta header và hoạt động với tool, thinking và streaming; nó chỉ định hình
phần text, nên model vẫn gọi tool. Không dùng tool cuối bị ép:
các model Claude hiện tại từ chối một `tool_choice` bắt buộc khi thinking bật.
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
(`gemini/stream.rs:293`). Một `output_schema` được gửi dưới dạng
`generationConfig.responseMimeType: "application/json"` cùng với
`responseJsonSchema` (`gemini/payload.rs`); Gemini kết hợp nó với function
calling từ Gemini 3 trở đi. Module ghi chú rằng adapter này chưa chạy
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
