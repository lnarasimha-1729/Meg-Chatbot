"""
Rate limiting middleware
=========================
Per-IP rate limit on /api/query. Returns HTTP 429 with Retry-After header.
Uses a simple in-memory sliding-window counter (no Redis dependency).
For production multi-instance deployments, replace with slowapi + Redis.
"""
import time, logging
from collections import defaultdict, deque
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_LIMITED_PREFIX = "/api/query"

MAX_REQUESTS = 30   # per window
WINDOW_SEC   = 60   # rolling window in seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP rate limiter. Beyond MAX_REQUESTS in WINDOW_SEC → 429."""

    def __init__(self, app):
        super().__init__(app)
        self._windows: dict[str, deque] = defaultdict(deque)

    def _get_ip(self, request: Request) -> str:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(_LIMITED_PREFIX):
            return await call_next(request)

        ip  = self._get_ip(request)
        now = time.monotonic()
        dq  = self._windows[ip]

        while dq and now - dq[0] > WINDOW_SEC:
            dq.popleft()

        if len(dq) >= MAX_REQUESTS:
            retry_after = int(WINDOW_SEC - (now - dq[0])) + 1
            logger.warning(f"Rate limit hit: ip={ip} requests={len(dq)}")
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded. Max {MAX_REQUESTS} requests per minute. "
                              f"Please wait {retry_after}s.",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        dq.append(now)
        if len(dq) > MAX_REQUESTS * 2:
            while len(dq) > MAX_REQUESTS:
                dq.popleft()

        response = await call_next(request)
        remaining = MAX_REQUESTS - len(dq)
        response.headers["X-RateLimit-Limit"]     = str(MAX_REQUESTS)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Window"]    = str(WINDOW_SEC)
        return response
