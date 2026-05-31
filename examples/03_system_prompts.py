"""System prompt control — every pattern.

Run:
    OPENAI_API_KEY=sk-... python examples/03_system_prompts.py

Demonstrates:
  1. Simple append (legacy system_prompt kwarg)
  2. SystemPromptConfig.append — same result, typed
  3. replace_defaults=True — your prompt IS the whole system
  4. Custom SystemBlocks prepended before the built-in identity
  5. Per-session override (system_blocks_override)
  6. Tool-aware protocol — only describe tools that actually exist
  7. Persona pattern — deterministic character with no SWE bleed
  8. Multi-tenant — same Agent, different system per session

AgentKit's default system blocks (in order):
  [0] identity  — "You are AgentKit, an autonomous SWE assistant …"
  [1] protocol  — "Tool use protocol: Read before Edit …" (only if SWE tools present)
  [2] env       — "Environment: cwd, OS, tools available …"
  [3] append    — your system_prompt / SystemPromptConfig.append (if set)

Setting replace_defaults=True removes [0] and [1]; only [2] + [3] remain.
"""

from __future__ import annotations

import asyncio
import os

from agent_kit import Agent
from agent_kit.config import FeatureFlags, SystemPromptConfig
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.registry import empty_tools
from agent_kit.types import SystemBlock

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"

BASE = dict(
    model=MODEL,
    openai_api_key=API_KEY,
    session_store=InMemorySessionStore(),
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    tools=empty_tools(),
)


# ── 1. Simple append ──────────────────────────────────────────────────────────
#
# The legacy way. Your text is appended under "User-provided instructions:".
# The SWE identity + protocol + env blocks remain.

def agent_append_legacy() -> Agent:
    return Agent(
        **BASE,
        system_prompt="Always reply concisely. Prefer bullet points over prose.",
    )


# ── 2. SystemPromptConfig.append ─────────────────────────────────────────────
#
# Typed equivalent of pattern 1. Use this in new code — IDE autocomplete works.

def agent_append_typed() -> Agent:
    return Agent(
        **BASE,
        system_prompt_config=SystemPromptConfig(
            append="Always reply concisely. Prefer bullet points over prose.",
        ),
    )


# ── 3. replace_defaults=True — full control ───────────────────────────────────
#
# Drops the "AgentKit SWE assistant" identity and tool protocol blocks.
# Your append text IS the agent's personality. The env block (cwd, tools) stays.
# Use this for any non-SWE domain: customer support, data analysis, coding tutor…

def agent_full_replace(domain_prompt: str) -> Agent:
    return Agent(
        **BASE,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=domain_prompt,
        ),
    )


# ── 4. Custom SystemBlocks prepended ─────────────────────────────────────────
#
# Inject structured content BEFORE the SWE identity (e.g. a DB schema, a
# document, a list of constraints).  Useful when your context has structure
# that benefits from being early in the prompt (better caching hit rate too).

def agent_with_prepended_context(schema_sql: str) -> Agent:
    schema_block = SystemBlock(
        text=f"Database schema (authoritative — do not deviate):\n\n```sql\n{schema_sql}\n```",
        cacheable=True,  # this block will be eligible for prompt caching
    )
    return Agent(
        **BASE,
        system_prompt_config=SystemPromptConfig(
            blocks=[schema_block],  # prepended before identity
            replace_defaults=False,
            append="You are a SQL assistant. Always use the schema above.",
        ),
    )


# ── 5. Per-session override ───────────────────────────────────────────────────
#
# session.system_blocks_override completely replaces what the agent sends on
# that session's requests. This is how subagents work internally.
# Use for: multi-tenant apps where each user has a different persona, locale,
# or access level.

async def multi_tenant_example(api_key: str) -> None:
    print("\n── 5. Per-session override ──")
    # One agent, many tenants
    agent = Agent(
        **BASE,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Default assistant.",  # fallback if no override
        ),
    )

    tenants = [
        ("alice", "You are ALICE. You answer only in formal English."),
        ("bob", "You are BOB. You answer only in informal English with emojis."),
    ]

    for name, persona in tenants:
        session = await agent.session(meta={"tenant": name})
        # Override the entire system for this session
        session.system_blocks_override = [
            SystemBlock(text=persona, cacheable=True),
        ]
        result = None
        async for event in session.run("What is the capital of France?"):
            if event.type == "result":
                result = event
        print(f"  {name}: {result.final_text[:100]}")


# ── 6. Tool-aware protocol ────────────────────────────────────────────────────
#
# When you use a custom toolset (no Bash, no Edit), the protocol block
# automatically omits clauses about those missing tools.
# This example shows the protocol is scoped to what's actually registered.

def show_tool_aware_protocol() -> None:
    from agent_kit.tools.registry import tools_from_defaults as _tfd
    # Use a base config without 'tools' so we can supply it per agent
    _base = {k: v for k, v in BASE.items() if k != "tools"}
    print("\n── 6. Tool-aware protocol blocks ──")

    # Full SWE toolset → full protocol
    swe_agent = Agent(**_base, tools=_tfd())
    swe_text = "\n".join(b.text for b in swe_agent.system_blocks)
    print(f"  SWE agent  — has 'Bash' clause: {'Bash runs' in swe_text}")
    print(f"  SWE agent  — has 'Edit' clause: {'Read a file before you Edit' in swe_text}")

    # Read-only toolset → only read clauses
    read_agent = Agent(**_base, tools=_tfd(exclude={"Bash", "Write", "Edit"}))
    read_text = "\n".join(b.text for b in read_agent.system_blocks)
    print(f"  Read agent — has 'Bash' clause: {'Bash runs' in read_text}")
    print(f"  Read agent — has 'Glob' clause: {'Glob is for' in read_text}")

    # No tools → no protocol block
    no_tools_agent = Agent(**_base, tools=empty_tools())
    no_tools_text = "\n".join(b.text for b in no_tools_agent.system_blocks)
    print(f"  No tools   — has 'Tool use protocol': {'Tool use protocol' in no_tools_text}")


# ── 7. Persona pattern ────────────────────────────────────────────────────────
#
# Build a deterministic character that never leaks the SWE identity.
# Combine replace_defaults + append + a temperature setting.

def agent_persona(name: str, role: str, style: str) -> Agent:
    persona = (
        f"You are {name}, {role}.\n\n"
        f"Style: {style}\n\n"
        f"Important: Never break character. Never mention AgentKit, OpenAI, or AI."
    )
    return Agent(
        **BASE,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=persona),
    )


# ── 8. Domain-specific prompts ────────────────────────────────────────────────
#
# Recipes for common domains. Paste and adapt.

PROMPTS = {
    "customer_support": (
        "You are a helpful customer support agent for AcmeCorp.\n"
        "- Always greet the customer by name if known.\n"
        "- Escalate to a human if the issue cannot be resolved in 2 turns.\n"
        "- Never make promises about refunds or SLAs without checking policy first.\n"
        "- Respond in the same language the customer uses."
    ),
    "code_reviewer": (
        "You are a senior code reviewer.\n"
        "- Focus on correctness, security, and performance in that order.\n"
        "- Be direct; skip compliments and filler.\n"
        "- Every criticism must include a specific suggestion.\n"
        "- Respond in Markdown with inline code examples."
    ),
    "data_analyst": (
        "You are a data analyst.\n"
        "- Prefer concise, quantitative answers.\n"
        "- When you quote a number, cite its source or caveat if uncertain.\n"
        "- Suggest a visualisation type when discussing data distributions.\n"
        "- If asked to 'analyse', produce: summary, key finding, recommendation."
    ),
    "legal_summariser": (
        "You are a legal document summariser.\n"
        "- Produce structured summaries: Parties, Key clauses, Obligations, Risks.\n"
        "- Flag ambiguous or non-standard clauses with ⚠️.\n"
        "- Never give legal advice; always recommend consulting a qualified lawyer.\n"
        "- Use plain English; avoid jargon except where legally necessary."
    ),
}


def agent_for_domain(domain: str) -> Agent:
    prompt = PROMPTS.get(domain)
    if prompt is None:
        raise ValueError(f"Unknown domain '{domain}'. Choose from: {list(PROMPTS)}")
    return agent_full_replace(prompt)


# ── Live demos ─────────────────────────────────────────────────────────────────


async def demo_replace_defaults(api_key: str) -> None:
    print("\n── 3. replace_defaults — customer support persona ──")
    agent = agent_for_domain("customer_support")
    session = await agent.session()
    system = "\n".join(b.text for b in agent.system_blocks)
    print(f"  SWE identity present: {'software engineering' in system}")
    print(f"  Custom identity present: {'AcmeCorp' in system}")

    result = None
    async for event in session.run("Hi, I never received my order #12345."):
        if event.type == "result":
            result = event
    print("  Response:", result.final_text[:200])


async def demo_persona(api_key: str) -> None:
    print("\n── 7. Persona — ARIA the cooking assistant ──")
    agent = agent_persona(
        name="ARIA",
        role="a Michelin-starred chef specialising in Italian cuisine",
        style="warm, enthusiastic, uses occasional Italian phrases",
    )
    session = await agent.session()
    result = None
    async for event in session.run("How do I make a classic carbonara?"):
        if event.type == "result":
            result = event
    print("  Response:", result.final_text[:300])


async def main() -> None:
    show_tool_aware_protocol()

    if not API_KEY:
        print("\nSet OPENAI_API_KEY for live demos.")
        # Show that all constructors work without API key
        for label, fn in [
            ("legacy append", lambda: agent_append_legacy()),
            ("typed append", lambda: agent_append_typed()),
            ("replace defaults", lambda: agent_full_replace("Test.")),
            ("prepend context", lambda: agent_with_prepended_context("CREATE TABLE t (id INT);")),
            ("persona ARIA", lambda: agent_persona("ARIA", "chef", "warm")),
            ("domain customer_support", lambda: agent_for_domain("customer_support")),
        ]:
            a = fn()
            blocks = a.system_blocks
            print(f"  {label}: {len(blocks)} system blocks")
        return

    await demo_replace_defaults(API_KEY)
    await demo_persona(API_KEY)
    await multi_tenant_example(API_KEY)


if __name__ == "__main__":
    asyncio.run(main())
