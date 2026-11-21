# Benchmark

[Bản gốc tiếng Anh](../benchmarks.md)

linch-rust tốn bao nhiêu so với Python Linch, nó hoạt động ra sao khi bị kill hoặc chịu tải,
và những gì chưa được đo. Mỗi con số đều nêu rõ lần chạy mà nó đến từ. Các lần chạy dùng một
host chung 64 thread, host này cũng chạy các việc khác, nên hãy đọc giá trị tuyệt đối là
"trên một host đang có tải" và chỉ so sánh các ô đã chạy trong cùng một lần chạy.

## Thiết lập

| | |
|---|---|
| Host | 2 x Intel Xeon Silver 4514Y (64 hardware thread), 251 GB RAM, Ubuntu 24.04.4, Linux 7.0.0-34 |
| Postgres | 16.15, loopback, `max_connections` 100, `synchronous_commit on`, `fsync on` |
| Toolchain | rustc 1.99.0, release profile của repository (`lto = "fat"`, `codegen-units = 1`); Python 3.14.10 cho cả hai phía Python |
| Lần chạy đo lại ("final") | `origin/master` `617715b3` (gồm bản sửa độ bền vững lúc bắt đầu run, tool schema dùng chung, các bản sửa SGLang), `linch-server` sha256 `3749f7e79789ac69...`, 2026-11-19, load average 1 phút từ 2.3 đến 9.2 qua các round, 3 round xen kẽ |
| Lần chạy đo lại case 6 ("lần chạy case 6") | `origin/master` `1da43ee3` (gồm bản viết lại tách khung SSE, #211), bản build release của `linch-server` và `bench_driver`, các phía Python từ cùng cây mã với `linch-native` build từ `crates/linch-python`, 2026-11-21, load average 1 phút từ 2.2 đến 2.9 qua các round, 4 round đổi thứ tự các phía, mọi tiến trình ghim vào một core (xem [Giải mã stream](#giải-mã-stream-case-6-lần-chạy-case-6-4-round-x-20-lần-lặp)) |
| Lần chạy trước ("earlier") | `origin/master` `50eda478`, 2026-11-18, load average 1.9 đến 6.9, một lượt (3 lượt cho các ô bộ nhớ) |
| Provider | fixture có kịch bản và server loopback cho mọi con số hiệu năng và bộ nhớ; provider thật chỉ có trong [Quy mô và chạy thật](#quy-mô-và-chạy-thật) |

Ba phía được so sánh, như trong `linch-rust/bench`:

- `py`: Python Linch trong một tiến trình.
- `rs-client`: client `linch_rust` qua sidecar `linch-server` trên stdio, con đường một ứng dụng Python dùng.
- `rs-kernel`: kernel Rust được gọi trực tiếp (mức sàn, không có RPC).

Nơi một ô là một khoảng, nó có dạng `trung vị (nhỏ nhất đến lớn nhất)` qua 3 round, và giá trị
của mỗi round là trung vị của 20 lần lặp. Độ trễ commit của một bảng Postgres thông thường
(50 lần insert, p50) là 1.7 đến 3.9 ms qua các round của final: đó là mức sàn nằm dưới mọi ô
Postgres.

### Tái hiện

Các case 3, 6, 7 và 14 là harness của chính repository:
`python bench/run_all.py --out DIR --cases 3,6,7,14 --pg-dsn DSN` cùng server binary, ví dụ
`bench_driver` và một venv Python Linch như `linch-rust/bench/README.md` mô tả. Probe bộ nhớ
(một tiến trình `bench_driver`, dạng case 14, `VmRSS` lấy mẫu mỗi 50 ms, có và không có
`MALLOC_ARENA_MAX=2`), harness kill, fault proxy và các ramp tải không nằm trong repository.
Phương pháp của chúng được mô tả trong các phần dùng đến chúng.

## Hiệu năng so với Python Linch

### Loop, tool, khởi động, đồng thời (lần chạy earlier, `50eda478`, không đo lại)

Provider có kịch bản, không có mạng. Tính trên mỗi lượt provider hoặc mỗi lệnh gọi, trung vị của 20 lần lặp, một lượt.

| Ô | py | rs-client | rs-kernel |
|---|---|---|---|
| Loop, memory store, 10 lượt x 1 lệnh gọi | 2.76 ms/lượt | 0.20 | 0.044 |
| Loop, memory store, 50 lượt x 1 lệnh gọi | 3.49 ms/lượt | 0.53 | 0.067 |
| Read / Write / Edit | 4.8 / 4.8 / 5.2 ms/lệnh gọi | 0.39 / 0.20 / 0.26 | 0.20 / 0.17 / 0.17 |
| Grep / Glob | 5.0 / 4.3 ms/lệnh gọi | 0.67 / 0.67 | 0.38 / 0.41 |
| Bash | 17.7 ms/lệnh gọi | 1.19 | 1.03 |
| Grep trên 2000 tệp | 111 ms/lệnh gọi | 12.7 | 12.5 |
| MCP stdio echo | 4.86 ms/lệnh gọi | 0.68 | 0.71 |
| Khởi động nguội (import, open, run đầu tiên, thoát) | 425 ms | 184 | 11.7 |
| 16 session đồng thời của một loop 10 lượt | 56 run/s | 985 | 1986 |

Chi phí RPC của sidecar (`rs-client` trừ `rs-kernel`) là 0.16 đến 0.46 ms mỗi lượt.
Các hàng này có lần lặp ngắn (2 đến 27 ms cho các loop của `rs-client`); độ phân tán của chúng
trong một lần chạy là 39 đến 62 phần trăm, nên hãy đọc chúng như một bậc độ lớn.

### Loop Postgres, case 3 (lần chạy final)

Loop với kho Postgres: mỗi lượt commit nhiều lần, nên ô này bị chi phối bởi độ trễ commit và
khoảng cách với Python thu hẹp từ khoảng 10x xuống khoảng 1.2x đến 1.6x. Mili giây cho mỗi
lượt provider, 3 round (load 2.3 đến 2.7 lúc bắt đầu mỗi round).

| Dạng | py | rs-client | rs-kernel |
|---|---|---|---|
| 10 lượt x 1 lệnh gọi | 20.6 (19.6 đến 24.7) | 15.4 (14.6 đến 16.4) | 16.4 (13.8 đến 17.8) |
| 10 lượt x 3 lệnh gọi | 31.2 (30.8 đến 35.1) | 25.4 (19.2 đến 26.6) | 23.1 (19.5 đến 27.1) |
| 50 lượt x 1 lệnh gọi | 22.2 (21.3 đến 23.0) | 16.3 (14.7 đến 16.8) | 16.5 (16.3 đến 16.8) |
| 1 lượt x 8 lệnh gọi | 48.0 (36.9 đến 50.8) | 38.2 (36.1 đến 38.7) | 34.5 (34.5 đến 36.0) |

Số lần ghi mỗi run (tuple, 10x1 / 10x3 / 50x1 / 1x8): py 203 / 283 / 923 / 69, linch-rust 190 /
270 / 870 / 65. So với lần chạy earlier, vốn có lần lặp riêng ở load 2.2 (py 19.3 / 30.3 /
21.6 / 52.4, rs-client 16.2 / 24.9 / 17.0 / 38.4, rs-kernel 16.0 / 23.0 / 18.1 / 34.4), không
ô nào dịch chuyển ra ngoài độ phân tán.

### Hàng đợi worker, case 7 (lần chạy final)

100 job mỗi lần lặp trên Postgres. Client không lộ worker ra, nên `rs-client` không có ô.
Job mỗi giây: py 94 (91 đến 115), rs-kernel 271 (232 đến 271). Lượt đơn lẻ của earlier đọc
được 117 và 353; cả hai phía giờ đều đọc thấp hơn với tỷ lệ tương tự (rs-kernel so với py:
3.0x ở earlier, 2.9x bây giờ), điều này chỉ về tải của host hoặc Postgres trong giờ đó (p50
commit 1.8 đến 3.9 ms qua ba round), không phải một thay đổi ở phía nào.

### Session đồng thời trên Postgres, case 14 (lần chạy final)

M session bắt đầu cùng lúc trên một agent, mỗi session 4 lượt, độ trễ provider 1 s mỗi lượt
(thời gian run lý tưởng 4 s), kho Postgres. Wall time p50 của run tính bằng giây và số lượt
mỗi giây (4 x M trên cửa sổ đo).

| M | py | rs-client | rs-kernel | rs-client, 4 sidecar |
|---|---|---|---|---|
| 100 | 4.97 s, 73/s | 4.35 s, 91/s | 4.20 s, 95/s | 4.15 s, 96/s |
| 500 | 13.4 s, 89/s | 5.47 s, 351/s | 4.59 s, 424/s | 4.51 s, 439/s |
| 1000 | 25.0 s, 85/s | 7.46 s, 484/s | 5.34 s, 694/s | 4.83 s, 800/s |
| 2000 | 46.5 s, 89/s | 10.5 s, 686/s | 6.25 s, 1078/s | 5.76 s, 1308/s |

Các khoảng qua 3 round nằm trong `finalbench/RESULTS.md` của bản ghi lần chạy; các khoảng rộng
nhất là `rs-kernel` ở M=1000 (5.08 đến 13.11 s: một round trùng với việc khác trên host, hai
round còn lại đọc 5.1 và 5.3 s) và `rs-client` ở M=1000 (6.85 đến 8.59 s). Python làm bão hòa
event loop của nó ở khoảng 85 đến 90 lượt mỗi giây từ M=500 trở lên (CPU 20.7 s ở M=500,
87.6 s ở M=2000); linch-rust tiếp tục mở rộng. Có 0 run thất bại ở mọi ô của mọi round.

### Giải mã stream, case 6 (lần chạy case 6, 4 round x 20 lần lặp)

Một server loopback (`sse_server.py`) phát lại cùng một stream SSE có dạng đã ghi cho mỗi vendor, và mỗi phía giải mã nó
qua adapter provider của chính nó; một lần lặp là một run chỉ có văn bản. Phần này thay các bảng case 6 trước đó, vốn đến
từ các bản build trước khi viết lại tách khung SSE (#211) và không còn được hiển thị.

Cách đo: harness `linch-rust/bench/run_all.py --cases 6` ở `1da43ee3`, mặc định 20 lần lặp tính giờ mỗi ô (1 lần khởi
động), mỗi ô một tiến trình. Mỗi phía được chạy riêng, và ba phía chạy nối tiếp nhau trong 4 round, đổi thứ tự
(py, rs-client, rs-kernel; rồi rs-kernel, rs-client, py; và cứ thế). Toàn bộ harness (coordinator, server phát lại, tiến
trình của phía được đo và, với `rs-client`, cả sidecar) được ghim vào một core bằng `taskset -c 26`, nên ba phía dùng
chung core đó với server phát lại. Governor là `powersave` và không đổi được từ user space, nên `scaling_cur_freq` của
core được đọc mỗi 50 ms trong từng lần chạy. Load average của host là 2.2 đến 2.9 do các việc khác, không có việc nào trên
core đã ghim. Giá trị mỗi round là trung vị của 20 lần lặp; bảng cho trung vị qua 4 round và, trong ngoặc, round thấp nhất
và cao nhất.

| Vendor (sự kiện, byte) | Phía | Wall ms | MB/s | sự kiện/s |
|---|---|---|---|---|
| DeepSeek (20 004, 4.07 MB) | py | 1260 (1254 đến 1270) | 3.2 (3.2 đến 3.2) | 15.9 k (15.7 đến 16.0) |
| | rs-client | 94.4 (93.8 đến 98.8) | 43.1 (41.2 đến 43.4) | 211.8 k (202.5 đến 213.3) |
| | rs-kernel | 26.1 (25.7 đến 26.2) | 156.1 (155.6 đến 158.2) | 767.2 k (764.9 đến 777.5) |
| OpenAI chat (20 004, 4.03 MB) | py | 1268 (1263 đến 1290) | 3.2 (3.1 đến 3.2) | 15.8 k (15.5 đến 15.8) |
| | rs-client | 94.1 (93.3 đến 96.7) | 42.8 (41.7 đến 43.2) | 212.6 k (206.9 đến 214.3) |
| | rs-kernel | 25.9 (25.8 đến 26.5) | 155.4 (152.2 đến 156.3) | 771.7 k (755.6 đến 776.0) |
| OpenAI responses (20 008, 4.36 MB) | py | 2181 (2172 đến 2191) | 2.0 (2.0 đến 2.0) | 9.2 k (9.1 đến 9.2) |
| | rs-client | 93.9 (93.4 đến 95.9) | 46.4 (45.4 đến 46.6) | 213.0 k (208.6 đến 214.3) |
| | rs-kernel | 24.2 (24.0 đến 24.5) | 180.1 (178.0 đến 181.2) | 827.6 k (817.8 đến 832.5) |
| Anthropic (20 005, 2.49 MB) | py | 287.7 (285.7 đến 293.7) | 8.7 (8.5 đến 8.7) | 69.5 k (68.1 đến 70.0) |
| | rs-client | 90.3 (89.7 đến 95.8) | 27.6 (26.0 đến 27.8) | 221.5 k (208.7 đến 223.1) |
| | rs-kernel | 23.2 (22.8 đến 23.9) | 107.1 (104.2 đến 109.0) | 860.7 k (837.4 đến 876.2) |
| Gemini (20 000, 2.99 MB) | py | 1809 (1796 đến 1819) | 1.7 (1.6 đến 1.7) | 11.1 k (11.0 đến 11.1) |
| | rs-client | 95.3 (94.8 đến 100.9) | 31.4 (29.6 đến 31.5) | 210.0 k (198.2 đến 210.9) |
| | rs-kernel | 27.4 (27.2 đến 28.0) | 109.1 (106.6 đến 109.9) | 730.2 k (713.0 đến 735.3) |

Tỉ lệ wall time (trung vị qua các round): `py` so với `rs-client` là 13.3x (DeepSeek), 13.5x (OpenAI chat), 23.2x (OpenAI
responses), 3.2x (Anthropic) và 19.0x (Gemini); `py` so với `rs-kernel` là 48.3x, 48.9x, 90.2x, 12.4x và 66.1x;
`rs-client` so với `rs-kernel` là 3.5x đến 3.9x ở mọi vendor. Các ô Python khớp với lần chạy trước (DeepSeek 1255 đến 1266
ms trước đó, 1254 đến 1270 ms bây giờ; Anthropic 283 ms trước đó, 287.7 ms bây giờ).

Tần số và khả năng so sánh: ở mọi ô của mọi round, trung vị các mẫu của core là 3.40 GHz. Mẫu thấp nhất của một ô là
2.52 đến 3.38 GHz, và tỉ lệ mẫu dưới 3.3 GHz là 0 đến 9% với `rs-kernel`, 4 đến 25% với `rs-client` và 5 đến 16% với `py`,
tùy vendor (các ô `rs-kernel` kéo dài khoảng 1 s, nên mỗi ô có khoảng 40 mẫu; các lần tụt nằm ở đâu trong một ô không được
ghi lại). Trước lần chạy của một phía, core nhàn rỗi đọc 0.78 đến 2.7 GHz và sau đó là 2.4 đến 3.4 GHz. Trong một ô, lần
lặp chậm nhất trong 80 lần (4 round) nhiều nhất là 1.38x lần nhanh nhất, nên sự tách hai mức của các lần chạy trước không
xuất hiện. Cả 15 ô cùng một dải tần số và wall time của chúng so sánh được với nhau. Chúng không so sánh được với các con
số case 6 trước đó: khác bản build, và các lần chạy trước không ghim harness theo cách này.

Những gì các hàng không cho thấy: server phát lại dùng chung core đã ghim với phía được đo, nên mỗi wall time gồm cả thời
gian server tạo stream trên core đó; phần này lớn bao nhiêu thì không được đo. `rs-client` chạy với client và sidecar
trên cùng một core, không phải trên các core riêng.

Chi phí kernel mỗi sự kiện (wall của `rs-kernel` chia cho số sự kiện, trung vị qua các round): DeepSeek 1.30 us, OpenAI chat
1.30 us, OpenAI responses 1.21 us, Anthropic 1.16 us, Gemini 1.37 us. Thời gian mỗi sự kiện của Anthropic cao hơn DeepSeek
1.42 lần ở lần chạy trước không còn trong lần chạy này: Anthropic mất 0.89 lần thời gian của DeepSeek mỗi sự kiện. Bản viết
lại tách khung SSE (#211) nằm giữa các bản build trước (`d1f3cfe5`, `617715b3`) và bản này. Không có lần chạy nào của cùng
một bản build trước và sau #211, nên việc sự thay đổi có đến từ nó hay không, và bao nhiêu, không được tách riêng ở đây. Đường Anthropic của Python trong lần chạy này cũng nhanh hơn
các đường SDK OpenAI mỗi sự kiện (69.5 k so với 15.8 đến 15.9 k sự kiện/s); profile trước đó quy điều đó cho việc SDK
Anthropic kiểm tra một sự kiện bằng một lần gọi pydantic-core so với khoảng 10 lần gọi `construct_type` mỗi chunk trong SDK
OpenAI, và điều này không được profile lại. Xem [Khoảng trống đã biết](#khoảng-trống-đã-biết).

## Bộ nhớ theo session

### Bộ nhớ đỉnh ở M session đồng thời (dạng case 14)

Cùng ô như trên, kho Postgres. Bộ nhớ resident đỉnh của cây tiến trình của phía đó (client cộng
sidecar cho `rs-client`), trung vị (khoảng) qua 3 round, và cùng giá trị chia cho M.

| M | py | rs-client | rs-kernel | lần chạy earlier (`50eda478`): py / rs-client / rs-kernel |
|---|---|---|---|---|
| 500 | 159 MB (156 đến 160), 0.32 MB/session | 136 MB (135 đến 139), 0.27 | 134 MB (128 đến 134), 0.27 | 159 / 255 / 277 MB |
| 1000 | 186 MB (182 đến 186), 0.19 | 227 MB (224 đến 228), 0.23 | 248 MB (239 đến 252), 0.25 | 185 / 477 / 526 MB |
| 2000 | 244 MB (239 đến 244), 0.12 | 411 MB (406 đến 421), 0.21 | 478 MB (463 đến 502), 0.24 | 242 / 914 / 1021 MB |

Sự khác biệt giữa hai lần chạy ở các phía Rust đến từ một thay đổi: `ToolDefinition.input_schema`
giờ là một `Arc` được registry, catalog, prefix đã ghi log và mọi request dùng chung, nên một
session không còn deep-copy các tool schema. Heap profile của code trước cho thấy khoảng 100 MB
trong 178 MB heap đang sống nằm ở các bản sao đó, trong một tiến trình 574 MB; phần còn lại là
allocator giữ lại bộ nhớ. Các ô của Python không đổi (185 đến 186 MB và 242 đến 244 MB), đúng như dự kiến.

Ở M=2000 các phía Rust giờ dùng 1.7x (`rs-client`) và 2.0x (`rs-kernel`) bộ nhớ của Python cho
mỗi session (trước đó là 3.8x và 4.2x). Con số theo session của Python giảm khi M tăng vì mức
nền của nó (khoảng 42 MB ở 0 agent) được chia cho nhiều session hơn.

### Allocator và nút `MALLOC_ARENA_MAX` (probe bộ nhớ `rs-kernel`, lần chạy final)

Một tiến trình `bench_driver`, 1000 hoặc 2000 session đang chạy trên Postgres, mỗi session 4
lượt, `VmRSS` đỉnh lấy mẫu mỗi 50 ms, 3 round xen kẽ, 0 session thất bại.

| M | mặc định (glibc) | `MALLOC_ARENA_MAX=2` | tiết kiệm |
|---|---|---|---|
| 1000 | 268 MB (265 đến 269) | 207 MB (205 đến 207) | 23 % |
| 2000 | 492 MB (491 đến 511) | 344 MB (341 đến 357) | 30 % |

Probe trước đó ở cùng ô, trước khi có tool schema dùng chung, đọc 550 MB (545 đến 567) ở
M=1000 và 463 MB với giới hạn arena; riêng thay đổi tool schema đã đưa mức mặc định xuống
268 MB. Hãy đặt `MALLOC_ARENA_MAX=2` trong môi trường của server cho một tiến trình phục vụ
nhiều session cùng lúc ([Nhúng](./usage/embedding.md#định-cỡ-những-gì-đã-được-đo)).

### Agent nhàn rỗi

Memory store, một agent cho mỗi `open_agent`, N agent được mở rồi N run cùng lúc (4 lượt, mỗi
lượt 3 lệnh gọi tool), tổng bộ nhớ resident của client cộng sidecar (`rs`) hoặc của một tiến
trình (`py`), trung vị của 3 lần lặp lại, tính bằng MB. Giá trị của lần chạy earlier nằm trong ngoặc.

| | py | rs |
|---|---|---|
| Mức nền, 0 agent | 42.2 (42.1) | 38.8 (34.8) |
| N=100 đã mở, nhàn rỗi | 43.6 | 66.5 |
| N=1 đang chạy | 76.7 (76.7) | 50.6 (47.2) |
| N=10 đang chạy | 80.2 (80.2) | 54.1 (53.9) |
| N=50 đang chạy | 95.6 (95.5) | 72.6 (87.5) |
| N=100 đang chạy | 115.6 (115.7) | 92.1 (126.7) |
| KB biên cho mỗi agent, nhàn rỗi (hồi quy tuyến tính, N >= 10) | 13 (14) | 174 (261) |
| KB biên cho mỗi agent sau một run | 405 (404) | 426 (833) |
| Run đầu tiên của agent đầu tiên | +34.5 MB | +11.0 MB |

Bộ nhớ không trở về mức nền sau khi các agent đóng (N=100, 3 s sau khi đóng: py 115.9, rs 92.4
MB) và một đợt thứ hai trong cùng tiến trình làm nó tăng thêm 0.5 đến 2 MB: allocator giữ lại
để tái sử dụng, không phải rò rỉ. Run đầu tiên của Python tốn 34 MB vì lúc đó nó import SDK
provider và các tool. Dưới N=100 phía Rust đã nhỏ hơn; ở N=100 giờ nó nhỏ hơn (92 so với 116
MB) trong khi trước đó lớn hơn (127 so với 116 MB). Các ô mức nền và N=1 của `rs` cao hơn 3
đến 4 MB so với lần chạy earlier; điều đó chưa được điều tra.

### Khoảng trống còn lại và những gì đã biết về nó

Python vẫn tốn ít hơn cho mỗi session ở M=2000 (0.12 MB so với 0.21 đến 0.24). Các nguyên nhân
đã biết, từ heap profile của code trước, chưa được lặp lại trên bản build có schema dùng
chung: khoảng 28 KB future của run được box cho mỗi run, khoảng 26 MB buffer đối số sqlx và
mã hóa session-entry của các lần ghi đang chạy ở M=1000, các kết quả gần đây của `LoopGuard`,
và allocator giữ lại bộ nhớ qua các worker thread. Khoảng 0.04 MB cho mỗi lượt thêm đã được
đo và không được profile lại sau thay đổi schema. Giải mã stream, bộ nhớ và số thread của
server mỗi thứ chỉ được profile một lần; không cái nào trong số này là một phép kế toán chính xác.

## Độ tin cậy

Đo trên `50eda478` với provider OpenAI-chat có kịch bản qua HTTP và kho Postgres, trừ khi một
hàng ghi final (`617715b3`). Mỗi lần chạy dùng một transcript cố định (4 lượt, 6 lệnh gọi
host-tool ghi một ledger) và kiểm tra các event đã lưu, ledger và usage sau khi kill.

| Kiểm tra | Kết quả |
|---|---|
| `kill -9` vào một thời điểm ngẫu nhiên, 1 và 4 run đồng thời | 991 run bị kill trên 1000 run đã khởi chạy, 0 vi phạm bất biến (seq liên tục, đúng một kết quả, không có hiệu ứng tool nào được áp dụng hai lần, usage bằng tổng các receipt được chấp nhận); cận trên 95 % là 0.39 % cho 0 trên 991. 3 run không kết luận được trong harness (2 `RunExists`, 1 `RunTerminal`) và không được tính là đạt |
| `kill -9` trong cửa sổ giao nhận (các chunk cuối của một response, tối đa 6 ms sau byte cuối của nó) | 100 lần kill, 0 vi phạm; lệnh gọi provider đã trả tiền bị lặp lại ở 14 trong 100 (13 trong số đó bị kill ở chunk cuối, `[DONE]`) |
| Lệnh gọi provider đã trả tiền bị lặp lại cho mỗi lần kill, mọi lần kill | 4 trên 991 lặp lại một lần, không cái nào hai lần (mỗi cái là một response được giao đầy đủ mà receipt của nó chưa bền vững) |
| Tiếp quản sau `kill -9` | p50 2.67 s ở lease 3 s (tối đa 3.45 s); p50 29.3 s ở mặc định 30 s; từ lúc resume được chấp nhận đến event đầu tiên p50 16 đến 18 ms |
| Kill ép buộc ngay sau câu trả lời của `run.start`, 160 run (lần chạy final, `617715b3`; độ trễ được lấy với trung bình 50 ms) | 0 trên 160 không tìm thấy run (`RunNotFound`), 0 vi phạm bất biến. Trước bản sửa, cùng harness tìm thấy 70 trên 160 id đã được xác nhận không bền vững. Độ trễ kill sau câu trả lời: nhỏ nhất 20, trung vị 61, lớn nhất 112 ms |
| `SIGTERM` với một run tương tác và một job đang chạy | thoát ở drain timeout (1.52 s cho 1500 ms); 100 trên 100 lease được giải phóng; một server thứ hai nhận run tương tác trong 81 ms; job kết thúc 0.39 s sau khi thoát với 0 thất bại được đếm. Chỉ run tương tác: thoát trong 28 ms. `SIGKILL` cùng dạng: lease được giữ, tiếp quản sau lease (2.7 s) hoặc visibility timeout của job (29.9 s) |
| Độ trễ Postgres +50 ms và +500 ms mỗi chiều, 30 run mỗi mức | 30 trên 30 thành công, 0 vi phạm, không mất lease nào (+500 ms: run mất 129 s thay vì 1.4 s) |
| Postgres reset kết nối mỗi 10 s | 60 trên 60 run kết thúc ngay lần đầu |
| Reset mỗi 1 s (stress) | 27 trên 30 stream kết thúc với `RunError` (-32020, -32024); cả 30 đạt thành công sau resume hoặc phát lại cùng run id; 0 vi phạm |
| Postgres blackhole 40 s, lease 30 s | 5 trên 30 stream kết thúc `-32024` (hết thời gian lấy kết nối từ pool), tất cả resume đến thành công; với một server thứ hai, nó tiếp quản và các lần ghi của server thứ nhất bị từ chối: 0 event, hiệu ứng tool hoặc kết quả bị trùng |
| Lỗi provider, 12 kịch bản x 30 run (429, 503, 500, cắt stream, treo, fallback) | mỗi kịch bản mang tính xác định; số lần retry bị chặn (tối đa 5 trên primary, 1 trên backup); `total_usage` bằng đúng một response hoàn thành ở cả 360 run; một stream bị cắt hoặc 500 được đánh dấu `usage_complete: false` |
| Resume một session dài (event đầu tiên) | 1011 entry: p50 33 ms (p99 43); 9981 entry: p50 112 ms (p99 125), mỗi mức 20 run |
| Bộ test live trên vLLM | 196 đạt, 1 bỏ qua (cần một key DeepSeek), 1 thất bại rồi đạt khi chạy lại (race trong test harness), 0 lỗi sản phẩm |

Các hàng kill và lỗi là số lượng run, không phải tỷ lệ: 0 trên 991 là một cận, không phải lời hứa về số không.

## Quy mô và chạy thật

### 2500 run đồng thời (lần chạy earlier, `50eda478` và `4412ad65`)

Một `linch-server` (stdio, nên không có giới hạn tenant), một agent, N run cùng lúc, mỗi run
một lượt nhỏ (`max_tokens` 16, tắt retry, memory store trừ khi nói khác), 0 lỗi ở mọi N trong
mọi bảng dưới đây.

| Provider | N | Kết quả |
|---|---|---|
| DeepSeek (thật) | 10 đến 2500 | 2500 trên 2500 ok, 0 lỗi (0 x 429, 0 x 5xx, 0 timeout). Throughput đạt đỉnh ở 140 đến 173 run/s từ N=250; p50 của run 7.5 s ở N=2500. Server 180 MB RSS, 0.12 core |
| vLLM (thật) | 10 đến 2500 | 2500 trên 2500 ok. 124 run/s, p50 của run 10.8 s; vLLM chạy khoảng 32 đến 38 sequence cùng lúc và xếp hàng phần còn lại. Server 206 MB RSS, 0.07 core |
| vLLM, listener đa thread với một tenant | 100, 1000, 2500 | 2500 trên 2500 ok; 65 thread, 218 MB RSS, 0.16 core |
| Kho Postgres, 1 agent, 1000 run | 10 đến 1000 | 0 lỗi trên cả hai provider; pool 32 kết nối là trần (32 backend của chúng ta cộng 1 kết nối watcher), 9 đến 11 commit mỗi run ở N >= 100 |

Hãy đọc những con số này là "2500 run được chấp nhận và hoàn thành", không phải 2500 request
đang chạy tại provider. DeepSeek giới hạn số request đang chạy ở 311 cho số dư của tài khoản
này (một HTTP 429 trên mức đó, từ một thử nghiệm trực tiếp với 500 kết nối đồng thời); qua
linch khoảng 110 đến 170 request đang chạy, phù hợp với giới hạn 128 stream cho mỗi kết nối
HTTP/2 mà DeepSeek công bố (số kết nối không được đo). Linch thêm khoảng 0.06 đến 0.1 s mỗi
run ở N thấp và không thấy gì ở mức bão hòa. Các số liệu quy mô dùng một server stdio không có
tenant; `max_runs` của một tenant mặc định là 16 và server từ chối run thứ 17 với `-32005`
([Server](./usage/server.md)). Hãy tăng nó trong cấu hình tenant cho tải như thế này.

### Các ramp vLLM và SGLang (thời điểm và thiết lập khác nhau)

Một lượt nhỏ cho mỗi run, tắt retry, memory store, 0 lỗi đến N=1024 trên cả hai. Trung vị của 3
mẫu đến N=256 (N=512 và 1024: một mẫu). Linch so với một client HTTP thuần (một kết nối cho
mỗi request, cùng body), wall time của run tính bằng giây.

| N | vLLM linch / trực tiếp | SGLang linch / trực tiếp |
|---|---|---|
| 1 | 0.11 / 0.11 | 0.05 / 0.04 |
| 32 | 0.38 / 0.32 | 0.11 / 0.11 |
| 128 | 1.16 / 1.12 | 0.30 / 0.29 |
| 256 | 2.03 / 2.12 | 0.52 / 0.58 |
| 1024 | 6.85 / 8.00 | 2.43 / 2.10 |

Đây không phải một phép so sánh giữa các engine. SGLang 0.5.23 (snapshot NVFP4, 71 request
đang chạy) chạy một mình trên một lát GPU trống ở `d9beaa3a`; vLLM 0.32.0 (64 sequence,
speculative decoding) chạy trên GPU production dùng chung ở `13c7266b`. Kết quả mà chúng hỗ
trợ: linch nằm trong độ phân tán của một client thuần trên cả hai engine và không thêm lỗi nào.

### Cô lập cache theo tenant trên vLLM

Với `allow.cache_isolation` bật, hai tenant gửi cùng một prompt ~7200 token: run đầu tiên của
một tenant đọc 0 token đã cache, run thứ hai của nó đọc 4352, và run đầu tiên của tenant kia
đọc 0. Một tenant tắt isolation cache riêng biệt với cả hai. Ở khoảng 3000 token vLLM không
cache gì trên model này (block của nó là 2176 token), nên không có kết quả nào được khẳng định
ở kích thước đó. Bản thân trường `cache_salt` không được bắt trên đường truyền; bằng chứng là hành vi.

## Khoảng trống đã biết

### Chưa giải thích được

- Các ô Postgres của case 3 có những chuỗi lần lặp kéo dài 3 đến 22 s (so với 0.1 đến 1 s)
  ở 4 trong 12 ô của lần chạy earlier, trên `py` và `rs-kernel`, và không có ở `rs-client`.
  Ở lần chạy final: 0 trên 720 lần lặp vượt quá 5x trung vị của ô của nó. Chưa giải thích được;
  host và Postgres không được đặt công cụ đo trong các lần đứng hình của earlier.
- Case 6 ở các lần chạy trước (các bản build trước #211) chuyển giữa hai mức trong một ô, cách nhau khoảng 1.7x (`rs-kernel` Anthropic 290 so với
  510 ms, `rs-client` 380 so với 580 ms), ở cả ba phía. Hai mức là mức tần số CPU: governor là
  `powersave` với `intel_pstate` và HWP, base 2.0 GHz, turbo 3.4 GHz, tỉ lệ 1.70. Trong một lần profile
  60 lần lặp client đã ghim với công việc giống hệt, 48 lần dùng 507 đến 626 ms CPU của server với
  core đọc được trung vị 1998 MHz, và 12 lần liên tiếp dùng 304 đến 313 ms ở trung vị 3358 MHz. Ghim
  không chọn được mức: một core bận vẫn ở 2.0 GHz hàng chục giây trong một microbenchmark giải mã.
  Lần chạy final không ghi tần số, nên 594 so với 295 ms của nó trộn hai mức; hàng Anthropic đã được đo lại với mức ghi theo từng lần lặp. Ở lần chạy case 6 (phần trên), trung vị tần số của core là 3.40 GHz ở mọi ô và không ô nào tách làm hai; vì sao các lần chạy trước tách thì chưa xác định được. Vì sao HWP giữ một
  core bận ở base trong nhiều giây chưa xác định được (cần công cụ root: MSR, `turbostat`).
- Phương pháp: mỗi ô benchmark phải ghi mức tần số CPU của từng lần lặp và so sánh ở cùng mức.
- Đường client của case 6 so với kernel, đã profile trên một bản build trước #211 và chưa lặp lại từ đó (3.4 GHz, pipe 64 KiB, 30 lần lặp mỗi nấc, trung
  vị). Giả thuyết trước đó rằng chi phí theo số message JSON-RPC bị bác bỏ ở case này. Một run gửi
  khoảng 61 dòng cho 20 006 envelope, vì server nối các envelope đang xếp hàng của một run thành một
  dòng tối đa 64 KiB. `orjson.loads` ở client khoảng 12 ms trong khoảng 365 ms. Phần lớn khoảng cách
  còn lại là server chờ pipe stdout 64 KiB đầy: 43 lần chuyển ngữ cảnh tự nguyện mỗi run và bị chặn 39 đến
  56 ms, so với 2 lần và 7 đến 10 ms với pipe 1 MiB. Một stream có nhịp (100 delta/s) gửi một envelope
  mỗi dòng và tốn CPU client, khoảng 3% một core mỗi stream, không tốn wall time. Client Python nới pipe stdout của tiến trình con lên 256 KiB sau khi spawn (`F_SETPIPE_SZ`, Linux; bị từ
  chối thì giữ 64 KiB). Đo qua client thật, xen kẽ, 4 core, n=34 và 36 lần chạy ở 2.0 GHz trên cả core server
  lẫn core client: wall trung vị 566 ms với 64 KiB và 535 ms với 256 KiB (ít hơn 5.6%), số lần chuyển ngữ cảnh
  tự nguyện của server là 44 và 6 mỗi run. Ở core server 3.4 GHz (core client 2.0 GHz) chỉ có n=3 và n=5 lần
  chạy: 375 và 325 ms, chỉ mang tính tham khảo. Pipe từ client sang server không đổi: một run gửi một request
  119 byte.
- Định dạng message trên stdio: msgpack, protobuf và nén đã được đánh giá và bị loại. `python-msgpack`
  dùng nhiều hơn 5 đến 11% CPU client so với orjson cho ít hơn 19% byte. Nén thua trên pipe cục bộ:
  zstd mức 1 cần 1.5 ms để nén stream 3.7 MB, còn pipe chuyển nó trong 1.6 ms. Liên kết WebSocket từ xa
  chưa được đo.
- Case 14 (nhiều session trên Postgres): `rs-client` chậm hơn `rs-kernel` khoảng 1.7x ở M=2000 (10.5 s
  so với 6.25 s), và các kết quả case 6 ở trên không giải thích được điều này. Nó chỉ xuất hiện với
  Postgres trong một sidecar: 4 sidecar mất 5.76 s. Với memory store, một sidecar đạt 1272 đến
  1487 turn/s ở M=2000 (cửa sổ 5.38 đến 6.29 s) và client dùng dưới 20% một core, nên cả message
  JSON-RPC lẫn giải mã ở client đều không giải thích được. Hàng 10.5 s là giới hạn của một thread: một
  sidecar tốn khoảng 1 ms CPU cho mỗi lượt ở 2.1 GHz trên một thread duy nhất của nó (khoảng 700 đến
  750 lượt/s), thêm kết nối pool trên thread đó không được gì (64 kết nối 9.2 s, 80 kết nối 10.1 s, so
  với 9.7 s với 32), còn thêm thread thì có. Đo ở M=2000 với 32 kết nối (run wall p50, trung vị của 3 đến
  4 vòng ở 2.1 đến 2.3 GHz): một thread 9.7 s; một sidecar với 2 worker 9.1 s, 4 worker 7.4 s, 8 worker
  6.9 s, 16 worker 6.4 s; 2 sidecar mỗi sidecar 16 kết nối 7.5 s; 4 sidecar mỗi sidecar 8 kết nối 6.3 s;
  kernel trong tiến trình 6.3 s. CPU của server là 7.8 s với một thread, 13.2 s với 4 worker và 17.1 s
  với 16. Chênh lệch so với kernel là 0.8 s ở M=500, 1.3 s ở 1000 và 3.4 s ở 2000. Việc thời gian chờ
  trong các rào bền vững (khoảng 4.5 s mỗi run, gần như toàn bộ là chờ một kết nối pool) là do một
  thread xoay vòng các kết nối được suy ra, không được profile. `LINCH_STDIO_THREADS` và việc chia sidecar
  được mô tả ở [Chạy server](usage/server.md#nhiều-run-trên-một-sidecar-stdio).
- Số thread của server: các lần chạy DeepSeek đạt 110 thread ở N=2500 (server stdio nếu không
  thì dùng một thread). Cùng một stub loopback (không có provider thật), các run N=1000, memory
  store: khi địa chỉ hóa bằng IP literal server có 1 thread ở 4 trên 4 lần chạy; khi địa chỉ
  hóa bằng tên host `localhost`, 7, 17, 6 và 5 thread. Vậy các thread thừa đi cùng với việc
  phân giải tên host (blocking pool), không phải với số run. 110 thread của các lần chạy
  DeepSeek không được tái hiện với một resolver cục bộ, nên độ lớn của con số đó (độ trễ DNS
  thật, N=2500) chưa được giải thích.

### Chưa đo hoặc còn yếu

- Case 6 (cả năm vendor) được đo lại trên `1da43ee3` ở một dải tần số, thay các ô trước đó. Chưa đo ở đó: cùng một bản build trước và sau #211 (nên ảnh hưởng của bản viết lại tách khung không được tách riêng), server phát lại trên một core riêng khác với phía được đo, `rs-client` với client và sidecar trên các core riêng ở 3.4 GHz, và profile đường client ở trên sau #211. Các mẫu dưới 3.3 GHz nằm ở đâu trong một ô không được ghi lại.
- Loop, tool, khởi động, throughput 16 session và các provider giải mã khác là một lượt từ lần chạy earlier.
- Bảng agent nhàn rỗi chỉ có memory store; các ô bộ nhớ của kho Postgres (backend cho mỗi
  agent, kích thước pool) không được lặp lại trong lần chạy final. Bộ nhớ của một run đang
  chạy và của một agent đã chạy một lần không thể phân biệt trong các dữ liệu này: đỉnh bằng
  con số sau các run.
- Các ô một mẫu: các ramp vLLM và SGLang ở N=512 và 1024, mọi điểm tải DeepSeek và vLLM, kiểm
  tra cache-salt (một run cho mỗi hàng), các hàng `SIGTERM` và lỗi (một lượt).
- Các ô bị giới hạn bởi Postgres phụ thuộc vào độ trễ commit và chạy trên một host cũng phục
  vụ việc khác. Không có lần chạy nào trên máy yên tĩnh.
- Không có lần chạy trên kiến trúc CPU khác, không có lần chạy nào với mạng thật giữa sidecar
  và provider ngoài các ramp DeepSeek và vLLM thật, và không có so sánh với framework agent khác.
