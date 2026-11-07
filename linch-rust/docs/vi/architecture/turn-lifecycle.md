# Vòng đời của lượt

[Bản gốc tiếng Anh](../../architecture/turn-lifecycle.md)

> Thuộc [hướng dẫn kiến trúc linch-rust](./README.md).

Một run là một máy trạng thái trong `crates/linch-core/src/runner/`. Run là một
future mà bên gọi điều khiển bằng cách poll một `RunStream`. Mỗi lượt gửi đi một lời
gọi provider, nối response vào session và, khi response có lời gọi tool, lập kế hoạch
rồi chạy chúng. Trạng thái run được ghi vào run store theo nhóm, tại các barrier.
Dùng trang này để lần theo một run từ `Agent::run` đến terminal của nó, để đặt một
lần ghi mới so với một barrier, hoặc để xem một lần resume làm gì từ mỗi checkpoint.

Linch bản Python có cùng vòng lặp ([vòng đời của lượt](../../../../docs/architecture/turn-lifecycle.md)).
Điểm khác trong Rust:

- Vòng lặp được viết tường minh: một enum `Step`, mỗi variant ứng với một nơi mà run
  có thể resume (`crates/linch-core/src/runner/turn.rs:33-73`). Một lần resume đọc
  checkpoint bền vững và chọn bước; nó không phát lại vòng lặp.
- Run chỉ tiến lên khi stream của nó được poll. Drop stream là drop run ngay tại chỗ nó
  đang đứng, giống như một sự cố (`runner.rs:83-87`).
- Loop guard của Python (lời gọi lặp lại, lỗi lặp lại) không nằm trong runner. Runner
  chỉ phát `loop_guard` cho `max_turns` (`runner/end.rs:93-108`).
- Các hook point là bảy point trong [kernel](./kernel.md#chuỗi-hook). `AfterProviderCall`
  của Python đã bị bỏ (`hooks.rs:19-21`).
- Run của một subagent không thể suspend: một lời gọi cần phê duyệt sẽ bị từ chối
  (`runner/batch.rs:73-75`).

## Một lượt

`runner.rs:1-39` liệt kê các bước của lượt. Theo thứ tự:

1. **Compact** khi `spec.compaction` được đặt và provider view đến hạn
   ([thu gọn ngữ cảnh](./compaction.md)). Khi tắt, một lượt bắt đầu ở bước dispatch
   (`turn.rs:237-248`).
2. **Kiểm tra giới hạn.** Một token đã bị hủy sẽ abort run. `max_turns` và budget kết
   thúc run với trạng thái failed trước khi lời gọi được thực hiện (`turn.rs:271-284`).
3. **Dựng request** từ provider view của session. Các hook `context_build` chỉ thêm
   block vào phần đuôi của request (`turn.rs:285-295`, `runner/hooks.rs:117-129`).
   Chúng không bao giờ được ghi log.
4. **Stage và flush delivery.** Event `context_build` và delivery của lời gọi model được
   stage. Checkpoint `ProviderPending` được giữ lại, rồi `flush` ghi các event và
   checkpoint trong một lần commit (`turn.rs:299-305`). Đây là barrier trước khi gọi provider.
5. **Điều khiển lời gọi.** Các delta của stream được chuyển ngay cho bên gọi dưới dạng
   event chỉ có trong stream (`turn.rs:306-317`). Lời gọi kết thúc với một receipt, dù nó
   được chấp nhận, thất bại hay bị hủy (`provider/call.rs:1-9`, `43-55`).
6. **Ghi write-through response.** Khi thành công, run stage các event `Assistant` và
   `Usage` rồi lưu `AssistantReceived`, thứ được ghi write-through cùng receipt
   (`turn.rs:353-380`, `journal.rs:192-193`). Bên gọi chỉ thấy các event đó sau lần commit
   này (`journal.rs:25-28`).
7. **Nối response vào session** (`turn.rs:395-397`).
8. **Rẽ nhánh theo các lời gọi tool** của response (bên dưới).
9. **Nối các kết quả tool**, chạy `post_turn`, và bắt đầu lượt kế tiếp
   (`turn.rs:217-226`).

Một lời gọi thất bại kết thúc run với trạng thái failed bằng một lần ghi terminal duy nhất
mang theo receipt, usage, `Error`, trạng thái và `Result` (`turn.rs:337-347`). Một
response bị giới hạn đầu ra cắt trước khi có bất kỳ văn bản hay một lời gọi tool trọn vẹn
nào thì không phải là một câu trả lời: session không nhận gì, và run kết thúc theo cách
tương tự (`turn.rs:322-333`, `runner/end.rs:76-90, 130-139`).

### Vòng lặp tiếp tục khi response có lời gọi tool

`received` thu thập các block `tool_use` của response (`turn.rs:388-392`).

- **Có lời gọi tool**, run chuyển sang `Step::Batch` (`turn.rs:416-421`).
  Sau đó `Step::Results` nối các kết quả và bắt đầu lượt kế tiếp. `stop_reason` của
  provider không quyết định điều này; các block mới quyết định.
- **Không có**, trước hết run chờ mọi subagent chạy nền mà nó có, việc này có thể thêm
  một thông báo và một lượt nữa (`turn.rs:398-401`, `runner/inbox.rs:137-168`). Sau đó
  chuỗi hook `stop` chạy: một hook có thể giữ run tiếp tục bằng một phản hồi, được nối vào
  như một message của user và bắt đầu một lượt khác (`turn.rs:402-406`,
  `runner/hooks.rs:176-219`). Khi mọi hook cho phép dừng, `post_turn` chạy và run kết
  thúc với trạng thái `completed` (`turn.rs:407-414`).

`max_turns` là điều kiện dừng của vòng lặp: lời gọi sẽ vượt quá giới hạn này không được
thực hiện và run thất bại với `TurnLimitError` (`runner/end.rs:93-108`).

## Barrier và group commit

Một `Journal` giữ các event và checkpoint của một run trong bộ nhớ và ghi chúng bằng
`RunStore::commit` tại các barrier (`journal.rs:1-33`). Số lần ghi cho mỗi lượt và các
đường ghi Postgres nằm ở
[stores](./stores.md#số-lần-ghi-của-group-commit). Phần này nói về thời điểm một lần ghi diễn ra.

`Journal::save` áp dụng quy tắc theo phase (`journal.rs:190-208`):

| Phase | Ghi | Trạng thái |
|---|---|---|
| `Started`, `AssistantReceived`, `Compacted` | Write-through, ngay lập tức | không đổi |
| `AwaitingApproval` | Write-through, ngay lập tức | `awaiting_approval` |
| `ProviderPending`, `CompactionPending`, `ToolBatch`, `ToolResults` | Giữ lại | không đổi |

Một checkpoint được giữ lại là một snapshot: nó chỉ bao phủ các event đang chờ khi nó
được lưu (`journal.rs:76-80`, `201-202`). Mọi trạng thái đã commit đều là trạng thái mà
việc ghi write-through từng bước cũng sẽ tạo ra (`journal.rs:25-28`).

Bên gọi đặt các barrier, và run làm điều đó ở những chỗ sau:

| Barrier | Lời gọi | Vị trí |
|---|---|---|
| Trước khi gửi tới provider | `Journal::flush` | `turn.rs:303-305` |
| Quanh lời gọi tóm tắt của compaction | `flush` trước lời gọi; `flush` hoặc write-through `Compacted` sau lời gọi | `runner/compact.rs:94-96, 116-118, 143-147` |
| Trước mỗi tool thực sự chạy | `ToolSink::tool_starting` flush | `scheduler/execute.rs:39-45`, `runner/sink.rs:137-140` |
| Sau mỗi tool thực sự đã chạy | `tool_finished` flush | `sink.rs:142-182` |
| Trước mọi lần ghi session store | `Journal::flush_checkpoint` | `runner/write.rs:96-103`, `journal.rs:220-231` |
| Kết thúc run | `Journal::finish` | `journal.rs:233-257`, `runner/end.rs:55` |

Đây là quy tắc rằng trạng thái run phải bền vững trước tác dụng phụ kế tiếp: provider
được gọi, một tool bắt đầu, session store được ghi, hoặc run kết thúc chỉ sau khi mọi
event và checkpoint đang chờ đã bền vững. Đừng bao giờ hoãn một lần ghi qua một barrier.
Ba loại ghi không chờ barrier kế tiếp:

- Một receipt đã trả phí. Một lời gọi được chấp nhận ghi write-through cùng
  `AssistantReceived` (`turn.rs:370-374`). Một bản tóm tắt compaction ghi write-through
  cùng `Compacted` (`compact.rs:134-147`). Một lời gọi tóm tắt không trả về văn bản sẽ
  lưu `CompactionPending` và flush ngay, nên một lần resume không mua lại nó
  (`compact.rs:20-24, 114-120`).
- Phần kết thúc của một tool. Nó được flush ngay sau khi tool trả về, từ
  `tool_finished`, nên một lần resume tìm thấy kết quả và không chạy lại tool
  (`sink.rs:179-181`, `journal/reconcile.rs:316-320`).
- Một lần suspend. `AwaitingApproval` ghi write-through cùng trạng thái của nó
  (`journal.rs:195`).

Một lời gọi không chạy tool nào (được hook phục vụ, bị từ chối, bị deny hoặc bị abort)
có phần bắt đầu và kết thúc được stage mà không flush. Chúng đến store cùng barrier kế tiếp
(`sink.rs:3-7`, `179-181`). Hook không phải là barrier
(`runner/hooks.rs:1-9`).

`Journal::finish` commit trạng thái đang giữ trước, rồi đến trạng thái terminal cùng
event `Result`. Khi không có gì đang giữ, các event đang chờ, `Result` và trạng thái được
ghi trong một lần ghi, nên một sự cố không bao giờ để lại một receipt thất bại cùng
`Error` của nó mà trạng thái lại là `running` (`journal.rs:233-257`).

Một lần ghi thất bại làm journal bị nhiễm lỗi (poison): mọi lần ghi sau đó đều thất bại
với `JournalError::Poisoned`, và không còn gì đến được store. Run giữ trạng thái bền vững
cuối cùng của nó và một lần resume sẽ khôi phục từ đó (`journal.rs:30-32`,
`285-295`).

Bên gọi chỉ thấy một event sau khi nó được commit. `Outbox::deliver` xếp hàng các
envelope mà một lần commit đã làm bền vững và yield một lần, để stream chuyển chúng đi
trước khi run tiếp tục (`runner.rs:278-298`). Các delta và tiến độ của tool bỏ qua bước này:
chúng đi ra ngay và mang theo seq bền vững gần nhất (`runner.rs:74-80`,
`runner/sink.rs:184-199`).

## Batch tool

`Step::Batch` lập kế hoạch, rồi suspend hoặc chạy (`runner/batch.rs:29-118`).

1. **Plan.** `scheduler::plan` quyết định mọi lời gọi mà không chạy gì, dưới cancel token
   của run (`batch.rs:58-72`). Các hook record của từng lời gọi được chuyển cho bên gọi
   (`batch.rs:77-80`). Các quyết định được ghi lại cho checkpoint (`batch.rs:81-83`).
   Các bước của `plan` nằm ở
   [kernel](./kernel.md#các-seam-của-lời-gọi-tool).
2. **Suspend hoặc chạy.** Nếu có lời gọi nào chờ phê duyệt, run suspend và batch không
   được thực thi (`batch.rs:84-87`). Nếu không, nó lưu `ToolBatch` (được giữ lại, với
   `after_seq` = seq đã stage cuối cùng) và thực thi (`batch.rs:88-94`).
3. **Thu thập.** Các kết quả, mọi ghi chú từ peer và thông báo catalog trở thành một
   message của user, được stage dưới dạng event `user` với subtype `tool_result`. Run lưu
   `ToolResults` (được giữ lại). Sau đó `Step::Results` nối message đó, việc này trước
   hết flush checkpoint ấy (`batch.rs:99-118`, `turn.rs:217-226`).

### Chỉ công việc đọc mới chạy song song

`execute` gom các lời gọi thành nhóm (`scheduler/execute.rs:137-151`). Một lời gọi chỉ
vào một nhóm nếu lời gọi đầu tiên của nhóm là lời gọi đọc, nhóm còn dưới
`max_tool_concurrency`, và lời gọi đó cũng là lời gọi đọc, hoặc không chạy tool nào
(`execute.rs:155-163`). Mọi lời gọi khác chạy riêng. `try_join_all` chạy các lời gọi của
một nhóm cùng nhau, và các kết quả được trả về theo thứ tự lời gọi.

Một lời gọi đọc là lời gọi mà `Tool::parallel(input)` trả về true. Mặc định chỉ true với
scope `Read` (`crates/linch-core/src/tool.rs:34-36, 250-257`). Tool `Subagent` có scope
`Exec` và chỉ trả về true cho một lời gọi foreground của một loại có tên mà định nghĩa
liệt kê các tool của nó và mọi tool trong đó đều là tool `Read` đã đăng ký. Mọi thứ không
xác định đều fail closed
(`crates/linch-core/src/subagents/tool.rs:124-146`). Giới hạn mặc định là 4,
và 0 được tính là 1 (`agent/spec.rs:54-56`, `execute.rs:137`).

Các lời gọi trong cùng một nhóm chồng lấn nhau, nên `ToolSink` phải chấp nhận các lời gọi
đồng thời (`execute.rs:33`). Sink của journal lần lượt từng lượt qua một mutex được giữ
trong suốt mỗi lần flush (`sink.rs:38-40`). Một lỗi của sink dừng batch: tool này và mọi
tool sau đó không chạy, và các tool đang chạy trong nhóm bị hủy
(`execute.rs:39-45`).

## Suspend và resume

### Suspend

Một lời gọi vẫn cần phê duyệt sau chuỗi `permission_request` sẽ làm run suspend. `suspend`
stage một event `permission_request` và một event `suspended`, park subagent family
(nếu agent có subagent) để các run con của nó sống lâu hơn stream, và lưu
`AwaitingApproval` cùng các id đang chờ (`batch.rs:147-183`). Lần ghi write-through đó đặt
trạng thái `awaiting_approval` (`journal.rs:195`). Stream kết thúc bằng `Suspended` thay
vì `Result`. Sau đó run chờ trong store với trạng thái `awaiting_approval`; chỉ một lần
claim mới đưa nó về `running`
([stores](./stores.md#run-store)). Một lần ghi thất bại sẽ lấy lại phần park
(`batch.rs:177-182`).

### Resume và approve

`Agent::resume` tiếp tục một run sau sự cố, mất lease hoặc stream bị drop.
`Agent::approve` trả lời các lời gọi đang chờ của một run đã suspend và tiếp tục nó
(`agent.rs:253-288`). Cả hai đều vào `continue_run`
(`runner/start.rs:175-333`):

1. Một token đã bị hủy từ trước thì không làm gì: không claim, không ghi
   (`start.rs:185-187`).
2. `check` từ chối một run đã ở terminal, một run thuộc contract khác, và một approve
   trên run không ở trạng thái chờ phê duyệt. Nó chạy trước claim, để một lời gọi nhầm
   không fence một owner đang sống, và chạy lại sau claim
   (`start.rs:189-207`, `414-439`).
3. `claim` lấy lease và cộng 1 vào epoch, thứ fence owner trước đó
   ([stores](./stores.md#fencing)) (`start.rs:196-205`).
4. `reconcile` đọc checkpoint, các event và session log, rồi trả về việc cần làm tiếp
   theo. Nó là hàm thuần (`journal/reconcile.rs:149-169`).
5. Một event `init` mới với `resumed: true` được stage. Nó đi cùng barrier đầu tiên
   (`start.rs:284-296`).
6. Với approve, các câu trả lời của host được ghi lại thành các quyết định và commit
   cùng một checkpoint `ToolBatch` trước khi run tiếp tục, nên một sự cố sau đó vẫn giữ
   được chúng (`start.rs:259-261, 329-331`, `batch.rs:199-214`).

Những gì `reconcile` làm với từng phase (`journal/reconcile.rs:170-231`):

| Phase của checkpoint | Khôi phục | Run tiếp tục với |
|---|---|---|
| `Started` | `Started` | Nối prompt trừ khi nó đã có trong session, rồi đến lượt |
| `ProviderPending` | `Redispatch`, hoặc `Received` với một receipt đã được chấp nhận | Một receipt bị hủy cho delivery không có receipt, rồi attempt + 1; hoặc response bền vững |
| `AssistantReceived` | `Received` | Nối response trừ khi nó đã có trong session, rồi rẽ nhánh theo các lời gọi tool của nó |
| `ToolBatch` | `ToolBatch` | Với mỗi lời gọi: kết quả bền vững của nó, một placeholder, hoặc một lần plan lại |
| `AwaitingApproval` | `AwaitingApproval` | Plan lại với các quyết định đã ghi; suspend lần nữa nếu còn lời gọi cần câu trả lời |
| `ToolResults` | `ToolResults` | Nối message trừ khi nó đã có trong session, rồi đến lượt kế tiếp |
| `CompactionPending` | `Recompact` hoặc `Redispatch` | Một lần tóm tắt khác, hoặc lời gọi chính khi lời gọi tóm tắt đã kết thúc mà không có bản tóm tắt |
| `Compacted` | `Compacted` | Nối projection trừ khi nó đã có trong session, rồi dispatch |

Mỗi lời gọi tool của một batch được khôi phục sẽ tự khôi phục riêng
(`reconcile.rs:309-332`):

- Một lời gọi có phần kết thúc bền vững thì dùng lại kết quả đó (`Done`).
- Một lời gọi đã bắt đầu nhưng không có phần kết thúc nhận một kết quả lỗi nói rằng nó
  bị gián đoạn, và tool không được chạy lại (`Interrupted`,
  `reconcile.rs:20-22`). Run stage placeholder đó làm phần kết thúc của lời gọi
  (`batch.rs:44-57`). Một lời gọi `Subagent` hoặc `SubagentContinue` là ngoại lệ: nó được
  plan lại và resume run con bền vững của nó (`batch.rs:120-141`).
- Một lời gọi chưa từng bắt đầu được plan lại, và xin quyền lại trừ khi một quyết định
  đã ghi bao phủ nó (`Replan`).

Một allow đã ghi chỉ bao phủ input mà host đã thấy, và một deny đã ghi luôn từ chối
(`scheduler.rs:59-66`). Reconcile thất bại khi session log không khớp với checkpoint
(`journal/reconcile/session.rs:9-22`).

Một approve được thử lại nhận `NotSuspended`: khi một approve đã commit các câu trả lời
của nó thì run ở `ToolBatch`, và host tiếp tục nó bằng `resume`
(`agent.rs:262-273`, `start.rs:432-437`).

## Các terminal

Mọi terminal đều đi qua `Runner::end`: nó hủy và await các run con của run, stage trạng
thái cuối của chúng, dựng `Result` và gọi `Journal::finish` (`runner/end.rs:24-58`).

| Terminal | Trạng thái | Khi nào |
|---|---|---|
| `Result` success | `completed` | Một response chỉ có văn bản, sau `stop` và `post_turn` (`turn.rs:407-414`) |
| Abort | `aborted` | Cancel token đã kích hoạt (`end.rs:61-64`) |
| Hook fail closed | `failed` | Một hook `stop` fail closed (`end.rs:66-74`) |
| Giới hạn đầu ra | `failed` | Response không có văn bản hoặc lời gọi tool trọn vẹn (`end.rs:76-90`) |
| Giới hạn lượt | `failed` | Đạt `max_turns`, kèm một event `loop_guard` (`end.rs:92-108`) |
| Budget | `failed` | Budget không cho phép thêm lời gọi nào (`end.rs:110-122`) |
| Lỗi provider | `failed` | Lời gọi thất bại (`turn.rs:343-346`) |

Một trạng thái terminal là cuối cùng trong store ([stores](./stores.md#run-store)).
`Agent::fail` đánh dấu một run không bao giờ có thể tiếp tục (contract không khớp, một
trạng thái không reconcile được) là `failed` (`agent.rs:290-327`).

## Ví dụ về trace

Một lượt với một tool ghi, `k` = 1, tốn `3 + 2k` = 5 lần ghi run và 2
lần ghi session (`runner.rs:36-37`):

| Bước | Ghi |
|---|---|
| Dispatch | Run: delivery, `context_build` và `ProviderPending` được flush (1) |
| Response | Run: receipt, `Assistant`, `Usage` và `AssistantReceived` write-through (2) |
| Nối response | Session (1) |
| Tool bắt đầu | Run: `ToolBatch` và `tool_call_start` được flush (3) |
| Tool kết thúc | Run: `tool_call_end` được flush (4) |
| Kết quả | Run: `ToolResults` được flush trước lần ghi session (5); session (2) |

Bảng này là hình dạng được cố định bởi
`a_tool_turn_costs_three_plus_two_k_run_writes_and_two_session_writes`
(`journal/tests/turn.rs:116`).
