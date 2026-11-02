# Hiệu năng đọc nhật ký session

[Bản gốc tiếng Anh](../../architecture/session-log-performance.md)

Dùng trang này khi đánh giá chi phí đọc projection hoặc thay đổi bộ đọc
`SessionLog`. Hợp đồng công khai quy định kết quả là một chuỗi bất biến, tách biệt
khỏi dữ liệu gốc, chứa các đồ thị thông điệp có thể thay đổi: bên gọi có thể sửa
`Message` hoặc khối nội dung được trả về, nhưng thay đổi đó không được ảnh hưởng
đến nhật ký hay lần đọc sau.

## Đo lường

`SessionLog` cập nhật ngay các mục dữ liệu gốc, `full_history` và
`provider_view` khi thông điệp và projection được nối thêm. Các thuộc tính
projection trả về tuple chứa kết quả `deepcopy`, nên chi phí đọc toàn bộ projection
tăng theo đồ thị đối tượng cần sao chép. `entries` cũng trả về snapshot tách biệt
như vậy.

Các accessor ít tốn chi phí tránh sao chép toàn bộ projection: `visible_count` và
`history_count` trả về độ dài của danh sách đã lưu, còn `last_visible()` chỉ sao
chép thông điệp mới nhất trong provider view. `Session.last_provider_message()`
là phương thức bọc tương ứng ở cấp session.

Đoạn mã độc lập dưới đây đo các getter hiện tại trên máy cục bộ.
Nó chủ ý báo cáo số đo thay vì đưa vào một bảng kết quả trong quá khứ; phiên bản
Python, phần cứng, bộ cấp phát bộ nhớ và tải của bộ lập lịch đều ảnh hưởng đến
các con số.

<details>
<summary>Script benchmark có thể tái lập</summary>

```bash
PYTHONPATH=src python - <<'PY'
from __future__ import annotations

import gc
import statistics
import time

from linch import Message, SessionLog, TextBlock, ToolResultBlock, ToolUseBlock


def make_turn(turn: int) -> list[Message]:
    return [
        Message(
            role="user",
            content=[TextBlock(text=f"user prompt {turn}")],
            provider_metadata={"turn": turn, "nested": {"source": "client", "tags": list(range(4))}},
        ),
        Message(
            role="assistant",
            content=[
                TextBlock(text=f"thinking/result {turn}"),
                ToolUseBlock(
                    id=f"call-{turn}",
                    name="search",
                    input={"query": f"q{turn}", "limit": 5},
                ),
            ],
            provider_metadata={"request": {"id": f"req-{turn}", "latency_ms": 12.5}},
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=f"call-{turn}",
                    content=f"tool output for turn {turn}; " + ("x" * 120),
                )
            ],
            provider_metadata={"tool": "search", "status": "ok"},
        ),
        Message(
            role="assistant",
            content=[TextBlock(text=f"final answer {turn}; " + ("y" * 80))],
            provider_metadata={
                "finish": {
                    "stop_reason": "end_turn",
                    "usage": {"input": 100, "output": 20},
                }
            },
        ),
    ]


def build(turns: int) -> SessionLog:
    log = SessionLog()
    for turn in range(turns):
        log.append_many(make_turn(turn))
    return log


def timed(log: SessionLog, getter: str, repetitions: int) -> tuple[float, float]:
    properties = {"provider_view", "full_history", "entries"}
    read = (
        (lambda: getattr(log, getter))
        if getter in properties
        else (lambda: getattr(log, getter)())
    )
    read()
    gc.collect()
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter_ns()
        for _ in range(repetitions):
            if read() is None:
                raise AssertionError
        samples.append((time.perf_counter_ns() - started) / 1e6)
    median_ms = statistics.median(samples)
    return median_ms, min(samples)


for turns in (50, 200):
    log = build(turns)
    repetitions = 20 if turns == 200 else 50
    print(f"turns={turns} messages={log.history_count} repetitions={repetitions}")
    for getter in ("provider_view", "full_history", "entries", "last_visible"):
        median_ms, minimum_ms = timed(log, getter, repetitions)
        print(
            f"  {getter:16s} median={median_ms:.3f}ms "
            f"min={minimum_ms:.3f}ms per_read={median_ms / repetitions * 1000:.3f}us"
        )
PY
```

</details>

Benchmark là công cụ quan sát, không phải kiểm thử tính tương thích. Các kiểm thử
tính tương thích dựa trên mã nguồn nằm trong
`tests/session_log/test_session_log.py`: chúng thay đổi thông điệp, khối nội dung,
metadata và các mục projection được trả về, rồi xác minh rằng các lần đọc sau vẫn
không đổi. `tests/loop/test_history_reads.py` kiểm tra riêng rằng quá trình gắn
lời gọi model chính không đọc `full_history` chỉ để tính danh tính bền vững của nó.

## Vì sao `deepcopy` vẫn được dùng để triển khai việc đọc tách biệt

`Message.provider_metadata` và `ToolUseBlock.input` được thiết kế để chứa các giá
trị có kiểu `Any`, và union của các khối nội dung có thể mở rộng. Vì vậy, cách
triển khai hiện tại dùng `deepcopy` để bảo đảm quyền sở hữu và các lần đọc tách
biệt, giữ nguyên đồ thị đối tượng Python đang tồn tại mà không đưa thêm định dạng
tuần tự hóa hay bộ nhớ đệm thứ hai. Một bộ nhớ đệm dựa trên tuần tự hóa nhanh hơn
sẽ cần một hợp đồng mới về tính tương thích và bộ nhớ; trang này không đưa ra
hợp đồng như vậy.

## `SessionLog._history_fingerprint`

`_history_fingerprint()` là phép tính SHA-256 tích lũy riêng tư, thực hiện khi cần,
trên mảng JSON chuẩn tắc biểu diễn các thông điệp trong lịch sử. Nó lưu đệm trạng
thái hash của phần tiền tố và số thông điệp lịch sử đã được tuần tự hóa, rồi chỉ
tuần tự hóa phần đuôi chưa xử lý ở lần yêu cầu tiếp theo. Thông điệp chỉ xuất hiện
trong provider view hoặc mục projection không làm thay đổi digest kiểm toán.
Nếu việc tuần tự hóa phần đuôi thất bại, trạng thái tiền tố hợp lệ và con trỏ
trước đó được giữ nguyên.

Quá trình gắn lời gọi model chính dùng `history_count` và fingerprint này thay
vì sao chép rồi tính lại fingerprint của toàn bộ projection kiểm toán trong mỗi
lần gắn. Hành vi cụ thể của fingerprint và số lần đọc được kiểm thử trong
`tests/session_log/test_session_log.py` và `tests/loop/test_history_reads.py`.

## Danh mục bộ đọc

Các nơi đọc projection trong runtime được liệt kê tường minh có chủ đích:

- `src/linch/session.py` cung cấp `Session.provider_view`,
  `Session.full_history`, `Session.message_count` và
  `Session.last_provider_message()`.
- `src/linch/session_log.py` triển khai `SessionLog.entries`,
  `SessionLog.full_history`, `SessionLog.provider_view`, các accessor đếm,
  `SessionLog.last_visible()` và `_history_fingerprint()`.
- `src/linch/loop/request.py` dựng request từ `provider_view`; các bộ dựng context
  trong `src/linch/hooks/adapters.py` nhận provider view tách biệt.
- `src/linch/loop/terminals.py` duyệt provider view tách biệt theo thứ tự ngược,
  còn `src/linch/compaction.py` làm việc trên các danh sách projection tách biệt
  trước khi ghi một projection mới.
- `src/linch/loop/finalize.py`, `src/linch/loop/feedback.py` và
  `src/linch/loop/resume.py` xem xét các snapshot `full_history` tách biệt để
  quyết định việc kết thúc và khôi phục.
- `src/linch/loop/runtime.py` lưu snapshot của provider view và gắn lời gọi chính
  bằng `history_count` cùng `_history_fingerprint()`.
- `src/linch/subagents/runner.py` khởi tạo session con từ provider view tách biệt.
  `src/linch/agent.py` và `src/linch/sessions/store.py` khôi phục và lưu bền vững
  các snapshot provider view liên quan.

Các bộ đọc này có thể biến đổi giá trị tách biệt của chúng, nhưng không thay đổi
đồ thị thông điệp thuộc quyền sở hữu của nhật ký. `Session.append(...)` có tính
bền vững vẫn là ranh giới ở cấp session để lưu lịch sử; gọi trực tiếp
`SessionLog.append(...)` là thao tác trong bộ nhớ.
