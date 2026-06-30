"""
Cache Warmer
============
On startup, pre-executes the most common queries so the first real user gets an
instant cached response instead of waiting for Gemini + Neon round-trips.

Called once from main.py lifespan after Neon + Gemini are confirmed healthy.
All failures are non-fatal — the app starts normally either way.
"""
import asyncio, logging
from backend.services.cache import get_cached, set_cached

logger = logging.getLogger(__name__)

# Top queries to pre-warm — match the most common Focus PG analytical questions.
_WARM_QUERIES = [
    "How many producer groups are registered under Focus?",
    "How many PGs are there per district?",
    "Top 10 blocks by Focus PG count.",
    "Total tranche-1 disbursement.",
    "What percent of PGs have received any disbursement?",
    "District-wise tranche-1 disbursement.",
    "How many PGs have identified a bookkeeper?",
    "Gender split of Focus PG members.",
    "Bank-wise split of Focus PGs.",
    "How many PG members are registered overall?",
]


async def warm_cache() -> int:
    """
    Pre-fill the Redis cache with the top queries.
    Returns the count of queries successfully warmed.
    Runs each query with a 20-second timeout; skips on failure.
    """
    from backend.services.cache import check_health as cache_ok
    # No point warming if the cache backend (Redis) is unavailable — the results
    # cannot persist, and firing many Gemini calls at startup only saturates the
    # circuit breaker for the first real users. Skip cleanly.
    if not await cache_ok():
        logger.info("Cache backend unavailable — skipping cache warm (no Redis).")
        return 0

    from backend.services.gemini_service import (
        generate_sql, generate_nl_answer, suggest_chart, validate_sql,
    )
    from backend.database import execute_sql_query

    warmed = 0
    for q in _WARM_QUERIES:
        try:
            cached = await get_cached(q)
            if cached:
                warmed += 1
                continue

            async with asyncio.timeout(20):
                sql, conf = await generate_sql(q, context=[])
                if "CANNOT_ANSWER" in sql:
                    continue
                ok, _ = validate_sql(sql)
                if not ok:
                    continue
                results = await execute_sql_query(sql)
                if not results:
                    continue
                answer, follow_up = await generate_nl_answer(q, sql, results, len(results), "en", [])
                chart_type = suggest_chart(results)
                payload = {
                    "question":          q,
                    "answer":            answer,
                    "intent":            "SQL",
                    "data":              results[:100],
                    "sql_query":         sql,
                    "row_count":         len(results),
                    "execution_time_ms": 0,
                    "confidence":        "high" if conf > 0.7 else "medium",
                    "chart_type":        chart_type,
                    "follow_up":         follow_up,
                }
                await set_cached(q, payload)
                warmed += 1
                logger.info(f"Cache warmed: {q[:60]}")
                await asyncio.sleep(0.5)

        except asyncio.TimeoutError:
            logger.warning(f"Cache warm timeout for: {q[:60]}")
        except Exception as e:
            logger.warning(f"Cache warm failed for '{q[:50]}': {e}")

    return warmed
