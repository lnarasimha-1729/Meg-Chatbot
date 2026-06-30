# Meghalaya Chatbot — Scheme Analytics (NL-to-SQL)

AI-powered conversational analytics for three Government of Meghalaya farmer-welfare
schemes. Officers ask questions in plain English (with voice & multilingual support)
and get instant answers, tables, and charts — no SQL knowledge required.

```
Officer: "How many beneficiaries are there in each scheme?"
System:  Focus 197,442 · FOCUS+ 105,813 · CM Elevate 2,847   + bar chart

Officer: "What about West Garo Hills?"     (understands the follow-up)
System:  district-wise breakdown for all three schemes
```

The system turns natural-language questions into SQL, runs them against a live
Neon PostgreSQL database, and writes back a human-readable answer. There is **no RAG** —
every answer is grounded in a freshly generated query over the real data.

---

## The three schemes

| Scheme | What it tracks | Table | Port |
| --- | --- | --- | --- |
| **Focus** | Producer Groups (PGs), PG finance, members, bookkeepers | `focus_pg` | 8200 |
| **Focus+ / Unified Data** | DBT — fixed ₹2,500 payment to individual farmers (EPIC roster) | `Meghalaya_Chatbot` | 8000 |
| **CM Elevate** | Loan + subsidy across 13 livelihood verticals | `cm_elevate` | 8100 |

The three tables live in the **same Neon database**, so the **Unified Data** backend
(port 8000) also serves as a **cross-scheme** entry point: it auto-routes each question
to the right scheme(s) and can join across them (by EPIC for Focus↔Focus+, by district
for anything involving CM Elevate).

---

## Repository layout

```
.
├── Unified-Data/      # Focus+ backend + the SHARED frontend (served on :8000)
│   ├── backend/       #   FastAPI app (routers, services, config)
│   └── frontend/      #   ai_query.html + portal UI (used by all three backends)
├── Focus/             # Focus (Producer Group) backend — :8200
├── CM-Elevate/        # CM Elevate (loan/subsidy) backend — :8100
├── run_all.bat        # Launch all three backends (Windows cmd)
└── run_all.ps1        # Launch all three backends (PowerShell)
```

Each backend is self-contained and follows the same structure:

```
backend/
├── main.py                 # FastAPI app + frontend mount
├── config.py               # settings (reads .env)
├── database.py             # Neon connection
├── routers/query.py        # the /query pipeline
└── services/
    ├── edge_handler.py     # greetings / off-topic / concept answers (no API cost)
    ├── gemini_service.py   # NL-to-SQL generation + NL answer (Gemini)
    ├── prompt_assembler.py # schema + few-shot prompt
    ├── cross_scheme.py     # SHARED cross-scheme rules & concept answers
    ├── pinned_sql.py       # SHARED pre-verified SQL for tricky questions
    ├── cache.py / cache_warmer.py
    └── context_store.py    # multi-turn conversation memory
```

> `cross_scheme.py` and `pinned_sql.py` are kept **byte-identical** across all three
> backends. The apps are deployed separately and must not import each other, so the
> shared logic is copied rather than imported.

---

## How a question is answered

```
        User question (text or voice)
                  │
   1. Edge case?  ├─ greeting / identity / off-topic / scheme definition  ──► instant reply (no API call)
                  │
   2. Resolve     ├─ rewrite a follow-up into a standalone question (uses prior turns)
                  │
   3. Classify    ├─ SQL (fetch fresh data) vs REASON (answer over prior data)
                  │
   4. SQL path    ├─ pinned SQL?  → use pre-verified query
                  │  else          → Gemini generates SQL → validate → run on Neon
                  │                   (one repair retry on a DB error)
                  ▼
        NL answer + table/chart
```

---

## Setup

**Requirements:** Python 3.11+, a Neon PostgreSQL database, a Google Gemini API key.

1. **Create a virtual environment and install deps** (per backend, or share one venv):

   ```bash
   cd Unified-Data
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   pip install -r requirements.txt
   ```

   `run_all.ps1` launches all three backends from `Unified-Data/.venv`, so a single
   shared venv there is enough. (`Unified-Data` and `CM-Elevate` ship a
   `requirements.txt`; `Focus` reuses the same dependencies.)

2. **Configure `.env`** in each backend folder (`Unified-Data/`, `Focus/`, `CM-Elevate/`).
   `.env` files are gitignored — never commit secrets. Required keys:

   ```env
   NEON_DATABASE_URL=postgresql://user:pass@host.neon.tech/dbname
   GEMINI_API_KEY=your_gemini_key
   DATA_TABLE=Meghalaya_Chatbot      # focus_pg / cm_elevate for the other two
   SECRET_KEY=your_secret
   PORT=8000                         # 8200 for Focus, 8100 for CM Elevate
   ```

---

## Running

**All three at once (Windows):**

```powershell
powershell -ExecutionPolicy Bypass -File run_all.ps1
```

Each backend opens in its own window. Then open **http://localhost:8000** — the Unified
Data app serves the shared UI and routes questions to the right scheme.

**One backend on its own:**

```bash
cd Unified-Data
.venv\Scripts\python -m uvicorn backend.main:app --port 8000
```

| Backend | URL |
| --- | --- |
| Unified Data (UI + cross-scheme) | http://localhost:8000 |
| CM Elevate | http://localhost:8100 |
| Focus | http://localhost:8200 |

---

## Tech stack

- **Backend:** FastAPI + SQLAlchemy
- **Database:** Neon PostgreSQL (serverless)
- **AI:** Google Gemini (NL-to-SQL + answer generation)
- **Cache:** Redis (optional; in-memory fallback)
- **Frontend:** single-page HTML/JS console (charts via Chart.js)
