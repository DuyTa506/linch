# Thu gọn ngữ cảnh

[Bản gốc tiếng Anh](../../architecture/compaction.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Compaction gộp phần cũ của một provider view dài thành một bản tóm tắt. Một run thực
hiện việc này ở đầu một lượt, trước lời gọi provider chính, khi spec của agent bật nó.
Bản tóm tắt đến từ một lời gọi model phụ trợ với delivery và receipt riêng. Kết quả là
một projection có ghi log trên log chỉ nối thêm của session: model nhìn thấy một view
ngắn hơn, còn lịch sử đầy đủ giữ nguyên mọi message. Các phần thuần nằm trong
`crates/linch-core/src/compaction.rs`; phần của run, cùng các barrier của nó, là
`crates/linch-core/src/runner/compact.rs`. Dùng trang này khi bạn thay đổi thời điểm
một run compact, nội dung của bản tóm tắt, hoặc cách một run khôi phục một bản tóm tắt
đã trả phí.

Linch bản Python có cùng cấu trúc ([compaction](../../../../docs/architecture/compaction.md)).
Điểm khác trong Rust:

- Chỉ có một chiến lược, tên là `summary` (`compaction.rs:33-34`). Không có compaction
  ladder, không có nấc `micro_compact` không dùng LLM, không có `DetailedCompaction`
  và không có chiến lược tùy chỉnh. `CompactionSpec` là dữ liệu, và prompt là hook duy
  nhất của nó (`compaction.rs:54-77`).
- Không có đường phản ứng (reactive). Không có gì trong `linch-core` compact để đáp lại
  một lỗi độ dài ngữ cảnh; một run chỉ compact ở đầu lượt.
- Việc ước lượng kích thước được tích hợp sẵn: khoảng bốn ký tự cho mỗi token và 1000
  cho mỗi ảnh (`compaction.rs:117-146`). Không có bộ ước lượng token cắm thêm được.
- Một bản tóm tắt đã trả phí được giữ nguyên vẹn trong checkpoint `Compacted`, không
  phải dưới dạng một công thức chỉ chứa hash. Một lần resume nối nó vào mà không cần
  lời gọi provider nào nữa
  (`crates/linch-core/src/run.rs:126-134`, `runner/compact.rs:155-171`).
- Projection luôn được ghi vào session store dưới dạng một log entry
  (`runner/write.rs:83-92`). Không có phát hiện store và không có opt-in.

## Bật compaction

`AgentSpec::compaction` mặc định là `None`, nghĩa là không bao giờ compact
(`crates/linch-core/src/agent/spec.rs:60-63`). Hãy đặt một `CompactionSpec`
(`compaction.rs:54-77`):

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `threshold_tokens` | không có | Compact khi view ước lượng đạt số token này. Nếu không có, compact gần cửa sổ ngữ cảnh của provider (bên dưới) |
| `keep_recent_turns` | 10 | Số lượt assistant được giữ nguyên văn sau bản tóm tắt; tối thiểu 1 |
| `prompt` | `DEFAULT_PROMPT` | Chỉ dẫn cho lời gọi tóm tắt (`compaction.rs:22-28`) |
| `max_output_tokens` | 4096 | Số token tối đa mà bản tóm tắt được dùng |

`CompactionSpec::new(n)` đặt ngưỡng và để phần còn lại ở giá trị mặc định
(`compaction.rs:93-100`). `AgentBuilder::build` từ chối ngưỡng bằng 0, `keep_recent_turns`
bằng 0 hoặc `max_output_tokens` bằng 0 với `BuildError::Invalid`
(`compaction.rs:102-114`, `agent/builder.rs:216-218`).

Spec là một phần của run contract, nên thay đổi nó khiến các agent có cài đặt cũ và mới
không tương thích: một lần resume run cũ bị từ chối
(`agent/spec.rs:131-155`, `runner/start.rs:425-431`).

## Khi nào một run compact

Một lượt bắt đầu ở `Step::Compact` khi spec có compaction, và ở
`Step::Dispatch` nếu không (`runner/turn.rs:237-248`). `compact` quay về lời gọi chính
mà không làm gì khi một trong các điều sau đúng
(`runner/compact.rs:62-77`):

1. Run đã bị hủy: nó abort thay vì tiếp tục (`compact.rs:57-60`).
2. `max_turns` hoặc budget đã không cho phép thêm lời gọi nào. Lời gọi chính kết thúc
   run bằng terminal của chính nó (`compact.rs:65-68`).
3. `keep_from` không tìm thấy điểm nào để gộp. View cần ít nhất một lượt assistant giữa
   message đầu tiên và các lượt được giữ (`compaction.rs:172-187`). Một lượt đầu chỉ có
   một prompt thì không bao giờ compact.
4. View chưa đến hạn.

Việc đến hạn do `due` quyết định (`compaction.rs:148-170`):

- Với `threshold_tokens`, ước lượng bằng hoặc vượt ngưỡng đó.
- Không có nó, ước lượng cộng với phần dự trữ đầu ra đạt 80% cửa sổ ngữ cảnh của model.
  Phần dự trữ là `max_output_tokens` của spec khi được đặt và khác 0, nếu không thì
  bằng một phần năm cửa sổ, tối thiểu 1 và tối đa 32768. Cửa sổ là
  `Provider::context_window` cho model của spec
  (`agent.rs:181-184`, [providers](./providers.md)).

Ước lượng tính văn bản, thinking, thông báo catalog, input của tool và kết quả của tool;
ảnh tốn một lượng cố định; chữ ký và reasoning bị che không được tính
(`compaction.rs:117-146`).

`keep_from` là message assistant thứ `keep_recent_turns` tính từ cuối view. Phần được
giữ bắt đầu từ message đó, nên một kết quả tool không bao giờ bị cắt rời khỏi lời gọi
của nó (`compaction.rs:172-187`).

## Lời gọi tóm tắt

Nếu view đến hạn, run dựng một request tóm tắt (`compaction.rs:189-207`,
`runner/compact.rs:78-91`):

- Các message là view trước `keep_from`.
- Block system là prompt tóm tắt, không cacheable.
- Các tool là những tool mà catalog của session đang giữ, gồm các tool prefix đã đóng
  băng cộng với những tool thêm vào kể từ đó, với tool choice bị tắt. Một provider có
  thể từ chối lời gọi tool và kết quả tool trong một request không định nghĩa tool nào
  (`compaction.rs:189-191`, `compact.rs:84-89`, `catalog.rs:120-142`).
- `max_output_tokens` là giới hạn compaction của spec. Không đặt cache control;
  chỉ lời gọi chính mới yêu cầu prompt caching (`agent.rs:169-179`).

Lời gọi là một lời gọi model loại `compaction`, với id
`{run_id}:compaction:{turn}` và một số thứ tự lần thử delivery
(`provider/call.rs:96-108`). Event delivery của nó được stage, checkpoint
`CompactionPending` được giữ lại, và một lần flush ghi cả hai trước lời gọi
(`compact.rs:90-96`). Lời gọi không stream delta nào: bản tóm tắt không phải là câu trả
lời của assistant (`compact.rs:97-98`).

Lời gọi kết thúc với một receipt. Usage của nó được tính và trừ vào budget, và một event
`usage` được stage, dù lời gọi có cho ra bản tóm tắt hay không
(`compact.rs:99-105`, `runner/write.rs:121-144`).

### Khi không có bản tóm tắt

Một lời gọi thất bại, hoặc trả lời không có văn bản, không gây lỗi nghiêm trọng. Lượt
tiếp tục mà không compact, với lời gọi chính (`compact.rs:114-120`). Receipt và
`usage` được ghi ngay, cùng một checkpoint `CompactionPending` có tính đến chúng, vì một
response đã trả phí phải bền vững ngay khi nó tồn tại. Một lần resume từ checkpoint đó
thấy receipt đã kết thúc và đi thẳng tới lời gọi chính ở attempt 1, nên không bao giờ
mua lại bản tóm tắt (`journal/reconcile.rs:210-220`). Một lần hủy trong lúc gọi sẽ abort
run (`compact.rs:106-111`).

## Projection có ghi log

Một bản tóm tắt được chấp nhận trở thành một message của user và một view thay thế:

1. `summary_message` mở đầu văn bản bằng `<summary of earlier conversation>`, để model
   đọc nó như một bản tóm lược (`compaction.rs:36-37, 209-212`).
2. Khi skill được bật, message còn mang một `<system-reminder>` chứa nội dung của từng
   skill mà model đã nạp trong session, vì bản tóm tắt đã gộp các kết quả tool của chúng
   đi (`compact.rs:122-126`, `crates/linch-core/src/skills.rs:171`).
3. Nếu session có catalog, một thông báo nhắc lại mọi thay đổi catalog của session
   (`compact.rs:127-132`).
4. `replacement` là message đầu tiên như cũ, bản tóm tắt, rồi các message từ `keep_from`
   trở đi (`compaction.rs:214-226`).

Sau đó run stage event `compaction` (số message và token trước và sau, chiến lược, thời
lượng) và lưu `Compacted` cùng bản tóm tắt và `keep_from`
(`compact.rs:133-147`, `crates/linch-core/src/events/kinds/control.rs:64`).
`Compacted` ghi write-through cùng receipt và `usage`, nên một bản tóm tắt đã trả phí
không bao giờ bị mất (`journal.rs:192-193`).

Tiếp theo, `compacted` ghi projection vào session (`compact.rs:158-171`).
`project` flush checkpoint đang giữ, nối một `LogEntry::Projection` vào session store
trong một lần ghi, rồi ghi nó vào log trong bộ nhớ
(`runner/write.rs:83-92`). Checkpoint `Compacted` đã bền vững, nên lần flush
không còn gì đang giữ để ghi. Lời gọi chính theo sau.

Một lượt có compact thêm hai lần ghi run và một lần ghi session: delivery cùng
`CompactionPending`, rồi write-through `Compacted`, rồi lần nối projection
(`compact.rs:3-10`).

### Khôi phục

`reconcile` ánh xạ các phase của compaction (`journal/reconcile.rs:210-230`):

| Checkpoint | Trạng thái tìm thấy | Run tiếp tục với |
|---|---|---|
| `CompactionPending` | Không có receipt cho delivery của bản tóm tắt | Một lời gọi tóm tắt khác, sau một receipt bị hủy cho lời gọi chưa kết thúc |
| `CompactionPending` | Có một receipt | Lời gọi chính; lời gọi tóm tắt đã kết thúc mà không có bản tóm tắt |
| `Compacted` | Projection chưa có trong session | Nối nó, rồi đến lời gọi chính |
| `Compacted` | Projection đã là entry cuối cùng | Lời gọi chính |

Phép kiểm tra cho `Compacted` là session có độ dài bằng độ dài của checkpoint, hoặc dài
hơn một entry với một `Projection` ở cuối mà bản tóm tắt của nó là bản tóm tắt của
checkpoint. Mọi độ dài khác là một xung đột session
(`journal/reconcile/session.rs:75-98`).

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
Run ghi session store trước và log trong bộ nhớ sau (`runner/write.rs:89-90`).

Log bền vững là các entry của session store. `session_log` dựng lại log trong bộ nhớ từ
chúng, nên một lần reload phát lại từng `Projection` và provider view trở về dạng đã
compact (`runner/write.rs:181-196`,
`crates/linch-core/src/store.rs:197-219`).

Request mà model nhận được sau một lần compaction được dựng từ các phần sau:

| Phần | Nguồn |
|---|---|
| Các block system và tool | Prefix đã đóng băng của session, từng byte một (`session_log.rs:53-60`) |
| Message đầu tiên | Message đầu tiên của view, không đổi (`compaction.rs:214-226`) |
| Bản tóm tắt | Message user `summary` cùng các reminder và thông báo catalog của nó |
| Các lượt được giữ | `keep_recent_turns` lượt assistant cuối và những gì theo sau chúng |
| Các message sau đó | Được nối sau projection khi run tiếp tục |
| Các block `context_build` | Chỉ được thêm vào phần đuôi của request, không bao giờ được ghi log (`runner/hooks.rs:117-129`) |

Compaction không chạm vào prefix hay message đầu tiên, nên prefix đã cache của provider
vẫn còn sau đó (`compaction.rs:7-10`). System prompt và các tool của lời gọi chính giữ
nguyên như cũ, còn bản tóm tắt và các lượt được giữ là phần thay đổi. Các reminder skill
và thông báo catalog trong bản tóm tắt nằm trong provider view chứ không nằm trong full
history (`compact.rs:12-18`).

Model không bao giờ thấy lại các message đã gộp. Chúng vẫn nằm trong `full_history`
và trong session store, để kiểm toán và phát lại.
