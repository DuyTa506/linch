# Profile theo miền nghiệp vụ

[Bản gốc tiếng Anh](../../usage/profiles.md)

Dùng profile khi các agent dùng chung provider và kho lưu trữ cần những vai trò có phiên bản khác nhau.

[← Hướng dẫn sử dụng](./README.md)

Profile là một vai trò có phiên bản: các chỉ dẫn được thêm vào sau chỉ dẫn của chính
agent, các tool mà agent không được gọi, và giới hạn số lượt. Đặt nó làm `profile`
trong cấu hình agent. Hỗ trợ khách hàng và thanh toán trên cùng một kho lưu trữ là
hai agent được mở từ các cấu hình chỉ khác nhau ở profile.

---

## Hai vai trò trên cùng một thiết lập

```python
import asyncio

from linch_rust import AgentConfig, Profile, Server, ToolCallEndEvent, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


@tool(scope="read")
async def support_search(query: str) -> str:
    """Search the support knowledge base."""
    return "Reset access from Settings > Security."


@tool(scope="read")
async def billing_lookup(invoice: str) -> str:
    """Look up an invoice."""
    return "Invoice 1042: paid on 3 May."


base: Profile = {"id": "base", "version": "1", "max_turns": 8}
support: Profile = {
    **base,
    "id": "support",
    "version": "2",
    "system_prompt": "Help customers resolve product issues.",
    "tool_deny": ["billing_lookup"],
}
billing: Profile = {
    **base,
    "id": "billing",
    "version": "1",
    "system_prompt": "Explain invoices using the billing lookup results.",
    "tool_deny": ["support_search"],
}


def config(profile: Profile, turns: list) -> AgentConfig:
    return {
        "namespace": f"acme/{profile['id']}",
        "spec": {"model": MODEL, "system_prompt": "You answer for Acme."},
        "provider": {"id": "scripted", "turns": turns},
        "profile": profile,
    }


async def answer(server: Server, profile: Profile, turns: list, question: str) -> None:
    agent = await server.open_agent(config(profile, turns), tools=[support_search, billing_lookup])
    async for envelope in await agent.run("s1", question):
        event = envelope.event
        if isinstance(event, ToolCallEndEvent):
            text = event.result["content"][0]["text"]
            print(f"{profile['id']}: {event.tool_name} -> {text}")


async def main() -> None:
    async with Server() as server:
        await asyncio.gather(
            answer(server, support, [
                tool_turn("c1", "billing_lookup", {"invoice": "1042"}),
                tool_turn("c2", "support_search", {"query": "reset access"}),
                text_turn("Go to Settings > Security."),
            ], "How do I reset access?"),
            answer(server, billing, [
                tool_turn("c1", "billing_lookup", {"invoice": "1042"}),
                text_turn("Invoice 1042 was paid on 3 May."),
            ], "Was invoice 1042 paid?"),
        )


asyncio.run(main())
```

```text
support: billing_lookup -> Tool call denied: billing_lookup denied by tool rule in layer "profile"
support: support_search -> Reset access from Settings > Security.
billing: billing_lookup -> Invoice 1042: paid on 3 May.
```

Hai run chạy đồng thời, nên thứ tự các dòng có thể xen kẽ khác đi.

---

## Các trường

| Trường | Bắt buộc | Tác dụng |
|---|---|---|
| `id` | có | Một id vai trò hoặc tenant ổn định; không được để trống |
| `version` | có | Phiên bản của vai trò; không được để trống |
| `system_prompt` | không | Được thêm vào sau system prompt của agent, cách một dòng trống |
| `tool_deny` | không | Các tool mà lệnh gọi luôn bị từ chối |
| `max_turns` | không | Số lần gọi mô hình tối đa của một run, tối thiểu là 1; thay thế `spec.max_turns` |

Một profile không hợp lệ làm `open_agent` thất bại với `OpenError`, nêu tên trường,
ví dụ `profile id must not be blank`.

- `tool_deny` thêm một lớp quyền tên là `profile` để từ chối từng tên trong danh
  sách. Một lệnh từ chối ở bất kỳ lớp nào đều thắng, nên không quy tắc hay chế độ
  nào của ứng dụng chủ, kể cả `skip_dangerous`, cho phép một tool bị từ chối chạy.
  Mô hình vẫn được cung cấp tool đó; lệnh gọi thất bại với lý do như hiển thị ở trên.
- Với [deep agent](./deep-agent.md), system prompt gồm theo thứ tự: chỉ dẫn của
  preset, `spec.system_prompt` của agent, system prompt của profile, và của hệ
  thống tệp ảo.

---

## Xếp lớp

Một trường đến sau sẽ thay thế trường đến trước, nên một lớp là phép gộp dict với
profile cụ thể hơn đặt sau cùng: `{**base, **domain, **tenant_patch}`. Hãy cho mỗi
lớp `id` và `version` riêng; lớp cuối cùng thắng. Một danh sách `tool_deny` rỗng
thay thế một danh sách dài hơn. Ứng dụng chủ viết bằng Rust có cùng quy tắc này qua
`linch::Profile::layer`.

---

## Phiên bản và resume

Profile được áp dụng khi agent mở, không phải theo từng run: mọi run của agent đó
đều dùng nó.

Hợp đồng của run bao gồm những gì profile thay đổi trong spec, tức chỉ dẫn và
`max_turns`. Hợp đồng không bao gồm `id`, `version` hay `tool_deny`. Vì vậy:

- Văn bản profile hoặc giới hạn lượt mới sẽ mở một agent với hợp đồng mới; một run
  đã bắt đầu theo hợp đồng cũ không thể được agent mới resume (`ContractMismatch`).
  Hãy giữ profile đã phát hành và mở agent với profile đó để resume các run của nó.
- Chỉ đổi `tool_deny`, hoặc chỉ đổi `version`, thì hợp đồng không đổi; một run được
  resume sau đó sẽ theo các lệnh từ chối mới.

Hãy tự ghi lại profile mà một run đã dùng (ví dụ bên cạnh id của run); server không
lưu điều này.

---

## Điểm khác so với Python Linch

| Python Linch | linch-rust |
|---|---|
| `RunOptions(profile=...)` theo từng run trên một agent | Một profile cho mỗi agent được mở |
| `tool_allow` | Chưa có; dùng `tool_deny` |
| `plugins`, `output_schema`, `budget` | Chưa có; budget nằm trong [`spec.budget`](./agent.md#budget) |
| Hợp đồng ghim `id`, `version` và `content_hash` | Hợp đồng chỉ bao gồm thay đổi về prompt và `max_turns` |

`DeepAgentProfile` là một thứ khác: nó đặt độ bền vững, chế độ coordinator và chính
sách worker của deep agent. Xem [Deep agent](./deep-agent.md#profile).

---

## Các trang liên quan

- [Cấu hình agent](./agent.md): các trường cấu hình khác
- [Tool](./tools.md): chế độ quyền, lớp và quy tắc
- [Run](./runs.md): resume và `ContractMismatch`
