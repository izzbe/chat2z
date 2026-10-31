from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import settings

pool: AsyncConnectionPool | None = None
S = settings.SCHEMA  # validated identifier, safe to interpolate


async def open_pool() -> None:
    global pool
    # prepare_threshold=None: Supabase's pooler runs in transaction mode and breaks server-side prepared statements.
    pool = AsyncConnectionPool(
        settings.DATABASE_URL, min_size=1, max_size=5, open=False,
        kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": True},
    )
    await pool.open(wait=True, timeout=20)


async def close_pool() -> None:
    if pool:
        await pool.close()


async def fetch(sql: str, *args) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall() if cur.description else []


async def fetchone(sql: str, *args) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def execute(sql: str, *args) -> None:
    async with pool.connection() as conn:
        await conn.execute(sql, args)


@asynccontextmanager
async def tx():
    """One connection, one transaction: `async with db.tx() as conn: await conn.execute(...)`."""
    async with pool.connection() as conn:
        async with conn.transaction():
            yield conn
