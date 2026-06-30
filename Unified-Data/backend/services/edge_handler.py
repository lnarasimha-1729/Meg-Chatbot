"""
Edge Case Handler — catches non-analytical queries BEFORE hitting the Gemini API.
Zero cost. Instant response. Professional tone.

Tuned for the Unified Data product: a NL-to-SQL assistant over the Meghalaya
Focus Plus farmer payment & registration register.
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
        "Hello! I'm the Unified Data assistant for the Meghalaya Focus Plus farmer "
        "payment & registration database. I can help you with:\n\n"
        "• Farmer lookups — by name, Member ID, EPIC/voter ID, account number, or PG group\n"
        "• District / block / village counts and listings\n"
        "• Payment status — who is paid (Rs. 2,500), who is pending, totals by district\n"
        "• Bank, batch, and data-quality questions\n\n"
        "What would you like to know?"
    ),
    "identity": (
        "I am the **Unified Data** assistant — a natural-language-to-SQL system built for the "
        "Meghalaya Focus Plus farmer payment & registration register (105,813 records).\n\n"
        "I can:\n"
        "• Look up individual farmers and their payment details\n"
        "• Count and list farmers by district, block, village, bank, or batch\n"
        "• Report paid vs. pending farmers and total amounts disbursed\n\n"
        "I'm purpose-built for this dataset — not a general-purpose chatbot. How can I help?"
    ),
    "thanks": (
        "You're welcome! Feel free to ask anything else about the farmer payment records — "
        "lookups, district counts, payment totals, and more."
    ),
    "goodbye": (
        "Thank you for using the Unified Data assistant. Have a great day! "
        "Come back anytime for farmer payment or registration queries."
    ),
    "silly": (
        "That's an interesting question, but I'm a **purpose-built assistant** for the Meghalaya "
        "Focus Plus farmer payment database — so general topics are outside my scope.\n\n"
        "Try asking me something like:\n"
        "• \"How many farmers are in West Garo Hills?\"\n"
        "• \"Find farmer FP10658117\"\n"
        "• \"What is the total amount paid in East Khasi Hills?\"\n"
        "• \"How many farmers are still unpaid?\""
    ),
    "profanity": (
        "I understand you may be frustrated, and I'm here to help! I specialise in the Meghalaya "
        "Focus Plus farmer payment records.\n\n"
        "Let's try again — for example: \"How many farmers have been paid?\" or "
        "\"List farmers in Mylliem block\"."
    ),
    "confused": (
        "No worries! Here are some things you can ask me:\n\n"
        "**Lookups & lists:**\n"
        "• \"Find farmer named Saba Lin Shabong\"\n"
        "• \"Show all farmers in East Khasi Hills\"\n"
        "• \"List farmers who bank with Meghalaya Rural Bank\"\n\n"
        "**Counts & totals:**\n"
        "• \"How many farmers are registered in each district?\"\n"
        "• \"How many have been paid vs. pending?\"\n"
        "• \"Total amount paid in West Garo Hills\"\n\n"
        "Just type your question and I'll get right on it!"
    ),
    "off_topic": (
        "That's a great question — but it's outside my area! I'm the **Unified Data** assistant, built "
        "exclusively for the Meghalaya Focus Plus farmer payment & registration database.\n\n"
        "Could you ask me something about the farmer records? For example:\n\n"
        "• \"How many farmers are there in total?\"\n"
        "• \"Which district has the most farmers?\"\n"
        "• \"Show new registrations not yet paid\"\n"
        "• \"How many records come from each data batch?\""
    ),
}


def detect_edge_case(question: str) -> dict | None:
    """Returns {type, response} if edge case detected; None if a legitimate SQL query."""
    q = question.strip()
    if len(q) < 2:
        return {"type": "confused", "response": _RESPONSES["confused"]}

    ql = q.lower()

    # ── Conceptual / explanatory question about the schemes (answer with FACTS, not SQL) ──
    # MUST run before the data-intent early-exit (words like 'focus'/'scheme' are also data words).
    concept_key = detect_concept_question(ql)
    if concept_key:
        return {"type": "concept", "response": CONCEPT_ANSWERS[concept_key]}

    # ── Early exit: clear farmer-data intent → skip all edge checks ──
    _DATA_STRONG = [
        r"\bfarmer", r"\bmember", r"\bbeneficiar",
        r"\bdistrict\b", r"\bblock\b", r"\bvillage\b",
        r"\bpaid\b", r"\bunpaid\b", r"\bpending\b", r"\bpayment\b", r"\bamount\b",
        r"\bbank\b", r"\baccount\b", r"\bifsc\b", r"\bcheque\b", r"\bchq\b",
        r"\bepic\b", r"\bvoter\b", r"\bmobile\b",
        r"\bpg\b", r"\bproducer\s*group\b", r"\bnic\b",
        r"\bbatch\b", r"\bsource\b", r"\bregistration\b", r"\bregistered\b",
        r"\bmeghalaya\b", r"garo\b", r"khasi\b", r"jaintia\b", r"ri\s*bhoi\b",
        r"\bfp\d", r"\blegacy\b",
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

    # Whitelist: allow questions related to the farmer-payment dataset
    data_words = [
        "farmer", "farmers", "member", "beneficiar",
        "district", "block", "village", "meghalaya",
        "garo", "khasi", "jaintia", "ri bhoi",
        "paid", "unpaid", "pending", "payment", "amount", "rupee", "rs",
        "cheque", "chq", "credit advice",
        "bank", "account", "ifsc", "mobile", "phone",
        "epic", "voter", "pg", "producer group", "nic",
        "batch", "source", "legacy", "focus", "registration", "registered",
        "count", "total", "how many", "show", "list", "find", "search", "lookup",
        "compare", "breakdown", "distribution", "sum", "average", "name",
        # analytical / data-quality vocabulary (these are ALL answerable from the table)
        "data", "quality", "scorecard", "health", "completeness", "coverage", "summary", "overview",
        "duplicate", "duplicates", "anomaly", "anomalies", "suspicious", "fake", "fraud", "fraudulent",
        "missing", "null", "empty", "incomplete", "invalid", "outlier", "shared",
        "ratio", "percentage", "percent", "share", "rate", "proportion",
        "highest", "lowest", "most", "least", "top", "bottom", "max", "maximum", "min", "minimum",
        "rank", "ranking", "popular", "biggest", "smallest", "largest",
        "which", "what", "who", "where", "trend", "per ", "each", "group", "by ",
        "scheme", "loan", "subsidy", "disburse", "sanction", "applicant", "application",
        # cross-scheme vocabulary (Focus / Focus+ / CM Elevate joins)
        "elevate", "overlap", "venn", "cross", "both", "all three", "all 3", "only in",
        "intersect", "footprint", "saturat", "penetration", "heat map", "heatmap", "dominance",
    ]
    has_context = any(re.search(w, ql) for w in data_words)
    if not has_context:
        return {"type": "off_topic", "response": _RESPONSES["off_topic"]}

    return None
