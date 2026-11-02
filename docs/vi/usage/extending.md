# Tự triển khai điểm mở rộng
[Bản gốc tiếng Anh](../../usage/extending.md)

Sử dụng trang này khi bạn cần tự triển khai và đưa một điểm mở rộng vào runtime.

Linch cung cấp các điểm mở rộng trong runtime để bạn triển khai và đưa vào sử dụng. Phần lớn dùng protocol theo cơ chế duck typing: đối tượng của bạn cần có các thuộc tính và phương thức tương ứng. Các provider tích hợp sẵn kế thừa `BaseProvider`, một lớp cơ sở trừu tượng; provider tùy chỉnh cũng có thể đáp ứng giao diện runtime dựa trên cấu trúc. Trang này bổ sung cho các mẫu đã được mô tả cho
[`ExecutionBackend`](../../usage/tools.md#bash-execution-backend),
[`Tool`](../../usage/tools.md#custom-tools), [`FileBackend` và `ContextBuilder`](./context-and-memory.md),
provider và [hook](../../usage/hooks.md).

Một đối tượng đáp ứng protocol dựa trên cấu trúc: triển khai các phương thức được liệt kê với chữ ký tương ứng, rồi truyền đối tượng vào vị trí của adapter tích hợp sẵn. Runtime kiểm tra các phương thức tùy chọn bằng `getattr`/`hasattr`; hãy triển khai các khả năng tùy chọn theo đúng ngữ nghĩa trong tài liệu.

Import các hợp đồng mở rộng và thành phần chính sách được hỗ trợ từ package cấp cao nhất.
Ví dụ, chính sách quyền tùy chỉnh có thể tạo các quy tắc có thứ tự mà không phụ thuộc vào đường dẫn module riêng tư:

```python
from linch import BashRule, PathRule, ToolRule
```

Các hàm kiểm tra hợp đồng dùng lại được có sẵn cho những điểm mở rộng có các bất biến khó nhận thấy của kho lưu trữ bất đồng bộ:

```python
import pytest
from linch import (
    assert_file_backend_contract,
    assert_isolation_backend_contract,
    assert_mailbox_contract,
    assert_tool_contract,
)

@pytest.mark.asyncio
async def test_my_file_backend_contract(tmp_path):
    await assert_file_backend_contract(lambda: MyFileBackend(tmp_path / "files.db"))

@pytest.mark.asyncio
async def test_my_isolation_backend_contract(tmp_path):
    await assert_isolation_backend_contract(lambda: MyIsolationBackend(root=tmp_path))

@pytest.mark.asyncio
async def test_my_mailbox_contract(tmp_path):
    await assert_mailbox_contract(lambda: MyMailbox(tmp_path / "mailbox.db"))

@pytest.mark.asyncio
async def test_my_tool_contract():
    await assert_tool_contract(
        MyTool(),
        valid_input={"query": "contract smoke"},
        invalid_input={},
    )
```

Các hàm hỗ trợ backend, isolation và mailbox nhận một factory trả về adapter mới, rỗng; `assert_tool_contract` nhận một instance tool. Chúng kiểm tra cùng các bảo đảm hành vi mà các adapter tích hợp sẵn của Linch dựa vào: ngữ nghĩa CRUD/liệt kê/chỉnh sửa của file backend, các workspace cô lập riêng biệt có thể ghi, thao tác lấy hết rồi xóa thông điệp khỏi mailbox, không mất thông điệp khi gửi đồng thời, và cấu trúc metadata/kiểm tra đầu vào/thực thi/tài nguyên của tool tùy chỉnh.

Các tệp mẫu có thể sao chép nằm trong
[`examples/extensions/`](../../../examples/extensions/): mẫu package provider và tool. Chúng luôn được duy trì ở trạng thái có thể import và được kiểm thử cơ bản để theo sát các protocol công khai.

---

## `IsolationBackend` — nơi subagent chạy

Subagent có thể chạy trong một thư mục làm việc cô lập để các thao tác ghi tệp không bao giờ xung đột với agent cha hoặc subagent ngang hàng. `tools/isolation.py` cung cấp `TempDirIsolation`; hãy triển khai protocol để dùng container, sandbox từ xa hoặc workspace lấy từ một pool làm môi trường cô lập.

```python
from typing import Protocol

class IsolationBackend(Protocol):
    async def acquire(self) -> str: ...                      # returns a cwd path
    async def release(self, cwd: str, *, keep: bool = False) -> None: ...
```

- `acquire()` cấp phát workspace và trả về `cwd` của nó. Subagent chạy tại đó.
- `release(cwd, keep=...)` thu hồi workspace. `keep=True` yêu cầu backend giữ lại workspace (ví dụ để kiểm tra khi có lỗi) thay vì xóa nó.

```python
class GitWorktreeIsolation:
    def __init__(self, repo: str) -> None:
        self._repo = repo

    async def acquire(self) -> str:
        path = await _make_worktree(self._repo)   # your provisioning
        return path

    async def release(self, cwd: str, *, keep: bool = False) -> None:
        if not keep:
            await _remove_worktree(cwd)
```

Đưa đối tượng này vào luồng tạo subagent (xem [deep-agent](./deep-agent.md)). `acquire` phải trả về thư mục dùng được và có thể ghi. Bạn nên làm cho `release` có tính lũy đẳng để việc dọn dẹp lặp lại vẫn an toàn; protocol và hàm kiểm tra hợp đồng không bắt buộc điều này.

---

## `Mailbox` — thông điệp ngang hàng giữa các agent

Mailbox là nền tảng trao đổi thông điệp giữa các agent. `InMemoryMailbox` là bản triển khai tích hợp sẵn duy nhất, với trạng thái tồn tại trong vòng đời của tiến trình. Hãy triển khai protocol để có hộp thư bền vững hoặc hoạt động giữa nhiều tiến trình trên hạ tầng do ứng dụng chủ quản lý; adapter mailbox nằm ngoài SDK lõi.

```python
from linch import Mailbox, MailboxMessage

message = MailboxMessage(
    sender="planner",
    recipient="worker",
    content="Inspect the request handler.",
)
await mailbox.send(message)
messages = await mailbox.drain("worker")
```

`Mailbox` định nghĩa `async send(message) -> None` và
`async drain(recipient: str) -> list[MailboxMessage]`. `MailboxMessage` tự tạo
`id`; các trường tùy chọn có giá trị mặc định là `type="message"`, `request_id=None`
và `in_reply_to=None`.

Hai bất biến mà bản triển khai của bạn **phải** bảo đảm:

- **`drain` lấy rồi xóa thông điệp và có tính nguyên tử** — một thông điệp được giao cho đúng một lần gọi drain. Hai lần gọi drain đồng thời trên cùng hộp thư không được cùng nhìn thấy thông điệp đó.
- **Các lần gọi `send` đồng thời không bao giờ làm mất thông điệp** — bảo vệ hộp thư của từng người nhận bằng khóa để mọi lần gửi xen kẽ đều đến nơi.

`sender`/`recipient` là các địa chỉ mà runtime không diễn giải (id session, `display_name` của worker hoặc bất kỳ định danh nào bạn chọn). `type` là một phân loại trung lập do *bạn* diễn giải. Kết hợp với hàm hỗ trợ `Correlator` để ghép `request_id` với phản hồi có `in_reply_to` tương ứng.

---

## Lập lịch và worker xử lý hàng đợi

Package tùy chọn `linch.worker` cung cấp lịch chạy bền vững và cơ chế điều phối run. Ứng dụng chủ đăng ký các tool lập lịch của package và cấu hình PgQueuer; xem
[Mở rộng quy mô](./production.md#mở-rộng-theo-chiều-ngang).

---

## `Limiter` — kiểm soát mọi lệnh gọi provider thực tế

Xử lý thử lại mang tính phản ứng: `providers/retry.py` tuân theo `Retry-After` sau khi đã nhận phản hồi 429. `Limiter` là cơ chế chủ động tương ứng — phần lõi giữ một suất của limiter trong suốt mỗi lệnh gọi provider thực tế, nhờ đó bạn điều tiết trước khi provider áp giới hạn.

```python
from typing import Protocol

class Limiter(Protocol):
    async def acquire(self, *, model: str) -> None: ...
    def release(self, *, model: str) -> None: ...
```

Một giới hạn số lệnh gọi đồng thời đơn giản chỉ cần vài dòng:

```python
import asyncio

class SemaphoreLimiter:
    def __init__(self, cap: int) -> None:
        self._sem = asyncio.Semaphore(cap)

    async def acquire(self, *, model: str) -> None:
        await self._sem.acquire()

    def release(self, *, model: str) -> None:
        self._sem.release()

agent = Agent(..., limiter=SemaphoreLimiter(8))
```

Tạo semaphore theo cơ chế khởi tạo trễ ở lần `acquire` đầu tiên nếu `Agent` có thể được tạo bên ngoài một vòng lặp đang chạy. Với trường hợp phổ biến đó, `Agent(max_provider_concurrency=8)` là cách viết tắt — nó tạo limiter này cho bạn; truyền cả hai sẽ phát sinh `ConfigError`.

**Hiểu rõ sự ràng buộc với vòng lặp.** `asyncio.Semaphore` gắn với vòng lặp ở lần acquire đầu tiên *phải chờ tranh chấp*, nên mẫu trên gặp lỗi khi `Agent` được dùng lại từ vòng lặp thứ hai — và chỉ khi các lệnh gọi bắt đầu xếp hàng, khiến lỗi chỉ xuất hiện khi có tải. `max_provider_concurrency` xử lý việc này cho bạn: nó tạo lại semaphore khi vòng lặp thay đổi và không có suất nào đang được giữ, đồng thời phát sinh `ConfigError` thay vì tạo lại khi vẫn còn suất chưa được trả, vì làm vậy sẽ cấp cho vòng lặp thứ hai một budget đầy đủ riêng. Limiter tự triển khai cần hoạt động trên nhiều vòng lặp chạy đồng thời phải dùng một thành phần đồng bộ không gắn với vòng lặp.

`model` được truyền vào để một limiter có thể duy trì budget riêng cho từng model: token bucket được phân theo model sẽ bổ sung token theo số yêu cầu mỗi phút của model đó, và `acquire` chờ một token. Vì `acquire` được await, việc hủy tự động lan truyền qua nó — run bị hủy sẽ ngừng chờ thay vì chiếm chỗ trong hàng đợi.

Có hai vị trí cần lưu ý. Cơ chế kiểm soát nằm *bên trong* generator của lượt, nên suất được giải phóng trong thời gian chờ trước khi thử lại với cùng model, và được giải phóng một cách xác định khi bên gọi bỏ run. Nó cũng bao quanh `strategy.compact(...)` tại điểm gọi duy nhất, nên `CompactionStrategy` tùy chỉnh cũng chịu giới hạn mà chữ ký `compact(ctx, provider)` không cần biết về limiter.

Limiter thuộc từng `Agent`, nên mặc định N agent trong một tiến trình độc lập với nhau. Chia sẻ một instance giữa các agent là cách cấp cho một tenant một budget duy nhất trên nhiều agent — đây là lựa chọn có chủ đích.

---

<a id="reversible-registration--every-registration-returns-a-disposable"></a>

## Đăng ký có thể hoàn tác — register và replace trả về `Disposable`

`ToolRegistry.register()` và `replace()` trả về một đối tượng `linch.Disposable` để hoàn tác, cho phép ứng dụng chủ thêm tool trong phạm vi một tác vụ rồi gỡ đăng ký đó sau khi xong. Còn `unregister()` trả về tool đã gỡ hoặc `None`.

```python
from linch import Disposable  # only needed for a type annotation

disposer = agent.tools.register(MyTool())   # -> Disposable
...
await disposer.dispose()                     # removes exactly that registration
await disposer.dispose()                     # single-use: a no-op
```

Ba thuộc tính giúp bạn yên tâm dựa vào cơ chế này:

- **Lũy đẳng.** Một lần `dispose()` thành công là kết thúc; lần gọi thứ hai không làm gì.
- **Kiểm tra danh tính đối tượng.** Hủy đăng ký tool chỉ gỡ tool nếu mục hiện tại vẫn là đối tượng đã đăng ký, nên disposer cũ không bao giờ xóa nhầm kết quả của lần `replace` sau đó.
- **Có thể thử lại khi lỗi.** Nếu quá trình thu dọn phát sinh ngoại lệ, disposer vẫn còn hiệu lực để lần đóng sau có thể hoàn tất việc dọn dẹp.

`register` từ chối tên trùng; dùng `replace` để thay thế nóng có thể hoàn tác.
Hợp đồng tương tự áp dụng cho các dịch vụ đăng ký trên `Context` của agent
(`ctx.register(key, service) -> Disposable`) và các listener của event. Để gắn bước thiết lập với thu dọn, `ctx.effect(fn)` chạy `fn` ngay và thu thập disposer mà hàm trả về; khi phạm vi đóng, nó hủy disposer đó (và mọi disposer được thêm sau nó) theo thứ tự ngược. Xem [kernel](../../architecture/kernel.md).

---

## Pipeline của tool — bọc `tools/execute`

Mỗi `Agent` sở hữu một `ToolPipeline` (`agent.tool_pipeline`) cung cấp ba điểm mở rộng quanh một lệnh gọi tool — `tools/pre-execute → tools/execute →
tools/post-execute`. Đây là nơi gắn các hành vi xuyên suốt (chỉ số đo lường, thời hạn trong phạm vi pipeline, tracing), thay vì sửa scheduler.

Điểm mở rộng `tools/execute` là một **chuỗi waterfall**: mỗi wrapper nhận một `next` và phải `await next()` để chạy phần được bọc (`tool.execute` thực tế là lệnh gọi trong cùng). Bao quanh `next()` để thêm hành vi:

```python
import time
from linch import ToolExecution   # public; the payload threaded through the seam

def metrics_wrapper(record):
    async def wrapper(execution: ToolExecution, next):
        start = time.perf_counter()
        error = None
        try:
            return await next()                     # run the wrapped body
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            record(execution.tool_name, time.perf_counter() - start, error)
    return wrapper

disposer = agent.tool_pipeline.on_execute(metrics_wrapper(my_sink))  # -> Disposable
```

`on_pre_execute(listener)` và `on_post_execute(listener)` đăng ký hai điểm mở rộng còn lại; cả ba đều trả về `Disposable`. Những điểm cần biết:

- **Không có chi phí bổ sung khi không dùng.** Khi cả ba giai đoạn đều không có listener, scheduler gọi thẳng `tool.execute`.
- **Payload chỉ được đọc đối với việc cấp quyền.** `ToolExecution` chứa `tool_name`, `tool_use_id`, `input`, `ctx`, `tool`, cùng các trường mở rộng `signal`/`metadata`. Pipeline chạy *sau* bước kiểm tra hợp lệ chuẩn và kiểm tra quyền, nên listener **không được** sửa `tool`, `input` hay quyết định cấp quyền — nếu có, scheduler từ chối thực thi để bảo đảm an toàn. Phép biến đổi ảnh hưởng đến cấp quyền thuộc về hook `PreToolUse`, nơi đầu vào được kiểm tra hợp lệ và kiểm tra quyền lại.
- **Bỏ qua `next()` đồng nghĩa phủ quyết.** Trả về từ wrapper mà không await `next()` sẽ chủ động ngắt phần thân — hãy dùng để chặn lệnh gọi có chủ đích, tránh vô tình bỏ qua.

---

## `ExecutionBackend` — một môi trường shell + hệ thống tệp thống nhất

`ExecutionBackend` kết hợp thực thi shell và truy cập hệ thống tệp. Thay một đối tượng sẽ thay cả hai cùng lúc. Đây là protocol theo cơ chế duck typing, không có lớp cơ sở:

```python
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class ShellBackend(Protocol):
    async def run(self, command: str, *, cwd: str, timeout_s: float,
                  signal: Any = None) -> "ExecResult": ...

@runtime_checkable
class ExecutionBackend(Protocol):
    shell: ShellBackend   # the subprocess half
    fs: "FileBackend"     # the filesystem half (see FileBackend on the tools page)
```

`LocalExecutionBackend` cung cấp tiến trình con cục bộ và tệp trên đĩa; `Agent` không có cấu hình bổ sung không mặc nhiên được cấp quyền thực thi này.
`RemoteExecutionBackend(shell=..., fs=...)` kết hợp các lớp truyền tải do bạn cung cấp (ví dụ một sandbox). `BashTool` lấy shell từ backend và các tool hệ thống tệp đọc `fs` của backend, nên chỉ cần một đối số để trỏ agent vào sandbox:

```python
from linch import Agent, LocalExecutionBackend

cwd = "."
agent = Agent(..., cwd=cwd, execution_backend=LocalExecutionBackend(cwd=cwd))
```

Tạo môi trường cục bộ với `cwd=Agent.cwd` để shell và hệ thống tệp dùng chung workspace (không khớp sẽ phát sinh `ConfigError`). `execution_backend` phải cung cấp cả `.shell` lẫn `.fs`; giá trị chỉ có shell sẽ bị từ chối. Ghép lớp truyền tải shell như `DockerBackend` với lớp truyền tải hệ thống tệp cho cùng môi trường thực thi thông qua `RemoteExecutionBackend(shell=..., fs=...)`. Nếu truyền rõ `filesystem=`, giá trị này phải chính là đối tượng `.fs` của backend.

**`execution_backend=` chuyển giao quyền sở hữu.** Agent đóng môi trường đó khi gọi `Agent.close()`, nên *không* đưa cùng một đối tượng cho hai agent — lần đóng đầu tiên sẽ khiến agent thứ hai dùng các lớp truyền tải đã đóng. Để chia sẻ một môi trường, hãy đăng ký nó trên `Context` và để từng agent kế thừa:

```python
from linch import Context, RemoteExecutionBackend

host = Context(label="host")
host.register("execution", RemoteExecutionBackend(shell=..., fs=...))

first = Agent(..., context=host)
second = Agent(..., context=host)   # same world, borrowed not owned
await first.close()                 # the shared world stays open
```

Môi trường được kế thừa là môi trường mượn: đóng một agent chỉ giải phóng phạm vi con của chính nó. Đây cũng là cách subagent nhận môi trường của agent cha
(`ctx.get("execution")`), trừ khi bạn cấp môi trường riêng cho nó.

---

## Lắp ghép system prompt — phần đầu tĩnh có thứ tự + các khối động

Quá trình lắp ghép system prompt dùng hai điểm mở rộng cho các phần tĩnh và ngữ cảnh động:

1. **Các phần tĩnh có thứ tự** — `SystemPromptConfig.sections` là danh sách
   `SystemPromptSection(name, text, cacheable=True)`. Vị trí mặc định là
   `"before_defaults"`; `"after_defaults"` và `"after_env"` cũng được hỗ trợ.
   Các phần giữ nguyên thứ tự khai báo trong mỗi vị trí. Với Anthropic, khối system cuối cùng có thể cache sẽ đặt điểm ngắt: riêng `cacheable=False` không loại một phần khỏi cache nếu có khối phía sau có thể cache.

   ```python
   from linch import SystemPromptConfig, SystemPromptSection

   agent = Agent(
       ...,
       system_prompt_config=SystemPromptConfig(
           append="You are a research analyst.",
           sections=[
               SystemPromptSection(name="policy", text="Cite sources for factual claims."),
           ],
       ),
   )
   ```

2. **Các khối động theo từng lượt** — `ContextBuilder` có thể tạo các khối *system* tạm thời, được tính lại mỗi lượt (đoạn trích RAG, ký ức được truy xuất, trạng thái hiện tại). Các khối này chỉ được thêm vào yêu cầu gửi provider và bị buộc đặt `cacheable=False`. Đây là điểm mở rộng cho mọi nội dung thay đổi giữa các lượt — xem [Ngữ cảnh và bộ nhớ](./context-and-memory.md).

Ngày được tạo cho từng tin nhắn của người dùng. Ký ức được truy xuất và ngữ cảnh khác có thể được đưa vào qua các khối system động. Dùng `ContextBuilder` khi bạn cần nội dung tính toán theo từng lượt; dùng `sections` khi bạn cần các khối tĩnh có thứ tự với ranh giới cache tường minh.

---

## Chính sách bộ nhớ tệp

Bộ nhớ tệp chủ đích không có hook trích xuất hay hợp đồng kho lưu trữ bộ nhớ. Dùng `FileMemoryContextBuilder` công khai với `ContextInjectionHook`, và giữ `ScopedFileBackend` của run trong `RunOptions(deps={"filesystem": scoped_backend})`.
Builder đọc `/memories/MEMORY.md` một lần mỗi run và đưa vào 200 dòng đầu tiên; nó không ghi bộ nhớ. Model có thể duy trì tệp qua các tool hệ thống tệp, còn chính sách của ứng dụng có thể cập nhật hoặc xóa nội dung lỗi thời qua backend. Giữ `MEMORY.md` dưới dạng chỉ mục với một mục cho mỗi tệp; đây là hướng dẫn trong prompt, không phải định dạng do builder bắt buộc.
Để truy xuất theo ngữ nghĩa, điều chỉnh công thức trong
[`examples/memory/mem0_recall.py`](../../../examples/memory/mem0_recall.py) với client bất đồng bộ giả trong kiểm thử đơn vị.

---

## Các trang liên quan

- [Tool](../../usage/tools.md) — các điểm mở rộng `Tool`, `ExecutionBackend`, `FileBackend`.
- [Ngữ cảnh và bộ nhớ](./context-and-memory.md) — `ContextBuilder`, bộ nhớ tệp và backend có phạm vi.
- [Hook](../../usage/hooks.md) — lớp điều phối duy nhất cho các adapter vòng đời và chính sách.
- [Quản lý phiên bản](../../versioning.md) — ý nghĩa của thay đổi protocol đối với semver.
