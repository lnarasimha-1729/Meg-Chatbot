"""
Prompt Assembler — single source of truth for all LLM prompt construction
for the CM Elevate (Meghalaya government scheme disbursement) NL-to-SQL engine.

Prompt types:
  build_question_resolver_prompt  — resolve follow-up questions to standalone
  build_intent_prompt             — classify SQL vs REASON  (no RAG in this product)
  build_sql_static_prefix         — large cacheable schema + few-shots + rules
  build_sql_dynamic               — per-request follow-up context + question
  build_sql_prompt                — full SQL prompt (fallback when caching off)
  build_nl_answer_prompt          — human-readable NL answer from SQL results
  build_reason_prompt             — reason over prior data (no fetch)

Schema is verified against the LIVE Neon table "cm_elevate"
(2,847 rows, 31 columns). NOTE: the raw table has misspelled columns and stores
almost every numeric/date value as TEXT — the rules below reflect REALITY, not
the idealised NLP_to_SQL_Prompt_Engineering_Guide.md (which uses different names).
"""
import json
from backend.config import settings
from backend.services.context_store import ConversationTurn
from backend.services.cross_scheme import (
    cross_scheme_rules, cross_scheme_shots, FOCUS_PG_TBL, FOCUS_PLUS_TBL, ELEVATE_TBL,
)

LANGS = {"en": "English", "hi": "Hindi"}

# The real Neon table name (lower-case, no spaces — quote for safety/consistency).
TABLE = f'"{settings.DATA_TABLE}"'


# ── Data-quality / anti-hallucination construction rules ──────────────────────
# These rules shape the STRUCTURE of generated SQL. They contain NO counts —
# every number in an answer must be computed by the query against live data.

COUNTS_GUARD = f"""
SQL CONSTRUCTION RULES (anti-hallucination — these shape the query; they do NOT supply answer values):
  1. The ONLY table is {TABLE}. Never reference any other table (the guide's name
     'disbursements' does NOT exist) or a schema prefix.
  2. ⚠ SEVERAL COLUMNS ARE MISSPELLED IN THE REAL TABLE — use the EXACT spelling:
       "sl__no" (double underscore), "bank_santioned", "refusedy_n",
       "loan_disrbusement_1", "loan_disrbusement_2", "loan_disrbusement_3".
     All other monetary columns are spelled normally (sanctioned, total_loan, etc.).
  3. ⚠ ALMOST ALL NUMERIC COLUMNS ARE STORED AS TEXT, not numbers. Before ANY
     arithmetic, SUM, AVG, ROUND, or numeric comparison, cast with:
        NULLIF(col, '')::numeric
     Affected columns: sanctioned, subsidy_disbursement_1/2/3,
     total_subsidy_disbursement, loan_disrbusement_1/2/3, total_loan,
     total_disbursement, year.
     ("bank_santioned" is the ONLY real double-precision column — no cast needed for
      SUM/AVG/comparison. BUT see rule 13: when you ROUND it, cast the result to numeric,
      because ROUND(double precision, 2) does NOT exist in PostgreSQL.)
  4. ⚠ "year" is TEXT like '2024.0'. Compare with NULLIF(year,'')::numeric = 2024
     (NOT year = 2024, NOT year = '2024').
  5. ⚠ DATE columns are TEXT and DIRTY. Some cells hold '#ERROR!', ' ', or malformed
     values like '18-02-1015'. ALWAYS guard before casting:
        WHERE disbursement_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
              AND disbursement_date::timestamp > 'YYYY-MM-DD'
     Date columns (chronological order of disbursement): "disbursement_date" (1st subsidy),
     "disbursement_date_1" (2nd), "disbursement_date_2" (3rd); loan dates:
     "loan_disbursement_date", "loan_disbursement_date_1", "loan_disbursement_date_2".
  6. "disbursed" vs "pending": filter on loan_disbursed = 'disbursed' / 'not disbursed'.
  7. desanctioned = '#ERROR!' means the record was DESANCTIONED — exclude from "active"
     queries unless the user explicitly asks for desanctioned records.
  8. refusedy_n = 'Y' means the application was REFUSED (column is misspelled, no '=Y/N').
  9. "active records" → (desanctioned IS NULL OR desanctioned <> '#ERROR!')
                        AND (refusedy_n IS NULL OR refusedy_n <> 'Y').
  10. District / scheme / name / block / village are clean TEXT. Use exact '=' for
      known district & scheme enum values; use ILIKE '%term%' for partial name/place search.
  10b. FUZZY / MIS-SPELLED / MIS-HEARD DISTRICT — resolve it, don't fail. Questions often come from voice or
      fast typing, so the district name is frequently garbled or phonetic. There are ONLY 12 valid districts
      (listed in the schema). Map the garbled input to the closest one by sound + spelling and filter on ITS
      exact canonical name with '='. NEVER filter on the user's raw misspelling, and do NOT return CANNOT_ANSWER
      just because the spelling was off.
        - Phonetic / speech-to-text:  "best guerrillas"/"west gorilla"/"west garo" → 'West Garo Hills';
          "east cause he"/"east kasi" → 'East Khasi Hills'; "re boy"/"ribhoi"/"ri bhoy" → 'Ri Bhoi'.
        - Abbreviations: "EKH" → 'East Khasi Hills', "WGH" → 'West Garo Hills', "SWGH" → 'South West Garo Hills'.
        - Partial but specific: "west khasi" → 'West Khasi Hills', "south garo" → 'South Garo Hills', etc.
        - If two are equally plausible and you cannot disambiguate, pick the single most likely and proceed —
          resolving to a real district always beats failing.
  11. "how many / count" → COUNT(*) with NO LIMIT.  Aggregates (SUM/AVG) → NO LIMIT.
  12. Listing rows → ALWAYS add LIMIT 50 unless the user explicitly asks for more or for a count/total.
  13. Always ROUND money to 2 decimals. ROUND(value, 2) requires a NUMERIC value, so:
        • TEXT money columns:  ROUND(SUM(NULLIF(col,'')::numeric), 2)
        • "bank_santioned" (FLOAT): ROUND(SUM(bank_santioned)::numeric, 2)  ← cast the result;
          ROUND(double precision, 2) does NOT exist and will error.
  14. NEVER invent or hardcode a count or amount. If a question cannot be answered from this single
      table, output exactly: CANNOT_ANSWER
"""


# ── Verified schema (cm_elevate) ──────────────────────────────────────────────

SCHEMA = f"""
-- DATABASE: Neon PostgreSQL (standard SQL, no backticks)
-- SINGLE TABLE: {TABLE}  (2,847 rows — one row per scheme application/disbursement)
-- Source: Meghalaya government scheme disbursement & sanction register (CM Elevate).

-- COLUMNS (real names & real types — many numbers/dates are TEXT and MUST be cast):
--   "sl__no"                      BIGINT   serial number within a scheme batch (note: double underscore; real integer, no cast)
--   "application_number"          TEXT     unique application ID (e.g. MEWSI000006, ARVSR000104, MPDSI...)
--   "name"                        TEXT     beneficiary name (individual OR cooperative/society, e.g. '... IVCS')
--   "desanctioned"                TEXT     '#ERROR!' = desanctioned; else NULL
--   "district"                    TEXT     district name (clean — exact match works)
--   "scheme"                      TEXT     scheme name (13 distinct values — see below)
--   "block"                       TEXT     block within the district
--   "village"                     TEXT     village name (often NULL)
--   "sanctioned"                  TEXT  ⚠ total sanctioned amount (Rs) — cast NULLIF(...,'')::numeric
--   "bank_santioned"              FLOAT    bank-sanctioned portion (Rs) — REAL number, no cast (misspelled name)
--   "subsidy_disbursement_1"      TEXT  ⚠ 1st subsidy tranche (Rs) — cast
--   "disbursement_date"           TEXT  ⚠ date of 1st subsidy (dirty — regex-guard before ::timestamp)
--   "subsidy_disbursement_2"      TEXT  ⚠ 2nd subsidy tranche (Rs) — cast
--   "disbursement_date_1"         TEXT  ⚠ date of 2nd subsidy
--   "subsidy_disbursement_3"      TEXT  ⚠ 3rd subsidy tranche (Rs) — cast
--   "disbursement_date_2"         TEXT  ⚠ date of 3rd subsidy
--   "total_subsidy_disbursement"  TEXT  ⚠ sum of subsidy tranches (Rs) — cast
--   "loan_disrbusement_1"         TEXT  ⚠ 1st loan tranche (Rs) — cast (MISSPELLED: disrbusement)
--   "loan_disbursement_date"      TEXT  ⚠ date of 1st loan
--   "loan_disrbusement_2"         TEXT  ⚠ 2nd loan tranche (Rs) — cast (MISSPELLED)
--   "loan_disbursement_date_1"    TEXT  ⚠ date of 2nd loan
--   "loan_disrbusement_3"         TEXT  ⚠ 3rd loan tranche (Rs) — cast (MISSPELLED)
--   "loan_disbursement_date_2"    TEXT  ⚠ date of 3rd loan
--   "total_loan"                  TEXT  ⚠ total loan disbursed (Rs) — cast; '0.0' means no loan
--   "total_disbursement"          TEXT  ⚠ total_subsidy + total_loan (Rs) — cast
--   "loan_entity"                 TEXT     disbursing entity: 'Bank' or 'LIFCOM' (NULL if no loan)
--   "refusedy_n"                  TEXT     'Y' = refused; else NULL (MISSPELLED column name)
--   "if_refused_why"              TEXT     reason for refusal (e.g. 'Repeated name', 'Government Employee')
--   "month"                       TEXT     month of sanction: only 'Apr','Nov','Feb','Mar' present
--   "year"                        TEXT  ⚠ year of sanction as text '2024.0' / '2025.0' — cast
--   "loan_disbursed"              TEXT     'disbursed' or 'not disbursed'

-- REFERENCE VALUES
-- Districts (clean, exact match works):
--   East Garo Hills, West Garo Hills, North Garo Hills, South Garo Hills, South West Garo Hills,
--   East Khasi Hills, West Khasi Hills, Eastern West Khasi Hills, South West Khasi Hills,
--   East Jaintia Hills, West Jaintia Hills, Ri Bhoi
--   ⚠ These are the ONLY valid districts. The user's spelling/voice input is often WRONG — map it to the
--     closest of these BEFORE filtering (see rule 10b), e.g. "best guerrillas" → 'West Garo Hills'.
-- Schemes (13 distinct — exact match works):
--   'Meghalaya Piggery Development Scheme', 'Meghalaya Poultry Farming Scheme',
--   'Meghalaya Sericulture & Weaving Scheme (Spinning)', 'Meghalaya Sericulture & Weaving Scheme(Weaving)',
--   'PRIME Agriculture Response Vehicle Scheme', 'Meghalaya Dairy Development Scheme',
--   'PRIME Tourism Vehicle Scheme', 'Meghalaya Agriculture Warehouse Scheme',
--   'Meghalaya Goat Farming Scheme', 'Meghalaya Any Business Venture Scheme',
--   'Meghalaya Common Facility Center Scheme', 'Meghalaya Sports & Wellness Scheme',
--   'Meghalaya Motorcaravan Scheme'
-- Application number prefixes (letter before trailing digits encodes scheme + type):
--   ...I = Individual, ...R = Rural cooperative/society, ...U = Urban.
--   e.g. MEWSI/MEWSR = Warehouse, ARVSI/ARVSR/ARVSU = PRIME Agri Vehicle, MCFCR = Common Facility,
--        MPDSI = Piggery, MPFSI = Poultry, MDDSI = Dairy, PTVSI/PTVSR = PRIME Tourism Vehicle.
-- loan_entity: 'Bank', 'LIFCOM'.   loan_disbursed: 'disbursed', 'not disbursed'.
"""


# ── Few-shot examples (every one VERIFIED to run against the live table) ──────

SHOTS = f"""-- PATTERN: count everything
Q: how many applications are there in total
SQL: SELECT COUNT(*) AS total_applications FROM {TABLE};

-- PATTERN: how many disbursed (active only)
Q: how many applications have been disbursed
SQL: SELECT COUNT(*) AS disbursed_count FROM {TABLE} WHERE loan_disbursed = 'disbursed' AND (desanctioned IS NULL OR desanctioned <> '#ERROR!') AND (refusedy_n IS NULL OR refusedy_n <> 'Y');

-- PATTERN: how many pending
Q: how many applications are still not disbursed
SQL: SELECT COUNT(*) AS pending_count FROM {TABLE} WHERE loan_disbursed = 'not disbursed' AND (desanctioned IS NULL OR desanctioned <> '#ERROR!');

-- PATTERN: count by district
Q: how many beneficiaries are from East Garo Hills
SQL: SELECT COUNT(*) AS beneficiary_count FROM {TABLE} WHERE district = 'East Garo Hills';

-- PATTERN: MISSPELLED / VOICE district — map to the closest of the 12 real districts, then filter on canonical name
Q: how many beneficiaries in best guerrillas
SQL: SELECT COUNT(*) AS beneficiary_count FROM {TABLE} WHERE district = 'West Garo Hills';

-- PATTERN: phonetic district from speech-to-text
Q: beneficiaries in east cause he
SQL: SELECT COUNT(*) AS beneficiary_count FROM {TABLE} WHERE district = 'East Khasi Hills';

-- PATTERN: total subsidy scheme-wise (TEXT money → cast)
Q: what is the total subsidy disbursed scheme-wise
SQL: SELECT scheme, ROUND(SUM(NULLIF(total_subsidy_disbursement, '')::numeric), 2) AS total_subsidy, COUNT(*) AS applications FROM {TABLE} GROUP BY scheme ORDER BY total_subsidy DESC NULLS LAST;

-- PATTERN: TOTAL disbursed amount (whole scheme). total_disbursement = subsidy disbursed + loan disbursed.
Q: what is the total disbursed amount
SQL: SELECT ROUND(SUM(NULLIF(TRIM(total_disbursement), '')::numeric), 2) AS total_disbursed FROM {TABLE} WHERE TRIM(total_disbursement) ~ '^[0-9]+(\\.[0-9]+)?$';

-- PATTERN: TOTAL sanctioned amount (whole scheme)
Q: total sanctioned amount under cm elevate
SQL: SELECT ROUND(SUM(NULLIF(TRIM(sanctioned), '')::numeric), 2) AS total_sanctioned FROM {TABLE} WHERE TRIM(sanctioned) ~ '^[0-9]+(\\.[0-9]+)?$';

-- PATTERN: total disbursement by district
Q: show total disbursement by district
SQL: SELECT district, ROUND(SUM(NULLIF(total_disbursement, '')::numeric), 2) AS total_disbursed, COUNT(*) AS total_applications, SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed_count FROM {TABLE} GROUP BY district ORDER BY total_disbursed DESC NULLS LAST;

-- PATTERN: loan via LIFCOM vs Bank
Q: what is the total loan disbursed through LIFCOM vs Bank
SQL: SELECT loan_entity, ROUND(SUM(NULLIF(total_loan, '')::numeric), 2) AS total_loan_amount, COUNT(*) AS application_count FROM {TABLE} WHERE loan_disbursed = 'disbursed' AND loan_entity IS NOT NULL GROUP BY loan_entity ORDER BY total_loan_amount DESC;

-- PATTERN: total bank-sanctioned amount (bank_santioned is FLOAT — cast the ROUND target)
Q: total bank sanctioned amount
SQL: SELECT ROUND(SUM(bank_santioned)::numeric, 2) AS total_bank_sanctioned FROM {TABLE};

-- PATTERN: total loan per district (TEXT money → cast; round target is already numeric)
Q: total loan per district
SQL: SELECT district, ROUND(SUM(NULLIF(total_loan, '')::numeric), 2) AS total_loan FROM {TABLE} GROUP BY district ORDER BY total_loan DESC NULLS LAST;

-- PATTERN: count by month/year (year is TEXT '2024.0')
Q: how many applications were sanctioned in April 2024
SQL: SELECT COUNT(*) AS applications, ROUND(SUM(NULLIF(total_disbursement, '')::numeric), 2) AS total_amount FROM {TABLE} WHERE month = 'Apr' AND NULLIF(year, '')::numeric = 2024;

-- PATTERN: month-wise trend for a year
Q: show month-wise disbursement trend for 2024
SQL: SELECT month, COUNT(*) AS applications, ROUND(SUM(NULLIF(total_disbursement, '')::numeric), 2) AS total_disbursed FROM {TABLE} WHERE NULLIF(year, '')::numeric = 2024 GROUP BY month ORDER BY applications DESC;

-- PATTERN: concentration TREND over time (is scheme concentration improving or worsening across districts?).
-- Concentration = HHI of each district's share of applications, computed PER YEAR (higher HHI / higher top-district
-- share = MORE concentrated = a few districts dominating; lower = more even spread). year is TEXT '2024.0' → LEFT(,4).
-- ⚠ CAVEATS the NL answer MUST state: only CM Elevate carries a year (Focus/Focus+ have no enrolment date, so this
-- trend is CM-Elevate-only), and there are just 2 years with 2025 PARTIAL (far fewer apps) — so any rise/fall is
-- INDICATIVE, not conclusive. Rising HHI/top-share = worsening (dominance); falling = improving (catching up).
Q: concentration trend over time
SQL: WITH per AS (SELECT LEFT(TRIM(year),4) AS yr, UPPER(TRIM(district)) AS d, COUNT(*) AS n FROM {TABLE} WHERE year ~ '^[0-9]{{4}}' AND NULLIF(TRIM(district),'') <> '' GROUP BY LEFT(TRIM(year),4), UPPER(TRIM(district))), tot AS (SELECT yr, SUM(n) AS total FROM per GROUP BY yr) SELECT per.yr AS year, tot.total AS applications, ROUND(SUM(POWER(per.n::numeric/tot.total,2)),4) AS concentration_hhi, ROUND(MAX(per.n::numeric/tot.total)*100,1) AS top_district_share_pct FROM per JOIN tot ON per.yr=tot.yr GROUP BY per.yr, tot.total ORDER BY per.yr;

-- PATTERN: concentration index PER QUARTER + its variance over time (finer-grained version of the yearly trend).
-- Quarter from the real disbursement_date timestamp ('YYYY-Qn'); index = HHI of district shares of applications per
-- quarter (high = few districts dominating, low = even spread). The chat plots the quarter→HHI series.
-- Low-volume quarters (<20 apps) are EXCLUDED via HAVING (they give artifact HHIs of 50-100% off a handful of apps).
-- ⚠ CAVEATS the NL answer MUST state: CM-Elevate-only (only scheme with dates); the trend is still indicative
-- (2025 partial) and should be read as noisy / no strong trend unless a clear direction holds across quarters.
Q: compute scheme concentration index per district per quarter and show its variance over time
SQL: WITH per AS (SELECT to_char(disbursement_date::timestamp,'YYYY-"Q"Q') AS q, UPPER(TRIM(district)) AS d, COUNT(*) AS n FROM {TABLE} WHERE disbursement_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' AND NULLIF(TRIM(district),'') <> '' GROUP BY 1, 2), tot AS (SELECT q, SUM(n) AS total FROM per GROUP BY q HAVING SUM(n) >= 20) SELECT per.q AS quarter, tot.total AS applications, ROUND(SUM(POWER(per.n::numeric/tot.total,2)),4) AS concentration_hhi, ROUND(MAX(per.n::numeric/tot.total)*100,1) AS top_district_share_pct FROM per JOIN tot ON per.q=tot.q GROUP BY per.q, tot.total ORDER BY per.q;

-- PATTERN: disbursements after a date (dirty TEXT date → regex-guard before cast)
Q: which applications received disbursement after January 1 2025
SQL: SELECT application_number, name, district, scheme, disbursement_date, total_disbursement FROM {TABLE} WHERE disbursement_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' AND disbursement_date::timestamp > '2025-01-01' ORDER BY disbursement_date::timestamp DESC LIMIT 50;

-- PATTERN: look up an application
Q: find details of application number MEWSR000040
SQL: SELECT * FROM {TABLE} WHERE application_number = 'MEWSR000040';

-- PATTERN: name / place search (search name, village, block)
Q: search for beneficiary named Songsak
SQL: SELECT application_number, name, district, scheme, loan_disbursed, total_disbursement FROM {TABLE} WHERE name ILIKE '%Songsak%' OR village ILIKE '%Songsak%' OR block ILIKE '%Songsak%' LIMIT 50;

-- PATTERN: cooperatives in a district with no loan yet (active only)
Q: list all cooperatives from West Garo Hills that haven't received loans
SQL: SELECT application_number, name, block, village, scheme, sanctioned FROM {TABLE} WHERE district = 'West Garo Hills' AND loan_disbursed = 'not disbursed' AND (refusedy_n IS NULL OR refusedy_n <> 'Y') AND (desanctioned IS NULL OR desanctioned <> '#ERROR!') ORDER BY name LIMIT 50;

-- PATTERN: count who got all three subsidy tranches (cast each)
Q: how many beneficiaries received all three subsidy tranches
SQL: SELECT COUNT(*) AS three_tranche_count FROM {TABLE} WHERE NULLIF(subsidy_disbursement_1, '')::numeric > 0 AND NULLIF(subsidy_disbursement_2, '')::numeric > 0 AND NULLIF(subsidy_disbursement_3, '')::numeric > 0;

-- PATTERN: scheme filter + district
Q: list all PRIME Agriculture Vehicle Scheme applicants from Ri Bhoi
SQL: SELECT application_number, name, block, village, sanctioned, total_disbursement, loan_disbursed FROM {TABLE} WHERE scheme = 'PRIME Agriculture Response Vehicle Scheme' AND district = 'Ri Bhoi' ORDER BY name LIMIT 50;

-- PATTERN: average/min/max sanctioned for a scheme (cast)
Q: what is the average sanctioned amount under the Warehouse Scheme
SQL: SELECT ROUND(AVG(NULLIF(sanctioned, '')::numeric), 2) AS avg_sanctioned, ROUND(MIN(NULLIF(sanctioned, '')::numeric), 2) AS min_sanctioned, ROUND(MAX(NULLIF(sanctioned, '')::numeric), 2) AS max_sanctioned FROM {TABLE} WHERE scheme = 'Meghalaya Agriculture Warehouse Scheme';

-- PATTERN: refusal reasons
Q: how many applications were refused and why
SQL: SELECT if_refused_why AS reason, COUNT(*) AS count FROM {TABLE} WHERE refusedy_n = 'Y' GROUP BY if_refused_why ORDER BY count DESC;

-- PATTERN: desanctioned records
Q: show all desanctioned records
SQL: SELECT application_number, name, district, scheme, month, year FROM {TABLE} WHERE desanctioned = '#ERROR!' ORDER BY district, name LIMIT 50;

-- PATTERN: district-wise completion rate
Q: district-wise disbursement completion rate
SQL: SELECT district, COUNT(*) AS total, SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed, SUM(CASE WHEN loan_disbursed = 'not disbursed' THEN 1 ELSE 0 END) AS pending, ROUND(100.0 * SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS disbursement_pct FROM {TABLE} WHERE (desanctioned IS NULL OR desanctioned <> '#ERROR!') AND (refusedy_n IS NULL OR refusedy_n <> 'Y') GROUP BY district ORDER BY disbursement_pct DESC;

-- PATTERN: partial disbursement (subsidy given but no loan yet)
Q: find beneficiaries who received subsidy but no loan
SQL: SELECT application_number, name, district, scheme, total_subsidy_disbursement, total_loan, sanctioned FROM {TABLE} WHERE NULLIF(total_subsidy_disbursement, '')::numeric > 0 AND (total_loan IS NULL OR NULLIF(total_loan, '')::numeric = 0) ORDER BY district, name LIMIT 50;

-- PATTERN: block-wise summary for a district
Q: block-wise summary for West Garo Hills
SQL: SELECT block, COUNT(*) AS total_applications, ROUND(SUM(NULLIF(sanctioned, '')::numeric), 2) AS total_sanctioned, ROUND(SUM(NULLIF(total_disbursement, '')::numeric), 2) AS total_disbursed, SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed_count FROM {TABLE} WHERE district = 'West Garo Hills' GROUP BY block ORDER BY total_disbursed DESC NULLS LAST;

-- PATTERN: applications per scheme
Q: how many applications per scheme
SQL: SELECT scheme, COUNT(*) AS applications FROM {TABLE} GROUP BY scheme ORDER BY applications DESC;
"""


# ── SQL prompt RULES (static — part of the cacheable prefix) ──────────────────

_SQL_RULES = f"""RULES:
- Output ONLY a valid PostgreSQL SELECT or WITH statement. No markdown, no backticks, no explanation.
- TABLE SCOPE IS DECIDED FROM INTENT: first run STEP 0 of the CROSS-SCHEME RULES to decide whether the answer
  needs one scheme or several, then pick the SMALLEST set of tables. Default to THIS backend's home table {TABLE}
  (CM Elevate). Use the OTHER scheme tables ({FOCUS_PLUS_TBL} Focus+, {FOCUS_PG_TBL} Focus) only when the intent
  truly spans schemes. Remember: {TABLE} has NO EPIC and NO bank-account column (CS-2/CS-3), so it joins the
  others only by district or fuzzy name. Never invent a table or column not in the schema / cross-scheme rules.
- Use the EXACT (sometimes misspelled) column names: "sl__no", "bank_santioned", "refusedy_n",
  "loan_disrbusement_1/2/3". Everything else is spelled normally.
- Cast every TEXT money/year column before arithmetic or comparison: NULLIF(col, '')::numeric.
  ("bank_santioned" is already a real number — do NOT cast it for SUM/AVG/comparison.)
- ROUND needs a NUMERIC argument. For "bank_santioned" (a FLOAT), cast the ROUND target to numeric:
  ROUND(SUM(bank_santioned)::numeric, 2).  ROUND(double precision, 2) does NOT exist and errors.
- Year filter: NULLIF(year, '')::numeric = 2024.
- Date filter: guard first — disbursement_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' AND disbursement_date::timestamp > '...'.
- Disbursed: loan_disbursed = 'disbursed'.  Pending: loan_disbursed = 'not disbursed'.
- Refused: refusedy_n = 'Y'.   Desanctioned: desanctioned = '#ERROR!'.
- "active" → (desanctioned IS NULL OR desanctioned <> '#ERROR!') AND (refusedy_n IS NULL OR refusedy_n <> 'Y').
- District & scheme: exact '=' for known enum values; ILIKE '%term%' for partial name/place search.
- FUZZY DISTRICTS: the input district is often misspelled / mis-heard (voice). There are ONLY 12 valid districts.
  Map the garbled input to the closest canonical district by sound + spelling and filter on THAT exact name with
  '=' (e.g. "best guerrillas"→'West Garo Hills', "east cause he"→'East Khasi Hills', "re boy"→'Ri Bhoi',
  "EKH"→'East Khasi Hills'). Never filter on the raw misspelling; never CANNOT_ANSWER for a bad district spelling.
- Generic region words: "Garo Hills" → district IN (the 5 Garo districts); "Khasi Hills" → the 4 Khasi districts;
  "Jaintia Hills" → ('East Jaintia Hills','West Jaintia Hills').
- "warehouse scheme" → 'Meghalaya Agriculture Warehouse Scheme'; "CFC"/"common facility" →
  'Meghalaya Common Facility Center Scheme'; "PRIME"/"vehicle scheme" →
  'PRIME Agriculture Response Vehicle Scheme' (ask nothing — pick the agriculture one unless "tourism" is said).
- ROUND all money to 2 decimals.   Order financial results by amount DESC NULLS LAST.
- Count/total questions → COUNT(*)/SUM(...) with NO LIMIT.
- Listing rows → ALWAYS add LIMIT 50 (unless the user explicitly asks for all or for a count).
- Always alias columns with readable names (AS total_disbursed, AS applications, etc.).
- For a cross-scheme ask that needs an EPIC or bank account on CM Elevate (it has NEITHER — see CROSS-SCHEME
  RULE CS-3), answer the possible part and state the limit, or output CANNOT_ANSWER.
- If the question genuinely cannot be answered from any scheme table, output exactly: CANNOT_ANSWER"""


def build_sql_static_prefix() -> str:
    """
    The large, byte-for-byte identical portion of the SQL prompt
    (schema + counts guard + few-shots + rules). Cached server-side via
    Gemini context caching (see ai_service.ai_call's cache_prefix param).
    """
    return (
        f"{SCHEMA}\n\n{COUNTS_GUARD}\n\n{cross_scheme_rules('CM Elevate')}\n\n"
        f"EXAMPLES:\n{SHOTS}\n\n{cross_scheme_shots()}\n\n{_SQL_RULES}"
    )


def build_sql_dynamic(question: str, context: list[ConversationTurn] = None) -> str:
    """The per-request tail of the SQL prompt: follow-up context + question."""
    prior_hint = ""
    if context:
        analytical = [t for t in context if t.intent in ("SQL", "REASON")]
        if analytical:
            lines = ["FOLLOW-UP CONTEXT (recent conversation — use to understand what the user is looking at):"]
            for t in analytical[-3:]:
                lines.append(f"  User asked: \"{t.resolved_question}\"")
                lines.append(f"  Answer: {t.answer[:200]}")
                if t.sql_data:
                    lines.append(f"  Data returned: {json.dumps(t.sql_data[:10], default=str)}")
            lines.append("The current question may be a follow-up. Apply any implied filters — district, "
                         "scheme, block, loan_entity, or disbursed/pending scope — from above.\n")
            prior_hint = "\n".join(lines) + "\n"

    return f"{prior_hint}Question: {question}\nSQL:"


def build_sql_prompt(question: str, context: list[ConversationTurn] = None) -> str:
    """Full SQL prompt (static prefix + dynamic tail). Fallback when caching is unavailable."""
    return f"{build_sql_static_prefix()}\n\n{build_sql_dynamic(question, context)}"


# ── Follow-up signal detection (heuristic, no API call) ───────────────────────

_REFERENTIAL = frozenset([
    ' it ', " it's", ' its ', ' that ', ' this ', ' those ', ' these ',
    ' them ', ' their ', ' there ', ' such ', ' same ',
    'that one', 'this one', 'the same', 'the previous', 'the above',
])

_CONTINUATION = frozenset([
    'what about', 'how about', 'same for', 'and the', 'and what', 'and how',
    'now show', 'now what', 'also show', 'as well', 'and also',
    'and pending', 'and disbursed', 'and refused',
])

_AGGREGATION = frozenset([
    'sum of', 'total of', 'combine', 'add both', 'add them', 'add up',
    'both of', 'all three', 'all of them', 'altogether',
])

_REASONING = frozenset([
    'why ', 'why?', 'why is', 'why are', 'why does', 'why did', 'why has',
    'explain', 'explanation', 'reason for', 'what caused', 'how come',
    'what does this mean', 'what does that mean', 'interpret',
    'summarize', 'summarise', 'summary',
    'tell me more', 'elaborate', 'in short', 'in summary',
    'what about', 'and what about', 'how about',
    'show me only', 'filter by', 'break that down', 'break it down',
    'compare that', 'compare those',
])

_REFLECTIVE = frozenset([
    'which is the highest', 'which is the lowest',
    'which one is highest', 'which one is lowest',
    'which is bigger', 'which is smaller', 'which has more', 'which has less',
    'what is the highest', 'what is the lowest',
    'biggest', 'smallest', 'difference between',
    'top one', 'bottom one',
    'which district', 'which block', 'which scheme', 'which entity', 'which village',
    'which one', 'which has', 'which had', 'which is',
    ' lowest', ' highest', ' most ', ' least ',
    'the lowest', 'the highest', 'the most', 'the least',
])

_TOPIC_TERMS = {
    'application', 'applications', 'beneficiar', 'name', 'society', 'cooperative',
    'district', 'block', 'village', 'meghalaya', 'garo', 'khasi', 'jaintia', 'ri bhoi',
    'scheme', 'piggery', 'poultry', 'dairy', 'warehouse', 'sericulture', 'prime', 'vehicle',
    'tourism', 'goat', 'business', 'facility',
    'disbursed', 'pending', 'subsidy', 'loan', 'sanctioned', 'tranche', 'amount', 'disbursement',
    'lifcom', 'bank', 'entity', 'refused', 'desanctioned', 'month', 'year', 'rate', 'completion',
}


def _norm(text: str) -> str:
    return f" {text.lower().strip()} "


def is_followup(question: str, context: list[ConversationTurn]) -> bool:
    """Decide if a question needs context-aware resolution."""
    if not context:
        return False
    analytical = [t for t in context if t.intent != "EDGE"]
    if not analytical:
        return False

    q = _norm(question)
    n_words = len(q.split())

    if any(sig in q for sig in _REFERENTIAL):  return True
    if any(sig in q for sig in _CONTINUATION): return True
    if any(sig in q for sig in _AGGREGATION):  return True
    if any(sig in q for sig in _REASONING):    return True
    if any(sig in q for sig in _REFLECTIVE):   return True

    q_topics   = {t for t in _TOPIC_TERMS if t in q}
    ctx_blob   = " ".join(_norm(t.resolved_question) for t in analytical[-3:])
    ctx_topics = {t for t in _TOPIC_TERMS if t in ctx_blob}
    shared_topics = q_topics & ctx_topics
    new_topics    = q_topics - ctx_topics

    if n_words >= 6 and new_topics and not shared_topics:
        return False
    if n_words <= 7:
        return True
    if shared_topics:
        return True
    return False


def is_reason_question(question: str, context: list[ConversationTurn]) -> bool:
    """
    Decide if a question can be answered purely by REASONING over prior data,
    without fetching new SQL. Conservative — when unsure, return False.
    """
    if not context:
        return False
    analytical = [t for t in context if t.intent != "EDGE"]
    if not analytical:
        return False
    has_data = any(t.sql_data for t in analytical)
    if not has_data:
        return False

    q = _norm(question)
    if any(sig in q for sig in _REASONING):
        return True

    if any(sig in q for sig in _REFLECTIVE):
        q_topics   = {t for t in _TOPIC_TERMS if t in q}
        ctx_blob   = " ".join(_norm(t.resolved_question) for t in analytical[-3:])
        ctx_topics = {t for t in _TOPIC_TERMS if t in ctx_blob}
        new_topics = q_topics - ctx_topics
        if len(new_topics) <= 1:
            return True
    return False


# ── Internal context formatter ─────────────────────────────────────────────────

def _row_headline(rows: list, max_rows: int = 6) -> str:
    if not rows:
        return ""
    rows = rows[:max_rows]
    cols = list(rows[0].keys())

    def _is_num(v):
        try:
            float(str(v)); return True
        except (ValueError, TypeError):
            return False

    num_cols = [c for c in cols if all(_is_num(r.get(c)) for r in rows if r.get(c) is not None)]
    lbl_cols = [c for c in cols if c not in num_cols]
    if not num_cols:
        return "; ".join(", ".join(f"{k}={v}" for k, v in r.items()) for r in rows)
    metric = num_cols[0]
    parts = []
    for r in rows:
        label_bits = " | ".join(str(r.get(c, "")) for c in lbl_cols) or "row"
        parts.append(f"{label_bits} → {metric}={r.get(metric)}")
    return "; ".join(parts)


def _fmt_context(context: list[ConversationTurn], *, mode: str = "compact", max_turns: int = 4) -> str:
    if not context:
        return ""
    analytical = [t for t in context if t.intent != "EDGE"]
    if not analytical:
        return ""
    analytical = analytical[-max_turns:]

    if mode == "full":
        lines = ["CONVERSATION HISTORY (use the actual data rows below to answer the question):"]
        for i, t in enumerate(analytical, 1):
            lines.append(f"[{i}] User asked: {t.resolved_question}")
            short_ans = (t.answer[:300] + "…") if len(t.answer) > 300 else t.answer
            lines.append(f"     Answer given: {short_ans}")
            if t.sql_data:
                lines.append(f"     Data rows ({len(t.sql_data)}): {json.dumps(t.sql_data, default=str)}")
        lines.append("")
        return "\n".join(lines) + "\n"

    lines = ["CONVERSATION HISTORY (recent turns — use to resolve references and maintain continuity):"]
    for i, t in enumerate(analytical, 1):
        lines.append(f"[{i}] Q: {t.resolved_question}")
        short_ans = (t.answer[:200] + "…") if len(t.answer) > 200 else t.answer
        lines.append(f"     A: {short_ans}")
        if t.sql_data:
            headline = _row_headline(t.sql_data, max_rows=6)
            if headline:
                lines.append(f"     Key numbers: {headline}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ── Question Resolver ─────────────────────────────────────────────────────────

def build_question_resolver_prompt(question: str, context: list[ConversationTurn]) -> str:
    ctx_block = _fmt_context(context, mode="compact", max_turns=4).rstrip()

    analytical = [t for t in context if t.intent != "EDGE"]
    if analytical:
        last = analytical[-1]
        if last.sql_data:
            rows_preview = json.dumps(last.sql_data[:15], default=str)
            ctx_block += (
                f"\n\nMOST RECENT TURN DATA (use these actual rows to resolve references):\n"
                f"Question: {last.resolved_question}\n"
                f"Answer summary: {last.answer[:300]}\n"
                f"Data rows: {rows_preview}"
            )

    return f"""You rewrite follow-up questions into complete, standalone questions for the
CM Elevate Meghalaya government scheme disbursement assistant.

{ctx_block}

CURRENT USER MESSAGE: "{question}"

YOUR JOB:
Rewrite the current message into ONE complete, standalone question that makes sense without the history,
while keeping the user's tone and meta-intent intact.

PRESERVE THE USER'S INTENT WORDS:
- Keep "why", "explain", "summarize" — they want reasoning over the prior data, not a fresh fetch.
- Keep "which is highest/lowest", "compare", "difference between" — a reflective comparison.
- Keep "show", "list", "give me" — they want fresh data.

RESOLUTION RULES:
1. Replace pronouns ("it", "that", "those", "them") with the concrete subject from history.
2. Carry forward filters (district, scheme, block, disbursed/pending, Bank/LIFCOM) UNLESS the user changes them.
3. If the user introduces a brand-new topic not in history, do NOT bolt prior filters on.
4. If the message is already standalone, return it unchanged.
5. Output ONLY the rewritten question. No quotes, no prefix, no explanation.

EXAMPLES:
History: "total disbursement by district" answered with district totals.
"what about just disbursed ones?"  →  "What is the total disbursement by district for disbursed applications only?"

History: shows scheme-wise subsidy totals.
"which scheme is highest?"  →  "Which scheme has the highest total subsidy in the data shown?"

History: West Garo Hills block summary.
"what about Ri Bhoi?"  →  "Show the block-wise summary for Ri Bhoi."

REWRITTEN QUESTION:"""


# ── Intent Classification (SQL vs REASON — no RAG in this product) ────────────

def build_intent_prompt(question: str, context: list[ConversationTurn] = None) -> str:
    ctx = _fmt_context(context or [], mode="compact", max_turns=3)
    has_prior_data = bool(context) and any(
        t.intent != "EDGE" and t.sql_data for t in (context or [])
    )
    reason_hint = (
        "REASON is AVAILABLE — prior turns have data rows you can reason about."
        if has_prior_data else
        "REASON is NOT AVAILABLE — there is no prior data to reason about. Choose SQL."
    )

    return f"""{ctx}Route this user message to ONE of: SQL or REASON.
{reason_hint}

SQL    = run a fresh query against the LIVE scheme-disbursement table for numbers, counts, lists, breakdowns, lookups.
REASON = answer using ONLY the prior conversation data shown above. No new fetch. Use this when the user is
         asking to reason ABOUT what was already shown — "why", "explain", "summarize", "which one is highest
         among these", "what does that mean".

DECISION ORDER:
1. Reasoning verbs (why / explain / summarize / interpret) over prior data → REASON.
2. Reflective comparison ("which is the highest?", "biggest one?") referring to data already shown → REASON.
3. Anything asking for fresh numbers, lists, lookups, breakdowns, totals → SQL.
4. When in doubt between SQL and REASON, prefer SQL (fresh data is safer than stale).

EXAMPLES:
"How many applications are there?"                 → SQL
"Total subsidy disbursed scheme-wise"              → SQL
"Find application MEWSR000040"                     → SQL
"Total loan through LIFCOM vs Bank"                → SQL
(after a scheme breakdown) "which is highest?"     → REASON
(after a breakdown)        "explain that"          → REASON
(after district totals)    "summarize"             → REASON

Reply with EXACTLY one word: SQL or REASON.

Question: {question}
Answer:"""


# ── Reasoning over prior data (no fetch) ─────────────────────────────────────

def build_reason_prompt(question: str, context: list[ConversationTurn], language: str = "en") -> str:
    lang_name  = LANGS.get(language, "English")
    lang_instr = f"Respond in {lang_name}." if language != "en" else ""

    ctx = _fmt_context(context or [], mode="full", max_turns=4)

    analytical = [t for t in (context or []) if t.intent != "EDGE" and t.sql_data]
    primary_data_block = ""
    if analytical:
        last = analytical[-1]
        rows_json = json.dumps(last.sql_data, default=str)
        primary_data_block = (
            f"PRIMARY DATA TO REASON ABOUT (most recent query results):\n"
            f"Question that produced this data: \"{last.resolved_question}\"\n"
            f"All data rows: {rows_json}\n"
        )

    return f"""You are the CM Elevate Meghalaya government scheme disbursement assistant.
The user is asking a follow-up that should be answered by REASONING over the data already shown in this
conversation. Do NOT invent new numbers. Do NOT pretend to query a database. Use only the rows below.

{primary_data_block}
FULL CONVERSATION HISTORY:
{ctx}
USER'S QUESTION: "{question}"

HOW TO ANSWER:
- Start with the PRIMARY DATA above — these are the actual rows from the most recent query.
- For "which district/scheme/block/entity": scan the rows and name the matching row + its value.
- For "which is highest/lowest/most/least": sort the rows by the relevant metric and name the top/bottom row
  with its exact value from the data.
- For "explain / summarize": describe the 2–3 most important numbers from the most recent turn.
- NULL/empty columns are "not recorded" — do NOT cite them as reasons for any value.
- If you genuinely cannot answer from the data, say so honestly and suggest a follow-up query.

FORMATTING:
- 2–4 sentences. Direct answer first, then the supporting numbers.
- CURRENCY: use the ₹ symbol (NEVER "Rs."). Write the FULL figure in Indian comma notation with ₹ in front
  (e.g. ₹17,57,91,339), and for large amounts add the crore value in brackets, e.g. ₹23,32,00,000 (₹23.32 Cr).
  NEVER give only the abbreviated "Cr"/"lakh" form without the full number.
- Do NOT mention SQL, queries, databases, or that this came from conversation history.
{lang_instr}

ANSWER:"""


# ── Natural Language Answer ───────────────────────────────────────────────────

def build_nl_answer_prompt(
    question: str, sql: str, results: list, row_count: int,
    language: str, context: list[ConversationTurn] = None,
) -> str:
    lang_name  = LANGS.get(language, "English")
    lang_instr = f"Respond in {lang_name}." if language != "en" else ""
    ctx        = _fmt_context(context or [], mode="compact", max_turns=3)

    # Cross-scheme honesty hint: if the SQL spans more than one scheme table, the answer must NOT
    # over-claim "across all three schemes" — CM Elevate has no EPIC and is excluded from any per-person
    # (EPIC-keyed) ranking. Detect the multi-table case from the SQL and inject a scope-honesty rule.
    _sql_l = (sql or "").lower()
    _tables_hit = sum(t in _sql_l for t in ('meghalaya_chatbot', 'focus_pg', 'cm_elevate'))
    _epic_keyed = ('epic id' in _sql_l) or ('member_epic_id' in _sql_l)
    cross_scheme_note = ""
    if _tables_hit >= 2:
        cross_scheme_note = (
            "\nCROSS-SCHEME SCOPE HONESTY (this query spans more than one scheme — be precise about scope):\n"
            "- Do NOT claim a recipient is \"across all three schemes\" unless the data row's own scheme count "
            "says so. If a 'schemes' column is present, state each recipient's ACTUAL scheme count; most are in "
            "only one or two schemes, so say that plainly rather than echoing the user's 'all 3 schemes' wording.\n"
        )
        if _epic_keyed:
            cross_scheme_note += (
                "- This per-person ranking is keyed on EPIC, which exists ONLY in Focus+ and Focus. CM Elevate has "
                "no EPIC, so it is EXCLUDED from this ranking. Frame the figure as \"combined disbursement across "
                "Focus+ and Focus\" (the two EPIC-bearing schemes), and add ONE sentence noting CM Elevate is not "
                "included here and can be compared only at the district level. Never imply CM Elevate is in the sum.\n"
            )
        # Venn / overlap / "all three" buckets: there is NO true 3-way EPIC overlap (CM Elevate has no EPIC).
        if any(k in _sql_l for k in ('intersect', 'except', 'only', '∩', 'bucket', 'overlap')) or \
           any(k in (question or '').lower() for k in ('venn', 'overlap', 'all three', 'all 3', 'only in', 'triple')):
            cross_scheme_note += (
                "- OVERLAP/VENN HONESTY: the real EPIC overlap is Focus+ ∩ Focus ONLY. CM Elevate has no EPIC, so "
                "there is NO genuine 3-way (Focus∩Focus+∩Elevate) person overlap — if the data shows CM Elevate it "
                "is a SEPARATE district-level count, not an EPIC-Venn bucket. NEVER describe a 'triple overlap' or "
                "'in all three schemes by EPIC' number; say the Venn is 2-scheme by EPIC plus Elevate by district.\n"
            )

    return f"""You are the CM Elevate Meghalaya government scheme disbursement assistant.
{ctx}The user asked: "{question}"
Query context: {sql[:200]}
Database returned {row_count} rows: {json.dumps(results[:50], default=str)}

CRITICAL GROUNDING RULES — READ BEFORE ANSWERING:
- Use ONLY numbers that appear verbatim in the data rows shown above. NEVER invent or estimate numbers.
- For ranking questions (highest/lowest/most/least/top): the row order from the SQL IS the answer.
  ORDER BY ... ASC → first row is LOWEST. ORDER BY ... DESC → first row is HIGHEST.
- If the data has only 1 row, that single row IS the direct answer — state it directly.
{cross_scheme_note}
DOMAIN FACTS (apply when relevant):
- This database tracks Meghalaya government scheme sanctions & disbursements (subsidy + loan tranches).
- loan_disbursed = 'disbursed' vs 'not disbursed' is the headline status; loans come via Bank or LIFCOM.
- desanctioned = '#ERROR!' means the sanction was withdrawn; refusedy_n = 'Y' means the application was refused.
- total_disbursement = total subsidy + total loan. A total_loan of 0 means subsidy-only so far.
- Amounts are in Rupees and can be large (lakhs/crores) — present them clearly.
- NULL columns mean "not recorded" — never describe a NULL as a problem, anomaly, or decline.
- YEAR-OVER-YEAR / TREND caveat: the data spans only 2024 and 2025, and 2025 is PARTIAL (far fewer records). When
  the result compares years (e.g. a concentration/HHI or count trend), state the direction BUT add that it is
  indicative only because 2025 is a partial year with far fewer records — do NOT present it as a firm conclusion.
- CONCENTRATION/HHI VOLUME SKEW: an HHI or top-district-share from a period (quarter/month) with very few
  applications is a statistical artifact, NOT real concentration — a quarter with 1 app shows HHI 1.0 / 100%
  trivially. NEVER headline the "highest concentration" as a low-volume period. Judge the trend ONLY from the
  high-volume periods, name the applications count alongside each figure, and call the series noisy / no clear
  trend if the swings are driven by tiny periods.

FORMATTING RULES:
- Lead with the direct answer or most important finding.
- Highlight highest/lowest values by name with exact numbers.
- If many rows, mention the top 2–3 AND the bottom 1–2 by name with exact numbers.
- CURRENCY: use the ₹ symbol (NEVER "Rs."); full Indian comma notation with ₹ in front, plus crore in brackets
  for large amounts, e.g. ₹23,32,00,000 (₹23.32 Cr). Never abbreviate-only.
- 2–4 sentences. No mention of SQL, queries, databases, or technical pipeline.
- If results span many districts/schemes/months, end with one sentence on the overall trend or
  the single most notable insight.
{lang_instr}

After the answer, on a NEW line, output a follow-up question the user would naturally ask NEXT,
prefixed exactly with "FOLLOWUP:". It MUST:
- be specific to THIS question and these results (reference a real district/scheme/value from the rows when useful),
- be a single short question (max ~12 words) that drills deeper or compares,
- be answerable from this same dataset (no external data).
Example: FOLLOWUP: Break the top scheme down by district?
If no sensible deeper question exists, output "FOLLOWUP: NONE".

Answer:"""
