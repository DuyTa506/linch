# Bộ nhớ đệm tool

[Bản gốc tiếng Anh](../../usage/tool-cache.md)

Dùng `ToolCacheConfig` để ghi nhớ kết quả gọi tool đọc có dữ liệu ổn định trong một run.

`ToolCacheHook` ghi nhớ kết quả gọi tool **có phạm vi đọc** **trong một run duy nhất**
để model không tốn chi phí thực hiện lại một truy vấn giống hệt truy vấn trước đó.
Đây là lớp *tối ưu hiệu suất* xây dựng trên hệ thống hook — không phải lớp bảo đảm
an toàn (nó không ngăn các vòng lặp mất kiểm soát; [loop guard](./agent.md)
vẫn là lớp bảo vệ cuối cùng).

Tính năng này **mặc định tắt**. Bộ nhớ đệm chỉ phù hợp với tool có dữ liệu đủ ổn
định trong một run (tìm kiếm/grep trên tập dữ liệu cố định), vì vậy bạn chỉ định
các tool được phép lưu vào bộ nhớ đệm thay vì để SDK tự đoán.

<a id="enable"></a>

## `ToolCacheConfig`

```python
from linch import Agent, ToolCacheConfig

# Selective (recommended): only these tools are cached.
agent = Agent(..., tool_cache=ToolCacheConfig(allow={"Grep", "Search"}))

# Every read-scope tool (you accept the staleness trade-off below).
agent = Agent(..., tool_cache=True)

# Default: disabled.
agent = Agent(...)
```

`tool_cache` nhận `ToolCacheConfig`, một `ToolCacheHook` có sẵn, `True`
(tất cả tool có phạm vi đọc), hoặc `None`/`False` (tắt).

```python
from dataclasses import dataclass


@dataclass(slots=True)
class ToolCacheConfig:
    allow: set[str] | None = None   # None = every read-scope tool
    deny: frozenset[str] = frozenset()
    max_entries: int = 256          # per-run LRU cap
    max_value_bytes: int = 100_000   # maximum cached result size
```

`ToolCacheHook` yêu cầu `max_entries >= 1` và `max_value_bytes >= 0`.
Kết quả lớn hơn `max_value_bytes` không được lưu vào bộ nhớ đệm.

## Cách hoạt động

Hook nằm tại hai điểm kiểm soát (xem [hooks.md](./hooks.md)):

- **`PreToolUse`** — khi **đầu vào khớp chính xác** với một mục bộ nhớ đệm của tool
  được phép lưu, hook trả về `HookResult.resolve(tool_result=...)`: tool **không
  được thực thi** và kết quả trong bộ nhớ đệm được trả về.
- **`PostToolUse`** — kết quả thành công của một lệnh gọi chưa có trong bộ nhớ đệm
  và được phép lưu sẽ được lưu lại.

Khóa bộ nhớ đệm là `tool_name` + JSON đã sắp xếp của đầu vào mà hook bộ nhớ đệm
nhận được, nên chỉ các lệnh gọi **giống hệt nhau** mới khớp (`{"query": "a"}` và
`{"query": "b"}` không bao giờ trùng khóa).

## Bảo đảm tính đúng đắn

- **Chỉ phạm vi đọc.** Tool `write`/`exec` không bao giờ được lưu vào bộ nhớ đệm,
  kể cả khi bạn đưa chúng vào `allow` — việc ghi lại có thể là chủ ý, và trả về
  kết quả cũ cho một tool có tác động phụ sẽ là lỗi.
- **Thao tác ghi làm mất hiệu lực kết quả đọc.** Khi bất kỳ tool `write`/`exec`
  nào chạy, các kết quả đọc trong bộ nhớ đệm của run được xóa trước khi điều phối
  và một lần nữa khi hoàn tất, kể cả khi hoàn tất chạy nền qua `PostToolUse`.
  Với chuỗi `Read → Write → Read`, sau khi thao tác ghi hoàn tất, thao tác đọc
  thứ hai được thực thi lại. Các thao tác đọc đồng thời vẫn có thể thấy giá trị
  cũ cho đến khi thao tác ghi đang chạy hoàn tất.
- **Lỗi không bao giờ được lưu vào bộ nhớ đệm.** Tự động thử lại yêu cầu
  `RetryOptions`; bộ nhớ đệm không bật tính năng thử lại.
- **Theo từng run.** Bộ nhớ đệm dùng `run_id` làm khóa và bị loại bỏ khi run kết
  thúc. Nó nằm trong bộ nhớ và **không được lưu vào checkpoint**: sau khi resume
  từ trạng thái lưu trữ bền vững, bộ nhớ đệm đơn giản là chưa có dữ liệu (không
  tìm thấy mục), chứ không bao giờ trả sai.

## Điểm cần lưu ý

Bộ nhớ đệm theo từng run giả định các tool của chính agent là những thành phần
duy nhất ghi dữ liệu. Nếu dữ liệu nền của một thao tác đọc bị tiến trình **bên
ngoài** thay đổi giữa hai lệnh gọi giống hệt nhau trong cùng run, kết quả trả về
có thể đã cũ. Chỉ cho phép lưu vào bộ nhớ đệm các tool có dữ liệu ổn định trong
suốt run, hoặc tắt bộ nhớ đệm đối với các nguồn thường xuyên thay đổi.

## Liên quan

- [Hook](./hooks.md) — các điểm kiểm soát và hành động `resolve` mà tính năng này dựa vào.
- [Tool](./tools.md) — `scope` của tool, yếu tố quyết định khả năng lưu vào bộ nhớ đệm.
