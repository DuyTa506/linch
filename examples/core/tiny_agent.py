import asyncio

from linch import Agent
from linch.sessions import InMemorySessionStore


async def main() -> None:
    agent = Agent(
        model="gpt-5",
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()

    async for event in session.run("Check current files in this directory, delete file have content relate to Donald Trump"):
        if event.type == "assistant":
            for block in event.message.content:
                if block.type == "text":
                    print(block.text, end="")
        if event.type == "result":
            break

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
