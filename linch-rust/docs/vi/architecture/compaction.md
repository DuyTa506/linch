# Thu gọn ngữ cảnh

[Bản gốc tiếng Anh](../../architecture/compaction.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Compaction rút ngắn provider view của một run dài. Một bản tóm tắt gộp phần cũ của view
thành một message; micro-compaction xóa các kết quả tool cũ mà không có lời gọi model
nào. Một run compact ở đầu một lượt, trước lời gọi provider chính, khi view đến hạn, và
ngoài lượt, ở dạng bắt buộc, khi provider từ chối prompt của lời gọi chính vì dài hơn
cửa sổ ngữ cảnh của model. Kết quả luôn là một projection có ghi log trên log chỉ nối
thêm của session: model nhìn thấy một view ngắn hơn, còn lịch sử đầy đủ giữ nguyên mọi
message. Các phần thuần nằm trong `crates/linch-core/src/compaction.rs`; phần của run,
cùng các barrier của nó, là `crates/linch-core/src/runner/compact.rs`. Dùng trang này khi
bạn thay đổi thời điểm một run compact, nội dung của bản tóm tắt, hoặc cách một run khôi
phục một lần compaction.

Khôi phục câu trả lời bị cắt, một lần retry khác do kích thước thúc đẩy, được mô tả ở
cuối trang ([câu trả lời bị cắt](#cut-off-answers)). Cách dùng tất cả những thứ này từ
Python nằm ở [Memory](../usage/memory.md).

Linch bản Python có cùng cấu trúc ([compaction](../../../../docs/architecture/compaction.md)).
Điểm khác trong Rust:

- `CompactionSpec` là dữ liệu. Ladder là cố định: micro-compaction khi `micro` được đặt,
  rồi đến một bản tóm tắt. Không có chiến lược tùy chỉnh; prompt là hook duy nhất của
  bản tóm tắt (`compaction.rs:111-148`). `DetailedCompaction` của Python là một prompt,
  `DETAILED_PROMPT` (`compaction.rs:45-68`).
- Đường phản ứng (reactive, một lần từ chối vì độ dài ngữ cảnh) chỉ chạy khi spec có
  compaction. Python luôn có một chiến lược để quay về; ở đây một lần từ chối mà không có
  compaction sẽ kết thúc run.
- Việc ước lượng kích thước được tích hợp sẵn: khoảng bốn ký tự cho mỗi token và 1000
  cho mỗi ảnh (`compaction.rs:193-226`). Không có bộ ước lượng token cắm thêm được.
- Một bản tóm tắt đã trả phí được giữ nguyên vẹn trong checkpoint `Compacted`, không
  phải dưới dạng một công thức chỉ chứa hash. Một lần resume nối nó vào mà không cần
  lời gọi provider nào nữa
  (`crates/linch-core/src/run.rs:147-154`, `runner/compact.rs:245-266`).
- Projection luôn được ghi vào session store dưới dạng một log entry
  (`runner/write.rs:83-98`). Không có phát hiện store và không có opt-in.

## Bật compaction

`AgentSpec::compaction` mặc định là `None`, nghĩa là không bao giờ compact
(`crates/linch-core/src/agent/spec.rs:67`). Hãy đặt một `CompactionSpec`
(`compaction.rs:111-148`):

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `threshold_tokens` | không có | Compact khi view ước lượng đạt số token này. Nếu không có, compact gần cửa sổ ngữ cảnh của provider (bên dưới) |
| `keep_recent_turns` | 10 | Số lượt assistant được giữ nguyên văn sau bản tóm tắt; tối thiểu 1 |
| `prompt` | `DEFAULT_PROMPT` | Chỉ dẫn cho lời gọi tóm tắt (`compaction.rs:34-43`) |
| `max_output_tokens` | 4096 | Số token tối đa mà bản tóm tắt được dùng |
| `micro` | false | Xóa các kết quả tool cũ trước một bản tóm tắt ([bên dưới](#micro-compaction)) |
| `max_forced` | 3 | Số bản tóm tắt tối đa một run tạo ra vì một lần từ chối do độ dài ngữ cảnh; 0 là không bao giờ ([bên dưới](#context-length-recovery)) |

`micro` và `max_forced` bị bỏ khỏi spec đã serialize khi ở giá trị mặc định, nên contract
digest của một spec viết trước khi chúng tồn tại không đổi. `CompactionSpec::new(n)` đặt
ngưỡng và để phần còn lại ở giá trị mặc định (`compaction.rs:168-173`).
`AgentBuilder::build` từ chối ngưỡng bằng 0, `keep_recent_turns` bằng 0 hoặc
`max_output_tokens` bằng 0 với `BuildError::Invalid` (`compaction.rs:176-191`,
`agent/builder.rs:237`).

Spec là một phần của run contract, nên thay đổi nó khiến các agent có cài đặt cũ và mới
không tương thích: một lần resume run cũ bị từ chối
(`agent/spec.rs`, `runner/start.rs`).

## Khi nào một run compact

Một lượt bắt đầu ở `Step::Compact` khi spec có compaction, và ở
`Step::Dispatch` nếu không (`runner/turn.rs:313`). `compact` quay về lời gọi chính
mà không làm gì khi một trong các điều sau đúng
(`runner/compact.rs:72-127`):

1. Run đã bị hủy: nó abort thay vì tiếp tục.
2. `max_turns` hoặc budget đã không cho phép thêm lời gọi nào. Lời gọi chính kết thúc
   run bằng terminal của chính nó.
3. View chưa đến hạn.
4. `keep_from` không tìm thấy điểm nào để gộp. View cần ít nhất một lượt assistant giữa
   message đầu tiên và các lượt được giữ (`compaction.rs:260-270`). Một lượt đầu chỉ có
   một prompt thì không bao giờ compact.

Việc đến hạn do `due` quyết định (`compaction.rs:228-243`):

- Với `threshold_tokens`, ước lượng bằng hoặc vượt ngưỡng đó.
- Không có nó, ước lượng cộng với phần dự trữ đầu ra đạt 80% cửa sổ ngữ cảnh của model.
  Phần dự trữ là `max_output_tokens` của spec khi được đặt và khác 0, nếu không thì
  bằng một phần năm cửa sổ, tối thiểu 1 và tối đa 32768. Cửa sổ là
  `Provider::context_window` cho model của spec ([providers](./providers.md)).

Ước lượng tính văn bản, thinking, thông báo catalog, input của tool và kết quả của tool;
ảnh tốn một lượng cố định; chữ ký và reasoning bị che không được tính
(`compaction.rs:193-226`).

`keep_from` là message assistant thứ `keep_recent_turns` tính từ cuối view. Phần được
giữ bắt đầu từ message đó, nên một kết quả tool không bao giờ bị cắt rời khỏi lời gọi
của nó (`compaction.rs:245-270`).

<a id="micro-compaction"></a>

## Micro-compaction

Với `micro`, một view đến hạn được xóa trước (`runner/compact.rs:104-111`).
`clear_tool_results` thay mọi kết quả tool trước các lượt được giữ bằng
`CLEARED_RESULT`, `[tool result elided to save context]`, trừ khi nó đã không dài hơn
placeholder và không chứa ảnh (phép thử của Python, `compaction.rs:276-333`). Mọi
`tool_use_id` đều giữ kết quả của nó, và các message không đổi thì được chia sẻ, không
sao chép.

Nếu view đã xóa không còn đến hạn, `clear` ghi lại nó và lượt tiếp tục với lời gọi chính
(`runner/compact.rs:301-342`):

1. `pre_compact` chạy với strategy `micro`.
2. Event `compaction` (strategy `micro`, `messages_before` bằng `messages_after`) được
   stage cùng một checkpoint `Cleared { before, attempt }` đang giữ (`run.rs:159-168`).
3. `project` flush checkpoint đang giữ và event của nó, rồi nối projection, lý do
   `micro-compaction`, vào session.
4. `post_compact` chạy.

Đó là một lần ghi run và một lần ghi session, và không có lời gọi provider nào. Nếu view
đã xóa vẫn đến hạn, bản tóm tắt theo sau, và nó đọc phần được gộp với kết quả tool đã
được xóa, như Python làm sau bước micro của nó (`runner/compact.rs:128-135`).

Việc xóa thay đổi các message ở giữa view, nên prefix đã cache của provider kết thúc ở
kết quả bị xóa đầu tiên. Đó là lý do `micro` mặc định tắt.

## Lời gọi tóm tắt

Nếu view đến hạn, run dựng một request tóm tắt (`compaction.rs:388-404`,
`runner/compact.rs:128-150`):

- Các message là view trước `keep_from`.
- Block system là prompt tóm tắt, không cacheable.
- Các tool là những tool mà catalog của session đang giữ, gồm các tool prefix đã đóng
  băng cộng với những tool thêm vào kể từ đó, với tool choice bị tắt. Một provider có
  thể từ chối lời gọi tool và kết quả tool trong một request không định nghĩa tool nào.
- `max_output_tokens` là giới hạn compaction của spec. Không đặt cache control;
  chỉ lời gọi chính mới yêu cầu prompt caching.
- Một run đã failover sang một model fallback sẽ tóm tắt trên model đó.

Lời gọi là một lời gọi model loại `compaction`, với id
`{run_id}:compaction:{turn}` và một số thứ tự lần thử delivery. Các bản tóm tắt của một
lượt (một bản ở đầu lượt và các bản bắt buộc) lấy attempt 1, 2, ... theo thứ tự. Event
delivery của nó được stage, checkpoint `CompactionPending` được giữ lại, và một lần
flush ghi cả hai trước lời gọi. Lời gọi không stream delta nào: bản tóm tắt không phải là
câu trả lời của assistant.

Lời gọi kết thúc với một receipt. Usage của nó được tính và trừ vào budget, và một event
`usage` được stage, dù lời gọi có cho ra bản tóm tắt hay không (`runner/write.rs`).

### Khi không có bản tóm tắt

Ở đầu một lượt, một lời gọi thất bại, hoặc trả lời không có văn bản, không gây lỗi
nghiêm trọng. Lượt tiếp tục mà không compact, với lời gọi chính. Receipt và `usage` được
ghi ngay, cùng một checkpoint `CompactionPending` có tính đến chúng, vì một response đã
trả phí phải bền vững ngay khi nó tồn tại. Một lần resume từ checkpoint đó thấy receipt
đã kết thúc và đi thẳng tới lời gọi chính ở attempt 1, nên không bao giờ mua lại bản
tóm tắt (`journal/reconcile.rs:251-273`). Một lần hủy trong lúc gọi sẽ abort run.

Một bản tóm tắt bắt buộc mà không cho ra kết quả sẽ kết thúc run (bên dưới).

## Projection có ghi log

Một bản tóm tắt được chấp nhận trở thành một message của user và một view thay thế:

1. `summary_message` mở đầu văn bản bằng `<summary of earlier conversation>`, để model
   đọc nó như một bản tóm lược (`compaction.rs:82, 406-410`). Khi redaction che response,
   văn bản tóm tắt được che trước.
2. Khi skill được bật, message còn mang một `<system-reminder>` chứa nội dung của từng
   skill mà model đã nạp trong session, vì bản tóm tắt đã gộp các kết quả tool của chúng
   đi (`crates/linch-core/src/skills.rs`).
3. Nếu session có catalog, một thông báo nhắc lại mọi thay đổi catalog của session.
4. `replacement` là message đầu tiên như cũ, bản tóm tắt, rồi các message từ `keep_from`
   trở đi (`compaction.rs:412-424`).

Sau đó run stage event `compaction` (số message và token trước và sau, chiến lược, thời
lượng, `forced`) và lưu `Compacted` cùng bản tóm tắt và `keep_from`
(`runner/compact.rs:219-243`, `crates/linch-core/src/events/kinds/control.rs`).
`Compacted` ghi write-through cùng receipt và `usage`, nên một bản tóm tắt đã trả phí
không bao giờ bị mất.

Tiếp theo, `compacted` ghi projection vào session (`runner/compact.rs:245-266`).
`project` flush checkpoint đang giữ, nối một `LogEntry::Projection` vào session store
trong một lần ghi, ghi nó vào log trong bộ nhớ, rồi chạy các callback `on_compact` của
agent (`runner/write.rs:83-98`). Checkpoint `Compacted` đã bền vững, nên lần flush
không còn gì đang giữ để ghi. Lời gọi chính theo sau.

Một lượt có compact thêm hai lần ghi run và một lần ghi session: delivery cùng
`CompactionPending`, rồi write-through `Compacted`, rồi lần nối projection
(`runner/compact.rs:1-13`).

### `on_compact`

`AgentBuilder::on_compact` đăng ký một callback của host chạy sau mỗi projection
compaction, summary hay micro (`agent/builder.rs:132`). Nó không phải một hook: nó không
nằm trong run contract và không để lại bản ghi nào. Facade `linch` dùng nó để quên bằng
chứng đọc của session (`ReadTracker::forget`, `crates/linch/src/open.rs:191`), như
`reset_read_tracker` của Python: model phải `Read` lại một tệp trước khi `Edit` hoặc
`Write` thay đổi nó, vì những gì nó đã đọc có thể đã bị gộp đi. Ở một lần resume nối thêm
một projection, các callback chạy lại; quên hai lần thì vô hại.

<a id="context-length-recovery"></a>

## Khôi phục theo độ dài ngữ cảnh

Mỗi adapter ánh xạ lần từ chối một prompt quá dài của provider của nó sang
`ProviderError::ContextLength` ([providers](./providers.md#lỗi-và-retry)). Nó không thể
retry, và một lần từ chối với status 4xx (hoặc 503, 529) trước bất kỳ event nào là miễn
phí, như mọi lần từ chối kiểu đó (`provider/call.rs:257`). `kind` của event lỗi là
`context_length`.

`dispatch` hỏi `overflow` trước khi coi lỗi là cuối cùng
(`runner/turn.rs:464`, `runner/compact.rs:268-295`):

1. Với `micro`, nếu có kết quả tool cũ nào đáng xóa, `clear` ghi lại view đã xóa (với
   `forced` được đặt trên event) và chuyển sang attempt chính kế tiếp của lượt.
2. Nếu không, chừng nào số bản tóm tắt bắt buộc đã tạo còn ít hơn `max_forced` và
   `forced_keep_from` tìm được một điểm để gộp, run tạo một bản tóm tắt với `redispatch`
   đặt thành attempt chính kế tiếp. `forced_keep_from` giữ `keep_recent_turns`, hoặc chỉ
   lượt cuối khi cách đó không gộp được gì, vì chính các lượt được giữ có thể là thứ
   không vừa (`compaction.rs:272-274`).
3. Nếu không, `overflow` không cho gì và lần từ chối kết thúc run như mọi lỗi provider.

Một bản tóm tắt bắt buộc mà thất bại, trả lời không có văn bản, hoặc không tìm được chỗ
để giữ sẽ kết thúc run qua `unshortened` (`runner/end.rs:124`): một event `error` tên
`ProviderError`, kind `context_length`, với thông báo "the prompt exceeds the model's
context window, and the compaction it forced could not shorten it", và một run `failed`.
Provider đã từ chối prompt mà view chưa compact sẽ gửi, nên tiếp tục chỉ mua lại cùng một
lần từ chối.

Lời gọi chính kế tiếp là attempt delivery kế tiếp của cùng lượt đó. Nó không phải một
retry của provider: không có event `provider_retry`, và số lần thử của retry policy không
bị tiêu. Mỗi bước lặp lại ở lần từ chối kế tiếp, nên một lượt có thể xóa, rồi tóm tắt,
rồi tóm tắt lần nữa chỉ giữ lượt cuối. `max_forced` giới hạn các bản tóm tắt của một run;
số điểm có thể gộp giới hạn một lượt.

### Một lần resume đọc lại gì

Không có gì mới được ghi để nhớ một compaction bắt buộc. `Forced::of` đọc nó từ các
delivery bền vững (`compaction.rs:336-386`): một delivery tóm tắt đến sau một delivery
chính của cùng lượt là bắt buộc; số lượng những cái đó là `max_forced` đã tiêu của run;
số delivery chính và tóm tắt của lượt cho ra các attempt kế tiếp. `runner/start.rs:454`
đặt các bộ đếm của runner từ nó, nên một run được resume không quên một bản tóm tắt bắt
buộc cũng không dùng lại một delivery id.

### Khôi phục

`reconcile` ánh xạ các phase của compaction (`journal/reconcile.rs:251-284`):

| Checkpoint | Trạng thái tìm thấy | Run tiếp tục với |
|---|---|---|
| `CompactionPending` | Không có receipt cho delivery của bản tóm tắt | Một lời gọi tóm tắt khác, sau một receipt bị hủy cho lời gọi chưa kết thúc; một bản bắt buộc thì sau đó đi tiếp tới attempt chính của nó |
| `CompactionPending` | Có một receipt, bản tóm tắt ở đầu lượt | Lời gọi chính ở attempt 1; lời gọi tóm tắt đã kết thúc mà không có bản tóm tắt |
| `CompactionPending` | Có một receipt, bản tóm tắt bắt buộc | Run kết thúc qua `unshortened` |
| `Compacted` | Projection chưa có trong session | Nối nó, rồi lời gọi chính ở attempt kế tiếp của lượt |
| `Compacted` | Projection đã là entry cuối cùng | Lời gọi chính ở attempt kế tiếp của lượt |
| `Cleared` | Projection chưa có trong session | Xóa lại view từ `before`, nối nó, rồi attempt chính `attempt` |
| `Cleared` | Một projection ở cuối với lý do `micro-compaction` | Attempt chính `attempt` |

Phép kiểm tra cho `Compacted` là session có độ dài bằng độ dài của checkpoint, hoặc dài
hơn một entry với một `Projection` ở cuối mà bản tóm tắt của nó là bản tóm tắt của
checkpoint. Với `Cleared`, projection ở cuối phải có lý do micro. Mọi độ dài khác là một
xung đột session (`journal/reconcile/session.rs:77-122`).

`Cleared` là phase checkpoint mới duy nhất. Nó chỉ được ghi bởi một agent có compaction
đặt `micro`, nên checkpoint của mọi agent khác giữ nguyên hình dạng; một test ghim JSON
của các phase cũ (`run.rs`).

## Session log và những gì model nhìn thấy

`SessionLog` là một chuỗi entry chỉ nối thêm: message, projection và prefix đã đóng băng
của session. Hai view đọc được suy ra từ nó
(`crates/linch-core/src/session_log.rs:1-23`):

- `full_history` giữ mọi message được nối với `historical: true`. Một projection không
  bao giờ thay đổi nó.
- `provider_view` giữ mọi message được nối với `visible: true`, và mỗi
  `ProjectionEntry` thay view tích lũy bằng `replacement` của nó.

`record_projection` nối entry và hoán đổi view dẫn xuất. Một replacement bằng với view
hiện tại thì không được ghi (`session_log.rs:158-183`).
Không có phương thức nào sửa hoặc xóa một entry (`session_log.rs:14-15`). Các view là
snapshot dùng chung qua `Arc`, nên một lần đọc chỉ là clone con trỏ (`session_log.rs:200-211`).
Run ghi session store trước và log trong bộ nhớ sau (`runner/write.rs:83-98`).

Log bền vững là các entry của session store. `session_log` dựng lại log trong bộ nhớ từ
chúng, nên một lần reload phát lại từng `Projection` và provider view trở về dạng đã
compact (`runner/write.rs:188`, `crates/linch-core/src/store.rs`).

Request mà model nhận được sau một lần compaction được dựng từ các phần sau:

| Phần | Nguồn |
|---|---|
| Các block system và tool | Prefix đã đóng băng của session, từng byte một (`session_log.rs:53-60`) |
| Message đầu tiên | Message đầu tiên của view, không đổi (`compaction.rs:412-424`) |
| Bản tóm tắt | Message user `summary` cùng các reminder và thông báo catalog của nó |
| Các lượt được giữ | `keep_recent_turns` lượt assistant cuối và những gì theo sau chúng |
| Các message sau đó | Được nối sau projection khi run tiếp tục |
| Các block `context_build` | Chỉ được thêm vào phần đuôi của request, không bao giờ được ghi log (`runner/hooks.rs:255-266`) |

Một bản tóm tắt không chạm vào prefix hay message đầu tiên, nên prefix đã cache của
provider vẫn còn sau đó. System prompt và các tool của lời gọi chính giữ nguyên như cũ,
còn bản tóm tắt và các lượt được giữ là phần thay đổi. Các reminder skill và thông báo
catalog trong bản tóm tắt nằm trong provider view chứ không nằm trong full history.

Model không bao giờ thấy lại nội dung đã gộp hoặc đã xóa. Nó vẫn nằm trong
`full_history` và trong session store, để kiểm toán và phát lại.

<a id="cut-off-answers"></a>

## Câu trả lời bị cắt

`AgentSpec::truncation_recovery` (`crates/linch-core/src/truncation.rs`,
`TruncationRecovery` của Python) tiếp tục một câu trả lời văn bản mà giới hạn đầu ra đã
cắt. Nó không phải compaction, nhưng giống compaction, nó phản ứng với kích thước mà
không cần retry của provider.

Khi một response có văn bản, không có lời gọi tool và stop reason `max_tokens` đến, và
số lần đã tiếp tục trong run ít hơn `max_attempts`, `received` giữ văn bản của nó như một
phần và stage phản hồi (của spec, hoặc `DEFAULT_FEEDBACK`, trong một `<system-reminder>`)
làm message user kế tiếp, cùng đường đi mà `continue` của một callback `stop` dùng
(`runner/turn.rs:532-608`). Câu trả lời bị cắt được nối vào session như mọi response.
Văn bản cuối cùng của run là các phần, theo thứ tự, rồi đến văn bản của response cuối;
toàn bộ văn bản đó là thứ mà output schema, các verifier và `stop` nhìn thấy. Khi
redaction scrub response, văn bản ghép được scrub lại, vì một match bị cắt đôi qua hai
phần chỉ nguyên vẹn ở đó. Một
response gọi tool bắt đầu một chuỗi phần mới.

Không có gì mới được checkpoint. `Truncated::of` đọc lại số lần thử và các phần đang chờ
từ các event bền vững `assistant` và `user`: một câu trả lời bị cắt được tính khi phản
hồi của nó đã bền vững (`truncation.rs`, `runner/start.rs:457`).

## Test

- `crates/linch-core/tests/e2e_memory_off.rs`: một golden digest của các event và
  checkpoint của một run không có compaction, và của một run mà compaction không bao giờ
  kích hoạt, để một tính năng đang tắt không tốn gì.
- `crates/linch-core/tests/e2e_context_length.rs`: tóm tắt và gọi lại, micro trước, lần
  từ chối không thể rút ngắn, `max_forced`, một bản tóm tắt bắt buộc thất bại, redaction
  của một bản tóm tắt bắt buộc, và một lần crash ở mọi lần ghi của từng đường, được
  resume về cùng một kết quả.
- `crates/linch-core/tests/e2e_truncation.rs`: các câu trả lời được tiếp tục và ghép lại,
  phép kiểm tra schema trên toàn bộ văn bản, một lần crash ở mọi lần ghi.
- `crates/linch/tests/compaction.rs`: một lần compaction khiến model đọc lại một tệp
  trước khi sửa.
