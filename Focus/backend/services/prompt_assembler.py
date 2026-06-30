"""
Prompt Assembler — single source of truth for all LLM prompt construction
for the Focus Producer Group (PG) NL-to-SQL engine.

Prompt types:
  build_question_resolver_prompt  — resolve follow-up questions to standalone
  build_intent_prompt             — classify SQL vs REASON  (no RAG in this product)
  build_sql_static_prefix         — large cacheable schema + few-shots + rules
  build_sql_dynamic               — per-request follow-up context + question
  build_sql_prompt                — full SQL prompt (fallback when caching off)
  build_nl_answer_prompt          — human-readable NL answer from SQL results
  build_reason_prompt             — reason over prior data (no fetch)

Schema is VERIFIED against the LIVE Neon table "focus_pg" (221,088 rows).
⚠ The shipped FOCUS_NLP_to_SQL guide is IDEALISED and WRONG about the real table:
  • It describes TWO tables (focus_pg + focus_pg_members). REALITY: ONE flattened
    table "focus_pg" — member columns live inside it. There is NO focus_pg_members
    table and NO JOINs.
  • It claims typed INT/DECIMAL/DATE columns. REALITY: EVERY column is TEXT — all
    numbers and dates must be cast, with regex guards (the data is dirty).
  • PG-level rows carry a non-null "focus_pg_id" (37,354 distinct PGs). Member-only
    rows have focus_pg_id NULL. So PG-level metrics filter focus_pg_id IS NOT NULL;
    member-level metrics use COUNT(*) over all rows.
The rules below reflect REALITY, not the guide.
"""
import json
import re
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
  0. ⚠ CANONICAL DEFINITION OF "BENEFICIARY" (NEVER deviate — this is fixed to keep answers consistent):
       • A BENEFICIARY = a unique PG MEMBER, identified by member_unique_id.
       • "how many beneficiaries / total beneficiaries / unique beneficiaries / members / how many members" →
            COUNT(DISTINCT member_unique_id) FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> ''
            (member_unique_id is the true per-member id, format FP<pg_id><seq>, fully populated → ~197,442).
            Use this DISTINCT count for the plain "how many beneficiaries" too — a beneficiary is a unique person.
         ⚠ NEVER use COUNT(DISTINCT member_name) — names legitimately repeat (two real people share a name) so it
            UNDERCOUNTS; and NEVER use member_epic_id — it is mostly blank, so it grossly undercounts.
            member_unique_id is the ONLY correct beneficiary key.
       • Any beneficiary/member count carries the SAME scope filters (district/block/PG/bank) the user
         set — apply them in the WHERE clause; do not change the counting column because of a filter.
  1. The ONLY table is {TABLE}. There is NO "focus_pg_members" table and NO JOINs —
     member columns are flattened INTO {TABLE}. Never reference any other table or schema prefix.
  2. ⚠ EVERY COLUMN IS TEXT (even ids, amounts, ages, dates). Before ANY arithmetic,
     SUM, AVG, ROUND, numeric comparison, or date comparison you MUST cast — and the
     data is DIRTY, so guard with a regex first (see rules 5–7).
  3. ⚠ PG-LEVEL vs MEMBER-LEVEL — this is the most important rule:
       • The table is denormalised: PG-profile rows carry a non-null "focus_pg_id"
         (one row per Producer Group, 37,354 distinct). Member-only rows have
         focus_pg_id NULL.
       • PG-LEVEL metrics (number of PGs, PGs per district/block, disbursement,
         bookkeeper, bank, IVCS): filter "focus_pg_id" IS NOT NULL, and count PGs
         with COUNT(DISTINCT focus_pg_id) (NEVER COUNT(*) — that counts member rows).
       • MEMBER-LEVEL metrics (member counts, gender split, age, member bank): use
         COUNT(*) over rows where the member column is populated. Do NOT filter on
         focus_pg_id for member-level questions.
  4. ⚠ "EMPTY" IS A SPACE, NOT ''. Blank cells are stored as ' ' (one or more spaces),
     not as ''. Always test emptiness with TRIM: NULLIF(TRIM(col), '') IS NOT NULL,
     or TRIM(col) <> '' / TRIM(col) = ''.
  5. ⚠ NUMERIC CAST PATTERN (money, age, counts). Guard then cast:
        NULLIF(TRIM(col), '')::numeric                      -- only when col is known-clean
        CASE WHEN TRIM(col) ~ '^[0-9]+(\\.[0-9]+)?$' THEN TRIM(col)::numeric END  -- dirty cols
     Use the guarded CASE form for amounts and especially member_age (which contains
     garbage like '-28', '17000', '140').
  6. ⚠ AGE OUTLIERS: member_age has junk. Treat only 18–100 as valid for "average age",
     "youngest/oldest", etc.:  WHERE member_age ~ '^[0-9]+$' AND member_age::int BETWEEN 18 AND 100.
     For an explicit "age anomalies / outliers" question, do the opposite (<18 or >100).
  7. ⚠ DATE CAST PATTERN. Dates are TEXT like '2022-05-17 00:00:00' with some junk
     ('0003-07-22', '  '). Guard with a 4-digit-year regex before casting:
        WHERE finance_date ~ '^(19|20)\\d{{2}}-\\d{{2}}-\\d{{2}}'
              AND finance_date::timestamp >= '2021-01-01'
     Tranche-1 date = "finance_date"; tranche-2 date = "disburse_date_2".
  8. DISBURSEMENT (Focus seed capital is paid in tranches):
       • Tranche 1 amount = "finance_amount_disbursed";  Tranche 2 amount = "disburse_amount_2".
       • "received tranche 1 / any finance / disbursed" → finance_amount_disbursed is a
         valid positive number: TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$'
         AND TRIM(finance_amount_disbursed)::numeric > 0.
       • "awaiting / not disbursed / no finance" → that condition is false (NULL/blank/0).
       • Total disbursement per PG = tranche1 + tranche2 (cast each, COALESCE blanks to 0).
  9. BOOKKEEPER: "has_bookkeeper_been_identified" is 'Yes' / 'No' / blank (' '). A blank
     means NOT recorded (a data gap), NOT a "No". Only count 'Yes'/'No' explicitly.
  10. GENDER: "gender_id" is 'Male' / 'Female' / 'Other' / blank, plus a few stray numeric
      values (data spill). Restrict to gender_id IN ('Male','Female','Other') for any
      gender split or gender-filtered count.
  11. District / block / pg_name / village_id / bank_name are TEXT. Use exact '=' for the
      11 known district enum values; ILIKE '%term%' for partial name / place / bank search.
      ⚠ district_name is NULL/blank on ~182k member rows and also holds stray junk (a block name). For ANY
      district group/rank, restrict to the 11-name whitelist (see schema) — never GROUP BY raw district_name.
  11b. FUZZY / MIS-SPELLED / MIS-HEARD DISTRICT — resolve it, don't fail. Questions often come from voice or
      fast typing, so the district name is frequently garbled or phonetic. There are ONLY 11 valid districts
      (listed in the schema; note 'Ri-Bhoi' has a HYPHEN). Map the garbled input to the closest one by sound +
      spelling and filter on ITS exact canonical name with '='. NEVER filter on the raw misspelling, and do NOT
      return CANNOT_ANSWER just because the spelling was off.
        - Phonetic / speech-to-text:  "best guerrillas"/"west gorilla"/"west garo" → 'West Garo Hills';
          "east cause he"/"east kasi" → 'East Khasi Hills'; "re boy"/"ribhoi"/"ri bhoy" → 'Ri-Bhoi' (with hyphen).
        - Abbreviations: "EKH" → 'East Khasi Hills', "WGH" → 'West Garo Hills', "SWGH" → 'South West Garo Hills'.
        - Partial but specific: "west khasi" → 'West Khasi Hills', "south garo" → 'South Garo Hills', etc.
        - If two are equally plausible and you cannot disambiguate, pick the single most likely and proceed.
  12. "how many / count" → COUNT(...) with NO LIMIT.  Aggregates (SUM/AVG) → NO LIMIT.
  13. Listing rows → ALWAYS add LIMIT 50 unless the user explicitly asks for more or a count/total.
  13a. ⚠ LISTING + TOTAL: when the user asks "which / list / show me which …" (a list of matching
     rows, especially with a HAVING/filter like "only one beneficiary", "with no disbursement"),
     ALSO return the TRUE total number of matching rows as a "total_count" column using a window
     function: COUNT(*) OVER () AS total_count. This makes the real total (e.g. 312) available to the
     answer even though only 50 rows are shown. Add it as the LAST column. Do NOT use it for plain
     "how many" count questions (those already return a single COUNT).
  13b. ⚠ SUPERLATIVE = RANKED LIST, NOT one row. "fewest / lowest / least / smallest" and
     "most / highest / largest / top" over a SUBJECT (districts, blocks, villages, PGs) mean
     "rank them and show the ordered list" — treat "lowest" EXACTLY like "fewest", and "highest"
     like "most". Aggregate per subject, then ORDER BY the metric (ASC for fewest/lowest, DESC for
     most/highest) and LIMIT 10. Do NOT collapse to LIMIT 1 — the user wants the ranked list, not a
     single name. Only use LIMIT 1 / a single row when the user explicitly asks for "the single
     <lowest/highest> one" or "which ONE".
  14. Always ROUND money to 2 decimals: ROUND(SUM(...)::numeric, 2). Amounts are in Rupees.
  15. ⚠ ADVANCED ANALYTICS — build these compositionally; do NOT give up or hardcode. Postgres supports the full
      toolkit, so a "tricky" question is just CTEs + the right function. Reusable shapes:
       • MEDIAN / PERCENTILE: percentile_cont(0.5) WITHIN GROUP (ORDER BY metric). For Pn use 0.<n>.
       • "ABOVE/BELOW the median (or average)" of a per-group metric → 3 steps:
           (a) CTE: metric per group  (b) CTE: the median/avg of that metric  (c) main: CROSS JOIN the 1-row
           stat CTE and WHERE metric > / < it, ORDER BY metric DESC. (Same shape your reference query uses.)
       • RANKING / "top N per district", "rank", "Nth highest": window fns —
           ROW_NUMBER()/RANK()/DENSE_RANK() OVER (PARTITION BY grp ORDER BY metric DESC) in a CTE, filter rn<=N.
       • RUNNING TOTAL / share: SUM(x) OVER (ORDER BY ...) ; x::numeric/SUM(x) OVER () for share-of-total.
       • Quantiles/buckets: ntile(n) OVER (ORDER BY metric).
      ⚠ "members per PG" / "PG size" / "PGs with N members" GROUP BY pg_name, NOT focus_pg_id — focus_pg_id is one
        row per member (so it gives 1 member each); the real PG is its NAME and members share it across rows. Use
        member_count = COUNT(DISTINCT member_unique_id) per pg_name (+ district_name to keep same-named PGs apart).
        (Counting the NUMBER OF PGs is still COUNT(DISTINCT focus_pg_id) per rule 3 — only "members per PG" uses name.)
      Prefer returning the qualifying ROWS the user asked for; never refuse a question just because it needs a
      median/percentile/window function — those are all expressible here.
  16. NEVER invent or hardcode a count or amount. If a question cannot be answered from this
      single table, output exactly: CANNOT_ANSWER
"""


# ── Verified schema (focus_pg) ────────────────────────────────────────────────

SCHEMA = f"""
-- DATABASE: Neon PostgreSQL (standard SQL, no backticks)
-- SINGLE TABLE: {TABLE}  (221,088 rows — a DENORMALISED Producer-Group × member register)
-- Source: Meghalaya FOCUS scheme — Producer Group (PG) formation & finance.
-- The FOCUS scheme forms village Producer Groups, each nested under an IVCS
-- (Integrated Village Cooperative Society), and disburses seed capital in tranches.

-- ⚠ ROW MODEL (critical): PG-profile rows have a NON-NULL "focus_pg_id"
--   (37,354 distinct Producer Groups). Member-only rows have focus_pg_id NULL.
--   → PG-level questions  : WHERE focus_pg_id IS NOT NULL, COUNT(DISTINCT focus_pg_id)
--   → member-level questions: COUNT(*) over rows with the member field populated.

-- COLUMNS (ALL stored as TEXT — every number/date MUST be cast with a regex guard):
--   "focus_pg_id"                  TEXT  PG id; NON-NULL only on PG-profile rows (37,354 distinct PGs)
--   "pg_name"                      TEXT  Producer Group name (reused across villages → not unique)
--   "has_bookkeeper_been_identified" TEXT 'Yes' / 'No' / blank(' ' = not recorded)
--   "bank_account_no"              TEXT  PG bank account number
--   "bank_ifsc"                    TEXT  PG bank IFSC
--   "bank_branch"                  TEXT  PG bank branch
--   "bank_name"                    TEXT  ⚠ PG bank name — UNRELIABLE: on PG rows it is almost all numeric
--                                          garbage (codes like '8'). For real bank breakdowns use
--                                          member_bank_name instead (see below).
--   "ivcs_name"                    TEXT  IVCS this PG is linked to (blank if none)
--   "ivcs_account_no"              TEXT  IVCS account number
--   "block_name"                   TEXT  C&RD / administrative block
--   "district_name"                TEXT  district — POPULATED ON ONLY ~15,877 ROWS. It is NULL/blank on the
--                                          ~182k member-only rows (those rows have NO district, NO pg link).
--                                          So a district count = ONLY the rows that actually carry a district,
--                                          and you MUST restrict to the 11 valid district names (see whitelist
--                                          below) — the column also contains stray junk like a block name.
--   "village_id"                   TEXT  village name / id
--   "finance_amount_disbursed"     TEXT  ⚠ TRANCHE-1 amount (Rs) — guarded cast; blank/0 = not disbursed
--   "finance_date"                 TEXT  ⚠ tranche-1 date ('2022-05-17 00:00:00'; dirty — regex-guard)
--   "disburse_amount_2"            TEXT  ⚠ TRANCHE-2 amount (Rs) — guarded cast; almost always blank
--   "disburse_date_2"              TEXT  ⚠ tranche-2 date
--   "focus_scheme_pg_members_details_id" TEXT member record id (note the real spelling)
--   "member_name"                  TEXT  member full name
--   "member_epic_id"               TEXT  member Voter ID / EPIC number
--   "member_age"                   TEXT  ⚠ member age — DIRTY (has '-28','0','17000','140'); valid range 18–100
--   "member_unique_id"             TEXT  FOCUS member unique id (format FP<pg_id><seq>)
--   "gender_id"                    TEXT  'Male' / 'Female' / 'Other' / blank (+ a few stray numbers)
--   "member_bank_name"             TEXT  member personal bank name — THE reliable bank field; real values
--                                          like 'MEGHALAYA RURAL BANK','STATE BANK OF INDIA','SBI','MRB',
--                                          'MEGHALAYA CO-OPERATIVE APEX BANK','MCAB' (use for any bank split)
--   "member_bank_account_no"       TEXT  member personal bank account
--   "member_bank_ifsc_code"        TEXT  member personal bank IFSC
--   "member_bank_branch"           TEXT  member personal bank branch
--   "focus_pg_members_count"       TEXT  always '1' — a per-member count flag

-- REFERENCE VALUES
-- Districts — there are EXACTLY 11 valid district names (note the HYPHEN in 'Ri-Bhoi'):
--   East Khasi Hills, West Khasi Hills, South West Khasi Hills,
--   East Garo Hills, West Garo Hills, North Garo Hills, South Garo Hills, South West Garo Hills,
--   East Jaintia Hills, West Jaintia Hills, Ri-Bhoi
--   ⚠ district_name ALSO contains NULL, blank ' ', and stray junk (e.g. a block name
--     'Khatarshnong Laitkroh C & RD Block'). For ANY district count/group/filter you MUST restrict to the
--     whitelist above, i.e. add:  district_name IN (<the 11 names>)  — this drops NULL, blank, and junk.
--   ⚠ district is recorded on only ~15,877 rows; the other ~182k members have NO district. A "members per
--     district" answer therefore covers ONLY members whose district is recorded — say so in the answer.
--   (a blank district ' ' also exists = "district not recorded" — exclude unless asked.)
--   ⚠ These are the ONLY valid districts. The user's spelling/voice input is often WRONG — map it to the
--     closest of these BEFORE filtering (see rule 11b), e.g. "best guerrillas" → 'West Garo Hills'.
-- Bookkeeper: 'Yes', 'No' (blank = not recorded).   Gender: 'Male', 'Female', 'Other'.
-- Banks (examples): 'Meghalaya Rural Bank', 'State Bank of India', 'Meghalaya Co-operative Apex Bank'.
-- Tranche-1 amount column = finance_amount_disbursed; tranche-2 = disburse_amount_2.
"""


# ── Few-shot examples (every one VERIFIED to run against the live table) ──────

SHOTS = f"""-- PATTERN: total number of Producer Groups (PG-level → DISTINCT id). Count only REAL numeric PG ids.
-- focus_pg_id is dirty TEXT: besides the real numeric ids it holds junk values ('Grand Total', 'MRB', a blank
-- space) — "IS NOT NULL" would count those and report 37,354. The TRUE unique PG count is 37,351, so filter to
-- numeric ids with ~ '^[0-9]+$'.
Q: how many producer groups are registered under focus
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS total_pgs FROM {TABLE} WHERE focus_pg_id ~ '^[0-9]+$';

-- PATTERN: distinct PG names (names are reused → fewer than total PGs)
Q: how many distinct PG names exist
SQL: SELECT COUNT(DISTINCT pg_name) AS distinct_pg_names FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(COALESCE(pg_name,'')) <> '';

-- PATTERN: districts / blocks covered (PG-level)
Q: how many districts have focus PGs
SQL: SELECT COUNT(DISTINCT district_name) AS districts FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(COALESCE(district_name,'')) <> '';

-- PATTERN: PGs per district (PG-level; exclude blank district)
Q: how many PGs are there per district
SQL: SELECT district_name, COUNT(DISTINCT focus_pg_id) AS pgs FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(COALESCE(district_name,'')) <> '' GROUP BY district_name ORDER BY pgs DESC;

-- PATTERN: PGs in one district
Q: how many focus PGs in West Jaintia Hills
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS pgs FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND district_name = 'West Jaintia Hills';

-- PATTERN: MISSPELLED / VOICE district — map to the closest of the 11 real districts, then filter on canonical name
Q: how many beneficiaries in best guerrillas
SQL: SELECT COUNT(*) AS total_beneficiaries, COUNT(DISTINCT member_unique_id) AS distinct_beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND district_name = 'West Garo Hills';

-- PATTERN: garbled "Ri-Bhoi" from speech (keep the HYPHEN in the canonical value)
Q: beneficiaries in re boy
SQL: SELECT COUNT(*) AS total_beneficiaries, COUNT(DISTINCT member_unique_id) AS distinct_beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND district_name = 'Ri-Bhoi';

-- PATTERN: top blocks by PG count
Q: top 10 blocks by focus PG count
SQL: SELECT block_name, COUNT(DISTINCT focus_pg_id) AS pgs FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(COALESCE(block_name,'')) <> '' GROUP BY block_name ORDER BY pgs DESC LIMIT 10;

-- PATTERN: "fewest/lowest <subject> by <metric>" → RANKED LIST ascending (NOT a single row).
-- "lowest" is treated identically to "fewest": both return the ordered list, not one district.
Q: which districts have the fewest beneficiaries
SQL: SELECT district_name, COUNT(*) AS beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND district_name IN ('East Khasi Hills','West Khasi Hills','South West Khasi Hills','East Garo Hills','West Garo Hills','North Garo Hills','South Garo Hills','South West Garo Hills','East Jaintia Hills','West Jaintia Hills','Ri-Bhoi') GROUP BY district_name ORDER BY beneficiaries ASC LIMIT 10;

Q: which districts have the lowest beneficiaries
SQL: SELECT district_name, COUNT(*) AS beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND district_name IN ('East Khasi Hills','West Khasi Hills','South West Khasi Hills','East Garo Hills','West Garo Hills','North Garo Hills','South Garo Hills','South West Garo Hills','East Jaintia Hills','West Jaintia Hills','Ri-Bhoi') GROUP BY district_name ORDER BY beneficiaries ASC LIMIT 10;

-- PATTERN: top N villages by beneficiaries (member-level count per village, ranked DESC)
Q: top 5 villages by beneficiaries
SQL: SELECT village_id AS village, COUNT(*) AS beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND TRIM(COALESCE(village_id,'')) <> '' GROUP BY village_id ORDER BY beneficiaries DESC LIMIT 5;

-- PATTERN: total tranche-1 disbursement (PG-level money; guarded cast)
Q: total focus disbursement in tranche 1
SQL: SELECT COUNT(*) AS pgs_with_tranche1, ROUND(SUM(TRIM(finance_amount_disbursed)::numeric), 2) AS total_disbursed FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(finance_amount_disbursed)::numeric > 0;

-- PATTERN: how many PGs received tranche 2
Q: how many PGs have received tranche 2
SQL: SELECT COUNT(*) AS pgs_with_tranche2, ROUND(SUM(TRIM(disburse_amount_2)::numeric), 2) AS total_tranche2 FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(disburse_amount_2) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(disburse_amount_2)::numeric > 0;

-- PATTERN: % of PGs with any disbursement
Q: what percent of PGs have received any disbursement
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS total_pgs, COUNT(DISTINCT focus_pg_id) FILTER (WHERE TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(finance_amount_disbursed)::numeric > 0) AS disbursed_pgs, ROUND(100.0 * COUNT(DISTINCT focus_pg_id) FILTER (WHERE TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(finance_amount_disbursed)::numeric > 0) / COUNT(DISTINCT focus_pg_id), 1) AS pct_disbursed FROM {TABLE} WHERE focus_pg_id IS NOT NULL;

-- PATTERN: district-wise tranche-1 disbursement
Q: district-wise tranche 1 disbursement
SQL: SELECT district_name, COUNT(*) FILTER (WHERE TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(finance_amount_disbursed)::numeric > 0) AS disbursed_pgs, ROUND(SUM(CASE WHEN TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' THEN TRIM(finance_amount_disbursed)::numeric ELSE 0 END), 2) AS total_disbursed FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(COALESCE(district_name,'')) <> '' GROUP BY district_name ORDER BY total_disbursed DESC;

-- PATTERN: average tranche-1 amount per PG that got it
Q: average tranche 1 amount per PG
SQL: SELECT ROUND(AVG(TRIM(finance_amount_disbursed)::numeric), 2) AS avg_tranche1 FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(finance_amount_disbursed)::numeric > 0;

-- PATTERN: top PGs by tranche-1 amount
Q: top 10 PGs by tranche 1 disbursement
SQL: SELECT pg_name, district_name, ROUND(TRIM(finance_amount_disbursed)::numeric, 2) AS tranche1 FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(finance_amount_disbursed) ~ '^[0-9]+(\\.[0-9]+)?$' ORDER BY TRIM(finance_amount_disbursed)::numeric DESC LIMIT 10;

-- PATTERN: PGs awaiting disbursement
Q: which PGs have not received any finance yet
SQL: SELECT focus_pg_id, pg_name, district_name, block_name FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND NOT (TRIM(COALESCE(finance_amount_disbursed,'')) ~ '^[0-9]+(\\.[0-9]+)?$' AND TRIM(finance_amount_disbursed)::numeric > 0) ORDER BY district_name, pg_name LIMIT 50;

-- PATTERN: bookkeeper breakdown (explicit Yes/No only; blank = not recorded)
Q: how many PGs have identified a bookkeeper
SQL: SELECT has_bookkeeper_been_identified AS bookkeeper, COUNT(DISTINCT focus_pg_id) AS pgs FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND has_bookkeeper_been_identified IN ('Yes','No') GROUP BY has_bookkeeper_been_identified ORDER BY pgs DESC;

-- PATTERN: gender split of members (member-level; restrict to valid enums)
Q: gender split of focus PG members
SQL: SELECT gender_id, COUNT(*) AS members FROM {TABLE} WHERE gender_id IN ('Male','Female','Other') GROUP BY gender_id ORDER BY members DESC;

-- PATTERN: total members/beneficiaries (Rule 0 — beneficiary = UNIQUE member → COUNT(DISTINCT member_unique_id))
Q: how many PG members are registered overall
SQL: SELECT COUNT(DISTINCT member_unique_id) AS total_members FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '';

-- PATTERN: PGs whose member count is ABOVE the state median (CTE → median → CROSS JOIN, return the qualifying rows).
-- This is the GENERAL "rows above the median of a per-group metric" shape: 1) a CTE computes the metric per group,
-- 2) a median CTE with percentile_cont(0.5) WITHIN GROUP (ORDER BY metric), 3) CROSS JOIN + WHERE metric > median,
-- ORDER BY metric DESC. ⚠ KEY CHOICE: group a PG by its NAME (pg_name), NOT focus_pg_id — focus_pg_id is ~1 row per
-- member (one id per row), so grouping by it gives 1 member each; pg_name is the real PG and its members share the
-- name across rows. member_count = COUNT(DISTINCT member_unique_id). This returns the real ranked list (max ~41).
Q: PGs with member count above state median
SQL: WITH pg_member_count AS (SELECT pg_name, district_name, COUNT(DISTINCT member_unique_id) AS member_count FROM {TABLE} WHERE NULLIF(TRIM(pg_name),'') <> '' GROUP BY pg_name, district_name), state_median AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY member_count) AS median_members FROM pg_member_count) SELECT p.pg_name, p.district_name, p.member_count FROM pg_member_count p CROSS JOIN state_median m WHERE p.member_count > m.median_members ORDER BY p.member_count DESC LIMIT 100;

-- PATTERN: total beneficiaries (Rule 0 — beneficiary = UNIQUE person → COUNT(DISTINCT member_unique_id))
Q: total number of beneficiaries
SQL: SELECT COUNT(DISTINCT member_unique_id) AS total_beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '';

-- PATTERN: distinct beneficiaries (Rule 0 — distinct key is member_unique_id ONLY; NOT name, NOT epic)
Q: total number of distinct beneficiaries
SQL: SELECT COUNT(DISTINCT member_unique_id) AS distinct_beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND TRIM(COALESCE(member_unique_id,'')) <> '';

-- PATTERN: total + distinct beneficiaries TOGETHER in one district (only members whose district is recorded)
Q: total beneficiaries and distinct beneficiaries in East Khasi Hills
SQL: SELECT COUNT(*) AS total_beneficiaries, COUNT(DISTINCT member_unique_id) AS distinct_beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND district_name = 'East Khasi Hills';

-- PATTERN: beneficiaries per district — restrict to the 11 valid names (drops NULL/blank/junk districts)
Q: district wise beneficiary count
SQL: SELECT district_name, COUNT(*) AS beneficiaries FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND district_name IN ('East Khasi Hills','West Khasi Hills','South West Khasi Hills','East Garo Hills','West Garo Hills','North Garo Hills','South Garo Hills','South West Garo Hills','East Jaintia Hills','West Jaintia Hills','Ri-Bhoi') GROUP BY district_name ORDER BY beneficiaries DESC;

-- PATTERN: members below an age (age is dirty → guard + valid range)
Q: list members below the age of 25
SQL: SELECT member_name, member_age, gender_id, pg_name, district_name FROM {TABLE} WHERE member_age ~ '^[0-9]+$' AND member_age::int BETWEEN 18 AND 100 AND member_age::int < 25 ORDER BY member_age::int ASC LIMIT 50;

-- PATTERN: average member age (exclude age outliers)
Q: what is the average member age
SQL: SELECT ROUND(AVG(member_age::numeric), 1) AS avg_age, COUNT(*) AS members_counted FROM {TABLE} WHERE member_age ~ '^[0-9]+$' AND member_age::int BETWEEN 18 AND 100;

-- PATTERN: member lookup by name
Q: find the member named Kelias Biam
SQL: SELECT member_name, member_unique_id, gender_id, member_age, pg_name, district_name FROM {TABLE} WHERE member_name ILIKE '%Kelias Biam%' LIMIT 50;

-- PATTERN: member lookup by EPIC id
Q: which PG does EPIC ABC1234567 belong to
SQL: SELECT member_name, member_epic_id, pg_name, district_name, block_name FROM {TABLE} WHERE member_epic_id ILIKE '%ABC1234567%' LIMIT 50;

-- PATTERN: bank-wise split (real bank names live in member_bank_name, NOT the PG bank_name column)
Q: bank-wise split of focus PGs
SQL: SELECT member_bank_name AS bank, COUNT(*) AS members FROM {TABLE} WHERE member_bank_name ~ '[A-Za-z]' AND TRIM(member_bank_name) NOT IN ('INFORMATION AWAITED','Soon to be submitted') GROUP BY member_bank_name ORDER BY members DESC LIMIT 50;

-- PATTERN: PGs linked to an IVCS
Q: how many PGs are linked to an IVCS
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS pgs_with_ivcs FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND TRIM(COALESCE(ivcs_name,'')) <> '';

-- PATTERN: members per PG (member count grouped by PG)
Q: top 10 PGs by member count
SQL: SELECT pg_name, COUNT(*) AS members FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND TRIM(COALESCE(pg_name,'')) <> '' GROUP BY pg_name ORDER BY members DESC LIMIT 10;

-- PATTERN: members of a specific bank
Q: how many members bank with State Bank of India
SQL: SELECT COUNT(*) AS sbi_members FROM {TABLE} WHERE member_bank_name ILIKE '%State Bank of India%';

-- PATTERN: list PGs in a district
Q: show all producer groups in West Khasi Hills
SQL: SELECT DISTINCT focus_pg_id, pg_name, block_name, village_id FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND district_name = 'West Khasi Hills' ORDER BY pg_name LIMIT 50;

-- PATTERN: "which … with only N" — list the matching rows AND the TRUE total (window count)
-- so the answer can state "There are 312 villages" even though only 50 rows are shown.
-- Shape: aggregate in a CTE, filter with HAVING, then COUNT(*) OVER () the result set.
Q: which villages have only one beneficiary
SQL: WITH per_village AS (SELECT village_id, COUNT(*) AS beneficiary_count FROM {TABLE} WHERE TRIM(COALESCE(member_name,'')) <> '' AND TRIM(COALESCE(village_id,'')) <> '' GROUP BY village_id HAVING COUNT(*) = 1) SELECT village_id AS village, beneficiary_count, COUNT(*) OVER () AS total_count FROM per_village ORDER BY village_id LIMIT 50;

-- PATTERN: data-quality — PGs missing bookkeeper info (blank, not 'No')
Q: how many PGs are missing bookkeeper information
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS missing_bookkeeper FROM {TABLE} WHERE focus_pg_id IS NOT NULL AND (has_bookkeeper_been_identified IS NULL OR TRIM(has_bookkeeper_been_identified) = '');

-- PATTERN: age anomalies (the inverse of the valid-range guard)
Q: how many member age records are anomalies
SQL: SELECT COUNT(*) AS age_outliers FROM {TABLE} WHERE member_age ~ '^-?[0-9]+$' AND (member_age::int < 18 OR member_age::int > 100);
"""


# ── SQL prompt RULES (static — part of the cacheable prefix) ──────────────────

_SQL_RULES = f"""RULES:
- Output ONLY a valid PostgreSQL SELECT or WITH statement. No markdown, no backticks, no explanation.
- TABLE SCOPE IS DECIDED FROM INTENT: first run STEP 0 of the CROSS-SCHEME RULES to decide whether the answer
  needs one scheme or several, then pick the SMALLEST set of tables. Default to THIS backend's home table {TABLE}
  (Focus PG) — there is NO focus_pg_members table, so single-scheme Focus questions never JOIN. Use the OTHER
  scheme tables ({FOCUS_PLUS_TBL} Focus+, {ELEVATE_TBL} CM Elevate) only when the intent truly spans schemes.
  Never invent any table or column not listed in the schema or cross-scheme rules.
- EVERY column is TEXT. Cast with a regex guard before arithmetic/comparison:
    amounts: TRIM(col) ~ '^[0-9]+(\\.[0-9]+)?$' then TRIM(col)::numeric
    age    : member_age ~ '^[0-9]+$' AND member_age::int BETWEEN 18 AND 100
    dates  : col ~ '^(19|20)\\d{{2}}-\\d{{2}}-\\d{{2}}' then col::timestamp
- Blank cells are a SPACE, not ''. Test emptiness with TRIM(col) = '' / TRIM(col) <> ''.
- PG-level (count of PGs, per-district/block, disbursement, bookkeeper, bank, IVCS):
    WHERE focus_pg_id IS NOT NULL and COUNT(DISTINCT focus_pg_id). NEVER COUNT(*) for PG counts.
- Member-level (member counts, gender, age, member bank): COUNT(*) over rows where the
    member column is populated; do NOT filter focus_pg_id.
- Disbursement: tranche 1 = finance_amount_disbursed, tranche 2 = disburse_amount_2.
    "disbursed/received finance" = that amount is a number > 0; "awaiting" = it is blank/NULL/0.
- Bookkeeper: only 'Yes'/'No' are explicit; blank = not recorded (never treat blank as 'No').
- Gender: restrict to gender_id IN ('Male','Female','Other') (ignore stray numeric values).
- District/block/pg_name/bank: exact '=' for the 12 known districts (note 'Ri-Bhoi' has a hyphen);
    ILIKE '%term%' for partial name/place/bank search. Exclude blank district ' ' unless asked.
- FUZZY DISTRICTS: the input district is often misspelled / mis-heard (voice). There are ONLY 11 valid districts.
    Map the garbled input to the closest canonical district by sound + spelling and filter on THAT exact name with
    '=' (e.g. "best guerrillas"→'West Garo Hills', "east cause he"→'East Khasi Hills', "re boy"→'Ri-Bhoi',
    "EKH"→'East Khasi Hills'). Never filter on the raw misspelling; never CANNOT_ANSWER for a bad district spelling.
- ⚠ DISTRICT DATA IS SPARSE + DIRTY. district_name is NULL on the ~182k member-only rows and also holds blank
    and junk (a block name). So:
    • For ANY group/rank BY district, restrict to the 11 valid names:
        WHERE district_name IN ('East Khasi Hills','West Khasi Hills','South West Khasi Hills','East Garo Hills',
        'West Garo Hills','North Garo Hills','South Garo Hills','South West Garo Hills','East Jaintia Hills',
        'West Jaintia Hills','Ri-Bhoi')
      This automatically drops NULL, blank ' ', and the stray block-name value — never GROUP BY raw district_name.
    • "members/beneficiaries in <district>" = COUNT over rows with member_name populated AND
      district_name = '<that one valid district>'. This counts ONLY members whose district is recorded
      (~15,877 of ~197k members carry a district); the rest have no district in the data and CANNOT be attributed.
      Do NOT try to derive a member's district from member_unique_id or any join — there is no reliable PG link.
- BANK SPLIT: the PG "bank_name" column is unreliable (mostly numeric garbage). For any "bank-wise"
    or "which bank" question, use "member_bank_name" (member-level) with AND member_bank_name ~ '[A-Za-z]'.
- Generic region words: "Garo Hills" → the 5 Garo districts; "Khasi Hills" → the 3 Khasi districts;
    "Jaintia Hills" → ('East Jaintia Hills','West Jaintia Hills').
- ROUND all money to 2 decimals: ROUND(SUM(...)::numeric, 2).  Order financial results by amount DESC.
- Count/total questions → COUNT(...)/SUM(...) with NO LIMIT.
- Listing rows → ALWAYS add LIMIT 50 (unless the user explicitly asks for all or for a count).
- Always alias columns with readable names (AS total_pgs, AS members, AS total_disbursed, etc.).
- For a cross-scheme ask that requires an EPIC or bank account on CM Elevate (it has NEITHER — see CROSS-SCHEME
  RULE CS-3), answer the possible part and state the limit, or output CANNOT_ANSWER.
- If the question genuinely cannot be answered from any scheme table, output exactly: CANNOT_ANSWER"""


def build_sql_static_prefix() -> str:
    """
    The large, byte-for-byte identical portion of the SQL prompt
    (schema + counts guard + few-shots + rules). Cached server-side via
    Gemini context caching (see ai_service.ai_call's cache_prefix param).
    """
    return (
        f"{SCHEMA}\n\n{COUNTS_GUARD}\n\n{cross_scheme_rules('Focus')}\n\n"
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
                         "block, bank, IVCS, disbursed/awaiting, or gender scope — from above.\n")
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
    'and pending', 'and disbursed', 'and awaiting',
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
    'which district', 'which block', 'which pg', 'which group', 'which bank', 'which ivcs',
    'which one', 'which has', 'which had', 'which is',
    ' lowest', ' highest', ' most ', ' least ',
    'the lowest', 'the highest', 'the most', 'the least',
])

_TOPIC_TERMS = {
    'pg', 'pgs', 'producer group', 'group', 'member', 'members', 'people',
    'district', 'block', 'village', 'meghalaya', 'garo', 'khasi', 'jaintia', 'ri-bhoi', 'ri bhoi',
    'bookkeeper', 'ivcs', 'bank', 'account', 'epic', 'gender', 'male', 'female', 'age',
    'finance', 'disbursed', 'disbursement', 'tranche', 'amount', 'awaiting', 'seed capital',
    'name', 'coverage', 'rate', 'percent', 'percentage', 'count', 'total',
}


def _norm(text: str) -> str:
    return f" {text.lower().strip()} "


# Subject nouns (what is being ranked) and metric nouns (the quantity being ranked by).
# A question that names BOTH is a self-contained ranked-LIST request — e.g. "which districts
# have the lowest beneficiaries" — NOT a bare reflective follow-up like "which is the lowest?".
_SUBJECT_NOUNS = frozenset([
    'district', 'districts', 'block', 'blocks', 'village', 'villages',
    'pg', 'pgs', 'producer group', 'producer groups', 'group', 'groups',
    'member', 'members', 'beneficiary', 'beneficiaries', 'bank', 'banks', 'ivcs',
])
_METRIC_NOUNS = frozenset([
    'beneficiary', 'beneficiaries', 'member', 'members', 'people', 'pg', 'pgs',
    'count', 'counts', 'number', 'amount', 'disbursement', 'disbursed', 'coverage',
    'finance', 'tranche', 'rate', 'percentage', 'percent', 'age',
])
# Bare superlatives that, ON THEIR OWN, signal a reflective follow-up ("which is the lowest?").
# These must NOT force the REASON/single-answer path when the question is actually a full
# "which <subject> have the lowest <metric>" LIST request.
_SUPERLATIVE_REFLECTIVE = frozenset([
    ' lowest', ' highest', ' most ', ' least ',
    'the lowest', 'the highest', 'the most', 'the least',
    'biggest', 'smallest', 'top one', 'bottom one',
])


def _is_self_contained_ranking(q: str) -> bool:
    """
    True when the question names BOTH a subject to rank AND a metric to rank by, e.g.
    "which districts have the lowest beneficiaries" / "blocks with the fewest members".
    Such a question is a fresh ranked-LIST query (SQL), even though it contains a superlative
    like "lowest"/"most" — it should NOT be treated as a bare reflective follow-up over prior data.
    """
    # Normalise punctuation to spaces so a trailing "?" / "," doesn't hide a word
    # ("beneficiaries?" must still match the metric noun "beneficiaries").
    qp = " " + re.sub(r"[^a-z0-9]+", " ", q).strip() + " "
    has_subject = any(f" {n} " in qp for n in _SUBJECT_NOUNS)
    has_metric  = any(f" {n} " in qp for n in _METRIC_NOUNS)
    # "have/with the lowest|fewest|most ..." phrasing is the give-away for a ranked list.
    asks_ranked_list = any(w in q for w in (
        'fewest', 'lowest', 'highest', 'most', 'least', 'fewer', 'lower', 'higher',
        'smallest', 'largest', 'top ', 'bottom ',
    ))
    return has_subject and has_metric and asks_ranked_list


def _reflective_match(q: str) -> bool:
    """
    _REFLECTIVE match, but bare superlatives ('lowest'/'highest'/'most'/'least') do NOT count
    when the question is a self-contained ranked-list request ("which districts have the lowest
    beneficiaries"). This keeps "which is the lowest?" → REASON while "which districts have the
    lowest beneficiaries" → SQL list.
    """
    if _is_self_contained_ranking(q):
        # Only the explicitly reflective phrases still count (e.g. "which one", "difference between");
        # the bare superlatives are intentionally ignored for self-contained ranking questions.
        non_superlative = _REFLECTIVE - _SUPERLATIVE_REFLECTIVE
        return any(sig in q for sig in non_superlative)
    return any(sig in q for sig in _REFLECTIVE)


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
    if _reflective_match(q):                   return True

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

    # A self-contained ranked-list question ("which districts have the lowest beneficiaries")
    # is a fresh SQL list, NOT reasoning over prior rows — never route it to REASON.
    if _is_self_contained_ranking(q):
        return False

    if _reflective_match(q):
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
Focus Producer Group (PG) assistant (Meghalaya FOCUS scheme — PG formation & finance).

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
2. Carry forward filters (district, block, bank, bookkeeper, disbursed/awaiting, gender) UNLESS the user changes them.
3. If the user introduces a brand-new topic not in history, do NOT bolt prior filters on.
4. If the message is already standalone, return it unchanged.
5. Output ONLY the rewritten question. No quotes, no prefix, no explanation.

EXAMPLES:
History: "PGs per district" answered with per-district counts.
"what about just the disbursed ones?"  →  "How many PGs per district have received tranche-1 disbursement?"

History: shows district-wise PG counts.
"which is highest?"  →  "Which district has the highest number of Focus PGs in the data shown?"

History: East Khasi Hills PG summary.
"what about West Garo Hills?"  →  "Show the Producer Group summary for West Garo Hills."

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

SQL    = run a fresh query against the LIVE Focus Producer Group table for numbers, counts, lists, breakdowns, lookups.
REASON = answer using ONLY the prior conversation data shown above. No new fetch. Use this when the user is
         asking to reason ABOUT what was already shown — "why", "explain", "summarize", "which one is highest
         among these", "what does that mean".

DECISION ORDER:
1. Reasoning verbs (why / explain / summarize / interpret) over prior data → REASON.
2. Reflective comparison ("which is the highest?", "biggest one?") referring to data already shown → REASON.
3. Anything asking for fresh numbers, lists, lookups, breakdowns, totals → SQL.
4. When in doubt between SQL and REASON, prefer SQL (fresh data is safer than stale).

EXAMPLES:
"How many producer groups are there?"              → SQL
"PGs per district"                                 → SQL
"Find the member named Kelias Biam"                → SQL
"Total tranche-1 disbursement"                     → SQL
(after a district breakdown) "which is highest?"   → REASON
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

    return f"""You are the Focus Producer Group (PG) assistant for the Meghalaya FOCUS scheme.
The user is asking a follow-up that should be answered by REASONING over the data already shown in this
conversation. Do NOT invent new numbers. Do NOT pretend to query a database. Use only the rows below.

{primary_data_block}
FULL CONVERSATION HISTORY:
{ctx}
USER'S QUESTION: "{question}"

HOW TO ANSWER:
- Start with the PRIMARY DATA above — these are the actual rows from the most recent query.
- For "which district/block/PG/bank": scan the rows and name the matching row + its value.
- For a SINGULAR superlative ("which district/block/PG has the highest/most/lowest/fewest …"): name EXACTLY
  ONE — the single top (or bottom) row by the relevant metric — with its exact value, and STOP. Do NOT list
  the runners-up or turn it into a ranking. The user asked for one answer; give one. (Only list several if the
  user explicitly said "top N", "rank", "list", or "which ones".)
- For "explain / summarize": describe the 2–3 most important numbers from the most recent turn.
- If the rows carry a "total_count" column, that is the TRUE total number of matching rows (the list
  may be capped at 50). When the question is "which/how many … match", OPEN with that total
  (e.g. "There are 312 villages with exactly one beneficiary.") — use total_count, not the number of
  rows visible. Never present total_count as a per-row value.
- NULL/empty columns are "not recorded" — do NOT cite them as reasons for any value.
- If you genuinely cannot answer from the data, say so honestly and suggest a follow-up query.

FORMATTING:
- 2–4 sentences. Direct answer first, then the supporting numbers.
- CURRENCY: use the ₹ symbol (NEVER "Rs."). Write the FULL figure in Indian comma notation with ₹ in front
  and add the crore value in brackets, e.g. ₹47,73,73,000 (₹47.73 Cr). NEVER give only the abbreviated
  "Cr"/"lakh" form without the full number.
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

    return f"""You are the Focus Producer Group (PG) assistant for the Meghalaya FOCUS scheme.
{ctx}The user asked: "{question}"
Query context: {sql[:200]}
Database returned {row_count} rows: {json.dumps(results[:50], default=str)}

CRITICAL GROUNDING RULES — READ BEFORE ANSWERING:
- Use ONLY numbers that appear verbatim in the data rows shown above. NEVER invent or estimate numbers.
- ⚠ EACH LABEL STAYS BOUND TO ITS OWN VALUE. Every name (village/district/block/PG/bank) must be paired
  with the EXACT number from the SAME row — never mix a name from one row with a value from another, and
  never reorder. Read each row as a unit: row = {{label → value}}. If unsure, quote fewer rows rather
  than risk a wrong pairing. The text must agree with the table exactly.
- For ranking questions (highest/lowest/most/least/top): the row order from the SQL IS the answer, in the
  order given. ORDER BY ... ASC → first row is LOWEST. ORDER BY ... DESC → first row is HIGHEST.
- ⚠ SHORT EXPLICIT TOP-N (e.g. "top 5 villages", ≤10 rows): walk the rows IN ORDER, listing each
  name with its exact value (e.g. "BHANGARPAR (311), then BOIRAGIPARA (283), …"). Do NOT split into
  "top few and bottom few" and do NOT summarise — the user asked for the ranked list, so reflect every
  row shown, in order, faithfully.
- If the data has only 1 row, that single row IS the direct answer — state it directly.
{cross_scheme_note}
DOMAIN FACTS (apply when relevant):
- FOCUS forms village Producer Groups (PGs), each linked to an IVCS, and disburses seed capital in tranches.
- Tranche 1 (finance_amount_disbursed) is the initial seed capital; tranche 2 (disburse_amount_2) is the
  follow-on, contingent on a bookkeeper being identified. Very few PGs have reached tranche 2.
- A bookkeeper "blank" value means it was NOT recorded — that is a data gap, not a "No".
- Amounts are in Rupees and can be large (lakhs/crores) — present them clearly.
- NULL/blank columns mean "not recorded" — never describe a blank as a problem, anomaly, or decline.

FORMATTING RULES:
- Lead with the direct answer or most important finding.
- ⚠ If the rows contain a "total_count" column, that value is the TRUE total number of matching
  rows (the list may be capped at 50). OPEN the answer by stating that total so officers see the
  full count — e.g. "There are 312 villages with exactly one beneficiary." Then list/illustrate the
  rows shown. Use the total_count value, NOT the number of rows you can see, for the total. Do NOT
  print "total_count" as if it were a per-row value.
- Highlight highest/lowest values by name with exact numbers.
- For a SHORT explicit Top-N (≤10 rows), list every row IN ORDER with its exact value (see grounding rule above).
- Only for LARGE result sets (many rows, not an explicit small Top-N) may you summarise by mentioning the
  top 2–3 AND the bottom 1–2 by name with exact numbers instead of every row.
- CURRENCY: use the ₹ symbol (NEVER "Rs."); full Indian comma notation with ₹ in front, plus crore in brackets,
  e.g. ₹47,73,73,000 (₹47.73 Cr). Never abbreviate-only.
- 2–4 sentences. No mention of SQL, queries, databases, or technical pipeline.
- If results span many districts/blocks, end with one sentence on the overall trend or the single most notable insight.
{lang_instr}

After the answer, on a NEW line, output a follow-up question the user would naturally ask NEXT,
prefixed exactly with "FOLLOWUP:". It MUST:
- be specific to THIS question and these results (reference a real district/block/value from the rows when useful),
- be a single short question (max ~12 words) that drills deeper or compares,
- be answerable from this same dataset (no external data).
Example: FOLLOWUP: Drill into West Garo Hills to see block-level beneficiaries?
If no sensible deeper question exists, output "FOLLOWUP: NONE".

Answer:"""
