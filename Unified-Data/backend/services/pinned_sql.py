"""
Pinned (hard-coded) SQL for a few questions the LLM does not reproduce reliably —
e.g. queries needing a low-volume filter the model keeps dropping, or a fuzzy
cross-scheme name search the model assembles inconsistently.

match_pinned_sql(question) returns the exact SQL string (already verified to run and
to pass validate_sql) when the question matches a pinned pattern, else None. The router
runs it directly, bypassing generate_sql; the model then only writes the NL answer.

IDENTICAL copy in each backend's services/ dir (apps are deployed separately).
"""
import re

# The three scheme tables (same Neon DB). Hard-coded constants, used directly in pinned SQL.
_FOCUS_PG   = "focus_pg"
_FOCUS_PLUS = '"Meghalaya_Chatbot"'
_ELEVATE    = "cm_elevate"

# Per-quarter geographic concentration index (HHI of district shares of CM Elevate
# applications), low-volume quarters (<20 apps) excluded so artifact HHIs (a 1-app
# quarter → HHI 1.0) never appear. Quarter from the real disbursement_date timestamp.
_CONCENTRATION_PER_QUARTER = (
    "WITH per AS (SELECT to_char(disbursement_date::timestamp,'YYYY-\"Q\"Q') AS q, "
    "UPPER(TRIM(district)) AS d, COUNT(*) AS n FROM {ELEVATE} "
    "WHERE disbursement_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' AND NULLIF(TRIM(district),'') <> '' "
    "GROUP BY 1, 2), tot AS (SELECT q, SUM(n) AS total FROM per GROUP BY q HAVING SUM(n) >= 20) "
    "SELECT per.q AS quarter, tot.total AS applications, "
    "ROUND(SUM(POWER(per.n::numeric/tot.total,2)),4) AS concentration_hhi, "
    "ROUND(MAX(per.n::numeric/tot.total)*100,1) AS top_district_share_pct "
    "FROM per JOIN tot ON per.q=tot.q GROUP BY per.q, tot.total ORDER BY per.q;"
)

# Fuzzy cross-scheme name lookup (pg_trgm). {N} is the safe-escaped search name; the `%`
# operator uses pg_trgm's default similarity threshold (~0.3). Matches are UNIONed across
# all 3 schemes, deduped by (scheme,name,district,identifier), ranked by similarity.
_FUZZY_NAME_LOOKUP = (
    "SELECT scheme, name, district, identifier, ROUND(MAX(sim)::numeric,2) AS match_score FROM ("
    " SELECT 'FOCUS' AS scheme, member_name AS name, district_name AS district, member_unique_id AS identifier,"
    " similarity(lower(member_name), lower('{N}')) AS sim FROM " + _FOCUS_PG + " WHERE member_name % '{N}'"
    " UNION ALL"
    " SELECT 'CM Elevate', name, district, application_number,"
    " similarity(lower(name), lower('{N}')) FROM " + _ELEVATE + " WHERE name % '{N}'"
    " UNION ALL"
    " SELECT 'Unified', \"Member Name as per CR\", \"District\", NULL,"
    " similarity(lower(\"Member Name as per CR\"), lower('{N}')) FROM " + _FOCUS_PLUS +
    " WHERE \"Member Name as per CR\" % '{N}'"
    " ) u WHERE name IS NOT NULL AND TRIM(name) <> ''"
    " GROUP BY scheme, name, district, identifier ORDER BY match_score DESC LIMIT 50;"
)


def _extract_lookup_name(q_lower: str, original: str) -> str | None:
    """Pull the search name out of a 'lookup/find/search [for] <name> [across schemes]' question.
    Returns the name (original casing) or None. Strips trailing scope phrases and quotes."""
    m = re.search(
        r"\b(?:lookup|look up|find|search(?:\s+for)?|locate|trace)\b\s+(.+)",
        original, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    # drop a leading "beneficiary/applicant/member/person/name (called/named)"
    name = re.sub(r"^(?:the\s+)?(?:beneficiary|applicant|member|person|farmer|name)s?\b"
                  r"(?:\s+(?:called|named|by\s+name))?\s*", "", name, flags=re.IGNORECASE).strip()
    # cut a parenthetical aside, e.g. "John (fuzzy match across all schemes)"
    name = name.split("(")[0].strip()
    # cut trailing scope phrases ("across all schemes", "in all datasets", "everywhere", "by name")
    name = re.split(r"\b(?:across|in all|everywhere|fuzz|by name|over all|throughout)\b",
                    name, flags=re.IGNORECASE)[0].strip()
    name = name.strip(" '\"?.,").strip()
    return name or None


# Plain FOCUS beneficiary/member count = UNIQUE people = COUNT(DISTINCT member_unique_id) (~197,442). Pinned so a
# bare "how many beneficiaries" on the Focus product stays on focus_pg and uses the distinct count (the cross-scheme
# rules otherwise turn it into a per-scheme split / COUNT(*) = 197,504).
_FOCUS_BENEFICIARY_COUNT = (
    "SELECT COUNT(DISTINCT member_unique_id) AS beneficiaries FROM " + _FOCUS_PG +
    " WHERE NULLIF(TRIM(member_name),'') IS NOT NULL;"
)


def match_pinned_sql(question: str, elevate_table: str, home_scheme: str | None = None) -> str | None:
    """Return pinned SQL for a recognised question, else None.
    `home_scheme='Focus'` (the Focus product) enables Focus-specific pins like the plain beneficiary count;
    leave None on the unified router so an unscoped 'how many beneficiaries' stays a per-scheme split."""
    q = re.sub(r"\s+", " ", question.lower()).strip()

    # FOCUS PRODUCT ONLY: plain Focus beneficiary/member count → distinct unique people (~197,442). Only when NO
    # other scheme is named, not a cross-scheme/per-scheme ask, and not scoped by district/block (those need a WHERE).
    if (home_scheme == "Focus"
            and re.search(r"\bbeneficiar|\bmembers?\b|\bfarmers?\b", q)
            and re.search(r"\bhow many\b|\btotal\b|\bnumber of\b|\bcount\b", q)
            and "focus plus" not in q and "focus+" not in q and "elevate" not in q
            and not re.search(r"per scheme|each scheme|all scheme|across|by scheme|cross", q)
            and not re.search(r"\bdistrict\b|\bblock\b|\bvillage\b|\bbank\b|\bgender\b|\bpaid\b|\bunpaid\b"
                              r"|garo|khasi|jaintia|bhoi| in ", q)):
        return _FOCUS_BENEFICIARY_COUNT

    # Concentration over TIME → per-quarter HHI index (the meaningful "variance over time" series).
    if "concentration" in q and re.search(
            r"\bquarter\b|\bvariance\b|over time|trend|improv|worsen|catching up|dominat", q):
        return _CONCENTRATION_PER_QUARTER.format(ELEVATE=elevate_table)

    # Fuzzy cross-scheme name lookup: a lookup/find/search verb that yields a name. A fuzzy/cross-scheme/by-name
    # cue makes intent explicit, but a bare "lookup <name>" also qualifies once a name is successfully extracted.
    if re.search(r"\b(lookup|look up|find|search|locate|trace)\b", q):
        cue = ("fuzz" in q or "across" in q or "all scheme" in q or "all dataset" in q
               or "by name" in q or bool(re.search(r"\bname\b", q)))
        name = _extract_lookup_name(q, question)
        # reject analytical phrasings ("find the highest district", "search top blocks") — those are NOT name lookups
        _DATA_WORD = re.compile(
            r"\b(highest|lowest|most|least|top|bottom|count|total|number|district|districts|block|blocks|village|"
            r"scheme|schemes|median|average|sum|how many|disburs|amount|paid|pending|loan|all|each|per)\b",
            re.IGNORECASE)
        if name and not _DATA_WORD.search(name) and (cue or len(name.split()) <= 3):
            safe = name.replace("'", "''")           # escape single quotes (no model in this path)
            safe = re.sub(r"[;\\]", "", safe)         # strip statement-breakers / backslashes
            if safe.strip():
                return _FUZZY_NAME_LOOKUP.replace("{N}", safe)
    return None
