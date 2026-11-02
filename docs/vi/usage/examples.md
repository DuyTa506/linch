# Ví dụ

[Bản gốc tiếng Anh](../../usage/examples.md)

Dùng mục lục này để tìm các ví dụ có thể chạy cho phân hệ bạn đang tích hợp.

[← Hướng dẫn sử dụng](./README.md)

Mã ví dụ có thể chạy nằm trong thư mục `examples/`, được tổ chức theo phân hệ. Các bản demo cục bộ (đánh dấu *local*) chạy mà không cần API key thật, nên bạn có thể tìm hiểu cơ chế hoạt động ngoại tuyến trước khi kết nối provider. Một số ví dụ cũng cung cấp các hàm tạo hoặc hàm hỗ trợ dùng ngoại tuyến, trong khi `main()` của script dùng provider thật.

---

## `core/`

| Tệp | Nội dung minh họa |
|------|---------------|
| `core/tiny_agent.py` | `Agent` tối giản kết hợp vòng lặp session; thêm `workspace_tools()` một cách tường minh khi ứng dụng cần truy cập tệp hoặc shell |
| `core/coding_agent.py` | Agent kỹ thuật phần mềm với `workspace_tools`, các rào chắn an toàn `BashRule`/`PathRule`, `LoopGuard` và ngữ cảnh nhiều lượt |
| `core/policy_aware_execution.py` | Thực thi Bash qua Docker với các quy tắc phân quyền và những giới hạn lúc chạy cần chủ động bật |
| `core/reading_agent.py` | Hỏi đáp về mã nguồn ở chế độ chỉ đọc: loại trừ Write/Edit/Bash, giới hạn đường dẫn bằng `PathRule` và dùng vai trò người đánh giá |
| `core/chat_agent.py` | Chat hỗ trợ khách hàng không dùng tool, với việc chèn chính sách bằng `ContextBuilder`, phản hồi JSON qua `OutputSchema` và ngữ cảnh nhiều lượt |
| `core/custom_permissions.py` | Các chế độ phân quyền, `ToolRule`/`PathRule`/`BashRule` và các callback phê duyệt đồng bộ, bất đồng bộ, tương tác |
| `core/system_prompts.py` | Nối thêm, thay thế giá trị mặc định, khối hệ thống tùy chỉnh, ghi đè theo session và các mẫu vai trò |
| `core/structured_output.py` | `OutputSchema` ở cấp agent và từng run, `final_tool_name` kết thúc, schema lồng nhau và xử lý lỗi phân tích cú pháp |
| `core/multi_session.py` | Mẫu ứng dụng web: một `Agent`, nhiều người dùng, session được lưu bền vững, các phụ thuộc theo session và siêu dữ liệu |
| `core/loop_guard_agent.py` *local* | Phát hiện vòng lặp gọi lặp lại, buộc đưa ra câu trả lời cuối cùng và phương án dự phòng `max_turns` |
| `core/interactive_cli.py` | REPL tương tác trên terminal với cơ chế tắt agent gọn gàng |
| `core/governance_redaction.py` *local* | Các quy tắc regex do ứng dụng chủ cung cấp loại bỏ dữ liệu cần che khỏi kết quả tool và câu trả lời cuối cùng thông qua `RedactionHook` |
| `core/ask_user_agent.py` | `AskUserPlugin`, phản hồi theo nguyên tắc từ chối khi không chắc chắn, xử lý hết thời gian chờ và các trình xử lý CLI dạng blocking (hàm tạo có thể kiểm thử ngoại tuyến) |
| `core/budget_capped_agent.py` *local* | Giới hạn token bằng `RunBudget` dùng chung và các event cảnh báo/lỗi budget với các lượt sử dụng theo kịch bản |
| `core/compaction_ladder_agent.py` *local* | Nén nhỏ trước khi buộc LLM tóm tắt khi provider báo `ContextLengthError` |
| `core/workflow_fleet.py` | Phân nhánh workflow tất định, các giai đoạn pipeline, budget dùng chung và phát lại nhật ký Postgres sau khi thất bại |

---

## `tools/` — *có các bản demo cục bộ*

| Tệp | Nội dung minh họa |
|------|---------------|
| `tools/custom_tools.py` | Năm mẫu tool: đọc, ghi, thực thi, tiêm phụ thuộc và tool dạng lớp với nhiều bước kiểm tra tính hợp lệ |
| `tools/runtime_tools.py` *local* | Thêm/xóa/thay thế/chọn trong sổ đăng ký lúc chạy, siêu dữ liệu tài nguyên và xuất schema; lời gọi agent tùy chọn dùng dịch vụ thật |
| `tools/rag_tools.py` | Các client RAG được tiêm vào cho các tool tìm kiếm kết hợp, theo từ khóa, theo đồ thị và trên web |
| `tools/tool_middleware.py` *local* | Middleware viết lại đầu vào đã kiểm tra hợp lệ, chặn lời gọi nhạy cảm và che dữ liệu trong đầu ra tool |

---

## `context/` — *có các bản demo cục bộ*

| Tệp | Nội dung minh họa |
|------|---------------|
| `context/context_injection.py` *local* | Các mẫu ContextInjectionHook: RAG theo lượt, budget, các tool được chọn |

---

## `memory/` — *có các bản demo cục bộ*

| Tệp | Nội dung minh họa |
|------|---------------|
| `memory/mem0_recall.py` | Các hook ngữ cảnh và kết thúc tương thích mem0 để truy hồi trong phạm vi run và ghi sau lượt thành công; ứng dụng chủ cung cấp client bộ nhớ async |

---

## `observability/`

| Tệp | Nội dung minh họa |
|------|---------------|
| `observability/observability_agent.py` | Thiết lập OpenTelemetry trực tiếp với việc tiêm `Context` cho từng agent và các bộ xuất ra console |

---

## `providers/`

| Tệp | Nội dung minh họa |
|------|---------------|
| `providers/openai_agent.py` | OpenAIChatCompletionsProvider — hỏi đáp cơ bản, event suy luận, dùng tool, suy luận kết hợp dùng tool, đầu ra có cấu trúc, nhiều lượt |
| `providers/anthropic_agent.py` | AnthropicProvider — hỏi đáp cơ bản, suy luận mở rộng (với `PartialAssistantEvent`), lưu đệm prompt |
| `providers/deepseek_agent.py` | DeepSeek qua cả endpoint tương thích OpenAI và tương thích Anthropic — suy luận, dùng tool, suy luận kết hợp dùng tool, nhiều lượt |

---

## `integrations/` — *có bản demo cục bộ*

| Tệp | Nội dung minh họa |
|------|---------------|
| `integrations/redis_mailbox.py` *local* | Adapter `Mailbox` bên ngoài theo ngữ nghĩa danh sách Redis, lấy hết thư một cách nguyên tử, các trường tương quan và `assert_mailbox_contract` với client giả |
| `integrations/skills_usage.py` *local* | Tạo skill dự án, nạp/gọi skill và các event vòng đời với provider giả |

## `extensions/` — *các mẫu*

| Tệp | Nội dung minh họa |
|------|---------------|
| `extensions/provider_template.py` | Adapter `BaseProvider` tối giản với các event streaming đã chuẩn hóa |
| `extensions/tool_package_template.py` | Ưu tiên `@tool`, kèm tool dạng lớp nâng cao và hàm tạo sổ đăng ký |

## `recipes/` — *có bản demo cục bộ*

| Tệp | Nội dung minh họa |
|------|---------------|
| `recipes/research_desk.py` | Agent phân tích tài liệu không lập trình với tool chuyên ngành, kho ngữ liệu/sổ trích dẫn qua `ctx.deps`, `OutputSchema` và cơ chế thử lại `FinalAnswerVerifierHook` cho câu trả lời không có trích dẫn; hàm tạo của nó có thể kiểm thử ngoại tuyến còn `main()` dùng dịch vụ thật |
| `recipes/loop_runner.py` | `LoopSpec` + `LoopRunner.run_once()` cho công việc định kỳ do ứng dụng chủ kích hoạt, nạp `.env` của dự án và các sản phẩm đầu ra trong `domains/<loop_id>/` |
| `recipes/runner_recipes.py` *local* | Các lớp bọc cron, webhook, chu kỳ cố định và cổng kiểm tra CI do ứng dụng chủ quản lý trên `LoopRunner.run_once()` với provider giả |
| `recipes/ralph_loop.py` | Bộ khung thực thi dài hạn, trong đó mỗi vòng lặp nhận một session mới và chỉ mang trạng thái qua `StateFileBackend` cho đến khi thỏa điều kiện hoàn tất; `max_iterations` giới hạn chi phí |

## `coordination/` — *có các bản demo cục bộ*

[Lớp điều phối](./coordination.md) tùy chọn cung cấp khả năng nhắn tin giữa các agent ngang hàng và liên kết yêu cầu/phản hồi. Hàm tạo và quá trình bắt tay qua hộp thư của ví dụ được kiểm thử nhanh ngoại tuyến bằng `ScriptedProvider` trong `tests/examples/test_example_coordination.py`; `main()` của nó dùng provider thật.

| Tệp | Nội dung minh họa |
|------|---------------|
| `coordination/team_mailbox.py` | Nhóm hai agent dùng chung `InMemoryMailbox`: một agent gửi tới agent kia bằng `send_message`, và thư được lấy ra ở lượt tiếp theo của bên nhận, kèm quá trình bắt tay yêu cầu/phản hồi qua `Correlator` (phê duyệt kế hoạch) |

---

## `rag/` — *có các bản demo cục bộ*

| Tệp | Nội dung minh họa |
|------|---------------|
| `rag/keyword_connector.py` *local* | Bộ kết nối `RetrievedChunk` không có phụ thuộc, xếp hạng kho ngữ liệu trong bộ nhớ theo mức trùng khớp từ khóa |

---

## `worker/`

| Tệp | Nội dung minh họa |
|------|---------------|
| `worker/minimal.py` | Nhóm worker dựa trên PgQueuer với provider theo kịch bản, kho lưu trữ run Postgres, các chế độ enqueue/worker/event và không cần API key mô hình |
| `worker/benchmark_enqueue.py` | Đo thông lượng đưa vào hàng đợi Postgres với schema riêng biệt và kiểm tra `NOTIFY` tại thời điểm commit |

---

Các subagent tích hợp sẵn có thể dùng mà không cần định nghĩa trên đĩa. Sau những thay đổi đáng kể về triển khai hoặc workflow, hãy yêu cầu mô hình gọi `Subagent` với `subagent_type="verification"` và prompt gồm yêu cầu ban đầu, sản phẩm đầu ra hoặc tệp đã thay đổi, cách tiếp cận và các kiểm tra bạn muốn nó chạy. Subagent xác minh bị giới hạn ở `Read`, `Glob`, `Grep` và `Bash`, và phải kết thúc bằng `VERDICT: PASS`, `VERDICT: FAIL` hoặc `VERDICT: PARTIAL`.

---

Quay lại [Mục lục hướng dẫn sử dụng](./README.md).
