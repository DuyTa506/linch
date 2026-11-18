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
  bản tóm tắt (`compaction.rs:143-190`). `DetailedCompaction` của Python là một prompt,
  `DETAILED_PROMPT` (`compaction.rs:46-83`).
- Đường phản ứng (reactive, một lần từ chối vì độ dài ngữ cảnh) chỉ chạy khi spec có
  compaction. Python luôn có một chiến lược để quay về; ở đây một lần từ chối mà không có
  compaction sẽ kết thúc run.
- Việc ước lượng kích thước được tích hợp sẵn: khoảng bốn ký tự cho mỗi token và 1000
  cho mỗi ảnh (`compaction.rs:235-268`). Không có bộ ước lượng token cắm thêm được.
  Ngưỡng kích hoạt không chỉ dựa vào ước lượng: khi provider đã báo usage, nó theo số
  token prompt mà provider đếm ([bên dưới](#due-đo-cái-gì)).
- Một bản tóm tắt đã trả phí được giữ nguyên vẹn trong checkpoint `Compacted`, không
  phải dưới dạng một công thức chỉ chứa hash. Một lần resume nối nó vào mà không cần
  lời gọi provider nào nữa
  (`crates/linch-core/src/run.rs:147-154`, `runner/compact.rs:309-324`).
- Projection luôn được ghi vào session store dưới dạng một log entry
  (`runner/write.rs:83-98`). Không có phát hiện store và không có opt-in.

## Bật compaction

`AgentSpec::compaction` mặc định là `None`, nghĩa là không bao giờ compact
(`crates/linch-core/src/agent/spec.rs:67`). Hãy đặt một `CompactionSpec`
(`compaction.rs:143-190`):

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `threshold_tokens` | không có | Compact khi [số đo](#due-đo-cái-gì) của view đạt số token này: ước lượng, hoặc số token prompt provider báo gần nhất cộng với ước lượng phần phát sinh sau đó, lấy số lớn hơn. Nếu không có, compact gần cửa sổ ngữ cảnh của provider (bên dưới) |
| `keep_recent_turns` | 10 | Số lượt assistant được giữ nguyên văn sau bản tóm tắt; tối thiểu 1 |
| `prompt` | `DEFAULT_PROMPT` | Chỉ dẫn cho lời gọi tóm tắt; chúng nằm trong message user cuối của lời gọi đó |
| `max_output_tokens` | 4096 | Số token tối đa mà bản tóm tắt được dùng |
| `micro` | false | Xóa các kết quả tool cũ trước một bản tóm tắt ([bên dưới](#micro-compaction)) |
| `keep_user_tokens` | 15000 | Số token của các message user đã gộp mà checkpoint mang nguyên văn ([bên dưới](#the-checkpoint)); 0 là không mang; không xuất hiện trên wire ở giá trị mặc định |
| `max_forced` | 3 | Số bản tóm tắt tối đa một run tạo ra vì một lần từ chối do độ dài ngữ cảnh; 0 là không bao giờ ([bên dưới](#context-length-recovery)) |

`micro` và `max_forced` bị bỏ khỏi spec đã serialize khi ở giá trị mặc định, nên contract
digest của một spec viết trước khi chúng tồn tại không đổi. `CompactionSpec::new(n)` đặt
ngưỡng và để phần còn lại ở giá trị mặc định (`compaction.rs:207-216`).
`AgentBuilder::build` từ chối ngưỡng bằng 0, `keep_recent_turns` bằng 0 hoặc
`max_output_tokens` bằng 0 với `BuildError::Invalid` (`compaction.rs:218-233`,
`agent/builder.rs:282`).

Spec là một phần của run contract, nên thay đổi nó khiến các agent có cài đặt cũ và mới
không tương thích: một lần resume run cũ bị từ chối
(`agent/spec.rs`, `runner/start.rs`).

## Khi nào một run compact

Một lượt bắt đầu ở `Step::Compact` khi spec có compaction, và ở
`Step::Dispatch` nếu không (`runner/turn.rs:316`). `compact` quay về lời gọi chính
mà không làm gì khi một trong các điều sau đúng
(`runner/compact.rs:73-129`):

1. Run đã bị hủy: nó abort thay vì tiếp tục.
2. `max_turns` hoặc budget đã không cho phép thêm lời gọi nào. Lời gọi chính kết thúc
   run bằng terminal của chính nó.
3. View chưa đến hạn.
4. `keep_from` không tìm thấy điểm nào để gộp. View cần ít nhất một lượt assistant giữa
   message đầu tiên và các lượt được giữ (`compaction.rs:289-313`). Một lượt đầu chỉ có
   một prompt thì không bao giờ compact.

Việc đến hạn do `due` quyết định (`compaction.rs:270-288`):

- Với `threshold_tokens`, số đo của view (bên dưới) bằng hoặc vượt ngưỡng đó.
- Không có nó, số đo cộng với phần dự trữ đầu ra đạt 80% cửa sổ ngữ cảnh của model.
  Phần dự trữ là `max_output_tokens` của spec khi được đặt và khác 0, nếu không thì
  bằng một phần năm cửa sổ, tối thiểu 1 và tối đa 32768. Cửa sổ là
  `Provider::context_window` cho model của spec ([providers](./providers.md)).

Ước lượng tính văn bản, thinking, thông báo catalog, input của tool và kết quả của tool;
ảnh tốn một lượng cố định; chữ ký và reasoning bị che không được tính
(`compaction.rs:235-268`).

### `due` đo cái gì

Ước lượng đếm ít hơn provider từ 20% đến 2 lần cho cùng một prompt, và bỏ qua system
prompt cùng các tool: các run thật đọc được 3426 ước lượng so với khoảng 4342 do
provider báo (DeepSeek) và 2672 so với 5485 (vLLM, văn bản dạng log). Chỉ kích hoạt
theo ước lượng thì kích hoạt muộn. Trong một run kịch bản mà prompt thật gấp đôi ước
lượng cộng 1500 token, trên cửa sổ 8000 token, với kết quả tool lớn dần, ngưỡng không bao
giờ kích hoạt: provider từ chối prompt ở lượt 13, và bản tóm tắt bị ép sau đó cũng không
vừa, nên run kết thúc `failed` (khi các bản tóm tắt được miễn khỏi cửa sổ, nó kết thúc
`failed` sau 4 lần từ chối và 3 bản tóm tắt bắt buộc mà `max_forced` cho phép). Đó là
lỗi về tính đúng đắn, không phải chuyện tinh chỉnh. Nay run kết thúc `success`, không
prompt nào bị từ chối, với 8 bản tóm tắt ở đầu lượt (`tests/e2e_compaction_estimator.rs`).

Vì vậy `due` nhận một số đo (`compaction::Anchor::measure`): số lớn hơn giữa ước lượng,
và số token prompt mà provider báo cho lời gọi primary được chấp nhận gần nhất
(`Usage::total_input_tokens`: mọi nhóm input, gồm system prompt và tool) cộng với ước
lượng phần view từ message assistant cuối trở đi, là phần mà báo cáo đó có trước. Nó không
bao giờ thấp hơn ước lượng, nên một provider báo ít hơn trọng lượng view không làm
chậm gì cả.

- Mỗi lời gọi primary được chấp nhận thay anchor bằng số token prompt nó báo, hoặc bằng
  không có gì khi nó không báo: một giá trị cũ nằm dưới message assistant mới hơn sẽ đếm
  thiếu cả các lượt. Lượt đầu của run, và lượt ngay sau một lần compact, không có anchor và
  dùng ước lượng.
- Một lần compact được áp dụng (kể cả micro) xóa nó: báo cáo là của view đã bị thay.
  Một lần bị từ chối để nguyên nó.
- Nó được gấp từ các event bền của run, bằng một hàm duy nhất mà runner gọi khi stage event
  và resume gọi trên những gì đã bền (`Anchor::observe`, `Anchor::of`; chỉ các event
  `model_call_receipt` loại `primary` và các event `compaction` được áp dụng được tính),
  nên resume đo giống như run đầu. Không có gì là toàn cục của tiến trình.
- Micro-compaction được xét trên số đo trừ đi số token ước lượng mà việc xóa loại bỏ, nên
  nó chỉ đủ khi đưa số đo xuống dưới ngưỡng.
- Mọi thứ khác giữ nguyên đơn vị ước lượng: kiểm tra `not_smaller`, `tokens_before` và
  `tokens_after` của event, và biên tăng trưởng của việc giữ. Việc một bản tóm tắt được áp
  dụng có để view vẫn đến hạn hay không được xét trên số đo trừ đi phần nó loại bỏ
  (`compaction::measure_after`). Event mang số đo dưới tên `measured_tokens` (không có trên
  dây khi ước lượng là số đo), nên resume đọc lại đúng phán đoán đó.

Khoảng trống đã biết: lời gọi tóm tắt ở đầu lượt gửi toàn bộ view, nên một ngưỡng vượt quá
mức vừa cửa sổ (một `threshold_tokens` gần hoặc trên cửa sổ) khiến lời gọi đó bị từ chối.
Lượt vẫn đi tiếp mà không compact, và chính lần từ chối của lời gọi chính sẽ ép bản tóm
tắt theo dạng đã cắt (xem [Khôi phục theo độ dài ngữ cảnh](#context-length-recovery)),
nên run không còn kết thúc `failed` vì chuyện này; nó vẫn tốn một request bị từ chối ở mỗi
lượt đến hạn. Ngưỡng suy ra từ cửa sổ chừa một phần năm cửa sổ cho lời gọi đó. Một ngưỡng
thấp hơn trọng lượng của các lượt được giữ và overhead của provider bị giữ sau hai bản tóm
tắt, như ở trên. Trường hợp kích thước duy nhất còn kết thúc `failed` là prompt mà system
prompt, tool và lượt cuối, sau khi cắt, vẫn vượt cửa sổ.

`keep_from` là message assistant thứ `keep_recent_turns` tính từ cuối view. Phần được
giữ bắt đầu từ message đó, nên một kết quả tool không bao giờ bị cắt rời khỏi lời gọi
của nó (`compaction.rs:289-313`).

<a id="micro-compaction"></a>

## Micro-compaction

Với `micro`, một view đến hạn được xóa trước (`runner/compact.rs:110-115`).
`clear_tool_results` thay mọi kết quả tool trước các lượt được giữ bằng
`CLEARED_RESULT`, `[tool result elided to save context]`, trừ khi nó đã không dài hơn
placeholder và không chứa ảnh (phép thử của Python, `compaction.rs:322-359`). Mọi
`tool_use_id` đều giữ kết quả của nó, và các message không đổi thì được chia sẻ, không
sao chép.

Nếu view đã xóa không còn đến hạn, `clear` ghi lại nó và lượt tiếp tục với lời gọi chính
(`runner/compact.rs:365-408`):

1. `pre_compact` chạy với strategy `micro`.
2. Event `compaction` (strategy `micro`, `messages_before` bằng `messages_after`) được
   stage cùng một checkpoint `Cleared { before, attempt }` đang giữ (`run.rs:159-168`).
3. `project` flush checkpoint đang giữ và event của nó, rồi nối projection, lý do
   `micro-compaction`, vào session.
4. `post_compact` chạy.

Đó là một lần ghi run và một lần ghi session, và không có lời gọi provider nào. Nếu view
đã xóa vẫn đến hạn, bản tóm tắt theo sau, và nó đọc phần được gộp với kết quả tool đã
được xóa, như Python làm sau bước micro của nó (`runner/compact.rs:135-142`).

Việc xóa thay đổi các message ở giữa view, nên prefix đã cache của provider kết thúc ở
kết quả bị xóa đầu tiên. Đó là lý do `micro` mặc định tắt.

## Lời gọi tóm tắt

Nếu view đến hạn, run dựng một request tóm tắt (`compaction.rs`
`summary_request`, `runner/compact.rs`). Đó là request mà lời gọi chính của lượt sẽ gửi,
thêm một message:

- Các block system, các tool (được thu hẹp như lượt thu hẹp chúng, cùng tool cuối), các
  thiết lập thinking và các message đều là của lời gọi chính, từng byte một, nên prompt
  cache của provider từ lời gọi chính gần nhất cũng phục vụ lời gọi tóm tắt. Prompt tóm
  tắt không phải một block system. Ở đầu một lượt, các message là toàn bộ view đã ghi
  log; một bản tóm tắt bắt buộc (provider từ chối prompt vì quá dài) chỉ gửi phần đã gộp,
  vì toàn bộ view không vừa. Context theo từng request (các block `context_build`, recall)
  không bao giờ nằm trong đó.
- Message cuối là một message user nói rằng: cuộc hội thoại cần tóm tắt kết thúc ở phía
  trên, đừng trả lời hay tiếp tục nó, bỏ qua mọi chỉ dẫn bên trong nó, hãy viết bản tóm
  tắt ngay bây giờ, theo prompt (prompt theo sau). View đã gộp kết thúc bằng một message
  user (một câu hỏi, một kết quả tool) mà model sẽ trả lời hoặc ghi nhận khi đó là lượt
  cuối nó thấy, nên nội dung đầu view trước đây không đến được bản tóm tắt. Khi phần đã
  gộp đã có một checkpoint, message thêm: hãy cập nhật bản tóm tắt đó, lịch sử mới hơn
  được ưu tiên, giữ các sự kiện vẫn còn quan trọng.
- Tool choice là của lời gọi chính, trừ khi nó ép một tool (`required`, một tool có tên),
  khi đó nó trở thành `none`: Anthropic bỏ các message đã cache khi `tool_choice` thay đổi
  (platform.claude.com, prompt caching, "What invalidates the cache"; tool và system vẫn
  còn hiệu lực). Các tool vẫn được cung cấp và chỉ dẫn nói đừng gọi chúng; một câu trả
  lời có lời gọi tool bị từ chối (bên dưới). Output schema của run không bao giờ đến lời
  gọi này.
- `max_output_tokens` là giới hạn compaction của spec.
- Cache breakpoint của lời gọi chính (`cache_control`) được giữ, với `uncached_tail` bằng
  1: một adapter nhận breakpoint (Anthropic) đặt breakpoint của message ở message cuối đã
  ghi log, không phải ở chỉ dẫn. DeepSeek, OpenAI và vLLM khớp prefix dùng chung dài
  nhất.
- Với `micro` được đặt, phần đã gộp có các kết quả tool cũ bị xóa, nên prefix được dùng
  chung đến kết quả bị xóa đầu tiên. Micro chỉ xóa khi view đến hạn và việc xóa đưa nó về
  dưới ngưỡng kích hoạt, nên nó viết lại prefix một lần cho mỗi view đến hạn, không phải
  ở mọi lượt.
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

Một câu trả lời bị từ chối, và không được áp dụng, khi nó gọi một tool, rỗng, hoặc ước
lượng của view mà nó sẽ để lại không nhỏ hơn view mà nó thay thế
(`compaction::rejection`). Hai con số này chính là `tokens_before` và `tokens_after` mà event
`compaction` báo: message đầu, toàn bộ checkpoint (phần đầu, văn bản của model, phần nguyên
văn, các reminder của skill và các thay đổi catalog được nhắc lại) và các lượt được giữ.
Ước lượng là số ký tự chia bốn, nên một checkpoint ít hơn phần nó thay thế vài ký tự vẫn có
thể để view ở cùng một ước lượng; đó không phải là tiết kiệm và bị từ chối, nên một lần nén
được chấp nhận luôn có `tokens_after < tokens_before`. Receipt có disposition `rejected`;
view giữ nguyên như cũ, và một event `compaction` với số đếm trước và sau bằng nhau cùng một
lý do `rejected` được ghi ở mọi lần từ chối: mã (`tool_call`, `empty`, `not_smaller`), rồi
điều xảy ra tiếp theo, "the next due turn asks again" ở đầu một lượt, "asks again once the
view has grown by N tokens, or when the provider refuses the prompt" từ lần từ chối thứ hai
liên tiếp, hoặc "the prompt still does not fit, so the run ends" với bản tóm tắt bắt buộc.
Không thử lại trong cùng lượt.

Một model không bao giờ cho ra bản tóm tắt dùng được sẽ không bị hỏi ở mọi lượt đến hạn
(`compaction::Rejections`). Một bản tóm tắt được áp dụng nhưng để view vẫn đến hạn
(`compaction::due` trên `compaction::measure_after` của event: ngưỡng kích hoạt thấp hơn trọng lượng của
checkpoint cộng các lượt được giữ, nên lượt kế tiếp lại đến hạn) được tính như một lần từ
chối: nó đã trả phí và không đổi điều gì mà ngưỡng nhìn thấy được. Sau
`REJECTIONS_BEFORE_HOLD` (2) lần như vậy liên tiếp, bất kể mã nào, một lượt đến hạn sẽ không
hỏi cho tới khi view ước lượng đã lớn thêm `window / HOLD_GROWTH_DIVISOR` (một phần mười
context window của model) kể từ lần gần nhất; lần kế tiếp lại giữ từ kích thước mới, và một
bản tóm tắt (bắt buộc hay không) để view dưới ngưỡng xóa bộ đếm. Hai bản tóm tắt đầu của một
run như vậy vẫn được áp dụng: chỉ những lần hỏi sau đó phải chờ. Bản tóm tắt bắt buộc, theo sau một lần provider
từ chối, không bao giờ bị giữ. Bộ đếm được đọc lại từ các event `compaction` bền vững của run
khi resume (một event bị từ chối, hoặc một event được áp dụng mà `tokens_after` vẫn đến hạn), nên một lần crash không gỡ việc giữ. Bộ đếm là của riêng run: mỗi run mới trên
một session bắt đầu từ không. Biên là một phần mười window bất kể `threshold_tokens` (một
model mà provider báo window 0 có biên 1 token, tức không giữ gì). Hai lần hỏi là bằng chứng rằng model sẽ không
làm được trên prefix này, mỗi lần là một request đầy đủ prefix trả phí vô ích; một phần mười
window là vài lượt tăng trưởng, một prefix đã đổi đáng để thử lại.

Ở đầu một lượt, một lời gọi thất bại, trả lời không có văn bản, hoặc bị từ chối, không
gây lỗi nghiêm trọng. Lượt tiếp tục mà không compact, với lời gọi chính. Receipt và
`usage` được ghi ngay, cùng một checkpoint `CompactionPending` có tính đến chúng, vì một
response đã trả phí phải bền vững ngay khi nó tồn tại. Một lần resume từ checkpoint đó
thấy receipt đã kết thúc và đi thẳng tới lời gọi chính ở attempt 1, nên không bao giờ mua
lại bản tóm tắt (`journal/reconcile.rs:251-273`). Một lần hủy trong lúc gọi sẽ abort run.

Một bản tóm tắt bắt buộc mà không cho ra kết quả sẽ kết thúc run (bên dưới).

<a id="the-checkpoint"></a>

## Checkpoint

Message thay thế các lượt đã gộp là một checkpoint (`compaction::checkpoint`): block đầu
tiên là phần đầu của bản tóm tắt và bản tóm tắt của model; các block sau nó là các
message user của phần đã gộp, được harness sao chép nguyên văn (mỗi block bắt đầu bằng
`[user message, verbatim]`), mới nhất trước, tối đa `keep_user_tokens` (khoảng bốn ký tự
cho mỗi token), không bao giờ quá một nửa ước lượng của phần đã gộp, và không bao giờ quá
phần còn lại của phần đã gộp sau khi trừ phần đầu, văn bản của model và các block đuôi
(reminder của skill, thay đổi catalog được nhắc lại): các block nguyên văn và marker của
chúng co lại cho vừa. Quy tắc luôn đúng: toàn bộ view còn lại được ước lượng nhỏ hơn view trước đó,
nếu không bản tóm tắt bị từ chối với `not_smaller` (ở trên); nên một checkpoint không
bao giờ lớn hơn thứ nó thay thế, và với một window nhỏ hơn `keep_user_tokens` cộng output của
bản tóm tắt thì nó không bao giờ vượt window mà phần đã gộp vừa trong đó. Message không vừa
thì bị cắt với `[truncated]` (được tính vào phép vừa, không tính vào hai mức trần) và các
message cũ hơn bị bỏ.
Kết quả tool và các message `<system-reminder>` không phải lời của user nên không được
sao chép. Codex giữ tối đa 20k token message user gần đây và opencode 15k; model không cần
trích dẫn một chỉ dẫn để nó sống sót qua một bản tóm tắt.

Khi phần đã gộp đã có một checkpoint, chỉ dẫn tóm tắt yêu cầu cập nhật nó (lịch sử mới hơn
được ưu tiên, giữ các sự kiện vẫn còn quan trọng), và checkpoint mới lấy các block
nguyên văn của checkpoint cũ sau các message user mới hơn, trong cùng ngân sách. Văn bản
prompt tóm tắt không thuộc run contract. Các trường hợp request và checkpoint nằm trong
`crates/linch-core/tests/golden/compaction_request.json`, mà test của Linch bản Python
cũng đọc.

## Projection có ghi log

Một bản tóm tắt được chấp nhận trở thành một message của user và một view thay thế:

1. `summary_message` mở đầu văn bản bằng `<summary of earlier conversation>`, để model
   đọc nó như một bản tóm lược (`compaction.rs:106, 581-600`). Khi redaction che response,
   văn bản tóm tắt được che trước.
2. Khi skill được bật, message còn mang một `<system-reminder>` chứa nội dung của từng
   skill mà model đã nạp trong session, vì bản tóm tắt đã gộp các kết quả tool của chúng
   đi (`crates/linch-core/src/skills.rs`).
3. Nếu session có catalog, một thông báo nhắc lại mọi thay đổi catalog của session.
4. `replacement` là message đầu tiên như cũ, bản tóm tắt, rồi các message từ `keep_from`
   trở đi (`compaction.rs:692-704`).

Sau đó run stage event `compaction` (số message và token trước và sau, chiến lược, thời
lượng, `forced`) và lưu `Compacted` cùng bản tóm tắt và `keep_from`
(`runner/compact.rs:265-303`, `crates/linch-core/src/events/kinds/control.rs`).
`Compacted` ghi write-through cùng receipt và `usage`, nên một bản tóm tắt đã trả phí
không bao giờ bị mất.

Tiếp theo, `compacted` ghi projection vào session (`runner/compact.rs:309-324`).
`project` flush checkpoint đang giữ, nối một `LogEntry::Projection` vào session store
trong một lần ghi, ghi nó vào log trong bộ nhớ, rồi chạy các callback `on_compact` của
agent (`runner/write.rs:83-98`). Checkpoint `Compacted` đã bền vững, nên lần flush
không còn gì đang giữ để ghi. Lời gọi chính theo sau.

Một lượt có compact thêm hai lần ghi run và một lần ghi session: delivery cùng
`CompactionPending`, rồi write-through `Compacted`, rồi lần nối projection
(`runner/compact.rs:1-13`).

### `on_compact`

`AgentBuilder::on_compact` đăng ký một callback của host chạy sau mỗi projection
compaction, summary hay micro (`agent/builder.rs:135`). Nó không phải một hook: nó không
nằm trong run contract và không để lại bản ghi nào. Facade `linch` dùng nó để quên bằng
chứng đọc của session (`ReadTracker::forget`, `crates/linch/src/open.rs:218`), như
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
(`runner/turn.rs:591`, `runner/compact.rs:332-364`):

1. Với `micro`, nếu có kết quả tool cũ nào đáng xóa, `clear` ghi lại view đã xóa (với
   `forced` được đặt trên event) và chuyển sang attempt chính kế tiếp của lượt.
2. Nếu không, khi lượt cuối (từ message assistant cuối trở đi, phần duy nhất một bản tóm
   tắt không bao giờ gộp) nặng hơn ngân sách cắt, `clear` ghi lại view với kết quả tool của
   nó đã bị cắt, như một projection có ghi log (strategy `clip`, lý do `clip`, `Cleared`
   với ngân sách `clip`): một bản tóm tắt không giúp được một lượt vốn đã quá lớn. Ngân sách là một nửa cửa sổ tính bằng token ước lượng ở lần từ chối đầu
   của lượt, rồi một phần tư, một phần tám và một phần mười sáu ở các lần sau
   (`compaction::last_turn_budget`); quá đó thì không còn lần cắt nào.
3. Nếu không, chừng nào số bản tóm tắt bắt buộc đã tạo còn ít hơn `max_forced` và
   `forced_keep_from` tìm được một điểm để gộp, run tạo một bản tóm tắt với `redispatch`
   đặt thành attempt chính kế tiếp. `forced_keep_from` giữ `keep_recent_turns`, hoặc chỉ
   lượt cuối khi cách đó không gộp được gì, vì chính các lượt được giữ có thể là thứ
   không vừa (`compaction.rs:314-321`).
4. Nếu không, `overflow` không cho gì và lần từ chối kết thúc run, với thông báo nói run
   đo được gì (bên dưới).

**Request tóm tắt bắt buộc được cắt cho vừa.** Phần đã gộp là thứ duy nhất một bản tóm tắt
phải đọc, và nó có thể lớn hơn chính cửa sổ (một kết quả tool khổng lồ trong đó, hoặc
nhiều kết quả cỡ vừa trong một view chưa bao giờ chạm ngưỡng). Vì vậy một bản tóm tắt
bắt buộc đọc nó đã cắt (`compaction/clip.rs`): kết quả tool trước, rồi văn bản của bất kỳ
message nào, giữ đầu và đuôi quanh một dấu `[... N bytes omitted ...]`, cắt ở ranh giới
UTF-8 bằng một mức cắt chung cho mọi văn bản, mức lớn nhất đưa ước lượng xuống dưới ngân
sách (không message nào bị bỏ và mọi `tool_use_id` vẫn ghép cặp). Ngân sách của delivery
đầu là cửa sổ trừ `max_output_tokens` của bản tóm tắt, và một phần vừa dưới mức đó được
gửi nguyên, nên một bản tóm tắt bắt buộc của view đã vừa không đổi. Ước lượng đếm ít hơn
provider tới 2x, nên ngân sách không được tin: khi provider cũng từ chối request tóm tắt
vì quá dài cửa sổ, một event `compaction` ghi lại điều đó (`rejected` bắt đầu bằng
`context_length`, `forced`, `clipped_bytes`) bền cùng receipt, và delivery kế tiếp đọc
một nửa, tối đa bốn delivery (`MAX_SUMMARY_TRIES`; mỗi cái được trả tiền và ghi log như
mọi lời gọi provider, và chỉ tính một lần vào `max_forced`). Một request không thể cắt
thêm thì không được gửi lại. Không có gì bị cắt cho bản tóm tắt ở đầu lượt: request của nó
giữ nguyên các byte mà cache đang giữ (`tests/golden/compaction_proactive_request.json`).

Một bản tóm tắt bắt buộc mà thất bại vì lý do khác, trả lời không có văn bản, bị từ chối,
hoặc không tìm được chỗ để giữ sẽ kết thúc run qua `unshortened` (`runner/end.rs`): một
event `error` tên `ProviderError`, kind `context_length`, với thông báo "the prompt exceeds
the model's context window, and the compaction it forced could not shorten it", và một run
`failed`. Provider đã từ chối prompt mà view chưa compact sẽ gửi, nên tiếp tục chỉ mua lại
cùng một lần từ chối.

**Trường hợp kích thước duy nhất còn thất bại.** Khi không gì rút ngắn được view (request
tóm tắt bị từ chối ở mọi kích thước, hoặc không còn gì để gộp hay cắt, `max_forced` đã hết),
run kết thúc `failed` với cùng dạng `error` (`ProviderError`, `context_length`, `retryable`
false, `status` của provider được giữ) và một thông báo nêu cửa sổ cùng số token ước lượng
của system prompt, tool và phần hội thoại còn lại, rồi lời của chính provider: "the prompt
exceeds the model's context window of 8000 tokens and nothing is left to shorten: the system
prompt and tools take about 40 estimated tokens and the conversation 170, after summarizing
and clipping it as far as it goes. Shrink the system prompt or the tool set, or use a model
with a larger window (the provider said: ...)". Các kích thước là ước lượng của harness,
không phải số đếm của provider (`Runner::overflow_message`).

Lời gọi chính kế tiếp là attempt delivery kế tiếp của cùng lượt đó. Nó không phải một
retry của provider: không có event `provider_retry`, và số lần thử của retry policy không
bị tiêu. Mỗi bước lặp lại ở lần từ chối kế tiếp, nên một lượt có thể xóa, rồi tóm tắt,
rồi tóm tắt lần nữa chỉ giữ lượt cuối. `max_forced` giới hạn các bản tóm tắt của một run;
số điểm có thể gộp giới hạn một lượt.

### Một lần resume đọc lại gì

Không có gì được giữ trong bộ nhớ. `Forced::of` đọc nó từ các delivery và event bền vững
(`compaction.rs`): một delivery tóm tắt đến sau một delivery chính của cùng lượt là bắt
buộc; số lượng những cái đó là `max_forced` đã tiêu của run; số delivery chính và tóm tắt
của lượt cho ra các attempt kế tiếp; các delivery tóm tắt kể từ delivery chính cuối của
lượt là số lần thử của bản tóm tắt bắt buộc (nên delivery kế tiếp đọc ngân sách nào); và
một event `compaction` có `rejected` bắt đầu bằng `context_length`, không có delivery nào
sau nó, nói rằng bản tóm tắt cuối bị từ chối vì quá dài (`tries`, `refused`).
`runner/start.rs` đặt các bộ đếm của runner từ nó, nên một run được resume không quên một
bản tóm tắt bắt buộc, không dùng lại một delivery id, và một lần từ chối đã ghi trước khi
crash không bị nhầm với một loại lỗi khác. Các event từ chối đó bị loại khỏi việc giữ theo
số lần bị từ chối (`Rejections::of`): một request nhỏ hơn trả lời chúng, chúng không phải
một câu trả lời vô dụng. Một lần cắt được đọc lại từ ngân sách `clip` của checkpoint
`Cleared`, mà resume áp lại lên view (độ dài session của checkpoint chứng minh nó không
đổi).

### Khôi phục

`reconcile` ánh xạ các phase của compaction (`journal/reconcile.rs:251-284`):

| Checkpoint | Trạng thái tìm thấy | Run tiếp tục với |
|---|---|---|
| `CompactionPending` | Không có receipt cho delivery của bản tóm tắt | Một lời gọi tóm tắt khác, sau một receipt bị hủy cho lời gọi chưa kết thúc; một bản bắt buộc thì sau đó đi tiếp tới attempt chính của nó |
| `CompactionPending` | Có một receipt, bản tóm tắt ở đầu lượt | Lời gọi chính ở attempt 1; lời gọi tóm tắt đã kết thúc mà không có bản tóm tắt |
| `CompactionPending` | Có một receipt, bản tóm tắt bắt buộc bị từ chối vì quá dài (event `compaction` của nó đã bền), dưới 4 lần thử | Một lời gọi tóm tắt khác trên một nửa phần cũ, rồi attempt chính của nó |
| `CompactionPending` | Có một receipt, bất kỳ bản tóm tắt bắt buộc nào khác, hoặc đã hết lần thử | Run kết thúc qua `unshortened` |
| `Compacted` | Projection chưa có trong session | Nối nó, rồi lời gọi chính ở attempt kế tiếp của lượt |
| `Compacted` | Projection đã là entry cuối cùng | Lời gọi chính ở attempt kế tiếp của lượt |
| `Cleared` | Projection chưa có trong session | Xóa lại view từ `before`, nối nó, rồi attempt chính `attempt` |
| `Cleared` với `clip` | Projection chưa có trong session | Cắt lại view từ `before` tới ngân sách `clip`, nối nó, rồi attempt chính `attempt` |
| `Cleared` | Một projection ở cuối với lý do `micro-compaction` hoặc `clip` | Attempt chính `attempt` |

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
| Message đầu tiên | Message đầu tiên của view, không đổi (`compaction.rs:692-704`) |
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
(`runner/turn.rs:538-614`). Câu trả lời bị cắt được nối vào session như mọi response.
Văn bản cuối cùng của run là các phần, theo thứ tự, rồi đến văn bản của response cuối;
toàn bộ văn bản đó là thứ mà output schema, các verifier và `stop` nhìn thấy. Khi
redaction scrub response, văn bản ghép được scrub lại, vì một match bị cắt đôi qua hai
phần chỉ nguyên vẹn ở đó. Một
response gọi tool bắt đầu một chuỗi phần mới.

Không có gì mới được checkpoint. `Truncated::of` đọc lại số lần thử và các phần đang chờ
từ các event bền vững `assistant` và `user`: một câu trả lời bị cắt được tính khi phản
hồi của nó đã bền vững (`truncation.rs`, `runner/start.rs:537-539`).

## Test

- `crates/linch-core/tests/e2e_memory_off.rs`: một golden digest của các event và
  checkpoint của một run không có compaction, và của một run mà compaction không bao giờ
  kích hoạt, để một tính năng đang tắt không tốn gì.
- `crates/linch-core/tests/e2e_context_length.rs`: tóm tắt và gọi lại, micro trước, lần
  từ chối không thể rút ngắn, `max_forced`, một bản tóm tắt bắt buộc thất bại, redaction
  của một bản tóm tắt bắt buộc, và một lần crash ở mọi lần ghi của từng đường, được
  resume về cùng một kết quả.
- `crates/linch-core/tests/e2e_compaction_estimator.rs`: ngoài anchor, một kết quả khổng lồ
  trong phần đã gộp (request tóm tắt bị cắt và thử lại), trong lượt cuối (bị cắt trong
  view, còn nguyên trong session), một ngưỡng bằng cửa sổ, một bản tóm tắt bị từ chối ở mọi
  kích thước, một overhead tự nó đã vượt cửa sổ, một request tóm tắt ở đầu lượt được ghim
  từng byte (`tests/golden/compaction_proactive_request.json`), và một lần crash ở mọi lần
  ghi của từng cái, gồm cả việc giữ được đọc lại từ `measured_tokens`.
- `crates/linch-core/src/compaction/clip/tests.rs`: phép cắt (dấu, UTF-8, một mức cắt chung,
  mọi lời gọi vẫn ghép cặp, ngân sách như một hậu điều kiện).
- `crates/linch-core/tests/e2e_truncation.rs`: các câu trả lời được tiếp tục và ghép lại,
  phép kiểm tra schema trên toàn bộ văn bản, một lần crash ở mọi lần ghi.
- `crates/linch/tests/compaction.rs`: một lần compaction khiến model đọc lại một tệp
  trước khi sửa.
