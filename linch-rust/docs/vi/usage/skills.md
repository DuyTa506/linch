# Skill

[Bản gốc tiếng Anh](../../usage/skills.md)

Dùng skill khi mô hình cần nạp các quy trình prompt tái sử dụng theo tên trong một run.

[← Hướng dẫn sử dụng](./README.md)

Skill là một prompt markdown có tên. Mô hình nạp skill bằng tool `Skill`; tool trả về
nội dung của skill, với các đối số đã được điền vào, làm kết quả của lệnh gọi, và mô
hình hành động theo nội dung đó ở lượt kế tiếp. Có thể hình dung skill như các lệnh
bắt đầu bằng dấu gạch chéo mà agent tự gọi cho chính mình.

---

## Bật skill

Đặt `skills: True` trong cấu hình agent. Khi đó agent có tool `Skill`, skill `verify`
tích hợp sẵn, và mọi skill hợp lệ trong `<config_dir>/skills/<directory>/SKILL.md`.

- `config_dir` là `.linch` khi không có. Đường dẫn tương đối được phân giải theo
  `workspace`, hoặc nếu không có `workspace` thì theo thư mục làm việc của server,
  tức thư mục mà tiến trình Python của bạn đang chạy (`Server()` khởi động server
  ở đó). Đường dẫn tuyệt đối được dùng nguyên như vậy.
- Chỉ thư mục đó được quét: không có thư mục skill của người dùng hay toàn cục.
- Skill được nạp một lần, khi agent mở. Để nhận các tệp đã thay đổi, hãy mở một agent
  mới: việc nạp lại không có trên RPC, nên client này không thể nạp lại một agent đang
  mở. Ứng dụng chủ viết bằng Rust thì có thể, với `OpenedAgent::reload_skills` trong
  crate `linch` (quét lại thư mục) hoặc `Agent::reload_skills` trong `linch-core`
  (nhận danh sách mới).

System prompt liệt kê mọi skill mà mô hình có thể gọi, mỗi skill một dòng:
`- <name>: <description> — <when_to_use> (args: <argument_hint>)`. Một dòng bị cắt ở
250 ký tự. Danh sách có thể chiếm 1% [cửa sổ ngữ cảnh](./providers.md#context-window)
của provider, tính bốn ký tự một token (5120 ký tự cho 128000 token), như trong
Python Linch. Khi vượt mức đó, mọi mô tả bị cắt về phần chia đều nhau; khi một phần
chia nhỏ hơn 20 ký tự, chỉ liệt kê tên. Danh sách là một phần của tiền tố được cache
trong mỗi request.

---

## Skill `verify` tích hợp sẵn

```text
Skill({"skill": "verify", "args": "focus on billing workflow"})
```

`verify` yêu cầu mô hình lập kế hoạch và chạy các bước kiểm tra dựa trên bằng chứng
cho công việc đã hoàn thành, rồi kết thúc bằng `VERDICT: PASS`, `VERDICT: FAIL` hoặc
`VERDICT: PARTIAL`. Prompt của nó là prompt của Python Linch. `args` thu hẹp trọng
tâm. Một skill của dự án có tên `verify` (không phân biệt hoa thường) sẽ thay thế nó;
hãy giữ dòng verdict trong nội dung nếu ứng dụng chủ của bạn phân tích dòng đó.

---

## Viết một skill cho dự án

`.linch/skills/release-notes/SKILL.md`:

```markdown
---
description: Write release notes from the session's finished tasks.
when_to_use: Before tagging a release.
argument_hint: version, then audience
arguments: [version, audience]
allowed_tools: [TaskList]
---
# Release notes for $version

List the finished tasks with TaskList, then write notes for $audience.
Templates are in ${SKILL_DIR}/templates.
Full request: $ARGUMENTS
```

Chạy ví dụ này từ thư mục chứa `.linch` (là `/srv/app` trong đầu ra bên dưới). Scripted
provider đóng vai mô hình: nó nạp skill, gọi `TaskList` rồi trả lời.

```python
import asyncio

from linch_rust import (
    AgentConfig,
    ContextBuildEvent,
    Server,
    SkillCompletedEvent,
    SkillInvokedEvent,
    ToolCallEndEvent,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn

turns = [
    tool_turn("c1", "Skill", {"skill": "release-notes", "args": "1.4.0 'support team'"}),
    tool_turn("c2", "TaskList", {}),
    text_turn("Notes written."),
]

config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": turns},
    "skills": True,
    "tasks": True,
    "config_dir": ".linch",
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Write the release notes for 1.4.0.")
        async for envelope in run:
            event = envelope.event
            if isinstance(event, ContextBuildEvent):
                print("offered:", event.selected_tools)
            elif isinstance(event, SkillInvokedEvent):
                print("skill_invoked:", event.name, event.args)
            elif isinstance(event, SkillCompletedEvent):
                print("skill_completed:", event.name, "failed" if event.is_error else "ok")
            elif isinstance(event, ToolCallEndEvent) and event.tool_name == "Skill":
                print(event.result["content"][0]["text"])


asyncio.run(main())
```

```text
offered: ['TaskCreate', 'TaskList', 'TaskGet', 'TaskUpdate', 'Skill']
skill_invoked: release-notes 1.4.0 'support team'
Skill base directory: /srv/app/.linch/skills/release-notes

# Release notes for 1.4.0

List the finished tasks with TaskList, then write notes for support team.
Templates are in /srv/app/.linch/skills/release-notes/templates.
Full request: 1.4.0 'support team'

skill_completed: release-notes ok
offered: ['TaskList']
offered: ['TaskCreate', 'TaskList', 'TaskGet', 'TaskUpdate', 'Skill']
```

### Frontmatter

| Trường | Bắt buộc | Ý nghĩa |
|---|---|---|
| `description` | có | Một chuỗi không rỗng, hiển thị trong danh sách |
| `name` | không | Tên mà mô hình truyền vào; là tên thư mục khi không có. Phải khớp `^[a-z0-9][a-z0-9_-]*$`, không phân biệt hoa thường |
| `when_to_use` | không | Được thêm vào dòng của danh sách |
| `argument_hint` | không | Hiển thị trong danh sách dưới dạng `(args: ...)` |
| `arguments` | không | Tên cho các đối số theo vị trí, mỗi tên khớp `^[a-z_][a-z0-9_]*$`; không được là `ARGUMENTS`, không trùng lặp |
| `allowed_tools` | không | Các tool duy nhất được cung cấp ở lượt mô hình sau khi nạp (xem [Overlay](#overlay)) |
| `model` | không | Model của lượt mô hình sau khi nạp |
| `version` | không | Metadata |
| `disable_model_invocation` | không | `true` loại skill khỏi danh sách, và một lệnh gọi tới nó thất bại với `Skill is not invokable by the model` |

Frontmatter được đọc theo YAML 1.2: `yes` và `no` là chuỗi, nên hãy viết
`disable_model_invocation: true`. Khóa lặp lại làm tệp không hợp lệ.

### Placeholder

| Trong nội dung | Trở thành |
|---|---|
| `$ARGUMENTS` | Chuỗi `args`, đúng như được cho |
| `$<name>` của mỗi mục trong `arguments` | Token ở vị trí đó của `args`, được tách như một dòng shell (có tôn trọng dấu nháy và dấu gạch chéo ngược); rỗng khi thiếu |
| `${SKILL_DIR}`, `${LINCH_SKILL_DIR}` | Thư mục của skill |
| `${LINCH_SESSION_ID}` | Id của session đang gọi |

Việc thay thế chỉ diễn ra một lượt, placeholder dài nhất trước: một giá trị đã thay
thế không bao giờ được mở rộng lần nữa, và không có dấu nháy shell nào được thêm vào,
nên hãy đặt trong dấu nháy các đường dẫn có thể chứa dấu cách. Kết quả luôn bắt đầu
bằng dòng `Skill base directory: <dir>`.

Một skill có thể bảo mô hình chạy các script đi kèm qua `Bash`, ví dụ
`python "${SKILL_DIR}/scripts/check.py"`. Lệnh gọi đó đi qua các bước kiểm tra quyền
thông thường; nạp skill không cấp quyền nào. Thư mục phải tồn tại trên máy chạy
server.

### Skill không được nạp

Một skill bị loại khi thư mục của nó không có `SKILL.md`, frontmatter không hợp lệ,
thư mục là symlink, hoặc tên của nó đã bị một tool của agent chiếm (một tool của ứng
dụng chủ, một tool workspace, `TaskList` khi `tasks` bật) hoặc bị một skill được nạp
trước đó chiếm. `Agent.skipped` liệt kê từng mục bị loại, kèm đường dẫn và lý do
([Cấu hình agent](./agent.md#skipped-definitions)); một lệnh gọi tới skill không có
sẽ thất bại với `Unknown skill: <name>`.

---

## Overlay

Một skill có `allowed_tools` hoặc `model` chỉ thay đổi lượt mô hình kế tiếp, như đầu
ra ở trên cho thấy: lượt sau khi nạp chỉ cung cấp `TaskList`, và lượt sau đó cung cấp
lại mọi tool.

- `allowed_tools` chỉ thu hẹp những gì được cung cấp. Nó không bao giờ cho phép một
  lệnh gọi mà chính sách quyền sẽ hỏi. Một lệnh gọi tới tool ngoài tập này thất bại
  với `Tool "<name>" is not registered`.
- `model` chuyển model của lượt đó. Khi model của run kết thúc bằng `[1m]` mà model
  của skill không có, hậu tố được giữ lại.
- Không có gì được lưu cho một overlay: một lượt được resume nhận overlay mà nó đã
  được gửi kèm.

---

## Event

Run đầu tiên của một session liệt kê các skill trong một `SkillsLoadedEvent` ngay sau
`init`: gồm `name`, `description`, `when_to_use` và `argument_hint` của từng skill,
sắp xếp theo tên. Mỗi lệnh gọi `Skill` thêm một `SkillInvokedEvent` (`name`, và `args`
khi có) ngay trước `tool_call_start` của nó và một `SkillCompletedEvent` (`name`, và
`is_error` là true khi lệnh gọi thất bại) ngay sau `tool_call_end` của nó, như trong
Python. Cả hai đều bền vững, nên `Agent.events(run_id)` phát lại chúng. Xem
[Event](./events.md).

---

## Quyền

Tool `Skill` có scope read: nó chỉ trả về văn bản, nên chạy mà không cần phê duyệt ở
mọi chế độ và có thể chạy cùng các lệnh gọi read khác. Một quy tắc `tool` có điều
kiện `skill` sẽ hỏi hoặc từ chối cho một skill:

```python
permissions = {
    "mode": "default",
    "project_root": "/",
    "layers": [
        {"name": "host", "rules": [
            {"type": "tool", "tool": "Skill", "skill": "release-notes", "decision": "ask"},
        ]},
    ],
}
```

Với chính sách này, một lệnh gọi `Skill` cho `release-notes` tạm dừng run để chờ phê
duyệt như mọi lệnh hỏi khác (xem [Run](./runs.md)); các skill khác được nạp mà không
hỏi. `"skill": "*"` khớp mọi skill.

---

## Độ bền vững

- Mọi skill, kể cả nội dung và đường dẫn thư mục của nó, là một phần của hợp đồng run
  của agent. Sửa một `SKILL.md`, hoặc mở agent với các skill ở đường dẫn khác, sẽ tạo
  ra hợp đồng mới, và một run đã bắt đầu theo hợp đồng cũ không thể được resume
  (`ContractMismatch`). Hãy giữ cùng các tệp ở cùng đường dẫn trên mọi server có thể
  resume một run Postgres.
- Khi compaction tóm tắt bỏ đi kết quả của một skill, nội dung của skill được đưa
  lại vào bản tóm tắt, để mô hình tiếp tục làm theo nó.

---

## Điểm khác so với Python Linch

| Python Linch | linch-rust |
|---|---|
| `FeatureFlags(skills=True)` | `skills: True` trong cấu hình |
| `agent.reload_skills()` | Không có trong client này hay trên RPC; hãy mở một agent mới. Ứng dụng chủ viết bằng Rust có `OpenedAgent::reload_skills` và `Agent::reload_skills` |
| Danh sách là một lời nhắc trong prompt đầu tiên | Một khối được cache của system prompt |
| Tool `Skill` có scope exec với các quy tắc auto-allow | Scope read |
| `skills_loaded` một lần cho mỗi đối tượng session, nên xuất hiện lại sau khi khởi động lại | Chỉ ở run đầu tiên của session, và chỉ khi run cung cấp tool `Skill` |
| PyYAML: `yes` là `true`, khóa lặp lại giữ giá trị cuối | YAML 1.2: `yes` là chuỗi, khóa lặp lại là không hợp lệ |

---

## Các trang liên quan

- [Subagent](./subagents.md): agent con thấy danh sách khi nó có tool `Skill`
- [Tool](./tools.md): phần còn lại của bề mặt tool và các quy tắc quyền
- [Cấu hình agent](./agent.md): `skills`, `config_dir` và các công tắc khác
