# CLI tạo khung dự án

[Bản gốc tiếng Anh](../../usage/cli.md)

Dùng CLI `linch` để tạo khung dự án, thêm tool và tìm tài liệu đã cài đặt.

[<- Hướng dẫn sử dụng](./README.md)

Khi cài đặt linch, một console script `linch` được đăng ký để tạo khung các dự án
agent. Đây là một công cụ *sinh mã độc lập*: mã được tạo là mã sử dụng Linch thông
thường mà bạn sở hữu và chỉnh sửa — không phụ thuộc vào CLI khi chạy, không bị
ràng buộc vào công cụ và không cần thêm thư viện phụ thuộc (CLI chỉ dùng thư viện
chuẩn nên có mặt trong mọi bản cài đặt).

---

## `linch new <name>`

Tạo một dự án agent có thể chạy và kiểm thử:

```bash
linch new my-agent
cd my-agent
pip install -e '.[dev]'
python -m my_agent.agent   # runs offline — no API key needed
pytest                     # generated tests also run offline
```

```text
my-agent/
├── pyproject.toml            # hatchling, src layout, pytest configured
├── README.md
├── .gitignore
├── src/my_agent/
│   ├── __init__.py
│   ├── agent.py              # build_agent() factory + runnable entry point
│   └── tools/
│       ├── __init__.py
│       └── greet.py          # example @tool
└── tests/
    ├── test_agent.py         # drives the agent with ScriptedProvider
    └── test_greet.py         # assert_tool_contract on the example tool
```

Dự án **ưu tiên hoạt động ngoại tuyến**: `build_agent()` chuyển sang dùng
`ScriptedProvider` (từ `linch.ext.evals`) khi chưa đặt `OPENAI_API_KEY`, nên vòng
lặp agent và các bài kiểm thử được tạo có thể hoạt động ngay cả khi chưa có thông
tin xác thực. Đặt `OPENAI_API_KEY` sẽ chuyển sang provider OpenAI mặc định của
Linch; `LINCH_MODEL` ghi đè model (mặc định là `gpt-5`). Xem
[Provider](./providers.md) để cấu hình tường minh một provider khác.

Tên dự án có thể dùng kebab-case (`my-agent`); gói Python dùng dạng snake-case
(`my_agent`). Tên phải bắt đầu bằng chữ cái thường, chỉ dùng chữ cái thường/chữ
số với dấu phân cách `-`/`_` đơn, và không được là từ khóa Python hay tên dành
riêng (`linch`, `src`, `test`, `tests`).

## `linch add tool <name>`

Chạy từ bên trong một dự án đã tạo khung (hoặc truyền `--dir`); lệnh thêm khung
hàm tool cùng một bài kiểm thử hợp đồng:

```bash
linch add tool web-search
# created src/my_agent/tools/web_search.py
# created tests/test_web_search.py
```

Khung hàm là một hàm bất đồng bộ được gắn decorator `@tool`, sẵn sàng để triển
khai; bài kiểm thử gọi `assert_tool_contract` (từ `linch`) để siêu dữ liệu, quy
trình xác thực và cấu trúc thực thi của tool được xác lập ngay từ commit đầu
tiên. Đăng ký tool trong `build_agent()` bằng cách thêm nó vào `empty_tools(...)`
— xem [Tool](../../usage/tools.md).

Lệnh tìm ngược lên các thư mục cha tới `pyproject.toml` gần nhất có khai báo
Linch trong `[project].dependencies`, rồi tìm một gói Python trong `src/`. Nếu
có nhiều gói, lệnh chọn gói khớp với `[project].name`, hoặc báo không thể xác
định duy nhất. Lệnh từ chối ghi đè bất kỳ tệp nào trong hai tệp được tạo.

## `linch docs path`

In ra đường dẫn tuyệt đối tới thư mục chứa cây thư mục `docs/` và `examples/` đã cài đặt:

```bash
linch docs path
# /path/to/site-packages/linch/_docs
grep -rn 'RunBudget' "$(linch docs path)/docs/usage"
```

Lệnh kiểm tra tài liệu đóng gói trước, sau đó kiểm tra bản mã nguồn được checkout
đối với bản cài đặt editable. Lệnh hoạt động từ mọi thư mục và không cần kết nối
mạng. Nếu thiếu tài liệu, hãy cài lại Linch từ một wheel có đóng gói tài liệu,
hoặc khôi phục các thư mục `docs/` và `examples/` của bản checkout.

## `linch skill install`

Sao chép skill `build-agent-with-linch` vào dự án để một coding agent có thể tìm
thấy tài liệu này và xây dựng dựa trên Linch:

```bash
linch skill install                    # .claude/skills and .agents/skills
linch skill install --target linch     # .linch/skills, for a Linch-built coding agent
linch skill install --force            # replace an installed copy after upgrading
```

`--target` chấp nhận `claude`, `codex` và `linch`, và có thể được dùng nhiều lần.
`--dir` chọn dự án. Skill khớp với phiên bản Linch đã cài đặt. `linch new` cài
skill cho Claude Code và Codex.

---

## Mã thoát

| Mã | Ý nghĩa |
|---|---|
| 0 | Thành công (cả `--help`) |
| 2 | Lỗi sử dụng: lệnh không xác định, thiếu đối số, tên không hợp lệ |
| 1 | Lỗi trạng thái: thư mục đích không rỗng, không có/không xác định được duy nhất dự án, tệp đã tồn tại, thiếu tài liệu |

## Các trang liên quan

- [Bắt đầu nhanh](../../usage/quickstart.md) — các cấu trúc tương tự, được xây dựng thủ công từng bước.
- [Tool](../../usage/tools.md) — mọi thông tin về `@tool`, phạm vi và quyền.
- [Đánh giá](./evals.md) — `ScriptedProvider` và các mẫu kiểm thử ngoại tuyến.
