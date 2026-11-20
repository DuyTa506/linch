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
| `remote` | Trên một máy do client của bạn cung cấp, không phải trên server (xem [Thực thi từ xa](./remote-execution.md)) | Một kết nối client phục vụ các lệnh |

Lựa chọn này áp dụng cho `Bash` ở foreground và background, nên cũng áp dụng cho
`BashOutput` và `BashKill`. Các tool tệp (`Read`, `Write`, `Edit`, `Grep`, `Glob`)
luôn chạy trong server, bị giới hạn trong workspace; mọi backend thấy cùng một thư mục
workspace, nên một tệp `Bash` ghi chính là tệp `Read` đọc. Một lệnh dùng chung
workspace không thể đẩy các tool tệp ra khỏi nó bằng cách hoán đổi một thư mục hay
tệp thành symlink trong lúc một lệnh gọi chạy: mọi đường dẫn được đi từng tên từ một
descriptor đang mở của thư mục gốc và symlink dẫn ra ngoài bị từ chối (điều được
và không được che: [Giới hạn truy cập](../architecture/tools.md#giới-hạn-truy-cập)).

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
- **`/etc`, theo danh sách cho phép.** Những gì một shell, coreutils, `git`,
  `python3` và `curl` đọc trên Ubuntu (dò bằng `strace`, rồi chạy thử trong sandbox),
  cộng với các đường dẫn chứng chỉ và cấu hình mà bản phân phối khác dùng (`pki/*` và
  `crypto-policies` trên Fedora, `ssl/cert.pem` trên Alpine và Arch):
  `alternatives`, `ca-certificates*`, `crypto-policies/back-ends`,
  `debian_version`, `fonts`, `gai.conf`, `gitattributes`, `gitconfig`,
  `gnutls/config`, `group`, `host.conf`, `hosts`, `ld.so.cache`,
  `ld.so.conf`, `ld.so.conf.d`, `ld.so.preload`, `locale.alias`,
  `localtime`, `lsb-release`, `magic`, `magic.mime`, `mime.types`,
  `netconfig`, `networks`, `nsswitch.conf`, `os-release`, `passwd`,
  `pki/ca-trust/extracted`, `pki/tls/cert.pem`, `pki/tls/certs`,
  `pki/tls/openssl.cnf`, `protocols`, `python3*`, `resolv.conf`,
  `selinux/config`, `services`, `ssl/cert.pem`, `ssl/certs`, `ssl/openssl.cnf`,
  `terminfo`, `timezone` và `userdb` (một mục có `*` đại diện cho mọi tên bắt
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
  `clone`. `io_uring`, mà bộ lọc sẽ không thấy các request của nó, bị từ chối. Bộ lọc là một denylist: một
  syscall mà nó không nêu tên thì được phép, nên Landlock và việc từ chối chạy bằng root
  mới là ranh giới, còn bộ lọc chỉ đóng các cửa được liệt kê ở đây.
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
  Các tool tệp ở trong workspace nhờ lượt đi của chính chúng, không phải Landlock:
  một symlink mà lệnh sandbox để lại trong workspace chỉ được đi theo khi đích của nó
  còn nằm trong thư mục gốc. Các tool tệp không che hard link, nhưng dưới `sandbox` một lệnh không thể tạo hard
  link tới tệp của host: Landlock từ chối `ln` vượt ranh giới workspace (`Invalid
  cross-device link`, có test).

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
nhãn `linch.boot=<host boot id>:<pid namespace>:<uid>:<server pid>:<start time>`; việc
mở một backend docker xóa, ở nền, các container có nhãn mà server của chúng đã chạy
trong lần boot host này, cùng pid namespace và cùng người dùng, và đã mất. Một container
có server mà nó không nhìn thấy (host khác, container khác, người dùng khác dưới
`hidepid`) được để nguyên. Một container mà client bị kill đã yêu cầu tạo nhưng chưa
khởi động có thể còn lại cho tới khi server của nó mất; nó không chạy gì.

Một container giới hạn những gì runtime của Docker giới hạn: hệ thống tệp, cây process
và network namespace của chính nó, cùng các giới hạn ở trên. Không có gì ngoài
workspace được giữ lại giữa các lệnh. Daemon là của root, nên một client truy cập được
nó thì làm được mọi thứ; hãy giữ socket của nó xa các tenant (backend sandbox từ chối
Unix socket vì lý do đó).

### Các mức cô lập

Một container dùng chung kernel của host. Các system call mà một lệnh thực hiện đi vào
kernel đó, nên một lỗi trong nó có thể để một lệnh thoát ra khỏi container. Backend `sandbox`
có cùng rủi ro còn lại (Landlock và seccomp cũng là tính năng của kernel). Với các lệnh đến từ
nội dung không đáng tin, hoặc trên một server được nhiều tenant dùng chung, hãy chạy các
container dưới một runtime mạnh hơn. Từ yếu nhất đến mạnh nhất:

| Mức | Cô lập | Ghi chú |
|---|---|---|
| `local` | Không gì ngoài user của server | Chỉ cho agent đáng tin |
| `sandbox` | Landlock, seccomp, rlimit trên kernel của host | Không có daemon; Linux 6.12 trở lên |
| `docker` với `runc` (mặc định của Docker) | Namespace, cgroup, capability bị bỏ, trên kernel của host | Rủi ro còn lại là một lỗi thoát qua kernel hoặc runtime |
| `docker` với gVisor (`runsc`) | Một kernel ở user-space trả lời các system call của container; ít cái chạm tới host | Chỉ Linux; xem các chi phí bên dưới |
| `docker` với một runtime VM (Kata Containers, Firecracker) | Một guest kernel trong một máy ảo | Mạnh nhất; cần ảo hóa trên host |

gVisor không cần thay đổi gì ở linch-rust, vì nó là một runtime của Docker. Hãy cài `runsc`
và đăng ký nó với daemon, rồi hoặc đặt nó làm mặc định của daemon, trong
`/etc/docker/daemon.json`, để mọi `docker run` mà backend phát ra đều dùng nó:

```json
{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}, "default-runtime": "runsc"}
```

hoặc để nguyên mặc định và trỏ `docker_path` của policy tới một wrapper chỉ thêm runtime vào
`run`:

```sh
#!/bin/sh
# /usr/local/bin/docker-runsc
if [ "$1" = run ]; then shift; exec docker run --runtime=runsc "$@"; fi
exec docker "$@"
```

```python
execution = {"backend": "docker", "docker_path": "/usr/local/bin/docker-runsc"}
```

Trên một server, hãy đặt cùng `docker_path` đó trong mục `allow.execution` của tenant, mục mà
một cấu hình phải khớp.

Chi phí của gVisor: một lệnh thực hiện nhiều system call chạy chậm hơn (các báo cáo cho thấy
chậm hơn hàng chục phần trăm với loại việc đó) và một container khởi động lâu hơn; một số
system call và tệp `/proc` và `/sys` chưa được cài đặt, nên một tool cần chúng sẽ lỗi; nó chỉ
chạy trên Linux. Hãy đo các lệnh của chính bạn trước khi dựa vào nó. Với một runtime cấp VM,
hãy đăng ký nó với daemon theo cách tương tự và dùng nó thay cho `runsc`.

Backend docker không được kiểm chứng với một Docker daemon trong CI của linch-rust, và hoàn
toàn không được kiểm chứng với gVisor hay một runtime VM: hãy thử cấu hình của bạn với một lệnh
bắt buộc phải lỗi (đọc một tệp ngoài workspace) trước khi tin vào nó.

## Kiểm thử live

`python/tests/live/test_ts_backends.py` chạy từng đảm bảo dưới đây qua một `linch-server` thật và
một backend thật, với model có kịch bản (không có LLM). Tên mỗi test nêu đảm bảo của nó. Một case
bị bỏ qua kèm lý do khi máy không chạy được (không phải Linux, không có Landlock ABI 6, không truy
cập được docker), nên CI không có docker vẫn xanh.

| Đảm bảo | `local` | `sandbox` | `docker` |
|---|---|---|---|
| Lệnh trả output và exit status; run sống sót khi một lệnh lỗi | có | có | có |
| Lệnh bắt đầu trong workspace và tệp của nó là tệp của công cụ file | có | có | có |
| Không bí mật nào của server lọt vào môi trường của lệnh | có | có | có |
| `environ` của server không đọc được từ lệnh | n/a | có | có |
| Ghi ngoài workspace không tới được host (kể cả tool tệp, khi một lệnh hoán đổi thư mục thành symlink) | n/a | có | có |
| Không đọc được tệp host nằm cạnh workspace (kể cả tool tệp, cùng phép hoán đổi) | n/a | có | có |
| Syscall bị cấm cho kết quả lỗi, không làm sập | n/a | có | n/a |
| Lệnh không `ptrace` được tiến trình ngoài sandbox | n/a | có | n/a |
| Không có capability, `no-new-privileges`, `mount` bị từ chối | n/a | n/a | có |
| Giới hạn bộ nhớ, thời gian CPU, cỡ tệp lỗi bên trong lệnh | n/a | có | chỉ bộ nhớ |
| Fork bomb bị chặn và run sống sót | n/a | có | có |
| Mạng tắt theo mặc định | n/a | có | có |
| Timeout giết cả cây tiến trình, không mồ côi | có | có | có |
| Luồng 100 MB bị cắt và bộ nhớ server vẫn nhỏ | có | có | có |
| Hủy run không để lại tiến trình hay container | có | có | có |
| `kill -9` server không để lại tiến trình (lệnh nền với `local` và `sandbox`) hay container | có | có | có |
| `docker inspect` cho thấy user, capability bị bỏ, giới hạn, root chỉ đọc, mạng và một mount duy nhất | n/a | n/a | có |

Các case `sandbox` và `local` chạy khi đặt `LINCH_SERVER_BIN`:

```sh
cd linch-rust/python
LINCH_SERVER_BIN=/path/to/linch-server pytest -m live tests/live/test_ts_backends.py
```

Các case `docker` còn cần `LINCH_LIVE_DOCKER=1`, để không bao giờ chạy nhầm. Chúng dùng
`LINCH_LIVE_DOCKER_IMAGE` (mặc định `alpine:3.20`, tự pull nếu thiếu), giới hạn mỗi container
(256 MiB, nửa CPU, 64 tiến trình) và đặt tên `linch-live-<id>` qua một wrapper `docker_path`, nên
`docker ps -a --filter name=linch-live-` tìm ra mọi container còn sót. Người dùng truy cập daemon
qua nhóm `docker` chạy chúng như sau:

```sh
LINCH_LIVE_DOCKER=1 LINCH_SERVER_BIN=/path/to/linch-server \
  sg docker -c 'pytest -m live tests/live/test_ts_backends.py'
```

Các đảm bảo khác (đường dẫn Landlock, allowlist `/etc`, namespace, socket Unix, sandbox bật mạng)
được phủ bởi `crates/linch-tools/tests/sandbox.rs` và `python/tests/live/test_ts_sandbox.py`.

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
`crates/linch-tools/src/exec.rs`) với bốn backend dựng sẵn được chọn bằng cấu hình thay
vì bằng object; backend `remote` thay cho remote shell của Python bằng cách nhờ
`ExecutionHost` của client chạy từng lệnh ([Thực thi từ xa](./remote-execution.md)), và
backend sandbox không có đối ứng trong Python. `TempDirIsolation` của Python cho mỗi nhánh subagent một bản làm việc
riêng; home riêng của sandbox là theo session, và workspace vẫn dùng chung với các tool
tệp.
