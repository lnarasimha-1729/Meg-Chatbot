"""
/api/query — single endpoint, auto-routing for the Focus NL-to-SQL engine:
  1. EDGE   : greetings, identity, off-topic → instant canned response (no API cost)
  2. REASON : follow-up over data already shown → reason over prior rows (no fetch)
  3. SQL    : data question → Gemini generates PostgreSQL SQL → execute → NL answer + chart

There is NO RAG and NO web search — this product is a pure single-table NL-to-SQL
system over the focus_pg Meghalaya FOCUS Producer Group register.

Multi-turn conversation:
  - session_id ties requests together across a browser session.
  - Each question is first resolved into a standalone question using prior context
    ("what about West Garo Hills?" → "Show the Producer Group summary for West Garo Hills.").
  - Resolved question + raw SQL data is stored per turn so arithmetic / comparison
    follow-ups produce correct, coherent answers.
"""
import time, logging, base64, asyncio, json as _json
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse

from backend.database import execute_sql_query
from backend.schemas import QueryRequest, QueryResponse
from backend.services.edge_handler import detect_edge_case
from backend.services.gemini_service import (
    resolve_question,
    classify_intent, generate_sql, generate_nl_answer,
    validate_sql, suggest_chart, answer_from_context,
)
from backend.config import settings
from backend.services.ai_service import _BASE as BASE, _CHAT_MODEL as CHAT
import httpx
from backend.services.context_store import context_store
from backend.services.pinned_sql import match_pinned_sql

router = APIRouter(prefix="/api/query", tags=["Query"])
logger = logging.getLogger(__name__)


# ── Main query endpoint ───────────────────────────────────────────────────────

@router.post("", response_model=QueryResponse)
async def query(req: QueryRequest):
    start = time.time()

    # ── Load conversation context for this session ────────────
    ctx = await context_store.get_context(req.session_id) if req.session_id else []

    # ── Step 1: Edge Case Check (FREE — no API call) ──────────
    edge = detect_edge_case(req.question)
    if edge:
        logger.info(f"Edge case: {edge['type']} | Q: {req.question[:60]}")
        await context_store.add_turn(
            req.session_id, req.question, req.question, edge["response"], "EDGE",
        )
        return QueryResponse(
            question=req.question, answer=edge["response"], intent="EDGE",
            edge_type=edge["type"], row_count=0,
            execution_time_ms=int((time.time() - start) * 1000), confidence="high",
        )

    # ── Step 2: Resolve follow-up questions (multi-turn core) ─
    resolved = await resolve_question(req.question, ctx)
    if resolved != req.question:
        logger.info(f"Resolved: '{req.question[:60]}' → '{resolved[:80]}'")

    # ── Step 3: Classify Intent (SQL | REASON) ────────────────
    # REASON requires prior data to reason over. With no analytical context the
    # answer is always SQL — skip the Gemini classification call entirely (saves
    # one full round-trip on every first-turn query → big latency win).
    has_prior_data = any(t.intent != "EDGE" and t.sql_data for t in ctx)
    if not has_prior_data:
        intent = "SQL"
    else:
        intent = await classify_intent(resolved, ctx)
    logger.info(f"Intent: {intent} | Q: {resolved[:80]}")

    # ── Step 3.5: REASON Path — answer over prior data (no fetch) ─
    if intent == "REASON":
        try:
            reason_text = await answer_from_context(resolved, ctx, req.language)
            if reason_text and reason_text.strip():
                await context_store.add_turn(
                    req.session_id, req.question, resolved, reason_text, "REASON",
                )
                prior_with_data = next(
                    (t for t in reversed(ctx) if t.intent != "EDGE" and t.sql_data), None,
                )
                prior_data = prior_with_data.sql_data if prior_with_data else None
                return QueryResponse(
                    question=req.question, answer=reason_text, intent="REASON",
                    data=prior_data or [],
                    row_count=len(prior_data) if prior_data else 0,
                    execution_time_ms=int((time.time() - start) * 1000), confidence="high",
                )
            logger.info("REASON returned empty — falling through to SQL")
        except Exception as e:
            logger.warning(f"REASON path failed, falling through to SQL: {e}")
        intent = "SQL"

    # ── Step 4: SQL Path (Neon PostgreSQL) ────────────────────
    # NOTE: the Redis response cache was REMOVED here. A 300s TTL made the same
    # question return DIFFERENT answers over time (a cached number for 5 min, then a
    # freshly regenerated — sometimes differently-shaped — query). For a data assistant
    # that is the worst failure mode. With temperature=0.0 generation, re-running the
    # query every time is deterministic AND always reflects the live table.
    try:
        # Pinned SQL (Focus product): plain "how many beneficiaries/members" → distinct unique people (~197,442),
        # bypassing the cross-scheme per-scheme split. The model only writes the prose.
        pinned = match_pinned_sql(resolved, "cm_elevate", home_scheme="Focus")
        if pinned:
            sql, conf = pinned, 0.95
            logger.info(f"Pinned SQL used | Q: {resolved[:60]}")
        else:
            sql, conf = await generate_sql(resolved, ctx)

        if "CANNOT_ANSWER" in sql:
            fallback = (
                "I can only answer questions about the Focus Producer Group (PG) records — "
                "PG lookups, district/block/bank/IVCS breakdowns, tranche-1 & tranche-2 disbursement, "
                "bookkeeper coverage, and member details (counts, gender, age, bank). "
                "Could you rephrase your question?"
            )
            await context_store.add_turn(req.session_id, req.question, resolved, fallback, "SQL")
            return QueryResponse(
                question=req.question, intent="SQL", answer=fallback, row_count=0,
                execution_time_ms=int((time.time() - start) * 1000), confidence="low",
            )

        # generate_sql already retries + repairs + self-validates, so SQL reaching here is
        # complete and valid. This is a final defensive check; on the rare miss we ask the
        # user to rephrase rather than surfacing a raw SQL/Postgres error.
        ok, reason = validate_sql(sql)
        if not ok:
            logger.warning(f"SQL failed final validation ({reason}). SQL: {sql[:120]}")
            friendly = (
                "I couldn't build a reliable query for that just now — the service was busy. "
                "Please try asking again, or rephrase it slightly."
            )
            await context_store.add_turn(req.session_id, req.question, resolved, friendly, "SQL")
            return QueryResponse(
                question=req.question, intent="SQL", answer=friendly, row_count=0,
                execution_time_ms=int((time.time() - start) * 1000), confidence="low",
            )

        # Execute on Neon PostgreSQL. If the DB rejects the query, regenerate ONCE — a fresh
        # generation usually produces a runnable variant — before surfacing the error.
        try:
            results = await execute_sql_query(sql)
        except RuntimeError as e:
            logger.warning(f"Neon exec failed ({e}); regenerating SQL once.")
            try:
                sql2, _ = await generate_sql(resolved, ctx)
                if "CANNOT_ANSWER" not in sql2 and validate_sql(sql2)[0]:
                    results = await execute_sql_query(sql2)
                    sql = sql2
                else:
                    raise e
            except RuntimeError as e2:
                logger.warning(f"Neon exec failed again: {e2}")
                friendly = (
                    "I couldn't run that query just now — please try again or rephrase it slightly."
                )
                await context_store.add_turn(req.session_id, req.question, resolved, friendly, "SQL")
                return QueryResponse(
                    question=req.question, intent="SQL", answer=friendly, row_count=0,
                    execution_time_ms=int((time.time() - start) * 1000), confidence="low",
                )

        row_count = len(results)

        answer, follow_up = await generate_nl_answer(resolved, sql, results, row_count, req.language, ctx)
        chart_type = suggest_chart(results)
        ms = int((time.time() - start) * 1000)

        payload = {
            "question":          req.question,
            "answer":            answer,
            "intent":            "SQL",
            "data":              results[:100],
            "sql_query":         sql,
            "row_count":         row_count,
            "execution_time_ms": ms,
            "confidence":        "high" if conf > 0.7 else "medium",
            "chart_type":        chart_type,
            "follow_up":         follow_up,
        }

        await context_store.add_turn(
            req.session_id, req.question, resolved, answer, "SQL", sql_data=results[:50],
        )

        if not req.include_sql:
            payload.pop("sql_query", None)
        return QueryResponse(**payload)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Query error: {e}", exc_info=True)
        # In development, surface the real error class + message so the cause is visible
        # without digging through the server console. Production stays generic.
        detail = "We encountered an issue. Please try again shortly."
        if settings.ENVIRONMENT == "development":
            detail = f"We encountered an issue. [debug: {type(e).__name__}: {str(e)[:300]}]"
        raise HTTPException(500, detail)


# ── SSE Streaming endpoint ───────────────────────────────────────────────────

@router.post("/stream")
async def query_stream(req: QueryRequest, request: Request):
    """SSE streaming version of /api/query — streams answer tokens as they arrive."""

    async def event_generator():
        start = time.time()
        ctx = await context_store.get_context(req.session_id) if req.session_id else []

        # ── Edge check ────────────────────────────────────────────
        edge = detect_edge_case(req.question)
        if edge:
            await context_store.add_turn(
                req.session_id, req.question, req.question, edge["response"], "EDGE",
            )
            yield f"data: {_json.dumps({'token': edge['response']})}\n\n"
            yield f"data: {_json.dumps({'done': True, 'intent': 'EDGE'})}\n\n"
            return

        resolved = await resolve_question(req.question, ctx)
        intent = await classify_intent(resolved, ctx)

        # ── REASON path (no fetch) ────────────────────────────────
        if intent == "REASON":
            try:
                reason_text = await answer_from_context(resolved, ctx, req.language)
                if reason_text:
                    words = reason_text.split()
                    for i, word in enumerate(words):
                        chunk = word + (" " if i < len(words) - 1 else "")
                        yield f"data: {_json.dumps({'token': chunk})}\n\n"
                    prior = next((t for t in reversed(ctx) if t.intent != "EDGE" and t.sql_data), None)
                    await context_store.add_turn(req.session_id, req.question, resolved, reason_text, "REASON")
                    yield f"data: {_json.dumps({'done': True, 'intent': 'REASON', 'data': prior.sql_data if prior else []})}\n\n"
                    return
            except Exception:
                pass
            intent = "SQL"

        # ── SQL path ──────────────────────────────────────────────
        # Response cache removed (see the non-streaming endpoint): a TTL'd cache made
        # the same question drift to different answers over time. temperature=0.0
        # generation makes re-querying deterministic and always live.
        try:
            sql, conf = await generate_sql(resolved, ctx)
            if "CANNOT_ANSWER" in sql:
                msg = "I can only answer questions about the Focus Producer Group (PG) records."
                yield f"data: {_json.dumps({'token': msg})}\n\n"
                yield f"data: {_json.dumps({'done': True, 'intent': 'SQL'})}\n\n"
                return

            ok, reason = validate_sql(sql)
            if not ok:
                logger.warning(f"Stream SQL failed final validation ({reason}). SQL: {sql[:120]}")
                msg = ("I couldn't build a reliable query for that just now — the service was busy. "
                       "Please try again or rephrase it slightly.")
                yield f"data: {_json.dumps({'token': msg})}\n\n"
                yield f"data: {_json.dumps({'done': True, 'intent': 'SQL'})}\n\n"
                return

            try:
                results = await execute_sql_query(sql)
            except RuntimeError as e:
                logger.warning(f"Stream exec failed ({e}); regenerating once.")
                sql2, _ = await generate_sql(resolved, ctx)
                if "CANNOT_ANSWER" not in sql2 and validate_sql(sql2)[0]:
                    results = await execute_sql_query(sql2)
                    sql = sql2
                else:
                    raise e
            answer, follow_up = await generate_nl_answer(resolved, sql, results, len(results), req.language, ctx)
            chart_type = suggest_chart(results)

            words = answer.split()
            for i, word in enumerate(words):
                yield f"data: {_json.dumps({'token': word + (' ' if i < len(words)-1 else '')})}\n\n"

            await context_store.add_turn(req.session_id, req.question, resolved, answer, "SQL", sql_data=results[:50])
            yield f"data: {_json.dumps({'done': True, 'intent': 'SQL', 'chart_type': chart_type, 'data': results[:100], 'follow_up': follow_up})}\n\n"

        except Exception as e:
            logger.error(f"Stream query error: {e}", exc_info=True)
            yield f"data: {_json.dumps({'token': 'An error occurred. Please try again.'})}\n\n"
            yield f"data: {_json.dumps({'done': True, 'intent': 'SQL'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Transcribe endpoint (voice → text via Gemini) ─────────────────────────────

_LANG_HINT = {"en-IN": "English", "hi-IN": "Hindi"}
_ALLOWED_AUDIO_TYPES = {"audio/webm", "audio/wav", "audio/mp3", "audio/ogg", "audio/mpeg", "audio/mp4"}
_MAX_AUDIO_BYTES = 5 * 1024 * 1024  # 5 MB


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), language: str = Form("en-IN")):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio. Please record again.")
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio file too large. Maximum size is 5 MB.")
    mime_type = (audio.content_type or "audio/webm").split(";")[0].strip()
    if mime_type not in _ALLOWED_AUDIO_TYPES:
        mime_type = "audio/webm"
    audio_b64 = base64.b64encode(audio_bytes).decode()
    lang      = _LANG_HINT.get(language, "English")

    payload = {"contents": [{"parts": [
        {"text": f"Transcribe this audio exactly as spoken in {lang}. "
                 f"Return ONLY the transcribed text — no quotes, no preamble, nothing else."},
        {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
    ]}]}

    # Audio transcription needs a model with solid audio support; flash-lite is the
    # most reliable for this on this key. Retry transient 503/5xx (Gemini is flaky)
    # so a single hiccup does not surface as "Transcription failed" to the user.
    url = f"{BASE}/models/gemini-2.5-flash-lite:generateContent?key={settings.GEMINI_API_KEY}"
    _RETRY = {429, 500, 502, 503, 504}
    last_err = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            for attempt in range(1, 4):
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code in _RETRY:
                        last_err = f"HTTP {resp.status_code}"
                        logger.warning(f"Transcribe attempt {attempt}/3 got {resp.status_code}")
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    transcript = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    transcript = transcript.strip('"').strip("'").strip()
                    if not transcript:
                        raise HTTPException(422, "Could not understand the audio. Please speak clearly and try again.")
                    return {"transcript": transcript}
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    last_err = type(e).__name__
                    logger.warning(f"Transcribe attempt {attempt}/3 network error: {e}")
                    await asyncio.sleep(0.5 * attempt)
    except HTTPException:
        raise
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Transcription parse error: {e}")
        raise HTTPException(502, "Could not understand the audio. Please try again.")
    except Exception as e:
        logger.error(f"Transcription error: {e}")
    raise HTTPException(503, f"Transcription service is busy ({last_err or 'unknown'}). Please try again in a moment.")


# ── Suggestions endpoint ──────────────────────────────────────────────────────

# Focus PG use-case questions, grouped by category for the sidebar.
_USE_CASES = {
    "📊 Overview / Summary": [
        "How many producer groups are registered under Focus?",
        "How many distinct PG names exist?",
        "How many districts have Focus PGs?",
        "How many blocks have Focus PGs?",
        "How many PG members are registered overall?",
        "What percent of PGs have received any disbursement?",
        "Give me an overall Focus PG summary",
    ],
    "🗺️ District-wise Analysis": [
        "How many PGs are there per district?",
        "Top 5 districts by Focus PG count",
        "Districts with the lowest Focus PG coverage",
        "Top-3 districts' share of all Focus PGs",
        "District-wise tranche-1 disbursement",
        "How many Focus PGs in West Jaintia Hills?",
        "Show all producer groups in West Khasi Hills",
    ],
    "🏘️ Block & Village Analysis": [
        "Top 10 blocks by Focus PG count",
        "How many PGs in Khatarshnong Laitkroh block?",
        "Top-3 blocks' share of all Focus PGs",
        "How many PGs are missing village info?",
    ],
    "💰 Disbursement (Tranches)": [
        "Total tranche-1 disbursement",
        "How many PGs have received tranche 2?",
        "Average tranche-1 amount per PG",
        "Top 10 PGs by tranche-1 disbursement",
        "PGs awaiting tranche 1 (no disbursement yet)",
        "District-wise tranche-1 disbursement",
        "PGs with a bookkeeper but no disbursement yet",
    ],
    "📘 Bookkeeper Coverage": [
        "How many PGs have identified a bookkeeper?",
        "Bookkeeper coverage by district",
        "Districts where bookkeeper coverage is lowest",
        "How many PGs are missing bookkeeper info?",
    ],
    "👥 Member Analysis": [
        "Gender split of Focus PG members",
        "Average members per Focus PG",
        "Top 10 PGs by member count",
        "PGs with member count greater than 10",
        "List all members below the age of 25",
        "What is the average member age?",
        "How many member age records are anomalies?",
    ],
    "🏦 Banking & IVCS": [
        "How many PGs have a bank account?",
        "Bank-wise split of Focus PGs",
        "How many PGs are linked to an IVCS?",
        "Top IVCS by PG linkage count",
        "How many members bank with State Bank of India?",
    ],
    "🔍 Lookups": [
        "Find the member named Kelias Biam",
        "Member lookup by EPIC ID",
        "Show all producer groups in East Khasi Hills",
        "1-page brief for Focus PGs in East Khasi Hills",
    ],
    "🧩 Data Quality": [
        "How many PGs are missing district information?",
        "How many PGs are missing village info?",
        "How many PGs are missing bookkeeper info?",
        "Run a Focus PG data-quality scorecard",
        "Member age column anomalies — count outliers",
    ],
}


@router.get("/suggestions")
async def suggestions():
    return {"categories": _USE_CASES}
