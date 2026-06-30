"""
/api/query — single endpoint, auto-routing for the Unified Data NL-to-SQL engine:
  1. EDGE   : greetings, identity, off-topic → instant canned response (no API cost)
  2. REASON : follow-up over data already shown → reason over prior rows (no fetch)
  3. SQL    : data question → Gemini generates PostgreSQL SQL → execute → NL answer + chart

There is NO RAG and NO web search — this product is a pure single-table NL-to-SQL
system over the Meghalaya_Chatbot farmer-payment register.

Multi-turn conversation:
  - session_id ties requests together across a browser session.
  - Each question is first resolved into a standalone question using prior context
    ("what about paid only?" → "How many paid farmers are there per district?").
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
    cross = (req.mode == "cross_scheme")   # UI "Cross Scheme" mode → force multi-scheme handling

    # ── Load conversation context for this session ────────────
    ctx = await context_store.get_context(req.session_id) if req.session_id else []

    # ── Step 1: Edge Case Check (FREE — no API call) ──────────
    # In Cross Scheme mode, only let through the truly conversational edges (greeting/identity/
    # thanks); do NOT let the "off-topic / out-of-scope" edge short-circuit a real data question,
    # since cross-scheme questions can look off-topic to a single-scheme whitelist.
    edge = detect_edge_case(req.question)
    if edge and cross and edge.get("type") in ("off_topic", "out_of_scope", "unsupported"):
        edge = None
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
    # No response cache: every query is freshly generated + executed so the answer
    # depends only on the question and the live data, never on when it was last asked.
    try:
        # Pinned SQL: questions the LLM won't reproduce reliably (e.g. the per-quarter
        # concentration index, which needs a low-volume filter the model keeps dropping)
        # use a fixed, pre-verified query; the model only writes the NL answer.
        pinned = match_pinned_sql(resolved, "cm_elevate")
        if pinned:
            sql, conf = pinned, 0.95
            logger.info(f"Pinned SQL used | Q: {resolved[:60]}")
        else:
            sql, conf = await generate_sql(resolved, ctx, cross_scheme=cross)

        if "CANNOT_ANSWER" in sql:
            if cross:
                fallback = (
                    "I tried to answer that across all three schemes (Focus, FOCUS+, CM Elevate) but "
                    "couldn't map it to the available data. Cross-scheme questions work best when they "
                    "compare counts, districts, disbursement, or overlap — e.g. \"beneficiaries in each "
                    "scheme\", \"districts active in all 3\", or \"combined disbursement per district\". "
                    "Could you rephrase it that way?"
                )
            else:
                fallback = (
                    "I can only answer questions about the Meghalaya Focus Plus farmer payment "
                    "records — farmer lookups, district/block/village counts, payment status "
                    "(paid vs. pending), bank, batch, and totals. Could you rephrase your question?"
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

        # Execute on Neon PostgreSQL. If the DB rejects the query (e.g. a ROUND-on-float
        # type error the static fixes didn't catch), regenerate ONCE — a fresh generation
        # usually produces a runnable variant — before surfacing the error to the user.
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

        # Confidence reflects the ACTUAL outcome, not just "the LLM produced SQL".
        # generate_sql set conf=0.9 before validation/execution; downgrade it now that
        # we know whether the query was clean and whether it actually returned data.
        confidence = "high" if (conf > 0.7 and row_count > 0) else (
            "low" if row_count == 0 else "medium"
        )

        payload = {
            "question":          req.question,
            "answer":            answer,
            "intent":            "SQL",
            "data":              results[:100],
            "sql_query":         sql,
            "row_count":         row_count,
            "execution_time_ms": ms,
            "confidence":        confidence,
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
        # No response cache — always generate + execute fresh (deterministic at temp 0).
        try:
            sql, conf = await generate_sql(resolved, ctx)
            if "CANNOT_ANSWER" in sql:
                msg = "I can only answer questions about the Meghalaya Focus Plus farmer payment records."
                yield f"data: {_json.dumps({'token': msg})}\n\n"
                yield f"data: {_json.dumps({'done': True, 'intent': 'SQL'})}\n\n"
                return

            # generate_sql already retried/repaired/validated; final defensive check only.
            ok, reason = validate_sql(sql)
            if not ok:
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
            row_count = len(results)

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

# All 60 verified use-case questions, grouped by category (source: green_usecases).
# Category labels carry an emoji for the sidebar; order follows first appearance.
_USE_CASES = {
    "🧭 Across schemes": [
        "How many beneficiaries are there in each scheme?",
        "Which scheme has the most beneficiaries?",
        "Top 5 districts by total cross-scheme footprint",
        "How many beneficiaries are in both Focus and Focus+?",
        "Which districts are active in all 3 schemes?",
        "Districts with single-scheme dependency",
    ],
    "🔢 Counts": [
        "How many beneficiaries are there in Focus+?",
        "How many members are there in Focus?",
        "How many CM Elevate applications were sanctioned?",
    ],
    "🗺️ Coverage": [
        "How many districts does Focus+ reach?",
        "Which villages have only one beneficiary?",
        "Which districts have the fewest beneficiaries?",
    ],
    "📍 Geography": [
        "How many beneficiaries in Selsella block?",
        "How many beneficiaries in Nayagaon village?",
        "How many beneficiaries in West Garo Hills?",
        "How many beneficiaries in East Khasi Hills?",
        "How many beneficiaries in East Garo Hills?",
        "How many beneficiaries in North Garo Hills?",
        "How many beneficiaries in South West Garo Hills?",
        "How many beneficiaries in South Garo Hills?",
        "How many beneficiaries in West Khasi Hills?",
        "How many beneficiaries in South West Khasi Hills?",
        "How many beneficiaries in Eastern West Khasi Hills?",
        "How many beneficiaries in Ri Bhoi?",
        "How many beneficiaries in East Jaintia Hills?",
        "How many beneficiaries in West Jaintia Hills?",
        "Top 5 blocks by beneficiaries",
        "Top 5 villages by beneficiaries",
        "How many beneficiaries in the Garo Hills region?",
        "Top blocks in West Garo Hills by beneficiaries",
        "Top blocks in East Khasi Hills by beneficiaries",
        "Top blocks in East Garo Hills by beneficiaries",
        "Top blocks in North Garo Hills by beneficiaries",
        "Top blocks in South West Garo Hills by beneficiaries",
        "Top blocks in South Garo Hills by beneficiaries",
        "Top villages in West Garo Hills by beneficiaries",
        "Top villages in East Khasi Hills by beneficiaries",
        "Top villages in East Garo Hills by beneficiaries",
        "Top villages in North Garo Hills by beneficiaries",
        "Top villages in South West Garo Hills by beneficiaries",
    ],
    "🏦 Banks": [
        "Which bank serves the most beneficiaries?",
        "Bank-wise split of beneficiaries",
        "What share of beneficiaries does SBI serve?",
        "How many beneficiaries do the top 3 banks cover?",
        "What is Meghalaya Rural Bank's share?",
        "Bank-wise split in West Garo Hills?",
        "Bank-wise split in East Khasi Hills?",
        "Bank-wise split in East Garo Hills?",
        "Bank-wise split in North Garo Hills?",
        "Bank-wise split in South West Garo Hills?",
        "Bank-wise split in South Garo Hills?",
        "Which districts does State Bank of India serve most?",
        "Which districts does Meghalaya Rural Bank serve most?",
        "Which districts does Central Bank of India serve most?",
    ],
    "🏆 Ranking": [
        "Top 5 districts by beneficiaries",
        "Which district has the most beneficiaries?",
        "Which district has the fewest beneficiaries?",
        "Rank all districts by beneficiaries",
        "Bottom 5 districts by beneficiaries",
        "Which districts is SBI NOT the top bank in?",
    ],
    "🔍 Filter": [
        "Which districts have more than 5,000 beneficiaries?",
        "Which districts have fewer than 1,000 beneficiaries?",
    ],
    "⚖️ Comparison": [
        "Compare West Garo Hills and East Khasi Hills",
        "Compare the Garo Hills and Khasi Hills regions",
    ],
    "📊 Share": [
        "What % of beneficiaries are in West Garo Hills?",
    ],
    "➗ Average": [
        "Average beneficiaries per district",
        "Average beneficiaries per village",
    ],
    "🧹 Data Quality": [
        "How many beneficiaries have no mobile number?",
    ],
}


@router.get("/suggestions")
async def suggestions():
    return {"categories": _USE_CASES}
