# Memory

[Bản gốc tiếng Anh](../../usage/memory.md)

Dùng trang này để giữ một run dài nằm trong context window của model, để khôi phục khi
nó không vừa hoặc một câu trả lời bị cắt, và để cho một agent một bộ nhớ sống lâu hơn các
session của nó.

Một agent ghi nhớ ở ba nơi:

| Cái gì | Nằm ở đâu | Thiết lập bằng |
|---|---|---|
| Cuộc hội thoại của một session | Log của session, trong store của nó; model nhìn thấy một view của log | Không cần gì: session nào cũng có |
| Một view ngắn hơn của một cuộc hội thoại dài | Một bản tóm tắt (hoặc các kết quả tool đã xóa) được ghi log trong session | [`spec.compaction`](#compaction) |
| Các sự thật agent giữ trong tệp của chính nó | `/memories/MEMORY.md` trong hệ thống tệp ảo | [`filesystem.memory`](#file-memory) |
| Các sự thật giữ qua nhiều session | Store của chính bạn: tệp, một vector index, Notion, một database | Một [callback `context_build`](#external-memory) để đọc nó, một host tool để ghi nó |

Hai trường spec nữa khôi phục một lời gọi model đi sai vì kích thước:
[một prompt dài hơn context window](#when-the-prompt-does-not-fit)
và [một câu trả lời bị giới hạn đầu ra cắt](#cut-off-answers).

---

<a id="compaction"></a>

## Compaction

Một run compact ở đầu một lượt, trước khi gọi model, khi view sắp gửi đến hạn. Nó gộp phần
cũ của view thành một bản tóm tắt và giữ nguyên các lượt cuối. Session giữ mọi message;
chỉ những gì model nhìn thấy là ngắn đi.

```python
from linch_rust import AgentConfig

config: AgentConfig = {
    "namespace": "support",
    "spec": {
        "model": "claude-sonnet-4-6",
        "compaction": {"keep_recent_turns": 6, "micro": True},
    },
    "provider": {"id": "anthropic"},
}
```

`"compaction": {}` bật nó với mọi giá trị mặc định. Khi vắng mặt, một run không bao giờ
compact. Các trường, có kiểu `Compaction`:

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `threshold_tokens` | không có: theo context window | Compact một view được ước tính từ số token này trở lên (khoảng bốn ký tự mỗi token, 1000 mỗi ảnh). Nếu không có, compact khi ước lượng cộng với phần dự trữ cho đầu ra đạt 80% [context window](./providers.md#context-window) của provider |
| `keep_recent_turns` | `10` | Số lượt assistant được giữ nguyên văn sau bản tóm tắt; tối thiểu 1 |
| `prompt` | prompt tích hợp sẵn | Chỉ dẫn cho lời gọi tóm tắt. `DETAILED_COMPACTION_PROMPT` là `DetailedCompaction` của Python Linch (bên dưới) |
| `max_output_tokens` | `4096` | Số token nhiều nhất bản tóm tắt được dùng; tối thiểu 1 |
| `micro` | `False` | Xóa các kết quả tool cũ trước khi tóm tắt ([bên dưới](#micro-compaction)) |
| `max_forced` | `3` | Số bản tóm tắt nhiều nhất một run tạo ra vì provider từ chối một prompt là quá dài ([bên dưới](#when-the-prompt-does-not-fit)); `0` là không bao giờ |

Một bản tóm tắt là một lời gọi provider thêm trên provider và model của agent, với usage
riêng, được tính vào [budget](./agent.md#budget). Nó được báo cáo bằng một
[event](./events.md#compactionevent-compaction) `compaction` với `strategy` là `summary`.
Các callback `pre_compact` và `post_compact` nhìn thấy nó.

Spec compaction là một phần của [run contract](./agent.md#the-run-contract): một run bắt
đầu dưới một cài đặt không được resume dưới một cài đặt khác.

### Model nhìn thấy gì sau đó

```text
system blocks and tools      unchanged, byte for byte
first message                unchanged
<summary of earlier conversation> ... (a user message)
the last keep_recent_turns assistant turns, and what follows them
```

Các block system, các tool và message đầu tiên không đổi, nên prefix đã cache của
provider cho đến chúng vẫn còn sau một lần compaction. Khi skill được bật, bản tóm tắt
còn nhắc lại từng skill mà model đã nạp; với một tool catalog đã đổi trong session, nó
nhắc lại các thay đổi.

Một agent workspace cũng quên những gì model đã đọc: sau một lần compaction, model phải
`Read` lại một tệp trước khi có thể `Edit` hoặc `Write` nó, vì nội dung nó đã đọc có thể
đã bị gộp đi (`reset_read_tracker` của Python Linch).

<a id="micro-compaction"></a>

### Micro-compaction

Với `micro`, một view đến hạn được xóa trước, không có lời gọi model: mọi kết quả tool
trước các lượt được giữ được thay bằng `[tool result elided to save
context]`, trừ khi nó đã không dài hơn thế và không chứa ảnh. Mỗi lời gọi tool giữ kết
quả của nó, nên view vẫn hợp lệ với mọi provider. Nếu view đã xóa không còn đến hạn, lượt
tiếp tục với nó và không có bản tóm tắt nào được tạo (`strategy` là `micro`). Nếu nó vẫn
đến hạn, bản tóm tắt được tạo từ view đã xóa.

Việc xóa thay đổi các message ở giữa view, nên prefix đã cache kết thúc ở kết quả bị xóa
đầu tiên, trong khi một bản tóm tắt sẽ kết thúc nó ở bản tóm tắt. Hãy để `micro` tắt khi
cache quan trọng hơn một lời gọi tóm tắt.

### Prompt chi tiết

`DetailedCompaction` của Python Linch là một prompt tóm tắt gồm sáu mục đánh số (yêu cầu,
các artifact chính, lỗi và cách sửa, các việc đang chờ, công việc hiện tại, bước kế tiếp),
được viết để công việc có thể tiếp tục chỉ từ bản tóm tắt. Nó có sẵn dưới dạng
`DETAILED_COMPACTION_PROMPT`:

```python
from linch_rust import DETAILED_COMPACTION_PROMPT

spec = {
    "model": "claude-sonnet-4-6",
    "compaction": {"prompt": DETAILED_COMPACTION_PROMPT, "max_output_tokens": 8192},
}
```

---

<a id="when-the-prompt-does-not-fit"></a>

## Khi prompt không vừa

Mỗi provider từ chối một prompt dài hơn context window của model bằng lời riêng của nó.
Adapter nhận ra lần từ chối đó (bảng bên dưới) và run đọc nó là `context_length`, không
phải một lỗi thông thường: một lời gọi y hệt khác không thể thành công, nên nó không bao
giờ được [thử lại](./providers.md#lỗi-và-thử-lại).

Với `spec.compaction` được đặt, run rút ngắn view của nó và gọi lại, theo thứ tự này:

1. Với `micro`, nó xóa các kết quả tool cũ, nếu có cái nào đáng xóa.
2. Nếu không, nó tạo một bản tóm tắt, giữ `keep_recent_turns` (hoặc chỉ lượt cuối, khi
   đó chính là thứ không vừa). Mỗi bản tính vào `max_forced`.
3. Khi không bước nào rút ngắn được view (không còn gì để gộp, hoặc `max_forced` đã hết),
   hoặc lời gọi tóm tắt thất bại, run kết thúc.

Mỗi bước là một event `compaction` với `forced` là true. Lời gọi theo sau là lần thử kế
tiếp của cùng lượt đó, không phải một retry của provider, và không có event
`provider_retry` nào được gửi cho nó. Một lần từ chối từ một lời gọi không tốn gì thì
không bị tính phí.

Khi không có `spec.compaction`, hoặc khi các bước đã hết, run kết thúc bằng một event
`error` như dưới đây và một `result` có subtype `error`:

```text
{'name': 'ProviderError', 'kind': 'context_length', 'message': "prompt exceeds the model's context window: prompt is too long: 201838 tokens > 200000 maximum", 'retryable': False, 'status': 400}
```

Python Linch luôn có một chiến lược compaction để quay về. linch-rust chỉ compact khi spec
yêu cầu, nên hãy đặt `compaction` trên mọi agent mà các run của nó có thể vượt quá cửa
sổ.

### Lần từ chối của từng provider trông thế nào

| Provider | Được nhận ra là `context_length` |
|---|---|
| `anthropic` | HTTP 400 có thông báo nói `prompt is too long` hoặc `exceed context limit`. Một 413 (body request quá lớn tính theo byte) vẫn là một lỗi HTTP |
| `openai-chat` | HTTP 400 với code `context_length_exceeded`, hoặc một thông báo nói `maximum context length` |
| `openai-responses` | HTTP 400, hoặc một event `response.failed`, với code `context_length_exceeded`, hoặc một thông báo nói `exceeds the context window` hoặc `maximum context length` |
| `gemini` | HTTP 400 có thông báo nói `exceeds the maximum number of tokens allowed` |
| `deepseek` | HTTP 400 có thông báo nói `maximum context length` |
| `vllm` | HTTP 400 có thông báo nói `maximum context length` |
| `sglang` | HTTP 400 có thông báo nói `maximum context length` hoặc `exceeds the maximum allowed length` |
| `llamacpp` | HTTP 400 với type `exceed_context_size_error`, hoặc một thông báo nói `exceeds the available context size` hoặc `larger than the max context size` |

Việc so khớp thông báo bỏ qua hoa thường. Các thông báo của Anthropic, Gemini và DeepSeek
không được vendor của chúng ghi tài liệu; đó là những thông báo mà các API đó gửi hiện
nay. Một provider đổi lời lẽ sẽ khiến lần từ chối trở lại thành một HTTP 400 thông thường,
và kết thúc run như trước.

Để kiểm thử cách xử lý của bạn ngoại tuyến, một lượt [scripted](./providers.md#the-scripted-provider)
có thể thất bại với `{"error": {"type": "context_length", "status": 400,
"message": "..."}}`.

---

<a id="cut-off-answers"></a>

## Câu trả lời bị cắt

Một model chạm giới hạn đầu ra sẽ dừng giữa chừng câu trả lời, với stop reason
`max_tokens`. `spec.truncation_recovery` (có kiểu `TruncationRecovery`,
`TruncationRecovery` của Python Linch) gửi một câu trả lời như vậy trở lại:

```python
spec = {
    "model": "claude-sonnet-4-6",
    "max_output_tokens": 2048,
    "truncation_recovery": {"max_attempts": 2},
}
```

| Trường | Mặc định | Điều khiển gì |
|---|---|---|
| `max_attempts` | `1` | Số câu trả lời bị cắt nhiều nhất một run tiếp tục; tối thiểu 1. Quá mức đó, một câu trả lời bị cắt là câu cuối |
| `feedback` | "Your previous response was cut off because it reached the output token limit. Continue exactly where you left off — do not repeat earlier text." | Những gì model được bảo, trong một `<system-reminder>`; không để trống |

Phần bị cắt được ghi log như câu trả lời của model, phản hồi được ghi log như message user
kế tiếp (một event `user` với subtype `notification`), và model thực hiện thêm một lượt.
Câu trả lời cuối cùng của run là mọi phần theo thứ tự, và toàn bộ văn bản đó là thứ mà
[output schema](./structured-output.md), các [verifier](./security.md#verifiers) và các
callback `stop` nhìn thấy. Chỉ một câu trả lời có văn bản và không có lời gọi tool mới
được tiếp tục; một lượt gọi tool bắt đầu một câu trả lời mới. Mỗi lần tiếp tục là một
lượt, nên `max_turns` giới hạn nó.

---

<a id="file-memory"></a>

## Bộ nhớ dạng tệp

`filesystem: {"memory": true}` cho một agent thường bộ nhớ mà [deep agent](./deep-agent.md)
có: model ghi chú trong [hệ thống tệp ảo](./tools.md#hệ-thống-tệp-ảo) dưới `/memories/`,
mỗi sự thật một tệp, với `/memories/MEMORY.md` là chỉ mục một dòng cho mỗi tệp. Không cần
preset; đó là `FileMemoryContextBuilder` của Python Linch.

```python
config = {
    "namespace": "support",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "stores": {"type": "postgres", "dsn": "postgres://..."},
    "filesystem": {"memory": True},
}
```

Trước mỗi lời gọi model, server thêm hai khối văn bản vào request đó: một chỉ dẫn (ký ức
nằm dưới thư mục nêu bên dưới, `MEMORY.md` là chỉ mục, dùng các tool tệp để giữ chúng và
bỏ các sự thật cũ) và, khi tệp tồn tại và không rỗng, 200 dòng đầu của nó, tối đa 16 KiB,
với một ghi chú cho mỗi lần cắt. Chỉ mục được hiển thị như dữ liệu, không phải chỉ thị: nó
nằm giữa các thẻ `<memory-index>` dưới một dòng nói rõ điều đó, và một thẻ đóng nằm trong
nội dung bị phá (`<\/memory-index>`) để nó không thể kết thúc khung sớm. Các khối nằm ở
đuôi request, như [bộ nhớ ngoài](#external-memory), và không bao giờ được
ghi log. Không gì ghi ký ức thay cho model: nó dùng `write_file` và `edit_file`, nên các
quyền thông thường áp dụng.

- **Đọc cho mỗi request, không phải một lần cho mỗi run.** Python Linch đọc chỉ mục một
  lần cho mỗi run và giữ lại, để prompt không đổi giữa run. Ở đây các khối không bao giờ
  thuộc phần prefix được cache, vốn giữ nguyên trong cả hai cách, và đuôi của lượt trước
  không được ghi log nên không thể cache qua các request. Đọc lại tốn một lần đọc tệp và
  cho model thấy chỉ mục nó vừa sửa ở lượt kế tiếp. Một run được resume ở tiến trình khác
  đọc lại tệp trong cả hai thiết kế.
- **Tệp nằm ở đâu.** Với kho Postgres, mọi session của namespace dùng chung các tệp dưới
  `/memories/`, nên session 2 thấy những gì session 1 ghi, và tiến trình mới cũng vậy. Với kho memory,
  mỗi session có tệp riêng, giữ trong handle của agent: không gì được chia sẻ giữa các
  session, và `close_session` xóa chúng, nên tệp không sống lâu hơn session của nó. Dùng
  kho Postgres cho ký ức bền.
- **Tệp của ai: `memory_scope`.** Trên Postgres, các tệp được chia sẻ bởi mọi session *và
  mọi principal* của namespace, và model tin các ghi chú của chính nó, nên một run có ít
  quyền hơn có thể ghi những ghi chú mà một run nhiều quyền hơn rồi đọc như của mình. Mặc
  định (`"principal"`), principal của mỗi run có một thư mục riêng,
  `/memories/<key>/MEMORY.md`, trong đó `<key>` là 32 chữ số hex của SHA-256 trên
  tenant, user và các role của nó (an toàn cho đường dẫn và không nêu tên ai). Chỉ dẫn nêu
  thư mục đó, nên model giữ ghi chú ở đấy. Một run không có principal (kết nối cục bộ, hoặc
  không truyền) dùng `/memories/MEMORY.md`. `filesystem: {"memory": True, "memory_scope":
  "namespace"}` là hành vi cũ: một tệp cho mọi run của namespace, phù hợp khi mọi run đáng
  tin như nhau. Scope thuộc hợp đồng của run. Đây là tách biệt theo đường dẫn, không phải
  theo quyền: các tool hệ thống tệp không bị giới hạn scope, nên `ls /memories` liệt kê mọi
  thư mục và một run có `write_file` có thể ghi vào thư mục của principal khác. Dùng các
  luật permission để giữ một role tránh xa `/memories/` nếu điều đó quan trọng.
- **Hợp đồng.** Tắt thì không được gửi và hợp đồng của run không đổi; bật thì hook thuộc
  hợp đồng, nên một run không thể resume qua việc bật hay tắt nó, hay đổi scope của nó.
  `deep_agent` luôn có, bất kể `memory`, với cùng `memory_scope`. Văn bản của hook đã đổi
  cùng phiên bản này (có khung, có giới hạn, theo principal), nên một run của deep agent
  hay agent `memory` tạo trước đó không thể resume.
- `memory` là một trường của `filesystem`, nên nó cần hệ thống tệp bật; không có cách xin
  nó khi hệ thống tệp tắt.

---

<a id="external-memory"></a>

## Bộ nhớ ngoài

Bộ nhớ dài hạn là một store do bạn sở hữu. Agent đọc nó qua một
[callback](./agent.md#callback) `context_build`: trước mỗi lời gọi model, callback tìm
trong store và trả về các block văn bản. Các block chỉ đi cùng request đó, ở phần đuôi
của nó, như một phần của message user cuối cùng. Chúng không bao giờ được ghi log, nên:

- session không lớn lên cùng văn bản được truy xuất, và một lần compaction không bao giờ
  phải gộp nó;
- prefix đã cache của mọi request sau đó vẫn như cũ;
- request kế tiếp tìm lại, với cuộc hội thoại ở trạng thái lúc đó.

`linch_rust.memory.retrieval` dựng callback đó từ bất kỳ object nào có một phương thức
`retrieve`:

```python
from linch_rust import Server
from linch_rust.memory import Chunk, retrieval


class Wiki:
    async def retrieve(self, query: str, *, k: int) -> list[Chunk]:
        hits = await search_my_index(query, limit=k)  # your vector index, Notion, SQL...
        return [Chunk(id=h.id, source=h.url, text=h.body, score=h.score) for h in hits]


async with Server() as server:
    agent = await server.open_agent(config, callbacks=[retrieval(Wiki(), k=4)])
```

| Đối số của `retrieval` | Mặc định | Điều khiển gì |
|---|---|---|
| `retriever` | bắt buộc | Store; `retrieve(query, *, k)` trả về các `Chunk`, tốt nhất trước, hoặc một coroutine của chúng |
| `k` | `4` | Số chunk nhiều nhất mỗi request |
| `policy_id`, `policy_version` | `"memory"`, `1` | Vị trí của callback trong run contract; tăng version khi hành vi của store thay đổi |
| `timeout` | không có | Số giây server chờ; khi thất bại hoặc timeout, request đi mà không có bộ nhớ (`context_build` fail open) |

Truy vấn là message mới nhất mà user đã viết, bỏ qua kết quả tool và phản hồi
`<system-reminder>`, nên câu hỏi đã bắt đầu các lời gọi tool của một run vẫn là truy vấn
xuyên suốt chúng. Mỗi chunk đến model dưới dạng:

```text
[doc:{id}]
Source: {source}
{text}
```

Các id và source nằm trên `ContextBuildEvent` của lời gọi dưới dạng `metadata["memory"]`,
cho các observer và eval; model không bao giờ thấy metadata.

Mọi request của một callback `context_build` đều có `session_id` và `run_id`. Để giữ bộ
nhớ của một user tách khỏi user khác, hãy viết callback riêng chọn store từ chúng;
`latest_question` và `render` trong `linch_rust.memory` là các phần mà `retrieval` được
tạo nên.

### Ghi các memory

Model ghi bộ nhớ bằng cách gọi một host tool mà bạn khai báo, nên bạn quyết định một
memory là gì và nó đi đâu:

```python
from linch_rust import tool


@tool(scope="write")
def remember(title: str, text: str) -> str:
    """Save a fact for later conversations, as a short titled note."""
    return store.remember(title, text)
```

Một tool `write` hỏi phê duyệt ở permission mode `default`; hãy chạy agent ở
`accept_edits`, hoặc cho phép tool bằng một permission rule.

`python/examples/external_memory.py`, trong repository (không có trong wheel), ghép cả hai nửa trên một thư mục ghi chú Markdown,
như một vault Obsidian: một `NoteStore` tìm theo từ khóa, `retrieval` trên nó, và một tool
`remember`. Nó chạy ngoại tuyến với provider scripted, và `tests/test_memory.py` chạy nó,
kiểm tra rằng một ghi chú lưu ở một run được nhớ lại ở run sau và không có văn bản nào được
nhớ lại nằm trong session.

### Vì sao là callback chứ không phải một kiểu connector

Python Linch có `RagConnector`, một protocol mà context builder gọi, và `RetrievedChunk`.
Ở đây, store chạy trong process của bạn còn run chạy trong server, nên một connector sẽ
chỉ là thêm một kiểu callback với cùng một vòng đi về như `context_build`. `Retriever` và
`Chunk` có hình dạng của connector, và `retrieval` là adapter, nằm trong client, nơi thư
viện của store sống. Python Linch đặt văn bản được truy xuất trong các block system; ở
đây nó đi ở phần đuôi của request, nên việc truy xuất không bao giờ làm dịch prefix đã
cache.

---

## So với Python Linch

| Python Linch | linch-rust |
|---|---|
| `CompactionLadder` với nấc micro và các bản tóm tắt bắt buộc | `spec.compaction` với `micro` và `max_forced` |
| `DetailedCompaction` | `spec.compaction.prompt = DETAILED_COMPACTION_PROMPT` |
| Một chiến lược compaction tùy chỉnh | Không hỗ trợ: `prompt` và `max_output_tokens` là các núm điều chỉnh |
| Khôi phục từ lỗi prompt-too-long, luôn bật | Chỉ với `spec.compaction`; không có nó, lần từ chối kết thúc run |
| `reset_read_tracker` sau một lần compaction | Có sẵn cho agent workspace |
| `TruncationRecovery` | `spec.truncation_recovery` |
| `RagConnector`, `ContextBuilder`, `FileMemoryContextBuilder` | Một callback `context_build`; `linch_rust.memory.retrieval` |

## Các trang liên quan

- [Config của agent](./agent.md#compaction): mọi trường `spec`.
- [Event](./events.md#compactionevent-compaction): event `compaction`.
- [Provider](./providers.md#lỗi-và-thử-lại): những gì được thử lại.
- [Kiến trúc: compaction](../architecture/compaction.md): các barrier và việc khôi phục
  từng bước.
