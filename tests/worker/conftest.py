import pytest


@pytest.fixture
async def pool(pg_dsn):
    asyncpg = pytest.importorskip("asyncpg")
    pytest.importorskip("pgqueuer")
    pool = await asyncpg.create_pool(
        pg_dsn,
        min_size=1,
        max_size=12,
    )
    try:
        from linch.worker import install

        await install(pool)
        yield pool
    finally:
        await pool.close()
