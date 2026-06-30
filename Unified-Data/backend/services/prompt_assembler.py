"""
Prompt Assembler — single source of truth for all LLM prompt construction
for the Unified Data (Meghalaya Focus Plus farmer payment) NL-to-SQL engine.

Prompt types:
  build_question_resolver_prompt  — resolve follow-up questions to standalone
  build_intent_prompt             — classify SQL vs REASON  (no RAG in this product)
  build_sql_static_prefix         — large cacheable schema + few-shots + rules
  build_sql_dynamic               — per-request follow-up context + question
  build_sql_prompt                — full SQL prompt (fallback when caching off)
  build_nl_answer_prompt          — human-readable NL answer from SQL results
  build_reason_prompt             — reason over prior data (no fetch)

Schema is verified against the live Neon table "Meghalaya_Chatbot"
(105,813 rows, 22 columns, 4 source batches).

The SQL prompt understands the behaviors exercised by SQL_Context.md:
  - district grouping on UPPER(TRIM(...)) so casing variants don't split a district
  - bank-name variant merging (SBI / MRB) via LIKE prefixes
  - regions (Garo/Khasi/Jaintia Hills) = SUM across several districts
  - mobile '.0' float-artifact handling
  - window functions, self-joins, subqueries (rankings, per-group tops, duplicate/shared-account audits)
  - CANNOT_ANSWER sentinel when the answer is not in this table (gender/age/income/crop/etc.)
"""
import json
from backend.config import settings
from backend.services.context_store import ConversationTurn
from backend.services.cross_scheme import (
    cross_scheme_rules, cross_scheme_shots, FOCUS_PG_TBL, FOCUS_PLUS_TBL, ELEVATE_TBL,
)

LANGS = {"en": "English", "hi": "Hindi"}

# The real Neon table name. Quoted because it is mixed-case.
TABLE = f'"{settings.DATA_TABLE}"'


# ── Data-quality / anti-hallucination construction rules ──────────────────────
# These rules shape the STRUCTURE of generated SQL. They contain NO counts —
# every number in an answer must be computed by the query against live data.

COUNTS_GUARD = f"""
SQL CONSTRUCTION RULES (anti-hallucination — these shape the query; they do NOT supply answer values):
  0. TERMINOLOGY — "beneficiary", "farmer", "member", and "enrolled" ALL mean the SAME thing:
     ONE ROW of the routed scheme's table = one enrolled beneficiary. So "how many beneficiaries / farmers /
     members" is ALWAYS COUNT(*) over the relevant rows — NEVER filtered by payment unless the user explicitly
     says "paid", "received payment", "pending", "unpaid", or "not paid". (When no scheme is named and the
     count is generic, give one COUNT per scheme — a per-scheme split — not a single table's count.)
       - "How many beneficiaries in X?"  → COUNT(*) ... (every enrolled farmer, paid or not).
       - "How many PAID beneficiaries / how many were paid in X?" → add WHERE "Amount(Rs)" IS NOT NULL.
       - "How many UNPAID / pending in X?" → add WHERE "Amount(Rs)" IS NULL.
     Do NOT silently restrict "beneficiary" to paid rows. The grand total of beneficiaries is the full table.
  1. TABLE SCOPE IS NOT FIXED — this is the UNIFIED entry point with NO default scheme. Run STEP 0 of the
     CROSS-SCHEME RULES first to ROUTE the question to the right table(s): {TABLE} (Focus+), {FOCUS_PG_TBL}
     (Focus), and/or {ELEVATE_TBL} (CM Elevate). A question that pins one scheme → that one table; a generic
     "beneficiaries/farmers/members" or plain district count with no scheme-pinning metric → a PER-SCHEME SPLIT
     across all three. The column rules below (2–17) describe Focus+ ({TABLE}); for the OTHER tables use the
     exact columns/types in the CROSS-SCHEME RULES (CS-9/CS-9b). Never use a schema prefix or invent a table.
  1a. ⚠ NO-SCHEME GENERIC COUNT = PER-SCHEME SPLIT, NOT Focus+. If the user asks a generic
     "how many beneficiaries / farmers / members are there" (optionally "in <district>") and names NO scheme and
     gives NO scheme-pinning metric (no "Focus+", no "paid/₹2500/DBT", no "PG/tranche/bookkeeper", no
     "loan/subsidy"), you MUST return ONE COUNT PER SCHEME (Focus+, Focus, CM Elevate) as a per-scheme split —
     NEVER a single COUNT(*) on {TABLE}. Defaulting a bare beneficiary count to Focus+ alone is WRONG here and
     hides two schemes. (Only when the user explicitly says "Focus+" / a Focus+-only metric do you use {TABLE} alone.)
  2. Column names contain spaces and mixed case — ALWAYS double-quote them EXACTLY:
     "S.No.", "District", "Block", "Village", "Member_id", "PG ID",
     "Member Name as per CR", "Member Name as per Bank Account", "EPIC ID",
     "Mobile Number", "Bank Name", "Other Bank Name", "Account No", "IFSC Code",
     "Amount(Rs)", "CHQ No / Released vide", "Date of Payment", "Farmer ID NIC",
     "Source Sheet".
  3. NEVER query these broken/empty columns: "New CR" (all '#ERROR!'),
     "Legacy CR" (all NULL), "Focus Legacy" (all NULL).
  4. "District" casing can vary (e.g. 'WEST GARO HILLS' vs 'West Garo Hills' are the SAME district).
     - To FILTER one district: match the WHOLE name, case-insensitively:
         UPPER(TRIM("District")) = 'WEST GARO HILLS'   ← use EQUALITY, not ILIKE '%...%'.
       ⚠ NEVER use a substring match for a district. Several names are prefixes of others —
       '%WEST GARO HILLS%' also matches 'SOUTH WEST GARO HILLS', '%GARO HILLS%' matches all five Garo
       districts, '%KHASI HILLS%' matches all four Khasi districts. Substring = wrong, inflated counts.
       (Substring ILIKE is fine only for a genuine REGION like '%GARO%' — see rule 12 — or for Block/Village.)
     - To GROUP/COUNT/RANK by district: ALWAYS group on UPPER(TRIM("District")), never on the raw
       "District" column, or the same district will be split across casing variants and counts will be wrong.
  4b. FUZZY / MIS-SPELLED / MIS-HEARD DISTRICT NAMES — resolve them, don't fail. The question often comes from
      voice or fast typing, so the district is frequently garbled, phonetic, or partial. There are ONLY 12 valid
      districts (listed in the schema). Pick the closest one by sound + spelling and use ITS exact canonical name
      in the filter. NEVER query the user's raw misspelling, and do NOT return CANNOT_ANSWER just because the
      spelling was off — only the 12 real districts exist, so almost any garbled input maps to one of them.
        - Phonetic / speech-to-text errors:  "best guerrillas"/"west gorilla"/"west garo" → WEST GARO HILLS;
          "east cause he"/"east kasi" → EAST KHASI HILLS; "re boy"/"ribhoi"/"ri bhoy" → RI BHOI;
          "jaintia"/"jayanti" (ambiguous) → ask nothing, default to the most populous match or treat as the
          Jaintia REGION if the user clearly means the area (see rule 12).
        - Partial names: "garo"/"khasi"/"jaintia" ALONE = a REGION, not a district (rule 12). But "west khasi",
          "south garo", "east jaintia" etc. are specific districts — complete them to the full canonical name.
        - Abbreviations/variants: "EKH" → EAST KHASI HILLS, "WGH" → WEST GARO HILLS, "SWGH" → SOUTH WEST GARO HILLS.
        - If two canonical districts are equally plausible and you truly cannot disambiguate, pick the single most
          likely one and proceed (the NL answer can note the assumption). Resolving to a real district always
          beats failing.
  5. NULL means "not recorded". "missing / no / without X" → WHERE "X" IS NULL.
     "has / with X" → WHERE "X" IS NOT NULL. (Columns are loaded as real NULL, not the string 'null'.)
  6. Every PAID record has "Amount(Rs)" = 2500. There is NO other amount. So:
       - "how much / total amount paid" → SUM("Amount(Rs)") and COUNT(*), never assume a figure.
       - a filter for any amount other than 2500 will return zero rows — don't invent one.
       - "AVERAGE payment per (paid) member / per farmer" → AVG("Amount(Rs)") WHERE "Amount(Rs)" IS NOT NULL.
         NEVER average over the whole table (the unpaid rows are NULL and would distort the result).
         The correct average is always 2500 because every paid row is exactly 2500.
  7. PAID farmers → WHERE "Amount(Rs)" IS NOT NULL.  UNPAID / new registrations → WHERE "Amount(Rs)" IS NULL.
     "members/farmers missing/without a PG ID" → WHERE "PG ID" IS NULL (count with COUNT(*)).
     Paid status maps 1:1 to batch: every 'Legacy Focus plus data' row is paid; every 'Meg-One-Focus-Plus-New%'
     row is unpaid. Don't claim otherwise.
  8. BANK-NAME VARIANTS must be merged. The same bank appears under multiple spellings:
       - State Bank of India: UPPER("Bank Name") LIKE 'STATE BANK OF INDIA%' (merges plain + '(SBI)').
       - Meghalaya Rural Bank: UPPER("Bank Name") LIKE 'MEGHALAYA RURAL BANK%'.
     For "how many use bank X" use a LIKE prefix, not '='. When GROUPING all banks, group on the raw
     "Bank Name" but warn that casing/'(SBI)' variants may list the same bank twice.
  9. "Date of Payment" is an Excel serial number (FLOAT), NOT a real date. Only two values
     exist (45882 = 13 Aug 2025, 45883 = 14 Aug 2025). Do NOT apply date functions to it; return it as-is.
     There is NO application/registration date — only this payment serial.
  10. "Mobile Number" and "Farmer ID NIC" are FLOAT. Cast to display: CAST("Mobile Number" AS BIGINT).
      Some mobiles are stored with a trailing '.0' artifact (float text). To clean/validate a mobile,
      strip the trailing '.0' before checking length. Treat a cleaned 10-digit value as valid, not as an error.
  11. Name searches must use ILIKE '%value%' on BOTH "Member Name as per CR" AND
      "Member Name as per Bank Account" (the bank name is only present on ~12,527 newer rows).
  12. REGIONS group districts: "Garo Hills" = the 5 districts whose name contains 'GARO'
      (WEST/EAST/NORTH/SOUTH/SOUTH WEST GARO HILLS); "Khasi Hills" = the 4 containing 'KHASI';
      "Jaintia Hills" = the 2 containing 'JAINTIA'. For a region question, filter
      UPPER("District") LIKE '%GARO%' (etc.) and SUM/COUNT across them — never treat a region as one district.
  13. "how many / count" questions → COUNT(*) with NO LIMIT.  Aggregates (SUM/AVG) → NO LIMIT.
  14. Listing rows → ALWAYS add LIMIT 100 unless the user explicitly asks for more or asks for a count/total.
      For "show me everything / all records" do NOT dump the table — return a COUNT(*) summary or LIMIT 100.
  15. IDs are NOT unique: "Member_id" and "Account No" can repeat (one Member_id appears 72 times;
      146 accounts are shared by 2 members). A lookup "may return multiple rows" — that is expected, not a bug.
  16. NEVER invent or hardcode a count or amount. If the question asks for a column NONE of the scheme tables
      have (gender, age/DOB, income, crop/land, caste, application date, etc.), output exactly: CANNOT_ANSWER.
      Do the same for a cross-scheme ask that requires an EPIC or bank account on CM Elevate — it has NEITHER
      (see CROSS-SCHEME RULE CS-3); answer the possible part and state the limit, or CANNOT_ANSWER.
  17. DO NOT GIVE UP ON ANSWERABLE QUESTIONS. The table HAS "District", "Block", and "Village", so any
      "which/what/top/most/fewest <block OR village> in <a district/block>" question IS answerable — write a
      GROUP BY on "Block" (or "Village") with the parent filter and ORDER BY the count. NEVER answer such a
      question with the parent district's TOTAL, and NEVER claim "the data only provides the district total" or
      that block/village data is unavailable — that is FALSE. CANNOT_ANSWER is ONLY for columns that truly do
      not exist (rule 16: gender/age/income/crop/caste/date) — never for a Block/Village/Bank/Source breakdown.
"""


# ── Verified schema (Meghalaya_Chatbot) ───────────────────────────────────────

SCHEMA = f"""
-- DATABASE: Neon PostgreSQL (standard SQL, no backticks)
-- SINGLE TABLE: {TABLE}  (105,813 rows — one row per farmer registration/payment)
-- Source: Meghalaya Focus Plus farmer payment & registration register.

-- COLUMNS (double-quote all of them; types and non-null counts are real):
--   "S.No."                           BIGINT   105,813  row serial number
--   "District"                        TEXT     105,812  ⚠ mixed casing — use UPPER()/ILIKE
--   "Block"                           TEXT     105,812  sub-district unit
--   "Village"                         TEXT     105,782  village name
--   "Member_id"                       TEXT     102,295  farmer ID, format FP######## (e.g. FP10658117)
--   "PG ID"                           TEXT      93,245  Producer Group ID (e.g. PG-FOCUS-EKH-14693)
--   "Member Name as per CR"           TEXT     105,813  name in Cash Register — ALWAYS populated
--   "Member Name as per Bank Account" TEXT      12,527  name in bank — only newer batches
--   "EPIC ID"                         TEXT     105,355  voter card ID (3 letters + 7 digits, e.g. JHM0624601)
--   "Mobile Number"                   FLOAT     59,596  ⚠ stored as float, cast to display
--   "Bank Name"                       TEXT     105,813  bank name (a few rows hold IFSC codes)
--   "Other Bank Name"                 TEXT       1,669  free-text bank name when "Others" selected
--   "Account No"                      TEXT     105,813  bank account number
--   "IFSC Code"                       TEXT     105,805  bank IFSC (e.g. SBIN0RRMEGB)
--   "Amount(Rs)"                      FLOAT     93,286  ⚠ ALWAYS 2500 for paid rows; NULL = unpaid
--   "CHQ No / Released vide"          TEXT      93,286  cheque no ('061781','061782') or 'Credit Advice Letter'
--   "Date of Payment"                 FLOAT     93,286  ⚠ Excel serial (45882 = 13 Aug 2025); NOT a real date
--   "Farmer ID NIC"                   FLOAT     18,169  NIC-assigned farmer ID
--   "Source Sheet"                    TEXT     105,813  data batch identifier (see values below)
--   "New CR" / "Legacy CR" / "Focus Legacy"  → BROKEN/EMPTY — never query

-- REFERENCE VALUES
-- Districts (12 real; stored UPPERCASE but mixed-case variants exist — group on UPPER(TRIM("District"))):
--   WEST GARO HILLS, EAST KHASI HILLS, EAST GARO HILLS, NORTH GARO HILLS,
--   SOUTH WEST GARO HILLS, SOUTH GARO HILLS, WEST KHASI HILLS, SOUTH WEST KHASI HILLS,
--   EASTERN WEST KHASI HILLS, RI BHOI, EAST JAINTIA HILLS, WEST JAINTIA HILLS
--   ⚠ These 12 are the ONLY valid districts. The user's spelling/voice input is often WRONG —
--     you MUST map it to the closest of these 12 BEFORE writing the filter (see rule 4b).
-- REGIONS (a region = several districts; NEVER a single "District" value):
--   "Garo Hills"   → UPPER("District") LIKE '%GARO%'   (5 districts)
--   "Khasi Hills"  → UPPER("District") LIKE '%KHASI%'  (4 districts)
--   "Jaintia Hills"→ UPPER("District") LIKE '%JAINTIA%' (2 districts)
-- Bank-name variants (merge with a LIKE prefix, see rule 8):
--   'State Bank of India' / 'State Bank of India (SBI)'  → 'STATE BANK OF INDIA%'
--   'Meghalaya Rural Bank' (+ casing variants)           → 'MEGHALAYA RURAL BANK%'
--   'Others'  → real bank unknown (free text may be in "Other Bank Name")
-- Source Sheet (batches):
--   'Legacy Focus plus data'   (93,286 rows — the only batch with payments)
--   'Meg-One-Focus-Plus-New', 'Meg-One-Focus-Plus-New2', 'Meg-One-Focus-Plus-New3'
--     (new registrations — NO Amount/PG ID; do have Mobile + Bank Account Name)
-- CHQ values: '061781', '061782', 'Credit Advice Letter'
-- COLUMNS THAT DO NOT EXIST (questions about these → CANNOT_ANSWER, never fabricate):
--   gender/sex, age/date-of-birth, income/salary, crop/land/acreage, caste/category,
--   application date or any date other than the "Date of Payment" serial.
"""


# ── Few-shot examples (verified against the guide & live data) ────────────────

SHOTS = f"""-- PATTERN: count everything ("beneficiary" = "farmer" = one row; NO payment filter)
Q: how many beneficiaries are there in Focus+
SQL: SELECT COUNT(*) AS total_beneficiaries FROM {TABLE};

-- PATTERN: beneficiaries in a district WHEN FOCUS+ IS NAMED (single table). A BARE "how many beneficiaries
-- in <district>" with NO scheme named is the per-scheme SPLIT instead (see UNIFIED AUTO-ROUTING shots) — do
-- NOT answer a no-scheme district count from {TABLE} alone.
Q: how many Focus+ beneficiaries in West Garo Hills
SQL: SELECT COUNT(*) AS beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS';

-- PATTERN: PAID beneficiaries (filter to paid ONLY when the user says paid/received)
Q: how many beneficiaries have been paid in West Garo Hills
SQL: SELECT COUNT(*) AS paid_beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS' AND "Amount(Rs)" IS NOT NULL;

-- PATTERN: MISSPELLED / VOICE district — map to the closest of the 12 real districts, then filter on the canonical name
Q: how many beneficiaries in best guerrillas
SQL: SELECT COUNT(*) AS beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS';

-- PATTERN: phonetic district from speech-to-text
Q: total farmers in east cause he
SQL: SELECT COUNT(*) AS beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'EAST KHASI HILLS';

-- PATTERN: garbled "Ri Bhoi"
Q: beneficiaries in re boy
SQL: SELECT COUNT(*) AS beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'RI BHOI';

-- PATTERN: district abbreviation
Q: how many farmers in SWGH
SQL: SELECT COUNT(*) AS beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'SOUTH WEST GARO HILLS';

-- PATTERN: count everything in FOCUS+ specifically (scheme named → single table). A BARE "how many
-- beneficiaries/farmers are there" with NO scheme named is NOT this — route it to the per-scheme SPLIT
-- (see the UNIFIED AUTO-ROUTING shots in the cross-scheme section), never to this single table.
Q: how many farmers are there in Focus+
SQL: SELECT COUNT(*) AS total_farmers FROM {TABLE};

-- PATTERN: count by district
Q: how many farmers are registered in each district
SQL: SELECT UPPER("District") AS district, COUNT(*) AS total_farmers FROM {TABLE} GROUP BY UPPER("District") ORDER BY total_farmers DESC;

-- PATTERN: paid vs unpaid
Q: how many farmers have been paid and how many are pending
SQL: SELECT COUNT("Amount(Rs)") AS paid_farmers, COUNT(*) - COUNT("Amount(Rs)") AS unpaid_farmers, COUNT(*) AS total_farmers FROM {TABLE};

-- PATTERN: total amount paid in a district (all amounts are 2500)
Q: what is the total amount paid in West Garo Hills
SQL: SELECT SUM("Amount(Rs)") AS total_paid_rs, COUNT(*) AS farmers_paid FROM {TABLE} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS' AND "Amount(Rs)" IS NOT NULL;

-- PATTERN: average payment per paid member (MUST exclude NULL/unpaid rows; result is always 2500)
Q: what is the average payment amount per paid member
SQL: SELECT AVG("Amount(Rs)") AS average_payment_rs, COUNT(*) AS paid_members FROM {TABLE} WHERE "Amount(Rs)" IS NOT NULL;

-- PATTERN: members missing a PG ID
Q: how many members are missing a PG ID
SQL: SELECT COUNT(*) AS members_missing_pg_id FROM {TABLE} WHERE "PG ID" IS NULL;

-- PATTERN: records per source sheet (data batch)
Q: how many records came from each source sheet
SQL: SELECT "Source Sheet", COUNT(*) AS total_records FROM {TABLE} GROUP BY "Source Sheet" ORDER BY total_records DESC;

-- PATTERN: list farmers in a district (always LIMIT 100)
Q: show all farmers in East Khasi Hills
SQL: SELECT "S.No.", "Member_id", "Member Name as per CR", "Block", "Village", "Bank Name", "Amount(Rs)" FROM {TABLE} WHERE UPPER(TRIM("District")) = 'EAST KHASI HILLS' LIMIT 100;

-- PATTERN: find a farmer by name (search BOTH name columns)
Q: find farmer Saba Lin Shabong
SQL: SELECT "S.No.", "Member_id", "Member Name as per CR", "District", "Block", "Village", "Account No", "Bank Name" FROM {TABLE} WHERE "Member Name as per CR" ILIKE '%Saba Lin Shabong%' OR "Member Name as per Bank Account" ILIKE '%Saba Lin Shabong%' LIMIT 100;

-- PATTERN: look up by Member ID
Q: show details for member FP10658117
SQL: SELECT * FROM {TABLE} WHERE "Member_id" = 'FP10658117';

-- PATTERN: look up a PG group
Q: list all members of PG group PG-FOCUS-EKH-14693
SQL: SELECT "Member_id", "Member Name as per CR", "Village", "Block", "Amount(Rs)", "CHQ No / Released vide" FROM {TABLE} WHERE "PG ID" = 'PG-FOCUS-EKH-14693';

-- PATTERN: search by EPIC / voter ID
Q: find farmer with voter ID JHM0624601
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Block", "Village", "Mobile Number" FROM {TABLE} WHERE "EPIC ID" = 'JHM0624601';

-- PATTERN: farmers paid by a cheque
Q: who was paid via cheque 061782
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Block", "Amount(Rs)", "Date of Payment" FROM {TABLE} WHERE "CHQ No / Released vide" = '061782' LIMIT 100;

-- PATTERN: farmers in a block
Q: show farmers in Mylliem block
SQL: SELECT "Member_id", "Member Name as per CR", "Village", "Amount(Rs)", "Bank Name" FROM {TABLE} WHERE "Block" ILIKE '%MYLLIEM%' LIMIT 100;

-- PATTERN: farmers banking with a specific bank
Q: list farmers who bank with Meghalaya Rural Bank
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Account No", "IFSC Code" FROM {TABLE} WHERE "Bank Name" ILIKE '%Meghalaya Rural Bank%' LIMIT 100;

-- PATTERN: missing mobile number
Q: which farmers have no mobile number recorded
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Block", "Village" FROM {TABLE} WHERE "Mobile Number" IS NULL LIMIT 100;

-- PATTERN: new (unpaid) registrations
Q: show new registrations not yet paid
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Block", "Mobile Number", "Source Sheet" FROM {TABLE} WHERE "Amount(Rs)" IS NULL ORDER BY "Source Sheet", "District" LIMIT 100;

-- PATTERN: name mismatch between CR and Bank
Q: show farmers where bank name differs from CR name
SQL: SELECT "Member_id", "Member Name as per CR", "Member Name as per Bank Account", "District", "Account No" FROM {TABLE} WHERE "Member Name as per Bank Account" IS NOT NULL AND UPPER(TRIM("Member Name as per CR")) <> UPPER(TRIM("Member Name as per Bank Account")) LIMIT 100;

-- PATTERN: count per block within a district, with paid count
Q: how many farmers per block in North Garo Hills
SQL: SELECT "Block", COUNT(*) AS farmers, COUNT("Amount(Rs)") AS paid FROM {TABLE} WHERE UPPER(TRIM("District")) = 'NORTH GARO HILLS' GROUP BY "Block" ORDER BY farmers DESC;

-- PATTERN: WHICH block in a district has the MOST/FEWEST — this IS answerable: GROUP BY "Block" within the
-- district, ORDER BY the count. NEVER answer with the district total and NEVER say block data is unavailable.
Q: which block in West Garo Hills has the most beneficiaries
SQL: SELECT "Block", COUNT(*) AS beneficiaries FROM {TABLE} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS' GROUP BY "Block" ORDER BY beneficiaries DESC LIMIT 1;

-- PATTERN: which village in a district/block has the most (same idea, GROUP BY "Village")
Q: which village in East Khasi Hills has the most farmers
SQL: SELECT "Village", COUNT(*) AS farmers FROM {TABLE} WHERE UPPER(TRIM("District")) = 'EAST KHASI HILLS' GROUP BY "Village" ORDER BY farmers DESC LIMIT 1;

-- PATTERN: search by account number
Q: find farmer with account number 87003025970
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Block", "Bank Name", "IFSC Code", "Amount(Rs)" FROM {TABLE} WHERE "Account No" = '87003025970';

-- PATTERN: farmers with a NIC Farmer ID
Q: list farmers who have a NIC Farmer ID assigned
SQL: SELECT "Member_id", "Member Name as per CR", "District", "Farmer ID NIC" FROM {TABLE} WHERE "Farmer ID NIC" IS NOT NULL LIMIT 100;

-- PATTERN: count by source batch with paid count
Q: how many records come from each data batch
SQL: SELECT "Source Sheet", COUNT(*) AS total_records, COUNT("Amount(Rs)") AS paid FROM {TABLE} GROUP BY "Source Sheet" ORDER BY total_records DESC;

-- PATTERN: bank-wise farmer count
Q: how many farmers per bank
SQL: SELECT "Bank Name", COUNT(*) AS farmers FROM {TABLE} GROUP BY "Bank Name" ORDER BY farmers DESC LIMIT 100;

-- PATTERN: count for ONE bank — MERGE spelling variants with a LIKE prefix
Q: how many farmers bank with State Bank of India
SQL: SELECT COUNT(*) AS sbi_farmers FROM {TABLE} WHERE UPPER("Bank Name") LIKE 'STATE BANK OF INDIA%';

-- PATTERN: Meghalaya Rural Bank merged count
Q: how many use Meghalaya Rural Bank
SQL: SELECT COUNT(*) AS mrb_farmers FROM {TABLE} WHERE UPPER("Bank Name") LIKE 'MEGHALAYA RURAL BANK%';

-- PATTERN: SBI share of all farmers (merge variants in numerator)
Q: what share of farmers does SBI serve
SQL: SELECT ROUND(100.0 * SUM(CASE WHEN UPPER("Bank Name") LIKE 'STATE BANK OF INDIA%' THEN 1 ELSE 0 END) / COUNT(*), 2) AS sbi_share_pct FROM {TABLE};

-- PATTERN: ratio (ALWAYS ROUND to 2 dp, guard denominator) — never show a raw long decimal
Q: what is the ratio of unpaid to paid members
SQL: SELECT ROUND(CAST(COUNT(*) - COUNT("Amount(Rs)") AS NUMERIC) / NULLIF(COUNT("Amount(Rs)"), 0), 2) AS ratio_unpaid_to_paid FROM {TABLE};

-- PATTERN: percentage unpaid (ROUND to 2 dp)
Q: what percentage of members are unpaid
SQL: SELECT ROUND(100.0 * (COUNT(*) - COUNT("Amount(Rs)")) / COUNT(*), 2) AS pct_unpaid FROM {TABLE};

-- PATTERN: region = SUM across several districts (NOT one district)
Q: how many farmers in the Garo Hills region
SQL: SELECT COUNT(*) AS garo_hills_farmers FROM {TABLE} WHERE UPPER("District") LIKE '%GARO%';

-- PATTERN: compare two regions in one row
Q: compare the Garo Hills and Khasi Hills regions
SQL: SELECT SUM(CASE WHEN UPPER("District") LIKE '%GARO%' THEN 1 ELSE 0 END) AS garo_hills, SUM(CASE WHEN UPPER("District") LIKE '%KHASI%' THEN 1 ELSE 0 END) AS khasi_hills FROM {TABLE};

-- PATTERN: paid vs unpaid per district in one row (cross-tab)
Q: paid vs unpaid per district
SQL: SELECT UPPER(TRIM("District")) AS district, COUNT("Amount(Rs)") AS paid, COUNT(*) - COUNT("Amount(Rs)") AS unpaid FROM {TABLE} WHERE "District" IS NOT NULL GROUP BY UPPER(TRIM("District")) ORDER BY unpaid DESC;

-- PATTERN: payment-completion rate per district
Q: which district has the lowest payment completion rate
SQL: SELECT UPPER(TRIM("District")) AS district, ROUND(100.0 * COUNT("Amount(Rs)") / COUNT(*), 1) AS pct_paid FROM {TABLE} WHERE "District" IS NOT NULL GROUP BY UPPER(TRIM("District")) ORDER BY pct_paid ASC;

-- PATTERN: districts ABOVE the average district size (subquery)
Q: which districts have more farmers than the average district
SQL: SELECT district, c FROM (SELECT UPPER(TRIM("District")) AS district, COUNT(*) AS c FROM {TABLE} WHERE "District" IS NOT NULL GROUP BY UPPER(TRIM("District"))) t WHERE c > (SELECT COUNT(*)::numeric / COUNT(DISTINCT UPPER(TRIM("District"))) FROM {TABLE} WHERE "District" IS NOT NULL) ORDER BY c DESC;

-- PATTERN: largest village in each district (ROW_NUMBER window)
Q: largest village in each district
SQL: SELECT district, village, c FROM (SELECT UPPER(TRIM("District")) AS district, "Village" AS village, COUNT(*) AS c, ROW_NUMBER() OVER (PARTITION BY UPPER(TRIM("District")) ORDER BY COUNT(*) DESC) AS rn FROM {TABLE} WHERE "District" IS NOT NULL GROUP BY UPPER(TRIM("District")), "Village") t WHERE rn = 1 ORDER BY c DESC;

-- PATTERN: each district's share of the grand total (SUM OVER window)
Q: each district's share of all farmers
SQL: SELECT UPPER(TRIM("District")) AS district, COUNT(*) AS c, ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct FROM {TABLE} WHERE "District" IS NOT NULL GROUP BY UPPER(TRIM("District")) ORDER BY c DESC;

-- PATTERN: rank banks handling ties (RANK / DENSE_RANK window)
Q: rank banks by number of farmers
SQL: SELECT "Bank Name", COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*) DESC) AS rnk, DENSE_RANK() OVER (ORDER BY COUNT(*) DESC) AS dense FROM {TABLE} GROUP BY "Bank Name" ORDER BY c DESC LIMIT 100;

-- PATTERN: running total of farmers, district by district
Q: cumulative farmer count by district largest first
SQL: SELECT district, c, SUM(c) OVER (ORDER BY c DESC) AS running_total FROM (SELECT UPPER(TRIM("District")) AS district, COUNT(*) AS c FROM {TABLE} WHERE "District" IS NOT NULL GROUP BY UPPER(TRIM("District"))) t ORDER BY c DESC;

-- PATTERN: duplicate member IDs (appear more than once)
Q: which member IDs appear more than once
SQL: SELECT "Member_id", COUNT(*) AS c FROM {TABLE} WHERE "Member_id" IS NOT NULL GROUP BY "Member_id" HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 100;

-- PATTERN: total/SUM disbursed for DUPLICATE PG IDs (PG ID appears more than once). This is an AGGREGATE — return
-- ONE SUM row, NEVER the list of duplicate rows. Sum paid "Amount(Rs)" over every row whose "PG ID" is in the set
-- of PG IDs that occur >1 time. (A repeated PG ID is a Producer GROUP with many members — normal — so this is the
-- total flowing through multi-member groups, NOT proven over-payment; flag only as "review".) Works as a follow-up
-- too: "what is their total disbursed amount" after a duplicate-PG list → THIS sum, not a re-list.
Q: what is the total disbursed amount for all duplicate PG IDs
SQL: SELECT SUM("Amount(Rs)") AS total_disbursed_rs, COUNT(*) AS paid_rows
     FROM {TABLE}
     WHERE "Amount(Rs)" IS NOT NULL
       AND "PG ID" IN (SELECT "PG ID" FROM {TABLE} WHERE "PG ID" IS NOT NULL GROUP BY "PG ID" HAVING COUNT(*) > 1);

-- PATTERN: accounts shared by more than one member
Q: which accounts are linked to more than one member
SQL: SELECT "Account No", COUNT(DISTINCT "Member_id") AS members FROM {TABLE} GROUP BY "Account No" HAVING COUNT(DISTINCT "Member_id") > 1 ORDER BY members DESC LIMIT 100;

-- PATTERN: member pairs sharing an account (self-join)
Q: list the member pairs that share an account
SQL: SELECT a."Account No", a."Member_id" AS member_a, b."Member_id" AS member_b FROM {TABLE} a JOIN {TABLE} b ON a."Account No" = b."Account No" AND a."Member_id" < b."Member_id" LIMIT 100;

-- PATTERN: "suspicious / fake / fraudulent / dubious accounts" → surface accounts shared by multiple members
-- (the only data-driven anomaly signal). These are flagged for REVIEW, not proof of fraud.
Q: show me farmers who might have fake accounts
SQL: SELECT "Account No", COUNT(DISTINCT "Member_id") AS members, STRING_AGG(DISTINCT "Member_id", ', ') AS member_ids FROM {TABLE} WHERE "Account No" IS NOT NULL GROUP BY "Account No" HAVING COUNT(DISTINCT "Member_id") > 1 ORDER BY members DESC LIMIT 100;

-- PATTERN: estimated over-payment from duplicate paid rows
Q: estimated over-payment from duplicate payments
SQL: SELECT SUM("Amount(Rs)") - (COUNT(DISTINCT "Member_id") * 2500) AS est_overpayment_rs FROM {TABLE} WHERE "Amount(Rs)" IS NOT NULL;

-- PATTERN: village names that occur in more than one district
Q: which village names occur in more than one district
SQL: SELECT "Village", COUNT(DISTINCT UPPER(TRIM("District"))) AS districts FROM {TABLE} GROUP BY "Village" HAVING COUNT(DISTINCT UPPER(TRIM("District"))) > 1 ORDER BY districts DESC LIMIT 100;

-- PATTERN: records with no district recorded
Q: which records have no district
SQL: SELECT "Member_id", "Member Name as per CR", "PG ID", "Block" FROM {TABLE} WHERE "District" IS NULL LIMIT 100;

-- PATTERN: bank recorded only as "Others"
Q: how many farmers have their bank recorded as Others
SQL: SELECT COUNT(*) AS others_count FROM {TABLE} WHERE UPPER(TRIM("Bank Name")) = 'OTHERS';

-- PATTERN: distinct IFSC codes
Q: how many distinct IFSC codes are there
SQL: SELECT COUNT(DISTINCT "IFSC Code") AS distinct_ifsc FROM {TABLE} WHERE "IFSC Code" IS NOT NULL;

-- PATTERN: data quality scorecard / health check / completeness summary → ONE row of key coverage metrics
Q: give me a data quality scorecard
SQL: SELECT COUNT(*) AS total_records, COUNT(*) - COUNT("District") AS missing_district, COUNT(*) - COUNT("Mobile Number") AS missing_mobile, COUNT(*) - COUNT("PG ID") AS missing_pg_id, COUNT(*) - COUNT("Member_id") AS missing_member_id, COUNT(*) - COUNT(DISTINCT "Member_id") AS duplicate_member_id_rows, COUNT(*) - COUNT("Amount(Rs)") AS unpaid_rows FROM {TABLE};
"""



# ── SQL prompt RULES (static — part of the cacheable prefix) ──────────────────

_SQL_RULES = f"""RULES:
- Output ONLY a valid PostgreSQL SELECT or WITH statement. No markdown, no backticks, no explanation.
- TABLE SCOPE IS DECIDED FROM INTENT: first run STEP 0 of the CROSS-SCHEME RULES to decide whether the answer
  needs one scheme or several, then pick the SMALLEST set of tables. Default to the single Focus+ table {TABLE}
  (double-quoted); use {FOCUS_PG_TBL} (Focus) and/or {ELEVATE_TBL} (CM Elevate) only when the intent truly spans
  them. Never invent any table or column that is not listed in the schema or cross-scheme rules.
- Double-quote every column name (they all contain spaces or mixed case).
- District FILTER: UPPER(TRIM("District")) = 'VALUE' (whole-name equality, NOT ILIKE '%...%' — a substring
  match wrongly catches prefix-sharing names, e.g. '%WEST GARO HILLS%' also matches SOUTH WEST GARO HILLS).
- FUZZY DISTRICTS: the input district is often misspelled / mis-heard (voice). There are ONLY 12 valid districts.
  Map the garbled input to the closest canonical district by sound + spelling and filter on THAT exact name
  (e.g. "best guerrillas"→'WEST GARO HILLS', "east cause he"→'EAST KHASI HILLS', "re boy"→'RI BHOI', "EKH"→'EAST
  KHASI HILLS'). Never filter on the raw misspelling; never CANNOT_ANSWER for a bad district spelling. A bare
  "garo/khasi/jaintia" is a REGION (LIKE), but "west garo / east khasi" etc. complete to the full district name.
- District GROUP/COUNT/RANK: group on UPPER(TRIM("District")) so casing variants don't split a district.
- REGION ("Garo/Khasi/Jaintia Hills") = several districts → UPPER("District") LIKE '%GARO%' (etc.), then SUM/COUNT.
- Bank counts merge variants: UPPER("Bank Name") LIKE 'STATE BANK OF INDIA%' / 'MEGHALAYA RURAL BANK%' (LIKE, not =).
- Name searches: ILIKE '%value%' on both "Member Name as per CR" and "Member Name as per Bank Account".
- Block/Village searches: "Block" ILIKE '%value%' / "Village" ILIKE '%value%'.
- Exact-ID lookups (Member_id, PG ID, EPIC ID, Account No): use = 'value'. IDs may return multiple rows — that's fine.
- "missing / no / without X" → WHERE "X" IS NULL.  "has / with X" → WHERE "X" IS NOT NULL.
- Paid farmers: "Amount(Rs)" IS NOT NULL.  Unpaid/new: "Amount(Rs)" IS NULL.
- All paid amounts are exactly 2500 — for "how much" use SUM("Amount(Rs)") + COUNT(*).
- "average payment per member/farmer" → AVG("Amount(Rs)") WHERE "Amount(Rs)" IS NOT NULL (never average NULL/unpaid rows).
- ANY division — ratio, percentage, share, average, rate, "per X" — MUST be rounded, and because "Amount(Rs)",
  "Mobile Number" etc. are FLOAT/double precision, you MUST CAST the whole expression to NUMERIC before ROUND
  (PostgreSQL has NO ROUND(double precision, int) — it errors "function does not exist"). Correct form:
      ROUND( CAST( <the entire division expression> AS NUMERIC ), 2 )
  Always guard the denominator with NULLIF(denominator, 0). Examples:
      ROUND(CAST(100.0 * SUM(...) / NULLIF(SUM("Amount(Rs)"), 0) AS NUMERIC), 2)   -- percentage / share
      ROUND(CAST(COUNT(*) AS NUMERIC) / NULLIF(COUNT(DISTINCT "Block"), 0), 2)      -- average / ratio
  NEVER return a raw division like 0.13428595930793473833. NEVER write ROUND(<float expr>, 2) without the CAST.
- "missing / without a PG ID" → WHERE "PG ID" IS NULL.
- Mobile display: CAST("Mobile Number" AS BIGINT). Cleaning a mobile: strip a trailing '.0' before length checks.
- NEVER query "New CR", "Legacy CR", or "Focus Legacy".
- "Date of Payment" is an Excel serial float — return as-is, never apply date functions. There is no application date.
- Window functions (ROW_NUMBER/RANK/SUM OVER/LAG/NTILE) and self-joins are allowed for ranking, per-group tops, and duplicate/shared-account audits.
- Count/total questions → COUNT(*)/SUM(...) with NO LIMIT.
- Listing rows → ALWAYS add LIMIT 100 (unless the user explicitly asks for all or for a count). "show everything" → COUNT(*) summary or LIMIT 100, never a full dump.
- Always alias columns with readable names (AS district, AS total_farmers, etc.).
- "legacy / old data" → "Source Sheet" = 'Legacy Focus plus data'.
- "new registrations / new data" → "Source Sheet" LIKE 'Meg-One-Focus-Plus-New%'.
- "which/top/most/fewest <block> in <district>" (or <village> in a district/block) IS answerable: GROUP BY
  "Block" (or "Village") with the parent filter, ORDER BY the count, LIMIT 1 for "the single most/fewest".
  NEVER answer it with the district TOTAL and NEVER claim block/village data is unavailable — it exists.
- If the question asks for a column the table lacks (gender, age/DOB, income, crop/land, caste, application date)
  or otherwise cannot be answered from this single table, output exactly: CANNOT_ANSWER. Do NOT use CANNOT_ANSWER
  for any Block/Village/Bank/Source/District breakdown — those columns all exist and are always answerable."""


def build_sql_static_prefix() -> str:
    """
    The large, byte-for-byte identical portion of the SQL prompt
    (schema + counts guard + few-shots + rules). Cached server-side via
    Gemini context caching (see ai_service.ai_call's cache_prefix param).
    """
    return (
        f"{SCHEMA}\n\n{COUNTS_GUARD}\n\n{cross_scheme_rules('Focus+', auto_route=True)}\n\n"
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
            lines.append("The current question may be a follow-up. Apply any implied filters, district, "
                         "block, bank, or paid/unpaid scope from above.\n")
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
    'and unpaid', 'and paid', 'and pending',
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
    'compare that', 'compare those',
])
# NOTE: "what about", "how about", "break it down", "show me only", "filter by" were REMOVED from
# _REASONING. They almost always mean "fetch a NEW slice/row" (e.g. "what about the fewest", "what about
# East Khasi Hills") which REASON-over-old-rows cannot answer — they belong to _CONTINUATION → fresh SQL.

_REFLECTIVE = frozenset([
    'which is the highest', 'which is the lowest',
    'which one is highest', 'which one is lowest',
    'which is bigger', 'which is smaller', 'which has more', 'which has less',
    'what is the highest', 'what is the lowest',
    'biggest', 'smallest', 'difference between',
    'top one', 'bottom one',
    'which district', 'which block', 'which bank', 'which batch', 'which village',
    'which one', 'which has', 'which had', 'which is',
    ' lowest', ' highest', ' most ', ' least ',
    'the lowest', 'the highest', 'the most', 'the least',
])

_TOPIC_TERMS = {
    'farmer', 'farmers', 'member', 'beneficiar',
    'district', 'block', 'village', 'meghalaya', 'garo', 'khasi', 'jaintia',
    'paid', 'unpaid', 'pending', 'payment', 'amount', 'cheque', 'chq',
    'bank', 'account', 'ifsc', 'mobile', 'epic', 'voter',
    'pg', 'producer group', 'nic', 'batch', 'source', 'registration', 'registered',
    'legacy', 'new',
    # cross-scheme vocabulary (Focus / Focus+ / CM Elevate)
    'scheme', 'focus', 'elevate', 'subsidy', 'loan', 'disburse', 'overlap', 'footprint',
    'both', 'all three', 'cross',
}


def _norm(text: str) -> str:
    return f" {text.lower().strip()} "


import re as _re_pa


def names_scheme(question: str) -> str | None:
    """
    Return the scheme EXPLICITLY named in the question ('Focus', 'Focus+', 'CM Elevate'), or None.
    Order matters: check Focus+ and CM Elevate BEFORE bare 'focus', because 'focus plus' contains 'focus'.
    """
    ql = question.lower()
    if _re_pa.search(r"focus\s*\+|focus\s*plus|\bfocusplus\b|\bdbt\b", ql):
        return "Focus+"
    if _re_pa.search(r"\belevate\b|cm\s*elevate|\bcml\b|cm\s*assure|\bassure\b", ql):
        return "CM Elevate"   # "CM Assure" is a user-facing alias for the CM Elevate scheme
    if _re_pa.search(r"\bfocus\b", ql):           # bare 'focus' (not 'focus plus' — handled above)
        return "Focus"
    return None


# A question is SELF-CONTAINED (no context needed) if it names a scheme AND carries its own metric/intent —
# e.g. "how many beneficiaries in focus", "total loans in cm elevate". Such a question must NOT be rewritten
# against prior turns (that is what bled Focus+ numbers into a Focus question).
_SELF_METRIC = (
    'how many', 'how much', 'count', 'total', 'number of', 'list', 'show', 'which', 'what is',
    'top ', 'average', 'sum', 'breakdown', 'compare',
    # relational / overlap phrasing carries its own intent too — e.g. "Focus PG members who are
    # also CM Assure farmers". Without these, such a scheme-named question was treated as a
    # follow-up and inherited a prior district (e.g. West Garo Hills) it never mentioned.
    'who are', 'who is', 'also ', 'common', 'overlap', 'both ', 'members', 'farmers',
    # statistical / distribution phrasing — these make a question self-contained too, e.g.
    # "PGs with member count above state median" must NOT inherit a prior turn's context.
    'median', 'percentile', 'above ', 'below ', 'mean ', 'variance', 'distribution', 'per ', 'each ',
)


def is_self_contained_scheme_q(question: str) -> bool:
    q = _norm(question)
    return names_scheme(question) is not None and any(m in q for m in _SELF_METRIC)


def is_followup(question: str, context: list[ConversationTurn]) -> bool:
    """Decide if a question needs context-aware resolution."""
    if not context:
        return False
    analytical = [t for t in context if t.intent != "EDGE"]
    if not analytical:
        return False

    # A question that names its own scheme AND has its own metric is self-contained — never a follow-up.
    # This stops "how many beneficiaries in focus" from being rewritten with a prior Focus+/CM-Elevate turn.
    if is_self_contained_scheme_q(question):
        return False

    q = _norm(question)
    n_words = len(q.split())

    if any(sig in q for sig in _REFERENTIAL):  return True
    if any(sig in q for sig in _CONTINUATION): return True
    if any(sig in q for sig in _AGGREGATION):  return True
    if any(sig in q for sig in _REASONING):    return True
    if any(sig in q for sig in _REFLECTIVE):   return True

    # SELF-CONTAINED ANALYTICAL question — names its own SUBJECT + METRIC and has NO referential/continuation
    # word above. Such a question is complete on its own and must NOT be rewritten against a prior turn (that bled
    # the previous "concentration over time" context into "PGs with member count above state median"). Requires a
    # concrete subject (pg/member/district/...) AND a self-metric (median/average/count/...); referential signals
    # were already ruled out by the returns above.
    _SUBJECT = ('pg', 'producer group', 'member', 'beneficiar', 'farmer', 'district', 'block',
                'village', 'bank', 'scheme', 'application', 'loan', 'tranche', 'disburs')
    if any(s in q for s in _SUBJECT) and any(m in q for m in _SELF_METRIC):
        return False

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

    # Pure reasoning verbs (why/explain/summarize/interpret) are always REASON-able over any prior data.
    if any(sig in q for sig in _REASONING):
        return True

    # A reflective comparison ("which is highest/lowest/most/least") can ONLY be answered from prior rows
    # if those rows actually CONTAIN the comparison set. Two failure modes force a fresh SQL fetch instead:
    #   1. The most recent result is a SINGLE row (e.g. a LIMIT 1 "the most" answer). You cannot derive
    #      "the fewest" / "the second" / any other slice from one row — that needs a new query.
    #   2. The question introduces a NEW place/scope not in the recent context (e.g. after a West Garo Hills
    #      result, "and East Khasi Hills?"). The prior rows don't hold that scope — fetch it.
    if any(sig in q for sig in _REFLECTIVE):
        # Most recent turn that actually returned rows.
        last_with_data = next((t for t in reversed(analytical) if t.sql_data), None)
        if not last_with_data or len(last_with_data.sql_data) < 2:
            return False  # too few rows to compare → fresh SQL

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
Meghalaya Focus Plus farmer payment assistant.

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
2. Carry forward filters (district, block, bank, paid/unpaid) UNLESS the user changes them.
3. If the user introduces a brand-new topic not in history, do NOT bolt prior filters on.
4. If the message is already standalone, return it unchanged.
5. Output ONLY the rewritten question. No quotes, no prefix, no explanation.

EXAMPLES:
History: "farmer count by district" answered with district counts.
"what about paid only?"  →  "How many paid farmers are there in each district?"

History: shows district farmer totals.
"which district has the most?"  →  "Which district has the most farmers in the data shown?"

History: West Garo Hills paid total.
"what about East Khasi Hills?"  →  "What is the total amount paid in East Khasi Hills?"

History: district counts listed, West Garo Hills first, then East Khasi Hills, then East Garo Hills.
"compare the first and third districts you mentioned"  →  "Compare the farmer counts of West Garo Hills and East Garo Hills."

History: "Which district has the most farmers?" → West Garo Hills.
"and the second one?"  →  "Which district has the second most farmers?"

History: East Khasi Hills is the current district in focus.
"show me its largest village"  →  "What is the largest village in East Khasi Hills by farmer count?"

History: "Which block in West Garo Hills has the most beneficiaries?" → Tikrikilla.
"what about the fewest"  →  "Which block in West Garo Hills has the fewest beneficiaries?"

History: "Which block in West Garo Hills has the most beneficiaries?" → Tikrikilla.
"and East Khasi Hills?"  →  "Which block in East Khasi Hills has the most beneficiaries?"

History: villages in Tikrikilla block were just listed.
"how many are paid there"  →  "How many beneficiaries have been paid in Tikrikilla block?"

History: "How many beneficiaries are in Focus+?" → 105,813.
"what about CM Elevate?"  →  "How many beneficiaries are in CM Elevate?"

History: per-district footprint across all three schemes was just shown.
"which district is highest"  →  "Which district has the highest total footprint across all three schemes?"

History: "How many beneficiaries are in both Focus and Focus+?" → an EPIC-overlap count.
"and only in Focus+?"  →  "How many beneficiaries are only in Focus+ and not in Focus?"

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

SQL    = run a fresh query against the LIVE farmer-payment table for numbers, counts, lists, breakdowns, lookups.
REASON = answer using ONLY the prior conversation data shown above. No new fetch. Use this when the user is
         asking to reason ABOUT what was already shown — "why", "explain", "summarize", "which one is highest
         among these", "what does that mean".

DECISION ORDER:
1. Reasoning verbs (why / explain / summarize / interpret) over prior data → REASON.
2. Reflective comparison ("which is the highest?", "biggest one?") → REASON **only if** the data already shown
   CONTAINS the full set being compared (i.e. the prior turn returned MANY rows). If the prior turn showed a
   SINGLE row (e.g. "the most X" = one row), you CANNOT derive "the fewest", "the second", or any other slice
   from it → SQL.
3. A follow-up that asks for a NEW slice, the OPPOSITE superlative, or a DIFFERENT place/scope than what was
   shown → SQL. Examples: prior showed the block with the MOST; "what about the fewest" / "and the lowest" →
   SQL. Prior was about West Garo Hills; "and East Khasi Hills?" / "what about Tura?" → SQL.
4. Anything asking for fresh numbers, lists, lookups, breakdowns, totals → SQL.
5. When in doubt between SQL and REASON, prefer SQL (fresh data is safer than stale, and never produces a
   false "I cannot answer").

EXAMPLES:
"How many farmers are there?"                          → SQL
"District-wise farmer count"                           → SQL
"Find farmer FP10658117"                               → SQL
"Total amount paid in West Garo Hills"                 → SQL
(after a MULTI-ROW district breakdown) "which is highest?" → REASON
(after a breakdown)          "explain that"            → REASON
(after district counts)      "summarize"              → REASON
(after "the block with the MOST") "what about the fewest?" → SQL   ← prior was ONE row; need a new query
(after a West Garo Hills answer) "and East Khasi Hills?"   → SQL   ← new place not in prior data
(after one block's villages) "now show paid only"         → SQL   ← new slice/filter
(after a Focus+ count) "what about CM Elevate?"           → SQL   ← a different scheme's roster, fresh fetch
(after a 3-scheme footprint table) "which district is highest?" → REASON ← the rows already hold all districts

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

    return f"""You are the Meghalaya Focus Plus farmer-payment assistant.
The user is asking a follow-up that should be answered by REASONING over the data already shown in this
conversation. Do NOT invent new numbers. Do NOT pretend to query a database. Use only the rows below.

{primary_data_block}
FULL CONVERSATION HISTORY:
{ctx}
USER'S QUESTION: "{question}"

HOW TO ANSWER:
- Start with the PRIMARY DATA above — these are the actual rows from the most recent query.
- For "which district/block/bank/batch": scan the rows and name the matching row + its value.
- For "which is highest/lowest/most/least": sort the rows by the relevant metric and name the top/bottom row
  with its exact value from the data.
- ORDINALS / POSITIONS ("the second one", "the first and third districts you mentioned", "the next largest"):
  use the row ORDER as it was presented in the data — 1st row = first, 2nd row = second, etc. Name the exact
  row(s) at those positions with their values; keep that ordering stable across follow-ups.
- PRONOUNS ("its largest village", "how many of them", "do the same for the smallest"): resolve the pronoun to
  the specific subject from the prior turn (e.g. "its" → the district named in the previous answer) before answering.
- For "explain / summarize": describe the 2–3 most important numbers from the most recent turn.
- For "WHY / what is the reason / explain that": give the OPERATIONAL reason using the DOMAIN EXPLANATIONS below,
  grounded in the prior number. Do NOT say "the data does not say" when a domain explanation below applies —
  these are verified facts about how the Focus+ register works, so cite them as the reason.
- NULL/empty columns are "not recorded" — do NOT cite them as reasons for any value, and never call a farmer fraudulent.
- If no domain explanation fits and the data truly cannot explain it, say so honestly and suggest a follow-up query.

DOMAIN EXPLANATIONS (verified facts — use these to answer "why" about the most recent number):
- WHY farmers are UNPAID / payment pending: every UNPAID row ("Amount(Rs)" IS NULL) belongs to the NEW
  registration batches ("Source Sheet" LIKE 'Meg-One-Focus-Plus-New%'), which are recently enrolled farmers
  whose Rs. 2,500 DBT has not been disbursed YET. The PAID rows are all the 'Legacy Focus plus data' batch.
  So "unpaid" = newly-registered, not-yet-in-a-payment-cycle (often also missing a "PG ID"/bank detail needed
  before release) — NOT a rejection or a problem with the farmer. State this as the reason.
- WHY some farmers have no PG ID / no mobile / no bank-account name: those fields are captured progressively;
  the older legacy batch was loaded before some columns existed, so they are blank for those rows ("not recorded").
- WHY all paid amounts are the same (Rs. 2,500): Focus+ is a FIXED DBT of Rs. 2,500 per farmer — there is no
  variable amount, so every paid row is exactly 2,500 by design.
- WHY a district/block has more farmers: it reflects enrolment volume in that area (more registered farmers),
  not a data error. Do not over-interpret beyond "more farmers are registered there."
- For cross-scheme "why" (e.g. why an overlap is small): EPIC coverage — Focus PG records EPIC on only ~29% of
  rows and CM Elevate has no EPIC, so person-level overlaps are partial by data design, not because few people
  truly enrol in multiple schemes.

FORMATTING:
- 2–4 sentences. Direct answer first, then the supporting numbers.
- CURRENCY: use the ₹ symbol (NEVER "Rs."). Write amounts as the FULL figure in Indian comma notation
  (1,40,000 not 140000) with ₹ in front, e.g. ₹1,40,000. For large amounts add the crore value in brackets,
  e.g. ₹23,32,00,000 (₹23.32 Cr). NEVER give only the abbreviated "Cr"/"lakh" form without the full number.
- Do NOT mention SQL, queries, databases, or that this came from conversation history.
{lang_instr}

ANSWER:"""


# ── Natural Language Answer ───────────────────────────────────────────────────

def build_nl_answer_prompt(
    question: str, sql: str, results: list, row_count: int,
    language: str, context: list[ConversationTurn] = None,
    asked_questions: list[str] = None,
) -> str:
    lang_name  = LANGS.get(language, "English")
    lang_instr = f"Respond in {lang_name}." if language != "en" else ""
    ctx        = _fmt_context(context or [], mode="compact", max_turns=3)

    # Build the "already asked" list so the FOLLOWUP never repeats an earlier question/follow-up. Use the prior
    # turns' questions (the follow-ups the user accepted become the next turn's question, so this also covers
    # repeated follow-ups). Keep the last several so the suggestion keeps ADVANCING instead of looping back.
    already_asked_block = ""
    _prior_qs = asked_questions if asked_questions is not None else [
        t.resolved_question for t in (context or []) if t.intent != "EDGE" and t.resolved_question
    ]
    if _prior_qs:
        _seen = "; ".join(f'"{q}"' for q in _prior_qs[-6:])
        already_asked_block = (
            f"- ALREADY ASKED this session (do NOT repeat or rephrase any of these — pick a genuinely NEW angle): {_seen}\n"
        )

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
        # Per-scheme split where one scheme is NULL (e.g. a VILLAGE-name split: Focus PG has only village_id).
        if 'null' in _sql_l and ('village' in _sql_l or 'village' in (question or '').lower()):
            cross_scheme_note += (
                "- A NULL Focus value here means Focus PG could not be filtered by VILLAGE NAME (it stores only "
                "village_id, not a name — CS-9). Report Focus+ and CM Elevate village counts, and add one sentence: "
                "the Focus (PG) figure is unavailable by village name. Do NOT print 'Focus = 0' as if it were zero.\n"
            )
        if _epic_keyed:
            cross_scheme_note += (
                "- This per-person ranking is keyed on EPIC, which exists ONLY in Focus+ and Focus. CM Elevate has "
                "no EPIC, so it is EXCLUDED from this ranking. Frame the figure as \"combined disbursement across "
                "Focus+ and Focus\" (the two EPIC-bearing schemes), and add ONE sentence noting CM Elevate is not "
                "included here and can be compared only at the district level. Never imply CM Elevate is in the sum.\n"
            )
        # Venn / overlap / "common" / "across all" buckets: there is NO true 3-way EPIC overlap (CM Elevate has
        # no EPIC). Trigger on bare "all"/"across all"/"common"/"shared" too — a govt user asking "common ... across
        # all" MUST be told why the figure is 2-scheme, or it looks like a bug.
        _ql = (question or '').lower()
        if any(k in _sql_l for k in ('intersect', 'except', 'only', '∩', 'bucket', 'overlap')) or \
           any(k in _ql for k in ('venn', 'overlap', 'all three', 'all 3', 'across all', 'all scheme',
                                  'every scheme', 'only in', 'triple', 'common', 'shared', 'same person', 'in both')):
            cross_scheme_note += (
                "- OVERLAP/COMMON HONESTY (MANDATORY — include this even if the answer gets longer): the real EPIC "
                "overlap is Focus+ ∩ Focus ONLY. CM Elevate has no EPIC, so it CANNOT be part of a per-person "
                "'common across schemes' count. You MUST state the count is the Focus+ ∩ Focus overlap AND add one "
                "sentence: CM Elevate is excluded because it has no EPIC/person key and can only be compared by "
                "district. NEVER describe a 'triple overlap' or 'common across all three' by EPIC; never let the "
                "user believe CM Elevate was included. If they want Elevate folded in, offer the district-level view.\n"
            )

    return f"""You are the Meghalaya Focus Plus farmer-payment assistant.
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
- Every paid farmer received exactly Rs. 2,500 — never quote a different per-farmer amount.
- "Amount(Rs)" NULL means the farmer is registered but not yet paid (a new-batch registration).
- "Date of Payment" is an Excel serial number; serial 45882 = 13 August 2025, 45883 = 14 August 2025.
- "Mobile Number" is a float — display it as a plain integer (drop the trailing .0).
- NULL columns mean "not recorded" — never describe a NULL as a problem, anomaly, or decline.
- TREND / OVER-TIME caveat: only CM Elevate carries a year, and it spans just 2024 + a PARTIAL 2025 (far fewer
  records). For any year-over-year / concentration / trend result, state the direction but add that it is CM-Elevate
  only and indicative only (2025 is partial) — never a firm conclusion.
- CONCENTRATION/HHI VOLUME SKEW: an HHI / top-share from a period (quarter/month) with very few applications is an
  artifact, not real concentration (1 app → HHI 1.0 / 100%). NEVER headline a low-volume period as "highest
  concentration"; judge the trend from high-volume periods, cite the applications count with each figure, and call
  the series noisy / no clear trend when swings come from tiny periods.
- The same bank may appear under variant spellings (e.g. "State Bank of India" and "...(SBI)"); if the rows
  show both, mention they are the same bank rather than listing it as two.
- "Garo/Khasi/Jaintia Hills" is a REGION (several districts), not a single district.
- Duplicate payments or shared accounts are anomalies to FLAG as "needs review" — never call any farmer
  fraudulent and never assert over-payment as confirmed; say it is an estimate that needs manual review.

FORMATTING RULES:
- Lead with the direct answer or most important finding.
- Highlight highest/lowest values by name with exact numbers.
- If many rows, mention the top 2–3 AND the bottom 1–2 by name with exact numbers.
- CURRENCY: use the ₹ symbol (NEVER "Rs."). Write the FULL figure in Indian comma notation with ₹ in front,
  and for large amounts add the crore value in brackets, e.g. ₹23,32,00,000 (₹23.32 Cr). Never abbreviate-only.
- 2–4 sentences. No mention of SQL, queries, databases, or technical pipeline.
- If results span many districts/blocks/batches, end with one sentence on the overall trend or
  the single most notable insight.
{lang_instr}

After the answer, on a NEW line, output a follow-up question the user would naturally ask NEXT,
prefixed exactly with "FOLLOWUP:". It MUST:
- be specific to THIS question and these results (reference a real district/block/bank/value from the rows when useful),
- be a single short question (max ~12 words) that drills deeper or compares,
- be answerable from this same farmer-payment dataset (no external data),
- ADVANCE the exploration: pick a NEW angle (a different district/block/bank/scheme/metric, a deeper drill-down,
  or a comparison) than what was already asked. Do NOT repeat or paraphrase any earlier question/follow-up.
{already_asked_block}- NEVER reuse a generic default like "Drill into West Garo Hills…" two turns in a row — vary it every turn.
If no sensible NEW deeper question exists, output "FOLLOWUP: NONE".

Answer:"""
