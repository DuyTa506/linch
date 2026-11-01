# Linch 3.0: reliability mặc định cho mọi provider

Source: finalized reliability design, captured and cross-reviewed on 2026-10-30.
Coordination note: the original plan's no-tag/no-publish restriction applies to that planning session. Feature implementation and reviewed release integration are authorized separately by this thread's active user goal.

## 1. Mục tiêu và phối hợp

Bổ sung reliability vào core để Linch tự phục hồi những lỗi thực thi phổ biến, có giới hạn và quan sát được. Áp dụng thống nhất cho mọi provider; giữ tính trung lập của SDK và quyền quyết định nghiệp vụ ở ứng dụng.

- Không thêm dependency Forge, dò VRAM hay quản lý inference server.
- Giữ nguyên cơ chế compaction và mặc định CompactionLadder hiện tại; đưa chúng vào kiểm thử tương tác.
- Session “Đọc OpenHuman, so sánh Linch” đã được thông báo và xác nhận phạm vi. Bên đó phụ trách nghiên cứu discovery, compression, cache và memory; reliability thuộc plan này.
- Không sửa openhuman/, plans/, hoặc thay đổi đang có trong docs/ROADMAP.md. Trước triển khai, đối chiếu lại thay đổi từ session kia tại request, scheduler và run contract.
- Nhắm tới 3.0, ghi rõ breaking defaults và hướng dẫn migration; không tag hoặc publish package trong công việc này.

## 2. API và mặc định

Thêm ba dataclass frozen=True, slots=True, xuất qua linch.__all__:

```python
ToolPrerequisite(tool_name: str, requires_success: tuple[str, ...])
CompletionGate(required_successful_tools: tuple[str, ...])
ReliabilityPolicy(
    max_correction_turns: int = 2,
    retry_invalid_responses: bool = True,
    rescue_text_tool_calls: bool = True,
    inject_recovery_hints: bool = True,
    prerequisites: tuple[ToolPrerequisite, ...] = (),
    completion: CompletionGate | None = None,
)
```

- Thêm reliability vào Agent và AgentOptions. Bỏ qua tham số thì tạo policy mặc định; None tắt subsystem.
- Các boolean cho phép ablation từng cơ chế. Dùng prerequisites=() hoặc completion=None để bỏ điều kiện tương ứng.
- Bản đầu cấu hình ở cấp Agent; trạng thái và ngân sách correction thuộc từng run, không chia sẻ giữa session hoặc kế thừa ledger sang subagent.
- max_correction_turns là số nguyên không âm, tính tổng trong run và không reset khi có tiến triển. Giá trị 0 từ chối ngay trường hợp cần correction.
- Kiểm tra tên canonical, cạnh trùng, tự phụ thuộc và chu trình. Kiểm tra sự tồn tại của tool sau bước khởi tạo/load tools, trước lần gọi provider đầu tiên; không chấp nhận terminal tool làm bằng chứng hoàn thành.

Ý nghĩa của bước thành công: một foreground tool đã trả kết quả chuẩn hóa thành công sau hooks trong run hiện tại. Lỗi, permission denial, interrupted call và background ACK không được tính.

Ledger được chụp trước mỗi batch: hai call trong cùng batch không thỏa prerequisite cho nhau, kể cả scheduler chạy tuần tự. Bản đầu chỉ kiểm tra theo tên tool; điều kiện theo tài nguyên, argument hoặc dữ liệu nghiệp vụ tiếp tục thuộc ứng dụng.

## 3. Thay đổi runtime

Tách cấu hình công khai tại src/linch/reliability.py và logic riêng tại src/linch/loop/reliability.py; tích hợp tại các chokepoint hiện có.

### Phục hồi response không hợp lệ

- Correction áp dụng cho end_turn rỗng, envelope gọi tool rõ ràng nhưng không hợp lệ, và native call có arguments không parse được hoặc không phải JSON object.
- Với malformed native arguments, tiếp tục đọc tới message_end để lấy usage và kiểm tra cấu trúc stream. Sau đó từ chối cả response batch trước khi chạy bất kỳ tool nào, kể cả sibling hợp lệ.
- Ghi một assistant diagnostic cố định, được đánh dấu do Linch tạo, rồi feedback yêu cầu gửi lại và nói rõ batch chưa thực thi. Không lưu raw arguments hoặc provider metadata của response bị từ chối.
- Sai thứ tự event, ID không hợp lệ, thiếu message_end, usage sai kiểu và lỗi transport vẫn đi theo contract lỗi provider hiện có.
- Không dùng sentinel trong input hay tạo tool call giả với {}.

### Rescue parsing bảo thủ

Chỉ nhận toàn bộ phần text của một end_turn theo đúng dạng:

```text
<tool_call>{"name":"ToolName","arguments":{...}}</tool_call>
```

- Cho phép whitespace ngoài envelope; tối đa 64 KiB UTF-8; strict JSON, đúng hai khóa, một call duy nhất.
- Không quét prose, code fence hoặc thinking; không đoán/sửa tên và arguments.
- Chỉ nhận tool thực sự được cung cấp trong request cuối cùng sau request hooks.
- Bỏ qua khi có structured output, final_tool_name, tool_choice="none" hoặc force-final.
- Call được cứu đi qua validation, hooks, prerequisites và permissions thông thường. Rescue thành công không tiêu tốn correction turn.

### Error feedback và điều kiện thực thi

- Dùng ToolResult.is_error và recovery_hint hiện có; không suy luận lỗi bằng regex trên nội dung tool.
- Đưa hint cho từng lỗi, kể cả batch thành công một phần, sau các ToolResultBlock trong cùng message kết quả. Giới hạn tổng feedback bổ sung ở 1.024 ký tự.
- Hint không tạo thêm vòng inference, không tiêu correction budget và không tự chạy lại tool.
- Unknown tool, sai schema hoặc permission denial đã có paired error result thì tiếp tục vòng tool thông thường; không tự kết thúc run chỉ vì có lỗi.
- Prerequisite được kiểm tra sau canonical input validation và trước permission/execution. Vi phạm trả error result; không tự gọi predecessor.
- Completion gate kiểm tra cả text-final và final-tool. Thiếu bước thì dùng feedback có giới hạn; final-tool bị từ chối phải được đóng pairing bằng cơ chế hiện có.
- Hết correction budget mà điều kiện vẫn thiếu thì trả ResultEvent(subtype="error"), không chấp nhận thành công. Force-final không được vượt completion gate.
- Cancellation, turn limit, run budget và loop guard luôn giới hạn việc tiếp tục. Giữ riêng transport retry, tool execution retry và structured-output repair hiện có.

Văn bản trả lời trực tiếp hợp lệ vẫn kết thúc bình thường khi ứng dụng không khai báo điều kiện khác.

## 4. Durability và quan sát

Dùng namespace core riêng, có version:

```python
RunCheckpoint.extension_state["linch.reliability"]
```

Lưu ledger thành công, correction count và pending action. Không đưa reliability vào ToolResultBlock hay dữ liệu activation của MCP/skills.

- Trước mỗi append do reliability tạo, checkpoint pending intent chứa thông điệp canonical, định danh hành động và counter đã cập nhật.
- Append thông điệp còn thiếu bằng kiểm tra structural equality; chỉ xóa pending sau khi toàn bộ thông điệp đã durable.
- Resume xử lý pending trước nhánh replay provider_pending, tránh gọi lại model hoặc thêm feedback lần nữa.
- Ghi kết quả rescue/rejected response và usage đã nhận vào pending trước khi commit assistant history. Persisted cumulative usage được áp dụng một lần; không mở rộng sang cam kết exactly-once billing hoặc thay đổi shared RunBudget.
- Đưa toàn bộ policy và phiên bản parser vào RunContract. Mismatch, state hỏng hoặc version không hỗ trợ phải fail closed; không âm thầm reset ledger/counter.
- Xuất ReliabilityEvent và is_reliability_event. Event có decision_id ổn định, mechanism, action, reason, turn và correction count để quan sát và khử trùng lặp khi resume.
- Run không cần can thiệp không phát thêm reliability event, feedback hoặc provider call.

Cập nhật tài liệu defaults, event codec, public API và migration 3.0. Resume run cũ tiếp tục tuân theo contract compatibility hiện có.

## 5. Triển khai và kiểm chứng

Thực hiện theo thứ tự, mỗi phần có test thất bại trước khi bổ sung runtime:

1. Policy, state và contract: cấu hình mặc định/tắt riêng, validation DAG, isolation giữa session, serialization và mismatch.
2. Correction và rescue: strict stream validation, từ chối batch trước execution, grammar rescue, ngân sách correction và usage.
3. Prerequisites và completion: ledger sau kết quả cuối cùng, chặn trước side effect, gate cả hai terminal paths.
4. Feedback và durability: thứ tự tool results/hints, crash recovery, telemetry và migration.
5. Eval so sánh: mặc định đầy đủ, tắt toàn bộ và tắt riêng từng cơ chế trên cùng workload.

Ma trận offline bắt buộc:

- Direct text, refusal và output truncation không bị ép gọi tool.
- Rescue hợp lệ; prose/code example, oversized input, unknown/unoffered tool và structured-output response không bị thực thi nhầm.
- Malformed native JSON/non-object; batch trộn hợp lệ và không hợp lệ phải thực thi zero tool.
- Không va chạm với input schema có khóa __invalid_json.
- Prerequisite thiếu, cùng batch, predecessor lỗi và background ACK không mở khóa dependent call.
- Completion thành công, retry tối đa hai lần, exhaustion và force-final.
- Partial failure giữ nguyên kết quả sibling thành công; hints không gây replay write/exec.
- Crash trước/sau từng append, trước checkpoint cuối và khi có frozen request: không thêm feedback trùng, không reset counter, không thực thi lại tool đã hoàn thành.
- Provider matrix dùng fake transports cho mọi adapter; kiểm tra request history và tool pairing.
- Compaction, hooks, cancellation, budgets và subagent isolation vẫn đúng.

Eval model thật nằm ngoài unit suite và skip khi thiếu credentials. Đo tỷ lệ hoàn thành đúng, số call dư, correction count, token và latency; không dùng kết quả scripted provider để tuyên bố tăng chất lượng model.

Chạy targeted tests trong từng phần, sau đó pytest, ruff check ., ruff format --check . và pyright. Chỉ cập nhật baseline khi thay đổi đã được giải thích; không dùng ngưỡng latency từ API thật làm CI gate.

## Handoff clarification

Pending multi-message append must reconcile the durable ordered prefix and append only the missing suffix; independent last-message comparisons are insufficient. Preserve the received cumulative usage totals when resuming.
