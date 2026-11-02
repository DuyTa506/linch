# Hệ thống tệp ảo và chuyển kết quả lớn ra ngoài ngữ cảnh

[Bản gốc tiếng Anh](../../usage/filesystem.md)

Dùng hệ thống tệp ảo để chuyển kết quả tool lớn ra ngoài ngữ cảnh và cung cấp các thao tác với tệp.

[← Hướng dẫn sử dụng](./README.md)

Kết quả tool lớn — các đoạn RAG, dữ liệu tìm kiếm web, nội dung đọc từ tệp lớn — có thể nhanh chóng chiếm hết ngữ cảnh. Khi được bật, hệ thống tệp ảo của Linch có thể ghi các kết quả quá lớn đủ điều kiện vào `FileBackend` và thay chúng trong hội thoại bằng bản xem trước ngắn cùng đường dẫn để mô hình đọc lại khi cần.

---

## Vì sao cần chuyển kết quả ra ngoài ngữ cảnh

Khi một tool đủ điều kiện trả về kết quả dạng chuỗi quá lớn, scheduler ghi toàn bộ kết quả vào `FileBackend` và thay nội dung mô hình thấy trong `provider_view` bằng bản xem trước ngắn cùng tham chiếu đường dẫn. Kết quả đủ điều kiện phải thành công (không có lỗi), có nội dung là chuỗi, thuộc tool không nằm trong `skip_tools` và có kích thước ước tính vượt ngưỡng đã cấu hình. Mô hình chỉ lấy lại phần cần thiết qua `read_file`, thay vì mang theo toàn bộ dữ liệu trong mỗi lệnh gọi provider sau đó. Sau khi chuyển ra ngoài thành công, toàn bộ nội dung gốc vẫn nằm trong backend và trên `ToolCallEndEvent.tool_result` cho bên tiêu thụ event; chỉ bản sao dành cho mô hình được rút gọn. Nếu ghi vào backend thất bại, kết quả gốc vẫn được giữ nguyên trong hội thoại. Để biết cơ chế này tích hợp vào điểm kiểm soát thực thi tool như thế nào, xem [./tools.md](./tools.md).

---

## Bật tường minh

Tool hệ thống tệp và cơ chế chuyển kết quả lớn ra ngoài ngữ cảnh cần được bật chủ động. Với `FeatureFlags` mặc định, agent không có tool hệ thống tệp ảo hay backend lưu kết quả chuyển ra ngoài. Bật tường minh hệ thống con bằng `FeatureFlags(filesystem=True)` và chọn backend, hoặc để các session đang hoạt động dùng `StateFileBackend` do agent tạo. Việc cung cấp backend `filesystem=` tường minh (hoặc `fs` của môi trường thực thi) sẽ chọn backend đó.

Khi hoạt động, ngưỡng mặc định được tính tự động từ cửa sổ ngữ cảnh của mô hình (`threshold_fraction=0.1` → 10 % cửa sổ ngữ cảnh). Mô hình có cửa sổ 128 nghìn token chuyển ra ngoài các kết quả trên khoảng 12.800 token; mô hình 200 nghìn token áp dụng với kết quả trên khoảng 20.000 token.

```python
from linch import (
    Agent,
    CompositeFileBackend,
    DiskFileBackend,
    FeatureFlags,
    OffloadConfig,
    StateFileBackend,
)

# Explicit — ephemeral session backend, threshold = 10 % of context window
agent = Agent(..., features=FeatureFlags(filesystem=True))

# Persist offloaded files under .linch/offload (inspectable, gitignored)
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    filesystem=DiskFileBackend(root=".linch/offload"),
)

# Tune the threshold or fraction explicitly
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    result_offload=OffloadConfig(threshold_tokens=5_000),   # hard override
    # Or replace the value above with OffloadConfig(threshold_fraction=0.05)
    # for 5 % of context.
)

# In-memory scratch + persistent /memories/ across sessions
import os
from linch.filesystem.postgres import PostgresFileBackend
# Install the optional driver first: pip install 'linch[postgres]'
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    filesystem=CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": PostgresFileBackend(os.environ["LINCH_POSTGRES_DSN"])},
    ),
)

# Disable automatic offloading but keep explicit filesystem tools
agent = Agent(
    ...,
    features=FeatureFlags(filesystem=True),
    filesystem=StateFileBackend(),
    result_offload=None,
)

# Disable the entire filesystem subsystem
agent = Agent(..., features=FeatureFlags(filesystem=False))
```

### Chọn backend

Các backend lưu trữ dưới đây khác nhau ở nơi lưu dữ liệu và cách xác định phạm vi tệp:

- **`StateFileBackend`** (mặc định cho session đang hoạt động, do agent tạo) — nằm trong bộ nhớ và riêng cho từng session. Không cần thiết lập; tệp nằm trong bộ nhớ, không được lưu lâu dài và session mới nhận một backend mặc định mới. Phù hợp với việc chuyển kết quả ra ngoài thông thường, khi mô hình chỉ cần đọc lại dữ liệu trong cùng một run.
- **`DiskFileBackend(root=...)`** — tệp thật trong một thư mục gốc được giới hạn bằng sandbox (mặc định là `.linch/offload`, được bỏ qua bởi git). Dữ liệu được lưu lâu dài và có thể kiểm tra trên đĩa, nên bạn có thể mở dữ liệu đã chuyển ra ngoài trong trình soạn thảo khi gỡ lỗi.
- **`PostgresFileBackend(dsn)`** — lưu lâu dài qua các session trong Postgres. Dùng khi mô hình cần đọc lại tệp từ các run trước.
- **`CompositeFileBackend(default=..., routes=...)`** — định tuyến đường dẫn theo tiền tố đến các backend khác nhau. Cách dùng phổ biến là lưu dữ liệu tạm trong bộ nhớ trên `StateFileBackend`, với phân vùng `/memories/` bền vững trên `PostgresFileBackend`, để ghi chú đã chắt lọc được giữ lại còn việc lưu các kết quả lớn vẫn ít tốn kém. Cung cấp tường minh phân vùng `/memories/` này cho cấu hình sẵn deep agent — xem [./deep-agent.md](./deep-agent.md).

Khi không cung cấp backend, agent tạo một `StateFileBackend` mới cho mỗi session. Mọi instance backend được cung cấp tường minh, bao gồm `CompositeFileBackend` trong ví dụ trên và `StateFileBackend` mặc định của nó, đều được chia sẻ giữa các session theo thiết kế.

`DiskFileBackend` chạy I/O tệp trên một luồng daemon có giới hạn; khi được khởi tạo, nó phân giải và tạo thư mục gốc. `PostgresFileBackend` dùng asyncpg và import thư viện đó khi được khởi tạo; hãy cài extra `linch[postgres]` trước, còn thao tác với pool và schema vẫn được trì hoãn đến khi cần. Cả hai đều thực hiện I/O backend bên ngoài luồng vòng lặp agent. Cơ chế tự động chuyển kết quả ra ngoài không ghi gì cho đến khi có kết quả đủ điều kiện vượt ngưỡng.

---

## Tool được đăng ký tự động

Khi hệ thống con hoạt động, bốn tool được đăng ký tự động. Agent thuần không nhận các tool này:

| Tool | Mô tả |
|---|---|
| `ls(prefix?)` | Liệt kê tệp trong hệ thống tệp ảo |
| `read_file(path, offset?, limit?)` | Đọc tệp, có thể giới hạn theo khoảng dòng |
| `write_file(path, content)` | Ghi ghi chú tạm hoặc kết quả trung gian |
| `edit_file(path, old_string, new_string, replace_all?)` | Chỉnh sửa tệp hiện có |

Mô hình được thông báo về các tool này và quy ước chuyển kết quả ra ngoài qua một khối system prompt được thêm tự động, nên bạn không phải hướng dẫn nó cách đọc lại. Khả năng giới hạn khoảng dòng bằng `offset`/`limit` của `read_file` có vai trò quan trọng ở đây: mô hình có thể chỉ đọc phần liên quan của khối dữ liệu lớn đã chuyển ra ngoài thay vì đưa toàn bộ trở lại ngữ cảnh. `write_file` và `edit_file` cũng cung cấp cho mô hình vùng ghi chép tạm trong phạm vi backend để lưu công việc trung gian vốn có thể làm hội thoại phình to.

---

## Các tùy chọn `OffloadConfig`

```python
from linch import OffloadConfig

OffloadConfig(
    enabled=True,               # master switch
    threshold_tokens=None,      # None = derive from context window (recommended)
    threshold_fraction=0.1,     # fraction used when threshold_tokens is None (10 %)
    preview_lines=10,
    path_prefix="/offload",     # virtual directory for auto-offloaded files
    skip_tools=frozenset({"read_file", "write_file", "edit_file", "ls"}),
)
```

Khi `threshold_tokens` là `None`, agent tính ngưỡng bằng `max(1_000, int(context_window * threshold_fraction))`. Giá trị được tính lại sau khi `provider.prepare()` thành công và sau khi thay provider. Nếu không thể xác định `context_window()`, việc tự động chuyển kết quả ra ngoài được bỏ qua trừ khi bạn cung cấp ngưỡng số nguyên tường minh (ví dụ `threshold_tokens=5_000`). Bạn có thể thay `skip_tools` mặc định: đó là một `frozenset` chứa tên các tool hệ thống tệp, nhưng bạn có thể truyền `frozenset` khác để thay đổi những tool có kết quả bị bỏ qua. Giảm `threshold_fraction` (hoặc đặt `threshold_tokens` nhỏ) khi bạn muốn rút gọn mạnh với cửa sổ ngữ cảnh hạn hẹp; tăng giá trị khi phần lớn kết quả đều nhỏ và bạn muốn tránh lượt gọi đọc lại.

Đặt `result_offload=None` để tắt việc tự động chuyển kết quả ra ngoài. Thao tác này không tắt các tool hệ thống tệp khi có `features=FeatureFlags(filesystem=True)` và backend `filesystem` tường minh (hoặc `fs` của môi trường thực thi). Đặt `features=FeatureFlags(filesystem=False)` để tắt toàn bộ hệ thống con. Với các cờ mặc định, hệ thống con vốn đã không hoạt động; bật nó là một quyết định tin cậy tường minh.

---

## Các trang liên quan

- [./tools.md](./tools.md) — tự động chuyển kết quả tool lớn ra ngoài ngữ cảnh
- [./deep-agent.md](./deep-agent.md) — phân vùng `/memories/` lưu trữ lâu dài
- [./context-and-memory.md](./context-and-memory.md) — giữ cửa sổ ngữ cảnh gọn nhẹ
- [../architecture/subsystems.md#39-virtual-filesystem-and-result-offload](../architecture/subsystems.md#39-hệ-thống-tệp-ảo-và-chuyển-kết-quả-ra-ngoài) — vị trí của hệ thống tệp trong vòng lặp
