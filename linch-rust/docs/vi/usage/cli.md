# CLI

[Bản gốc tiếng Anh](../../usage/cli.md)

Dùng lệnh `linch-rust` để tạo khung dự án, thêm tool của ứng dụng chủ vào dự án, tìm tài liệu được cài cùng gói, và cài skill hướng dẫn một coding agent cách dùng gói này.

[<- Hướng dẫn sử dụng](./README.md)

Cài wheel `linch-rust` sẽ cài một console script `linch-rust`. Mã mà lệnh này ghi ra
là mã `linch_rust` thông thường do bạn sở hữu và chỉnh sửa; không có gì trong dự án
phụ thuộc vào CLI. Nếu chạy từ một bản checkout mà không có wheel, hãy chạy các lệnh
tương tự bằng `python -m linch_rust.cli`.

---

## `linch-rust new <name>`

Tạo một dự án chạy và kiểm thử được ngoại tuyến trên scripted provider:

```bash
linch-rust new demo-agent
```

```text
created demo-agent/pyproject.toml
created demo-agent/README.md
created demo-agent/.gitignore
created demo-agent/src/demo_agent/__init__.py
created demo-agent/src/demo_agent/agent.py
created demo-agent/src/demo_agent/tools/__init__.py
created demo-agent/src/demo_agent/tools/greet.py
created demo-agent/tests/test_agent.py
created demo-agent/tests/test_greet.py
created demo-agent/.claude/skills/build-agent-with-linch-rust
created demo-agent/.agents/skills/build-agent-with-linch-rust

Next steps:
  cd demo-agent
  pip install -e '.[dev]'
  python -m demo_agent.agent   # runs offline, no API key needed
  pytest
```

```text
demo-agent/
├── pyproject.toml          # hatchling, src layout, pytest configured, depends on linch-rust
├── README.md
├── .gitignore
├── .claude/skills/build-agent-with-linch-rust/   # the coding-agent skill, for Claude Code
├── .agents/skills/build-agent-with-linch-rust/   # the same, for Codex
├── src/demo_agent/
│   ├── __init__.py
│   ├── agent.py            # build_config(), ask(), and TOOLS
│   └── tools/
│       ├── __init__.py
│       └── greet.py        # an example @tool(scope="read")
└── tests/
    ├── test_agent.py       # runs the agent on scripted turns
    └── test_greet.py       # checks the tool's declaration and answer
```

`python -m demo_agent.agent` in ra phản hồi ngoại tuyến:

```text
Hello from demo-agent (offline scripted reply).
```

và `pytest` chạy các bài kiểm thử được tạo trên một sidecar thật, vẫn ở chế độ ngoại tuyến:

```text
5 passed
```

`agent.py` làm những việc sau:

- `build_config(turns=None)` trả về `AgentConfig`. Khi có `turns`, hàm dùng scripted
  provider với các lượt đó. Khi không có, hàm dùng Responses API của OpenAI nếu đã
  đặt `OPENAI_API_KEY` (model là `LINCH_MODEL`, mặc định `gpt-5`), và nếu chưa đặt
  thì dùng một phản hồi scripted ngoại tuyến.
- `ask(prompt, config=None)` khởi động một `Server`, mở agent với `TOOLS`, chạy
  prompt trong session `main` và trả về văn bản cuối cùng.
- `TOOLS` liệt kê các tool của ứng dụng chủ mà agent có.

Sửa `build_config` để dùng provider khác ([Provider](./providers.md)), kho lưu trữ
hoặc quyền ([Cấu hình agent](./agent.md)).

Tùy chọn và quy tắc:

- `--dir DIR` tạo dự án trong `DIR` thay vì thư mục hiện tại; khi đó các bước tiếp theo
  là `cd DIR/<name>`.
- Tên có thể ở dạng kebab-case (`demo-agent`); gói là dạng snake-case của tên đó
  (`demo_agent`). Tên bắt đầu bằng chữ cái thường, dùng chữ cái thường và chữ số
  với dấu phân cách `-` hoặc `_` đơn, và không phải là từ khóa Python hay một trong
  các tên `linch`, `linch_rust`, `src`, `test`, `tests`.
- Thư mục đích phải chưa tồn tại hoặc rỗng.
- Dự án nhận skill `build-agent-with-linch-rust` cho Claude Code và Codex, đúng như
  `linch-rust skill install` ghi ra ([bên dưới](#linch-rust-skill-install)).

## `linch-rust add tool <name>`

Chạy lệnh bên trong một dự án (hoặc truyền `--dir`). Lệnh ghi một khung tool của ứng
dụng chủ và bài kiểm thử của nó:

```bash
cd demo-agent
linch-rust add tool web-search
```

```text
created src/demo_agent/tools/web_search.py
created tests/test_web_search.py
```

```python
"""The web_search host tool."""

from linch_rust import tool


@tool(scope="read")
async def web_search(query: str) -> str:
    """TODO: describe web_search; the model reads this."""
    # TODO: implement web_search.
    return f"web_search results for: {query}"
```

Để đưa tool cho agent, thêm nó vào `TOOLS` trong `agent.py`. Khung tool có scope
`read`; đổi scope thành `write` hoặc `exec` nếu tool thay đổi bất cứ thứ gì
([Tool](./tools.md#scopes)).

Lệnh tìm ngược lên từ thư mục hiện tại tới `pyproject.toml` gần nhất có
`[project].dependencies` nêu `linch-rust`, rồi chọn gói trong `src/` (gói cùng tên
với dự án nếu có nhiều gói). Lệnh không bao giờ ghi đè một tệp.

## `linch-rust docs path`

In ra thư mục chứa tài liệu được cài cùng gói, gồm các trang hướng dẫn sử dụng này
và các [trang kiến trúc](../architecture/README.md). Lệnh hoạt động ngoại tuyến, từ
mọi thư mục:

```bash
linch-rust docs path
# /path/to/site-packages/linch_rust/_docs
grep -rn "NotSuspended" "$(linch-rust docs path)/usage"
```

Lệnh tìm trong gói đã cài trước, rồi đến bản checkout mà client đang chạy từ đó.

## `linch-rust skill install`

Sao chép skill `build-agent-with-linch-rust` vào một dự án, để coding agent làm việc
ở đó tìm thấy các tài liệu này và xây dựng trên `linch_rust` theo cách tài liệu mô tả.
Skill dạy về client Python: tài liệu nằm ở đâu và cách tìm trong đó, các đối tượng
chính, kiểm thử trên scripted provider, gỡ lỗi, và khi nào nên chạy nhiều sidecar.
Tệp `references/design.md` của skill giúp chọn kiến trúc trước khi xây dựng.

```bash
linch-rust skill install                    # .claude/skills and .agents/skills
linch-rust skill install --target linch     # .linch/skills
linch-rust skill install --force            # replace an installed copy after upgrading
```

```text
created .claude/skills/build-agent-with-linch-rust
created .agents/skills/build-agent-with-linch-rust
```

| `--target` | Thư mục | Dành cho |
|---|---|---|
| `claude` | `.claude/skills/build-agent-with-linch-rust` | Claude Code |
| `codex` | `.agents/skills/build-agent-with-linch-rust` | Codex |
| `linch` | `.linch/skills/build-agent-with-linch-rust` | Một agent có `skills: True` và `config_dir` mặc định ([Skill](./skills.md)) |

- Có thể lặp lại `--target`; nếu bỏ qua thì skill được cài vào `claude` và `codex`.
- `--dir DIR` cài vào dự án ở `DIR` thay vì thư mục hiện tại.
- Một bản đã cài không bao giờ bị ghi đè: lệnh dừng trước khi sao chép bất cứ thứ gì,
  với mã thoát 1. `--force` thay thế từng bản sao và xóa các tệp mà phiên bản cũ có.
- Skill là bản đi kèm với gói đã cài, nên khớp với tài liệu của gói. Lệnh hoạt động
  ngoại tuyến, lấy từ gói trước, rồi từ bản checkout mà client đang chạy từ đó.

## Mã thoát

| Mã | Ý nghĩa | Thông báo ví dụ |
|---|---|---|
| 0 | Thành công, và `--help` | |
| 2 | Lỗi sử dụng: lệnh không xác định, thiếu đối số, tên hoặc target không hợp lệ | `linch-rust: error: invalid project name 'My_Agent': use lowercase letters and digits with single '-' or '_' separators, starting with a letter` |
| 1 | Các tệp trên đĩa gây cản trở: thư mục đích không rỗng, không có dự án, gói không xác định được duy nhất, tệp hoặc skill đã tồn tại, thiếu tài liệu hoặc skill | `linch-rust: error: target demo-agent already exists and is not empty` |

Các thông báo khác với mã thoát 1:

```text
linch-rust: error: no linch-rust project found from . upward (run it inside a project made by 'linch-rust new'; its pyproject.toml must depend on 'linch-rust')
linch-rust: error: refusing to overwrite existing file .../src/demo_agent/tools/web_search.py
linch-rust: error: docs not found: reinstall linch-rust from a wheel, or use a checkout that has linch-rust/docs
linch-rust: error: .claude/skills/build-agent-with-linch-rust already exists; pass --force to replace it
linch-rust: error: skill not found: reinstall linch-rust from a wheel, or use a checkout that has linch-rust/skills
```

## Các trang liên quan

- [Bắt đầu nhanh](./quickstart.md): các cấu trúc tương tự, làm thủ công.
- [Tool](./tools.md): `@tool`, scope và phê duyệt.
- [Provider](./providers.md#the-scripted-provider): các lượt scripted cho kiểm thử.
- [Skill](./skills.md): skill cho các agent bạn xây dựng, khác với skill của coding
  agent này.
