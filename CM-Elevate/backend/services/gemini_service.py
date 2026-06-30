"""
Gemini Service — domain logic layer for the CM Elevate NL-to-SQL engine
=======================================================================
Handles: intent classification (SQL | REASON) · follow-up resolution ·
         SQL generation · NL answer generation · reasoning-over-prior-data ·
         SQL validation · chart suggestion.

All actual HTTP calls go through ai_service.py (retry, circuit breaker,
deduplication, token guard, context caching). Nothing here touches httpx directly.
There is NO RAG and NO web search in this product — it is a pure single-table
NL-to-SQL system over the cm_elevate scheme-disbursement register.

Architecture:
  query.py (router)
      │
  gemini_service.py ◄── YOU ARE HERE (domain logic)
      │
  ai_service.py     (transport: retry, circuit breaker, dedup, caching)
      │
  httpx → Gemini API
"""
import re
import logging

from backend.config import settings

# The three sibling scheme tables this backend is allowed to query. Cross-scheme
# analytics (Focus / Focus+ / CM Elevate) JOINs across them on EPIC / district —
# they all live in the same Neon DB. Any table OUTSIDE this set is still rejected
# by validate_sql (injection / bleed defense).
_ALLOWED_TABLES = {"meghalaya_chatbot", "focus_pg", settings.DATA_TABLE.lower()}

from backend.services.ai_service import ai_call, ai_health
from backend.services.prompt_assembler import (
    build_question_resolver_prompt,
    build_intent_prompt,
    build_sql_static_prefix,
    build_sql_dynamic,
    build_nl_answer_prompt,
    build_reason_prompt,
    is_followup,
    is_reason_question,
)
from backend.services.context_store import ConversationTurn

logger = logging.getLogger(__name__)

# SQL keywords that must NEVER appear in generated queries
FORBIDDEN_SQL = [
    r'\bINSERT\b', r'\bUPDATE\b', r'\bDELETE\b', r'\bDROP\b',
    r'\bCREATE\b', r'\bALTER\b', r'\bTRUNCATE\b', r'\bMERGE\b',
    r'\bGRANT\b', r'\bREVOKE\b', r'\bEXEC\b', r'\bEXECUTE\b',
]


# ── Question Resolver ─────────────────────────────────────────────────────────

async def resolve_question(question: str, context: list[ConversationTurn]) -> str:
    """Resolve follow-up questions into standalone questions using conversation context."""
    if not is_followup(question, context):
        return question
    prompt = build_question_resolver_prompt(question, context)
    try:
        resolved = await ai_call(prompt, temperature=0.0, max_tokens=150)
        resolved = resolved.strip().strip('"').strip("'")
        return resolved if resolved else question
    except Exception as e:
        logger.warning("Question resolver failed, using original: %s", e)
        return question


# ── Intent Classification (SQL | REASON) ──────────────────────────────────────

async def classify_intent(question: str, context: list[ConversationTurn] = None) -> str:
    """Classify question as SQL or REASON. Local heuristic short-circuits the API call when obvious."""
    ql = f" {question.lower().strip()} "

    # Hard guard 1: a fresh-count / fetch question must ALWAYS hit SQL, never reason over stale rows.
    _FETCH_VERBS = (
        'how many', 'how much', 'count', 'total', 'number of', 'list ', 'show ',
        'give me', 'breakdown', 'break down', 'per ', 'each ',
        ' paid', 'unpaid', 'pending', 'disbursed', 'sanctioned', 'average', 'sum ', 'percentage', 'ratio', 'share of',
    )
    if any(v in ql for v in _FETCH_VERBS):
        return "SQL"

    # Hard guard 2: a SINGULAR superlative wants ONE definitive row → fresh SQL with LIMIT 1, not a reasoned list.
    _SINGULAR_SUPERLATIVE = ('which ', 'what is the ', 'what district', 'what scheme', 'name the ')
    _SUPERLATIVE = ('highest', 'lowest', 'most ', 'fewest', 'largest', 'smallest', 'maximum', 'minimum', 'top ')
    _OVER_PRIOR = ('of these', 'among these', 'of those', 'shown', 'you listed', 'you mentioned', 'above', 'in the list')
    if (any(s in ql for s in _SINGULAR_SUPERLATIVE) and any(s in ql for s in _SUPERLATIVE)
            and not any(o in ql for o in _OVER_PRIOR)):
        return "SQL"

    if is_reason_question(question, context or []):
        return "REASON"

    prompt = build_intent_prompt(question, context)
    try:
        r = await ai_call(prompt, temperature=0.0, max_tokens=8)
        i = r.strip().upper().split()[0] if r.strip() else "SQL"
        if i in ("SQL", "REASON"):
            if i == "REASON":
                analytical = [t for t in (context or []) if t.intent != "EDGE" and t.sql_data]
                if not analytical:
                    return "SQL"
            return i
        return "SQL"
    except Exception as e:
        logger.warning("Intent classification failed, defaulting to SQL: %s", e)
        return "SQL"


async def answer_from_context(
    question: str,
    context: list[ConversationTurn],
    language: str = "en",
) -> str:
    """Answer by REASONING over data already in conversation history. No SQL."""
    prompt = build_reason_prompt(question, context, language)
    try:
        # temperature=0.0 → deterministic reasoning over prior rows (same follow-up,
        # same answer every time).
        return await ai_call(prompt, temperature=0.0, max_tokens=512)
    except Exception as e:
        logger.warning("answer_from_context failed: %s", e)
        raise


# ── SQL Generation (NL-to-SQL Engine) ─────────────────────────────────────────

async def generate_sql(question: str, context: list[ConversationTurn] = None) -> tuple[str, float]:
    """
    Generate PostgreSQL SQL from a natural language question.

    The static prefix (schema + few-shots + rules) is cached server-side via
    Gemini context caching, so only the small dynamic tail is sent each request.

    Returns:
        (sql, confidence) — confidence is 0.0 for CANNOT_ANSWER, 0.9 otherwise.
    """
    prefix  = build_sql_static_prefix()
    dynamic = build_sql_dynamic(question, context)

    # Gemini under load (HTTP 503 storms) can return a TRUNCATED response — a query cut
    # off mid-clause (unbalanced parens / unterminated string) that would otherwise reach
    # Postgres as an error, OR a spurious CANNOT_ANSWER for a perfectly answerable question.
    # Permanent fix: generate up to N times and only accept SQL that is COMPLETE and passes
    # validation. We repair a near-miss locally when we can; otherwise regenerate at temp 0.
    # A real CANNOT_ANSWER is accepted only on the FIRST attempt, and never overrides healthy SQL.
    MAX_ATTEMPTS = 3
    best_sql = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # temp is ALWAYS 0.0 → greedy/deterministic decoding, so the same question
        # produces the same SQL every time. Budget 2048 from the first attempt: composite
        # questions (multi-tranche casts, district completion-rate breakdowns) generate
        # long SQL that truncated at 512/1024 and forced a non-deterministic retry.
        try:
            raw = await ai_call(dynamic, temperature=0.0, max_tokens=2048, cache_prefix=prefix)
        except Exception as e:
            logger.warning("SQL generation attempt %d failed (%s)", attempt, e)
            if best_sql:
                break
            continue
        sql = _clean_sql(raw)
        # Auto-fix ROUND(<float expr>, N) → ROUND(CAST(... AS NUMERIC), N); PostgreSQL has no
        # 2-arg ROUND for double precision, so a ROUND over "bank_santioned" errors without this.
        if "CANNOT_ANSWER" not in sql:
            sql = _fix_round_float(sql)

        # A genuine "out of scope" answer is only trustworthy on the first try; on a retry
        # it usually means the model got a garbled/short completion under load — ignore it.
        if "CANNOT_ANSWER" in sql:
            if attempt == 1 and not best_sql:
                return "CANNOT_ANSWER", 0.0
            continue

        if not _looks_truncated(sql):
            ok, _ = validate_sql(sql)
            if ok:
                logger.info("Generated SQL (attempt %d): %s", attempt, sql[:150])
                return sql, 0.9

        # Truncated or invalid: try a cheap deterministic repair before spending another call.
        repaired = _repair_sql(sql)
        if repaired and not _looks_truncated(repaired):
            ok, _ = validate_sql(repaired)
            if ok:
                logger.info("Repaired truncated SQL (attempt %d): %s", attempt, repaired[:150])
                return repaired, 0.8

        logger.warning("Attempt %d SQL truncated/invalid, retrying. Tail: %r", attempt, sql[-60:])
        best_sql = sql

    # All attempts exhausted without clean SQL. Last resort: try repairing the best we got.
    repaired = _repair_sql(best_sql)
    if repaired and not _looks_truncated(repaired):
        ok, _ = validate_sql(repaired)
        if ok:
            logger.info("Repaired SQL after exhausting retries: %s", repaired[:150])
            return repaired, 0.7
    logger.error("Could not produce valid SQL after %d attempts for: %s", MAX_ATTEMPTS, question[:80])
    return "CANNOT_ANSWER", 0.0


def _repair_sql(sql: str) -> str:
    """
    Best-effort deterministic repair of a query truncated by the API under load.
    Closes an odd trailing quote and balances parentheses, then strips any dangling
    tail keyword/comma. Returns "" if it can't make something plausibly complete.
    Never invents new logic — only completes obvious cut-offs.
    """
    if not sql:
        return ""
    s = sql.strip().rstrip(";").rstrip()
    if not (s.upper().startswith("SELECT") or s.upper().startswith("WITH")):
        return ""
    if " FROM " not in f" {s.upper()} ":
        return ""
    DANGLING = {"AND", "OR", "WHEN", "THEN", "ELSE", "CASE", "WHERE", "BY", "GROUP",
                "ORDER", "SELECT", "FROM", "ON", "AS", "LIKE", "ILIKE", "NOT", "IN", "=", ","}
    changed = True
    while changed:
        changed = False
        s = s.rstrip().rstrip(",").rstrip()
        toks = s.split()
        if toks and toks[-1].upper() in DANGLING:
            s = " ".join(toks[:-1]); changed = True
    if s.count("'") % 2 != 0:
        s += "'"
    open_p, close_p = s.count("("), s.count(")")
    if close_p > open_p:
        return ""
    s += ")" * (open_p - close_p)
    return s + ";"


# bank_santioned is the only genuine double-precision column in cm_elevate; a 2-arg ROUND
# over it (ROUND(SUM(bank_santioned), 2)) is rejected by PostgreSQL (no ROUND(double, int)).
# We wrap such a ROUND's argument in CAST(... AS NUMERIC) so the query runs.
_FLOAT_COLS = ("bank_santioned",)


def _fix_round_float(sql: str) -> str:
    """
    Deterministically wrap ROUND(<expr involving a FLOAT column>, N) → ROUND(CAST(<expr> AS NUMERIC), N).
    Safety net for when the model writes ROUND(SUM(bank_santioned), 2) without the cast.
    """
    if "ROUND(" not in sql.upper():
        return sql
    out = sql
    i = 0
    result = []
    up = out.upper()
    while i < len(out):
        if up.startswith("ROUND(", i):
            j = i + len("ROUND(") - 1
            start_args = j + 1
            depth = 1
            k = start_args
            while k < len(out):
                c = out[k]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if k >= len(out):
                result.append(out[i:]); break
            args = out[start_args:k]
            d = 0; comma = -1
            for idx, c in enumerate(args):
                if c == "(":
                    d += 1
                elif c == ")":
                    d -= 1
                elif c == "," and d == 0:
                    comma = idx
            if comma != -1:
                expr = args[:comma]
                prec = args[comma + 1:]
                touches_float = any(col in expr for col in _FLOAT_COLS)
                already_numeric = "AS NUMERIC" in expr.upper()
                if touches_float and not already_numeric:
                    args = f"CAST({expr.strip()} AS NUMERIC),{prec}"
            result.append(f"ROUND({args})")
            i = k + 1
        else:
            result.append(out[i])
            i += 1
    return "".join(result)


def _looks_truncated(sql: str) -> bool:
    """
    Heuristic: did the model stop mid-statement? Catches the failure mode where a long
    query is cut off (unbalanced quotes/parens, or a dangling keyword), so we can retry
    instead of shipping a broken query to Postgres.
    """
    if not sql:
        return True
    s = sql.rstrip().rstrip(";").rstrip()
    if s.count("'") % 2 != 0:
        return True
    if s.count("(") != s.count(")"):
        return True
    last = s.upper().rsplit(None, 1)[-1] if s.split() else ""
    DANGLING = {"AND", "OR", "WHEN", "THEN", "ELSE", "CASE", "WHERE", "BY",
                "SELECT", "FROM", "ON", "AS", "LIKE", "ILIKE", "NOT", "IN", "="}
    if s.endswith(",") or last in DANGLING:
        return True
    return False


# ── Natural Language Answer ───────────────────────────────────────────────────

def _split_followup(text: str) -> tuple[str, str | None]:
    """
    Split a model reply into (answer, follow_up). The prompt asks the model to append
    a line beginning with 'FOLLOWUP:' carrying a context-specific next question.
    Returns follow_up=None when absent or explicitly 'NONE'.
    """
    answer, follow_up = text, None
    m = re.search(r"(?im)^\s*FOLLOWUP:\s*(.*)$", text)
    if m:
        answer = text[:m.start()].rstrip()
        fu = m.group(1).strip().strip('"').strip()
        if fu and fu.upper() != "NONE":
            follow_up = fu
    return answer.strip(), follow_up


async def generate_nl_answer(
    question: str, sql: str, results: list, row_count: int,
    language: str = "en", context: list[ConversationTurn] = None,
) -> tuple[str, str | None]:
    """Generate a (answer, follow_up) pair from SQL results with anti-hallucination grounding."""
    if row_count == 0:
        msgs = {
            "en": "No records found for this query. The district or scheme name may have different spelling, "
                  "or there may be no matching applications — try a broader or partial search term.",
            "hi": "इस प्रश्न के लिए कोई रिकॉर्ड नहीं मिला। कृपया कोई व्यापक या आंशिक खोज शब्द आज़माएं।",
        }
        return msgs.get(language, msgs["en"]), None
    prompt = build_nl_answer_prompt(question, sql, results, row_count, language, context)
    try:
        # temperature=0.0 so the SAME data is always phrased the same way.
        raw = await ai_call(prompt, temperature=0.0, max_tokens=512)
        return _split_followup(raw)
    except Exception as e:
        # The SQL already ran and `results` holds the correct data — do NOT fail the
        # whole request just because Gemini is overloaded on this final phrasing call.
        # Return a deterministic answer built straight from the rows instead.
        logger.warning("NL answer generation failed (%s) — using deterministic fallback", e)
        return _fallback_answer(results, row_count), None


def _fallback_answer(results: list, row_count: int) -> str:
    """
    Plain-language answer assembled directly from SQL rows, with NO LLM call.
    Used as a safety net when Gemini is unavailable for the answer-phrasing step,
    so a correct result is never lost to API flakiness.
    """
    if not results:
        return "No records found for this query."

    def fmt(v):
        try:
            f = float(v)
            if f.is_integer():
                n = int(f)
                s = str(abs(n))
                if len(s) > 3:
                    head, tail = s[:-3], s[-3:]
                    import re as _re
                    head = _re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
                    s = f"{head},{tail}"
                return ("-" if n < 0 else "") + s
        except (ValueError, TypeError):
            pass
        return str(v)

    if row_count == 1 and len(results[0]) == 1:
        k, v = next(iter(results[0].items()))
        return f"{k.replace('_', ' ').strip().capitalize()}: {fmt(v)}."
    if row_count == 1:
        parts = [f"{k.replace('_', ' ').strip()}: {fmt(v)}" for k, v in results[0].items()]
        return "Result — " + "; ".join(parts) + "."
    head = "; ".join(
        ", ".join(f"{k}: {fmt(v)}" for k, v in r.items())
        for r in results[:5]
    )
    more = f" (showing 5 of {row_count} rows)" if row_count > 5 else ""
    return f"Found {row_count} result(s){more}. {head}."


# ── Health Check ──────────────────────────────────────────────────────────────

async def check_health() -> bool:
    result = await ai_health()
    return result["status"] in ("ok", "degraded")


# ── SQL Validation (strict whitelist) ─────────────────────────────────────────

def validate_sql(sql: str) -> tuple[bool, str]:
    """
    Validate generated SQL:
    - Only SELECT/WITH statements allowed
    - No DDL or DML keywords
    - Balanced parentheses
    - No multiple statements (SQL-injection guard)
    """
    u = sql.upper().strip().rstrip(";").strip()
    if not (u.startswith("SELECT") or u.startswith("WITH")):
        return False, "Only SELECT/WITH statements allowed"
    for p in FORBIDDEN_SQL:
        if re.search(p, sql, re.IGNORECASE):
            return False, f"Forbidden keyword detected: {p}"
    if sql.count("'") % 2 != 0:
        return False, "Unterminated string literal (query was likely truncated)"
    if u.count("(") != u.count(")"):
        return False, "Unbalanced parentheses"
    clean = re.sub(r"'[^']*'", "", sql)  # remove string literals
    if ";" in clean.rstrip(";"):
        return False, "Multiple statements not allowed"
    # ── Table allowlist guard (defense-in-depth) ───────────────────
    # Cross-scheme analytics may JOIN the three sibling scheme tables (Focus / Focus+ /
    # CM Elevate). Any OTHER table name after FROM/JOIN is rejected (injection / bleed
    # to an unknown table). String literals are already stripped from `clean`.
    # CTE names defined in WITH ... AS (...) are local aliases, NOT real tables — collect them
    # so a multi-CTE query (e.g. "... FROM pc, m") isn't rejected as referencing an "unknown table".
    cte_names = {m.lower() for m in re.findall(r'(?:\bWITH\b|,)\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s+AS\s*\(', clean, re.IGNORECASE)}
    referenced = re.findall(r'\b(?:FROM|JOIN)\s+"?([A-Za-z_][\w]*)"?', clean, re.IGNORECASE)
    for tbl in referenced:
        if tbl.lower() not in _ALLOWED_TABLES and tbl.lower() not in cte_names:
            return False, f"Query references an unknown table ({tbl}); only the scheme tables are allowed"
    return True, "OK"


# ── Chart Suggestion ──────────────────────────────────────────────────────────

def suggest_chart(results: list) -> str | None:
    """
    Suggest a chart type based on result shape.
      0. Single row → None (a single record is a detail/KPI card, NEVER a chart —
         charting one record's columns against each other is meaningless).
      1. ≤2 cols, 1 label + 1 numeric, ≤6 rows → doughnut
      2. ≤2 cols, 1 label + 1 numeric, >6 rows → bar
      3. 2 label cols + 1 numeric col → stacked
      4. 1 label + ≥2 numeric cols → grouped_bar
    """
    if not results:
        return None
    cols = list(results[0].keys())
    # A single record: chart ONLY when it is a small all-numeric KPI comparison
    # (e.g. disbursed/pending/total). A SELECT * lookup (mixed text+numeric IDs)
    # is a detail card, never a chart.
    if len(results) == 1:
        if 2 <= len(cols) <= 4 and all(_is_num(results[0].get(c)) for c in cols):
            return "kpi_bar"
        return None
    if len(cols) < 2:
        return None

    lbl = [c for c in cols if not all(_is_num(r.get(c)) for r in results)]
    num = [c for c in cols if c not in lbl and all(_is_num(r.get(c)) for r in results)]

    if not num:
        return None
    if not lbl:
        return "bar"

    if len(lbl) >= 2 and len(num) == 1:
        return "stacked"
    if len(num) >= 2:
        return "grouped_bar"
    if len(results) <= 6:
        return "doughnut"
    return "bar"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_num(v) -> bool:
    try:
        float(str(v))
        return True
    except (ValueError, TypeError):
        return False


def _clean_sql(raw: str) -> str:
    """Strip markdown code fences and normalize SQL output."""
    sql = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
    sql = re.sub(r"```\s*", "", sql).strip()
    if sql and "CANNOT_ANSWER" not in sql and not sql.endswith(";"):
        sql += ";"
    return sql
