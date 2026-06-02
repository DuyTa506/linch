import asyncio

from linch import Agent


async def main() -> None:
    agent = Agent(model="gpt-5", permissions={"mode": "default"})
    session = await agent.session()
    try:
        while True:
            prompt = input("> ").strip()
            if prompt in {"/exit", "exit", "quit"}:
                break
            async for event in session.run(prompt):
                if event.type == "assistant":
                    for block in event.message.content:
                        if block.type == "text":
                            print(block.text, end="")
                    print()
                if event.type == "result":
                    break
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
