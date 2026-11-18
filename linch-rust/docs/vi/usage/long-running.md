# Agent chạy dài

Một agent làm việc hàng giờ không hỏng giống như một cuộc chat. Context của nó
đầy và bị compact, server của nó restart, một session mới bắt đầu với cuộc hội
thoại trống, và một model được bảo "tiếp tục" phải tự tìm ra "tiếp tục" nghĩa là
gì. Trang này nói cần giữ gì **bên ngoài** cuộc hội thoại để không điều nào ở
trên làm mất công việc, cách steer một run khi nó đang làm việc, và cách test
toàn bộ.

Mọi thứ ở đây được dựng từ các phần mà các trang khác mô tả; phần bổ sung duy
nhất là `task_ledger` ([Task](./tasks.md#một-ledger-tồn-tại-qua-các-session)) và
`Agent.steer` ([Run](./runs.md#steer)).

## Khác gì so với một run ngắn

| Run ngắn | Run dài |
|---|---|
| Prompt là mục tiêu | Prompt trôi khỏi context, hoặc bị tóm tắt |
| Cuộc hội thoại là kế hoạch và tiến độ | Compaction viết lại cuộc hội thoại; một session mới bắt đầu trống |
| Crash nghĩa là làm lại từ đầu | Crash là chuyện thường; run được resume và không được làm lại việc đã xong |
| "Xong" là điều model nói | Model nói "xong" quá sớm, hoặc không bao giờ |
| Bạn chờ kết quả | Bạn có thể cần chỉnh run trong lúc nó làm việc |

## Bốn thứ giữ bên ngoài context

Hãy đặt từng thứ này ở nơi compaction không với tới. Mỗi thứ có một chỗ của nó
trong SDK này.

### 1. Hợp đồng mục tiêu: một system section

Hãy viết "xong" nghĩa là gì dưới dạng một system section. Một session gửi các
system block của nó trước tiên trong mọi request, từng byte giống như request đầu
tiên của nó (chúng là prefix đóng băng của session, được ghi vào log một lần cùng
prompt đầu tiên), và compaction chỉ viết lại các message đứng sau chúng, nên nó
không thể tóm tắt mất mục tiêu. Một run được resume gửi cùng các block đó, và một
session mới gửi các section của config mà nó được bắt đầu.

```python
config: AgentConfig = {
    "spec": {
        "model": "deepseek-flash",
        "system": {
            "sections": [{
                "name": "goal",
                "text": "The report is done when report.md has one section "
                        "per input file and every section cites its file.",
                "order": 0,
            }],
        },
    },
    ...
}
```

Các section là một phần của hợp đồng của run, nên một run bắt đầu dưới một mục
tiêu sẽ không được resume dưới mục tiêu khác ([Config của
agent](./agent.md#hợp-đồng-của-run)). `context_budget` chỉ giới hạn những gì hook
thêm vào một request, không bao giờ giới hạn một section.

### 2. Ledger tiến độ: danh sách task

Task là các hàng trong session store, không phải message, nên chúng sống sót qua
compaction. Mặc định chúng thuộc về một session; một session mới bắt đầu không có
task nào. Hãy đặt `task_ledger` và mọi session của agent dùng chung một danh
sách, và mỗi request mang theo các task đang mở như context bổ sung:

```python
"tasks": True,
"task_ledger": {"list": "report-ledger"},
```

Model thấy các dòng `#2 [in_progress] Summarise b.txt` và số task đã hoàn thành,
dù cuộc hội thoại của nó trống đến đâu, và giữ chúng luôn cập nhật bằng
`TaskUpdate`. Block này được đọc lại cho mỗi request, không bao giờ được ghi vào
log, và bị giới hạn bởi `max_chars`.

### 3. Artifact: file, không phải message

Những gì run tạo ra thuộc về file: báo cáo đến lúc này, ghi chú, một danh sách
nháp. Khi bật `filesystem` và dùng kho Postgres, mọi session của namespace dùng
chung một filesystem ảo, nên một session sau đọc được những gì session trước đã
ghi. Hãy nói với model, trong section mục tiêu, file nào là sản phẩm bàn giao và
rằng nó nên ghi vào file đó dần dần. Offload ([Config của agent](./agent.md))
vốn đã chuyển các kết quả tool lớn vào đó.

### 4. Event journal: chuyện đã xảy ra

Journal là bản ghi những gì đã chạy: mọi lời gọi tool, mọi kết quả, mọi steer.
Đó là thứ bạn đọc để kiểm tra công việc, và là thứ một run được resume đọc để
biết nó đã làm gì (một tool đã có bản ghi bắt đầu thì không bao giờ chạy lại). Hãy
đọc nó bằng `agent.events(run_id)`; đừng hỏi model xem nó đã làm gì.

## Một bước kiểm tra "xong" độc lập

Một model tự đánh giá công việc của mình sẽ nói "xong" quá sớm. Hãy kiểm tra từ
bên ngoài vòng lặp, dựa trên các sự kiện bền vững: ledger không còn task nào mở,
file sản phẩm tồn tại và có đúng hình dạng mà mục tiêu nêu, journal cho thấy các
lời gọi mà mục tiêu yêu cầu. Hãy chạy bước kiểm tra này trong code của bạn sau khi
run kết thúc, và nếu nó thất bại, hãy bắt đầu session kế tiếp với lỗi đó làm
prompt. Bước kiểm tra đó là gì
cũng được (một script, một job CI, một agent thứ hai với prompt riêng); mẫu hình
vẫn vậy: phán quyết đến từ thứ gì đó khác với message cuối của chính model.

## Steering

`agent.steer(run_id, "Use tabs, not spaces.")` gửi văn bản cho một run đang chạy.
Run đọc nó ở ranh giới lượt kế tiếp, như một message user riêng, và một run lẽ ra
đã kết thúc sẽ chạy thêm một lượt để hành động theo nó. Steer bền vững trước khi
lời gọi trả về, một run được resume đọc nó đúng một lần, và một run đang chờ phê
duyệt cũng nhận nó. Chi tiết, giới hạn và lỗi: [Run](./runs.md#steer).

Hãy dùng nó cho những gì một người trong vòng lặp sẽ nói: "bỏ qua file c", "hạn
chót đã dời", "dừng lại và tóm tắt". Đừng dùng nó như một kênh prompt thứ hai cho
các agent khác: `send_message` của chính một model đi vào một mailbox và được định
khung như một message từ peer, còn steer là của bạn.

## Mẫu hình harness

Hãy test một thiết lập chạy dài bằng cách phá nó, và assert trên những gì bền
vững, không phải trên những gì model nói:

1. Bắt đầu run trên các kho Postgres, với ngưỡng compaction rất thấp để
   compaction chạy mỗi lượt, và một task cần nhiều bước (dựng một báo cáo
   markdown từ ba file text).
2. SIGKILL server ở giữa chừng. Một server bị kill không bao giờ nhả lease của
   nó, nên hãy dùng lease ngắn nhất (`lease_ttl_ms: 1000`) và thử lại resume cho
   đến khi nó rảnh.
3. Resume trên một server mới, rồi bắt đầu một **session mới** cho phần việc kế
   tiếp.
4. Khi nó kết thúc, hãy đọc trạng thái bền vững: các hàng ledger (mọi task đều
   `completed`), các file (báo cáo có ba section), và journal (không tool nào
   chạy hai lần; mỗi file được đọc một lần; có các event compaction).

Các test trong `python/tests/test_long_haul.py` (provider scripted, Postgres) và
`python/tests/test_long_haul_live.py` (model thật, bị bỏ qua khi không có
`LINCH_LIVE_VLLM_URL`) là các ví dụ chạy được.

## Giới hạn, nói thẳng

- Một steer chờ bước đang làm dở. Lời gọi trả lời khi run đã ghi steer, việc xảy
  ra ở ranh giới bước kế tiếp của nó, nên một lời gọi model hoặc batch tool dài
  làm chậm câu trả lời (và việc giao) đúng bằng thời gian đó.
- Kho memory không an toàn trước crash. Ledger, các file và một steer chỉ sống sót
  qua một lần restart trên Postgres.
- Ledger là một danh sách, không phải một kế hoạch. Model phải giữ nó cập nhật;
  không gì kiểm tra các cập nhật của nó có đúng hay không. Đó là việc của bước
  kiểm tra độc lập.
- Block ledger tốn context ở mọi request (bị giới hạn bởi `max_chars`). Một danh
  sách rất dài được hiển thị bằng một con số khi vượt giới hạn.
- Compaction tóm tắt cuộc hội thoại, nên các chi tiết mà model chỉ biết qua hội
  thoại có thể mất. Mọi thứ nó phải giữ đều thuộc về ledger hoặc một file.
- Một steer đi tới server đang điều khiển run. Server khác từ chối nó bằng
  `RunLeased`; không có định tuyến.

## Các trang liên quan

- [Task](./tasks.md): các task tool và ledger.
- [Run](./runs.md): steer, resume, approve, cái gì bền vững.
- [Memory](./memory.md): context được chèn vào request.
- [Config của agent](./agent.md): `filesystem`, `spec.system` và compaction.
