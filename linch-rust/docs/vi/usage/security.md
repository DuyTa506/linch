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
    "spec": {"model": "deepseek-flash"},
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
chối sẽ làm. Permission, redaction và [sandbox](./sandbox.md) mới là các hàng rào.

## Các trang liên quan

- [Tool](./tools.md): quy tắc quyền, phê duyệt.
- [Sandbox](./sandbox.md): nơi `Bash` chạy và mỗi backend giới hạn những gì; một
  guardrail thứ tư, nằm dưới các permission mode.
- [Config của agent](./agent.md): `spec`, intercept callback, hợp đồng của run.
- [Structured output](./structured-output.md): output schema và các retry của nó.
- [Event](./events.md): `verification`, `error`, `partial_assistant`, `tool_progress`.
- [Server](./server.md): tenant và các allowlist của chúng.
