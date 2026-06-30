# NLP-to-SQL Prompt Engineering Guide
## FOCUS Producer Group (PG) Database

---

## 1. DATABASE CONTEXT (Schema for Prompt Assembler Layer)

### Table: `focus_pg` (Producer Group Master)
| Column | Type | Description |
|---|---|---|
| `focus_pg_id` | INT | Primary Key – unique group identifier |
| `pg_name` | VARCHAR | Name of the Producer Group |
| `has_bookkeeper_been_identified` | VARCHAR | 'Yes' / 'No' – whether a bookkeeper is assigned |
| `bank_account_no` | VARCHAR | PG's bank account number |
| `bank_ifsc` | VARCHAR | IFSC code of PG's bank branch |
| `bank_branch` | VARCHAR | PG's bank branch name |
| `bank_name` | VARCHAR | PG's bank name |
| `ivcs_name` | VARCHAR | IVCS entity name (if applicable) |
| `ivcs_account_no` | VARCHAR | IVCS account number |
| `block_name` | VARCHAR | C&RD Block / administrative block |
| `district_name` | VARCHAR | District name (e.g. West Jaintia Hills, West Khasi Hills) |
| `village_id` | VARCHAR | Village identifier |
| `finance_amount_disbursed` | DECIMAL | First loan/finance amount disbursed to PG |
| `finance_date` | DATE | Date of first disbursement (stored as serial date) |
| `disburse_amount_2` | DECIMAL | Second tranche disbursement amount |
| `disburse_date_2` | DATE | Date of second disbursement |

### Table: `focus_pg_members` (Member Details)
| Column | Type | Description |
|---|---|---|
| `focus_pg_members_details__id` | INT | Primary Key – member record ID |
| `focus_pg_id` | INT | Foreign Key → `focus_pg.focus_pg_id` |
| `member_name` | VARCHAR | Full name of the member |
| `member_epic_id` | VARCHAR | Voter ID / EPIC number |
| `member_age` | INT | Age of member |
| `member_unique_id` | VARCHAR | FOCUS system unique ID (format: FP{pg_id}{sequence}) |
| `gender_id` | VARCHAR | 'Male' / 'Female' |
| `member_bank_name` | VARCHAR | Member's personal bank name |
| `member_bank_account_no` | VARCHAR | Member's personal bank account number |
| `member_bank_ifsc_code` | VARCHAR | Member's personal bank IFSC |
| `member_bank_branch` | VARCHAR | Member's personal bank branch |
| `focus_pg_members_count` | INT | Always 1 — count flag per member record |

### Key Relationships
- `focus_pg` ↔ `focus_pg_members` → One-to-Many via `focus_pg_id`
- Total members per PG = `COUNT(*)` on `focus_pg_members` grouped by `focus_pg_id`
- Total disbursement = `COALESCE(finance_amount_disbursed, 0) + COALESCE(disburse_amount_2, 0)`

### Known Enum Values
- **Districts**: West Jaintia Hills, West Khasi Hills, South West Khasi Hills, East Khasi Hills
- **Bookkeeper**: 'Yes', 'No'
- **Gender**: 'Male', 'Female'
- **Banks**: Meghalaya Rural Bank, State Bank of India, Meghalaya Co-operative Apex Bank

---

## 2. SYSTEM PROMPT (Prompt Assembler Layer)

Use this as the `system` message in your LLM API call:

```
You are an expert SQL query generator for the FOCUS Producer Group (PG) Management System used in Meghalaya, India.

## DATABASE SCHEMA

### Table: focus_pg
- focus_pg_id (INT, PK)
- pg_name (VARCHAR) — name of the Producer Group
- has_bookkeeper_been_identified (VARCHAR) — 'Yes' or 'No'
- bank_account_no, bank_ifsc, bank_branch, bank_name (VARCHAR) — PG banking details
- ivcs_name, ivcs_account_no (VARCHAR) — IVCS details
- block_name (VARCHAR) — administrative block (e.g. 'Laskein C & RD Block')
- district_name (VARCHAR) — district (e.g. 'West Jaintia Hills', 'West Khasi Hills')
- village_id (VARCHAR) — village name
- finance_amount_disbursed (DECIMAL) — first disbursement amount (NULL if not disbursed)
- finance_date (DATE) — first disbursement date
- disburse_amount_2 (DECIMAL) — second disbursement amount (NULL if none)
- disburse_date_2 (DATE) — second disbursement date

### Table: focus_pg_members
- focus_pg_members_details__id (INT, PK)
- focus_pg_id (INT, FK → focus_pg.focus_pg_id)
- member_name (VARCHAR)
- member_epic_id (VARCHAR) — Voter ID
- member_age (INT)
- member_unique_id (VARCHAR) — format FP{pg_id}{sequence}
- gender_id (VARCHAR) — 'Male' or 'Female'
- member_bank_name, member_bank_account_no, member_bank_ifsc_code, member_bank_branch (VARCHAR)

## RULES
1. Always return ONLY a valid SQL query. No explanation. No markdown. No preamble.
2. Use table aliases: `p` for focus_pg, `m` for focus_pg_members.
3. For total disbursement, always use: COALESCE(p.finance_amount_disbursed, 0) + COALESCE(p.disburse_amount_2, 0)
4. For member count per PG, use: COUNT(m.focus_pg_members_details__id)
5. NULL-safe: treat missing amounts as 0 using COALESCE.
6. For partial name matches, use ILIKE (PostgreSQL) or LIKE with wildcards.
7. If the question is ambiguous, generate the most reasonable interpretation.
8. Always end the query with a semicolon.
```

---

## 3. FEW-SHOT EXAMPLES (Include in Prompt for Higher Accuracy)

Add these examples after your system prompt as demonstration pairs:

---

### Example 1 — Simple List Query
**User:** "Show me all producer groups in West Khasi Hills"
```sql
SELECT p.focus_pg_id, p.pg_name, p.block_name, p.village_id
FROM focus_pg p
WHERE p.district_name = 'West Khasi Hills';
```

---

### Example 2 — Aggregation with Join
**User:** "How many members does each PG have?"
```sql
SELECT p.focus_pg_id, p.pg_name, COUNT(m.focus_pg_members_details__id) AS total_members
FROM focus_pg p
LEFT JOIN focus_pg_members m ON p.focus_pg_id = m.focus_pg_id
GROUP BY p.focus_pg_id, p.pg_name
ORDER BY total_members DESC;
```

---

### Example 3 — Financial Filter
**User:** "Which PGs have received more than 75000 in total disbursement?"
```sql
SELECT p.focus_pg_id, p.pg_name,
       COALESCE(p.finance_amount_disbursed, 0) + COALESCE(p.disburse_amount_2, 0) AS total_disbursed
FROM focus_pg p
WHERE COALESCE(p.finance_amount_disbursed, 0) + COALESCE(p.disburse_amount_2, 0) > 75000
ORDER BY total_disbursed DESC;
```

---

### Example 4 — Gender-based Count
**User:** "How many female members are there across all PGs?"
```sql
SELECT COUNT(*) AS female_member_count
FROM focus_pg_members m
WHERE m.gender_id = 'Female';
```

---

### Example 5 — PGs without a bookkeeper
**User:** "List all groups that don't have a bookkeeper"
```sql
SELECT p.focus_pg_id, p.pg_name, p.block_name, p.district_name
FROM focus_pg p
WHERE p.has_bookkeeper_been_identified = 'No';
```

---

### Example 6 — Member Lookup by Name
**User:** "Find the member named Kelias Biam"
```sql
SELECT m.member_name, m.member_unique_id, m.gender_id, m.member_age,
       p.pg_name, p.district_name
FROM focus_pg_members m
JOIN focus_pg p ON m.focus_pg_id = p.focus_pg_id
WHERE m.member_name ILIKE '%Kelias Biam%';
```

---

### Example 7 — PGs with No Disbursement Yet
**User:** "Show PGs that have not received any finance"
```sql
SELECT p.focus_pg_id, p.pg_name, p.district_name
FROM focus_pg p
WHERE p.finance_amount_disbursed IS NULL
  AND p.disburse_amount_2 IS NULL;
```

---

### Example 8 — Age-based Filter
**User:** "List all members below the age of 25"
```sql
SELECT m.member_name, m.member_age, m.gender_id, p.pg_name
FROM focus_pg_members m
JOIN focus_pg p ON m.focus_pg_id = p.focus_pg_id
WHERE m.member_age < 25
ORDER BY m.member_age ASC;
```

---

### Example 9 — Bank-specific Query
**User:** "How many members bank with State Bank of India?"
```sql
SELECT COUNT(*) AS sbi_members
FROM focus_pg_members m
WHERE m.member_bank_name ILIKE '%State Bank of India%';
```

---

### Example 10 — District-level Summary
**User:** "Give me a summary of total disbursement by district"
```sql
SELECT p.district_name,
       COUNT(DISTINCT p.focus_pg_id) AS total_pgs,
       SUM(COALESCE(p.finance_amount_disbursed, 0) + COALESCE(p.disburse_amount_2, 0)) AS total_disbursed
FROM focus_pg p
GROUP BY p.district_name
ORDER BY total_disbursed DESC;
```

---

## 4. QUERY TRANSFORMATION PATTERNS

### Synonym Mapping (teach your NLP layer these aliases)
| User says... | Maps to... |
|---|---|
| "group", "PG", "producer group", "SHG" | `focus_pg` table |
| "members", "people", "participants" | `focus_pg_members` table |
| "loan", "finance", "funds", "money disbursed" | `finance_amount_disbursed` / `disburse_amount_2` |
| "village", "location", "area" | `village_id` |
| "block", "panchayat block" | `block_name` |
| "bookkeeper assigned", "has accounts person" | `has_bookkeeper_been_identified` |
| "voter ID", "EPIC", "ID card" | `member_epic_id` |
| "total money", "total finance" | `COALESCE(finance_amount_disbursed,0) + COALESCE(disburse_amount_2,0)` |

---

## 5. QUERY RESOLVER PROMPT (Second-pass Validation)

After generating SQL, run it through a second prompt for validation:

```
You are a SQL validator for a PostgreSQL database. Review the following SQL query for:
1. Syntax errors
2. Missing JOIN conditions
3. NULL handling (use COALESCE where amounts may be NULL)
4. Correct column names (verify against schema below)
5. Correct table names: only `focus_pg` and `focus_pg_members` are valid

Schema columns:
- focus_pg: focus_pg_id, pg_name, has_bookkeeper_been_identified, bank_account_no, bank_ifsc, bank_branch, bank_name, ivcs_name, ivcs_account_no, block_name, district_name, village_id, finance_amount_disbursed, finance_date, disburse_amount_2, disburse_date_2
- focus_pg_members: focus_pg_members_details__id, focus_pg_id, member_name, member_epic_id, member_age, member_unique_id, gender_id, member_bank_name, member_bank_account_no, member_bank_ifsc_code, member_bank_branch

Return ONLY the corrected SQL query. If no corrections needed, return the original query unchanged.

Query to validate:
{sql_query}
```

---

## 6. FULL PROMPT ASSEMBLER — API CALL TEMPLATE

```python
import anthropic

def natural_language_to_sql(user_query: str) -> str:
    client = anthropic.Anthropic()

    system_prompt = """
    You are an expert SQL query generator for the FOCUS Producer Group Management System.
    [Paste the full system prompt from Section 2 here]
    """

    few_shot_examples = """
    Examples:
    Q: Show all PGs in West Khasi Hills
    A: SELECT p.focus_pg_id, p.pg_name, p.block_name FROM focus_pg p WHERE p.district_name = 'West Khasi Hills';

    Q: How many members does each PG have?
    A: SELECT p.pg_name, COUNT(m.focus_pg_members_details__id) AS members FROM focus_pg p LEFT JOIN focus_pg_members m ON p.focus_pg_id = m.focus_pg_id GROUP BY p.pg_name;

    Q: Which PGs received more than 75000 total?
    A: SELECT p.pg_name, COALESCE(p.finance_amount_disbursed,0)+COALESCE(p.disburse_amount_2,0) AS total FROM focus_pg p WHERE COALESCE(p.finance_amount_disbursed,0)+COALESCE(p.disburse_amount_2,0) > 75000;
    """

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": f"{few_shot_examples}\n\nNow generate SQL for: {user_query}"
            }
        ]
    )
    return response.content[0].text.strip()
```

---

## 7. EDGE CASE HANDLING

### Query Formation Rules for Tricky Cases

| Scenario | Handling |
|---|---|
| User asks for "total members" without specifying PG | `COUNT(*)` on `focus_pg_members` (no filter) |
| User asks about "groups with no money" | Check both disbursement columns are NULL |
| User says "recently disbursed" | Filter `finance_date` DESC, `LIMIT 10` |
| User asks for a specific member's PG | JOIN on `focus_pg_id`, return `pg_name` |
| User asks "which district has most PGs" | GROUP BY `district_name`, ORDER BY `COUNT(*) DESC` |
| User asks for members "without bank details" | `WHERE member_bank_account_no IS NULL` |
| Mixed gender count per PG | GROUP BY `pg_name, gender_id` |

---

*Guide built from FOCUS PG database schema — Sheet: Final Draft*
