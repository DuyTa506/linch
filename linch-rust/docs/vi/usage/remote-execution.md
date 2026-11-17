# Remote execution: Bash trên máy của bạn

[English](../../usage/remote-execution.md)

Dùng trang này khi `Bash` của agent cần chạy lệnh ở nơi server không có mặt: laptop
của bạn, một build box, một dịch vụ sandbox mà bạn đã vận hành. Với
`"execution": {"backend": "remote"}`, server yêu cầu client đã mở agent chạy từng lệnh,
qua chính kết nối mà client đang có, rồi stream ngược lại stdout, stderr và exit code.
Các backend khác (`local`, `sandbox`, `docker`) nằm ở trang [Sandbox](./sandbox.md).

```python
from linch_rust import AgentConfig, RemoteExecution, Server, SubprocessHost

remote: RemoteExecution = {"backend": "remote"}
config: AgentConfig = {
    "namespace": "app",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "workspace": "/srv/app/work",   # the server's directory; see "Files"
    "execution": remote,
}

async with Server() as server:
    agent = await server.open_agent(config, execution=SubprocessHost(cwd="/home/me/project"))
```

`SubprocessHost` là host tham chiếu: nó chạy mỗi lệnh bằng `/bin/sh -c` trong một
process group riêng, với `asyncio.create_subprocess_exec`. Để chạy lệnh theo cách khác
(SSH, một sandbox API), hãy viết một `ExecutionHost`: bốn method, `start`, `read`,
`kill` và `close`.

## Bảo mật: lệnh chạy ở đâu

**Một lệnh remote chạy ở nơi host chạy nó, với đặc quyền của host.** Sandbox, Landlock,
rlimit và container của server không áp dụng; sự cô lập duy nhất là của chính host. Hãy
chạy nó ở nơi bạn sẵn lòng để model chạy một shell.

- Server gửi lệnh, `cwd` của policy và các biến trong allowlist của `Bash` (`TERM`,
  `LANG`, `LC_ALL`, `LC_CTYPE`, cùng những biến agent đã thêm). Nó bỏ `PATH`, `HOME`,
  `USER`, `SHELL` và `TMPDIR`, vì chúng mô tả server và vô nghĩa trên host (backend
  `docker` cũng làm vậy); `SubprocessHost` tự điền chúng từ môi trường của nó. Không có
  key provider hay bí mật nào khác của server trong môi trường riêng của lệnh.
- `SubprocessHost` chạy lệnh với tư cách người dùng của client, nên lệnh có thể đọc môi
  trường của process client qua `/proc` (`cat /proc/$PPID/environ` hiện những gì client
  đã khởi động cùng), như docstring của `Server` đã cảnh báo cho process server. Giữ
  key provider và bí mật khác ra khỏi môi trường của client (đọc chúng từ một kho bí
  mật). Operator cần cô lập hơn mức đó thì chạy host bằng một người dùng khác, hoặc
  viết một `ExecutionHost` làm điều đó.
- Host thấy các lệnh của model và phải được tin cậy với chúng; server tin câu trả lời
  của host về output và exit code, không hơn.
- Một tenant của `linch-server` bị **từ chối theo mặc định**: `allow.execution` phải
  liệt kê mục remote, kèm policy, và một cấu hình chỉ được nêu một mục đúng như đã liệt
  kê (như với `sandbox` và `docker`). Một tenant có danh sách bắt đầu bằng `remote` nhận
  nó ngay cả khi cấu hình không nêu backend nào, nên mọi client của tenant đó phải phục
  vụ host (một agent không có `Bash` không bao giờ gọi tới nó).
- Một lỗi hiển thị cho model không bao giờ chứa đường dẫn của server hay chi tiết của
  kết nối. Một thông báo do chính host đưa ra khi nó từ chối một lần start được hiển thị
  dưới dạng văn bản thuần, cắt còn 2 KiB.

## Một agent remote phục vụ được ai

Host chính là kết nối. Vì vậy một agent mà `Bash` chạy trên backend remote (`bash` bật,
và `workspace` chứa các tool cũng có):

- **không thể được lên lịch hay chạy trong worker.** `schedule.create` và `job.enqueue`
  từ chối nó với `-32040` (`not_background`, `data.reason` là `remote_execution`);
  `schedule_tools` bị từ chối ngay khi mở; một worker được đưa một definition như vậy
  sẽ từ chối nó. Không có ai để chạy lệnh. Một agent không có `Bash` (`"bash": false`,
  hoặc không có `workspace`) không bao giờ chạy lệnh nào, nên backend không cản nó.
- **gắn với kết nối của nó.** Nếu kết nối đóng, các lệnh nó đã bắt đầu không còn thuộc
  quyền server dừng nữa: host phải kill chúng (`SubprocessHost` làm vậy, khi kết nối kết
  thúc hoặc agent đóng).
- **không có `Bash` nếu không có host.** `AgentConfig.open` ngoài một kết nối (Rust
  `open_with_remote(..., None)`, extension Python) thất bại với một thông báo nói rõ
  điều đó, và client Python raise `ValueError` trước khi mở.

### Một host, nhiều principal

Host là client đã mở agent, nên các run của mọi principal trên agent đó (mỗi `run`
với `principal` riêng) đều đến cùng một host. Server không giới hạn một lệnh theo
principal: `exec.start` chỉ nêu agent và session. Nó mang theo `principal` của run đã
tạo lời gọi (`{tenant, user, roles}`, chính cái đã đưa cho `run.start`; vắng mặt khi
run không có), để một host phục vụ nhiều hơn một principal có thể giới hạn hoặc từ chối
lệnh theo nó (mỗi principal một thư mục làm việc, một tài khoản hay một container).
Python: `ExecStart.principal`. `SubprocessHost` bỏ qua nó và chạy mọi lệnh như nhau,
dưới quyền người dùng của tiến trình này: đừng dùng chung một host giữa các principal
cần được tách biệt. `exec.read` và `exec.kill` chỉ nêu lệnh; `exec_id` của nó là cái
host đã thấy ở `exec.start`, nên host nào ghi nhớ principal ở đó thì cũng biết nó về
sau. Trên kết nối của một tenant, tenant của principal là tenant của chính kết nối;
`user` và `roles` là điều client tuyên bố, như với các quy tắc quyền.

## Tệp: các tool tệp tác động lên server

Hãy đọc phần này trước khi bật. `Read`, `Write`, `Edit`, `Grep` và `Glob` chạy trong
process của server, trên thư mục `workspace` của nó. Các lệnh của một host remote chạy ở
nơi khác, nên một tệp `Bash` ghi ở đó không phải là tệp `Read` đọc ở đây. Thay vì lặng
lẽ trộn hai thế giới:

| Policy | Các tool model có |
|---|---|
| `{"backend": "remote"}` | chỉ `Bash`, `BashOutput`, `BashKill` |
| `{"backend": "remote", "shared_workspace": true}` | cả tám; host thấy workspace của server ở cùng đường dẫn (một shared mount, hoặc cùng một máy), lệnh bắt đầu ở đó, nên là một thế giới duy nhất |

Chỉ đặt `shared_workspace` khi điều đó đúng. `workspace` vẫn bắt buộc: nó nêu thư mục
của server (model không dùng tới nếu không có `shared_workspace`). Để cho model các tệp
trên host, hãy chạy chúng qua `Bash`, hoặc khai báo host tool.

Các trường của policy:

| Trường | Mặc định | Đặt gì |
|---|---|---|
| `cwd` | tùy host chọn | Thư mục mà lệnh bắt đầu trên host |
| `shared_workspace` | `false` | Host có thấy workspace ở cùng đường dẫn hay không (đăng ký các tool tệp; lệnh bắt đầu trong workspace trừ khi đặt `cwd`) |

Policy là một phần của hợp đồng run thông qua mô tả của `Bash`, như policy của các
backend khác; một agent không có backend giữ hợp đồng mà nó đã có.

## Cách nó hoạt động

`Bash` ở foreground và background, `BashOutput` và `BashKill` đều đi qua host.

- **Timeout và hủy.** Server thực thi chúng. Khi tới hạn, hoặc khi run bị hủy, nó gửi
  `exec.kill` và chỉ báo `timed out` hoặc `aborted` sau khi host trả lời rằng lệnh đã
  chết: câu trả lời phải mang `exit` của lệnh. Khi scheduler cho lời gọi một hạn chót
  (`call_timeout_ms`), một lệnh remote kết thúc sớm hơn 6 giây (5 giây của kill và một
  lần read) khi hạn chót lớn hơn khoảng 12 giây, và sớm hơn một nửa hạn chót khi nhỏ hơn
  mức đó, để câu trả lời kill và output đã có vẫn tới được model. Một host mất 5 giây để
  xác nhận kill cần `call_timeout_ms` ít nhất khoảng 10 giây: với giá trị ngắn hơn (hoặc
  một `timeout_ms` của `Bash` chừa lại ít hơn thời gian kill cần trước hạn chót),
  scheduler bỏ lời gọi trước và model đọc `Tool "Bash" timed out after N ms` mà không
  có output. Không có `call_timeout_ms` thì scheduler không đặt hạn chót và chỉ
  `timeout_ms` của `Bash` được áp dụng. Giữ `call_timeout_ms` từ 12 giây trở lên với
  backend remote.
- **Một host không trả lời là `interrupted`.** Nếu host ngừng trả lời các lần read (6
  giây), báo lỗi, đóng kết nối, hoặc không xác nhận một lần kill (5 giây, hoặc một câu
  trả lời không có `exit`), lời gọi kết thúc với mã `interrupted` và `[interrupted: ...
  the command's outcome is unknown; it may still be running]`. Một khi một lần read đã
  mất, lời gọi vẫn là `interrupted` dù kill sau đó có báo exit. Output đã tới được giữ
  lại; không có exit code, signal hay timeout nào bị bịa ra. Một process nền hiển thị
  tương tự trong `BashOutput` (`status: interrupted`).
- **Một lần start mà host có thể đã thực hiện cũng là `interrupted`.** Chỉ một lần start
  mà host từ chối (một câu trả lời lỗi) mới là `spawn_failed`: nó chứng minh lệnh đã
  không chạy. Nếu câu trả lời cho `exec.start` bị mất, không đọc được hoặc đến muộn,
  server không thể biết, nên nó gửi `exec.kill` và kết thúc lời gọi là `interrupted`
  thay vì để model thử lại một lệnh có thể chạy hai lần. Một lệnh nền start như vậy sẽ
  hiện `interrupted` trong `BashOutput` ngay từ đầu.
- **Một host không thể làm ngập server.** Server đợi ít nhất 25 ms giữa các lần read mà
  câu trả lời gần như trống, và quá 256 MiB output của một lệnh thì nó kill lệnh và kết
  thúc lời gọi với mã `output_limit` và `[output limit reached (256 MiB); the command was
  killed]` (một process nền hiển thị `status: output_limit`). Nếu host không xác nhận lần
  kill đó, lời gọi là `interrupted`.
- **Lệnh nền** không có pid trên server (`metadata` chỉ có `process_id`), chết cùng
  session của chúng (`session.close`), `BashKill` và agent, và, qua host, cùng kết nối.
- Output bị giới hạn như mọi backend: 4000 byte đầu và 26000 byte cuối của mỗi stream
  tới được model.

## Giao thức

Ba request của server trên kết nối đã mở agent (xem trang [server](./server.md) về wire
format); không có gì khác được khai báo, `execution` của cấu hình chính là khai báo.

| Request | Params | Result |
|---|---|---|
| `exec.start` | `agent, exec_id, command, cwd?, env?, background?, session_id, principal?` | `{}` |
| `exec.read` | `agent, exec_id, wait_ms` | `{stdout?, stderr?, exit?}` |
| `exec.kill` | `agent, exec_id` | `{stdout?, stderr?, exit?}` |

`exec.read` là một long poll: trả lời ngay khi có output hoặc lệnh đã kết thúc, nếu
không thì sau `wait_ms` (1000) với `{}`. `exit` là `{"code": n}` hoặc `{"signal": n}`,
chỉ đến một lần và sau toàn bộ output, và host khi đó quên lệnh đó. Một lần read chỉ được
lấy output khi nó trả lời: server hủy một lần read mà nó bỏ cuộc (`$/cancelRequest`) và
khi đó không mất gì. `exec.kill` kill cả process group của lệnh, đợi tới khi nó chết và
trả về những gì nó đã in kể từ lần read cuối cùng cùng `exit` của nó; một câu trả lời
không có `exit` không phải là sự xác nhận. `ExecutionHost` của Python mang cùng hợp đồng
này; ở đó `agent` của mỗi lời gọi còn nêu cả kết nối (`<tag>/<handle>`), nên một host có
thể phục vụ nhiều kết nối.

`SubprocessHost` giữ các giới hạn mà một host cần. Tối đa 4 MiB của mỗi stream chờ một
lần read: quá đó nó ngừng đọc pipe của lệnh, và lệnh bị chặn cho tới khi server đọc. Một
lần kill trả về 256 KiB cuối của mỗi stream, và chỉ kèm `exit` khi shell đã được thu
dọn. Một lệnh không ai đọc trong `idle_timeout` giây (120) bị kill và bị quên: server
poll mỗi giây khi nó còn kết nối.

Cái gì chết khi nào, với `SubprocessHost` (mỗi lần kill là cả process group):

| Chuyện gì xảy ra | Cái gì chết |
| --- | --- |
| Một lệnh tự kết thúc | những gì nó để lại đang chạy trong group của nó |
| Timeout của lệnh, hoặc cancel | lệnh đó, bằng `exec.kill` |
| `close_session`, `close`, kết nối thoát sạch | mọi lệnh của agent hoặc kết nối đó |
| Server chết | mọi lệnh của kết nối của nó (kết nối kết thúc) |
| Client chết, kể cả bằng `SIGKILL` | mọi lệnh nó đã bắt đầu: mỗi lệnh có một process canh chừng đọc một pipe mà chỉ client giữ, và kill group khi pipe đóng |
| Không ai đọc một lệnh trong `idle_timeout` giây | lệnh đó |

Process canh chừng là một `/bin/sh` chỉ chạy các lệnh builtin của shell, nên không cần
`PATH` và không cần dependency; nó đứng trong một session riêng và kết thúc cùng lệnh.
Một host của riêng bạn sống lâu hơn client (một phiên SSH, một API sandbox) phải tự làm
điều tương tự: server không thể dừng một lệnh khi client của nó đã mất.

Khác biệt so với `RemoteExecutionBackend` của Python Linch: Python gộp một remote shell
và một remote filesystem và chạy cả hai trong thế giới của host; ở đây lớp tệp ở lại
trên server và bị giữ lại trừ khi workspace được dùng chung. `run(command, cwd, timeout,
signal)` của Python là một lời gọi; ở đây nó là `start`, `read` và `kill`, nên một lệnh
dài được stream và một lần kill được xác nhận.
