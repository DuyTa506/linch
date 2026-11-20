# Bảo mật

[Bản gốc tiếng Anh](../../usage/security.md)

Ba guardrail quyết định một agent được làm gì và những gì được rời khỏi nó: permission
mode quyết định lời gọi tool nào chạy, redaction che văn bản nhạy cảm trước khi bất cứ
thứ gì lưu hay stream nó, và verifier kiểm tra câu trả lời cuối cùng trước khi run hoàn
tất. Cả ba đều là config và không cần code nào trong server. Redaction và verifier là
một phần của [hợp đồng của run](./agent.md#the-run-contract); một agent không có chúng
gửi cùng các request và ghi cùng các checkpoint và event như trước khi chúng tồn tại.

---

<a id="permission-modes"></a>

## Permission mode

`permissions.mode` đặt việc một lời gọi tool làm gì khi không có
[quy tắc quyền](./tools.md#permission-rules) nào khớp với nó:

| Mode | Tool đọc | Tool ghi | Tool exec | Python Linch |
|---|---|---|---|---|
| `default` | chạy | hỏi | hỏi | `default` |
| `accept_edits` | chạy | chạy, trừ khi đường dẫn đích nằm ngoài `project_root`, khi đó hỏi | hỏi | `acceptEdits` |
| `skip_dangerous` | chạy | chạy | chạy | `skip-dangerous` |

Một lời gọi hỏi sẽ treo run chờ `agent.approve`, hoặc đi tới một callback
`permission_request` ([phê duyệt](./tools.md#phê-duyệt)). `auto_allow` liệt kê các tool
chạy ở mọi mode trừ khi một quy tắc nói khác; `tasks` và `deep_agent` đặt các tool task
vào đó.

```python
from linch_rust import AgentConfig, Permissions

permissions: Permissions = {
    "mode": "accept_edits",
    "project_root": "/srv/acme",
    "auto_allow": ["lookup"],
}
config: AgentConfig = {
    "namespace": "acme",
    "spec": {"model": "deepseek-flash", "max_turns": 20},
    "provider": {"id": "deepseek"},
    "workspace": "/srv/acme",
    "permissions": permissions,
}
```

Trên một server từ xa, một tenant chỉ được dùng các mode trong allowlist
`permission_modes` của nó, `default` và `accept_edits` trừ khi server nói khác. Các mục
`auto_allow` và các quy tắc cho phép cần `skip_dangerous` trong danh sách đó, vì mỗi cái
cho một lời gọi chạy mà không hỏi ([Server](./server.md)).

Python Linch đưa permission mode và các quy tắc của nó vào hợp đồng của run; ở đây
chúng không nằm trong đó, nên `resume` và `approve` dùng chính sách mà agent có lúc đó.

<a id="redaction"></a>

## Redaction

`spec.redaction` giữ các regex rule thay thế văn bản nhạy cảm. Linch cung cấp cơ chế mà
không có pattern nào, như `RedactionHook` của Python Linch: bạn quyết định cái gì là
nhạy cảm và nó được thay bằng gì.

```python
from linch_rust import Redaction

redaction: Redaction = {
    "rules": [
        {"pattern": r"sk-[A-Za-z0-9]{20,}", "replacement": "[API_KEY]"},
        {"pattern": r"(?i)[\w.+-]+@([\w-]+\.)+[a-z]{2,}", "replacement": "[EMAIL]"},
    ],
}
config["spec"] = {"model": "deepseek-flash", "redaction": redaction}
```

| Trường | Mặc định | Nó điều khiển gì |
|---|---|---|
| `rules` | bắt buộc | Các rule áp dụng theo thứ tự, mỗi rule trên đầu ra của rule trước; ít nhất một |
| `rules[].pattern` | bắt buộc | Một biểu thức chính quy theo [cú pháp của crate `regex`](https://docs.rs/regex/latest/regex/#syntax): khớp trong thời gian tuyến tính, không có look-around hay back-reference; `(?i)` cho mọi kiểu chữ |
| `rules[].replacement` | `[REDACTED]` | Thứ thay một match; `$1` hoặc `${name}` gọi tên một nhóm, `$$` là một dấu `$` nguyên văn |
| `response` | `True` | Che những gì model viết |
| `tool_results` | `True` | Che những gì tool trả về |
| `prompt` | `False` | Che prompt của một run mới, input của chính host |

Mỗi bề mặt bao phủ những gì:

| Bề mặt | Được che | Không được gửi khi nó bật |
|---|---|---|
| `response` | Văn bản và reasoning không có chữ ký của mọi response assistant, trước khi các callback `after_provider_call` thấy nó và trước khi nó được ghi log; một bản rewrite của callback thì che lại; câu trả lời cuối cùng sau một rewrite `stop`; các bản tóm tắt compaction | Các delta `partial_assistant`: một match có thể nằm vắt qua hai delta |
| `tool_results` | Mọi chuỗi của một kết quả tool (value và metadata của output, message và details của một lỗi) trước khi các callback `post_tool_use` thấy nó, và lần nữa sau khi một callback thay thế nó, nên một kết quả được offload được lưu ở dạng đã che; các câu trả lời `AskUser`; câu trả lời của một background worker; các message peer từ mailbox | Các event `tool_progress` |
| `prompt` | Văn bản prompt của một run mới, trước các callback `user_prompt_submit`, và lần nữa sau một rewrite | không có gì |

Trên mỗi bề mặt đang bật, thứ được che là thứ session ghi log và thứ model đọc lại ở
lượt kế tiếp: văn bản đó không bao giờ đến một store, một event, một callback hay
request kế tiếp ở dạng thô. Một lần crash ở bất kỳ lần ghi nào vẫn giữ điều đó, vì
không có gì chứa văn bản response được ghi trước khi redaction chạy. Các phần liệt kê
bên dưới không được che, và đến các event `assistant` và `tool_call_start` cùng log
đúng như được viết.

Không bao giờ bị thay đổi:

- Input của một lời gọi tool. Tool chạy đúng thứ được ghi log; để ngăn model đưa một bí
  mật cho tool, hãy từ chối lời gọi bằng một rule hoặc một callback `pre_tool_use`.
- Reasoning có chữ ký của provider (thinking của Anthropic). Chữ ký của nó sẽ không còn
  xác minh được khi response được gửi trở lại. Hãy tắt thinking khi văn bản của nó cần
  được che.
- Reasoning được mã hóa, và các khóa object trong một kết quả tool.

`agent.open` từ chối một rule không biên dịch được, một rule khớp văn bản rỗng (nó sẽ
chèn replacement của mình giữa mọi hai ký tự), không có rule nào, và mọi bề mặt đều tắt
(`OpenError`, code -32011). Một tenant từ xa được đưa tối đa 32 rule, mỗi rule tối đa
1024 byte (`NotAllowed`, `kind` là `redaction`); mỗi pattern đã biên dịch bị giới hạn ở
1 MiB. Subagent dùng redaction của agent.

<a id="verifiers"></a>

## Verifier

`spec.verification` giữ các kiểm tra mà câu trả lời cuối cùng phải qua, tức
`FinalAnswerVerifierHook` của Python Linch. Một câu trả lời không đạt được gửi lại cho
model cùng phản hồi trong khi còn retry.

```python
from linch_rust import Verification

verification: Verification = {
    "max_retries": 2,
    "verifiers": [
        {
            "name": "cites",
            "check": {"type": "matches", "pattern": r"\[\d+\]"},
            "feedback": "Cite a source for each claim as [n].",
        },
        {
            "name": "no_keys",
            "check": {"type": "forbids", "pattern": r"sk-[A-Za-z0-9]{20,}"},
            "on_fail": "stop",
        },
        {
            "name": "topic",
            "check": {"type": "keywords", "keywords": ["refund", "policy"]},
        },
    ],
}
config["spec"] = {"model": "deepseek-flash", "verification": verification}
```

| Trường | Mặc định | Nó điều khiển gì |
|---|---|---|
| `max_retries` | 2 | Bao nhiêu câu trả lời không đạt được gửi lại cho model trong mỗi run, tính chung các verifier và callback `stop` |
| `verifiers` | không có | Các verifier tích hợp sẵn, chạy theo thứ tự; lần không đạt đầu tiên quyết định |
| `verifiers[].name` | bắt buộc | Đặt tên cho nó trong các event `verification`; duy nhất, không phải `output_schema` |
| `verifiers[].check` | bắt buộc | `{"type": "matches", "pattern"}`: pattern phải khớp; `{"type": "forbids", "pattern"}`: nó không được khớp; `{"type": "keywords", "keywords", "any"?, "case_sensitive"?}`: mỗi keyword (hoặc một keyword, với `any`) phải xuất hiện, ở mọi kiểu chữ trừ khi có `case_sensitive` |
| `verifiers[].feedback` | một câu gọi tên kiểm tra | Điều model được nói khi retry |
| `verifiers[].on_fail` | `retry` | `retry`: gửi phản hồi trong khi còn retry, rồi chấp nhận câu trả lời; `stop`: làm run thất bại ngay |

Một run diễn ra thế nào:

1. Một câu trả lời chỉ có văn bản được kiểm tra trước theo
   [output schema](./structured-output.md), nếu có, rồi theo các verifier, theo thứ
   tự, rồi các callback `stop` chạy.
2. Một lần không đạt `retry` stage một event `verification` (`action` là `retry`,
   `attempt` là số retry đã dùng, 1 ở lần đầu), ghi log phản hồi làm message user kế
   tiếp trong một `<system-reminder>`, và chạy thêm một lượt.
3. Quá `max_retries`, câu trả lời được chấp nhận nguyên trạng, với một event
   `verification` có `action` là `exhausted`, như trong Python Linch.
4. Một lần không đạt `stop` kết thúc run với trạng thái failed: một event
   `verification` có `action` là `stop`, một event `error` tên `VerificationError`, và
   một result với subtype `error` giữ lại văn bản của câu trả lời.

Các verifier kiểm tra văn bản mà session đã ghi log, nên khi redaction bật chúng thấy
văn bản đã che, và một run được resume quyết định giống như vậy.

<a id="a-verifier-in-your-process"></a>

### Một verifier trong process của bạn

Một callback `stop` trả lời `retry` là dạng client của một verifier. Không có
`spec.verification`, một lần retry của `stop` được chấp nhận một lần cho mỗi lần gọi
run. Có nó (kể cả khi không có verifier tích hợp nào, `{"max_retries": 3}`), mỗi lần
retry của `stop` trừ vào `max_retries`, là một event `verification` bền vững được đặt
tên theo `policy_id` của callback, và quá ngân sách thì được tính như một allow cùng một
event `exhausted`.

```python
from typing import Any

from linch_rust import callback


@callback("stop", "polite", 1)
def polite(request: dict[str, Any]) -> dict[str, Any]:
    if "please" in request["text"]:
        return {"action": "allow"}
    return {"action": "retry", "feedback": "Ask politely."}


config["spec"] = {"model": "deepseek-flash", "verification": {"max_retries": 3}}
agent = await server.open_agent(config, callbacks=[polite])
```

### Ngân sách

Schema và các verifier tiêu các ngân sách riêng:

| Ngân sách | Bị tiêu bởi | Quá nó |
|---|---|---|
| `structured_output_retries` (mặc định 0) | các câu trả lời vi phạm output schema | run thất bại với `StructuredOutputError` |
| `verification.max_retries` (mặc định 2) | các verifier tích hợp sẵn và retry của callback `stop` | câu trả lời được chấp nhận, `exhausted` |

Mỗi retry là một lời gọi provider, nên `max_turns` và ngân sách giới hạn cả hai. Các
retry được đếm từ các event bền vững của run, nên một lần crash rồi resume không bao giờ
cấp thêm. Các run của một subagent không dùng verifier của agent.

`agent.open` từ chối một name trống, lặp hoặc được dành riêng, một pattern không biên
dịch được hoặc khớp văn bản rỗng, và một keyword rỗng hoặc trống (`OpenError`, code
-32011). Một tenant từ xa được đưa tối đa 32 verifier, mỗi verifier có một pattern tối
đa 1024 byte hoặc tối đa 32 keyword (`NotAllowed`, `kind` là `verification`).

## Thứ tự tool và completion gate

[`spec.policy`](./agent.md#policy) từ chối một lời gọi cho tới khi các tool nó cần đã
thành công, và giữ một câu trả lời cuối cùng lại cho tới khi run đã làm xong điều nó
phải làm. Một gate làm run thất bại khi model không tuân theo (một verifier chấp nhận câu
trả lời khi các retry của nó đã hết). Nó là công cụ về độ tin cậy: một lời gọi bị từ
chối không hỏi phê duyệt và không chạy gì, nhưng một prerequisite **không phải ranh giới
bảo mật**, vì một tool có thể khởi động công việc khác làm được điều mà một tool bị từ
chối sẽ làm, và một lời gọi được tính chỉ theo tên tool và kết quả đã render của nó (một
host tool cùng tên, một hook trả lời lời gọi, hay một cache hit cũng được tính).
Permission, redaction và [sandbox](./sandbox.md) mới là các hàng rào.

## Ranh giới tin cậy

Kết nối là ranh giới tin cậy, và tenant là thứ nó mang theo. Host tự xác thực người
dùng của mình và khẳng định principal mà một run hành động thay mặt; linch không xác
thực người dùng. Server chỉ kiểm tra một điều về principal: `tenant` của nó phải là
tenant của kết nối (`admit_principal`, `linch-rpc/src/access/admit.rs:216-224`). Nó
không kiểm tra `user` hay `roles`.

Hệ quả:

- Mọi kết nối của một tenant đều có thể `resume`, `approve`, `answer`, `cancel` và đọc
  `events` của mọi run của tenant đó, khi biết run id. `cancel` và `events` hoàn toàn
  không nhận principal (`linch-rpc/src/server/run.rs:91-126`,
  `server/agents.rs:136-143`).
- Một lời tiếp tục run hành động thay mặt principal mà nó nêu tên, thay cho principal
  của chính run ([Run](./runs.md#hành-động-thay-cho-một-principal)). Duyệt một run đang treo bằng
  principal khác (một admin duyệt run của người dùng) là một luồng có chủ đích.
- Workflow khác: journal của nó từ chối một claim nêu principal khác với principal nó
  được khởi động cho (`PrincipalMismatch`, `linch-ext/src/workflow/conformance.rs:16`).
  Run không có kiểm tra như vậy.

Vì vậy host phải:

- Ánh xạ danh tính người dùng của chính mình sang các principal nó truyền, và không bao
  giờ để người dùng cuối chọn `principal` hay `run_id` của một lời gọi tới linch.
- Cấp quyền trước khi gọi linch: kiểm tra người dùng này được chạm vào run này, rồi mới
  gọi. Giữ run id không đoán được (một id do caller chọn như `order-42-reply` thì đoán
  được); server không ngăn một kết nối nêu tên run khác của tenant mình.
- Cho mỗi miền tin cậy một tenant riêng. Hai bên không được thấy run của nhau thì thuộc
  hai tenant, không phải hai principal.

## Nội dung không đáng tin

Kết quả tool, kết quả MCP và câu trả lời của subagent quay lại model dưới dạng văn bản
thường. Chỉ memory recall được rào: nó nằm trong `<memory-data untrusted="true">` và văn
bản đã lưu được escape (`linch-tools/src/memory_recall.rs:35`). Vì vậy văn bản mà một
trang web, một file, một email hay hệ thống khác đưa vào kết quả tool có thể bảo model
phải làm gì (prompt injection), và model có thể làm theo.

**Những gì phê duyệt và permission vẫn chặn**

- Một lời gọi tool bị tiêm được quyết định như mọi lời gọi khác: bởi permission mode và
  rule, theo từng lời gọi, không theo điều nội dung nói ([Permission mode](#permission-mode)).
  Một lời gọi cần hỏi sẽ treo run.
- Nội dung không đổi được principal, permission hay hook của run: chúng đến từ option
  của run, không từ transcript.
- Một tool MCP được coi là scope `exec` trừ khi server được cấu hình `trust_annotations`,
  vì `readOnlyHint` của server là không đáng tin (`linch-mcp/src/config.rs:176`, mặc
  định `false` ở `:197`). Ở mode `default` nó hỏi trước khi chạy
  ([Tool](./tools.md#mcp-tool)).

**Những gì chúng không chặn**

- Lái model tới bất kỳ tool nào mà policy đã cho chạy không cần hỏi: tool đọc,
  `auto_allow`, một allow rule, `accept_edits` cho chỉnh sửa trong project, và mọi lời
  gọi ở `skip_dangerous`. Gồm cả việc rò rỉ qua một tool được phép (một tool đọc lấy
  một URL dựng từ dữ liệu riêng tư, một tool ghi lưu nó ở chỗ người khác đọc được).
- Một hook `post_tool_use` thấy kết quả bị tiêm nhưng mặc định fail open: hook crash
  hoặc hết giờ thì kết quả đi qua ([Callback](./agent.md#callback)).
- Câu trả lời của subagent tới parent không có nhãn, và child chạy với permission của
  parent.
- Kernel bọc phản hồi của chính nó trong thẻ `<system-reminder>`
  (`linch-core/src/skills.rs:162`). Output của tool không bị escape, nên một kết quả có
  thể chứa văn bản giả dạng wrapper như vậy. Hiện kernel không vô hiệu hóa nó. Host cần
  thì có thể lọc hoặc viết lại kết quả trong một hook `post_tool_use` với
  `fail_mode: "closed"`.

**Hướng dẫn cho host.** Đây là hướng dẫn, không phải đảm bảo.

- Cho mỗi agent ít tool nhất đủ làm việc. Đừng cùng lúc cho một agent đầu vào không
  đáng tin, dữ liệu nhạy cảm và một đường giao tiếp ra ngoài mà không có phê duyệt ở
  bước cuối; nếu agent cần cả ba, hãy đòi phê duyệt cho các lời gọi ghi, thực thi hoặc
  chạm mạng.
- Đòi phê duyệt (mode `default` hoặc một rule `ask`) cho tool ghi, exec và mạng chạy trên
  dữ liệu từ nguồn không đáng tin. Ở đó nên chọn `default` hơn `skip_dangerous`.
- Đặt `fail_mode: "closed"` trên mọi hook bảo vệ dữ liệu.
- Giữ bí mật ngoài input của tool: input của một lời gọi tool không bao giờ bị
  [redaction](#redaction) che và bị [telemetry](#telemetry-ghi-nội-dung) ghi lại. Đưa
  credential cho tool qua config của chính tool, không qua model.

## Hành vi khi quá tải

Chuyện gì xảy ra khi một tài nguyên đầy, theo code tại thời điểm viết:

| Tài nguyên | Mặc định | Khi đầy | Chờ bao lâu | Lỗi |
|---|---|---|---|---|
| `max_runs` của tenant | 16 (`linch-rpc/src/access/allowlists.rs:18`) | `run`, `resume` hoặc `approve` bị từ chối; không gì được khởi động | Không chờ: `try_acquire_owned` trả lời ngay (`access.rs:115-117`, `:291-299`) | `LimitReached` -32005, `kind: runs` |
| Cổng đồng thời của provider (`max_provider_concurrency`) | Không giới hạn | Lời gọi vào một hàng đợi vào trước ra trước | Không giới hạn thời gian; chỉ cancel của run mới kết thúc việc chờ (`linch-core/src/provider/gate.rs:87-92`). Hàng đợi không giới hạn độ dài và không loại bỏ gì | Không có |
| HTTP provider | Connect 30 s, read 300 s (`linch-providers/src/clients.rs:132-133`) | Lời gọi thất bại | Đúng timeout; `request.timeout_ms` thay timeout read ([Provider](./providers.md)) | Một lỗi provider ([Lỗi và retry](./providers.md#lỗi-và-thử-lại)) |
| Pool Postgres | 30 s để lấy một kết nối (`linch_postgres::ACQUIRE_TIMEOUT`) | Lời gọi store thất bại với `pool timed out after 30s: raise max_pool_connections or lower concurrent runs` | 30 s | `RunError` -32020 (-32024 khi nó kết thúc một lần ghi journal) |
| Thời gian chạy của run | Không có hạn chót | Run vẫn tiếp tục | Đến khi `max_turns`, ngân sách hoặc một cancel kết thúc nó | Không có |

Một run không có hạn chót theo đồng hồ. Các giới hạn là `max_turns` và ngân sách, và
server từ chối agent của tenant không có cái nào (`run_bound`,
[Server](./server.md#những-gì-admission-từ-chối-và-lý-do)). Một run chờ ở cổng chờ mà không có
đồng hồ nào, nên một provider bão hòa có thể làm các run trong hàng chờ lâu bằng thời
gian bão hòa.

Host nên:

- Đặt `max_provider_concurrency` (trên agent, hoặc làm giới hạn của tenant) bằng mức key
  của provider cho phép, để hàng đợi nằm trước cổng thay vì trong rate limiter của
  provider.
- Đặt `max_turns` và một ngân sách cho mọi agent.
- Áp hạn chót từ host: cancel run (`agent.cancel`) khi đến hạn; cancel cũng kết thúc việc
  chờ ở cổng.
- Tự xếp hàng công việc vượt `max_runs` ở phía host, vì server từ chối nó.

## Telemetry ghi nội dung

Mặc định nội dung không có trong trace. `linch-otel` chỉ ghi prompt, câu trả lời, input
và kết quả của tool khi capture bật: đặt
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` (công tắc được đọc bởi
`capture_content_from_env`, `linch-otel/src/lib.rs:35-38`; tên nằm ở `keys.rs:90-92`),
hoặc `RunObserver::capture_content` trong một host dạng thư viện.

Khi bật, các attribute này mang nội dung: `gen_ai.input.messages`,
`gen_ai.output.messages`, `gen_ai.tool.call.arguments` và `gen_ai.tool.call.result`
([Observability](./observability.md#ghi-lại-nội-dung)).

- `linch-otel` không áp redaction và không giới hạn kích thước cho chúng.
- [`spec.redaction`](#redaction) che response và kết quả tool trước khi ghi log, nên các
  attribute đó mang văn bản đã che khi host đã bật redaction. Input của một lời gọi tool
  không bao giờ bị che, nên `gen_ai.tool.call.arguments` có thể mang một bí mật mà model
  được đưa hoặc tự bịa vào lời gọi, vào trace của bạn và nơi lưu chúng.

Giữ capture tắt ở production. Khi cần, hãy che ở OpenTelemetry collector (một processor
xóa hoặc che các attribute này) và giới hạn kích thước ở đó, và coi backend trace là nơi
giữ dữ liệu người dùng.

## Các trang liên quan

- [Tool](./tools.md): quy tắc quyền, phê duyệt.
- [Sandbox](./sandbox.md): nơi `Bash` chạy và mỗi backend giới hạn những gì; một
  guardrail thứ tư, nằm dưới các permission mode.
- [Config của agent](./agent.md): `spec`, intercept callback, hợp đồng của run.
- [Structured output](./structured-output.md): output schema và các retry của nó.
- [Event](./events.md): `verification`, `error`, `partial_assistant`, `tool_progress`.
- [Server](./server.md): tenant và các allowlist của chúng.
- [Run](./runs.md#hành-động-thay-cho-một-principal): principal mà một run hành động thay mặt.
- [Observability](./observability.md#ghi-lại-nội-dung): những gì telemetry ghi lại.
