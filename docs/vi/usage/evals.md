# Evals và benchmark

[Bản gốc tiếng Anh](../../usage/evals.md)

Dùng các eval suite để so sánh hành vi của agent giữa các provider, prompt và thiết lập runtime.

Evals của Linch chạy các `Agent` thông thường thông qua một harness chấm điểm nhỏ. Dùng chúng để so sánh provider, prompt, thiết lập recovery, cấu hình memory hoặc các thay đổi runtime mà không cần xây dựng test runner riêng; dùng `ScriptedProvider` cho các kiểm thử offline có tính xác định.

<a id="library-api"></a>

## `run_eval_benchmark` và `EvalSuite`

```python
import asyncio

from linch import (
    Agent,
    FeatureFlags,
    InMemorySessionStore,
    empty_tools,
)
from linch.ext.evals import (
    EvalBenchmarkTarget,
    EvalCase,
    EvalSuite,
    ScriptedProvider,
    TextTurn,
    run_eval_benchmark,
    text_contains,
)


def agent_for(*answers: str) -> Agent:
    return Agent(
        model="scripted-eval",
        provider=ScriptedProvider([TextTurn(text=answer) for answer in answers]),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
    )


async def main() -> None:
    suite = EvalSuite(
        name="capitals",
        cases=[
            EvalCase(prompt="Capital of France?", expected="Paris"),
            EvalCase(prompt="Capital of Germany?", expected="Berlin"),
        ],
        scorers=[text_contains("{expected}")],
    )

    targets = [
        EvalBenchmarkTarget("candidate-a", agent_for("Paris", "Berlin")),
        EvalBenchmarkTarget("candidate-b", agent_for("Paris", "Paris")),
    ]
    try:
        result = await run_eval_benchmark(suite, targets)
        print(result.to_markdown())
    finally:
        for target in targets:
            await target.agent.close()


asyncio.run(main())
```

`run_eval_benchmark()` tạo một `EvalResult` cho mỗi target và ghi lại thời gian thực đã trôi. Mỗi eval case vẫn nhận một Linch session mới thông qua `run_eval()`. Các case chạy tuần tự với từng target, và `run_eval()` giải phóng session của từng case trước khi bắt đầu case tiếp theo. Một case đạt khi run không ghi nhận lỗi và mọi scorer trả về giá trị khác `None` đều trả về `True`; kết quả scorer là `None` được xem là chưa biết. Scorer `run_completed` chỉ trả về `True` cho một `ResultEvent` thành công và trả về `None` khi không có result event.

## Các turn theo script, lệnh gọi tool và subagent

`ScriptedProvider` tiêu thụ đúng một turn từ danh sách của nó cho mỗi lần gọi `provider.stream()`. `TextTurn` phát ra một phản hồi văn bản. `ToolUseTurn` phát ra một phản hồi sử dụng tool với `tool_name`, `tool_input` và `tool_id` tùy chọn; bản thân nó không cung cấp văn bản tiếp theo sau một lệnh gọi tool thông thường. Loop thực thi tool đó rồi thường yêu cầu provider cung cấp turn khác, vì vậy hãy thêm một turn theo script nữa. Các đường đi terminal/final-tool có thể dừng mà không cần lệnh gọi tiếp theo. Nếu một lệnh gọi provider cần một turn sau khi danh sách đã cạn, `ScriptedProvider.stream()` sẽ phát sinh `RuntimeError` với thông báo cho biết đã tiêu thụ bao nhiêu turn và đã cấu hình bao nhiêu turn.

Con trỏ của provider thuộc về đối tượng provider, không thuộc về session. Một child run foreground dùng cùng đối tượng provider với parent vì child session do cùng một `Agent` sở hữu. Do đó parent và child cùng tiêu thụ một chuỗi dùng chung. Trong chuỗi này:

1. Parent tiêu thụ `ToolUseTurn` và gọi `Subagent`.
2. Child tiêu thụ `TextTurn` tiếp theo và trả văn bản của nó làm kết quả tool.
3. Parent tiếp tục chạy và tiêu thụ `TextTurn` cuối cùng.

Ví dụ offline nhỏ này minh họa thứ tự đó; tính năng tùy chọn duy nhất được bật là subagents:

```python
import asyncio

from linch import Agent, FeatureFlags, InMemorySessionStore, empty_tools
from linch.ext.evals import ScriptedProvider, TextTurn, ToolUseTurn


async def main() -> None:
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Subagent",
                tool_input={"description": "helper", "prompt": "Return the child marker."},
            ),
            TextTurn(text="child marker"),
            TextTurn(text="parent marker"),
        ]
    )
    agent = Agent(
        model="scripted-eval",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=True, mcp=False, filesystem=False),
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("Delegate this task.")]
        result = next(event for event in events if event.type == "result")
        assert result.final_text == "parent marker"
        print(result.final_text)
    finally:
        await agent.close()


asyncio.run(main())
```

Ví dụ này in ra `parent marker` mà không cần thông tin xác thực hay mạng. Nếu child chạy ở nền hoặc nhiều run dùng chung một provider đồng thời, thứ tự xen kẽ phụ thuộc vào cơ chế lập lịch; hãy cấp cho mỗi run một provider riêng khi chuỗi turn cần có khả năng tái lập độc lập.

## CLI

Tạo file suite:

```json
{
  "name": "capitals",
  "cases": [
    {"prompt": "Capital of France?", "expected": "Paris"},
    {"prompt": "Capital of Germany?", "expected": "Berlin"}
  ],
  "scorers": [
    {"type": "text_contains", "substring": "{expected}"},
    {"type": "run_completed"}
  ]
}
```

Tạo một target theo script:

```json
{
  "turns": [
    {"type": "text", "text": "Paris"},
    {"type": "text", "text": "Berlin"}
  ]
}
```

Chạy benchmark:

```bash
python scripts/eval_benchmark.py capitals.json \
  --scripted baseline=baseline-turns.json \
  --format markdown
```

Lặp lại `--scripted NAME=PATH` để so sánh nhiều target xác định:

```bash
python scripts/eval_benchmark.py capitals.json \
  --scripted baseline=baseline-turns.json \
  --scripted candidate=candidate-turns.json \
  --format json \
  --output eval-report.json
```

Script thoát với mã `1` khi bất kỳ target nào thấp hơn `--fail-under` (mặc định là `1.0`). Dùng `--fail-under 0` cho benchmark thăm dò, trong đó candidate không đạt vẫn cần tạo tệp báo cáo.

## Schema của suite

Tệp suite có thể ở dạng JSON hoặc YAML:

```yaml
name: memory-smoke
cases:
  - prompt: What policy mentions PTO rollover?
    expected: policy-1
    metadata:
      area: memory
scorers:
  - type: text_contains
    substring: "{expected}"
  - type: cost_under
    budget_usd: 0.05
```

Các loại scorer được hỗ trợ ánh xạ tới những scorer tích hợp sẵn được export từ `linch.ext.evals`:

| Loại | Trường |
|---|---|
| `text_contains` | `substring` (mặc định là `{expected}` khi bị bỏ qua) |
| `tool_called` | `tool` |
| `schema_valid` | `schema` |
| `cost_under` | `budget_usd` |
| `context_selected_tool` | `tool` |
| `context_not_trimmed` | không có |
| `context_metadata_contains` | `key`, `expected` tùy chọn |
| `recovery_succeeded` | `tool` tùy chọn |
| `run_completed` | không có |

`schema_valid` dùng dependency cốt lõi `jsonschema`. Scorer trả về `False` cho output không hợp lệ và cũng fail closed nếu validator không khả dụng trong một bản cài đặt lỗi, để output chưa được kiểm tra không thể âm thầm làm eval đạt.

Tệp chứa các turn theo script có thể ở dạng JSON hoặc YAML, và có thể là một list hoặc một object có list `turns`. Một text turn có dạng:

```json
{"type": "text", "text": "done"}
```

Một tool-use turn có dạng:

```json
{
  "type": "tool_use",
  "tool_name": "read_file",
  "tool_input": {"path": "/memories/MEMORY.md"}
}
```

CLI cố ý nối các agent theo script với `empty_tools()` để an toàn cho các smoke test trong CI. Dùng API thư viện khi một benchmark target cần registry tool thật, dependency file-memory, filesystem, hook hoặc provider live.
