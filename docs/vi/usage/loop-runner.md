# Bộ chạy vòng lặp ngoài

[Bản gốc tiếng Anh](../../usage/loop-runner.md)

Dùng `LoopRunner` khi ứng dụng chủ của bạn cần kích hoạt công việc agent định kỳ.

[← Hướng dẫn sử dụng](./README.md)

`LoopRunner` là thành phần cơ sở vòng lặp ngoài tích hợp trong SDK dành cho công
việc agent định kỳ. Nó không phải daemon và không quản lý webhook, cron hay vòng
đời tiến trình. Ứng dụng chủ gọi `run_once()` mỗi khi một cơ chế kích hoạt bên
ngoài yêu cầu chạy thêm một nhịp. Các lớp bọc nhỏ do ứng dụng chủ quản lý cho cơ
chế kích hoạt qua cron, webhook, khoảng thời gian cố định và cổng kiểm tra CI
nằm trong [`examples/recipes/runner_recipes.py`](../../../examples/recipes/runner_recipes.py).

Với `FileLoopArtifactStore` mặc định, mỗi nhịp tạo một session mới, chạy agent
một lần, ghi một `RunReport` và lưu bền trạng thái vòng lặp qua các tệp trong
`domains/<loop_id>/`. Một `LoopArtifactStore` tùy chỉnh có thể thay thế cách bố
trí trên hệ thống tệp này.

```
domains/<loop_id>/
  README.md
  LOG.md
  artifacts/
    .next_iteration
    runs/<run_id>.md
    runs/<run_id>.json
```

Việc tạo session mới là có chủ đích: công việc định kỳ không nên phụ thuộc vào
lịch sử hội thoại ẩn. Hãy đặt trạng thái bền vững trong các sản phẩm đầu ra, bộ
nhớ, tool hoặc kho lưu trữ riêng của bạn.

---

## Chạy một nhịp thủ công

```python
from linch import Agent
from linch.ext.loop_runner import LoopRunner, LoopSpec, LoopTrigger

agent = Agent(model="gpt-5", permissions={"mode": "skip-dangerous"})

spec = LoopSpec(
    id="docs-maintenance",
    charter="Keep the documentation accurate and runnable.",
    prompt="Review the domain artifacts and make one useful improvement.",
)

runner = LoopRunner(agent)
result = await runner.run_once(
    spec,
    LoopTrigger(source="manual", payload="Developer requested a maintenance pass."),
)

print(result.run_id, result.status, result.done)
print(result.artifact_paths)
```

Đóng agent khi ứng dụng chủ tắt, sau khi không còn cần các bộ chạy và session
của nó nữa.

Có một phiên bản có thể chạy với dịch vụ thật tại
[`examples/recipes/loop_runner.py`](../../../examples/recipes/loop_runner.py).
Phiên bản này nạp `.env` của dự án nếu có:

```bash
cat > .env <<'EOF'
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
model=deepseek-chat
EOF

python examples/recipes/loop_runner.py
```

Ví dụ cũng chấp nhận các tên tường minh: `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`,
`LOOP_RUNNER_BASE_URL` và `LOOP_RUNNER_MODEL`. Với OpenAI, hãy bỏ `BASE_URL` và
đặt `LOOP_RUNNER_MODEL` thành một model chat OpenAI như `gpt-4o-mini`.

`LoopSpec.root` mặc định là `"domains"`. Truyền một thư mục gốc khác khi ứng dụng
chủ muốn đặt các sản phẩm đầu ra của vòng lặp ở vị trí khác:

```python
spec = LoopSpec(
    id="nightly-evals",
    charter="Run nightly eval triage.",
    prompt="Inspect failures and summarize the next action.",
    root="/var/lib/my-app/linch-domains",
)
```

---

## Hoàn thành và xác minh

Bộ chạy không suy ra việc hoàn thành miền công việc từ các tệp đánh dấu. Hãy
sử dụng các hàm điều kiện do bên gọi cung cấp để xét `LoopTickResult` và
`RunReport` của nó.

```python
from linch.ext.loop_runner import LoopTickResult


def verify(result: LoopTickResult) -> bool:
    return result.report.status == "completed" and not result.report.errors


def done(result: LoopTickResult, artifacts) -> bool:
    return "ready for release" in (result.final_text or "").lower()


runner = LoopRunner(agent, verify=verify, done_predicate=done)
result = await runner.run_once(spec)
```

Nếu `verify` phát sinh ngoại lệ hoặc trả về `False`, kết quả nhịp được đánh dấu
`verification_failed`, `done` vẫn là `False`, và lỗi được thêm vào `LOG.md` bởi
kho lưu trữ sản phẩm đầu ra dựa trên tệp mặc định.

---

## Cron, CI và webhook

`LoopRunner` là bộ khung chạy một lần, nên mọi tích hợp với ứng dụng chủ đều có
cùng một cấu trúc:

```python
async def tick_from_cron() -> None:
    await runner.run_once(spec, LoopTrigger(source="cron", payload="0 * * * *"))


async def tick_from_ci(commit_sha: str) -> None:
    await runner.run_once(
        spec,
        LoopTrigger(source="ci", payload=commit_sha, metadata={"commit": commit_sha}),
    )


async def tick_from_webhook(body: str, delivery_id: str) -> None:
    await runner.run_once(
        spec,
        LoopTrigger(source="webhook", id=delivery_id, payload=body),
    )
```

Mỗi đối tượng `LoopRunner` có một khóa riêng cho từng vòng lặp trong cùng tiến
trình. Nếu hai lời gọi cho cùng `LoopSpec.id` chồng lấn trên bộ chạy đó, lời gọi
thứ hai phát sinh `ConfigError`. Khóa này không có hiệu lực trên toàn tiến trình
và không được chia sẻ giữa các đối tượng `LoopRunner` riêng biệt.

Với cron, CI worker, trình xử lý webhook hoặc nhiều tiến trình ứng dụng, hãy thêm
một kho lưu trữ quyền giữ khóa có thời hạn bền vững:

```python
from linch.ext.loop_runner import FileLoopLeaseStore

runner = LoopRunner(
    agent,
    leases=FileLoopLeaseStore(root="domains"),
    lease_owner="worker-1",
    lease_ttl_s=300,
)
```

`FileLoopLeaseStore` ghi nguyên tử `domains/<loop_id>/.lock.json` dưới khóa
`fcntl` và từ chối tiếp tục trên các nền tảng không có `fcntl`. `LoopRunner`
gia hạn quyền giữ khóa sau mỗi TTL/3 trong khi nhịp đang chạy.
Tiến trình khác không thể giành quyền chạy cùng vòng lặp cho đến khi bên giữ
nhả quyền hoặc TTL hết hạn. Dùng cùng thư mục gốc với `LoopSpec.root` khi bạn
thay đổi giá trị này.

---

## Tích hợp lập lịch

Các lịch bền vững được `linch.worker` điều phối trên PgQueuer. Để tìm hiểu cấu
trúc hàng đợi và các tool lập lịch, xem [Mở rộng quy mô](./production.md#mở-rộng-theo-chiều-ngang).

---

## Kiểm thử với dịch vụ thật

Bài kiểm thử tích hợp dùng cùng trình nạp `.env` của dự án nhưng yêu cầu một cờ
bật tường minh để các lần kiểm tra thông thường ở môi trường cục bộ và CI không
tiêu tốn hạn mức API:

```bash
LINCH_RUN_LIVE_LOOP_TESTS=1 pytest tests/integration/test_live_loop_runner.py -v
```

Bài kiểm thử chấp nhận các khóa `.env` chung theo kiểu DeepSeek (`API_KEY`,
`BASE_URL`, `model`) cũng như `DEEPSEEK_API_KEY` hoặc `OPENAI_API_KEY` tường minh,
sau đó xác minh một nhịp thực sự dùng provider, `done_predicate` và các tệp
`README.md`, `LOG.md`, báo cáo Markdown và báo cáo JSON được tạo trong miền
công việc của vòng lặp.
