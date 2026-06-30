"""
Redis cache for query responses. Optional — if Redis is unavailable the system
degrades gracefully (no caching, every query hits Gemini + Neon).
"""
import json, logging, hashlib
from backend.config import settings

logger = logging.getLogger(__name__)
_r = None


def _cache_key(q: str) -> str:
    """Stable cache key using MD5 — survives process restarts unlike hash().

    Namespaced by DATA_TABLE because all three products (Unified-Data, CM-Elevate,
    Focus) share one Redis instance. Without the prefix, an identically-worded
    question (e.g. "total disbursement by district") would return another scheme's
    cached answer.
    """
    return f"q:{settings.DATA_TABLE}:" + hashlib.md5(q.lower().strip().encode()).hexdigest()


async def _redis():
    global _r
    if _r is None:
        try:
            import redis.asyncio as a
            _r = a.from_url(settings.REDIS_URL, decode_responses=True)
            await _r.ping()
        except Exception as e:
            logger.debug(f"Redis unavailable: {e}")
            _r = None
    return _r


async def get_cached(q: str) -> dict | None:
    r = await _redis()
    if not r:
        return None
    try:
        d = await r.get(_cache_key(q))
        if not d:
            return None
        payload = json.loads(d)
        # ── Scheme-isolation guard (defense-in-depth) ──────────────
        # Reject any cached payload that does not belong to THIS scheme's table.
        # The key is already namespaced by DATA_TABLE, but this second check makes
        # cross-scheme bleed impossible even if Redis keys ever collide or a stale
        # entry was written by another product against a shared namespace.
        owner = payload.pop("_scheme", None)
        if owner is not None and owner != settings.DATA_TABLE:
            logger.warning(
                "Cache scheme mismatch: entry owned by %r but this backend is %r — ignoring.",
                owner, settings.DATA_TABLE,
            )
            return None
        return payload
    except Exception as e:
        logger.debug(f"Cache get error: {e}")
        return None


async def set_cached(q: str, payload: dict, ttl=300):
    r = await _redis()
    if not r:
        return
    try:
        # Stamp the owning scheme so a reader can verify the entry belongs to it.
        stamped = {**payload, "_scheme": settings.DATA_TABLE}
        await r.setex(_cache_key(q), ttl, json.dumps(stamped, default=str))
    except Exception as e:
        logger.debug(f"Cache set error: {e}")


async def check_health() -> bool:
    r = await _redis()
    if not r:
        return False
    try:
        await r.ping()
        return True
    except Exception:
        return False
