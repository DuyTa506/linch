# Provider

[Bản gốc tiếng Anh](../../usage/providers.md)

Trang này giúp bạn chọn và cấu hình provider mô hình, cũng như xem các khả năng mà provider hỗ trợ.

[← Hướng dẫn sử dụng](./README.md)

Linch dùng chung các lớp trừu tượng cho vòng lặp agent, tool, event và bộ nhớ giữa các provider.
Các adapter khác nhau về khả năng, cơ chế lưu cache, điều khiển suy luận, định dạng
truyền qua API và yêu cầu đối với endpoint. Một **provider** chuyển `ProviderRequest` của Linch
thành lời gọi API và streaming các event đã chuẩn hóa về vòng lặp. Hãy chọn adapter
phù hợp với API bạn đang dùng.

---

## Khi nào nên chọn provider nào

| Bạn muốn gọi… | Sử dụng |
|---|---|
| Các mô hình OpenAI được thiết kế cho suy luận (`o1`, `o3`, `gpt-5`), với các điều khiển effort/summary và `previous_response_id` có trạng thái | `OpenAIResponsesProvider` |
| OpenAI Chat Completions tiêu chuẩn (`gpt-4o`, `gpt-5-nano`) | `OpenAIChatCompletionsProvider` |
| Các endpoint triển khai cấu trúc OpenAI Chat được hỗ trợ (bao gồm các bản triển khai Azure, Groq, Together tương thích) | `OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(base_url=...))` |
| API gốc của DeepSeek — điều khiển thinking, suy luận trong vòng lặp tool, đầu ra JSON-object | `DeepSeekProvider` |
| Anthropic Claude — thinking thích ứng, lưu cache prompt, các khối thinking có chữ ký | `AnthropicProvider` |
| Google Gemini — cửa sổ ngữ cảnh lớn, ngữ nghĩa tool của Google | `GeminiProvider` (extra `[gemini]`) |
| Máy chủ `llama.cpp` tự triển khai | `LlamaCppProvider` |
| Máy chủ vLLM tự triển khai | `VLLMProvider` |
| Máy chủ SGLang tự triển khai | `SGLangProvider` |

Hai khía cạnh quan trọng nhất:

- **Nối chuỗi hội thoại.** Theo mặc định, OpenAI Responses tự động dùng
  `previous_response_id` khi có, chỉ gửi phần hội thoại mới được thêm vào.
  Chỉ dẫn và tool vẫn được gửi. Bạn có thể tắt nối chuỗi bằng
  `OpenAIReasoning(previous_response_id="disabled")`. Các adapter kiểu Chat gửi
  lịch sử hội thoại với mỗi request; các cơ chế cache và trạng thái khác
  phụ thuộc vào adapter và endpoint.
- **Truyền lại thinking / nội dung suy luận.** Các mô hình suy luận phát ra nội dung
  "thinking" trung gian (các khối thinking của Anthropic, bản tóm tắt suy luận của OpenAI,
  hoặc `reasoning_content` từ các mô hình tương thích OpenAI như DeepSeek). Linch
  giữ lại metadata suy luận cho các lượt tool tiếp theo — xem
  [Đọc các event thinking](#đọc-các-event-thinking).

Để tìm hiểu luồng dữ liệu phía sau giao diện provider (`context_window`, `stream`,
`capabilities`), xem [Hợp đồng provider](../../architecture/provider-contract.md).

---

## Cấu hình provider

Linch cung cấp sẵn nhiều provider. Hãy chọn một provider dựa trên API bạn muốn gọi.

```python
import os
from linch import Agent
from linch import InMemorySessionStore

# ── OpenAI Responses API (o1, o3, gpt-5 — reasoning-native models) ──────────
# Chaining sends a conversation suffix when available; instructions/tools remain.
# Supports native reasoning effort/summary levels and encrypted reasoning tokens.
from linch import OpenAIResponsesProvider, OpenAIResponsesProviderOptions
from linch import OpenAIReasoning

agent = Agent(
    model="gpt-5",
    provider=OpenAIResponsesProvider(
        OpenAIResponsesProviderOptions(
            api_key=os.environ["OPENAI_API_KEY"],
            reasoning=OpenAIReasoning(effort="high"),
        )
    ),
    session_store=InMemorySessionStore(),
)

# ── OpenAI Chat Completions (gpt-4o, gpt-5-nano, compatible endpoints) ─────
# Stateless: full message array resent every turn.
# Requires an endpoint implementing the supported Chat Completions shapes.
# Thinking events emitted when model streams reasoning_content (e.g. DeepSeek).
# Set include_partial_messages=True to receive PartialAssistantEvent for streaming.
from linch import OpenAIChatCompletionsProvider
from linch import OpenAIChatProviderOptions

agent = Agent(
    model="gpt-5-nano-2025-08-07",
    provider=OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=os.environ["OPENAI_API_KEY"])
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,   # stream text + thinking deltas
)

# ── Anthropic Claude ─────────────────────────────────────────────────────────
# This example configures adaptive thinking and effort. Support for these
# controls and native output_config.format depends on the selected model/API.
# include_partial_messages=True streams ThinkingBlock deltas as kind="thinking" events.
from linch import AnthropicProvider, AnthropicProviderOptions

agent = Agent(
    model="claude-sonnet-4-6",
    provider=AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            thinking={"type": "adaptive"},
            effort="high",
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ``None`` leaves thinking at the model/provider default. To turn it off on a
# model that supports disabling it, send the native mode explicitly:
provider = AnthropicProvider(
    AnthropicProviderOptions(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        thinking={"type": "disabled"},
    )
)

# ── Google Gemini ────────────────────────────────────────────────────────────
# Requires: pip install "linch[gemini]" (uses google-genai)
# Supports text, tool use, structured output, and large context windows.
from linch import GeminiProvider, GeminiProviderOptions

agent = Agent(
    model="gemini-2.5-pro",
    provider=GeminiProvider(
        GeminiProviderOptions(api_key=os.environ["GOOGLE_API_KEY"])
    ),
    session_store=InMemorySessionStore(),
)

# Vertex AI uses the same SDK with application default credentials:
provider = GeminiProvider(
    GeminiProviderOptions(project="my-project", location="us-central1")
)

# Native GenerateContentConfig defaults, including thinking on supported models.
# Per-run thinking={"type": "enabled", "budget_tokens": 1024} overrides this.
# Native thinking_budget/thinking_level dictionaries also work per run.
# Budget ranges and support for disabling thinking depend on the model.
provider = GeminiProvider(
    GeminiProviderOptions(
        api_key=os.environ["GOOGLE_API_KEY"],
        default_generation_config={
            "thinking_config": {"thinking_budget": 1024, "include_thoughts": True},
        },
    )
)
# Thought text streams as thinking_delta; output usage includes thought tokens.
# Linch retains signed model parts in message metadata for subsequent tool turns.
# Keep provider_metadata when copying assistant messages. Tools execute in Linch.
# Gemini uses native JSON Schema for tool parameters and OutputSchema responses.
# Agent teardown closes the provider; standalone callers should await provider.aclose().

# ── DeepSeek native endpoint ────────────────────────────────────────────────
# DeepSeekProvider preserves reasoning_content for subsequent tool turns,
# sends a thinking toggle, and uses JSON-object mode for OutputSchema.
# Model availability and thinking behavior depend on the deployment.
# With thinking enabled, DeepSeek accepts automatic/none tool selection only:
# disable thinking before using a required or named tool_choice.
from linch import DeepSeekProvider, DeepSeekProviderOptions

agent = Agent(
    model="deepseek-v4-flash",
    provider=DeepSeekProvider(
        DeepSeekProviderOptions(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
            thinking="enabled",  # use "disabled" for no reasoning
            effort="high",
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── llama.cpp server ─────────────────────────────────────────────────────────
# Uses llama.cpp's OpenAI-compatible /v1/chat/completions route.
# Streaming uses stream=True and includes stream_options by default. Set
# include_stream_options=False for older servers that reject that field.
# The provider uses llama.cpp's response_format schema shape.
# Context window is auto-detected from /v1/props or /props when available.
from linch import LlamaCppProvider, LlamaCppProviderOptions

agent = Agent(
    model=os.environ["LLAMACPP_MODEL"],
    provider=LlamaCppProvider(
        LlamaCppProviderOptions(
            api_key=os.environ["LLAMACPP_API_KEY"],
            base_url=os.environ["LLAMACPP_BASE_URL"],
            chat_template_kwargs={"enable_thinking": False},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── vLLM server ──────────────────────────────────────────────────────────────
# Uses vLLM's OpenAI-compatible /v1/chat/completions route.
# Server-specific request fields can be passed through extra_body.
from linch import VLLMProvider, VLLMProviderOptions

agent = Agent(
    model=os.environ["VLLM_MODEL"],
    provider=VLLMProvider(
        VLLMProviderOptions(
            api_key=os.environ.get("VLLM_API_KEY", "EMPTY"),
            base_url=os.environ["VLLM_BASE_URL"],
            context_window=128_000,
            extra_body={"top_k": 40},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── SGLang server ────────────────────────────────────────────────────────────
# Uses SGLang's OpenAI-compatible chat completions endpoint.
# The provider sends OpenAI stream_options by default so SGLang streams token
# usage (input/output, plus cached_tokens when enable_cache_report is set). Set
# include_stream_options=False for servers that reject that OpenAI field.
# SGLang sampling/cache-report controls are exposed through extra_body.
from linch import SGLangProvider, SGLangProviderOptions

agent = Agent(
    model=os.environ["SGLANG_MODEL"],
    provider=SGLangProvider(
        SGLangProviderOptions(
            api_key=os.environ.get("SGLANG_API_KEY", "EMPTY"),
            base_url=os.environ["SGLANG_BASE_URL"],
            context_window=128_000,
            sampling_params={"top_p": 0.9},
            enable_cache_report=True,
            extra_body={"custom": "value"},
        )
    ),
    session_store=InMemorySessionStore(),
    include_partial_messages=True,
)

# ── DeepSeek via Anthropic-compatible endpoint (limited compatibility) ───────
agent = Agent(
    model="deepseek-v4-flash",
    provider=AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/anthropic",
            api_mode="compatible",
        )
    ),
    session_store=InMemorySessionStore(),
)
```

Một vài lưu ý thực tế về các đoạn mã ở trên:

- **`include_partial_messages=True`** bật streaming các `PartialAssistantEvent`
  (các phần tăng thêm của văn bản *và* thinking). Khi không bật, bạn
  vẫn nhận được `AssistantEvent` đầy đủ ở cuối mỗi lượt — hãy bật khi bạn
  muốn hiển thị token trên UI ngay khi chúng đến.
- **Dùng `DeepSeekProvider` cho endpoint gốc của DeepSeek.** Provider này giữ lại
  `reasoning_content` qua các lượt, gửi tùy chọn bật/tắt thinking của provider,
  và dùng định dạng JSON-object mà DeepSeek hỗ trợ. Đường dẫn `/anthropic` là
  API tương thích: nó bỏ qua `budget_tokens` và không hỗ trợ JSON schema
  `output_config.format` của Claude, nên không phù hợp cho câu trả lời cuối cùng
  của agent hỗ trợ cần tuân thủ cấu trúc nghiêm ngặt. Khi thinking gốc của DeepSeek đang bật,
  hãy dùng chế độ chọn tool tự động; endpoint từ chối `tool_choice` bắt buộc hoặc
  chỉ định tên, nên Linch báo lỗi rõ ràng ngay tại phía client thay vì gửi request nhận lỗi 400.
- **`llama.cpp`** lấy tên mô hình và cửa sổ ngữ cảnh từ máy chủ đang chạy
  (`/v1/props` hoặc `/props`), nên bạn cung cấp đúng id mô hình mà máy chủ
  trả về. Đặt `chat_template_kwargs={"enable_thinking": False}` để tắt
  đầu ra thinking ở các mô hình có phát ra nội dung này.

---

## Chọn cách kết nối provider

Dùng provider trực tiếp khi Linch có ngữ nghĩa riêng cho API đó: OpenAI
Responses cho các điều khiển suy luận có trạng thái, Anthropic cho các dấu mốc cache prompt
tường minh và chữ ký thinking của Claude, Gemini cho ngữ nghĩa mô hình/tool của Google,
và llama.cpp, vLLM hoặc SGLang cho máy chủ cục bộ tự triển khai.

Dùng `OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(base_url=...))` khi
một dịch vụ triển khai giao thức OpenAI Chat Completions. Đây là cách
kết nối Azure, Groq, Together và các bản triển khai tương tự khi chúng triển khai
các cấu trúc request và response mà adapter hỗ trợ. Dùng `DeepSeekProvider` cho
API gốc của DeepSeek, nơi provider này xử lý ngữ nghĩa thinking và
JSON-object riêng của DeepSeek.

Tên mô hình và cửa sổ ngữ cảnh của llama.cpp, vLLM và SGLang thuộc cấu hình
triển khai. `LlamaCppProvider` có thể tự phát hiện `n_ctx` từ máy chủ khi
có thông tin; vLLM và SGLang mặc định dùng `128_000` và cho phép bạn đặt
`context_window` tường minh. Các provider này không có trong danh mục
tĩnh.

---

<a id="inspecting-the-model-catalog"></a>

## `list_provider_models` and `get_provider_model_info`

Bạn có thể tra cứu các mô hình đã biết của provider trực tiếp mà không cần tạo
client kết nối thật — hữu ích khi xây dựng UI chọn mô hình, kiểm tra cấu hình lúc khởi động,
hoặc xem cửa sổ ngữ cảnh và giá của mô hình trước khi tiêu thụ một token:

```python
from linch import get_provider_model_info, list_provider_models

for model in list_provider_models("anthropic"):
    print(model.model, model.context_window, model.pricing)

info = get_provider_model_info("gemini-2.5-pro", provider_id="gemini")
print(info.capabilities.structured_output if info else None)
```

`get_provider_model_info` trả về `None` khi mô hình không có trong danh mục
tĩnh, nên hãy kiểm tra kết quả trước khi đọc các thuộc tính (như ví dụ trên).

Danh mục tĩnh bao gồm các provider trực tiếp được tích hợp sẵn. Danh mục này chủ ý
**không bao gồm** các mô hình tương thích OpenAI/cục bộ (DeepSeek, llama.cpp, vLLM, SGLang),
vì danh sách mô hình và cửa sổ ngữ cảnh của chúng đến từ cấu hình bên ngoài
thay vì một bảng cố định.

| Id provider | Cách truy cập | Danh mục tĩnh | Giá |
|---|---|---:|---|
| `openai-responses` | `OpenAIResponsesProvider` | Có | `None` trừ khi bạn cung cấp giá tùy chỉnh |
| `openai-chat` | `OpenAIChatCompletionsProvider` | Có | `None` trừ khi bạn cung cấp giá tùy chỉnh |
| `anthropic` | `AnthropicProvider` | Có | Các mục Claude đã biết từ `linch.pricing` |
| `gemini` | `GeminiProvider` | Có | `None` trừ khi bạn cung cấp giá tùy chỉnh |
| `llamacpp` | `LlamaCppProvider` | Không, động/tự triển khai | `None` trừ khi bạn cung cấp giá tùy chỉnh |
| `vllm` | `VLLMProvider` | Không, tùy bản triển khai | `None` trừ khi bạn cung cấp giá tùy chỉnh |
| `sglang` | `SGLangProvider` | Không, tùy bản triển khai | `None` trừ khi bạn cung cấp giá tùy chỉnh |
| `deepseek` | `DeepSeekProvider` | Không, tùy bản triển khai | `None` trừ khi bạn cung cấp giá tùy chỉnh |

Chỉ các mô hình Claude đã biết mới có sẵn thông tin giá. Với mọi provider khác,
các trường chi phí trên event usage/result trả về `None` cho đến khi bạn cung cấp
bảng giá tùy chỉnh — xem [Event](./events.md#các-trường-chi-phí) để biết cách thực hiện.

---

<a id="capability-flags"></a>

## `ProviderCapabilities`

Mỗi bản ghi trong danh mục cung cấp một đối tượng `ProviderCapabilities` khai báo
các tính năng mà provider hỗ trợ. Trước mỗi lời gọi provider, Linch điều chỉnh
`ProviderRequest` để phù hợp với các khả năng này — ví dụ, loại bỏ
`output_schema` khi provider khai báo không hỗ trợ đầu ra có cấu trúc, và xóa
các gợi ý cache prompt với provider không có cache. Bạn hiếm khi cần thao tác trực tiếp
với đối tượng này, nhưng nó giải thích vì sao cùng một cấu hình `Agent` vẫn hoạt động đúng
trên các backend khác nhau.

| Id provider | Đầu ra có cấu trúc | Lựa chọn tool | Cache prompt |
|---|---:|---:|---:|
| `openai-responses` | Có | Có | Có |
| `openai-chat` | Có | Có | Có |
| `deepseek` | Có (`json_object` + kiểm tra tại phía client) | Có | Có |
| `anthropic` | Có | Có | Có |
| `gemini` | Có | Có | Có |
| `llamacpp` | Có | Có | Có |
| `vllm` | Có | Có | Có |
| `sglang` | Có | Có | Có |

Mọi provider trực tiếp đều hỗ trợ đầu ra có cấu trúc, nhưng cách thực hiện khác nhau
theo backend (Claude gốc dùng `output_config.format`; chế độ tương thích Anthropic
được chọn tường minh dùng một tool được sinh từ schema cuối cùng). Cơ chế này — cùng cách
thử lại để sửa lỗi schema — được trình bày trong
[Đầu ra có cấu trúc](./structured-output.md).

---

## Thay đổi danh mục theo cơ chế gốc (cần bật)

`AnthropicProviderOptions(native_catalog_changes=True)` và
`OpenAIResponsesProviderOptions(native_catalog_changes=True)` bật thay đổi danh mục
theo vị trí trên các mô hình được hỗ trợ. **Cả hai tùy chọn đều mặc định là `False` và
CHƯA ĐƯỢC XÁC MINH với endpoint Anthropic và OpenAI thật.** Các bài kiểm thử payload dùng
transport giả. Các điều kiện cho phép phản ánh cách triển khai adapter, không đảm bảo
endpoint có hỗ trợ. Khi tùy chọn tắt, provider dùng cách kết xuất chỉ nối thêm.

Ví dụ, truyền `native_catalog_changes=True` cùng `api_key` trong tùy chọn của một trong
hai provider, rồi dùng mô hình được hỗ trợ. Provider chỉ báo
`ProviderCapabilities.catalog_changes="positional"` khi đủ điều kiện; nếu không,
khả năng này mặc định là `"append_only"`.

- Anthropic yêu cầu API Claude chính chủ, `api_mode="native"` thực sự có hiệu lực,
  và tên mô hình khớp chính xác với một tên trong danh sách cho phép: `claude-fable-5-1`,
  `claude-mythos-5-1`, `claude-fable-5`, `claude-mythos-5`, `claude-opus-5-5`,
  `claude-opus-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-opus-4-8`.
  Không so khớp theo tiền tố: `claude-sonnet-5` vẫn chỉ nối thêm. `base_url`
  được đặt tường minh có ưu tiên cao hơn `ANTHROPIC_BASE_URL`; URL thực sự dùng phải
  có hostname `api.anthropic.com` (hoặc dùng endpoint mặc định). Đặt
  `api_mode="native"` không cho phép dùng proxy. Bedrock và Google Cloud vẫn
  chỉ nối thêm. Mỗi request theo vị trí đều bao gồm `inline-tools-2026-09-15`.
- Responses yêu cầu tên mô hình GPT xác định phiên bản 5.4 trở lên, bao gồm
  cả hậu tố ngày/biến thể. Các tên không xác định và `gpt-5.3` vẫn chỉ nối thêm.

Khi cấu hình snapshot danh mục cho session, session cố định tiền tố system
và các định nghĩa tool ban đầu trong metadata được lưu bền vững. Các bổ sung theo cơ chế gốc
được đưa vào các khối hội thoại bền vững trong khi tiền tố tool đó giữ nguyên.
Văn bản thông báo vẫn nằm trong thông điệp mang vai trò user, bao gồm thông báo MCP chỉ có tên; văn bản này không bao giờ có thẩm quyền system/developer.
Anthropic gửi các thông điệp system chỉ chứa tool, gồm định nghĩa `tool_addition`
hoặc tham chiếu đến tiền tố đã khai báo và tham chiếu `tool_removal`. Responses gửi
các mục `additional_tools` tại vị trí tương ứng trong log và duy trì lựa chọn
`allowed_tools` theo các tên đang hoạt động cho việc chọn tự động. Các lựa chọn bắt buộc và `"none"`
được giữ nguyên.

Trên Anthropic, việc xóa một tool được đưa vào bằng giá trị chỉ thể hiện bằng văn bản:
trên API, tool vẫn khả dụng, và việc gọi nó trả về lỗi tool không xác định.
Khôi phục tool không gửi lại định nghĩa của nó. Thông tin nguồn gốc lịch sử về việc đưa tool vào
bằng giá trị vẫn được giữ lại, nên quy tắc này vẫn áp dụng ngay cả sau khi nâng cấp schema. Các lần bổ sung trùng lặp
được loại bỏ sau khi nén ngữ cảnh. Anthropic từ chối ngay tại phía client khi tổng
các định nghĩa nội tuyến đã chuyển đổi vượt quá 4.194.304 byte UTF-8, với lỗi `ConfigError`; hãy bắt đầu
session mới hoặc tắt tùy chọn nếu chạm giới hạn này.

Việc sửa schema đầu vào, tiền tố tool ban đầu rỗng, `allowed_tools` ở cấp run và
việc thu hẹp tool theo ngữ cảnh dùng cách kết xuất chỉ nối thêm, chấp nhận cache miss khi
schema tool thay đổi. Các lớp ghi đè từ profile, ghi đè tool của session, ghi đè khối system
thực sự có hiệu lực và danh mục request đang hoạt động đều bỏ qua snapshot.
Khi chuyển sang phương án dự phòng chỉ nối thêm, danh sách tool sẽ bao gồm các định nghĩa gốc
đã thêm trước đó. Session mới đưa danh mục hiện tại vào tiền tố riêng của mình;
việc nén ngữ cảnh giữ nguyên các byte của tiền tố hiện có và hợp nhất thông báo.

Với run đã bật cơ chế gốc và có snapshot hiện hữu, việc đồng bộ danh mục chờ
đến khi xác định được ngữ cảnh được chọn. Vì vậy, các hook ngữ cảnh có thể chạy trước khi
thông báo mới được nối thêm.
Xem [hợp đồng provider](../../architecture/provider-contract.md#native-catalog-rendering)
để biết chi tiết về vị trí trong dữ liệu gửi qua API và việc phát lại.

---

## Lưu cache prompt

Mọi provider trực tiếp đều báo `prompt_cache=True`, nhưng cơ chế trên API
khác nhau:

| Provider | Cơ chế | Linch gửi/đọc gì |
|---|---|---|
| `anthropic` | Các điểm ngắt `cache_control` tường minh | Đánh dấu khối system tĩnh cuối cùng **và** thông điệp user cuối cùng — lưu cache `tools → system → conversation`, với tiền tố dài thêm sau mỗi lượt |
| `llamacpp` | `cache_prompt` tường minh | Gửi `cache_prompt: true` trong phần thân request |
| `sglang` | Cache do máy chủ quản lý | `enable_cache_report: true` yêu cầu báo cáo token được cache; khả năng này phụ thuộc cấu hình máy chủ |
| `openai-chat` | Cache tiền tố tự động | Yêu cầu thông tin usage qua `stream_options.include_usage`; đọc chi tiết token được cache khi được trả về |
| `deepseek` | Cache do máy chủ quản lý | Yêu cầu thông tin usage trong luồng streaming và chuẩn hóa số liệu cache trả về qua cơ chế OpenAI-chat |
| `openai-responses` | Cache tiền tố tự động | Đọc chi tiết token được cache từ usage của response |
| `vllm` | Cache tiền tố tự động (APC) được cấu hình trên máy chủ | Dùng cơ chế usage của OpenAI-chat; khả năng dùng cache phụ thuộc cấu hình máy chủ |
| `gemini` | Cache ngầm do provider quản lý | Đọc số token được cache khi response cung cấp; adapter này không gửi thiết lập điều khiển cache tường minh |

Bất kể cơ chế nào, cache **hit** đều được biểu diễn thống nhất qua
`Usage.cache_read_tokens`, rồi được tổng hợp vào tỷ lệ đọc cache của `RunReport` —
các số liệu được báo cáo này phản ánh việc tái sử dụng cache đã quan sát được.

Để kiểm tra trực tiếp hành vi cache prompt hiện tại của provider, chạy:

```bash
python scripts/benchmark_live_prompt_cache.py
```

Lệnh này đọc các khóa `.env` theo kiểu DeepSeek (`API_KEY`, `model`, `BASE_URL`,
`ANTHROPIC_BASE_URL`) và báo cáo `Usage.cache_read_tokens` /
`Usage.cache_creation_tokens` thực tế qua các lần gọi lặp lại với tiền tố ổn định. Dùng
`--prefix-salt "$(date +%s)"` khi bạn muốn có tiền tố mới thay vì tái sử dụng
cache provider đã được làm nóng bởi lần chạy benchmark trước. Theo mặc định, các phép so sánh provider
được tách biệt: script đưa tên provider vào tiền tố ổn định.
Chỉ truyền `--shared-prefix` khi bạn chủ ý muốn kiểm tra xem hai
endpoint có dùng chung cache prompt ở backend hay không.

Mọi adapter đều chuẩn hóa usage của cache prompt thành các nhóm token riêng:
chúng báo cáo token prompt chưa được cache trong `input_tokens` và cache hit trong
`cache_read_tokens` (với lượt ghi cache trong `cache_creation_tokens`). Khi chưa chắc,
hãy dùng các cột `cache_read` theo từng lời gọi; các lời gọi đã làm nóng cache có số token đọc gần bằng
kích thước toàn bộ prompt cho thấy cache đang hoạt động.

Để đo hành vi cache qua vòng lặp agent Linch thật và các lượt
trả kết quả tool, dùng:

```bash
python scripts/benchmark_live_prompt_cache.py --mode tool-loop --tool-turns 3
```

Để chạy bộ kịch bản cache — vòng lặp tool cơ sở, ngữ cảnh được chèn ổn định/thay đổi,
lựa chọn tool luân phiên, nén ngữ cảnh và điều hướng mô hình — dùng:

```bash
python scripts/benchmark_live_prompt_cache.py \
  --mode tool-loop \
  --scenario all \
  --tool-turns 3 \
  --prefix-salt "$(date +%s)"
```

Để điều hướng mô hình, truyền tường minh một mô hình thay thế:

```bash
python scripts/benchmark_live_prompt_cache.py \
  --mode tool-loop \
  --scenario model-steering \
  --steering-model deepseek-chat
```

Để kiểm tra ngoại tuyến khả năng cache của cấu trúc request, chạy:

```bash
python scripts/benchmark_prompt_cache.py
```

Benchmark ngoại tuyến dùng provider giả để mô phỏng việc đọc và ghi cache tiền tố qua
vòng lặp tool. Cách này hữu ích để so sánh các tool ổn định, ngữ cảnh thay đổi và
lựa chọn tool theo từng lượt mà không cần gọi API thật. Nó **không** chứng minh hành vi
cache thực tế phía máy chủ; hãy dùng usage của provider (`cache_read_tokens`) hoặc
script benchmark trực tiếp (`scripts/benchmark_live_prompt_cache.py`) cho việc đó.

### Giữ tiền tố được cache ổn định (điều gì tạo ra cache hit)

Việc tái sử dụng cache phụ thuộc vào quy tắc so khớp tiền tố của provider và tiền tố
request ổn định. Linch tái sử dụng văn bản system ổn định cho đến khi đầu vào prompt hoặc tool thay đổi,
xác định `cwd` lúc khởi tạo và tạo ngày cho mỗi thông điệp user.
Ngữ cảnh động được nối sau các khối system tĩnh với `cacheable=False`.
Hai thao tác do ứng dụng chủ kiểm soát có thể làm giảm việc tái sử dụng cache:

- **Nén ngữ cảnh** viết lại lịch sử thông điệp thành bản tóm tắt, nên tiền tố thông điệp
  thay đổi từ thời điểm đó. Việc tái sử dụng phần tool và tiền tố system
  không đổi phụ thuộc vào provider.
- **Lựa chọn tool theo từng lượt** — một `ContextBuilder` trả về tập
  `selected_tools` khác nhau ở mỗi lượt sẽ thay đổi các schema tool được cung cấp và có thể
  làm mất hiệu lực tiền tố đã cache. Hãy giữ tập tool ổn định qua các lượt nếu bạn
  phụ thuộc vào cache.

Hai bảo đảm củng cố điều này. Thứ nhất, `system_blocks` trong ngữ cảnh theo từng lượt luôn
bị buộc thành `cacheable=False` trong request gửi qua API — `ContextBuilder` không thể chuyển
điểm ngắt cache của Anthropic sang văn bản RAG thay đổi, ngay cả do nhầm lẫn. Thứ hai,
các lựa chọn tool tương đương có thứ tự ổn định: `selected_tools={"A", "B"}` và
`{"B", "A"}` tạo ra schema tool giống hệt nhau từng byte (việc chọn tuân theo thứ tự
chèn của registry cha, không theo thứ tự duyệt tập hợp), nên một tập hợp không có thứ tự không bao giờ
âm thầm xáo trộn tiền tố.

### Event `prompt_cache_advisory`

Khi tập tool hoặc mô hình thay đổi giữa các lời gọi provider trong cùng một
session, Linch phát ra
[`PromptCacheAdvisoryEvent`](./events.md) mang tính quan sát (`type == "prompt_cache_advisory"`)
với `reason` là `"tool_set_changed"` hoặc `"model_changed"` và `detail`
dễ đọc. Đây là **cảnh báo, không phải thao tác viết lại** — Linch không bao giờ tự sửa
`selected_tools` cho bạn (đó là chính sách, không phải cơ chế); nó chỉ cho bạn biết
rằng tiền tố đã cache đã mất hiệu lực để bạn quyết định việc thay đổi đó có chủ ý hay không.
Số lượng và lý do cảnh báo cũng được tổng hợp vào khối tóm tắt `prompt_cache`
của `RunReport` (`advisory_count`, `reasons`, `tool_selection_changes`) cùng với
`cache_read_ratio`.

Danh sách kiểm tra để có cache hit ổn định:

- **Tool ổn định** (không luân phiên `selected_tools`).
- **System prompt tĩnh**.
- **Nội dung RAG/tạm thời nằm sau tiền tố tĩnh**.
- Dùng `Agent(cache_ttl="5m")` hoặc `Agent(cache_ttl="1h")` cho các giá trị
  TTL của điểm ngắt Anthropic được hỗ trợ.

---

## Đọc các event thinking

Bất kỳ provider nào phát ra `reasoning_content` (DeepSeek và các mô hình suy luận
khác tương thích OpenAI) hoặc các khối thinking của Anthropic đều streaming nội dung
suy luận trung gian dưới dạng event `partial_assistant` với `delta["kind"] == "thinking"`. Điều này
đòi hỏi đặt `include_partial_messages=True` trên `Agent`. Phân biệt thinking
với văn bản trả lời bằng trường `kind`:

```python
async for event in session.run("What is 17 × 23?"):
    if event.type == "partial_assistant":
        if event.delta.get("kind") == "thinking":
            print("thinking:", event.delta["text"], end="", flush=True)
        elif event.delta.get("kind") == "text":
            print(event.delta["text"], end="", flush=True)
    elif event.type == "result":
        print("\nanswer:", event.final_text)
```

Các phần tăng thêm của thinking chỉ dùng để hiển thị — vòng lặp tự động thu thập và truyền lại
các chữ ký suy luận bên dưới, nên bạn không bao giờ cần tự gửi lại chúng.
Để xem đầy đủ các loại event mà luồng này tạo ra, xem
[Event](./events.md).

---

## Các trang liên quan

- [Event](./events.md) — các loại event mà `partial_assistant`/`usage`/`result` mang theo.
- [Đầu ra có cấu trúc](./structured-output.md) — cách `output_schema` được áp dụng theo từng provider.
- [Hook](./hooks.md) — quan sát các lời gọi provider và luồng event trực tiếp.
- [Hợp đồng provider](../../architecture/provider-contract.md) — giao diện `BaseProvider` và quy trình xử lý request.
