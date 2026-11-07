# Cấu hình tối thiểu cho môi trường production

[Bản gốc tiếng Anh](../../usage/production.md)

Dùng danh sách kiểm tra này khi tích hợp Linch vào một dịch vụ có các run được lưu bền vững và xử lý tắt dịch vụ.

Trang này là bản tóm tắt cách tích hợp Linch vào một dịch vụ async chạy dài hạn:
lưu trữ bền vững, streaming, có con người tham gia, resume sau khi khởi động lại,
tắt dịch vụ êm và các biện pháp giới hạn phạm vi ảnh hưởng. Mỗi phần đều liên kết đến trang
có thông tin đầy đủ — đây là danh sách kiểm tra cấu hình, không phải phần giải thích lại.

Linch là một **thư viện**. Dịch vụ của bạn quản lý tiến trình và vòng đời HTTP;
Linch quản lý việc thực thi agent. Runtime `linch.worker` tùy chọn phân phối
các run và công việc định kỳ trên một cụm worker.

Để dùng cơ sở dữ liệu cục bộ, hãy làm theo
[hướng dẫn thiết lập Postgres bằng Docker](../../usage/quickstart.md#5-keep-state-when-the-process-restarts).
Ứng dụng chủ đọc `LINCH_POSTGRES_DSN` và truyền giá trị đó cho từng store; SDK không
tự động đọc biến này.

## 1. Tạo agent một lần rồi tái sử dụng

Một `Agent` giữ cấu hình, registry, context và các session đang hoạt động —
hãy tạo nó khi khởi động và dùng chung giữa các request sử dụng cấu hình đó.
Dùng các agent riêng khi các tenant cần cấu hình provider, tool hoặc chính sách
khác nhau; danh tính và dependency của từng run có thể vẫn giới hạn trong phạm vi request.

```python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

dsn = "<Postgres DSN>"

agent = Agent(
    model="gpt-5",                         # the default provider is OpenAI
    session_store=PostgresSessionStore(dsn),  # conversation state
    run_store=PostgresRunStore(dsn),         # checkpoints and run events
    cwd="var/workspace",
)
```

- `session_store` lưu bền vững lịch sử hội thoại; `run_store` lưu bền vững các
  checkpoint và event của từng run (bắt buộc để resume bền vững). Các adapter lưu trữ bền vững
  tích hợp sẵn dùng Postgres; `InMemorySessionStore` và `InMemoryRunStore` dành cho
  kiểm thử và phát triển. Cài các adapter Postgres bằng extra `[postgres]`.
- Hệ thống tệp ảo bị tắt theo mặc định. Kết quả được chuyển ra ngoài được giữ trong
  session store, nên `PostgresSessionStore` mang chúng qua các tiến trình. Nếu các tệp do mô hình ghi
  phải tồn tại qua nhiều session hoặc tiến trình, hãy bật tính năng hệ thống tệp và
  dùng `PostgresFileBackend` trong [Hệ thống tệp](./filesystem.md).
- Xem [Agent và session](./agent.md).
- Để tìm hiểu lưu trữ bền vững run bằng Postgres, lease worker tùy chọn và kết nối lại stream theo
  số thứ tự, xem [Run store](./run-stores.md).

## 2. Stream event về ứng dụng chủ

Một run là một async generator tạo ra các event có kiểu. Chuyển tiếp chúng đến UI/log/hàng đợi
ngay khi nhận được — đừng chờ run kết thúc.

```python
from linch import ResultEvent

session = await agent.session()
async for event in session.run(user_prompt):
    await publish(event)            # your transport
    if isinstance(event, ResultEvent):
        final = event.final_text
```

Xem [Event](./events.md). Để có thông tin tổng quan ngắn gọn về tình trạng sau run (các
tool chậm/lỗi, áp lực context, chi phí và bộ đếm khôi phục), hãy tạo một
[`RunReport`](./events.md).

Khi dùng run store, mỗi session chỉ được có một run chưa kết thúc trên các
process. Run mới bị từ chối bằng `SessionBusyError` công khai; `run_id` của lỗi
xác định run hiện có và `session_id` xác định session. Các trạng thái `pending`,
`running`, `waiting_permission` và `suspended` đều giữ session bận. Run hoàn tất,
thất bại hoặc bị hủy sẽ giải phóng session. Ứng dụng chủ phải xếp hàng các prompt
đến sau hoặc báo bận cho bên gọi; mặc định Linch từ chối các run chồng lấn.
Subagent tạo session riêng nên có thể chạy khi session cha đang bận.

## 3. Cho con người tham gia với trạng thái bền vững

Khi một tool cần phê duyệt, Linch yield một `PermissionRequestEvent` trước khi
gọi callback `can_use_tool` đã cấu hình. Event là thông báo, không phải
kênh phản hồi: sau khi bên tiêu thụ nhận được nó, việc tiếp tục iterator
sẽ gọi callback; callback phải trả về quyết định trước khi tool thực thi. Hãy để callback
chờ UI hoặc dịch vụ chính sách của bạn. Khi không có callback, yêu cầu hỏi ý kiến bị từ chối. Các
quyết định cho phép/từ chối đã được đưa ra được ghi vào checkpoint và phát lại sau khi một run bị gián đoạn
resume. Với run bền vững, hãy cung cấp cho callback một
`resume_policy_id` ổn định (và `resume_policy_version` khi cần) để hợp đồng run
có thể được xác minh sau khi khởi động lại.

```python
from linch import PermissionRequestEvent

async for event in session.run(prompt):
    if isinstance(event, PermissionRequestEvent):
        # Notify the UI; can_use_tool owns the eventual allow/deny response.
        await publish_approval(event.requests, session_id=session.id,
                               run_id=session.active_run_id)
```

Quyền và quyết định bền vững: [Tool](../../usage/tools.md#permissions) và
phần quyền trong [Agent và session](./agent.md).

## 4. Resume sau khi khởi động lại

Với `run_store`, một run chưa kết thúc bị gián đoạn do triển khai hoặc sự cố sẽ resume
từ checkpoint cuối cùng sau khi hợp đồng run hiện tại được xác minh. Tạo lại
agent với cùng các store bền vững và chính sách phê duyệt, rồi kết nối lại
session gốc bằng ID. Các quyết định về quyền và kết quả hoàn tất tool đã được lưu bền vững
sẽ được phát lại; một tác động bên ngoài xảy ra trước khi bản ghi hoàn tất của nó được
lưu bền vững có thể được thực thi lại. Các tích hợp tool phải dùng `ctx.idempotency_key` để
đối soát ranh giới thực thi ít nhất một lần này. Một tool có event bắt đầu đã được lưu bền vững nhưng
event kết thúc chưa được lưu sẽ không tự động chạy lại: model nhận một kết quả lỗi
cho biết việc thực thi đã bị gián đoạn.

```python
session = await agent.session(id=session_id)  # reattach the original session
async for event in session.resume(run_id):
    await publish(event)
```

Worker khôi phục sau sự cố qua cơ chế PgQueuer phân phối lại job bị đình trệ và
resume checkpoint như mô tả bên dưới. Thực thi trong process không có bộ thu hồi
run sau sự cố: một hàng chưa kết thúc vẫn chặn run mới. Ứng dụng chủ nên resume
`SessionBusyError.run_id` trên session gốc, hoặc đối soát các tác động bên ngoài
và gọi `run_store.mark_failed(run_id, error={"message": ...})` nếu bỏ run đó.
Hủy task cũng tính là gián đoạn, kể cả khi client ngắt kết nối làm hủy handler
stream. Để kết thúc run khi client ngắt và giải phóng session, hãy gọi
`session.abort()` rồi tiếp tục tiêu thụ tới `ResultEvent` cuối cùng; hủy iterator
không tương đương với abort kết thúc run.

Resume yêu cầu trạng thái wire-version-3 hiện tại, một checkpoint và
hợp đồng thực thi khớp nhau. Các run đã kết thúc, run không có checkpoint và trạng thái trước 3.0,
thiếu phiên bản, có phiên bản không xác định hoặc không có hợp đồng đều bị từ chối; hãy bắt đầu
một run mới. Các store Postgres tích hợp sẵn từ chối schema hiện có không tương thích mà không
nâng cấp chúng; hãy dùng cơ sở dữ liệu mới cho schema không tương thích. Schema
phiên bản 3 tương thích vẫn dùng lại được, kể cả việc cài index tiếp nhận run.
Xem [Run store](./run-stores.md) và
[hướng dẫn chuyển đổi sang 3.0](../../migration-3.0.md).

Chỉ thay phiên bản package hoặc mô tả cấp cao nhất của tool vẫn cho phép resume
nếu phần còn lại của hợp đồng khớp nhau. Schema đầu vào/đầu ra, scope, mức đồng thời,
quyền và chính sách backend thực thi vẫn thuộc định danh. Khi nâng lên 3.1.0, hãy
ngừng nhận run mới và chờ tất cả run đang hoạt động kết thúc trước khi triển khai:
phiên bản chính sách Local/Docker đổi thành `2`, và schema Bash đã đổi. Việc wire
version 3 không đổi không cho phép resume các hợp đồng cũ đó. Hoàn tất hoặc chủ
động bỏ các run suspended và bị gián đoạn trong quá trình này; không để chúng
cho worker phiên bản mới.

## 5. Tắt dịch vụ mà không để sót công việc

Khi tắt dịch vụ, `await agent.close()` đưa các session về trạng thái ngừng hoạt động, hủy và chờ hoàn tất các
worker/tool do chúng sở hữu, rồi đóng các store, provider, backend hệ thống tệp và
hook có cung cấp thao tác dọn dẹp. Ứng dụng chủ quản lý các provider OpenTelemetry: flush và tắt
chúng sau khi các agent đã đóng. Để dừng một run đang thực thi, gọi
`session.abort()`; các lệnh gọi provider và tool có hỗ trợ dừng phối hợp sẽ quan sát tín hiệu abort của run,
và run phát ra kết quả `aborted` sau khi hoàn tất quá trình kết thúc thực thi.

```python
try:
    await serve_forever()
finally:
    await agent.close()
```

## 6. Giới hạn phạm vi ảnh hưởng

Đây là các cơ chế; bạn cung cấp chính sách.

- **Budget** — `RunOptions(budget=RunBudget(max_tokens=..., max_cost_usd=...))`
  chặn các lệnh gọi provider tiếp theo khi budget của run hoặc budget dùng chung với subagent
  đã cạn. Một lệnh gọi đã được cho phép vẫn có thể tiêu thụ lượng sử dụng mà nó báo cáo. Xem
  [Agent và session](./agent.md).
- **Bộ bảo vệ vòng lặp** — bật theo mặc định (`max_identical_tool_calls=3`,
  `max_consecutive_failures=3`); nó dừng các lệnh gọi tool giống hệt nhau lặp lại hoặc
  các lô tool liên tiếp đều thất bại hoàn toàn.
- **Quyền** — dùng `ToolRule`, `PathRule` và `BashRule` với
  chế độ quyền phù hợp; các yêu cầu hỏi ý kiến chưa có quyết định bị từ chối trừ khi
  callback `can_use_tool` đưa ra quyết định.
- **Thời gian chờ và thử lại** — `tool_timeout_ms` đặt giá trị mặc định cho agent; một tool có thể
  ghi đè bằng `execution_timeout_ms`. `tool_retry` thử lại các tool đọc và
  chỉ các tool ghi/thực thi được đánh dấu rõ là có thể thử lại; thao tác abort không bao giờ được thử lại.
- **Mức đồng thời của provider** — `Agent(max_provider_concurrency=N)` hoặc
  `Limiter` của riêng bạn cho hạn mức theo model hoặc token bucket. Đây là
  cơ chế chủ động tương ứng với thử lại: thử lại phản ứng với lỗi 429, còn limiter tránh phát sinh lỗi đó. Nó
  giới hạn các lệnh gọi provider, không phải các lệnh gọi `run()` — xem [Agent và session](./agent.md)
  và [Mở rộng](./extending.md).
- **Khôi phục khi bị cắt ngắn** — bật bằng `truncation_recovery=` (tắt theo mặc định;
  Linch không bao giờ ngầm tăng giới hạn đầu ra). Xem [Agent và session](./agent.md).
- **Che dữ liệu** — gắn một `RedactionHook` để lọc kết quả tool / câu trả lời cuối cùng
  bằng các mẫu do ứng dụng chủ cung cấp. Xem [Hook](../../usage/hooks.md).

## 7. Công việc định kỳ

Với công việc định kỳ được kích hoạt bởi cron/webhook/khoảng thời gian/CI, gọi
`LoopRunner.run_once()` từ bộ lập lịch của ứng dụng chủ — xem
[Trình chạy vòng lặp ngoài](./loop-runner.md) và
[`examples/recipes/runner_recipes.py`](../../../examples/recipes/runner_recipes.py).

## Mở rộng theo chiều ngang

Cài `linch[worker]` và import `linch.worker` một cách tường minh. Lệnh `import linch` của core
không bao giờ import PgQueuer; các tên worker không có trong `linch.__all__`.

"Worker" ở đây là tiến trình nhận job từ hàng đợi. Đây không phải pattern cho agent,
và khác với các subagent mà [tài liệu deep agent](./deep-agent.md) cũng gọi là worker.
`linch.worker` quyết định **run chạy ở đâu**. Agent, subagent và workflow quyết định
bên trong run làm gì.

Mỗi job chạy đúng một việc: một prompt đã xếp hàng, qua `session.run()` hoặc
`session.resume()` trên agent do factory của bạn trả về. Lịch chạy cũng tạo cùng loại
job này. Điều đó dẫn đến ba hệ quả:

- Subagent chạy trong tiến trình của job. Chúng không tự trải ra cả fleet; chỉ các run
  được xếp hàng riêng mới làm vậy.
- Khi run kết thúc, worker đóng session, và thao tác này hủy mọi subagent nền chưa xong.
  Trong job, hãy dùng subagent foreground, hoặc xếp hàng phần việc tiếp theo thành một
  run riêng.
- Worker không chạy `run_workflow`. Xem
  [Chạy qua nhiều tiến trình](./workflows.md#chay-qua-nhieu-tien-trinh).

Mô hình triển khai là **các tiến trình API → một Postgres → N tiến trình worker**. Các tiến trình
API gọi `enqueue_run`; chúng không chạy vòng lặp model. Postgres lưu
các session, run đang chờ, checkpoint, event có số thứ tự, định nghĩa lịch và
job PgQueuer. Các store và trình quản lý hàng đợi tái sử dụng pool asyncpg của ứng dụng chủ trong
mỗi tiến trình; mỗi tiến trình tạo pool riêng. Dành một kết nối pool cho mỗi
tiến trình worker để LISTEN và đủ kết nối bổ sung cho truy vấn và run.

```python
from linch import Agent, Principal, Profile, empty_tools
from linch.worker import Worker, WorkerJob, enqueue_run, install

await install(pool)  # once at process startup; serializes concurrent installers
support = Profile(id="support", version="1", system_prompt="Help resolve product issues.")
principal = Principal(tenant=tenant_id, user=user_id, roles={"support"})
run_id = await enqueue_run(pool, session_id, prompt, queue="provider-a",
                           meta={"tenant_id": tenant_id}, profile=support, principal=principal)

async def agent_factory(job: WorkerJob) -> Agent:
    provider = await provider_for_tenant(job.run.meta["tenant_id"])
    return Agent(
        model="your-model", provider=provider, tools=empty_tools(),
        session_store=job.session_store,
        run_store=job.run_store,
    )

worker = Worker(pool, agent_factory, queues={"provider-a": 8, "premium": 4},
                profiles=(support,), max_concurrent_runs=32,
                lease_ttl_s=30, stall_timeout_s=60, poll_interval_s=1)
await worker.run()
```

Factory là async và trả về một **agent mới cho mỗi job**. Ứng dụng chủ chọn
tenant, provider, tool, chính sách và dependency; tái sử dụng đúng `job.run_store`
và `job.session_store`. Worker sở hữu các adapter này trong toàn bộ vòng đời của nó,
xác minh schema của chúng trước khi lấy job khỏi hàng đợi và cung cấp cùng một `job.pool` mượn từ ứng dụng chủ
cho mọi factory. Adapter riêng cho từng job làm tăng việc tạo và hủy đối tượng và lặp lại quá trình khởi tạo schema.
Việc dọn dẹp agent giữ lại các adapter dùng chung này và để pool của ứng dụng chủ mở; ứng dụng
chủ đóng pool thực của nó sau khi các worker đã xử lý xong công việc đang chạy. Nếu một pool được dùng chung với
mã ứng dụng khác, hãy giữ nguyên các yêu cầu đặt lại kết nối và listener
của ứng dụng đó.

Worker kết nối lại sau lỗi cơ sở dữ liệu hoặc listener tạm thời, với độ trễ tăng theo
cấp số nhân có jitter cục bộ, tối đa hai giây. Mỗi lần thử lại tạo lại trình quản lý và
khôi phục LISTEN trên một kết nối mới mượn từ pool để thực hiện SQL hàng đợi tuần tự;
các thao tác ghi run/session dùng phần còn lại của pool ứng dụng chủ. Chu kỳ trước hoàn tất toàn bộ việc xử lý đang chạy
trước khi kết nối của nó được đóng, nên các bộ đệm lỗi chậm không thể kết nối lại và
giữ một job chưa kết thúc.
Cơ chế khôi phục job cũ hiện có sau đó thu hồi công việc bị gián đoạn. Các lỗi xác thực,
schema và cấu hình vẫn được truyền ra ngoài. Việc khởi động và lấy
kết nối bị giới hạn ở hai giây cho mỗi lần thử.

`stop()` cũng ngắt thời gian chờ kết nối lại và ngăn lấy thêm job; quá trình
khởi động/lấy kết nối đang chờ sẽ kết thúc trong giới hạn của lần thử đó. Hủy `run()` sẽ phát tín hiệu
dừng và chờ các callback đang hoạt động cùng việc dọn dẹp, kể cả khi bị hủy
nhiều lần, trước khi truyền việc hủy ra ngoài. Các job đang hoạt động không có thời hạn bắt buộc
để xử lý xong; ứng dụng chủ phải giữ pool mở cho đến khi `run()` trả về.

Pool được mượn lưu cache kết quả xác minh schema thành công cho cơ sở dữ liệu
và search path cố định của nó. Các store bổ sung trên `job.pool` tái sử dụng kết quả xác minh đó mà không
chạy SQL schema. Giữ cơ sở dữ liệu, role và search path của nó ổn định; sau khi thay đổi chúng
hoặc thay đổi schema, hãy tạo pool/worker mới để xác minh lại. Một unique index
có điều kiện thực thi giới hạn nhận run trên toàn cụm. Việc cài index thất bại
với `ConfigError` nếu session hiện có chứa nhiều run chưa kết thúc; hãy kiểm tra
và đối soát các hàng đó bằng bản triển khai cũ trước khi khởi động store/worker đã nâng cấp.

`enqueue_run` tạo một run `pending` và job của nó trong một giao dịch với
`dedupe_key="run:<run_id>"`. Run ID do bên gọi cung cấp khiến các lần gửi lặp lại
luôn là thao tác không làm gì, kể cả sau khi job trong hàng đợi đã được xác nhận. Việc tái sử dụng một ID
với session khác bị từ chối; prompt và metadata của lần gửi đầu tiên
được giữ lại. `enqueue_runs(pool, [RunRequest(...)])` gộp các thao tác chèn vào hàng đợi trong một
giao dịch. Nếu bị từ chối vì session bận, toàn bộ lô bị rollback, bao gồm các
hàng run và job trong hàng đợi. JSON của job bao gồm ID run/session, prompt, hàng đợi tùy chọn và
W3C `traceparent` của bên gọi. Worker khôi phục nó trước khi thực thi, để
span `invoke_agent` của kernel tham gia trace của bên gọi.

`enqueue_run(..., profile=..., principal=...)` và các trường `RunRequest`
tương ứng chọn cấu hình miền nghiệp vụ và danh tính dùng để phân quyền. Metadata của run
đang chờ và JSON của job chỉ lưu bền vững tham chiếu profile (id, version,
content hash) và principal đã được tuần tự hóa. Cấu hình `Worker(profiles=(...))` với
mọi định nghĩa đã phát hành mà các job của nó có thể cần. Định nghĩa bị thiếu và giá trị hash
không khớp khiến run thất bại với `ConfigError` chỉ rõ cách xử lý trước khi thực thi provider hoặc tool;
các định nghĩa và đối tượng plugin đang hoạt động không bao giờ được tuần tự hóa.

Worker truyền `PostgresRunStore` thông thường của nó cho factory và bắt đầu các run đang chờ
bằng `RunOptions(run_id=run_id, profile=resolved_profile, principal=principal)`.
Các run có checkpoint dùng `session.resume(run_id, options)` thay thế; cả hai cách
đều giữ lease mà store đã nhận quyền sở hữu. Resume và tiếp quản khôi phục danh tính từ
RunContract đã lưu, nên một bản triển khai mới phải giữ nguyên chính xác định nghĩa profile
đã ghim trong thời gian các run của nó còn có thể resume. Xem [profile miền nghiệp vụ](./profiles.md).

Mỗi hàng đợi run có tên là một entrypoint PgQueuer với
`concurrency_limit` được cơ sở dữ liệu thực thi trên **toàn bộ cụm worker**. Cấu hình cùng giới hạn trên
tất cả worker. `0` nghĩa là không giới hạn; mặc định là một hàng đợi run `default` với
mức đồng thời không giới hạn. Hàng đợi phải được đăng ký trên một worker để thực thi.
Các giới hạn này giới hạn run; dùng limiter của ứng dụng chủ cho hạn mức request/token của provider.
`Worker(max_concurrent_runs=32)` còn giới hạn số task hàng đợi đồng thời trong
mỗi process worker, độc lập với giới hạn hàng đợi trên toàn cụm. PgQueuer yêu cầu
ít nhất hai task khi batch size của worker bằng một; boolean và giá trị không
phải số nguyên bị từ chối. Điều chỉnh giới hạn theo năng lực đã đo của bản triển khai.
Callback lịch cũng dùng năng lực task hàng đợi.

**Năng lực xử lý.** Xác định quy mô cơ sở dữ liệu và pool từ benchmark riêng cho cách triển khai;
SDK không cam kết khả năng mở rộng worker tuyến tính. Dùng kết nối trực tiếp/pool theo session cho
kết nối LISTEN của worker; pool theo giao dịch cần một kết nối thông báo
riêng. Gộp bằng `enqueue_runs` khi lưu lượng tăng đột biến, giới hạn mức đồng thời của bên gửi vào hàng đợi và
đo với các bên tiêu thụ và tool được dùng trong production.

Hàng đợi chỉ phân phối công việc. Một job nhận quyền sở hữu lease của run riêng bằng
ID worker. Gia hạn bắt đầu ngay lập tức, sau đó lặp lại sau mỗi **TTL của lease / 3**.
Lỗi gia hạn tạm thời được thử lại trong TTL được xác nhận gần nhất; một lần gia hạn bị treo
bị giới hạn bởi cùng thời hạn đó. Phản hồi nhận quyền sở hữu/gia hạn thành công bắt đầu một
budget TTL mới, nên việc chờ một chỗ trong pool không tiêu tốn lease vừa nhận được.
`LeaseLostError` hoặc hết hạn mà không gia hạn thành công sẽ hủy task của run
mà không abort tín hiệu của nó hoặc ghi trạng thái kết thúc. Công việc của session được
đưa về trạng thái ngừng hoạt động trước khi giải phóng lease. Các run đã hoàn tất, thất bại và
bị abort được xác nhận mà không gọi thực thi; mọi lần thoát khi chưa kết thúc,
bao gồm việc hủy và mất quyền sở hữu, đều được thử lại từ checkpoint của nó.
Việc hủy callback hàng đợi dừng task của run đồng thời bảo vệ thao tác dọn dẹp và
kiểm tra trạng thái kết thúc, ngăn PgQueuer xác nhận công việc chưa hoàn tất.
Lỗi thiết lập factory và session ghi nguyên tử trạng thái thất bại, metadata
lỗi, một `ErrorEvent` và một `ResultEvent` cuối cùng. Khi tiếp quản, các giá trị
tổng kết cuối cùng và mức độ đầy đủ của dữ liệu sử dụng được lấy từ nhật ký lệnh gọi model bền vững, bao gồm
các biên nhận mới hơn checkpoint cuối cùng. Một lần ghi trạng thái kết thúc thất bại hoặc bị chặn do mất quyền sở hữu khiến
job vẫn có thể được thử lại.

Lỗi agent là một kết quả bền vững, không phải một lần thử lại của hàng đợi. Lỗi hạ tầng
như không thể truy cập store hoặc có lease đang hoạt động cạnh tranh dùng thời gian chờ thử lại
của `DatabaseRetryEntrypointExecutor`; các job đã hết số lần thử được giữ lại để ứng dụng chủ
kiểm tra thay vì bị loại bỏ.

PgQueuer 1.6 thử lại theo ID job trong hàng đợi: nó đặt lại trạng thái thành `queued`, tăng
số lần thử và xóa `queue_manager_id` mà không kiểm tra chủ sở hữu hàng đợi cũ. Vì vậy,
lần thử lại của một worker cũ có thể đưa lại vào hàng đợi một job mà worker khác đã lấy lại.
Lease của run vẫn ngăn thực thi đồng thời run đó; các lần nhận quyền sở hữu khi thử lại
gặp chủ sở hữu đang hoạt động sẽ tiếp tục chờ rồi thử lại. Chỉ quyền sở hữu hàng đợi không ngăn được các lần ghi
vào run từ chủ sở hữu cũ. Worker chỉ xác nhận một run có trạng thái kết thúc đã được lưu bền vững.

Khi tiến trình chết, cả heartbeat của hàng đợi và lease đều dừng. PgQueuer lấy lại
job `picked` bị bỏ lại khi heartbeat hàng đợi cuối cùng của nó đã cũ hơn
`stall_timeout_s`. Lần lấy tiếp theo còn chịu độ trễ do polling, cơ sở dữ liệu và lập lịch;
đây không phải thời hạn khôi phục theo thời gian thực tính từ lúc tiến trình chết.
Worker mới nhận quyền sở hữu lease đã hết hạn và resume checkpoint, đồng thời xác minh
hợp đồng thực thi đã lưu. Chọn thời gian chờ phát hiện đình trệ ít nhất bằng TTL
của lease, cộng thêm biên độ cho độ trễ lập lịch/DB. Các biên nhận tool bền vững được phát lại; tác động
bên ngoài của tool vẫn cần đối soát bằng `ctx.idempotency_key`. PgQueuer xác nhận
việc hủy task, nên cần buộc dừng tiến trình để kiểm tra việc khôi phục công việc mồ côi
thực tế. `worker.stop()` dừng lấy job và chờ các job đang hoạt động hoàn tất; hãy để `run()` trả về
trước khi đóng pool của ứng dụng chủ.

### Công việc định kỳ trên hàng đợi

`create_schedule(pool, session_id, payload, cron="0 9 * * *")` hoặc
`interval_s=3600` tạo nguyên tử một định nghĩa lịch và job được hoãn đầu tiên của nó.
Cron dùng croniter, năm trường, UTC. Mỗi giao dịch kích hoạt tạo
một run ID xác định, cập nhật định nghĩa sang lần tiếp theo và chèn job được hoãn kế tiếp
với `execute_after` và khóa chống trùng chứa thời điểm kích hoạt. Phát lại một
lần kích hoạt là thao tác không làm gì. Các lần bị lỡ được chạy bù theo thứ tự thời điểm kích hoạt sau thời gian ngừng hoạt động.
`list_schedules(pool, session_id=...)` trả về các định nghĩa;
`cancel_schedule(pool, id, session_id=...)` vô hiệu hóa các lần kích hoạt tương lai đồng thời giữ lại
các run đã được tạo. Các tiến trình API không cần vòng lặp polling của bộ lập lịch.

Ứng dụng chủ gắn các tool dành cho model một cách tường minh:

```python
from linch.worker import register_schedule_tools

undo = register_schedule_tools(agent.tools, pool, session_id=session_id)
# CreateSchedule / ListSchedules / CancelSchedule are bound to this target.
await undo()  # identity-safe, idempotent Disposable
```

### Stream từ bất kỳ tiến trình nào theo số thứ tự

Bên đọc stream chỉ cần một `PostgresRunStore`, không cần bộ nhớ của worker. Đọc
`await store.load_events(run_id, after_seq=last_seq)`, phân phối theo thứ tự tăng dần
và chỉ cập nhật `last_seq` sau khi phân phối thành công. Lưu bền vững con trỏ đó trong
client (ví dụ SSE `Last-Event-ID`) và dùng nó khi kết nối lại. Số thứ tự chỉ có ý nghĩa
trong phạm vi một run. Poll `run_events` theo `(run_id, seq)` khi không có kênh đánh thức
hoặc khi tranh chấp NOTIFY gây ảnh hưởng đáng kể; thông báo chỉ là gợi ý, không bao giờ là
nguồn dữ liệu chuẩn. Một lần poll rỗng không có nghĩa là đã hoàn tất; hãy quan sát
event kết quả đã được lưu bền vững. [Ví dụ cụm worker](../../../examples/worker/minimal.py) có thể chạy
bao gồm các chế độ gửi vào hàng đợi, worker và bên đọc bằng polling mà không cần khóa model thật.

### Đo enqueue / NOTIFY

Đo độ trễ và thông lượng gửi vào hàng đợi trên máy chủ triển khai. `enqueue_runs`
gộp các hàng run và thao tác chèn vào hàng đợi trong một giao dịch, giảm chi phí commit và
thông báo; giới hạn mức đồng thời của bên gửi vào hàng đợi khi lưu lượng tăng đột biến. Thông báo PgQueuer
đánh thức quá trình phân phối nhưng chỉ là gợi ý, không phải nguồn dữ liệu chuẩn. Bên đọc
stream nên poll `run_events` đã lưu bền vững theo ID run và số thứ tự khi cần
phát lại đáng tin cậy.

## Dependency tùy chọn cần được bật chủ động

Bản cài đặt core bao gồm OpenAI SDK, PyYAML, `typing_extensions` và
`jsonschema`. Các schema đầu ra có cấu trúc đã cấu hình luôn được xác thực.
Dependency tích hợp là các extra và khi thiếu sẽ gây lỗi kèm thông báo
`pip install 'linch[...]'` rõ ràng:

| Extra | Khả năng được bật |
|---|---|
| `linch[anthropic]` | Provider Anthropic |
| `linch[gemini]` | Provider Gemini |
| `linch[mcp]` | Các máy chủ tool MCP (yêu cầu `mcp>=2.0.0`; không hỗ trợ 1.x) |
| `linch[worker]` | Phân phối bằng PgQueuer, asyncpg và lịch croniter |
| `linch[postgres]` | Các store session/run và backend hệ thống tệp Postgres |

---

## Các trang liên quan

- [Agent và session](./agent.md) · [Event](./events.md) · [Hook](../../usage/hooks.md)
- [Trình chạy vòng lặp ngoài](./loop-runner.md) · [Điều phối](./coordination.md)
- [Mở rộng](./extending.md) · [Provider](./providers.md)
