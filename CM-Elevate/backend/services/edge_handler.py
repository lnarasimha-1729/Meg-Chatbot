"""
Edge Case Handler — catches non-analytical queries BEFORE hitting the Gemini API.
Zero cost. Instant response. Professional tone.

Tuned for the CM Elevate product: a NL-to-SQL assistant over the Meghalaya
government scheme disbursement & sanction register (cm_elevate).
"""
import re, logging
from backend.services.cross_scheme import detect_concept_question, CONCEPT_ANSWERS

logger = logging.getLogger(__name__)

# ── Pattern banks ─────────────────────────────────────────────

_GREETINGS = [
    r"^(hi|hello|hey|hii+|helo|namaste|namaskar|khublei|good\s*(morning|afternoon|evening|day|night))[\s!.?]*$",
    r"^(howdy|greetings|sup|whats?\s*up|yo|hola)[\s!.?]*$",
]

_IDENTITY = [
    r"(who|what)\s+(are|r)\s+(you|u)",
    r"(your|ur)\s+name",
    r"are\s+you\s+(ai|bot|human|real|chatbot|robot|machine|gpt|claude|gemini)",
    r"(tell|about)\s+(me\s+)?about\s+(yourself|you)",
    r"what\s+can\s+you\s+do",
    r"what\s+do\s+you\s+do",
    r"(introduce)\s+(yourself)",
    r"which\s+(ai|model|llm|technology)\s+(are|is|do)\s+you",
    r"(powered|built|made|developed|created)\s+by",
]

_THANKS = [
    r"^(thanks?|thank\s*you|ty|thx|thanku|dhanyavaad|shukriya)[\s!.?]*$",
    r"^(that.?s?\s+(great|helpful|perfect|awesome|nice|good|excellent|wonderful))[\s!.?]*$",
    r"^(great|perfect|awesome|excellent|wonderful|brilliant|fantastic)[\s!.?]*$",
]

_GOODBYE = [
    r"^(bye|goodbye|good\s*bye|see\s*you|cya|take\s*care|later)[\s!.?]*$",
    r"^(have\s+a\s+(good|great|nice|wonderful)\s+(day|evening|night))[\s!.?]*$",
]

_SILLY = [
    r"(tell|say)\s+(me\s+)?(a\s+)?joke",
    r"make\s+me\s+laugh",
    r"sing\s+(a\s+)?song",
    r"write\s+(a\s+)?(poem|story|essay|rap|song|lyrics)",
    r"(play|let.*play)\s+(a\s+)?game",
    r"do\s+you\s+(like|love|hate|feel|think|dream|sleep|eat|drink)",
    r"(favorite|favourite)\s+(color|colour|food|movie|song|book|sport|animal)",
    r"how\s+old\s+are\s+you",
    r"(marry|date|love|kiss|hug)\s+me",
    r"what\s+is\s+the\s+(meaning|purpose)\s+of\s+(life|everything)",
    r"(better|worse)\s+than\s+(chatgpt|gpt|openai|claude|gemini|copilot)",
    r"translate\s+(this|to|into)\s+",
    r"(write|draft|compose)\s+(a\s+)?(email|letter|message|cv|resume)",
    r"what\s+is\s+\d+\s*[\+\-\*\/]\s*\d+",
    r"(predict|forecast)\s+(future|stock|crypto|price|weather)",
    r"(stupid|dumb|useless|idiot|fool)\s*(bot|ai|system|app)?",
    r"(capital|president|prime\s*minister|currency|population)\s+of\s+\w+",
    r"(recipe|ingredients|how\s+to\s+cook)\s+",
    r"(ipl|cricket|football|match|score)\b",
    r"(bollywood|hollywood|movie|film|actor|actress)\b",
]

_CONFUSED = [
    r"^(i\s+don.?t\s+(know|understand)|what|huh|what\?|confused)[\s!.?]*$",
    r"^(help|help\s*me|i\s+need\s+help)[\s!.?]*$",
    r"^(hmm+|umm+|ok+|okay|k|yes|no|yeah|nah|sure|right|got\s*it)[\s!.?]*$",
    r"^(start|begin|let.s\s*(start|begin|go))[\s!.?]*$",
    r"^\?+$",
]

_OFF_TOPIC = [
    r"(weather|cricket|football|soccer|movie|film|song|recipe|news|horoscope)\b",
    r"(stock|share|market|bitcoin|crypto|nft|investment)\b",
    r"(train|flight|bus|ticket|booking|hotel|travel|visa)\b",
    r"(politics|election|parliament)\b",
    r"(amazon|flipkart|shopping)\b",
]

_PROFANITY_REDIRECT = [
    r"\b(fuck|shit|damn|bastard|crap)\b",
]

# ── Response templates ────────────────────────────────────────

_RESPONSES = {
    "greeting": (
        "Hello! I'm the CM Elevate assistant for the Meghalaya government scheme "
        "disbursement database. I can help you with:\n\n"
        "• Application lookups — by application number, beneficiary/society name, district, or scheme\n"
        "• Disbursement status — disbursed vs. pending, subsidy & loan tranches\n"
        "• Totals — subsidy and loan amounts by district, scheme, block, or Bank vs. LIFCOM\n"
        "• Refused / desanctioned records and completion rates\n\n"
        "What would you like to know?"
    ),
    "identity": (
        "I am the **CM Elevate** assistant — a natural-language-to-SQL system built for the "
        "Meghalaya government scheme sanction & disbursement register (2,847 applications across 13 schemes).\n\n"
        "I can:\n"
        "• Look up individual applications and their subsidy/loan disbursement details\n"
        "• Count and total applications by district, scheme, block, month, or disbursing entity\n"
        "• Report disbursed vs. pending, refused, and desanctioned records\n\n"
        "I'm purpose-built for this dataset — not a general-purpose chatbot. How can I help?"
    ),
    "thanks": (
        "You're welcome! Feel free to ask anything else about the scheme disbursement records — "
        "lookups, district/scheme totals, disbursement status, and more."
    ),
    "goodbye": (
        "Thank you for using the CM Elevate assistant. Have a great day! "
        "Come back anytime for scheme disbursement or sanction queries."
    ),
    "silly": (
        "That's an interesting question, but I'm a **purpose-built assistant** for the Meghalaya "
        "CM Elevate scheme disbursement database — so general topics are outside my scope.\n\n"
        "Try asking me something like:\n"
        "• \"How many applications have been disbursed?\"\n"
        "• \"Total subsidy disbursed scheme-wise\"\n"
        "• \"Find application MEWSR000040\"\n"
        "• \"Total loan through LIFCOM vs Bank\""
    ),
    "profanity": (
        "I understand you may be frustrated, and I'm here to help! I specialise in the Meghalaya "
        "CM Elevate scheme disbursement records.\n\n"
        "Let's try again — for example: \"How many applications are pending?\" or "
        "\"Show total disbursement by district\"."
    ),
    "confused": (
        "No worries! Here are some things you can ask me:\n\n"
        "**Lookups & lists:**\n"
        "• \"Find application MEWSR000040\"\n"
        "• \"List PRIME Agriculture Vehicle Scheme applicants from Ri Bhoi\"\n"
        "• \"Search for beneficiary named Songsak\"\n\n"
        "**Counts & totals:**\n"
        "• \"How many applications have been disbursed vs. pending?\"\n"
        "• \"Total subsidy disbursed scheme-wise\"\n"
        "• \"District-wise disbursement completion rate\"\n\n"
        "Just type your question and I'll get right on it!"
    ),
    "off_topic": (
        "That's a great question — but it's outside my area! I'm the **CM Elevate** assistant, built "
        "exclusively for the Meghalaya government scheme disbursement & sanction database.\n\n"
        "Could you ask me something about the scheme records? For example:\n\n"
        "• \"How many applications are there in total?\"\n"
        "• \"Which scheme has the highest total subsidy?\"\n"
        "• \"Show all desanctioned records\"\n"
        "• \"Block-wise summary for West Garo Hills\""
    ),
}


def detect_edge_case(question: str) -> dict | None:
    """Returns {type, response} if edge case detected; None if a legitimate SQL query."""
    q = question.strip()
    if len(q) < 2:
        return {"type": "confused", "response": _RESPONSES["confused"]}

    ql = q.lower()

    # ── Conceptual / explanatory question about the schemes (answer with FACTS, not SQL) ──
    # MUST run before the data-intent early-exit (words like 'scheme'/'elevate' are also data words).
    concept_key = detect_concept_question(ql)
    if concept_key:
        return {"type": "concept", "response": CONCEPT_ANSWERS[concept_key]}

    # ── Early exit: clear scheme-data intent → skip all edge checks ──
    _DATA_STRONG = [
        r"\bapplication", r"\bbeneficiar", r"\bsociety\b", r"\bcooperative\b", r"\bivcs\b",
        r"\bdistrict\b", r"\bblock\b", r"\bvillage\b",
        r"\bdisbursed\b", r"\bdisbursement\b", r"\bpending\b", r"\bsanction", r"\bsubsidy\b",
        r"\bloan\b", r"\btranche\b", r"\bamount\b", r"\bdesanction", r"\brefused\b",
        r"\blifcom\b", r"\bbank\b", r"\bentity\b", r"\bscheme\b",
        r"\bpiggery\b", r"\bpoultry\b", r"\bdairy\b", r"\bwarehouse\b", r"\bsericulture\b",
        r"\bprime\b", r"\bvehicle\b", r"\btourism\b", r"\bgoat\b",
        r"\bmonth\b", r"\byear\b", r"\bcompletion\b",
        r"\bmeghalaya\b", r"garo\b", r"khasi\b", r"jaintia\b", r"ri\s*bhoi\b",
        r"\b(mewsi|mewsr|arvsi|arvsr|arvsu|mcfcr|mpdsi|mpfsi|mddsi|ptvsi|ptvsr|mgfsi)\b",
    ]
    if any(re.search(p, ql) for p in _DATA_STRONG):
        return None

    for pattern in _GREETINGS:
        if re.search(pattern, ql):
            return {"type": "greeting", "response": _RESPONSES["greeting"]}
    for pattern in _IDENTITY:
        if re.search(pattern, ql):
            return {"type": "identity", "response": _RESPONSES["identity"]}
    for pattern in _THANKS:
        if re.search(pattern, ql):
            return {"type": "thanks", "response": _RESPONSES["thanks"]}
    for pattern in _GOODBYE:
        if re.search(pattern, ql):
            return {"type": "goodbye", "response": _RESPONSES["goodbye"]}
    for pattern in _PROFANITY_REDIRECT:
        if re.search(pattern, ql):
            return {"type": "profanity", "response": _RESPONSES["profanity"]}
    for pattern in _SILLY:
        if re.search(pattern, ql):
            return {"type": "silly", "response": _RESPONSES["silly"]}
    for pattern in _CONFUSED:
        if re.search(pattern, ql):
            return {"type": "confused", "response": _RESPONSES["confused"]}

    # ── Meta-conversation / follow-up passthrough → reach resolve_question() ──
    _PASSTHROUGH = [
        r"\b(my|your)\s+(first|last|previous|prior|earlier)\s+(question|query|message)",
        r"what\s+did\s+(i|you)\s+(ask|say|answer|tell)",
        r"(summarize|summary)\s+(our|this|the)\s+(conversation|chat)",
        r"\b(sum|total|add|combine|combined|altogether)\b.{0,30}\b(both|them|these|those|two|it)\b",
        r"which\s+(is|one\s+is|are|has)\s+(the\s+)?(more|most|less|least|higher|highest|lower|lowest|bigger|biggest|smaller|smallest)\b",
        r"^(what\s+about|and|also|plus)\s+(both|them|these|those|it|the\s+other)[\s?]*$",
        r"^what\s+about\s+.{2,40}$",
        r"^(explain|why|reason)\b",
        r"\b(explain|why)\s+(this|that|the|above|it|these|those)\b",
        r"\byour\s+(opinion|thought|analysis|view|answer|response)\b",
    ]
    if any(re.search(p, ql) for p in _PASSTHROUGH):
        return None

    # ── Non-English passthrough (let the LLM handle Hindi/regional scripts) ──
    non_ascii_count = sum(1 for c in q if ord(c) > 127)
    if non_ascii_count >= 3:
        return None

    # Whitelist: allow questions related to the scheme-disbursement dataset
    data_words = [
        "application", "applicant", "beneficiar", "society", "cooperative",
        "district", "block", "village", "meghalaya",
        "garo", "khasi", "jaintia", "ri bhoi",
        "disbursed", "disbursement", "pending", "sanction", "subsidy", "loan",
        "tranche", "amount", "rupee", "rs", "desanction", "refused",
        "lifcom", "bank", "entity", "scheme",
        "piggery", "poultry", "dairy", "warehouse", "sericulture", "prime",
        "vehicle", "tourism", "goat", "business", "facility",
        "month", "year", "completion", "rate",
        "count", "total", "how many", "show", "list", "find", "search", "lookup",
        "compare", "breakdown", "distribution", "sum", "average", "name",
        # analytical / data-quality vocabulary (all answerable from the table)
        "data", "quality", "scorecard", "health", "completeness", "summary", "overview",
        "duplicate", "duplicates", "anomaly", "anomalies", "suspicious", "missing", "null",
        "empty", "incomplete", "invalid", "outlier", "shared",
        "ratio", "percentage", "percent", "share", "proportion", "approval",
        "highest", "lowest", "most", "least", "top", "bottom", "max", "maximum", "min", "minimum",
        "rank", "ranking", "popular", "biggest", "smallest", "largest",
        "which", "what", "who", "where", "trend", "per ", "each", "group", "by ", "stuck",
        # cross-scheme vocabulary (Focus / Focus+ / CM Elevate joins)
        "schemes", "focus", "focus+", "focus plus", "elevate", "epic", "voter", "pg", "producer group",
        "overlap", "venn", "cross", "both", "all three", "all 3", "only in",
        "intersect", "footprint", "saturat", "penetration", "heat map", "heatmap", "dominance",
    ]
    has_context = any(re.search(w, ql) for w in data_words)
    if not has_context:
        return {"type": "off_topic", "response": _RESPONSES["off_topic"]}

    return None
