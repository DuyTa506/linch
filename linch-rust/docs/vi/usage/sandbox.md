# Sandbox: nơi Bash chạy

[Bản gốc tiếng Anh](../../usage/sandbox.md)

Dùng trang này để chọn nơi `Bash` của agent chạy lệnh, và để biết mỗi lựa chọn giới
hạn những gì và không giới hạn những gì.

Theo mặc định `Bash` chạy `/bin/sh -c` trong workspace với tư cách người dùng của
server, chỉ với chín biến của host trong môi trường. `execution` trong cấu hình agent
chọn một backend khác:

| `backend` | Lệnh chạy | Cần |
|---|---|---|
| `local` (mặc định) | Trực tiếp với tư cách người dùng của server | Không cần gì |
| `sandbox` | Dưới Landlock, một bộ lọc seccomp và rlimit, với một home riêng cho mỗi session | Linux 6.12 trở lên (Landlock ABI 6) và `prlimit` (util-linux); không cần root, không cần user namespace, không cần daemon |
| `docker` | Trong một container `docker run --rm` mới cho mỗi lệnh | Một client `docker` và một daemon mà người dùng của server truy cập được |

Lựa chọn này áp dụng cho `Bash` ở foreground và background, nên cũng áp dụng cho
`BashOutput` và `BashKill`. Các tool tệp (`Read`, `Write`, `Edit`, `Grep`, `Glob`)
luôn chạy trong server, bị giới hạn trong workspace; mọi backend thấy cùng một thư mục
workspace, nên một tệp `Bash` ghi chính là tệp `Read` đọc.

```python
from linch_rust import AgentConfig, SandboxExecution

sandbox: SandboxExecution = {"backend": "sandbox"}  # every limit has a default
config: AgentConfig = {
    "namespace": "app",
    "spec": {"model": "deepseek-flash"},
    "provider": {"id": "deepseek"},
    "workspace": "/srv/app/work",
    "execution": sandbox,
}
```

Một agent có backend không thể giới hạn những gì policy của nó nói sẽ không mở được:
`OpenError` (mã `-32011`) nêu lý do, chẳng hạn kernel không có Landlock ABI 6, thiếu
`prlimit` hoặc thiếu `docker`. Backend chỉ được mở khi agent có `Bash`.

Một backend khác `local` là một phần của hợp đồng run: mô tả của `Bash` kết thúc bằng
một câu về những gì nó giới hạn, để model biết, và một run bắt đầu với một policy sẽ
không resume dưới policy khác. Backend local không thêm gì, nên hợp đồng của nó là cái
nó vẫn luôn có.

---

## Backend `sandbox`

Mỗi lệnh được fork từ một thread tồn tại ngắn, thread này trước tiên tự giới hạn mình
bằng Landlock và một bộ lọc seccomp; process con kế thừa cả hai, còn các thread khác
của server không bị giới hạn. Sau đó `prlimit` đặt các giới hạn và exec `/bin/sh`, giữ
nguyên process group và lifeline của lệnh nền.

| Trường | Mặc định | Đặt gì |
|---|---|---|
| `network` | `false` | Lệnh có được mở network socket hay không |
| `memory_bytes` | 2 GiB | Không gian địa chỉ của mỗi process (`RLIMIT_AS`) |
| `max_processes` | 512 | Số process và thread mà một lệnh có thể thêm vào những cái mà người dùng của server đã chạy (`RLIMIT_NPROC`, đặt bằng số đếm của người dùng lúc bắt đầu cộng với giá trị này) |
| `cpu_seconds` | 1800 | Thời gian CPU của mỗi process (`RLIMIT_CPU`): `SIGXCPU` ở mức giới hạn, `SIGKILL` một giây sau |
| `file_size_bytes` | 1 GiB | Tệp lớn nhất mà một process được ghi (`RLIMIT_FSIZE`) |
| `read_paths` | không có | Các đường dẫn tuyệt đối mà lệnh cũng có thể đọc và thực thi |

### Nó giới hạn những gì

- **Tệp.** Một lệnh chỉ đọc workspace, home riêng của nó, các thư mục hệ thống `/usr`,
  `/bin`, `/sbin`, `/lib*`, `/etc`, `/opt`, `/proc` và `/sys`, các thiết bị
  `/dev/null`, `/dev/zero`, `/dev/full`, `/dev/random` và `/dev/urandom`,
  cùng `read_paths`. Nó chỉ ghi workspace và home của nó. Home của người dùng server
  (khóa, `~/.ssh`, cấu hình cloud), terminal của server, phần còn lại của `/tmp` và
  home của các session khác đều bị đóng.
- **Một home riêng cho mỗi session.** `HOME` và `TMPDIR` chỉ tới một thư mục của
  session dưới một thư mục scratch của agent. Nó tồn tại qua các lệnh của session và
  bị xóa khi session đóng (`Agent.close_session`) và cùng với agent.
- **Môi trường.** Chỉ `PATH`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER` và `SHELL`
  được truyền từ server, với `HOME` và `TMPDIR` được thay, nên không có key provider
  nào ở đó. `/proc/<server pid>/environ` và `environ`, `mem` và `fd` của mọi process
  khác cũng bị đóng: Landlock cấm trace một process ngoài sandbox.
- **Signal.** Một lệnh không thể gửi signal cho process ngoài sandbox của nó, nên
  không thể kill server.
- **Unix socket.** `socket(AF_UNIX, ...)` luôn thất bại, và kết nối tới một abstract
  socket được tạo ngoài sandbox cũng vậy, nên một lệnh không thể chạm tới session bus
  của người dùng, socket Docker hay dịch vụ cục bộ khác sẽ khởi động thứ gì đó không
  bị giới hạn. `socketpair` vẫn hoạt động.
- **Mạng tắt** (mặc định): mọi lời gọi `socket` thất bại với `EACCES`, TCP hay UDP, và
  Landlock cũng từ chối TCP bind và connect. Với `network: true`, TCP và UDP hoạt
  động, và đích của `/etc/resolv.conf` đọc được cho DNS.
- **Không có đặc quyền mới.** Một chương trình setuid không được thêm gì, và
  `io_uring`, mà bộ lọc sẽ không thấy các request của nó, bị từ chối.
- **Tài nguyên.** Mỗi process bị giữ trong `memory_bytes`, `cpu_seconds` và
  `file_size_bytes`, và một lệnh trong `max_processes` process thêm, nên một fork bomb
  hay một tiến trình ngốn bộ nhớ thất bại ngay trong lệnh. Thời gian thực là timeout
  của `Bash`, như mọi backend.

### Nó không giới hạn những gì

- Lệnh chạy với user id của server: không có user namespace (một hạn chế của AppArmor
  hay sysctl thường vô hiệu hóa chúng với người dùng không đặc quyền), nên ranh giới là
  của Landlock, không phải của quyền tệp.
- `/proc` và `/sys` đọc được: một lệnh thấy command line của các process khác và các
  thiết bị của hệ thống, như mọi người dùng cục bộ.
- Giới hạn tính theo từng process, không theo từng lệnh: không có cgroup, nên không có
  tổng bộ nhớ, không có phần chia CPU, và `max_processes` là khoảng dư so với số đếm
  hiện tại của người dùng, mà các công việc khác của cùng người dùng cũng dùng chung.
- Landlock chặn việc mở tệp, không chặn việc tra cứu chúng: `stat`, `ls -d` và việc
  một đường dẫn có tồn tại vẫn thấy được bên ngoài các thư mục được phép.
- `memory_bytes` giới hạn không gian địa chỉ, không phải bộ nhớ resident: các runtime
  dự trữ trước những vùng lớn (JVM, Go, Node) có thể thất bại với giá trị nhỏ.
- Chương trình 32-bit bị kill: bộ lọc seccomp chỉ cho phép kiến trúc của chính server.
- Một server chạy bằng root bỏ qua `RLIMIT_NPROC`; hãy chạy nó bằng người dùng không
  đặc quyền.
- Với `network: true`, một lệnh chạm tới được mọi thứ mà host chạm được qua TCP hoặc
  UDP, kể cả các dịch vụ trên `localhost`.
- Các tool tệp, MCP stdio server và backend đĩa của hệ thống tệp ảo vẫn chạy trong
  hoặc từ server như trước; sandbox dành cho `Bash`.

## Backend `docker`

Mỗi lệnh chạy trong `docker run --rm` với các giới hạn của Python Linch và workspace
được mount tại chính đường dẫn của nó:

| Trường | Mặc định | Đặt gì |
|---|---|---|
| `image` | `python:3.12-slim` | Image; hãy nêu một digest để có thế giới tái lập được |
| `docker_path` | `docker` trên `PATH` của server | Client |
| `network` | `none` | `--network` (Python Linch để bridge mặc định của Docker bật) |
| `memory_bytes` | 2 GiB | `--memory` |
| `cpu_millis` | 2000 | `--cpus`, theo phần nghìn của một CPU |
| `pids_limit` | 512 | `--pids-limit` |
| `read_only_root` | `false` | `--read-only` |
| `tmpfs` | không có | Các mục `--tmpfs` |
| `user` | uid và gid của server | `--user`, để các tệp lệnh ghi thuộc về user mà các tool tệp chạy |

Mọi capability đều bị bỏ và `no-new-privileges` được đặt. Container nhận các biến của
`Bash` trừ `PATH`, `HOME`, `USER`, `SHELL` và `TMPDIR`, được truyền theo tên để không
có giá trị nào nằm trên command line. Client cũng nhận `DOCKER_*` và
`XDG_RUNTIME_DIR` của host để tìm daemon của nó.

Container thuộc về daemon, không thuộc về server, nên kill process group của client
không dừng được nó: một lệnh hết timeout, bị hủy hoặc bị kill, hoặc có session đóng,
được xóa bằng `docker rm -f`, và watchdog lifeline của một lệnh nền chạy đúng lệnh đó
khi server chết.

Một container giới hạn những gì runtime của Docker giới hạn: hệ thống tệp, cây process
và network namespace của chính nó, cùng các giới hạn ở trên. Không có gì ngoài
workspace được giữ lại giữa các lệnh. Daemon là của root, nên một client truy cập được
nó thì làm được mọi thứ; hãy giữ socket của nó xa các tenant (backend sandbox từ chối
Unix socket vì lý do đó).

## Trên server

Một tenant từ xa chỉ được dùng các backend mà server của nó liệt kê, mỗi cái với
policy của operator (`allow.execution`, xem [Server](./server.md#config)). Một cấu hình
nêu một backend phải bằng chính xác một mục, nên client không thể tự chọn image, mạng
hay giới hạn của mình; một cấu hình không nêu gì nhận mục đầu tiên. Vì vậy
`"execution": [{"backend": "sandbox"}]` giữ mọi agent của tenant trong sandbox và từ
chối `local`. Mặc định là `[{"backend": "local"}]`, và `Bash` vẫn cần `exec_tools`.

Một run nền (`job.enqueue` hoặc một lần kích hoạt của lịch) được server có worker chạy
nó kiểm tra lại ở mỗi lần thử, nên nó nhận backend mà server đó đặt cho tenant. Một
gốc `local_namespaces` được tin cậy như một client stdio và giữ lựa chọn trong cấu hình
của nó.

## Python Linch

`ExecutionBackend` của Python ghép một shell và một file backend
(`src/linch/execution/backend.py`), với `LocalExecutionBackend`,
`RemoteExecutionBackend` và shell `DockerBackend` (`src/linch/tools/execution.py:229`).
linch-rust có cùng seam đó (`ExecutionBackend` trong
`crates/linch-tools/src/exec.rs`) với ba backend dựng sẵn được chọn bằng cấu hình thay
vì bằng object; nó không có remote shell cắm thêm được, và backend sandbox không có đối
ứng trong Python. `TempDirIsolation` của Python cho mỗi nhánh subagent một bản làm việc
riêng; home riêng của sandbox là theo session, và workspace vẫn dùng chung với các tool
tệp.
