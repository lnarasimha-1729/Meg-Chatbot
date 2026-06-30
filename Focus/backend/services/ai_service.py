"""
ai_service.py — Robust AI Service Layer (transport)
====================================================
This is the single authoritative layer for all Gemini API calls.
gemini_service.py delegates here — nothing calls Gemini directly except this file.

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  query.py  (router)                                     │
  │      │                                                  │
  │  gemini_service.py  (domain logic: resolve/classify/    │
  │      │               generate_sql / generate_answer)    │
  │      │                                                  │
  │  ai_service.py  ◄── YOU ARE HERE                        │
  │      │  • Retry with exponential back-off               │
  │      │  • Circuit breaker (fail fast when Gemini down)  │
  │      │  • Request deduplication (in-flight cache)       │
  │      │  • Token budget guard                            │
  │      │  • Server-side context caching for the static    │
  │      │    schema + few-shot prefix                      │
  │      │                                                  │
  │  httpx  →  Gemini API                                   │
  └─────────────────────────────────────────────────────────┘
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

from backend.config import settings

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS and model config
# ─────────────────────────────────────────────────────────────────────────────

_BASE        = "https://generativelanguage.googleapis.com/v1beta"
# gemini-2.5-flash is materially more reliable + faster than -flash-lite on this
# key (flash-lite returns frequent 503s at 3-8s; flash is ~1s and stable).
_CHAT_MODEL  = "gemini-2.5-flash"

# Retry config. Gemini (esp. flash) returns frequent transient 503s that clear
# almost immediately, so retry FAST: short backoff + an extra attempt beats long
# exponential waits. Waits ≈ 0.4s, 0.8s, 1.6s, 3.2s.
_MAX_RETRIES        = 4
_RETRY_BACKOFF_BASE = 2.0   # base for 0.4 * 2^(attempt-1)
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# Circuit breaker config
_CB_FAILURE_THRESHOLD = 5    # open after this many consecutive failures
_CB_RECOVERY_SECONDS  = 60   # seconds before trying again (HALF_OPEN)

# Token guard — cap at ~30k tokens to prevent runaway prompts
_MAX_PROMPT_CHARS = 120_000  # ~30k tokens at ~4 chars/token

# In-flight deduplication window (seconds)
_INFLIGHT_TTL = 30.0


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────

class AIServiceUnavailable(RuntimeError):
    """Raised when the circuit breaker is OPEN — Gemini is unreachable."""

class AIPromptTooLarge(ValueError):
    """Raised when a prompt exceeds _MAX_PROMPT_CHARS."""

class AIEmptyResponse(RuntimeError):
    """Raised when Gemini returns an empty / malformed response."""


# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────────────────

class _CBState(Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class _CircuitBreaker:
    state:              _CBState = _CBState.CLOSED
    failure_count:      int      = 0
    last_failure_time:  float    = 0.0
    success_count:      int      = 0

    def record_success(self):
        if self.state == _CBState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= 2:
                log.info("[ai_service] Circuit breaker → CLOSED (recovered)")
                self.state         = _CBState.CLOSED
                self.failure_count = 0
                self.success_count = 0
        else:
            self.failure_count = 0

    def record_failure(self):
        self.failure_count    += 1
        self.last_failure_time = time.monotonic()
        self.success_count     = 0
        if self.failure_count >= _CB_FAILURE_THRESHOLD:
            if self.state != _CBState.OPEN:
                log.warning("[ai_service] Circuit breaker → OPEN after %d failures", self.failure_count)
            self.state = _CBState.OPEN

    def allow_request(self) -> bool:
        if self.state == _CBState.CLOSED:
            return True
        if self.state == _CBState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= _CB_RECOVERY_SECONDS:
                log.info("[ai_service] Circuit breaker → HALF_OPEN (probing)")
                self.state         = _CBState.HALF_OPEN
                self.success_count = 0
                return True
            return False
        return True


_cb = _CircuitBreaker()


# ─────────────────────────────────────────────────────────────────────────────
# IN-FLIGHT DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _InflightEntry:
    future:     asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


_inflight: dict[str, _InflightEntry] = {}
_inflight_lock = asyncio.Lock()


def _prompt_key(prompt: str, temperature: float, max_tokens: int) -> str:
    raw = f"{temperature}|{max_tokens}|{prompt}"
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HTTP CLIENT
# ─────────────────────────────────────────────────────────────────────────────

_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Process-wide httpx client so the TCP+TLS connection to Gemini is reused."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        async with _http_client_lock:
            if _http_client is None or _http_client.is_closed:
                _http_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=10.0),
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=40,
                        keepalive_expiry=120.0,
                    ),
                )
    return _http_client


async def aclose_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT CACHING  (Gemini cachedContents — caches the large static prompt prefix)
# ─────────────────────────────────────────────────────────────────────────────

# Server-side context caching is DISABLED: on this key/model the cachedContents
# creation + cached-call path adds a round-trip and frequently 503s, costing more
# latency than the token savings are worth. The static prefix is inlined instead.
_USE_CONTEXT_CACHE    = False

_CACHE_TTL_SECONDS    = 1800   # how long Gemini keeps the cached prefix
_CACHE_REFRESH_MARGIN = 300    # recreate this many seconds before expiry
_CACHE_FAIL_COOLDOWN  = 600    # after a failure, don't retry creating for this long


@dataclass
class _ContentCache:
    name:       str
    expires_at: float


_content_caches:   dict[str, _ContentCache] = {}
_cache_fail_until: dict[str, float]         = {}
_cache_lock = asyncio.Lock()


def _static_key(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


async def _ensure_content_cache(static_text: str) -> Optional[str]:
    """
    Return a Gemini `cachedContents` resource name for `static_text`,
    creating it if missing or near expiry. Returns None on ANY failure — the
    caller must then fall back to sending the full prompt.
    """
    key = _static_key(static_text)
    now = time.monotonic()

    entry = _content_caches.get(key)
    if entry and entry.expires_at - _CACHE_REFRESH_MARGIN > now:
        return entry.name

    if _cache_fail_until.get(key, 0.0) > now:
        return None

    async with _cache_lock:
        entry = _content_caches.get(key)
        if entry and entry.expires_at - _CACHE_REFRESH_MARGIN > time.monotonic():
            return entry.name
        if _cache_fail_until.get(key, 0.0) > time.monotonic():
            return None
        try:
            url = f"{_BASE}/cachedContents?key={settings.GEMINI_API_KEY}"
            payload = {
                "model":             f"models/{_CHAT_MODEL}",
                "systemInstruction": {"parts": [{"text": static_text}]},
                "ttl":               f"{_CACHE_TTL_SECONDS}s",
            }
            client = await _get_client()
            resp = await client.post(url, json=payload, timeout=30.0)
            resp.raise_for_status()
            name = resp.json()["name"]
            _content_caches[key] = _ContentCache(
                name=name, expires_at=time.monotonic() + _CACHE_TTL_SECONDS,
            )
            _cache_fail_until.pop(key, None)
            log.info("[ai_service] Context cache created: %s (%d chars for %ds)",
                     name, len(static_text), _CACHE_TTL_SECONDS)
            return name
        except Exception as e:
            _cache_fail_until[key] = time.monotonic() + _CACHE_FAIL_COOLDOWN
            log.warning("[ai_service] Context caching unavailable (%s) — using full prompts for ~%ds",
                        e, _CACHE_FAIL_COOLDOWN)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# CORE CALL
# ─────────────────────────────────────────────────────────────────────────────

async def ai_call(
    prompt:      str,
    temperature: float = 0.0,
    max_tokens:  int   = 1024,
    cache_prefix: Optional[str] = None,
) -> str:
    """
    Send a prompt to Gemini and return the text response.

    Guarantees: prompt-size guard, circuit breaker, in-flight dedup, retry with
    exponential back-off, and (optional) server-side caching of `cache_prefix`.
    """
    total_len = len(prompt) + (len(cache_prefix) if cache_prefix else 0)
    if total_len > _MAX_PROMPT_CHARS:
        raise AIPromptTooLarge(
            f"Prompt is {total_len:,} chars; limit is {_MAX_PROMPT_CHARS:,}. "
            "Trim context or reduce SHOTS."
        )

    if not _cb.allow_request():
        raise AIServiceUnavailable(
            "Gemini AI is temporarily unavailable. "
            f"Circuit breaker reopens in ~{_CB_RECOVERY_SECONDS}s."
        )

    key = _prompt_key((cache_prefix or "") + prompt, temperature, max_tokens)
    async with _inflight_lock:
        stale = [k for k, v in _inflight.items()
                 if time.monotonic() - v.created_at > _INFLIGHT_TTL]
        for k in stale:
            del _inflight[k]

        if key in _inflight:
            log.debug("[ai_service] Dedup hit — waiting for in-flight request")
            fut = _inflight[key].future
        else:
            loop = asyncio.get_event_loop()
            fut  = loop.create_future()
            _inflight[key] = _InflightEntry(future=fut)
            fut = None  # signal: this coroutine is the owner

    if fut is not None:
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=_INFLIGHT_TTL)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise AIServiceUnavailable("Timed out waiting for in-flight Gemini response")

    result: Optional[str] = None
    exc:    Optional[Exception] = None
    try:
        if cache_prefix and _USE_CONTEXT_CACHE:
            cache_name = await _ensure_content_cache(cache_prefix)
            if cache_name:
                try:
                    result = await _call_with_retry(
                        prompt, temperature, max_tokens, cached_content=cache_name
                    )
                except Exception as ce:
                    log.warning("[ai_service] Cached call failed (%s) — retrying without cache", ce)
                    _content_caches.pop(_static_key(cache_prefix), None)
                    result = await _call_with_retry(
                        f"{cache_prefix}\n\n{prompt}", temperature, max_tokens
                    )
            else:
                result = await _call_with_retry(
                    f"{cache_prefix}\n\n{prompt}", temperature, max_tokens
                )
        elif cache_prefix:
            # Context caching disabled — inline the static prefix. Costs a few
            # hundred extra tokens per call but avoids the cachedContents round-trip
            # and its 503-prone cached-call path (net latency win on this key).
            result = await _call_with_retry(
                f"{cache_prefix}\n\n{prompt}", temperature, max_tokens
            )
        else:
            result = await _call_with_retry(prompt, temperature, max_tokens)
        _cb.record_success()
    except Exception as e:
        _cb.record_failure()
        exc = e
    finally:
        async with _inflight_lock:
            entry = _inflight.pop(key, None)
        if entry:
            if exc is not None:
                entry.future.set_exception(exc)
            else:
                entry.future.set_result(result)

    if exc is not None:
        raise exc
    return result  # type: ignore[return-value]


async def _call_with_retry(
    prompt:        str,
    temperature:   float,
    max_tokens:    int,
    cached_content: Optional[str] = None,
) -> str:
    url = f"{_BASE}/models/{_CHAT_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
    payload = {
        "contents":         [{"parts": [{"text": prompt}]}],
        # gemini-2.5-flash is a *thinking* model: by default it spends part of the
        # maxOutputTokens budget on internal reasoning, which truncated long SQL
        # (e.g. the data-quality scorecard) mid-statement → "Unbalanced parentheses".
        # Every task here (SQL gen, intent, NL answer) is deterministic and needs no
        # extended thinking, so disable it (thinkingBudget=0) — this both frees the
        # full token budget for output and lowers latency.
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if cached_content:
        payload["cachedContent"] = cached_content

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            client = await _get_client()
            resp = await client.post(url, json=payload)
            latency_ms = int((time.monotonic() - t0) * 1000)

            if resp.status_code in _RETRY_STATUS_CODES:
                log.warning("[ai_service] HTTP %d on attempt %d/%d (%.0fms)",
                            resp.status_code, attempt, _MAX_RETRIES, latency_ms)
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                await _backoff(attempt)
                continue

            resp.raise_for_status()
            data = resp.json()

            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError, TypeError) as e:
                raise AIEmptyResponse(f"Malformed Gemini response: {e}") from e

            if not text:
                raise AIEmptyResponse("Gemini returned empty text")

            log.info("[ai_service] OK attempt=%d latency=%dms chars=%d temp=%.2f",
                     attempt, latency_ms, len(text), temperature)
            return text

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.warning("[ai_service] Network error attempt %d/%d after %dms: %s",
                        attempt, _MAX_RETRIES, latency_ms, type(e).__name__)
            last_exc = e
            await _backoff(attempt)

        except (AIEmptyResponse, AIPromptTooLarge):
            raise

        except Exception as e:
            log.error("[ai_service] Unexpected error attempt %d/%d: %s", attempt, _MAX_RETRIES, e)
            last_exc = e
            await _backoff(attempt)

    raise AIServiceUnavailable(f"Gemini unavailable after {_MAX_RETRIES} retries") from last_exc


async def _backoff(attempt: int):
    # Fast exponential: 0.4s, 0.8s, 1.6s, 3.2s — transient 503s usually clear by
    # the first retry, so don't burn multiple seconds on the early attempts.
    wait = 0.4 * (_RETRY_BACKOFF_BASE ** (attempt - 1))
    log.info("[ai_service] Backing off %.1fs before retry", wait)
    await asyncio.sleep(wait)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH PROBE
# ─────────────────────────────────────────────────────────────────────────────

async def ai_health() -> dict:
    cb_state = _cb.state.value
    if not _cb.allow_request():
        return {"status": "down", "circuit_breaker": cb_state, "latency_ms": 0}

    t0 = time.monotonic()
    try:
        text = await _call_with_retry("Reply with exactly: OK", 0.0, 8)
        latency_ms = int((time.monotonic() - t0) * 1000)
        ok = "OK" in text.upper()
        _cb.record_success()
        return {
            "status":          "ok" if ok else "degraded",
            "circuit_breaker": _cb.state.value,
            "latency_ms":      latency_ms,
        }
    except Exception as e:
        _cb.record_failure()
        return {
            "status":          "down",
            "circuit_breaker": _cb.state.value,
            "latency_ms":      int((time.monotonic() - t0) * 1000),
            "error":           str(e),
        }


def circuit_breaker_status() -> dict:
    return {
        "state":         _cb.state.value,
        "failure_count": _cb.failure_count,
        "last_failure":  _cb.last_failure_time,
    }
