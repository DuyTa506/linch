import os
from uuid import uuid4

import pytest


@pytest.fixture
async def pool():
    if not os.getenv("AGENT_KIT_TEST_PG_DSN"):
        pytest.skip("AGENT_KIT_TEST_PG_DSN unavailable")
    asyncpg = pytest.importorskip("asyncpg")
    pytest.importorskip("pgqueuer")
    schema = f"wp6_{uuid4().hex}"
    dsn = os.environ["AGENT_KIT_TEST_PG_DSN"]
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=12, server_settings={"search_path": schema}
    )
    try:
        from linch.worker import install

        await install(pool)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()
