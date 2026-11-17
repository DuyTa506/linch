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
`prlimit`, server chạy bằng root (với sandbox), thiếu `docker` hoặc policy không hợp
lệ. Backend chỉ được mở khi agent có `Bash`.

Với mọi backend, một descriptor mà server đang giữ mở được đánh dấu close-on-exec
trước khi một lệnh bắt đầu, nên không descriptor nào được truyền cho lệnh.

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
| `read_paths` | không có | Các đường dẫn tuyệt đối mà lệnh cũng có thể đọc và thực thi (chẳng hạn `/opt`) |

Server không được chạy bằng root: việc mở sandbox thất bại khi bất kỳ user id nào của
server là 0 hoặc nó giữ một capability trong tập effective hay ambient, vì một lệnh sẽ
giữ chúng.

### Nó giới hạn những gì

- **Tệp.** Một lệnh chỉ đọc workspace, home riêng của nó, các thư mục hệ thống `/usr`,
  `/bin`, `/sbin`, `/lib*`, `/proc` và `/sys`, các mục được liệt kê của `/etc` bên dưới,
  các thiết bị `/dev/null`, `/dev/zero`, `/dev/full`, `/dev/random` và `/dev/urandom`,
  cùng `read_paths`. Nó chỉ ghi workspace và home của nó. Home của người dùng server
  (khóa, `~/.ssh`, cấu hình cloud), terminal của server, phần còn lại của `/tmp`, home
  của các session khác, `/opt` và mọi mục khác của `/etc` đều bị đóng.
- **`/etc`, theo danh sách cho phép.** Chỉ những gì một shell, coreutils, `git`,
  `python3` và `curl` đọc (tìm ra bằng cách chạy chúng trong sandbox dưới `strace`):
  `alternatives`, `ca-certificates*`, `crypto-policies/back-ends`,
  `debian_version`, `fonts`, `gai.conf`, `gitattributes`, `gitconfig`,
  `gnutls/config`, `group`, `host.conf`, `hosts`, `ld.so.cache`,
  `ld.so.conf`, `ld.so.conf.d`, `ld.so.preload`, `locale.alias`,
  `localtime`, `lsb-release`, `magic`, `magic.mime`, `mime.types`,
  `netconfig`, `networks`, `nsswitch.conf`, `os-release`, `passwd`,
  `pki/ca-trust/extracted`, `pki/tls/cert.pem`, `pki/tls/certs`,
  `pki/tls/openssl.cnf`, `protocols`, `python3*`, `resolv.conf`,
  `selinux/config`, `services`, `ssh/ssh_config`, `ssh/ssh_config.d`,
  `ssh/ssh_known_hosts`, `ssl/cert.pem`, `ssl/certs`, `ssl/openssl.cnf`,
  `terminfo`, `timezone`, `userdb` và `wgetrc` (một mục có `*` đại diện cho mọi tên bắt
  đầu như vậy). Một bí mật được giữ ở chỗ khác trong `/etc` (cấu hình của một ứng dụng,
  thông tin xác thực trong `/etc/fstab`) vẫn bị đóng ngay cả khi quyền tệp cho phép
  người dùng đọc nó. Một chương trình cần mục khác sẽ thất bại với `Permission denied`;
  hãy nêu nó trong `read_paths`.
- **Một home riêng cho mỗi session.** `HOME` và `TMPDIR` chỉ tới một thư mục của
  session dưới một thư mục scratch của agent. Nó tồn tại qua các lệnh của session và
  bị xóa khi session đóng (`Agent.close_session`) và cùng với agent.
- **Môi trường.** Chỉ `PATH`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER` và `SHELL`
  được truyền từ server, với `HOME` và `TMPDIR` được thay, nên không có key provider
  nào ở đó. `/proc/<server pid>/environ` và `environ`, `mem` và `fd` của mọi process
  khác cũng bị đóng: Landlock cấm trace một process ngoài sandbox.
- **Signal và các process khác.** Một lệnh không thể gửi signal cho process ngoài
  sandbox của nó, nên không thể kill server. Nó cũng không thể đổi giới hạn, độ ưu tiên
  hay CPU affinity của process khác, dù server chạy với cùng người dùng: `prlimit64` và
  các lời gọi `sched_set*` chỉ nhận pid 0, `setpriority` và `ioprio_set` chỉ áp dụng cho
  chính process gọi (`nice`, `ionice`, `taskset` và `ulimit` lên chính nó vẫn hoạt động).
- **Unix socket.** `socket(AF_UNIX, ...)` luôn thất bại, và kết nối tới một abstract
  socket được tạo ngoài sandbox cũng vậy, nên một lệnh không thể chạm tới session bus
  của người dùng, socket Docker hay dịch vụ cục bộ khác sẽ khởi động thứ gì đó không
  bị giới hạn. `socketpair` vẫn hoạt động.
- **Mạng tắt** (mặc định): mọi lời gọi `socket` thất bại với `EACCES`, TCP hay UDP, và
  Landlock cũng từ chối TCP bind và connect. Với `network: true`, chỉ socket
  `AF_INET` và `AF_INET6` mở được: netlink, vsock, packet, crypto (`AF_ALG`) và mọi
  họ khác vẫn bị từ chối. Phân giải tên hoạt động không cần netlink (`AI_ADDRCONFIG`
  của glibc tự lùi về cách khác); đích của `/etc/resolv.conf` đọc được.
- **Không có đặc quyền hay namespace mới.** Một chương trình setuid không được thêm gì.
  Bộ lọc seccomp từ chối với `EPERM` các kernel module, `kexec`, `reboot`, swap, mọi
  lời gọi mount, `pivot_root` và `chroot`; keyring của người dùng (`keyctl`, `add_key`,
  `request_key`), `bpf`, `perf_event_open` và `userfaultfd`; cùng `unshare`, `setns` và
  `clone` với cờ `CLONE_NEW*`, nên ngay cả user namespace không đặc quyền cũng không mở
  được. `clone3`, mà bộ lọc không đọc được cờ của nó, nhận `ENOSYS` và glibc lùi về
  `clone`. `io_uring`, mà bộ lọc sẽ không thấy các request của nó, bị từ chối.
- **Tài nguyên.** Mỗi process bị giữ trong `memory_bytes`, `cpu_seconds` và
  `file_size_bytes`, và một lệnh trong `max_processes` process thêm, nên một fork bomb
  hay một tiến trình ngốn bộ nhớ thất bại ngay trong lệnh. Thời gian thực là timeout
  của `Bash`, như mọi backend.

### Nó không giới hạn những gì

- Lệnh chạy với user id của server: không có user namespace (một hạn chế của AppArmor
  hay sysctl thường vô hiệu hóa chúng với người dùng không đặc quyền), nên ranh giới là
  của Landlock, không phải của quyền tệp.
- `/proc` và `/sys` đọc được: một lệnh thấy command line và trạng thái của mọi process
  cùng các thiết bị của hệ thống, như mọi người dùng cục bộ. `environ`, `mem`, `fd` và
  `maps` của các process khác vẫn bị đóng (Landlock cấm trace ngoài sandbox). `/proc`
  được giữ có chủ ý: thiếu nó thì `ps`, `free`, `uptime`, các lần đọc kiểu `nproc` từ
  `/proc/cpuinfo` và `/proc/meminfo`, và mọi thứ đọc `/proc/self` đều thất bại; Landlock
  không thể cấp cho một lệnh `/proc/self` của chính nó mà không cấp phần còn lại. Đừng
  đặt bí mật trên command line của server.
- Các khóa `multiprocessing` của Python cần `/dev/shm`, vốn bị đóng, nên một `Pool` thất
  bại.
- Giới hạn tính theo từng process, không theo từng lệnh: không có cgroup, nên không có
  tổng bộ nhớ, không có phần chia CPU, và `max_processes` là khoảng dư so với số đếm
  hiện tại của người dùng, mà các công việc khác của cùng người dùng cũng dùng chung.
- Landlock chặn việc mở tệp, không chặn việc tra cứu chúng: `stat`, `ls -d` và việc
  một đường dẫn có tồn tại vẫn thấy được bên ngoài các thư mục được phép.
- `memory_bytes` giới hạn không gian địa chỉ, không phải bộ nhớ resident: các runtime
  dự trữ trước những vùng lớn (JVM, Go, Node) có thể thất bại với giá trị nhỏ.
- Chương trình 32-bit bị kill: bộ lọc seccomp chỉ cho phép kiến trúc của chính server.
- Với `network: true`, một lệnh chạm tới được mọi thứ mà host chạm được qua TCP hoặc
  UDP, kể cả các dịch vụ trên `localhost`.
- Các tool tệp, MCP stdio server và backend đĩa của hệ thống tệp ảo vẫn chạy trong
  hoặc từ server như trước; sandbox dành cho `Bash`.

## Backend `docker`

Mỗi lệnh chạy trong `docker run --rm` với các giới hạn của Python Linch và workspace
được mount tại chính đường dẫn của nó:

| Trường | Mặc định | Đặt gì |
|---|---|---|
| `image` | `python:3.12-slim` | Image; hãy nêu một digest để có thế giới tái lập được. Không được bắt đầu bằng `-` |
| `docker_path` | `docker` trên `PATH` của server | Client |
| `network` | `none` | `--network`: `none`, `bridge` hoặc tên một network; `host` và `container:<name>` bị từ chối (Python Linch để bridge mặc định của Docker bật) |
| `memory_bytes` | 2 GiB | `--memory` |
| `cpu_millis` | 2000 | `--cpus`, theo phần nghìn của một CPU |
| `pids_limit` | 512 | `--pids-limit` |
| `read_only_root` | `false` | `--read-only` |
| `tmpfs` | không có | Các mục `--tmpfs` |
| `user` | uid và gid của server | `--user`, để các tệp lệnh ghi thuộc về user mà các tool tệp chạy |
| `allow_root` | `false` | Lệnh có được chạy bằng root trong container hay không: nếu không, một `user` là `0`, `root` hoặc `0:<gid>`, hoặc một server chạy bằng root (có uid là mặc định), bị từ chối |

Mọi capability đều bị bỏ và `no-new-privileges` được đặt. Workspace được bind-mount
bằng `--mount type=bind,...` (một đường dẫn workspace có dấu phẩy hoặc dấu nháy bị từ
chối), và `--quiet` giữ tiến trình pull image ra khỏi output của lệnh. Container nhận
các biến của `Bash` trừ `PATH`, `HOME`, `USER`, `SHELL` và `TMPDIR`, được truyền theo
tên để không có giá trị nào nằm trên command line. Client cũng nhận `DOCKER_*` và
`XDG_RUNTIME_DIR` của host để tìm daemon của nó.

Container thuộc về daemon, không thuộc về server, nên kill process group của client
không dừng được nó: một lệnh hết timeout, bị hủy hoặc bị kill, hoặc có session đóng,
được xóa bằng `docker rm -f`. Mọi lệnh, foreground hay background, chạy dưới lifeline:
khi server chết, watchdog của nó kill client trước (để một client còn đang tạo
container không thể khởi động nó) rồi xóa container theo tên. Mỗi container được gắn
nhãn `linch.boot=<host boot id>:<server pid>:<start time>`; việc mở một backend docker
xóa, ở nền, các container có nhãn của lần boot host này mà process server của chúng đã
mất. Nó không bao giờ xóa container của một server còn sống hay của một host khác dùng
chung daemon.

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
