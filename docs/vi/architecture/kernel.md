# Kernel

[Bản gốc tiếng Anh](../../architecture/kernel.md)

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Dùng trang này khi bạn cần hiểu hoặc mở rộng các dịch vụ theo phạm vi, hiệu ứng
có thể hoàn tác, cơ chế phân phối vòng đời có kiểu, plugin, profile hoặc pipeline tool.

Kernel (`src/linch/kernel/`) là một lớp nhỏ, không có phụ thuộc,
cung cấp cho Linch ba nền tảng mà phần còn lại của SDK xây dựng dựa trên: **hiệu ứng
có thể hoàn tác**, **bus event có kiểu** và **phạm vi `Context` riêng cho từng agent**
(một IoC container theo phạm vi). Lớp này ưu tiên bất đồng bộ và không kèm phụ thuộc bên thứ ba.

Các tên công khai: `linch.Context`, `linch.Disposable` và `linch.Plugin`.
`EffectScope`, `EventBus` và `Next` là các thành phần hỗ trợ ở phía triển khai,
không phải tên công khai ở cấp cao nhất.

Lý do tồn tại: các điểm mở rộng (lớp bọc tool, provider cung cấp năng lực, listener)
phải **thêm và gỡ được** mà không cần sửa vòng lặp, và N agent phải chạy được
trong cùng một tiến trình mà không dùng chung biến toàn cục có thể thay đổi. Kernel thống nhất
cơ chế này thành “đăng ký → trả về một `Disposable`” và “mỗi agent sở hữu một phạm vi”.

---

## Disposable — hiệu ứng có thể hoàn tác

`Disposable` (`src/linch/kernel/effects.py::Disposable`) bọc một hàm hoàn tác. `await d.dispose()`
(hoặc `await d()`) chỉ thực hiện một lần thử dọn dẹp tại một thời điểm. Khi dọn dẹp thành công,
việc dọn dẹp kết thúc hẳn và các lời gọi sau không làm gì; nếu dọn dẹp thất bại thì vẫn có thể thử lại,
để một lần đóng sau hoàn tất việc dọn dẹp. Hàm hoàn tác có thể là coroutine. Mọi thao tác đăng ký
trong hệ thống đều trả về một đối tượng như vậy:

```python
disposer = registry.register(tool)  # returns a Disposable
...
await disposer.dispose()            # removes exactly that tool
await disposer.dispose()            # single-use: a no-op
```

Việc hoàn tác **kiểm tra định danh đối tượng**: hủy một đăng ký tool chỉ gỡ tool
nếu mục hiện tại vẫn chính là mục đã được đăng ký (vì vậy, hủy một đăng ký
đã lỗi thời không bao giờ ghi đè mất đối tượng thay thế).

`EffectScope` tập hợp các disposer và dọn dẹp chúng theo **thứ tự ngược lại**. Nó
thử chạy mọi disposer; các mục thành công giữ trạng thái hoàn tất, còn các mục thất bại
được giữ lại để thử lại sau.
`ctx.effect(fn)` chạy `fn` ngay lập tức và đăng ký disposer mà hàm trả về
(hoặc yield), để phần thiết lập và dọn dẹp nằm cùng nhau thay vì ở một khối
`finally` xa phía sau.

## EventBus — emit / serial / waterfall

`EventBus` (`src/linch/kernel/events.py::EventBus`) đăng ký listener bằng
`on(name, listener, *, prepend=False) -> Disposable` và phân phối theo ba cách:

Các thành phần phát vòng đời truyền giá trị `LifecyclePoint` có kiểu từ danh mục
vòng đời thay vì lặp lại một chuỗi. Kiểu mô tả được định nghĩa trong
`src/linch/kernel/events.py` và được danh mục đó xuất lại. Mỗi
điểm có kiểu khai báo kiểu payload và một chế độ phân phối; `EventBus` từ chối
các lời gọi `emit`, `serial` hoặc `waterfall` không khớp với chế độ đó. Các điểm hook
dùng chế độ `hook` và do `HookDispatcher` quản lý; thành phần này giữ nguyên
thứ tự và ngữ nghĩa thay đổi dữ liệu của hook do người dùng định nghĩa. Tên dạng chuỗi thuần vẫn dùng được
cho các event do ứng dụng chủ định nghĩa.

| Bộ phân phối | Ngữ nghĩa |
|---|---|
| `emit(name, *args)` | Phát rồi tiếp tục mà không chờ. Không có quyền phủ quyết; mỗi listener được cô lập bằng `try/except` riêng nên lỗi ở một listener không bao giờ làm hỏng các listener khác. Các listener coroutine được lập lịch thực thi. |
| `serial(name, *args)` | Chờ các listener theo thứ tự và **dừng ngay ở giá trị trả về đầu tiên** khác `None` và khác `False` (giá trị đó là kết quả). |
| `waterfall(name, *args, next)` | Chạy các listener như một chuỗi middleware từ ngoài vào trong (listener đăng ký đầu tiên nằm ngoài cùng). Mỗi listener có dạng `async def listener(*args, next)` và BẮT BUỘC phải `await next()` để chuyển tiếp vào bên trong; **không** gọi `next()` là chủ ý phủ quyết/dừng sớm chuỗi. |

Chọn bộ phân phối phù hợp với mục đích: `emit` để quan sát, `serial` cho
“bên phản hồi đầu tiên thắng”, `waterfall` cho các lớp bọc bao quanh (timeout, retry, metrics)
cần bọc lời gọi bên trong.

## Context — phạm vi riêng cho từng agent

`Context` (`src/linch/kernel/context.py::Context`) gộp một registry dịch vụ, một `EventBus` và một
`EffectScope` thuộc quyền sở hữu của nó. Mỗi `Agent` sở hữu một đối tượng này; các dịch vụ dùng chung được tra cứu thông qua
đó thay vì qua biến toàn cục của module — đây là điều giúp Linch an toàn khi phục vụ nhiều tenant.

- `register(key, service) -> Disposable` — thêm một dịch vụ; gọi dispose để gỡ.
  Ngoài quá trình kích hoạt plugin, việc đăng ký và gỡ sẽ phát `context/service`
  với `(context, key)` trên bus dùng chung sau khi registry thay đổi. Việc gỡ
  chờ mọi listener; các thông báo thất bại vẫn có thể thử lại thông qua cùng
  disposer đó. Khi không có listener, không có event nào được phân phối.
- `ctx.<name>` — đọc một năng lực **bắt buộc** dưới dạng thuộc tính (ví dụ:
  `ctx.execution`); phát sinh `AttributeError` nếu không tra cứu được. Tên bắt đầu bằng dấu gạch dưới gây lỗi để
  tránh đệ quy.
- `ctx.get(name, default=None)` — cùng phép tra cứu đó, nhưng **tùy chọn**: trả về *default* khi
  không tra cứu được. Cả hai đều tìm ngược lên các phạm vi cha, nên phạm vi con thấy dịch vụ mà
  cha đã đăng ký, trừ khi nó định nghĩa một dịch vụ cùng tên để che khuất.
- `on(...)` / `effect(...)` — chuyển tiếp đến bus / phạm vi.
- `inbox(producer) -> Disposable` — thêm một producer có thứ tự, cục bộ theo agent, hoạt động tại ranh giới run.
  Producer được gọi dưới dạng `producer(session, run_id)` và trả về một
  iterator bất đồng bộ của các giá trị `Message` hoặc `Event` đã được commit. Message được
  nối vào lịch sử session và phát dưới dạng `UserEvent` thông báo; Event
  được phát/lưu bền vững mà không nối thêm vào lịch sử lần nữa. Đăng ký này
  độc lập và có thể gỡ theo cách lũy đẳng; `aclose()` của iterator được
  chờ nếu có. Khi không có producer, vòng lặp không cấp phát hoặc phân phối
  coroutine inbox.
- `scope(label="scope", *, isolate_events=False) -> Context` — một phạm vi **con** dùng chung bus nhưng có
  lớp dịch vụ phủ riêng biệt và `EffectScope` con riêng, nên việc
  dọn dẹp từng agent (hoặc từng subagent) chỉ hủy nhánh đó. Với `isolate_events=True`, các đăng ký listener
  được chụp lại trên một bus độc lập; các thay đổi dịch vụ ở cha
  đã qua lọc vẫn đến được phạm vi con. `EventBus.fork()` và `ToolPipeline.fork()` đảm nhiệm
  cơ chế sao chép listener và giữ nguyên thứ tự đăng ký cùng quyền sở hữu.

`Agent` tạo một `Context` riêng thuộc quyền sở hữu của nó cho suốt vòng đời. Khi truyền `context=`,
context đó là phạm vi cha/ứng dụng chủ, còn context của Agent là
phạm vi con của nó; nếu không truyền, Agent tạo context gốc riêng. Phạm vi cha được truyền vào
vẫn tồn tại khi Agent đóng; ứng dụng chủ cũng có thể truyền
`parent.scope("agent")` khi muốn tạo phạm vi con một cách tường minh. Agent đăng ký
pipeline và, khi được cấu hình, môi trường thực thi trong phạm vi thuộc quyền sở hữu của nó.
Session và agent con tra cứu năng lực thông qua phạm vi;
chúng không dùng dịch vụ toàn cục của tiến trình. `await agent.close()` trước hết đưa
agent về trạng thái ngừng hoạt động rồi hủy context. Việc dọn dẹp context có tính giao dịch:
các đăng ký đã xử lý thành công không bị lặp lại, trong khi disposer thất bại vẫn
có thể thử lại và một lần `close()` sau sẽ tiếp tục phần dọn dẹp còn dang dở.

### Plugin

`Plugin` là một protocol theo kiểu duck typing với `name`, `version`, `provides`,
`requires` và `setup(ctx, config) -> Disposable` đồng bộ. Gắn plugin bằng
`await agent.use(plugin, config)`. Một bản gắn ở trạng thái chưa hoạt động cho đến khi mọi
dịch vụ bắt buộc đều tra cứu được trong context của agent; nó nhận một phạm vi con mới cho mỗi
lần kích hoạt. Gỡ một dịch vụ bắt buộc sẽ thu hồi các dịch vụ đã công bố trước khi chờ
dọn dẹp, và khi dịch vụ đó xuất hiện trở lại, một phạm vi mới sẽ được kích hoạt. `Disposable`
trả về sẽ tháo bản gắn và thử lại việc dọn dẹp thất bại ở một lần dispose
hoặc `agent.close()` sau. `Context.register` đồng bộ phát event thay đổi
ngay lập tức đối với các đăng ký thông thường; việc đối soát kích hoạt chạy
bất đồng bộ, trong khi việc gỡ chờ dọn dẹp các thành phần phụ thuộc.

Plugin chỉ được đăng ký các dịch vụ khai báo trong `provides`, kể cả
trong các phạm vi lồng nhau. Dịch vụ chưa được công bố cho agent cho đến khi thiết lập và
kiểm tra hoàn tất, nên việc thiết lập thất bại được hoàn tác hoàn toàn. Các bản gắn plugin và đăng ký inbox
là riêng cho từng agent; dùng chung `Context` cha không làm các agent dùng chung
dịch vụ con của một bản gắn.

### Profile theo miền

`Profile` (`src/linch/profiles.py::Profile`) kết hợp các trường khai báo bất biến và các mục plugin
theo id ổn định. `RunOptions(profile=...)` chọn định nghĩa đã kết hợp. Trước khi
vòng lặp bắt đầu, session đi vào một phạm vi run: một bản sao cấu hình nông của
cùng Agent dùng chung provider, kho lưu trữ và các thành phần phụ thuộc từ ứng dụng chủ, trong khi tool,
cấu hình quyền, đăng ký hook, danh sách listener của pipeline tool,
đăng ký dừng lượt và danh sách listener event của context là cục bộ theo
run. Quá trình thiết lập profile gắn các plugin đã chọn trong phạm vi đó. Các hook hiện có và đối tượng dịch vụ
của ứng dụng chủ vẫn giữ quyền sở hữu dùng chung thông thường. Không listener nào được đăng ký
trên bus của agent cha mà không qua lọc.

Registry tool có hiệu lực lấy giao giữa các tên được cho phép/bị từ chối của profile và mọi
cấu hình ghi đè tool của session. Một lớp quyền chỉ có quy tắc từ chối củng cố hạn chế này;
các quy tắc quyền của ứng dụng chủ vẫn quyết định việc cấp quyền. Chỉ dẫn theo vai trò được nối vào
system prompt hiện có, schema dùng `OutputSchema` và giới hạn budget tạo
một `RunBudget` mới. Việc dọn dẹp giải phóng liên kết cục bộ của run và hủy
phạm vi của nó, giữ nguyên Agent của session cùng các cấu hình ghi đè tường minh, kể cả
khi iterator đóng sớm hoặc thiết lập thất bại.
Công việc tách rời giải phóng phạm vi khi tác vụ cuối cùng kết thúc; các session con
được giữ lại tiếp tục giữ dịch vụ của chúng cho đến khi được giải phóng. Thông báo đóng session và
callback của tác vụ kích hoạt việc hủy mà không cần tắt Agent. Context
cha giữ trách nhiệm thử lại nếu việc hủy thất bại. Các thay đổi dịch vụ kế thừa
được chuyển tiếp qua phạm vi cô lập của kernel để các thành phần phụ thuộc của plugin cục bộ theo run
vẫn kích hoạt và ngừng kích hoạt bình thường.

Bên gọi trực tiếp truyền cùng profile khi resume; worker tra cứu profile từ
các định nghĩa `profiles` được cung cấp khi khởi chạy từ hàng đợi và khi tiếp quản.
`src/linch/loop/contracts.py::_build_run_contract` ghi id, phiên bản và
mã băm nội dung SHA-256 của profile vào `RunContract.payload.run_options.profile`. Phép kiểm tra
tính tương thích trên toàn bộ hợp đồng hiện có từ chối trường hợp bỏ sót hoặc thay đổi định nghĩa,
kể cả thứ tự thực thi plugin. `profile=None` không thêm trường hợp đồng hay
thay đổi yêu cầu nào. Xem [profile theo miền](../usage/profiles.md) để biết API.

---

## Những thành phần xây dựng trên kernel

- **Đăng ký có thể hoàn tác** — `src/linch/tools/registry.py::ToolRegistry.register/replace` trả về một
  `Disposable` (xem [Giao thức tool](./tool-protocol.md)).
- **Pipeline tool mở** — `ToolPipeline` (`src/linch/tools/pipeline.py::ToolPipeline`) cung cấp
  `tools/pre-execute → tools/execute → tools/post-execute`. `tools/execute` là một
  **waterfall** để các lớp bọc (`src/linch/tools/wrappers/`: metrics, timeout) bọc quá trình
  phân phối bên trong. Mỗi handle `next` chỉ được dùng một lần: một lớp bọc phải await nó đúng
  một lần, và việc bỏ qua nó là chủ ý dừng sớm chuỗi. Scheduler chỉ đưa mỗi
  lời gọi qua pipeline khi có listener gắn vào; nếu không, nó gọi
  `tool.execute` trực tiếp, nên hành vi giống hệt từng byte khi không sử dụng pipeline. Các
  listener đang hoạt động cần định danh chính sách ổn định để resume từ trạng thái lưu bền vững.
  Pipeline chạy sau bước kiểm tra hợp lệ đầu vào chuẩn và đánh giá quyền;
  listener của nó phải coi `ToolExecution.input`, `tool` và các trường context
  mang thẩm quyền là bất biến. Các phép viết lại có ảnh hưởng đến cấp quyền thuộc về
  `PreToolUse`, nơi đầu vào được kiểm tra hợp lệ và kiểm tra lại trước bước kiểm tra quyền.
- **Ranh giới năng lực** — `ExecutionBackend` (`src/linch/execution/backend.py::ExecutionBackend`)
  gộp một transport `shell` và một transport `fs` (hệ thống tệp) thành một môi trường.
  `LocalExecutionBackend` cung cấp shell cục bộ trên máy chủ và hệ thống tệp trên đĩa khi
  được tạo tường minh; nó không được cài ngầm trên một `Agent` trống.
  `RemoteExecutionBackend` gộp các transport được cung cấp. Các thành phần sử dụng (ví dụ:
  `BashTool`) phụ thuộc vào ranh giới này, thay vì một backend cụ thể (Định nghĩa → Provider
  → Thành phần sử dụng).
- **Tương thích tiến của event** — các event đã lưu bền vững nhưng chưa được nhận biết và có nhãn `ignorable` có thể
  được đọc vào rồi ghi ra thông qua một sentinel `IgnorableEvent` thay vì làm quá trình tải bị hủy (xem
  [Phân loại event](./events.md)).

## Cơ sở thiết kế

- **Đăng ký là hiệu ứng.** Một tính năng bật được thì phải tắt
  được; trả về `Disposable` giúp việc hoàn tác thống nhất và lũy đẳng thay vì
  phải thiết kế riêng cho từng registry.
- **Mở rộng trên bus, đừng sửa vòng lặp (Open–Closed).** Hành vi tool mới là
  một listener, thay vì một nhánh mới phải đưa xuyên qua `src/linch/scheduler.py`.
- **Phạm vi riêng cho từng agent, thay vì biến toàn cục.** `Context` là cơ chế phục vụ nhiều tenant:
  trạng thái dùng chung được giới hạn trong một cây agent và bị hủy cùng cây đó.

---

Quay lại [mục lục kiến trúc](./README.md).
