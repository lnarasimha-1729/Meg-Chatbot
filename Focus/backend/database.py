"""
Database Layer — Neon PostgreSQL (single unified database)

The Focus product is a pure NL-to-SQL system (NO RAG). All queries run
against one wide table — the Meghalaya FOCUS Producer Group (PG) register —
which lives in Neon as "focus_pg" (221,088 rows; 37,354 distinct PGs).

This module provides:
  • A pooled SQLAlchemy engine (used for health checks + conversation context).
  • execute_sql_query() — direct asyncpg execution of generated read-only SQL.
  • Friendly PostgreSQL error mapping for the NL-to-SQL pipeline.
"""
import logging, asyncio
from typing import AsyncGenerator
from sqlalchemy import text, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from backend.config import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════

def _fix_neon(url: str) -> str:
    if not url:
        raise ValueError("NEON_DATABASE_URL not set")
    url = url.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if "?" in url:
        base, params = url.split("?", 1)
        keep = [p for p in params.split("&") if not any(k in p for k in ["channel_binding", "connect_timeout"])]
        url = base + ("?" + "&".join(keep) if keep else "")
    if "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    url += "&connect_timeout=10"
    return url


class AsyncResultWrapper:
    def __init__(self, result):
        self._result = result
    def keys(self):
        return self._result.keys()
    def fetchmany(self, size: int):
        return self._result.fetchmany(size)
    def fetchall(self):
        return self._result.fetchall()
    def scalar(self):
        return self._result.scalar()


class AsyncSessionWrapper:
    def __init__(self, sync_session):
        self._sync_session = sync_session
    async def execute(self, statement, params=None):
        if params:
            result = await asyncio.to_thread(self._sync_session.execute, statement, params)
        else:
            result = await asyncio.to_thread(self._sync_session.execute, statement)
        return AsyncResultWrapper(result)
    async def commit(self):
        await asyncio.to_thread(self._sync_session.commit)
    async def rollback(self):
        await asyncio.to_thread(self._sync_session.rollback)
    async def close(self):
        await asyncio.to_thread(self._sync_session.close)


# ═══════════════════════════════════════════════════════════════
# Neon PostgreSQL Engine
# ═══════════════════════════════════════════════════════════════

_neon_engine = None
_NeonSessionFactory = None
_last_neon_error = None


def _init_neon():
    global _neon_engine, _NeonSessionFactory
    if _neon_engine is not None:
        return
    try:
        _neon_engine = create_engine(
            _fix_neon(settings.NEON_DATABASE_URL),
            pool_size=settings.NEON_POOL_SIZE,
            max_overflow=settings.NEON_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_timeout=15,
            pool_recycle=300,
            echo=settings.DEBUG,
        )
        _NeonSessionFactory = sessionmaker(bind=_neon_engine, autocommit=False, autoflush=False)
        logger.info("Neon PostgreSQL engine initialized")
    except Exception as e:
        logger.error(f"Neon init failed: {e}")
        _neon_engine = None


class neon_session_context:
    async def __aenter__(self):
        _init_neon()
        if _NeonSessionFactory is None:
            raise RuntimeError("Neon DB not initialized")
        self._session = AsyncSessionWrapper(_NeonSessionFactory())
        return self._session
    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                await self._session.rollback()
            else:
                try:
                    await self._session.commit()
                except Exception:
                    try:
                        await self._session.rollback()
                    except Exception:
                        pass
        finally:
            await self._session.close()


async def get_neon_db() -> AsyncGenerator[AsyncSessionWrapper, None]:
    async with neon_session_context() as s:
        yield s


# ═══════════════════════════════════════════════════════════════
# SQL Execution — direct asyncpg for generated SQL queries
# ═══════════════════════════════════════════════════════════════

def _friendly_pg_error(e: Exception) -> str:
    """
    Map PostgreSQL error codes to friendly, actionable messages.
    Covers the most common runtime errors encountered in NL-to-SQL pipelines.
    """
    msg = str(e).lower()
    code = getattr(getattr(e, "pgcode", None), "__str__", lambda: "")() or ""

    # Syntax / structure errors
    if "42601" in code or "syntax error" in msg:
        return ("The generated SQL contained a syntax error. "
                "Please rephrase your question or try a simpler query.")
    if "42703" in code or ("column" in msg and "does not exist" in msg):
        col = ""
        import re; m = re.search(r'column "([^"]+)"', str(e))
        if m: col = f' "{m.group(1)}"'
        return (f"Column{col} was not found in the focus_pg table. "
                "The member columns are flattened into focus_pg (there is no focus_pg_members "
                "table) — column names must match exactly.")
    if "42p01" in code or ("relation" in msg and "does not exist" in msg):
        return ("The data table could not be found. "
                "Please report this issue — the database table may have been renamed.")
    if "42883" in code or ("function" in msg and "does not exist" in msg):
        return ("An unsupported SQL function was used. "
                "Please rephrase your question differently.")

    # Data / value errors
    if "22p02" in code or "invalid input syntax for type numeric" in msg or "invalid input syntax for type double" in msg:
        return ("A monetary/amount/age column could not be parsed as a number. "
                "Every column in focus_pg is stored as text and is dirty — it must be "
                "regex-guarded before casting, e.g. WHERE TRIM(col) ~ '^[0-9]+(\\.[0-9]+)?$' "
                "then TRIM(col)::numeric.")
    if "22003" in code or "numeric field overflow" in msg:
        return ("A numeric value in the query exceeded the column's precision limit.")
    if "22007" in code or "invalid input syntax for type date" in msg:
        return ("An invalid date format was used. Disbursement date columns (finance_date, "
                "disburse_date_2) are dirty text timestamps (e.g. '2022-05-17 00:00:00') — "
                "regex-guard with ~ '^(19|20)\\d{2}-\\d{2}-\\d{2}' before casting to ::timestamp.")
    if "22012" in code or "division by zero" in msg:
        return ("Division by zero occurred in the query.")

    # Connectivity / timeout
    if "connection" in msg and ("refused" in msg or "timeout" in msg or "reset" in msg):
        return ("The database connection timed out. "
                "Neon PostgreSQL may be in cold-start — please wait 10 seconds and try again.")
    if "too many connections" in msg or "53300" in code:
        return ("The database connection pool is at capacity. Please retry in a moment.")

    # Auth
    if "28" in code or "password authentication" in msg:
        return ("Database authentication failed. Please check NEON_DATABASE_URL in your .env file.")

    return (f"Database error: {str(e)[:120]}. "
            "Please try rephrasing your question.")


# Shared asyncpg pool for generated-SQL execution. Reusing pooled connections
# avoids the ~2s TLS handshake to Neon on every query (a major latency source
# when a fresh connection was opened per request).
_pg_pool = None
_pg_pool_lock = asyncio.Lock()


async def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        async with _pg_pool_lock:
            if _pg_pool is None:
                import asyncpg
                _pg_pool = await asyncpg.create_pool(
                    _fix_neon(settings.NEON_DATABASE_URL),
                    min_size=1, max_size=settings.NEON_POOL_SIZE + settings.NEON_MAX_OVERFLOW,
                    command_timeout=30, max_inactive_connection_lifetime=240,
                )
    return _pg_pool


async def execute_sql_query(sql: str, params: list | None = None) -> list[dict]:
    """
    Execute a PostgreSQL SELECT on Neon and return rows as dicts.
    Uses a shared connection pool (no per-query connect cost).
    Raises RuntimeError with a friendly message on failure.
    """
    import asyncpg
    try:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *(params or []))
            return [dict(r) for r in rows]
    except asyncpg.PostgresError as e:
        raise RuntimeError(_friendly_pg_error(e)) from e
    except Exception as e:
        raise RuntimeError(_friendly_pg_error(e)) from e


# ═══════════════════════════════════════════════════════════════
# Health Checks
# ═══════════════════════════════════════════════════════════════

async def check_neon_health() -> bool:
    global _last_neon_error
    try:
        async with neon_session_context() as s:
            ok = (await s.execute(text("SELECT 1"))).scalar() == 1
            _last_neon_error = None
            return ok
    except Exception as e:
        _last_neon_error = str(e)
        return False


def get_last_neon_error() -> str | None:
    return _last_neon_error


async def wake_neon(retries: int = 3, delay: float = 2.0) -> bool:
    for i in range(1, retries + 1):
        if await check_neon_health():
            return True
        if _last_neon_error:
            logger.warning(f"Neon PostgreSQL attempt {i}/{retries} failed: {_last_neon_error}")
        if i < retries:
            await asyncio.sleep(delay)
    return False


# ═══════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════

async def dispose_all():
    global _pg_pool
    if _pg_pool is not None:
        try:
            await _pg_pool.close()
        except Exception:
            pass
        _pg_pool = None
    if _neon_engine:
        try:
            await asyncio.to_thread(_neon_engine.dispose)
        except Exception:
            pass


class Base(DeclarativeBase):
    pass
