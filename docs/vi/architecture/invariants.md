# Các bất biến cốt lõi

[Bản gốc tiếng Anh](../../architecture/invariants.md)

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Dùng trang này khi bạn thay đổi hành vi runtime có ảnh hưởng xuyên suốt nhiều thành
phần và cần biết các quy tắc về session, vòng lặp, lưu trữ bền vững, bảo mật hoặc bề
mặt API công khai phải luôn được giữ đúng.

Các bất biến sau phải được giữ vững qua mọi lần tái cấu trúc:

| # | Bất biến |
|---|---|
| 1 | **Một nhật ký session chỉ cho phép ghi thêm; trạng thái do session sở hữu mà mô hình nhìn thấy đều được ghi vào nhật ký** — `Session` sở hữu một `SessionLog` duy nhất (`session_log.py`). `provider_view` (mô hình nhìn thấy) và `full_history` (phục vụ kiểm toán) là **các bản chiếu dẫn xuất, chỉ đọc và được sao chép sâu**; đầu ra `ContextInjectionHook`/RAG của từng yêu cầu chỉ được tập hợp vào `ProviderRequest` và được chủ ý đặt ngoài phạm vi bất biến này. Không bao giờ dùng `append`/`extend`/`clear`/`[:]=` trên các bản chiếu. Bổ sung lịch sử qua `await session.append([message])` (tham số là `messages: list[Message]`), con đường duy nhất để lưu bền vững (ghi vào kho session trước rồi mới ghi vào nhật ký); gọi trực tiếp `SessionLog.append(...)` chỉ tác động trong bộ nhớ. |
| 2 | **Compaction là một *bản chiếu được ghi vào nhật ký*, không phải phép sửa đổi tại chỗ** — nó gọi `session.session_log.record_projection(...)`, thêm một `ProjectionEntry` và viết lại bản chiếu provider dẫn xuất; `full_history` không bị thay đổi. |
| 3 | **Giao thức tool dùng duck typing** — không có lớp cơ sở, không dùng `isinstance`; kiểm tra sự hiện diện của thuộc tính. |
| 4 | **`stream()` sinh ra các dict đã chuẩn hóa** — vòng lặp không được import các kiểu dữ liệu thô của bất kỳ provider nào. |
| 5 | **Prompt của SDK thuần mang tính trung lập và mô tả đúng các tool** — `Agent()` không ngầm mang vai trò lập trình hay có sẵn các tool cho workspace. Các nguyên tắc dành cho lập trình thuộc về những preset workspace/deep-agent được chọn tường minh, và mọi mệnh đề trong prompt chỉ được mô tả các tool được cung cấp cho yêu cầu đó. |
| 6 | **Tool `final_tool_name` không bao giờ được lập lịch** — vòng lặp chặn nó trước khi tới bộ lập lịch. |
| 7 | **Các bộ dựng ngữ cảnh không sửa đổi lịch sử** — `ContextInjectionHook` cung cấp `ContextBuildTurn.messages` dưới dạng một snapshot `provider_view` tách rời và trả về ngữ cảnh yêu cầu tạm thời; bộ dựng không ghi thêm vào bất kỳ bản chiếu bền vững nào trong hai bản chiếu. |
| 8 | **`run_deps` được gán một lần cho mỗi lần gọi `run_loop`** — ngay ở đầu, lấy từ `opts.deps ?? agent.deps`. |
| 9 | **Loop guard được bật theo mặc định** — `Agent()` không truyền `loop_guard=` sẽ nhận `LoopGuard()` với các ngưỡng an toàn; tắt một cách tường minh bằng `Agent(loop_guard=None)`. |
| 10 | **Các khả năng của provider được áp dụng cho từng yêu cầu** — `loop/request.py` luôn gọi `apply_provider_capabilities()` khi provider có `capabilities()`; không provider nào nhận những tính năng mà chính nó đã khai báo là không hỗ trợ. |
| 11 | **Filesystem/offload phải được bật tường minh** — một `Agent` thuần có `FeatureFlags(filesystem=False)`, không có các tool hệ thống tệp ảo và không có đường thực thi offload. Phải có `FeatureFlags(filesystem=True)` thì cấu hình backend/offload mới có thể tác động tới một run. |
| 12 | **Khi được bật, offload chỉ thay thế `ToolResult.content` trước khi dựng block** — kết quả đầy đủ được giữ lại tại `ToolCallEndEvent.tool_result`; `full_history` và `provider_view` chỉ nhận bản xem trước. `maybe_offload` không bao giờ ném ngoại lệ — nếu ghi vào backend thất bại, nó âm thầm trả lại kết quả gốc để một trục trặc lưu trữ không bao giờ làm hỏng run. |
| 13 | **Các tool hệ thống tệp được loại khỏi phạm vi offload khi tính năng này được bật** — `read_file`, `write_file`, `edit_file`, `ls` nằm trong `OffloadConfig.skip_tools` theo mặc định; đọc lại một tệp lớn không thể kích hoạt việc offload lại theo kiểu đệ quy. |
| 14 | **Các tác vụ nền bị hủy khi abort/close, không phải khi hoàn tất bình thường** — các tác vụ tool tách rời đã hoàn tất tự xóa mình khỏi `session.background_tasks`; close vẫn hủy và await các tác vụ còn dang dở. Một tool chạy nền hoàn tất bình thường sẽ đưa thông báo của nó vào hàng đợi trước khi ghi nhận kiểm toán về việc hoàn tất. Mặc định, việc chuyển thông báo diễn ra trong cùng tiến trình; với `DurabilityOptions(durable_inbox=True)`, thông báo hoàn tất đã được đưa vào hàng đợi sẽ tồn tại qua lần khởi động lại. Bản thân tác vụ đang thực thi không được khôi phục. |
| 15 | **Việc giữ lại subagent được cấu hình tường minh** — `SubagentTool` dùng thiết lập `retain_subagents` của agent cho các worker chạy tiền cảnh; worker chạy nền bắt buộc được giữ lại để vẫn có thể được truy cập theo định danh. Các session con được giữ lại vẫn nằm trong `agent._sessions` cho tới khi được giải phóng tường minh hoặc bị `agent.close()` xóa. Tùy chọn `max_idle_sessions` có thể giải phóng các session con nhàn rỗi; các run đang hoạt động, công việc nền đang chờ và các session cha sở hữu chúng được bảo vệ. `continue_subagent` yêu cầu session con vẫn còn hoạt động. |
| 16 | **Các thông báo dành cho lượt kế tiếp được lấy hết ra trước `ContextInjectionHook.build_context()`** — chế độ cũ lấy hết từ `session.pending_notifications`; chế độ bền vững commit hộp thư đến của session một cách nguyên tử. Việc sửa đổi trực tiếp hàng đợi cũ bị từ chối trong chế độ bền vững. Một lần resume từ trạng thái `provider_pending` đã đóng băng vẫn thuộc cùng lượt, nên việc chuyển thông báo mới phải chờ tới lượt sau. |
| 17 | **Một đối tượng `RunBudget` cho mỗi cây agent** — các agent con kế thừa `active_budget` của cha theo tham chiếu trong `run_subagent`; sổ theo dõi các lần gọi mô hình trong phạm vi run tính phí vào đối tượng đó đúng một lần cho mỗi biên nhận provider đã được quyết toán, và vòng lặp chiếu các biên nhận đó thành `UsageEvent`. Không bao giờ sao chép budget sang agent con. |
| 18 | **`micro_compact` dùng cơ chế copy-on-write** — các lần đọc provider-view và full-history trả về snapshot được sao chép sâu; việc lược bỏ phải dựng các đối tượng `Message`/`ToolResultBlock` mới, không bao giờ sửa đổi tại chỗ, và kết quả của nó được áp dụng qua `record_projection` (một bản chiếu được ghi vào nhật ký), thay vì sửa đổi danh sách bản chiếu. Sau khi vòng lặp áp dụng thành công compaction provider-view chủ động hoặc phản ứng, vòng lặp lưu bền vững một snapshot thay thế nếu kho lưu trữ hỗ trợ snapshot và mốc số thứ tự của session vẫn cho phép lưu cache. |
| 19 | **Các bản ghi nhật ký workflow là những `WorkflowEvent` chỉ cho phép ghi thêm trong kho run** — tính đúng đắn của việc phát lại phụ thuộc vào việc lưu bền vững các event `agent_end`/`agent_replayed` (cho `wf.agent`), `step_end`/`step_replayed` (cho `wf.step`) và `interrupt_resolved`/`interrupt_replayed` (cho `wf.interrupt`) với mọi lần gọi đã hoàn tất; không phát chúng cho một lần gọi thất bại *hoặc một lần thử lại thất bại* — một nút đã hết số lần thử phải chạy lại khi resume, không được phát lại một thất bại. Tập con được ghi vào nhật ký là `JOURNALED_KINDS` trong `ext/workflow/journal.py`; giữ nó đồng bộ chặt chẽ với `WORKFLOW_EVENT_KINDS` trong `events.py`. Snapshot nhật ký trong `RunCheckpoint.extension_state["linch.workflow"]` chỉ là phương tiện tăng tốc: nó phải luôn là tùy chọn, và khi thiếu hoặc sai định dạng thì phải quay về tổng hợp toàn bộ nhật ký event. |
| 20 | **Trạng thái run được lưu bền vững yêu cầu wire 3 và một hợp đồng đã được xác minh** — mọi envelope dùng chung phiên bản hiện tại được phản ánh bởi `linch.RUN_SCHEMA_VERSION`; các trường hợp thiếu phiên bản, phiên bản cũ/không xác định, thiếu trường checkpoint, thiếu hợp đồng và không khớp đều bị từ chối theo nguyên tắc fail-closed. Các event bắt buộc sai định dạng hoặc không xác định đều gây lỗi; chỉ các envelope không xác định nhưng hợp lệ và được đánh dấu tường minh `ignorable: true` mới được đọc rồi ghi lại dưới dạng `IgnorableEvent`. Bắt đầu run mới thay vì resume trạng thái trước 3.0. |
| 21 | **Bề mặt API công khai chính xác là `linch.__all__`** — `tests/package/test_public_api.py` bảo đảm mọi tên đều phân giải được, không có tên trùng lặp và không có thuộc tính công khai (không bắt đầu bằng dấu gạch dưới) nào bị lộ ra trên package mà không được khai báo. Thay đổi bề mặt này phải là một chỉnh sửa có chủ đích và có thể rà soát; `docs/versioning.md` là hợp đồng semver. |
| 22 | **Luồng event là một async generator thông thường** — `session.run()`/`resume()` trực tiếp `yield` các event từ `run_loop`; không có hàng đợi nội bộ không giới hạn. Tiến độ của từng tool là một bản chiếu luồng có giới hạn và được gộp lại, không bao giờ là ngữ cảnh provider hay trạng thái kết thúc. |
| 23 | **Không có trạng thái khả biến toàn cục trong tiến trình** — mỗi `Agent` tự dựng các registry/store/engine riêng để N agent được cách ly đa tenant; một agent thuần dùng kho session trong bộ nhớ và không tạo trạng thái hệ thống tệp cục bộ trong dự án. `session.abort()` và `agent.close()` xử lý hết các tác vụ worker nền và tool nền. |
| 24 | **Các quyết định về quyền chỉ cho phép những lần gọi đã được chuẩn hóa và được cung cấp** — phân giải/xác thực → ranh giới tool được cung cấp → `PreToolUse` → xác thực lại → quyền cuối cùng → thực thi. Những lần gọi không được cung cấp không bao giờ tới được hook hay cơ chế dự phòng của chính sách. Các phê duyệt được lưu bền vững dùng đầu vào chuẩn hóa cuối cùng làm khóa và không thể cho phép một lần gọi đã bị biến đổi hoặc không được cung cấp. |
| 25 | **Đầu vào chính xác của mô hình phải được bật tường minh và tuân theo nguyên tắc fail-closed** — mỗi lần thử gọi provider khi tính năng này được bật đều chụp snapshot của `ProviderRequest` có hiệu lực cuối cùng trước khi gửi đi. Một lần resume đang chờ dùng snapshot đó mà không chạy lại các hook ngữ cảnh hay hook sửa đổi yêu cầu; snapshot bị thiếu, hỏng hoặc không được hỗ trợ không bao giờ dẫn tới việc dùng một yêu cầu được dựng lại làm phương án dự phòng. |
| 26 | **Tool Output V2 được bật tường minh cho từng tool** — chỉ tool khai báo `output_schema` mới đi qua bước chuẩn hóa JSON chính tắc, xác thực Draft 2020-12 và kết xuất. Các tool cũ cùng các bản chiếu event/provider của chúng giữ nguyên hành vi hiện có. |

## Cơ sở thiết kế

Các bất biến này được ghi lại có chủ đích (và một số được cố định bằng kiểm thử):

- **Bất biến là hợp đồng, không phải quy ước truyền miệng.** Nhiều bất biến xuất hiện
  lặp lại dưới dạng những giả định xuyên suốt mà vòng lặp, compaction, subagent và
  resume đều phụ thuộc — liệt kê chúng giúp bạn đối chiếu một lần tái cấu trúc với
  danh sách tường minh, thay vì phải khám phá lại từng quy tắc bằng cách phá vỡ nó.
- **Các bất biến nền tảng được cố định bằng kiểm thử tập trung.** System prompt mặc
  định và tính trung thực trong mô tả tool (#5), các quy tắc về bản chiếu/thử lại
  (#18) và API công khai (#21) đều có kiểm thử chuyên biệt. Vì vậy, sai lệch vô ý sẽ
  khiến CI thất bại và đòi hỏi việc chỉnh sửa kèm cập nhật có chủ đích.

---

Quay lại [mục lục kiến trúc](./README.md).
