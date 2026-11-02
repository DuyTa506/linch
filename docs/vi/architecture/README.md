# Kiến trúc Linch 3.0

[Bản gốc tiếng Anh](../../architecture/README.md)

Tài liệu tham chiếu cho nhân agent Linch không có giao diện. Tài liệu mô tả các
hợp đồng hệ thống con, luồng dữ liệu, ranh giới lưu trữ bền vững và bất biến hiện tại.

---

Hướng dẫn này được chia theo chủ đề để bạn có thể đi thẳng đến một hệ thống con thay vì cuộn qua một tệp dài. Để biết *cách sử dụng* các tính năng này, xem [hướng dẫn sử dụng](../usage/README.md).

Các import được hỗ trợ và hợp đồng tương thích được mô tả trong
[Quản lý phiên bản và API công khai](../../versioning.md).

## Các phần

| Phần | Nội dung |
|---|---|
| 1. [Tổng quan hệ thống](./overview.md) | Nhân không có giao diện, ranh giới với ứng dụng chủ, cấu trúc triển khai và luồng dữ liệu |
| 2. [Vòng đời của lượt](./turn-lifecycle.md) | Những gì diễn ra trong mỗi lượt: lắp ghép request → stream từ provider → tool → lặp lại |
| 3. [Các hệ thống con](./subsystems.md) | Chi tiết các hệ thống con: vòng lặp, provider, tool, phân quyền, context/RAG, hệ thống tệp và nhiều thành phần khác |
| 4. [Phân loại event](./events.md) | Event trong luồng, ranh giới lưu trữ bền vững và các điểm điều phối vòng đời |
| 5. [Các kiểu dữ liệu chính](./data-types.md) | Các kiểu dữ liệu chính: thông điệp, khối, request, kết quả, mức sử dụng |
| 6. [Danh mục module](./module-inventory.md) | Danh mục cây mã nguồn theo từng module |
| 7. [Hợp đồng provider](./provider-contract.md) | Hợp đồng provider: context_window / stream / capabilities |
| 8. [Giao thức tool](./tool-protocol.md) | Giao thức tool theo duck typing và cấu trúc kết quả tool |
| 9. [Các lớp system prompt](./system-prompt.md) | Cách system prompt được lắp ghép từ các phần theo lớp |
| 10. [Các luồng đầu ra có cấu trúc](./structured-output.md) | Các luồng đầu ra có cấu trúc: thu nhận final-text và final-tool, sửa schema |
| 11. [Nén context](./compaction.md) | Nén context: thời điểm và cách tóm tắt context cũ |
| 12. [Skill và subagent](./skills-subagents.md) | Skill (lệnh slash) và subagent (vai trò, worker, fork/continue) |
| 13. [Nhân](./kernel.md) | Nhân IoC/effect: phạm vi `Context`, effect `Disposable` có thể hoàn tác, bus event (emit/serial/waterfall), pipeline tool mở và các điểm tích hợp khả năng |
| 14. [Nguyên tắc phát triển](./development_principles.md) | Quy tắc về phạm vi, mở rộng, tính bền vững, tương thích, bất đồng bộ và bảo mật cho người đóng góp |
| 15. [Các bất biến chính](./invariants.md) | Các bất biến chính mà toàn hệ thống duy trì |
| 16. [Hiệu năng đọc nhật ký session](./session-log-performance.md) | Benchmark việc đọc projection và danh mục bộ đọc |

## Mục lục tính năng Linch 3.0

Các thành phần của 3.0 được chủ ý chia giữa các trang kiến trúc và hướng dẫn sử dụng.
Dùng bản đồ này khi xem xét ranh giới tính năng hoặc chọn cách tích hợp.

| Tính năng | Hợp đồng và cách sử dụng |
|---|---|
| Gắn plugin và `agent.use` | [Plugin của nhân](../../architecture/kernel.md#plugins) và [các hàng plugin trong profile](../usage/profiles.md#các-lớp-và-việc-kiểm-tra) |
| Profile miền nghiệp vụ có phiên bản | [Profile miền nghiệp vụ](../usage/profiles.md) và [phạm vi profile](../../architecture/kernel.md#domain-profiles) |
| Tìm kiếm tool/skill trì hoãn | [Tìm kiếm tool trì hoãn](../../architecture/tool-protocol.md#deferred-tool-search) và [danh mục lớn](../usage/tools.md#toolsearchplugin--danh-mục-lớn) |
| Danh mục vòng đời có kiểu và trace ID | [Danh mục vòng đời được sinh tự động](./events.md#danh-mục-vòng-đời-có-kiểu) và [trace ID của event](../usage/events.md#trace-và-số-liệu-opentelemetry) |
| Principal theo từng run và quy tắc RBAC | [Đánh giá quyền](../../architecture/subsystems.md#37-permission-evaluation-and-principal) và [quy tắc vai trò](../usage/tools.md#principal-và-quy-tắc-theo-role-cho-từng-run) |
| Truy xuất RAG và kiểm tra căn cứ trích dẫn | [Cách dùng context/RAG](../usage/context-and-memory.md#ragconnector-và-register_grounding_check) và [hệ thống con RAG](../../architecture/subsystems.md#38-context-file-memory-and-rag) |
| Run, lease và fencing trong Postgres | [Kho run](../usage/run-stores.md) và [hệ thống con run bền vững](../../architecture/subsystems.md#33-durable-runs-leases-and-checkpoints) |
| Cụm worker PgQueuer và lịch chạy | [Mở rộng theo chiều ngang trong production](../usage/production.md#mở-rộng-theo-chiều-ngang) và [khôi phục worker](../../architecture/subsystems.md#34-worker-dispatch-and-pgqueuer-recovery) |
| Bộ nhớ lưu trên tệp | [Cách dùng bộ nhớ tệp](../usage/context-and-memory.md#filememorycontextbuilder) và [hệ thống con RAG/tệp](../../architecture/subsystems.md#38-context-file-memory-and-rag) |
| Gắn đo lường OpenTelemetry trong mã nguồn | [Trace và số liệu](../usage/events.md#trace-và-số-liệu-opentelemetry) và [kiến trúc OTel](../../architecture/subsystems.md#310-opentelemetry) |
| Các phần mở rộng `linch.ext.*` tường minh | [Ranh giới phần mở rộng](./overview.md#phần-mở-rộng) và [hệ thống con phần mở rộng](../../architecture/subsystems.md#311-extensions-and-host-owned-boundaries) |
| Độ tin cậy và dừng lượt | [Vòng đời của lượt](./turn-lifecycle.md#các-điểm-vòng-đời-có-kiểu) và [cách dùng cơ chế bảo đảm độ tin cậy](../usage/agent.md#reliabilitypolicy--độ-tin-cậy-của-tool-và-phản-hồi) |
| Môi trường thực thi thống nhất | [Cách dùng cơ chế thực thi](../usage/extending.md#executionbackend--một-môi-trường-shell--hệ-thống-tệp-thống-nhất) và [giao thức tool](./tool-protocol.md#toolregistry) |
| Nhật ký session chỉ nối thêm và các projection | [Bất biến của session](./invariants.md) và [hiệu năng nhật ký session](./session-log-performance.md) |
| Thống kê lời gọi model và nén context | [Event thống kê](../../usage/events.md#model-call-accounting) và [nén context](./compaction.md) |
| Đầu ra tool chuẩn tắc và chuyển giao bền vững | [Giao thức tool](./tool-protocol.md) và [cách dùng agent/session](../usage/agent.md) |
| Thế hệ định dạng truyền dữ liệu 3 và Python 3.11+ | [Hướng dẫn chuyển đổi](../../migration-3.0.md) và [hợp đồng quản lý phiên bản](../../versioning.md) |
