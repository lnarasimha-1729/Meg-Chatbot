"""
Focus v1.0 — Meghalaya Producer Group (PG) NL-to-SQL
====================================================
Database : Neon PostgreSQL (single table: focus_pg, 221,088 rows / 37,354 PGs)
AI Engine: Gemini (gemini-2.5-flash)
Routing  : EDGE → REASON → SQL   (NO RAG, NO web search — pure NL-to-SQL)
"""
import logging, os, time
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.database import (
    check_neon_health, wake_neon,
    neon_session_context, dispose_all, get_last_neon_error,
)
from backend.services.ai_service import aclose_client
from backend.services.gemini_service import check_health as gemini_ok
from backend.services.context_store import context_store
from backend.routers.query import router as query_router
from backend.middleware.rate_limit import RateLimitMiddleware

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("focus")

# Shared frontend lives in the Unified-Data folder (one console for all products).
# Falls back to a local Focus/frontend copy if present.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FE_LOCAL  = os.path.join(_HERE, "..", "frontend")
_FE_SHARED = os.path.join(_HERE, "..", "..", "Unified-Data", "frontend")
_FE = _FE_LOCAL if os.path.exists(os.path.join(_FE_LOCAL, "ai_query.html")) else _FE_SHARED


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("  Focus v1.0 — Meghalaya Producer Group (PG) NL-to-SQL")
    logger.info(f"  Environment : {settings.ENVIRONMENT}")
    if settings.BACKEND_ENABLED:
        logger.info(f"  Data DB     : Neon PostgreSQL (table: {settings.DATA_TABLE})")
        logger.info(f"  AI Engine   : Gemini (gemini-2.5-flash)")
        logger.info(f"  Mode        : NL-to-SQL only (no RAG)")
    else:
        logger.info("  Backend Services: DISABLED")
    logger.info("=" * 60)

    neon_ok = False
    gok = False

    if settings.BACKEND_ENABLED:
        # ── Neon PostgreSQL health check ────────────────────────────
        if settings.NEON_DATABASE_URL:
            neon_ok = await wake_neon(retries=5, delay=3.0)   # 15s window for cold start
            if neon_ok:
                logger.info("✅ Neon PostgreSQL connected")
            else:
                logger.info(f"⚠️  Neon PostgreSQL FAILED — {get_last_neon_error() or 'unknown error'}")

            # Conversation context table for multi-turn persistence
            if neon_ok:
                async with neon_session_context() as db:
                    try:
                        await context_store.setup(db)
                        logger.info("✅ Conversation context table ready")
                    except Exception as ce:
                        logger.warning(f"⚠️  Context table setup failed (non-fatal): {ce}")

                # Pre-warm the asyncpg query pool so the first user query does not
                # pay the ~2s connection cost.
                try:
                    from backend.database import _get_pg_pool
                    pool = await _get_pg_pool()
                    async with pool.acquire() as c:
                        await c.fetchval("SELECT 1")
                    logger.info("✅ Query connection pool warmed")
                except Exception as pe:
                    logger.warning(f"⚠️  Query pool warm failed (non-fatal): {pe}")
        else:
            logger.warning("⚠️  NEON_DATABASE_URL not set — database queries will be unavailable")

        # ── Gemini health check ─────────────────────────────────────
        gok = await gemini_ok()
        logger.info(f"{'✅' if gok else '⚠️ '} Gemini AI {'ready' if gok else 'NOT responding — check GEMINI_API_KEY'}")

        # ── Response cache DISABLED ─────────────────────────────────
        # The Redis response cache (and its warmer) was removed: a TTL'd cache made the
        # same question return different answers over time, which is unacceptable for a
        # data assistant. Generation is temperature=0.0, so re-querying live is both
        # deterministic and always current. cache.py / cache_warmer.py remain as dead code.

        logger.info("-" * 60)
        logger.info(f"  Status: Neon={'OK' if neon_ok else 'DOWN'} | Gemini={'OK' if gok else 'DOWN'}")
        logger.info("=" * 60)

    yield
    await aclose_client()
    await dispose_all()
    logger.info("Focus shut down")


app = FastAPI(
    title="Focus — Meghalaya Producer Group (PG) NL-to-SQL",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def timing(req: Request, call_next):
    s = time.time()
    resp = await call_next(req)
    resp.headers["X-Process-Time-Ms"] = str(int((time.time() - s) * 1000))
    return resp


app.include_router(query_router)

if os.path.isdir(_FE):
    app.mount("/static", StaticFiles(directory=_FE), name="static")


@app.get("/health")
async def health():
    neon = await check_neon_health()
    gm = await gemini_ok()
    return {
        "status": "healthy" if (neon and gm) else "degraded",
        "neon": "connected" if neon else "error",
        "gemini": "ok" if gm else "error",
        "cache": "disabled",
        "table": settings.DATA_TABLE,
        "version": settings.APP_VERSION,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# Serve HTML pages with no-cache so users always get the latest UI (the chat page
# changes often during development; a stale cached copy was causing confusion).
_NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}


def _serve_fe(filename: str):
    page = os.path.join(_FE, filename)
    if os.path.exists(page):
        return FileResponse(page, headers=_NOCACHE)
    return JSONResponse(status_code=404, content={"detail": f"{filename} not found"})


@app.get("/")
async def root():
    """Landing page — the shared AI query console (use cases load from /api/query/suggestions)."""
    page = os.path.join(_FE, "ai_query.html")
    if os.path.exists(page):
        return FileResponse(page, headers=_NOCACHE)
    return {"app": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/docs"}


@app.get("/ai-query")
async def ai_query_page():
    """Chat interface — where the portal login redirects after sign-in."""
    return _serve_fe("ai_query.html")


@app.get("/portal")
@app.get("/Meghalaya_UnifiedPortal_UI.html")
async def portal_page():
    """Unified Portal dashboard (login + landing)."""
    return _serve_fe("Meghalaya_UnifiedPortal_UI.html")


@app.exception_handler(Exception)
async def exc(req: Request, e: Exception):
    logger.error(f"Unhandled: {e}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=settings.HOST, port=settings.PORT,
        reload=settings.ENVIRONMENT == "development",
        log_level=settings.LOG_LEVEL.lower(),
    )
