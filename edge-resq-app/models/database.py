import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.config import (
    DATABASE_ADMIN_DB,
    DATABASE_URL,
    DB_POOL_MAX,
    DB_POOL_MIN,
    VECTOR_DIMENSION,
)
from app.rag.errors import DatabaseUnavailableError, DimensionMismatchError

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_pool: AsyncConnectionPool | None = None
_ready = False
_init_lock: asyncio.Lock | None = None


def get_connection():
    """Plain synchronous connection (kept for scripts)."""
    return psycopg.connect(DATABASE_URL)


async def _ensure_database() -> None:
    """Create the target database if it does not exist yet."""
    target = conninfo_to_dict(DATABASE_URL).get("dbname")
    try:
        async with await psycopg.AsyncConnection.connect(
            DATABASE_URL, connect_timeout=10
        ):
            return
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise

    logger.info("Database %r does not exist; creating it", target)
    admin_url = make_conninfo(DATABASE_URL, dbname=DATABASE_ADMIN_DB)
    async with await psycopg.AsyncConnection.connect(
        admin_url, autocommit=True, connect_timeout=10
    ) as conn:
        await conn.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target))
        )


async def _apply_schema_and_check() -> None:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, connect_timeout=10
    ) as conn:
        await conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        cur = await conn.execute(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'documents'::regclass AND attname = 'embedding'"
        )
        row = await cur.fetchone()

    column_dim = row[0] if row else None
    if column_dim != VECTOR_DIMENSION:
        raise DimensionMismatchError(
            f"documents.embedding is vector({column_dim}) but VECTOR_DIMENSION="
            f"{VECTOR_DIMENSION}. Align db/schema.sql and config before ingesting."
        )


async def init_db() -> None:
    """Create database/schema if needed and open the connection pool.

    Idempotent. Raises DatabaseUnavailableError if YugabyteDB cannot be reached.
    """
    global _pool, _ready, _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    async with _init_lock:
        if _ready:
            return
        try:
            await _ensure_database()
            await _apply_schema_and_check()
            if _pool is None:
                _pool = AsyncConnectionPool(
                    DATABASE_URL,
                    min_size=DB_POOL_MIN,
                    max_size=DB_POOL_MAX,
                    timeout=10,
                    open=False,
                )
                await _pool.open()
        except (psycopg.OperationalError, PoolTimeout, OSError) as exc:
            raise DatabaseUnavailableError(f"Cannot reach YugabyteDB: {exc}") from exc
        _ready = True
        logger.info("Database ready (pool %d-%d)", DB_POOL_MIN, DB_POOL_MAX)


async def close_db() -> None:
    global _pool, _ready
    if _pool is not None:
        await _pool.close()
    _pool = None
    _ready = False


@asynccontextmanager
async def connection():
    """Borrow a pooled async connection; commits on success, rolls back on error."""
    if not _ready:
        await init_db()
    try:
        async with _pool.connection() as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DatabaseUnavailableError(f"Database error: {exc}") from exc
