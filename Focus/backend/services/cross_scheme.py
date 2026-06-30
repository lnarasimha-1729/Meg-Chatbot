"""
Cross-scheme analytics block — SHARED, backend-agnostic.

The three scheme tables (Focus / Focus+ / CM Elevate) all live in the SAME Neon
database, so any of the three backends can JOIN across them in one query. This
module is the single source of truth for the cross-scheme RULES + few-shots, so
the three backends stay in sync. An IDENTICAL copy lives in each backend's
services/ dir (the apps are deployed separately and must not import each other).

Real table + join facts are verified live (2026-06-28):
  - Focus+ (DBT, ₹2,500/yr):   "Meghalaya_Chatbot"  — EPIC col "EPIC ID"   (~100% populated)
  - Focus  (Producer Groups):  focus_pg             — EPIC col member_epic_id (~29% populated)
  - CM Elevate (loan+subsidy):  cm_elevate           — *** NO EPIC, NO bank-account column ***

EPIC join links ONLY Focus+ <-> Focus. CM Elevate joins the others by DISTRICT
(reliable) or NAME (low-confidence fuzzy). See cross-scheme-join-reality memory.
"""

FOCUS_PLUS_TBL = '"Meghalaya_Chatbot"'
FOCUS_PG_TBL   = "focus_pg"
ELEVATE_TBL    = "cm_elevate"

# The 12 valid Meghalaya districts, NORMALISED (UPPER, hyphen→space) — the canonical
# whitelist used by the per-district cross-scheme rollup (CS-8) so every backend's
# "footprint"/heat-map count is deterministic and excludes blanks/junk district values.
_VALID_DISTRICTS = (
    "EAST KHASI HILLS", "WEST KHASI HILLS", "SOUTH WEST KHASI HILLS", "EASTERN WEST KHASI HILLS",
    "EAST GARO HILLS", "WEST GARO HILLS", "NORTH GARO HILLS", "SOUTH GARO HILLS", "SOUTH WEST GARO HILLS",
    "EAST JAINTIA HILLS", "WEST JAINTIA HILLS", "RI BHOI",
)
VALID_DISTRICTS_SQL = ", ".join(f"'{d}'" for d in _VALID_DISTRICTS)


# ── Conceptual / explanatory answers about the schemes (no SQL — these are FACTS, not data) ──
# Used by the edge handler to answer "what is X / how is Focus different from Focus+ / what is the
# join key / how reliable are joins" without generating SQL. Sourced from the project brief + verified data.

_SCHEME_FOCUS = (
    "**Focus** is the Producer Group (PG) formation & finance scheme. It organises farmers into "
    "Producer Groups (≈37,354 PGs) and tracks PG-level finance (tranche-1 and tranche-2 disbursement, "
    "bookkeeper coverage, IVCS). Members are tracked inside each PG, but their EPIC/voter ID is sparse "
    "(only ~29% of rows carry an EPIC)."
)
_SCHEME_FOCUS_PLUS = (
    "**Focus+** (FOCUS Plus) is the Direct Benefit Transfer (DBT) scheme — a fixed **Rs. 2,500** "
    "payment to individual farmers (105,813 beneficiary records; every paid row is exactly Rs. 2,500). "
    "Every beneficiary has an EPIC/voter ID recorded (~100%)."
)
_SCHEME_ELEVATE = (
    "**CM Elevate** is the state's loan + subsidy scheme across 13 livelihood verticals (Piggery, Poultry, "
    "Dairy, Sericulture, Warehouse, Vehicle, etc.), ~2,847 applications and a ~Rs. 61 Cr pipeline. It has "
    "**no EPIC and no bank-account column**, so it can only be linked to the other schemes by district or by name."
)

CONCEPT_ANSWERS = {
    "focus_vs_focusplus": (
        "**How is Focus different from FOCUS+?**\n\n"
        "| Feature | FOCUS | FOCUS+ |\n"
        "| --- | --- | --- |\n"
        "| **Purpose** | Organizes farmers into Producer Groups (PGs) and provides financial assistance to PGs for livelihood activities. | Provides **additional financial support directly to eligible FOCUS members**, primarily through Direct Benefit Transfer (DBT). |\n"
        "| **Primary Entity** | Producer Groups (PGs) and their members | Individual beneficiaries |\n"
        "| **Financial Assistance** | Funds are disbursed to Producer Groups (e.g., PG bank account, finance amount, disbursement dates). | Fixed assistance is transferred directly to individual beneficiary bank accounts. |\n"
        "| **Key Records** | PG ID, PG Name, Bookkeeper, PG Bank Details, Members, Finance Disbursement | Member ID, Member Name, Mobile Number, EPIC ID, Individual Bank Details |\n"
        "| **Bank Details** | Mainly PG bank account information along with member bank details | Individual beneficiary bank account details for DBT |\n"
        "| **Data Structure** | Hierarchical (PG → Members) | Flat beneficiary-wise records |\n"
        "| **Focus** | Community-based farming through Producer Groups | Individual financial assistance to registered FOCUS beneficiaries |"
    ),
    "what_is_focus": (
        "**FOCUS (Farmers' Collectivisation for Upscaling Production and Marketing Systems)** is a "
        "Government of Meghalaya initiative designed to strengthen rural livelihoods by organizing farmers "
        "into Producer Groups (PGs) and providing them with financial assistance to improve agricultural "
        "production, livestock activities, value addition, and market access.\n\n"
        "The scheme covers approximately **197,504 beneficiaries** organized into **37,354 Producer Groups (PGs)** "
        "across Meghalaya.\n\n"
        "**Scheme Objectives**\n"
        "- Organize farmers into Producer Groups (PGs).\n"
        "- Strengthen community-based farming and livelihood activities.\n"
        "- Provide direct financial assistance to eligible Producer Groups.\n"
        "- Improve agricultural productivity and rural incomes.\n"
        "- Promote collective production, procurement, and marketing.\n"
        "- Ensure financial inclusion by linking Producer Groups with bank accounts.\n"
        "- Maintain beneficiary and financial records for transparency and monitoring."
    ),
    "what_is_bookkeeper": (
        "A **Bookkeeper** is the designated person who manages the accounts, financial records, and "
        "documentation of a Producer Group, ensuring proper utilization and transparent tracking of scheme funds."
    ),
    "what_is_ivcs": (
        "**IVCS** is a village-level cooperative organization that supports Producer Groups by providing "
        "financial services, coordinating agricultural activities, and connecting farmers to markets and "
        "government schemes."
    ),
    "_what_is_focus_short":       f"{_SCHEME_FOCUS}",
    "what_is_focus_plus":  f"{_SCHEME_FOCUS_PLUS}",
    "what_is_elevate":     f"{_SCHEME_ELEVATE}",
    "what_are_schemes": (
        "There are three schemes in this system:\n\n"
        f"• {_SCHEME_FOCUS}\n\n"
        f"• {_SCHEME_FOCUS_PLUS}\n\n"
        f"• {_SCHEME_ELEVATE}"
    ),
    "join_key": (
        "**EPIC ID (voter ID)** is the primary join key across schemes. Focus+ stores it as \"EPIC ID\", "
        "Focus stores it as member_epic_id; matching those links the same farmer across the two schemes. "
        "**CM Elevate has no EPIC**, so it can only be related to the others by district or by name (lower "
        "confidence). Where an EPIC is missing, a fuzzy name match is the fallback."
    ),
    "join_reliability": (
        "Cross-scheme joins are only as reliable as EPIC coverage. **Focus+** has EPIC on ~100% of records, "
        "but **Focus PG** has EPIC on only ~29% of rows — so a Focus↔Focus+ join only covers that ~29% subset. "
        "**CM Elevate** has no EPIC at all and can be matched only by district or name (approximate). Treat any "
        "all-three-scheme person-level overlap as a lower-bound, not an exact figure."
    ),
}


def detect_concept_question(ql: str) -> str | None:
    """
    Detect a CONCEPTUAL / explanatory question about the schemes (answerable with FACTS, not SQL).
    `ql` is the lower-cased question. Returns a CONCEPT_ANSWERS key, or None.
    Must be checked BEFORE any data-intent early-exit (words like 'focus'/'scheme' are also data words).
    """
    import re as _re
    explain = bool(_re.search(
        r"\b(what|what's|whats|how|explain|describe|tell me about|difference|differ|differs|"
        r"vs\.?|versus|compared?|meaning of|define)\b", ql))
    if not explain:
        return None
    has_plus = bool(_re.search(r"focus\s*\+|focus\s*plus|\bdbt\b", ql))
    has_focus_word = "focus" in ql
    has_elevate = bool(_re.search(r"\belevate\b|cm\s*elevate", ql))
    mentions_join = bool(_re.search(r"\bjoin\b|\bkey\b|\blink(ed|s)?\b|\bmatch", ql))
    mentions_reliable = bool(_re.search(r"reliab|accura|trust|confiden|coverage of", ql))

    # "how is Focus different from Focus+ / Focus vs Focus+ / compare focus and focus plus"
    # BUT NOT a DATA comparison that merely mentions a scheme, e.g. "compare Focus+ beneficiaries in East and
    # West Garo Hills" (comparing DISTRICTS within a scheme) — that must go to SQL, not the concept explainer.
    # If the question carries a data dimension (a place, a count/amount metric, or "in <somewhere>"), it is NOT
    # the conceptual scheme-vs-scheme question.
    # A DATA DIMENSION (a place, a metric, a number-word) means the user wants DATA, not a definition. Any such
    # word disqualifies the whole concept path — "what is the combined DISBURSEMENT of Focus and CM Elevate" is a
    # SQL money query, NOT "what is CM Elevate". Concept answers are only for BARE definitional asks.
    has_data_dim = bool(_re.search(
        r"\b(district|districts|block|village|garo|khasi|jaintia|ri\s*bhoi|beneficiar|farmer|farmers|member|members|"
        r"count|how many|how much|amount|disburse|disbursed|disbursement|combined|sum|total|paid|unpaid|pending|"
        r"loan|loans|subsidy|sanction|sanctioned|application|applications|average|number of|"
        r"in\s+\w+\s+(hills|block|village|district))\b",
        ql))
    # A bare definitional question has an explain/what verb but is SHORT and carries no data dimension.
    is_definitional = (not has_data_dim) and bool(_re.search(
        r"\b(what\s+is|what's|whats|what\s+are|explain|describe|tell me about|meaning of|define|difference|differ|"
        r"vs\.?|versus|how\s+is|how\s+does)\b", ql))
    if not is_definitional:
        return None

    if (has_plus and has_focus_word and
            _re.search(r"differ|difference|vs\.?|versus|compare|how is|how does|between", ql)):
        return "focus_vs_focusplus"
    if mentions_join and (has_focus_word or has_elevate or "scheme" in ql):
        return "join_key"
    if mentions_reliable and ("scheme" in ql or "join" in ql or "cross" in ql):
        return "join_reliability"
    # single-scheme "what is X"
    if _re.search(r"\b(what|what's|whats|explain|describe|tell me about|meaning of|define)\b", ql):
        # role/term definitions — check BEFORE scheme names ("what is a bookkeeper in Focus" contains "focus")
        if _re.search(r"\bbook\s*keeper", ql):
            return "what_is_bookkeeper"
        if _re.search(r"\bivcs\b", ql):
            return "what_is_ivcs"
        if has_elevate:
            return "what_is_elevate"
        if has_plus:
            return "what_is_focus_plus"
        # A SPECIFIC scheme named (bare "focus") wins over the generic all-schemes answer, so
        # "what is the Focus scheme" → what_is_focus, not the 3-scheme overview.
        if has_focus_word:
            return "what_is_focus"
        if _re.search(r"\bschemes?\b|cross.?scheme|all (three|3)", ql):
            return "what_are_schemes"
    return None


def cross_scheme_rules(home_scheme: str = "Focus+", auto_route: bool = False) -> str:
    """
    The cross-scheme RULES block (STEP 0 intent-driven table scope + CS-1..CS-7).
    `home_scheme` is the scheme this backend natively serves — used only to phrase
    the "when no scheme is named, default to THIS table" guidance.

    `auto_route=True` is for the UNIFIED entry point (one box, no scheme buttons): there is
    NO home scheme, so an unscoped question must be ROUTED by its entity/metric, and a question
    that fits several schemes is answered as a per-scheme SPLIT (never silently pinned to one table).
    """
    home_tbl = {
        "Focus+": FOCUS_PLUS_TBL,
        "Focus":  FOCUS_PG_TBL,
        "CM Elevate": ELEVATE_TBL,
    }.get(home_scheme, FOCUS_PLUS_TBL)

    # ── Unified auto-router: replace the "default to home table" guidance with intent routing ──
    if auto_route:
        step_c = f"""  c) FIRST: is a SCHEME NAMED? A named scheme is a HARD table lock — use ONLY that table, ignore prior turns:
       - "focus plus" / "focus+" / "DBT" → Focus+ ({FOCUS_PLUS_TBL}).   CHECK "plus" BEFORE bare "focus".
       - bare "focus" (NO "plus")          → Focus ({FOCUS_PG_TBL}).  A "focus" question on {FOCUS_PLUS_TBL} is WRONG.
       - "cm elevate" / "elevate" / "CML"  → CM Elevate ({ELEVATE_TBL}).
     If NO scheme is named, ROUTE by the entity/metric in (a):
       - mentions loans / subsidy / sanctioned / disbursed / verticals (Piggery, Poultry, Dairy…) → CM Elevate ({ELEVATE_TBL})
       - mentions Producer Group / PG / bookkeeper / IVCS / tranche-1 / tranche-2 → Focus ({FOCUS_PG_TBL})
       - mentions DBT / the ₹2,500 payment / paid-vs-unpaid / EPIC roster / "Focus Plus" → Focus+ ({FOCUS_PLUS_TBL})
       - GENERIC "beneficiaries/farmers/members" or a plain district/block/village count with NO metric that pins
         one scheme (the SAME question is valid for all three rosters) → answer as a PER-SCHEME SPLIT across all
         three tables (one COUNT per scheme), NOT a single table. See the "per scheme" / footprint shots.
     READ THE METRIC BREADTH of a COMPARE (and "vs"/"versus"/"difference between"). Decide what to compute from the
     words, then compare it for BOTH sides across all 3 schemes (one row per scheme) unless a scheme is named:
       - "compare A and B" with NO metric → FULL PROFILE: every headline metric (beneficiaries COUNT + disbursement
         SUM) for both, all 3 schemes. Do NOT shrink it to Focus+ farmers only. (See the compare-profile shot.)
       - "compare A and B in terms of <X>" / "...by <X>" → ONLY metric X (beneficiaries→COUNT, disbursement/amount→
         SUM, paid→COUNT where paid, etc.), still across all 3 schemes. Honour the narrowing; don't add other metrics.
       - "compare Focus+ A and B" (scheme named) → that one scheme only.
     When still unsure, prefer the per-scheme split over guessing one table — showing all three is never wrong."""
        decision_default = ("ONE scheme   → query just that table when the entity/metric pins exactly one scheme.\n"
                            "       NO scheme named + generic → PER-SCHEME SPLIT (one count per table), never a single guess.")
        unsure_line = ("When unsure which single scheme is meant, prefer the PER-SCHEME SPLIT across all three tables "
                       "over guessing one — showing every scheme is never wrong, silently dropping two is.")
    else:
        step_c = f"""  c) Otherwise (the entity + metric live in exactly ONE scheme, no comparison) → use that ONE table only.
     If the question names no scheme, default to THIS backend's home table {home_tbl} ({home_scheme})."""
        decision_default = f"ONE scheme   → query just that table (most questions; default {home_scheme} {home_tbl})."
        unsure_line = ("When unsure whether a 2nd/3rd table is truly needed, prefer the SINGLE home table — only widen "
                       "scope when the intent genuinely cannot be answered from one scheme.")

    return f"""
CROSS-SCHEME RULES (joining Focus / Focus+ / CM Elevate — verified against the live DB 2026-06-28):

  THE THREE SCHEME TABLES (same Neon DB — you MAY reference several in one query):
    - Focus+ (DBT, ₹2,500/yr):   {FOCUS_PLUS_TBL}   — EPIC col "EPIC ID" (105,355 / 105,813 populated ~100%)
    - Focus  (Producer Groups):  {FOCUS_PG_TBL}     — EPIC col member_epic_id (only 63,843 / 221,088 rows ~29%)
    - CM Elevate (loan+subsidy):  {ELEVATE_TBL}      — *** NO EPIC COLUMN, NO bank-account column ***

  ════════════════════════════════════════════════════════════════════════════════════════════
  STEP 0 — DECIDE TABLE SCOPE FROM THE INTENT (do this silently BEFORE writing any SQL).
  ════════════════════════════════════════════════════════════════════════════════════════════
  Ask yourself: "To answer THIS question truthfully, whose data do I need?" Pick the SMALLEST set of
  tables that can answer it. Reason about meaning, NOT about whether a magic word is present.

  a) What ENTITY / METRIC is being asked about, and which scheme(s) own that data?
       - Producer Groups, PG members, bookkeepers, PG finance/tranches ........ Focus  ({FOCUS_PG_TBL})
       - DBT ₹2,500 farmer payments, EPIC/voter rosters, Focus Plus registrations Focus+ ({FOCUS_PLUS_TBL})
       - loans, subsidies, sanctioned/disbursed amounts, 13 livelihood verticals  CM Elevate ({ELEVATE_TBL})
  b) Does the question COMPARE, COMBINE, or RELATE more than one scheme, OR ask about people/places
     "across", "in both", "in all 3", "only in", "overlap/Venn", "vs", "more than one scheme",
     "which scheme", "enrolled in", a footprint/heat-map/saturation spanning schemes, OR name a scheme
     OTHER than this backend's own ({home_scheme})?  → it needs MULTIPLE tables.
     ⚠ ALSO multi-scheme (these phrasings describe how schemes are DISTRIBUTED, so they REQUIRE all 3 tables —
       NEVER answer them with a single-scheme COUNT(*)): "single-scheme dependency", "monolithic"/"mono-scheme",
       "over-reliant / reliant on one scheme", "scheme dominance / dominated by one scheme", "least/most
       diversified", "scheme concentration / mix / balance / spread", "districts depending on just one scheme".
       Each of these is the per-district CS-8 footprint split + a dominance ratio (see the dominance shot).
{step_c}

  DECISION OUTPUT (then write the SQL accordingly):
       {decision_default}
       TWO schemes  → if both are Focus & Focus+ → EPIC JOIN (CS-1). Any pair incl. CM Elevate → district
                      or fuzzy-name only (CS-2), because CM Elevate has no EPIC.
       THREE schemes→ EPIC overlap for Focus∩Focus+, CM Elevate folded in by DISTRICT (reliable) or NAME
                      (approximate). Never fake an EPIC for CM Elevate (CS-2/CS-3).
  {unsure_line}

  CS-1. JOIN KEY = EPIC, but ONLY between Focus+ and Focus.
        Focus+ "EPIC ID"  ==  Focus member_epic_id.
        Match case/space-insensitively: UPPER(TRIM(a."EPIC ID")) = UPPER(TRIM(b.member_epic_id)).
        Focus member_epic_id is NULL/blank on ~71% of rows — clean it: NULLIF(TRIM(member_epic_id),'').
        So any "appears in both Focus and Focus+" answer is limited to the ~29% of Focus rows that carry an EPIC.
        State this coverage caveat in the answer; never imply the join is complete.

  CS-2. CM ELEVATE CANNOT BE JOINED BY EPIC OR BANK ACCOUNT — it has neither column. Do NOT invent
        applicant_epic / bank_account_no for cm_elevate (they do NOT exist). The ONLY ways to relate CM Elevate
        to the other two schemes are:
          (a) by DISTRICT/BLOCK/VILLAGE  → reliable, use for all "all 3 schemes" rollups, heat maps, coverage.
          (b) by NAME (low-confidence fuzzy fallback): cm_elevate.name ~ "Member Name as per CR" (Focus+) or
              focus_pg.member_name (Focus). Only ~464 / ~653 of CM Elevate's ~2,823 names match — ALWAYS label
              name-based cross-scheme results as approximate / needs manual verification.

  CS-3. ANY query that asks for an EPIC-level fact that requires CM Elevate (e.g. "EPIC in ALL THREE schemes",
        "same EPIC different scheme for Elevate", "same bank account across schemes incl. Elevate", "AC code
        from EPIC for Elevate applicants") is NOT answerable as asked — cm_elevate has no EPIC/account column.
        Do NOT fabricate. Either (i) answer the part that IS possible (Focus+ ∩ Focus by EPIC, + CM Elevate by
        district/name) and state the limitation, or (ii) if nothing meaningful is left, output CANNOT_ANSWER.
        NEVER pretend cm_elevate has an EPIC.

  CS-4. DISTRICT is the safe cross-3-scheme dimension. District columns:
          Focus+  "District"   (mixed case)        → UPPER(TRIM("District"))
          Focus   district_name (NULL on member rows; junk values exist) → UPPER(TRIM(district_name))
          Focus   member rows mostly have NO district (only ~15,877 do) — district rollups on Focus are PG-row
                  scoped; say so.
          Elevate district      (clean, all 12 present) → UPPER(TRIM(district))
        Normalise every district with UPPER(TRIM(...)) before grouping/joining. The 12 canonical names are the
        same set across schemes (note Focus stores 'Ri-Bhoi' hyphenated; Elevate & Focus+ use 'RI BHOI' —
        compare with REPLACE(UPPER(TRIM(x)),'-',' ') to make 'RI-BHOI' = 'RI BHOI').

  CS-5. "beneficiaries in N schemes" / Venn / "only in X" → build per-EPIC presence with UNION/FULL JOIN over
        the EPIC-bearing tables (Focus+, Focus). CM Elevate joins this set by NAME only (flag as approximate)
        or is reported as a separate district-level column. Count distinct EPICs, not rows.

  CS-6. CM Elevate money columns are dirty TEXT — cast with NULLIF(col,'')::numeric (e.g. total_disbursement,
        sanctioned). bank_santioned is the only real float. Focus+ amount is "Amount(Rs)" (always 2500).
        "combined disbursement per EPIC across schemes" can only sum Focus+ (2500/paid) + Focus tranches; CM
        Elevate adds in by district/name only — make the scope explicit.

  CS-7. SCHEME-COUNT TERMINOLOGY: "Focus" = Producer Group scheme ({FOCUS_PG_TBL}); "Focus+"/"DBT" =
        {FOCUS_PLUS_TBL}; "CM Elevate"/"Elevate"/"loan"/"subsidy" = {ELEVATE_TBL}. These are THREE DIFFERENT
        schemes with THREE DIFFERENT rosters — never merge their row counts as if one table.

  CS-8. CANONICAL PER-DISTRICT CROSS-SCHEME ROLLUP (use this EXACT shape for "footprint", "per-district across
        schemes", "district x scheme", heat-map counts — so the numbers are DETERMINISTIC and identical every
        time). Each scheme contributes COUNT(*) of its rows that carry a VALID district:
          - Focus+   : UPPER(TRIM("District"))                  IN the 12 valid names
          - Focus    : REPLACE(UPPER(TRIM(district_name)),'-',' ') IN the 12 valid names   ← counts ALL Focus
                       rows carrying a district (PG + member rows), NOT just focus_pg_id rows; junk like
                       'Khatarshnong Laitkroh C & RD Block' is auto-excluded by the IN-list.
          - Elevate  : REPLACE(UPPER(TRIM(district)),'-',' ')   IN the 12 valid names
        ALWAYS use the 12-name whitelist below (it normalises Ri-Bhoi and drops blanks/junk), ALWAYS emit a
        selected, aliased total_footprint = focus_plus + focus + cm_elevate, and ORDER BY total_footprint DESC.
        VALID DISTRICT WHITELIST (normalised, hyphen→space, UPPER):
          ('EAST KHASI HILLS','WEST KHASI HILLS','SOUTH WEST KHASI HILLS','EASTERN WEST KHASI HILLS',
           'EAST GARO HILLS','WEST GARO HILLS','NORTH GARO HILLS','SOUTH GARO HILLS','SOUTH WEST GARO HILLS',
           'EAST JAINTIA HILLS','WEST JAINTIA HILLS','RI BHOI')

  CS-9. EXACT COLUMN NAMES OF THE OTHER SCHEMES (use these VERBATIM — do NOT guess names like 'village_name',
        'member_id', 'mobile' for the other tables; a wrong name makes the query ERROR). If a question needs a
        column a scheme does NOT have (see ✗), that scheme cannot answer that part — say so (CS-11).
        {FOCUS_PG_TBL} (Focus PG, all TEXT, dirty; blank = a space ' '):
          focus_pg_id, pg_name, has_bookkeeper_been_identified, bank_account_no, bank_ifsc, bank_branch,
          bank_name, ivcs_name, ivcs_account_no, block_name, district_name, village_id (✗ no village NAME — only id),
          finance_amount_disbursed (tranche-1), disburse_amount_2 (tranche-2), member_name, member_epic_id,
          member_age, member_unique_id (the member id — NOT 'member_id'), gender_id, member_bank_name,
          member_bank_account_no, member_bank_ifsc_code, focus_pg_members_count.
          ✗ Focus PG has NO mobile/phone column, NO payment-amount like Focus+ "Amount(Rs)".
        {ELEVATE_TBL} (CM Elevate, money/year are TEXT → NULLIF(col,'')::numeric; bank_santioned is real float):
          sl__no, application_number, name (the applicant name — NOT 'applicant_name'), district, scheme, block,
          village, sanctioned, bank_santioned, total_subsidy_disbursement, total_loan, total_disbursement,
          loan_disbursed ('disbursed'/'not disbursed'), refusedy_n ('Y'), month, year.
          ✗ CM Elevate has NO EPIC, NO bank-account number, NO mobile, NO member id (CS-2/CS-3).
        {FOCUS_PLUS_TBL} (Focus+) columns are in the main SCHEMA above ("EPIC ID","District","Block","Village",
          "Member Name as per CR","Account No","Bank Name","Mobile Number","Amount(Rs)", etc.).

  CS-9b. MONEY/NUMBER COLUMN TYPES DIFFER BY SCHEME — apply the RIGHT handling per table or the query ERRORS
         (42883 "function does not exist", e.g. TRIM() on a float). DO NOT apply Focus PG's "dirty TEXT" recipe to
         Focus+ or to bank_santioned.
           - Focus+ {FOCUS_PLUS_TBL}: "Amount(Rs)","Mobile Number","Farmer ID NIC" are ALREADY double precision.
             Use them DIRECTLY: SUM("Amount(Rs)"), COALESCE("Amount(Rs)",0). NEVER TRIM/regex/::numeric-cast them.
           - Focus {FOCUS_PG_TBL}: finance_amount_disbursed, disburse_amount_2 are TEXT + dirty (blank=' '). Cast
             guarded: COALESCE(NULLIF(TRIM(col),'')::numeric, 0).
           - CM Elevate {ELEVATE_TBL}: total_disbursement, total_loan, sanctioned, total_subsidy_disbursement are
             TEXT → COALESCE(NULLIF(TRIM(col),'')::numeric,0); BUT bank_santioned is ALREADY double precision
             (use directly, no cast).

  CS-10. COUNT THE THING ASKED, NOT A PROXY. "how many BENEFICIARIES / people / EPICs in all 3 / in 2+ / only in X"
         → count distinct EPICs (people), NEVER districts. "how many DISTRICTS ..." → count districts. Do not
         silently answer a people-question with a district number. Because CM Elevate has no EPIC, a true person-
         level "in ALL THREE" is impossible — answer Focus+∩Focus by EPIC and state CM Elevate can only be added
         by district/name (CS-3). For "only in CM Elevate" at PERSON level: not possible by EPIC; answer by name
         anti-join (approx) or say so.

  CS-11. PARTIAL-ANSWER OVER REFUSAL. If a cross-scheme question is answerable for SOME schemes but not all (e.g.
         needs a column one scheme lacks), ANSWER the part that works and state the gap — prefer that over a blanket
         CANNOT_ANSWER. Reserve CANNOT_ANSWER for when NOTHING meaningful can be returned. Examples that ARE
         answerable: "PG members not yet receiving DBT" = Focus member_epic_id NOT IN Focus+ "EPIC ID";
         "Focus PG farmers eligible for CM Elevate" = Focus districts/names not present in CM Elevate (by district
         or name, flagged approximate); "AC constituency concentration / overlap" = group by LEFT(epic,3) over the
         EPIC-bearing schemes (Focus+, Focus) only, noting CM Elevate is excluded (no EPIC). "AC CONSTITUENCY" ALWAYS
         means the 3-letter EPIC prefix LEFT(epic,3) — it is ALWAYS answerable from EPICs, NEVER CANNOT_ANSWER.
         "cross-scheme OVERLAP per AC" = AC prefix over the Focus+ ∩ Focus EPIC INTERSECT set. "ABOVE/BELOW AVERAGE
         per AC" = compare each AC's count to AVG(COUNT(*)) OVER () (or a scalar AVG subquery) — a HAVING/filter, an
         analytical question that IS answerable; do NOT refuse it.

  CS-12. WRITE SQL THAT ACTUALLY RUNS (these two mistakes recur on cross-scheme queries):
         (a) UNION/UNION ALL COLUMN TYPES MUST MATCH per position. EPIC columns are TEXT; amounts are NUMERIC.
             NEVER UNION a text column against a numeric one. For a per-EPIC "presence + amount" set, give EVERY
             branch the SAME typed columns and use TYPED nulls for the slots a branch doesn't have:
               e TEXT, focus_plus_amt NUMERIC, focus_amt NUMERIC, elevate_amt NUMERIC
               → in each branch put NULL::numeric (not bare NULL) for the amounts it lacks, and NULL::text for e.
             Focus+ amount = "Amount(Rs)"::numeric; Focus = COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)
             + COALESCE(NULLIF(TRIM(disburse_amount_2),'')::numeric,0); CM Elevate has no EPIC so it CANNOT join
             this EPIC set — leave it out of an EPIC-keyed disbursement query and say so.
         (b) ANTI-JOINS: use NOT EXISTS or EXCEPT, NEVER `x NOT IN (SELECT ... FROM big_table)` — on focus_pg
             (221k rows) a NOT IN subquery is slow enough to TIME OUT (and is NULL-unsafe). Pattern:
               ... WHERE NOT EXISTS (SELECT 1 FROM {FOCUS_PG_TBL} f WHERE UPPER(TRIM(f.member_epic_id)) = a.e)
             or  SELECT e FROM focus_plus_set EXCEPT SELECT e FROM focus_set.

  CS-13. "HOUSEHOLD / FAMILY / PERSON receiving the MOST across schemes" — there is NO household-id column in any
         scheme, and CM Elevate has no person key (CS-2/CS-3). So NEVER refuse this. Interpret it as the PERSON
         (the EPIC = the closest thing to a household identifier) with the HIGHEST COMBINED disbursement summed
         across the EPIC-bearing schemes (Focus+ "Amount(Rs)" + Focus tranche-1 + tranche-2), grouped by EPIC and
         ordered DESC. CM Elevate cannot be added at person level (no EPIC) — STATE that in the answer and, if the
         user wants Elevate folded in, offer the per-DISTRICT version (CS-8 rollup of disbursement). "the most" →
         ORDER BY combined_disbursement DESC LIMIT 1 (or LIMIT N for "top N households/families/beneficiaries").
         This is a PARTIAL answer (CS-11), NOT a CANNOT_ANSWER. The SAME mapping applies to "family", "beneficiary",
         "farmer", "who got the most money across schemes", "biggest recipient across all 3 schemes".
         ANSWER PHRASING (be honest about scope — do NOT imply every top recipient is in all three schemes):
           • SELECT the per-EPIC scheme count (COUNT(DISTINCT scheme) AS schemes) so the answer can show how many
             schemes each recipient actually appears in — most top recipients are in only ONE scheme; say so plainly.
           • Frame the figure as "combined disbursement across the two EPIC-bearing schemes (Focus+ and Focus)",
             NOT "across all three". STATE up front that CM Elevate is excluded from this per-person ranking because
             it has no EPIC, and offer the per-district disbursement rollup (CS-8) as the way to include Elevate.
           • NEVER write "received the most across all three schemes" — the ranking spans Focus+ and Focus only.

  CS-14. ⛔ NEVER FABRICATE A 3-WAY *EPIC* (PERSON-LEVEL) OVERLAP OR AN ELEVATE EPIC. CM Elevate has NO EPIC column
         (verified live). So a PERSON-LEVEL "same person in ALL THREE schemes" or a PERSON/EPIC F∩E / F+∩E / F∩F+∩E
         bucket is IMPOSSIBLE — do NOT invent a number for it. A "F∩F+∩E = 77 people"-style EPIC number is a
         HALLUCINATION. This ban is ONLY about EPIC/person-level overlap.
         ‼ DISTINGUISH THE TWO KINDS OF VENN:
           • "overlap matrix / venn split / 7-bucket venn" with NO "by EPIC"/"person" → this is a DISTRICT-PRESENCE
             Venn and is FULLY ANSWERABLE for all 7 buckets — use CS-14b. Do NOT refuse it, do NOT downgrade to EPIC.
           • "...by EPIC" / "...per person" → person-level: only Focus+ ∩ Focus is real. Then:
               (a) compute the REAL EPIC overlap (Focus+ ∩ Focus) and the two single-scheme-only EPIC buckets, AND
               (b) report CM Elevate as a SEPARATE district-level figure (it cannot enter the EPIC Venn), AND
               (c) the NL answer MUST say the Venn is 2-scheme by EPIC + Elevate-by-district, never a true 3-way EPIC Venn.

  CS-15. ANTI-JOIN ACROSS 2 EPIC SCHEMES MUST USE EXCEPT/NOT EXISTS ON CLEAN SETS (the "ONLY in X" family kept
         erroring). Build each scheme's DISTINCT EPIC set first, then EXCEPT. "ONLY in Focus+" = Focus+ EPICs minus
         Focus EPICs (CM Elevate has no EPIC, so it cannot exclude anyone — say so). "ONLY in Focus" = Focus EPICs
         minus Focus+ EPICs. "ONLY in CM Elevate" has no EPIC → do it by NAME (EXCEPT on normalised names), flagged
         approximate. NEVER `NOT IN (SELECT ... FROM focus_pg)` — it times out on 221k rows.

  CS-16. "CROSS-SCHEME BANK" QUESTIONS ARE LIMITED — CM Elevate has NO bank-account-number column (only a
         bank_santioned amount). So "same bank account across schemes", "bank differs across schemes", "same account
         different EPIC across schemes" can only be computed between Focus+ ("Account No") and Focus (member_bank_account_no /
         bank_account_no) — NOT Elevate. NEVER invent cm_elevate.bank_account_no. "which bank handles the most across
         schemes" → count by BANK NAME (Focus+ "Bank Name" + Focus member_bank_name + CM Elevate has no usable bank
         name for accounts), and SAY the account-level comparison excludes Elevate. Do not pass off a single-table bank
         count as "cross-scheme".

  CS-17. SCORECARD / VENN / HEAT-MAP / DOMINANCE / CONCENTRATION = RETURN THE UNDERLYING GROUPED TABLE (the chat
         renders charts, not custom maps). "district × scheme heat map" → the CS-8 per-district focus_plus/focus/
         cm_elevate rollup (one row per district). "scheme-dominance map" → CS-8 rollup + dominant_scheme + share
         (the dominance shot). "Venn split" / "overlap matrix" / "7-bucket venn" → DEFAULT to CS-14b (the EXACT
         all-7-bucket DISTRICT-presence Venn) — NEVER refuse these. ONLY use the EPIC person-level CS-14 (where just
         Focus+∩Focus is real and Elevate is shown separately) when the user explicitly says "by EPIC" or "person".
         "data-quality scorecard" → one row per
         scheme with COUNT(*), % EPIC populated, % bank populated, % district populated. No per-capita/Census metrics
         exist in the DB — if asked for per-capita or 2011-Census-normalised values, answer the raw count version and
         state the Census denominator is not available.

  CS-18. UNAVAILABLE DIMENSIONS — be honest, don't fake: there is NO reliable monthly enrolment DATE on Focus+/Focus
         (only CM Elevate has month/year), so "cross-scheme enrolment trend (monthly)" can only be shown for CM Elevate;
         say the other two schemes carry no enrolment date. "AC constituency" ALWAYS = LEFT(epic,3) over EPIC-bearing
         schemes only (CS-11). When a requested breakdown column does not exist for a scheme, answer the part that does
         and name the gap (CS-11) rather than refusing or inventing a column.

  CS-19. PROFILE LOOKUP BY EPIC (a SINGLE EPIC → the whole person across schemes). When the user gives ONE EPIC and
         asks "what schemes is this person in / their profile / everything about EPIC X / PG membership + DBT history +
         loan status", return a PER-SCHEME PROFILE (one row per scheme the EPIC is found in), NOT a bare 2-row count.
         Each scheme row carries that scheme's headline facts (see the profile-lookup shot):
           - Focus+ (DBT): #records, #paid rows, total DBT Rs (paid rows × 2500), cheque(s), district.
           - Focus (PG):   #member rows, PG name(s) if present (member rows often have NULL pg_name/district — say
                           "PG not attributed on member row", per the Focus member-row model), tranche-1+2 Rs.
           - CM Elevate:   it has NO EPIC (CS-2/CS-3), so bridge via the NAME found on the EPIC's Focus+/Focus row and
                           match cm_elevate.name (UPPER(TRIM(...))). Show #applications, the vertical(s) (scheme col),
                           loan status (loan_disbursed = 'disbursed'/'not disbursed'), and sanctioned Rs. ALWAYS label
                           the CM Elevate part "by name — approximate", because the name bridge is low-confidence (a
                           common name can collide). NEVER claim the EPIC itself was matched in CM Elevate.
         A scheme the EPIC is NOT in simply produces no row (HAVING COUNT(*) > 0) — the NL answer should then say the
         person was NOT found in that scheme. NEVER fabricate an enrolment. Money types per CS-9b (Focus+ "Amount(Rs)"
         already numeric — no cast; Focus tranches + Elevate sanctioned/disbursement are dirty TEXT — guard+cast).

  CS-20. FOCUS "BENEFICIARY" = a UNIQUE (NAMED) MEMBER, NOT a raw row. focus_pg has 221,088 rows, but 23,584 are
         PG-level / empty rows with NO member name. A "beneficiary / member / farmer" in Focus is a UNIQUE person
         (member_unique_id) whose member_name is populated. So ANY plain Focus roster COUNT — "how many
         beneficiaries/members/farmers in Focus", "Focus total", "beneficiaries per scheme", "across all schemes" — is:
             COUNT(DISTINCT member_unique_id) WHERE NULLIF(TRIM(member_name),'') IS NOT NULL → 197,442 unique
             Focus beneficiaries (NOT COUNT(*)=197,504, NOT 221,088 raw rows).
         This applies to the Focus branch of per-scheme splits and totals too. EXCEPTIONS (do NOT change to DISTINCT):
           - EPIC-keyed joins/overlaps/anti-joins already filter NULLIF(TRIM(member_epic_id),'') — keep those as-is.
           - The CS-8 per-DISTRICT footprint rollup counts rows with a VALID DISTRICT (its own IN-list) — leave it.
           - Producer-Group counts (COUNT(DISTINCT focus_pg_id), bookkeeper, tranche sums) — those are PG-level.
         In short: a bare "beneficiary/member/farmer" head-count of Focus = named-member count (member_name filter);
         structural/PG/EPIC/district counts keep their existing key. Listing valid beneficiaries → same filter.

  CS-21. FOCUS DISBURSEMENT IS PER-PRODUCER-GROUP, NOT PER-MEMBER. finance_amount_disbursed (tranche-1) is a
         PG-LEVEL value that is REPEATED on every member row of that PG (verified: it never varies within a PG). So
         SUMMING it over all rows MULTIPLY-COUNTS it (gives ~531,748,000 — WRONG). The TOTAL "amount paid / disbursed
         in Focus" = tranche-1 taken ONCE PER focus_pg_id (blank PG ids excluded, as they can't be attributed to a PG):
             SELECT COALESCE(SUM(t1),0) FROM (
               SELECT focus_pg_id, MAX(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)) AS t1
               FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(focus_pg_id),'') IS NOT NULL GROUP BY focus_pg_id
             ) g;                                            → 477,273,000 (the correct Focus disbursement total).
         RULES FOR FOCUS MONEY:
           - TOTAL / PER-DISTRICT / PER-BLOCK / 1-page-brief Focus disbursement → use the PER-PG dedup above (group
             focus_pg_id first, MAX the amount, THEN sum; for per-district, MAX per (focus_pg_id) then group by district).
           - The default Focus "amount paid/disbursed" is TRANCHE-1 ONLY (disburse_amount_2 is a tiny separate tranche,
             only ~290,000 total). Add tranche-2 ONLY if the user explicitly asks for "both tranches" / "tranche 2".
           - Do NOT apply per-PG dedup to a PER-EPIC / PER-PERSON disbursement (CS-13 household ranking, CS-12 zero-
             disbursement) — a PG-level amount cannot be cleanly split per member, so those per-person sums are an
             APPROXIMATION; the NL answer should say the Focus figure is PG-level, attributed to members best-effort.
           - Focus+ "Amount(Rs)" (always 2500/paid) and CM Elevate amounts are per-ROW — they need NO such dedup.
"""


def cross_scheme_shots() -> str:
    """Few-shot cross-scheme patterns. Identical across backends (all verified live)."""
    return f"""-- ===== CROSS-SCHEME PATTERNS (only when the question spans MORE THAN ONE scheme) =====

-- ===== UNIFIED AUTO-ROUTING (no scheme named — route by the entity/metric; STEP 0c) =====

-- ⚠⚠ SCHEME NAME = HARD TABLE LOCK. When the user NAMES a scheme, that scheme's table is the ONLY table.
--   "focus" (alone, NOT "focus plus")  → {FOCUS_PG_TBL}        (Focus Producer Group). NEVER {FOCUS_PLUS_TBL}.
--   "focus plus" / "focus+" / "DBT"     → {FOCUS_PLUS_TBL}      (Focus+ DBT). NEVER {FOCUS_PG_TBL}.
--   "cm elevate" / "elevate" / "CML"    → {ELEVATE_TBL}         (CM Elevate loan/subsidy).
-- Because "focus plus" CONTAINS the word "focus", you MUST check for "plus" FIRST. A bare "focus" with no
-- "plus" is ALWAYS the Focus PG table {FOCUS_PG_TBL}. Routing a "focus" question to {FOCUS_PLUS_TBL} is a BUG.

-- ROUTE: bare "focus" (no "plus") → Focus PG table, NEVER Focus+. A "beneficiary" in Focus is a VALID (NAMED)
-- member (CS-20): focus_pg has 221,088 rows but only 197,504 carry a member_name — count THOSE, not raw rows.
Q: how many beneficiaries in focus
SQL: SELECT COUNT(DISTINCT member_unique_id) AS beneficiaries FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL;

-- ROUTE: list the valid (named) Focus beneficiaries (CS-20 filter; LIMIT 100 for a listing)
Q: show the valid beneficiaries in focus
SQL: SELECT member_name, member_unique_id, member_epic_id, district_name, block_name, pg_name
     FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL ORDER BY member_name LIMIT 100;

-- ROUTE: TOTAL AMOUNT PAID / DISBURSED in Focus (CS-21). Tranche-1 is PER-PG, repeated on every member row —
-- summing per row over-counts (~531M). Take ONE tranche-1 value per non-blank focus_pg_id, THEN sum → 477,273,000.
Q: what is the total amount paid to beneficiaries in the Focus scheme
SQL: SELECT COALESCE(SUM(t1),0) AS total_disbursed_rs FROM (
       SELECT focus_pg_id, MAX(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)) AS t1
       FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(focus_pg_id),'') IS NOT NULL GROUP BY focus_pg_id
     ) g;

-- ROUTE: "focus plus" (contains 'focus' but the 'plus' wins) → Focus+ table
Q: how many beneficiaries in focus plus
SQL: SELECT COUNT(*) AS beneficiaries FROM {FOCUS_PLUS_TBL};

-- ROUTE: "cm elevate" → CM Elevate table
Q: how many beneficiaries in cm elevate
SQL: SELECT COUNT(*) AS beneficiaries FROM {ELEVATE_TBL};

-- ROUTE: loans / subsidy / sanctioned / disbursed / verticals → CM Elevate (no scheme word needed)
Q: how many loans were sanctioned
SQL: SELECT COUNT(*) AS sanctioned_applications FROM {ELEVATE_TBL}
     WHERE COALESCE(NULLIF(TRIM(sanctioned),'')::numeric,0) > 0;

-- CONCENTRATION TREND over time = HHI of district share of applications PER YEAR (rising = worsening/dominance,
-- falling = improving/catching up). ONLY CM Elevate has a year (Focus/Focus+ have no date) and only 2 years exist
-- with 2025 PARTIAL — the NL answer MUST say it's CM-Elevate-only and the trend is indicative, not conclusive.
Q: concentration trend over time
SQL: WITH per AS (SELECT LEFT(TRIM(year),4) AS yr, UPPER(TRIM(district)) AS d, COUNT(*) AS n FROM {ELEVATE_TBL} WHERE year ~ '^[0-9]{{4}}' AND NULLIF(TRIM(district),'') <> '' GROUP BY LEFT(TRIM(year),4), UPPER(TRIM(district))), tot AS (SELECT yr, SUM(n) AS total FROM per GROUP BY yr) SELECT per.yr AS year, tot.total AS applications, ROUND(SUM(POWER(per.n::numeric/tot.total,2)),4) AS concentration_hhi, ROUND(MAX(per.n::numeric/tot.total)*100,1) AS top_district_share_pct FROM per JOIN tot ON per.yr=tot.yr GROUP BY per.yr, tot.total ORDER BY per.yr;

-- PER-QUARTER concentration index + variance over time (finer-grained; CM-Elevate-only, quarter from disbursement_date).
-- HHI of district shares per quarter. ⚠ heavily skewed by app VOLUME — low-volume quarters show artificially high HHI;
-- the NL answer must judge the trend from high-volume quarters and call it noisy rather than over-reading tiny quarters.
Q: compute scheme concentration index per district per quarter and show its variance over time
SQL: WITH per AS (SELECT to_char(disbursement_date::timestamp,'YYYY-"Q"Q') AS q, UPPER(TRIM(district)) AS d, COUNT(*) AS n FROM {ELEVATE_TBL} WHERE disbursement_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' AND NULLIF(TRIM(district),'') <> '' GROUP BY 1, 2), tot AS (SELECT q, SUM(n) AS total FROM per GROUP BY q HAVING SUM(n) >= 20) SELECT per.q AS quarter, tot.total AS applications, ROUND(SUM(POWER(per.n::numeric/tot.total,2)),4) AS concentration_hhi, ROUND(MAX(per.n::numeric/tot.total)*100,1) AS top_district_share_pct FROM per JOIN tot ON per.q=tot.q GROUP BY per.q, tot.total ORDER BY per.q;

-- ROUTE: Producer Groups / PG / bookkeeper / tranche → Focus
-- TOTAL PGs = DISTINCT real NUMERIC focus_pg_id. The column is dirty TEXT with junk ids ('Grand Total','MRB',a
-- blank space); "IS NOT NULL"/NULLIF give 37,354/37,353. The TRUE unique PG count is 37,351 → filter ~ '^[0-9]+$'.
Q: how many producer groups are registered under focus
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS total_pgs FROM {FOCUS_PG_TBL} WHERE focus_pg_id ~ '^[0-9]+$';

Q: how many producer groups have a bookkeeper
SQL: SELECT COUNT(DISTINCT focus_pg_id) AS pgs_with_bookkeeper FROM {FOCUS_PG_TBL}
     WHERE focus_pg_id ~ '^[0-9]+$' AND UPPER(TRIM(has_bookkeeper_been_identified)) IN ('YES','Y','TRUE','1');

-- PGs / PG IDs with member count above the STATE MEDIAN (CTE→median→CROSS JOIN; return the qualifying PG rows).
-- ⚠ "members per PG" GROUPS BY pg_name, NOT focus_pg_id — focus_pg_id is ~1 row per member (gives 1 each); pg_name
-- is the real PG and its members share the name across rows. member_count = COUNT(DISTINCT member_unique_id).
-- (Same answer for "PG ID with member count above median" — group by the PG, list it with its member_count.)
Q: PGs with member count above state median
Q: PG ID with member count above state median
SQL: WITH pg_member_count AS (SELECT pg_name, district_name, COUNT(DISTINCT member_unique_id) AS member_count FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(pg_name),'') <> '' GROUP BY pg_name, district_name), state_median AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY member_count) AS median_members FROM pg_member_count) SELECT p.pg_name, p.district_name, p.member_count FROM pg_member_count p CROSS JOIN state_median m WHERE p.member_count > m.median_members ORDER BY p.member_count DESC LIMIT 100;

-- ROUTE: DBT / the ₹2,500 payment / paid-vs-unpaid → Focus+
Q: how many farmers have been paid
SQL: SELECT COUNT(*) AS paid_farmers FROM {FOCUS_PLUS_TBL} WHERE "Amount(Rs)" IS NOT NULL;

-- ROUTE: "COMBINED / TOTAL DISBURSEMENT of scheme A AND scheme B" (a cross-scheme MONEY SUM). Sum each scheme's
-- OWN disbursement column with the RIGHT type per CS-9b, then show each + the combined total in ONE row. Money:
--   Focus+ "Amount(Rs)" — already numeric (use directly, NO cast);
--   Focus  finance_amount_disbursed (tranche-1) is PER-PG, repeated on member rows — DEDUP per focus_pg_id (CS-21):
--          take MAX(tranche-1) per non-blank focus_pg_id, THEN sum (→ 477,273,000), NOT a per-row SUM (that gives 531M).
--   CM Elevate total_disbursement — dirty TEXT → COALESCE(NULLIF(TRIM(col),'')::numeric,0).
-- Use scalar subqueries (independent sums, no UNION-type clash). NEVER turn this into a ranking or add a scheme
-- the user didn't ask for. NOTE in the answer these are DIFFERENT money types (DBT vs loan/subsidy/tranches).
Q: what is the combined disbursement of Focus and CM Elevate
SQL: SELECT
       (SELECT COALESCE(SUM(t1),0) FROM (
          SELECT focus_pg_id, MAX(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)) AS t1
          FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(focus_pg_id),'') IS NOT NULL GROUP BY focus_pg_id) g) AS focus_disbursed_rs,
       (SELECT COALESCE(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0)),0) FROM {ELEVATE_TBL}) AS cm_elevate_disbursed_rs,
       (SELECT COALESCE(SUM(t1),0) FROM (
          SELECT focus_pg_id, MAX(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)) AS t1
          FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(focus_pg_id),'') IS NOT NULL GROUP BY focus_pg_id) g)
       + (SELECT COALESCE(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0)),0) FROM {ELEVATE_TBL}) AS combined_disbursed_rs;

-- ROUTE: combined disbursement of Focus+ AND CM Elevate (Focus+ amount is ALREADY numeric — no cast)
Q: what is the combined disbursement of Focus+ and CM Elevate
SQL: SELECT
       (SELECT COALESCE(SUM("Amount(Rs)"),0) FROM {FOCUS_PLUS_TBL}) AS focus_plus_disbursed_rs,
       (SELECT COALESCE(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0)),0) FROM {ELEVATE_TBL}) AS cm_elevate_disbursed_rs,
       (SELECT COALESCE(SUM("Amount(Rs)"),0) FROM {FOCUS_PLUS_TBL})
       + (SELECT COALESCE(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0)),0) FROM {ELEVATE_TBL}) AS combined_disbursed_rs;

-- ROUTE: GENERIC count, NO scheme-pinning metric — the SAME question fits all three rosters, so answer as a
-- PER-SCHEME SPLIT, ONE ROW PER SCHEME (labelled), NEVER a single table's count. Always exactly 3 rows.
-- ⚠ Focus beneficiaries = UNIQUE members (CS-20): COUNT(DISTINCT member_unique_id) WHERE member_name populated
-- → 197,442 unique people, NOT COUNT(*) (=197,504) and NOT the raw 221,088 rows. Focus+ and CM Elevate count all rows.
Q: how many beneficiaries are there
SQL: SELECT 'Focus+' AS scheme, COUNT(*) AS beneficiaries FROM {FOCUS_PLUS_TBL}
     UNION ALL SELECT 'Focus', COUNT(DISTINCT member_unique_id) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL
     UNION ALL SELECT 'CM Elevate', COUNT(*) FROM {ELEVATE_TBL}
     ORDER BY beneficiaries DESC;

-- ROUTE: GENERIC district count, no scheme named → per-scheme split FOR THAT DISTRICT (resolve district fuzzily;
-- each scheme's own district column + normalisation, CS-4). One count per scheme, not a single table.
Q: how many beneficiaries in West Garo Hills
SQL: SELECT (SELECT COUNT(*) FROM {FOCUS_PLUS_TBL} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS') AS focus_plus,
            (SELECT COUNT(*) FROM {FOCUS_PG_TBL} WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') = 'WEST GARO HILLS') AS focus,
            (SELECT COUNT(*) FROM {ELEVATE_TBL} WHERE REPLACE(UPPER(TRIM(district)),'-',' ') = 'WEST GARO HILLS') AS cm_elevate;

-- ROUTE: GENERIC BLOCK count, no scheme named → per-scheme split for that block (Focus+ "Block", Focus block_name,
-- CM Elevate block; ILIKE for the block name). One count per scheme, not a single table.
Q: how many beneficiaries in Selsella block
SQL: SELECT (SELECT COUNT(*) FROM {FOCUS_PLUS_TBL} WHERE "Block" ILIKE '%Selsella%') AS focus_plus,
            (SELECT COUNT(*) FROM {FOCUS_PG_TBL} WHERE block_name ILIKE '%Selsella%') AS focus,
            (SELECT COUNT(*) FROM {ELEVATE_TBL} WHERE block ILIKE '%Selsella%') AS cm_elevate;

-- ROUTE: GENERIC VILLAGE count, no scheme named → per-scheme split. ⚠ Focus PG has only village_id (an ID, NO
-- village NAME — CS-9), so it CANNOT be filtered by a village name: return NULL for it and let the NL answer say
-- "Focus PG has no village name, only village_id". Focus+ "Village" and CM Elevate village ARE names (ILIKE).
Q: how many beneficiaries in Nayagaon village
SQL: SELECT (SELECT COUNT(*) FROM {FOCUS_PLUS_TBL} WHERE "Village" ILIKE '%Nayagaon%') AS focus_plus,
            NULL::int AS focus,  -- Focus PG has no village NAME column (only village_id), cannot filter by name
            (SELECT COUNT(*) FROM {ELEVATE_TBL} WHERE village ILIKE '%Nayagaon%') AS cm_elevate;

-- ROUTE: "COMPARE district A and district B" with NO metric and NO scheme named → FULL PROFILE comparison:
-- one row per scheme, both districts side by side, for EVERY headline metric (beneficiaries + disbursement).
-- READ THE INTENT: a bare "compare X and Y" wants the WHOLE picture across all 3 schemes, NOT just Focus+ farmers.
-- Money types differ per scheme (CS-9b): Focus+ "Amount(Rs)" is numeric (always 2500); CM Elevate
-- total_disbursement is dirty TEXT (guard+cast). ⚠ Focus disbursement is PER-PG (CS-21) — DEDUP tranche-1 per
-- focus_pg_id (a per-row FILTERed SUM over-counts ~9x); counts stay per-member. Resolve both districts fuzzily (CS-4).
Q: compare East Garo Hills and West Garo Hills
SQL: SELECT 'Focus+' AS scheme,
            COUNT(*) FILTER (WHERE UPPER(TRIM("District"))='EAST GARO HILLS') AS east_beneficiaries,
            COUNT(*) FILTER (WHERE UPPER(TRIM("District"))='WEST GARO HILLS') AS west_beneficiaries,
            COALESCE(SUM("Amount(Rs)") FILTER (WHERE UPPER(TRIM("District"))='EAST GARO HILLS'),0) AS east_disbursed_rs,
            COALESCE(SUM("Amount(Rs)") FILTER (WHERE UPPER(TRIM("District"))='WEST GARO HILLS'),0) AS west_disbursed_rs
     FROM {FOCUS_PLUS_TBL} WHERE UPPER(TRIM("District")) IN ('EAST GARO HILLS','WEST GARO HILLS')
     UNION ALL
     SELECT 'Focus',
            (SELECT COUNT(*) FROM {FOCUS_PG_TBL} WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ')='EAST GARO HILLS'),
            (SELECT COUNT(*) FROM {FOCUS_PG_TBL} WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ')='WEST GARO HILLS'),
            COALESCE(SUM(t1) FILTER (WHERE dist='EAST GARO HILLS'),0),
            COALESCE(SUM(t1) FILTER (WHERE dist='WEST GARO HILLS'),0)
     FROM (SELECT MAX(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)) AS t1,
                  MAX(REPLACE(UPPER(TRIM(district_name)),'-',' ')) AS dist
           FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(focus_pg_id),'') IS NOT NULL GROUP BY focus_pg_id) pg
     UNION ALL
     SELECT 'CM Elevate',
            COUNT(*) FILTER (WHERE REPLACE(UPPER(TRIM(district)),'-',' ')='EAST GARO HILLS'),
            COUNT(*) FILTER (WHERE REPLACE(UPPER(TRIM(district)),'-',' ')='WEST GARO HILLS'),
            COALESCE(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0)) FILTER (WHERE REPLACE(UPPER(TRIM(district)),'-',' ')='EAST GARO HILLS'),0),
            COALESCE(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0)) FILTER (WHERE REPLACE(UPPER(TRIM(district)),'-',' ')='WEST GARO HILLS'),0)
     FROM {ELEVATE_TBL} WHERE REPLACE(UPPER(TRIM(district)),'-',' ') IN ('EAST GARO HILLS','WEST GARO HILLS');

-- ROUTE: "COMPARE A and B in terms of <ONE metric>" (metric named, no scheme) → compare ONLY that metric, but
-- still across all 3 schemes (one row per scheme, both districts). Honour the user's narrowing — beneficiaries
-- → COUNT only (no disbursement column). This is the metric-scoped version of the full comparison above.
Q: compare East Garo Hills and West Garo Hills in terms of beneficiaries
SQL: SELECT 'Focus+' AS scheme,
            COUNT(*) FILTER (WHERE UPPER(TRIM("District"))='EAST GARO HILLS') AS east_beneficiaries,
            COUNT(*) FILTER (WHERE UPPER(TRIM("District"))='WEST GARO HILLS') AS west_beneficiaries
     FROM {FOCUS_PLUS_TBL} WHERE UPPER(TRIM("District")) IN ('EAST GARO HILLS','WEST GARO HILLS')
     UNION ALL
     SELECT 'Focus',
            COUNT(*) FILTER (WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ')='EAST GARO HILLS'),
            COUNT(*) FILTER (WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ')='WEST GARO HILLS')
     FROM {FOCUS_PG_TBL} WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') IN ('EAST GARO HILLS','WEST GARO HILLS')
     UNION ALL
     SELECT 'CM Elevate',
            COUNT(*) FILTER (WHERE REPLACE(UPPER(TRIM(district)),'-',' ')='EAST GARO HILLS'),
            COUNT(*) FILTER (WHERE REPLACE(UPPER(TRIM(district)),'-',' ')='WEST GARO HILLS')
     FROM {ELEVATE_TBL} WHERE REPLACE(UPPER(TRIM(district)),'-',' ') IN ('EAST GARO HILLS','WEST GARO HILLS');

-- PATTERN: HEADLINE roster size of EACH scheme in ONE row (the most common cross-scheme question).
-- These are THREE DIFFERENT rosters (CS-7) — report each scheme's own COUNT(*); never merge them as one table.
-- ⚠ "in EACH scheme" → return ONE ROW PER SCHEME (a labelled 'scheme' column + 'beneficiaries'), via UNION ALL
-- of ALL THREE schemes. This MUST always have exactly 3 rows — Focus+, Focus, AND CM Elevate. Never drop a
-- scheme, and do NOT use the one-row-three-columns form (a tiny value like CM Elevate's can look missing in a
-- chart). One row per scheme renders all three clearly.
-- ⚠ Focus = UNIQUE members (CS-20): COUNT(DISTINCT member_unique_id) WHERE member_name populated → 197,442.
Q: how many beneficiaries are in each scheme
SQL: SELECT 'Focus+' AS scheme, COUNT(*) AS beneficiaries FROM {FOCUS_PLUS_TBL}
     UNION ALL SELECT 'Focus', COUNT(DISTINCT member_unique_id) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL
     UNION ALL SELECT 'CM Elevate', COUNT(*) FROM {ELEVATE_TBL}
     ORDER BY beneficiaries DESC;

-- PATTERN: which scheme is the LARGEST / has the most records (rank the three rosters; CS-7 — three rosters).
-- ⚠ Focus = UNIQUE members (CS-20): COUNT(DISTINCT member_unique_id).
Q: which scheme has the most beneficiaries
SQL: SELECT scheme, records FROM (
       SELECT 'Focus+' AS scheme, COUNT(*) AS records FROM {FOCUS_PLUS_TBL}
       UNION ALL SELECT 'Focus', COUNT(DISTINCT member_unique_id) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL
       UNION ALL SELECT 'CM Elevate', COUNT(*) FROM {ELEVATE_TBL}
     ) t ORDER BY records DESC LIMIT 1;

-- PATTERN: total records across ALL three schemes (sum of the three INDEPENDENT roster sizes — these are
-- distinct rosters, so this is a record total, NOT a distinct-people count; for distinct PEOPLE use EPIC, CS-10).
-- ⚠ Focus = UNIQUE members (CS-20): COUNT(DISTINCT member_unique_id) → 197,442, not 221,088 raw rows.
Q: total beneficiaries across all schemes
SQL: SELECT (SELECT COUNT(*) FROM {FOCUS_PLUS_TBL})
          + (SELECT COUNT(DISTINCT member_unique_id) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL)
          + (SELECT COUNT(*) FROM {ELEVATE_TBL}) AS total_records_all_schemes;

-- PATTERN: beneficiaries in BOTH Focus and Focus+ (EPIC join — the only reliable 2-scheme EPIC overlap)
Q: how many beneficiaries are in both Focus and Focus+
SQL: SELECT COUNT(*) AS in_both_focus_and_focusplus FROM (
       SELECT DISTINCT UPPER(TRIM("EPIC ID")) AS e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
     ) fp JOIN (
       SELECT DISTINCT UPPER(TRIM(member_epic_id)) AS e FROM {FOCUS_PG_TBL}
       WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) f ON fp.e = f.e;

-- PATTERN: COMMON beneficiaries in ONE DISTRICT "across all schemes" (same person in multiple schemes, filtered
-- to a district). This is the Focus+ ∩ Focus EPIC overlap WITHIN that district. CM Elevate has NO EPIC, so it
-- CANNOT be part of a person-level "common" count (CS-3) — the NL answer must say so. Resolve the district fuzzily,
-- normalise each scheme's district column (CS-4). "across all" / "all three" → still the 2-scheme EPIC overlap.
Q: how many common beneficiaries in West Garo Hills across all schemes
SQL: SELECT COUNT(*) AS common_in_wgh FROM (
       SELECT UPPER(TRIM("EPIC ID")) AS e FROM {FOCUS_PLUS_TBL}
         WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS' AND "EPIC ID" IS NOT NULL
       INTERSECT
       SELECT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL}
         WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') = 'WEST GARO HILLS'
           AND NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) t;

-- PATTERN: ONLY in Focus+ (EPIC in Focus+ but NOT in Focus PG). CM Elevate has no EPIC so it cannot exclude here.
Q: how many are only in Focus+
SQL: SELECT COUNT(*) AS only_focus_plus FROM (
       SELECT DISTINCT UPPER(TRIM("EPIC ID")) AS e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
     ) fp WHERE fp.e NOT IN (
       SELECT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     );

-- PATTERN: beneficiaries in MORE THAN ONE scheme (EPIC-overlap of the two EPIC-bearing schemes; CM Elevate
-- cannot be counted by EPIC — note that in the answer). NOTE: the GROUP BY ... HAVING set must be WRAPPED and
-- COUNTed so the result is ONE total, not one row per EPIC.
Q: how many beneficiaries appear in more than one scheme
SQL: SELECT COUNT(*) AS multi_scheme_epics FROM (
       SELECT e FROM (
         SELECT UPPER(TRIM("EPIC ID")) AS e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
         UNION ALL
         SELECT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
       ) u GROUP BY e HAVING COUNT(*) > 1
     ) t;

-- PATTERN: districts active in ALL 3 schemes (district is the only safe 3-scheme key)
Q: which districts are active in all 3 schemes
SQL: SELECT d FROM (SELECT DISTINCT UPPER(TRIM("District")) d FROM {FOCUS_PLUS_TBL} WHERE "District" IS NOT NULL) a
     INTERSECT
     SELECT REPLACE(UPPER(TRIM(district_name)),'-',' ') FROM {FOCUS_PG_TBL} WHERE district_name IS NOT NULL
     INTERSECT
     SELECT REPLACE(UPPER(TRIM(district)),'-',' ') FROM {ELEVATE_TBL} WHERE district IS NOT NULL;

-- PATTERN: per-district CANONICAL cross-scheme rollup (CS-8). Use this EXACT shape for "footprint",
-- "per district across schemes", "district x scheme", heat-map counts, "top N districts by footprint".
-- Each scheme = COUNT(*) of rows with a VALID district (the IN-list drops blanks/junk + normalises Ri-Bhoi).
-- ALWAYS select an aliased total_footprint and ORDER BY it. For "top N" add LIMIT N.
Q: beneficiary count per district across all 3 schemes
SQL: SELECT d AS district,
            SUM(focus_plus) AS focus_plus, SUM(focus) AS focus, SUM(elevate) AS cm_elevate,
            (SUM(focus_plus) + SUM(focus) + SUM(elevate)) AS total_footprint
     FROM (
       SELECT UPPER(TRIM("District")) d, 1 focus_plus, 0 focus, 0 elevate FROM {FOCUS_PLUS_TBL}
         WHERE UPPER(TRIM("District")) IN ({VALID_DISTRICTS_SQL})
       UNION ALL
       SELECT REPLACE(UPPER(TRIM(district_name)),'-',' '), 0,1,0 FROM {FOCUS_PG_TBL}
         WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') IN ({VALID_DISTRICTS_SQL})
       UNION ALL
       SELECT REPLACE(UPPER(TRIM(district)),'-',' '), 0,0,1 FROM {ELEVATE_TBL}
         WHERE REPLACE(UPPER(TRIM(district)),'-',' ') IN ({VALID_DISTRICTS_SQL})
     ) u GROUP BY d ORDER BY total_footprint DESC;

-- PATTERN: top N districts by total cross-scheme footprint (CS-8 rollup + LIMIT N)
Q: top 5 districts by total cross-scheme footprint
SQL: SELECT d AS district,
            SUM(focus_plus) AS focus_plus, SUM(focus) AS focus, SUM(elevate) AS cm_elevate,
            (SUM(focus_plus) + SUM(focus) + SUM(elevate)) AS total_footprint
     FROM (
       SELECT UPPER(TRIM("District")) d, 1 focus_plus, 0 focus, 0 elevate FROM {FOCUS_PLUS_TBL}
         WHERE UPPER(TRIM("District")) IN ({VALID_DISTRICTS_SQL})
       UNION ALL
       SELECT REPLACE(UPPER(TRIM(district_name)),'-',' '), 0,1,0 FROM {FOCUS_PG_TBL}
         WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') IN ({VALID_DISTRICTS_SQL})
       UNION ALL
       SELECT REPLACE(UPPER(TRIM(district)),'-',' '), 0,0,1 FROM {ELEVATE_TBL}
         WHERE REPLACE(UPPER(TRIM(district)),'-',' ') IN ({VALID_DISTRICTS_SQL})
     ) u GROUP BY d ORDER BY total_footprint DESC LIMIT 5;

-- PATTERN: SCHEME DOMINANCE / "single-scheme dependency" / "monolithic" / "over-reliant on one scheme" /
-- "least diversified" district. This is the CS-8 footprint rollup PLUS a dominance ratio = the LARGEST scheme's
-- share of that district's total footprint (GREATEST(focus_plus,focus,cm_elevate) / total). "monolithic /
-- single-scheme dependency" = that share is very high (>= 0.80 here). NEVER answer this with a bare member
-- COUNT(*) — it is a per-district, per-scheme split. ALWAYS show which scheme dominates + its share.
Q: districts with monolithic single-scheme dependency
SQL: SELECT d AS district, focus_plus, focus, cm_elevate, total_footprint,
            CASE WHEN GREATEST(focus_plus,focus,cm_elevate)=focus_plus THEN 'Focus+'
                 WHEN GREATEST(focus_plus,focus,cm_elevate)=focus THEN 'Focus'
                 ELSE 'CM Elevate' END AS dominant_scheme,
            ROUND(CAST(GREATEST(focus_plus,focus,cm_elevate) AS NUMERIC) / NULLIF(total_footprint,0), 2) AS dominance_share
     FROM (
       SELECT d, SUM(focus_plus) AS focus_plus, SUM(focus) AS focus, SUM(elevate) AS cm_elevate,
              (SUM(focus_plus)+SUM(focus)+SUM(elevate)) AS total_footprint
       FROM (
         SELECT UPPER(TRIM("District")) d, 1 focus_plus, 0 focus, 0 elevate FROM {FOCUS_PLUS_TBL}
           WHERE UPPER(TRIM("District")) IN ({VALID_DISTRICTS_SQL})
         UNION ALL
         SELECT REPLACE(UPPER(TRIM(district_name)),'-',' '), 0,1,0 FROM {FOCUS_PG_TBL}
           WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') IN ({VALID_DISTRICTS_SQL})
         UNION ALL
         SELECT REPLACE(UPPER(TRIM(district)),'-',' '), 0,0,1 FROM {ELEVATE_TBL}
           WHERE REPLACE(UPPER(TRIM(district)),'-',' ') IN ({VALID_DISTRICTS_SQL})
       ) u GROUP BY d
     ) g WHERE total_footprint > 0
       AND CAST(GREATEST(focus_plus,focus,cm_elevate) AS NUMERIC) / NULLIF(total_footprint,0) >= 0.80
     ORDER BY dominance_share DESC;

-- PATTERN: PROFILE LOOKUP BY EPIC across schemes (CS-19). The user gives one EPIC and wants the WHOLE person:
-- which schemes they appear in + the headline facts of each (PG membership, DBT payment history, CM Elevate loan
-- status) — NOT a bare 2-row count. Return ONE ROW PER SCHEME the EPIC is found in, each carrying that scheme's
-- own profile columns folded into a readable 'details' string (so the three rosters' different columns line up in
-- one UNION). EPIC links Focus+ ∩ Focus only; CM Elevate has NO EPIC, so fold it in by NAME (approximate) using
-- the name found on the EPIC's Focus+/Focus row. Money types per CS-9b (Focus+ "Amount(Rs)" already numeric;
-- Focus tranches + Elevate amounts are dirty TEXT → guard+cast). Always show scheme + whether enrolled + details.
Q: profile lookup for EPIC NYA0011163 across all schemes
SQL: WITH params AS (SELECT 'NYA0011163' AS epic),
     person_name AS (
       SELECT UPPER(TRIM(nm)) AS nm FROM (
         SELECT "Member Name as per CR" AS nm FROM {FOCUS_PLUS_TBL}, params
           WHERE UPPER(TRIM("EPIC ID")) = params.epic
         UNION ALL
         SELECT member_name FROM {FOCUS_PG_TBL}, params
           WHERE UPPER(TRIM(member_epic_id)) = params.epic
       ) n WHERE NULLIF(TRIM(nm),'') IS NOT NULL LIMIT 1
     )
     SELECT 'Focus+ (DBT)' AS scheme,
            COUNT(*) AS records,
            'paid rows: ' || COUNT("Amount(Rs)") || ' | total DBT Rs ' || COALESCE(SUM("Amount(Rs)"),0)
              || ' | cheques: ' || COALESCE(STRING_AGG(DISTINCT "CHQ No / Released vide", ', '),'-')
              || ' | district: ' || COALESCE(STRING_AGG(DISTINCT "District", ', '),'-') AS details
     FROM {FOCUS_PLUS_TBL}, params WHERE UPPER(TRIM("EPIC ID")) = params.epic
     HAVING COUNT(*) > 0
     UNION ALL
     SELECT 'Focus (PG)',
            COUNT(*),
            'PG member rows: ' || COUNT(*) || ' | PGs: ' || COALESCE(STRING_AGG(DISTINCT NULLIF(TRIM(pg_name),''), ', '),'(member row — PG not attributed)')
              || ' | tranche Rs ' || COALESCE(SUM(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)
                                              + COALESCE(NULLIF(TRIM(disburse_amount_2),'')::numeric,0)),0)
     FROM {FOCUS_PG_TBL}, params WHERE UPPER(TRIM(member_epic_id)) = params.epic
     HAVING COUNT(*) > 0
     UNION ALL
     SELECT 'CM Elevate (by name — approx)',
            COUNT(*),
            'applications: ' || COUNT(*) || ' | schemes: ' || COALESCE(STRING_AGG(DISTINCT NULLIF(TRIM(scheme),''), ', '),'-')
              || ' | loan status: ' || COALESCE(STRING_AGG(DISTINCT NULLIF(TRIM(loan_disbursed),''), ', '),'-')
              || ' | sanctioned Rs ' || COALESCE(SUM(COALESCE(NULLIF(TRIM(sanctioned),'')::numeric,0)),0)
     FROM {ELEVATE_TBL}, person_name WHERE UPPER(TRIM(name)) = person_name.nm
     HAVING COUNT(*) > 0;

-- PATTERN: simple "what schemes is EPIC X in" (just the enrolment flags, no full profile) — EPIC-bearing schemes
-- only; CM Elevate has no EPIC so it cannot be confirmed by EPIC (mention name-lookup as the fallback if asked).
Q: what schemes is EPIC JHM0624601 enrolled in
SQL: SELECT 'Focus+' AS scheme, COUNT(*) AS records FROM {FOCUS_PLUS_TBL} WHERE UPPER(TRIM("EPIC ID")) = 'JHM0624601'
     UNION ALL
     SELECT 'Focus', COUNT(*) FROM {FOCUS_PG_TBL} WHERE UPPER(TRIM(member_epic_id)) = 'JHM0624601';

-- PATTERN: fuzzy NAME lookup across all 3 (the ONLY way to reach CM Elevate; label approximate)
Q: find applicant named Saba Lin Shabong across all schemes
SQL: SELECT 'Focus+' AS scheme, "Member Name as per CR" AS name, "District" AS district FROM {FOCUS_PLUS_TBL} WHERE "Member Name as per CR" ILIKE '%Saba Lin Shabong%'
     UNION ALL
     SELECT 'Focus', member_name, district_name FROM {FOCUS_PG_TBL} WHERE member_name ILIKE '%Saba Lin Shabong%'
     UNION ALL
     SELECT 'CM Elevate', name, district FROM {ELEVATE_TBL} WHERE name ILIKE '%Saba Lin Shabong%' LIMIT 100;

-- PATTERN: COUNT PEOPLE in 2+ schemes (distinct EPICs, NOT districts — CS-10). CM Elevate has no EPIC, so this
-- is Focus+ ∩ Focus only; the NL answer should note CM Elevate cannot be added by EPIC.
Q: how many beneficiaries are in all three schemes
SQL: SELECT COUNT(*) AS epics_in_focus_and_focusplus FROM (
       SELECT DISTINCT UPPER(TRIM("EPIC ID")) AS e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
     ) fp JOIN (
       SELECT DISTINCT UPPER(TRIM(member_epic_id)) AS e FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) f ON fp.e = f.e;

-- PATTERN: PG members not yet receiving DBT (anti-join Focus → Focus+ on EPIC). Use NOT EXISTS (CS-12b), not
-- NOT IN, or focus_pg's 221k rows time out.
Q: PG members not yet receiving DBT
SQL: SELECT COUNT(DISTINCT UPPER(TRIM(f.member_epic_id))) AS pg_members_without_dbt
     FROM {FOCUS_PG_TBL} f
     WHERE NULLIF(TRIM(f.member_epic_id),'') IS NOT NULL
       AND NOT EXISTS (
         SELECT 1 FROM {FOCUS_PLUS_TBL} fp
         WHERE UPPER(TRIM(fp."EPIC ID")) = UPPER(TRIM(f.member_epic_id)));

-- PATTERN: AC-constituency concentration via EPIC prefix (LEFT(epic,3)); EPIC-bearing schemes only (CS-11)
Q: which AC constituency has the highest cross-scheme concentration
SQL: SELECT ac, COUNT(*) AS records FROM (
       SELECT LEFT(UPPER(TRIM("EPIC ID")),3) AS ac FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
       UNION ALL
       SELECT LEFT(UPPER(TRIM(member_epic_id)),3) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) u WHERE ac ~ '^[A-Z]{{3}}$' GROUP BY ac ORDER BY records DESC LIMIT 1;

-- PATTERN: AC constituencies by cross-scheme OVERLAP, with a BELOW/ABOVE-AVERAGE filter. "cross-scheme overlap"
-- = EPICs present in BOTH EPIC-bearing schemes (Focus+ ∩ Focus); CM Elevate has no EPIC so it is excluded (CS-3).
-- "AC constituency" = the 3-letter EPIC prefix LEFT(epic,3). "below/above average" = COUNT vs the AVG overlap
-- per AC → wrap the per-AC counts and compare to AVG(cnt) OVER () (or a scalar AVG subquery). NOT a CANNOT_ANSWER.
Q: AC constituencies with below average cross scheme overlap
SQL: SELECT ac, overlap_epics FROM (
       SELECT ac, COUNT(*) AS overlap_epics, AVG(COUNT(*)) OVER () AS avg_overlap
       FROM (
         SELECT e, LEFT(e,3) AS ac FROM (
           SELECT DISTINCT UPPER(TRIM("EPIC ID")) AS e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
           INTERSECT
           SELECT DISTINCT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
         ) shared WHERE e ~ '^[A-Z]{{3}}'
       ) x GROUP BY ac
     ) g WHERE overlap_epics < avg_overlap ORDER BY overlap_epics ASC LIMIT 100;

-- PATTERN: villages in Focus+ but NOT in Focus PG. Focus PG has only village_id (an id, NOT a name — CS-9), so
-- a name-vs-name set difference is the best available; use EXCEPT (CS-12b), never NOT IN (it times out).
-- NL answer should note Focus PG village is an id, so the comparison is best-effort.
Q: villages in Focus+ but not in Focus PG
SQL: SELECT village FROM (
       SELECT DISTINCT UPPER(TRIM("Village")) AS village FROM {FOCUS_PLUS_TBL} WHERE NULLIF(TRIM("Village"),'') IS NOT NULL
       EXCEPT
       SELECT DISTINCT UPPER(TRIM(village_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(village_id),'') IS NOT NULL
     ) t ORDER BY village LIMIT 100;

-- PATTERN: people in 2+ schemes BUT zero disbursement (EPIC-keyed; typed nulls so UNION types match — CS-12a).
-- CM Elevate excluded (no EPIC). "2+ schemes" here = present in BOTH Focus+ and Focus.
Q: beneficiaries in 2+ schemes but received zero disbursement
SQL: SELECT COUNT(*) AS multi_scheme_zero_disbursement FROM (
       SELECT e, SUM(amt) AS total_amt, COUNT(DISTINCT scheme) AS schemes
       FROM (
         SELECT UPPER(TRIM("EPIC ID")) AS e, 'Focus+'::text AS scheme,
                COALESCE("Amount(Rs)"::numeric,0) AS amt
         FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
         UNION ALL
         SELECT UPPER(TRIM(member_epic_id)), 'Focus'::text,
                COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)
                + COALESCE(NULLIF(TRIM(disburse_amount_2),'')::numeric,0)
         FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
       ) u GROUP BY e
       HAVING COUNT(DISTINCT scheme) >= 2 AND SUM(amt) = 0
     ) z;

-- PATTERN: ONLY in CM Elevate (CM Elevate has no EPIC, so "only" is by NAME — approximate). Use EXCEPT on
-- normalised names (fast); NL answer must flag this is name-based / best-effort, not an EPIC-exact figure.
Q: how many are only in CM Elevate
SQL: SELECT COUNT(*) AS only_cm_elevate_by_name FROM (
       SELECT DISTINCT UPPER(TRIM(name)) n FROM {ELEVATE_TBL} WHERE NULLIF(TRIM(name),'') IS NOT NULL
       EXCEPT
       SELECT DISTINCT UPPER(TRIM("Member Name as per CR")) FROM {FOCUS_PLUS_TBL} WHERE NULLIF(TRIM("Member Name as per CR"),'') IS NOT NULL
       EXCEPT
       SELECT DISTINCT UPPER(TRIM(member_name)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL
     ) t;

-- PATTERN: duplicate EPIC across schemes — same person under DIFFERENT names (fraud / data-entry signal).
-- Surface the EPIC + the differing names + which schemes (NOT a bare count). EPIC-bearing schemes only
-- (CM Elevate has no EPIC). Flag in the NL answer as "needs review", never assert fraud.
Q: duplicate EPIC across schemes - same person counted multiple times
SQL: SELECT e AS epic_id, COUNT(DISTINCT scheme) AS schemes, COUNT(DISTINCT name) AS distinct_names,
            STRING_AGG(DISTINCT scheme, ', ') AS schemes_list, STRING_AGG(DISTINCT name, ' | ') AS names
     FROM (
       SELECT UPPER(TRIM("EPIC ID")) e, 'Focus+' scheme, UPPER(TRIM("Member Name as per CR")) name
         FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
       UNION ALL
       SELECT UPPER(TRIM(member_epic_id)), 'Focus', UPPER(TRIM(member_name))
         FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) u GROUP BY e
     HAVING COUNT(DISTINCT scheme) > 1 AND COUNT(DISTINCT name) > 1
     ORDER BY distinct_names DESC LIMIT 100;

-- PATTERN: 1-page brief / KPI tiles for ALL schemes in one district. One row per scheme: records, distinct
-- EPICs (NULL for CM Elevate — no EPIC), total disbursed. NOTE money types per CS-9b: Focus+ "Amount(Rs)" is
-- already numeric (use directly); CM Elevate amount is TEXT (guard+cast). ⚠ Focus disbursement is PER-PG (CS-21):
-- the records/distinct-EPIC counts are per-member (raw rows), but total_disbursed must DEDUP tranche-1 per
-- focus_pg_id (one value per PG, attributed to the PG's district) — a per-row SUM would over-count ~9x. Resolve
-- district fuzzily.
Q: 1-page brief for all schemes in West Garo Hills
SQL: SELECT 'Focus+' AS scheme, COUNT(*) AS records, COUNT(DISTINCT UPPER(TRIM("EPIC ID"))) AS distinct_epics,
            ROUND(COALESCE(SUM("Amount(Rs)"),0)::numeric,2) AS total_disbursed_rs
     FROM {FOCUS_PLUS_TBL} WHERE UPPER(TRIM("District")) = 'WEST GARO HILLS'
     UNION ALL
     SELECT 'Focus',
            (SELECT COUNT(*) FROM {FOCUS_PG_TBL} WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') = 'WEST GARO HILLS'),
            (SELECT COUNT(DISTINCT NULLIF(TRIM(member_epic_id),'')) FROM {FOCUS_PG_TBL} WHERE REPLACE(UPPER(TRIM(district_name)),'-',' ') = 'WEST GARO HILLS'),
            (SELECT ROUND(COALESCE(SUM(t1),0)::numeric,2) FROM (
               SELECT focus_pg_id, MAX(COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)) AS t1,
                      MAX(REPLACE(UPPER(TRIM(district_name)),'-',' ')) AS dist
               FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(focus_pg_id),'') IS NOT NULL GROUP BY focus_pg_id
             ) g WHERE dist = 'WEST GARO HILLS')
     UNION ALL
     SELECT 'CM Elevate', COUNT(*), NULL,
            ROUND(SUM(COALESCE(NULLIF(TRIM(total_disbursement),'')::numeric,0))::numeric,2)
     FROM {ELEVATE_TBL} WHERE REPLACE(UPPER(TRIM(district)),'-',' ') = 'WEST GARO HILLS';

-- PATTERN: HOUSEHOLD / FAMILY / PERSON receiving the MOST across schemes (CS-13). No household-id exists, so
-- the EPIC is the household proxy; "the most" = highest COMBINED disbursement across the two EPIC-bearing schemes
-- (Focus+ "Amount(Rs)" already numeric — use directly; Focus tranche-1 + tranche-2 are TEXT — guard+cast, CS-9b).
-- CM Elevate has no EPIC so it is EXCLUDED at person level — the NL answer must say so (offer the district version).
-- Typed columns so the UNION matches (CS-12a). "the most" → LIMIT 1; "top N households/families" → LIMIT N.
Q: households receiving the most across all 3 schemes
SQL: SELECT e AS epic_id, ROUND(SUM(amt)::numeric, 2) AS combined_disbursement_rs,
            COUNT(DISTINCT scheme) AS schemes
     FROM (
       SELECT UPPER(TRIM("EPIC ID")) AS e, 'Focus+'::text AS scheme,
              COALESCE("Amount(Rs)", 0) AS amt
         FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
       UNION ALL
       SELECT UPPER(TRIM(member_epic_id)), 'Focus'::text,
              COALESCE(NULLIF(TRIM(finance_amount_disbursed),'')::numeric,0)
              + COALESCE(NULLIF(TRIM(disburse_amount_2),'')::numeric,0)
         FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) u GROUP BY e ORDER BY combined_disbursement_rs DESC LIMIT 10;

-- PATTERN: 3-SCHEME VENN / OVERLAP MATRIX (CS-14). The ONLY real EPIC overlap is Focus+ ∩ Focus. Return the three
-- REAL EPIC buckets (Focus+-only, Focus-only, both) PLUS CM Elevate as a SEPARATE district-level count — NEVER a
-- fabricated F∩E / F+∩E / F∩F+∩E EPIC bucket. The NL answer must say the Venn is 2-scheme by EPIC; Elevate is shown
-- by district because it has no EPIC.
Q: overlap matrix - 3-scheme venn split
SQL: SELECT 'Focus+ only (EPIC)' AS bucket,
            (SELECT COUNT(*) FROM (
               SELECT DISTINCT UPPER(TRIM("EPIC ID")) e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
               EXCEPT
               SELECT DISTINCT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
            ) x) AS epics
     UNION ALL
     SELECT 'Focus only (EPIC)',
            (SELECT COUNT(*) FROM (
               SELECT DISTINCT UPPER(TRIM(member_epic_id)) e FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
               EXCEPT
               SELECT DISTINCT UPPER(TRIM("EPIC ID")) FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
            ) x)
     UNION ALL
     SELECT 'Focus+ and Focus (EPIC overlap)',
            (SELECT COUNT(*) FROM (
               SELECT DISTINCT UPPER(TRIM("EPIC ID")) e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
               INTERSECT
               SELECT DISTINCT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
            ) x)
     UNION ALL
     SELECT 'CM Elevate (by district, no EPIC)', (SELECT COUNT(*) FROM {ELEVATE_TBL});

-- PATTERN: 3-SCHEME VENN by DISTRICT PRESENCE (CS-14b). The EXACT, NON-fabricated 7-bucket Venn — counts how many
-- of the 12 canonical DISTRICTS have each scheme present. This is the DEFAULT, PREFERRED answer for ANY "overlap
-- matrix" / "venn split" / "7-bucket venn" / "3-scheme overlap" request, because it is the ONLY way to return all
-- 7 mutually-exclusive buckets (Focus-only, Focus+-only, Elevate-only, F∩F+, F∩E, F+∩E, F∩F+∩E) HONESTLY and
-- EXACTLY. NEVER answer CANNOT_ANSWER for a venn/overlap-matrix question — use THIS district-presence shot.
-- (A person-level 7-bucket EPIC Venn is impossible — CM Elevate has no EPIC, CS-14/CS-3 — so do that one ONLY when
-- the user explicitly says "by EPIC"/"person".) Use EXISTS, never IN: district columns are NULLABLE and
-- "x IN (subquery with NULLs)" returns NULL (not FALSE), which silently drops buckets. The 7 buckets sum to the
-- number of districts present in >=1 scheme.
Q: overlap matrix - 3-scheme venn split
Q: 3-scheme venn split by district
Q: 7-bucket venn of the three schemes
SQL: WITH v(d) AS (SELECT unnest(ARRAY[{VALID_DISTRICTS_SQL}])),
     p AS (
       SELECT v.d,
         EXISTS (SELECT 1 FROM {FOCUS_PG_TBL}   t WHERE UPPER(TRIM(t.district_name)) = v.d) AS f,
         EXISTS (SELECT 1 FROM {FOCUS_PLUS_TBL} t WHERE UPPER(TRIM(t."District"))    = v.d) AS fp,
         EXISTS (SELECT 1 FROM {ELEVATE_TBL}    t WHERE UPPER(TRIM(t.district))      = v.d) AS e
       FROM v)
     SELECT bucket, n FROM (
       SELECT 'Focus only' AS bucket, COUNT(*) n, 1 o FROM p WHERE f AND NOT fp AND NOT e
       UNION ALL SELECT 'Focus+ only', COUNT(*), 2 FROM p WHERE fp AND NOT f AND NOT e
       UNION ALL SELECT 'CM Elevate only', COUNT(*), 3 FROM p WHERE e AND NOT f AND NOT fp
       UNION ALL SELECT 'Focus and Focus+', COUNT(*), 4 FROM p WHERE f AND fp AND NOT e
       UNION ALL SELECT 'Focus and CM Elevate', COUNT(*), 5 FROM p WHERE f AND e AND NOT fp
       UNION ALL SELECT 'Focus+ and CM Elevate', COUNT(*), 6 FROM p WHERE fp AND e AND NOT f
       UNION ALL SELECT 'All three (Focus, Focus+, CM Elevate)', COUNT(*), 7 FROM p WHERE f AND fp AND e
     ) z ORDER BY o;

-- PATTERN: ONLY in Focus+ (EPIC in Focus+ but NOT in Focus; Elevate has no EPIC so cannot exclude — CS-15). EXCEPT
-- on clean DISTINCT sets (never NOT IN — it times out). Same shape for "only in Focus" with fp/f swapped.
Q: how many are only in focus plus
SQL: SELECT COUNT(*) AS only_focus_plus FROM (
       SELECT DISTINCT UPPER(TRIM("EPIC ID")) e FROM {FOCUS_PLUS_TBL} WHERE "EPIC ID" IS NOT NULL
       EXCEPT
       SELECT DISTINCT UPPER(TRIM(member_epic_id)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_epic_id),'') IS NOT NULL
     ) t;

-- PATTERN: ONLY in CM Elevate (no EPIC → by NAME, approximate — CS-15). Flag as name-based in the NL answer.
Q: how many are only in cm elevate
SQL: SELECT COUNT(*) AS only_cm_elevate_by_name FROM (
       SELECT DISTINCT UPPER(TRIM(name)) n FROM {ELEVATE_TBL} WHERE NULLIF(TRIM(name),'') IS NOT NULL
       EXCEPT SELECT DISTINCT UPPER(TRIM("Member Name as per CR")) FROM {FOCUS_PLUS_TBL} WHERE NULLIF(TRIM("Member Name as per CR"),'') IS NOT NULL
       EXCEPT SELECT DISTINCT UPPER(TRIM(member_name)) FROM {FOCUS_PG_TBL} WHERE NULLIF(TRIM(member_name),'') IS NOT NULL
     ) t;

-- PATTERN: CROSS-SCHEME DATA-QUALITY SCORECARD (CS-17). One row per scheme: total rows + % EPIC populated +
-- % bank populated + % district populated. CM Elevate has NO EPIC and NO bank account → those % are NULL (say so).
Q: cross-scheme data-quality scorecard
SQL: SELECT 'Focus+' AS scheme, COUNT(*) AS records,
            ROUND(100.0*COUNT(NULLIF(TRIM("EPIC ID"),''))/COUNT(*),1) AS pct_epic,
            ROUND(100.0*COUNT(NULLIF(TRIM("Account No"),''))/COUNT(*),1) AS pct_bank,
            ROUND(100.0*COUNT(NULLIF(TRIM("District"),''))/COUNT(*),1) AS pct_district
     FROM {FOCUS_PLUS_TBL}
     UNION ALL
     SELECT 'Focus', COUNT(*),
            ROUND(100.0*COUNT(NULLIF(TRIM(member_epic_id),''))/COUNT(*),1),
            ROUND(100.0*COUNT(NULLIF(TRIM(member_bank_account_no),''))/COUNT(*),1),
            ROUND(100.0*COUNT(NULLIF(TRIM(district_name),''))/COUNT(*),1)
     FROM {FOCUS_PG_TBL}
     UNION ALL
     SELECT 'CM Elevate', COUNT(*), NULL, NULL,
            ROUND(100.0*COUNT(NULLIF(TRIM(district),''))/COUNT(*),1)
     FROM {ELEVATE_TBL};

-- PATTERN: WHICH BANK across schemes (CS-16). Count by BANK NAME over Focus+ + Focus (Elevate has no usable bank-
-- account/name for this) — the NL answer must say the account-level cross-scheme comparison excludes CM Elevate.
Q: which bank handles the most beneficiaries across schemes
SQL: SELECT bank, COUNT(*) AS records FROM (
       SELECT UPPER(TRIM("Bank Name")) AS bank FROM {FOCUS_PLUS_TBL} WHERE NULLIF(TRIM("Bank Name"),'') IS NOT NULL
       UNION ALL
       SELECT UPPER(TRIM(member_bank_name)) FROM {FOCUS_PG_TBL} WHERE member_bank_name ~ '[A-Za-z]'
     ) u GROUP BY bank ORDER BY records DESC LIMIT 10;

-- PATTERN: NOT-ANSWERABLE cross-scheme ask (needs an EPIC/account on CM Elevate, which it lacks)
Q: which EPIC appears in all three schemes including CM Elevate
SQL: CANNOT_ANSWER
"""
