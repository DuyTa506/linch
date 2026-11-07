# Domain profiles

Use a profile when agents that share a provider and stores need different
versioned roles.

[← Usage guide](./README.md)

A profile is a versioned role: instructions added after the agent's own, the
tools it may not call, and a turn limit. Set it as `profile` in the agent
config. Support and billing on one store are two agents opened from configs
that differ only in their profile.

---

## Two roles on one setup

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

The two runs are concurrent, so their lines may interleave differently.

---

## Fields

| Field | Required | Effect |
|---|---|---|
| `id` | yes | A stable role or tenant id; not blank |
| `version` | yes | The role's revision; not blank |
| `system_prompt` | no | Added after the agent's system prompt, after a blank line |
| `tool_deny` | no | Tools whose calls are always denied |
| `max_turns` | no | Most model calls one run may make, at least 1; replaces `spec.max_turns` |

An invalid profile fails `open_agent` with `OpenError`, naming the field, for
example `profile id must not be blank`.

- `tool_deny` adds a permission layer named `profile` that denies each name.
  A deny in any layer wins, so no host rule or mode, `skip_dangerous`
  included, lets a denied tool run. The model is still offered the tool; the
  call fails with the reason shown above.
- With a [deep agent](./deep-agent.md), the system prompt is, in order: the
  preset's instructions, the agent's `spec.system_prompt`, the profile's, and
  the virtual filesystem's.

---

## Layering

A later field replaces an earlier one, so a layer is a dict merge with the
more specific profile last: `{**base, **domain, **tenant_patch}`. Give each
layer its own `id` and `version`; the last one wins. An empty `tool_deny`
list replaces a longer one. Rust hosts have the same rule as
`linch::Profile::layer`.

---

## Versions and resume

A profile is applied when the agent opens, not per run: every run of that
agent takes it.

The run contract covers what the profile changes in the spec, its
instructions and `max_turns`. It does not cover the `id`, the `version` or
`tool_deny`. So:

- A new profile text or turn limit opens an agent with a new contract; a run
  started under the old one cannot be resumed by the new agent
  (`ContractMismatch`). Keep the released profile and open an agent with it to
  resume its runs.
- Changing only `tool_deny`, or only the `version`, keeps the contract; a
  resumed run then follows the new denies.

Record which profile a run used yourself (for example next to its run id);
the server does not store it.

---

## What differs from Python Linch

| Python Linch | linch-rust |
|---|---|
| `RunOptions(profile=...)` per run on one agent | One profile per opened agent |
| `tool_allow` | Not yet; use `tool_deny` |
| `plugins`, `output_schema`, `budget` | Not yet; a budget goes in [`spec.budget`](./agent.md#budget) |
| The contract pins `id`, `version` and `content_hash` | The contract covers only the prompt and `max_turns` changes |

`DeepAgentProfile` is a different thing: it sets the deep agent's
durability, coordinator mode and worker policy. See
[Deep agent](./deep-agent.md#profiles).

---

## Related pages

- [Agent configuration](./agent.md): the other config fields
- [Tools](./tools.md): permission modes, layers and rules
- [Runs](./runs.md): resume and `ContractMismatch`
