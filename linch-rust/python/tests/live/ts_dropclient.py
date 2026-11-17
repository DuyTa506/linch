"""A client process the toolsec tests SIGKILL: it opens an agent on a listening server and runs once.

Usage: python ts_dropclient.py ADDRESS TOKEN CONFIG_JSON_FILE [remote]

It prints one line per event type (flushed) so the parent can see how far it got, then waits.
With `remote` it serves a `SubprocessHost` for the agent's `Bash`.
"""

from __future__ import annotations

import asyncio
import json
import sys

from linch_rust import Server, SubprocessHost


async def main() -> None:
    address, token, config_path = sys.argv[1:4]
    config = json.loads(open(config_path).read())
    host = SubprocessHost() if len(sys.argv) > 4 and sys.argv[4] == "remote" else None
    async with Server.connect(address, token=token) as server:
        agent = await server.open_agent(config, execution=host)
        print("OPENED", flush=True)
        run = await agent.run("s1", "go")
        async for envelope in run:
            print("EVENT", envelope.event.type, flush=True)
        print("RUN_ENDED", flush=True)
        await asyncio.sleep(3600)


asyncio.run(main())
