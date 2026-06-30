# NLP-to-SQL Prompt Engineering Guide
## Database: Meghalaya Farmer Payment & Registration — `unified_data`
### Built from: Unified_Columns.xlsx (verified, 105,813 rows)

---

## ⚠️ DATA QUALITY FACTS (Critical for Prompt Design)

Before building prompts, your middle layer must know these facts:

| Issue | Detail |
|---|---|
| **District casing is inconsistent** | Same district appears as `WEST GARO HILLS` and `West Garo Hills` — always use `UPPER()` or `ILIKE` |
| **All payments are ₹2,500** | `Amount(Rs)` has only one value: 2500.0 across all 93,286 paid records |
| **New CR column is broken** | Every value is `#ERROR!` — never query this column |
| **Legacy CR & Focus Legacy are empty** | 0 non-null values — do not use in queries |
| **Date of Payment is Excel serial** | Value `45882` = 13 August 2025. Must convert: `base_date(1899-12-30) + N days` |
| **Bank Name column has IFSC codes** | Some rows have IFSC codes in `Bank Name` field — filter using `NOT REGEXP '[0-9]{7}'` |
| **Mobile Number is float** | Stored as float64 (e.g. `9876543210.0`), cast when displaying |
| **12,527 records have no Amount** | These are newer registrations (New source sheets) not yet paid |
| **3,518 records have no Member_id** | From `Meg-One-Focus-Plus-New2` source batch |

---

## 1. VERIFIED DATABASE SCHEMA

**Table name:** `unified_data`

```
Column                           Type        Non-Null    Notes
-------------------------------  ----------  ----------  ----------------------------------
S.No.                            INTEGER     105,813     Row serial number
District                         TEXT        105,812     ⚠ Mixed casing — always use UPPER() or ILIKE
Block                            TEXT        105,812     Sub-district unit
Village                          TEXT        105,782     Village name
Member_id                        TEXT        102,295     Farmer ID, format: FP########
PG ID                            TEXT         93,245     Producer Group ID, format: PG-FOCUS-EKH-#####
Member Name as per CR            TEXT        105,813     Name in Cash Register — always populated
Member Name as per Bank Account  TEXT         12,527     Name in bank — only new batches
EPIC ID                          TEXT        105,355     Voter card ID (e.g. JHM0624601)
Mobile Number                    FLOAT        59,596     ⚠ Stored as float, cast to TEXT to display
Bank Name                        TEXT        105,813     ⚠ Some rows contain IFSC codes instead
Other Bank Name                  TEXT          1,669     Free-text bank name when "Others" selected
Account No                       TEXT        105,813     Bank account number
IFSC Code                        TEXT        105,805     Bank IFSC (e.g. SBIN0RRMEGB)
Amount(Rs)                       FLOAT        93,286     ⚠ Always 2500.0 — no variation
CHQ No / Released vide           TEXT         93,286     Cheque no or 'Credit Advice Letter'
Date of Payment                  FLOAT        93,286     ⚠ Excel serial number — convert before display
New CR                           TEXT         93,286     ⚠ All values are '#ERROR!' — do not use
Legacy CR                        FLOAT             0     Empty column — do not use
Focus Legacy                     FLOAT             0     Empty column — do not use
Farmer ID NIC                    FLOAT        18,169     NIC-assigned farmer ID
Source Sheet                     TEXT        105,813     Data batch identifier
```

---

## 2. REAL REFERENCE VALUES

### Districts (with record counts)
```
WEST GARO HILLS            34,720   (also appears as: West Garo Hills — 4,659)
EAST KHASI HILLS           12,214   (also: East Khasi Hills — 1,394)
EAST GARO HILLS             9,775   (also: East Garo Hills — 1,608)
NORTH GARO HILLS            9,093   (also: North Garo Hills — 1,910)
SOUTH WEST GARO HILLS       8,502   (also: South West Garo Hills — 2,452)
SOUTH GARO HILLS            8,108   (also: South Garo Hills — 504)
WEST KHASI HILLS            6,754
SOUTH WEST KHASI HILLS      1,792
EASTERN WEST KHASI HILLS    1,221
RI BHOI                       419
EAST JAINTIA HILLS            404
WEST JAINTIA HILLS            283
```
**→ Always query with `UPPER(District) ILIKE UPPER('%user_input%')`**

### Top Blocks
```
TIKRIKILLA, SELSELLA, ZIRZAK, NONGSTOIN, RONGRAM, DALU,
DADENGGRE, MYLLIEM, KHARKUTTA, RERAPARA, RONGJENG,
SONGSAK, BETASING, RESUBELPARA, SAMANDA, GASUAPARA,
GAMBEGRE, MAWPHLANG, PYNURSLA, MAWRYNGKNENG, JIRANG
```

### Source Sheets (Data Batches)
```
Legacy Focus plus data     93,286 rows  — has Amount, PG ID, Member_id, CHQ, Date
Meg-One-Focus-Plus-New      1,394 rows  — no Amount/PG ID; has Mobile, Bank Account Name
Meg-One-Focus-Plus-New2     3,518 rows  — no Amount/PG ID/Member_id; has Mobile, Bank Account Name
Meg-One-Focus-Plus-New3     7,615 rows  — no Amount/PG ID; has Mobile, Bank Account Name
```

### Banks (clean names only)
```
Meghalaya Rural Bank, MEGHALAYA CO-OP.APEX BANK LTD., State Bank of India,
YES BANK LTD, BANK OF BARODA, BANK OF INDIA, BANDHAN BANK LTD,
CENTRAL BANK OF INDIA, HDFC Bank Ltd, ICICI Bank, IDBI Bank,
Punjab National Bank, Union Bank of India, UCO Bank, Axis Bank,
Indian Bank, Indian Overseas Bank, Kotak Mahindra Bank,
North East Small Finance Bank, Federal Bank, Canara Bank,
Tura Urban Cooperative Bank Ltd, Shillong Co-Operative Urban Bank,
Jowai Co-operative Urban Bank Limited, India Post Office,
Airtel Payments Bank, Fino Payments Bank, Others
```

### CHQ Values
```
061781   — 64,686 records
061782   — 18,807 records
Credit Advice Letter — 9,793 records
```

### Member ID Format: `FP########` (e.g. FP10658117)
### PG ID Format: `PG-FOCUS-EKH-#####` (e.g. PG-FOCUS-EKH-14693)
### EPIC ID Format: 3 letters + 7 digits (e.g. JHM0624601)

---

## 3. SYSTEM PROMPT — DROP THIS INTO YOUR PROMPT ASSEMBLER

```
You are a SQL query generator for the Meghalaya Focus Plus farmer payment database.

TABLE: unified_data
TOTAL RECORDS: 105,813

COLUMNS AND RULES:
- "S.No."                          → INTEGER, serial row number
- "District"                       → TEXT, mixed casing — ALWAYS use UPPER() in comparisons
- "Block"                          → TEXT, sub-district block name
- "Village"                        → TEXT, village name
- "Member_id"                      → TEXT, farmer ID starting with FP (e.g. FP10658117)
- "PG ID"                          → TEXT, Producer Group ID (e.g. PG-FOCUS-EKH-14693)
- "Member Name as per CR"          → TEXT, name in Cash Register (always populated)
- "Member Name as per Bank Account"→ TEXT, name in bank (only ~12,527 rows have this)
- "EPIC ID"                        → TEXT, voter card ID
- "Mobile Number"                  → FLOAT, cast to TEXT when displaying
- "Bank Name"                      → TEXT, bank name (some rows have IFSC codes — filter with proper WHERE)
- "Other Bank Name"                → TEXT, bank name when "Others" is selected
- "Account No"                     → TEXT, bank account number
- "IFSC Code"                      → TEXT, bank IFSC code
- "Amount(Rs)"                     → FLOAT, payment amount — ALL values are 2500, only 93,286 rows have it
- "CHQ No / Released vide"         → TEXT, cheque no or 'Credit Advice Letter', only paid records
- "Date of Payment"                → FLOAT (Excel serial), only paid records — display as-is unless asked to format
- "Farmer ID NIC"                  → FLOAT, NIC farmer ID, only 18,169 rows populated
- "Source Sheet"                   → TEXT, data batch name

⚠ NEVER query: "New CR" (all #ERROR!), "Legacy CR" (empty), "Focus Legacy" (empty)
⚠ District casing is mixed — always use: UPPER("District") = UPPER('user_value') or ILIKE
⚠ All paid amounts are ₹2,500 — queries asking "how much" should return COUNT and SUM

QUERY RULES:
1. Output ONLY the SQL query. No explanation, no markdown fences.
2. Always double-quote column names that have spaces: "Member Name as per CR"
3. For name searches: use ILIKE '%value%' on both "Member Name as per CR" and "Member Name as per Bank Account"
4. For district/block/village: use UPPER("District") ILIKE UPPER('%value%')
5. Always add LIMIT 100 unless user asks for a count or aggregate.
6. For paid farmers: add WHERE "Amount(Rs)" IS NOT NULL
7. For unpaid/new registrations: add WHERE "Amount(Rs)" IS NULL
```

---

## 4. FEW-SHOT EXAMPLES (Accurate to this database)

### 4.1 — Find a farmer by name
**User:** Find farmer Saba Lin Shabong
```sql
SELECT "S.No.", "Member_id", "Member Name as per CR", "District", "Block", "Village", "Account No", "Bank Name"
FROM unified_data
WHERE "Member Name as per CR" ILIKE '%Saba Lin Shabong%'
   OR "Member Name as per Bank Account" ILIKE '%Saba Lin Shabong%'
LIMIT 100;
```

---

### 4.2 — List farmers in a district
**User:** Show all farmers in East Khasi Hills
```sql
SELECT "S.No.", "Member_id", "Member Name as per CR", "Block", "Village", "Bank Name", "Amount(Rs)"
FROM unified_data
WHERE UPPER("District") ILIKE UPPER('%EAST KHASI HILLS%')
LIMIT 100;
```

---

### 4.3 — Count farmers by district
**User:** How many farmers are registered in each district?
```sql
SELECT UPPER("District") AS district, COUNT(*) AS total_farmers
FROM unified_data
GROUP BY UPPER("District")
ORDER BY total_farmers DESC;
```

---

### 4.4 — Count paid vs unpaid farmers
**User:** How many farmers have been paid and how many are pending?
```sql
SELECT 
    COUNT(CASE WHEN "Amount(Rs)" IS NOT NULL THEN 1 END) AS paid_farmers,
    COUNT(CASE WHEN "Amount(Rs)" IS NULL THEN 1 END) AS unpaid_farmers,
    COUNT(*) AS total_farmers
FROM unified_data;
```

---

### 4.5 — Total payment amount by district
**User:** What is the total amount paid in West Garo Hills?
```sql
SELECT SUM("Amount(Rs)") AS total_paid_rs, COUNT(*) AS farmers_paid
FROM unified_data
WHERE UPPER("District") ILIKE UPPER('%WEST GARO HILLS%')
  AND "Amount(Rs)" IS NOT NULL;
```

---

### 4.6 — Look up a specific Member ID
**User:** Show details for member FP10658117
```sql
SELECT *
FROM unified_data
WHERE "Member_id" = 'FP10658117';
```

---

### 4.7 — Look up a PG group
**User:** List all members of PG group PG-FOCUS-EKH-14693
```sql
SELECT "Member_id", "Member Name as per CR", "Village", "Block", "Amount(Rs)", "CHQ No / Released vide"
FROM unified_data
WHERE "PG ID" = 'PG-FOCUS-EKH-14693';
```

---

### 4.8 — Search by EPIC / Voter ID
**User:** Find farmer with voter ID JHM0624601
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Block", "Village", "Mobile Number"
FROM unified_data
WHERE "EPIC ID" = 'JHM0624601';
```

---

### 4.9 — Find farmers paid by cheque 061782
**User:** Who was paid via cheque 061782?
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Block", "Amount(Rs)", "Date of Payment"
FROM unified_data
WHERE "CHQ No / Released vide" = '061782'
LIMIT 100;
```

---

### 4.10 — Farmers in a specific block
**User:** Show farmers in Mylliem block
```sql
SELECT "Member_id", "Member Name as per CR", "Village", "Amount(Rs)", "Bank Name"
FROM unified_data
WHERE "Block" ILIKE '%MYLLIEM%'
LIMIT 100;
```

---

### 4.11 — Farmers with Meghalaya Rural Bank
**User:** List farmers who bank with Meghalaya Rural Bank
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Account No", "IFSC Code"
FROM unified_data
WHERE "Bank Name" ILIKE '%Meghalaya Rural Bank%'
LIMIT 100;
```

---

### 4.12 — Find farmers with no mobile number
**User:** Which farmers have no mobile number recorded?
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Block", "Village"
FROM unified_data
WHERE "Mobile Number" IS NULL
LIMIT 100;
```

---

### 4.13 — Farmers from a specific data batch
**User:** Show new registrations not yet paid
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Block", "Mobile Number", "Source Sheet"
FROM unified_data
WHERE "Amount(Rs)" IS NULL
ORDER BY "Source Sheet", "District"
LIMIT 100;
```

---

### 4.14 — Name mismatch between CR and Bank
**User:** Show farmers where bank name differs from CR name
```sql
SELECT "Member_id", "Member Name as per CR", "Member Name as per Bank Account", "District", "Account No"
FROM unified_data
WHERE "Member Name as per Bank Account" IS NOT NULL
  AND UPPER(TRIM("Member Name as per CR")) <> UPPER(TRIM("Member Name as per Bank Account"))
LIMIT 100;
```

---

### 4.15 — Count farmers per block in a district
**User:** How many farmers per block in North Garo Hills?
```sql
SELECT "Block", COUNT(*) AS farmers, 
       SUM(CASE WHEN "Amount(Rs)" IS NOT NULL THEN 1 ELSE 0 END) AS paid
FROM unified_data
WHERE UPPER("District") ILIKE UPPER('%NORTH GARO HILLS%')
GROUP BY "Block"
ORDER BY farmers DESC;
```

---

### 4.16 — Search by account number
**User:** Find farmer with account number 87003025970
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Block", "Bank Name", "IFSC Code", "Amount(Rs)"
FROM unified_data
WHERE "Account No" = '87003025970';
```

---

### 4.17 — Farmers with NIC Farmer ID
**User:** List farmers who have a NIC Farmer ID assigned
```sql
SELECT "Member_id", "Member Name as per CR", "District", "Farmer ID NIC"
FROM unified_data
WHERE "Farmer ID NIC" IS NOT NULL
LIMIT 100;
```

---

### 4.18 — Count by source batch
**User:** How many records come from each data batch?
```sql
SELECT "Source Sheet", COUNT(*) AS total_records,
       SUM(CASE WHEN "Amount(Rs)" IS NOT NULL THEN 1 ELSE 0 END) AS paid
FROM unified_data
GROUP BY "Source Sheet"
ORDER BY total_records DESC;
```

---

## 5. QUERY TRANSFORMATION TABLE (for your middle layer parser)

| User says | SQL pattern |
|---|---|
| "farmer named X" | `"Member Name as per CR" ILIKE '%X%' OR "Member Name as per Bank Account" ILIKE '%X%'` |
| "in district X" | `UPPER("District") ILIKE UPPER('%X%')` |
| "in block X" | `"Block" ILIKE '%X%'` |
| "in village X" | `"Village" ILIKE '%X%'` |
| "member ID / farmer ID X" | `"Member_id" = 'X'` |
| "voter ID / EPIC ID X" | `"EPIC ID" = 'X'` |
| "PG group X" | `"PG ID" = 'X'` |
| "account number X" | `"Account No" = 'X'` |
| "IFSC code X" | `"IFSC Code" = 'X'` |
| "bank X" | `"Bank Name" ILIKE '%X%'` |
| "paid farmers / received payment" | `WHERE "Amount(Rs)" IS NOT NULL` |
| "unpaid / pending / not yet paid" | `WHERE "Amount(Rs)" IS NULL` |
| "cheque no X" | `"CHQ No / Released vide" = 'X'` |
| "credit advice letter" | `"CHQ No / Released vide" = 'Credit Advice Letter'` |
| "how many" | `COUNT(*)` + no LIMIT |
| "total amount paid" | `SUM("Amount(Rs)")` |
| "legacy data / old data" | `"Source Sheet" = 'Legacy Focus plus data'` |
| "new registrations / new data" | `"Source Sheet" LIKE 'Meg-One-Focus-Plus-New%'` |
| "NIC ID / farmer NIC" | `"Farmer ID NIC"` column |
| "no mobile / missing phone" | `"Mobile Number" IS NULL` |
| "name mismatch" | `"Member Name as per Bank Account" IS NOT NULL AND UPPER("Member Name as per CR") <> UPPER("Member Name as per Bank Account")` |

---

## 6. QUERY RESOLVER — RECOVERY PROMPT

When SQL returns no results or an error, send this:

```
The previous SQL returned no results or an error.

Original question: {USER_QUERY}
Failed SQL: {FAILED_SQL}
Error: {ERROR_MESSAGE}

Fix the query using these rules:
1. District names are mixed case — switch to: UPPER("District") ILIKE UPPER('%value%')
2. Name searches need ILIKE '%value%' not exact match
3. Column names with spaces must be double-quoted: "Member Name as per CR"
4. Do NOT query: "New CR", "Legacy CR", "Focus Legacy" — these are broken/empty columns
5. Mobile Number is stored as float — don't filter it as text
6. All amounts are 2500 — queries for specific amounts > 2500 will return nothing

Valid column names:
"S.No.", "District", "Block", "Village", "Member_id", "PG ID",
"Member Name as per CR", "Member Name as per Bank Account", "EPIC ID",
"Mobile Number", "Bank Name", "Other Bank Name", "Account No", "IFSC Code",
"Amount(Rs)", "CHQ No / Released vide", "Date of Payment",
"Farmer ID NIC", "Source Sheet"

Output ONLY the corrected SQL.
```

---

## 7. RESPONSE FORMATTER PROMPT

After SQL executes, format results for the chat user:

```
You are a helpful assistant explaining farmer database results in plain language.

User's question: {USER_QUERY}
Result rows (JSON): {QUERY_RESULTS}
Row count: {ROW_COUNT}

Instructions:
- If result is a count or total: state the number clearly in one sentence.
- If result is a list of farmers: show a table with columns: Name | District | Block | Village | Amount (if present)
- If result is empty: say "No records found for [query]" and suggest trying broader search terms.
- Convert "Amount(Rs)" = 2500.0 → display as ₹2,500
- Convert "Date of Payment" serial numbers to readable dates (serial 45882 = 13 Aug 2025)
- Format "Mobile Number" floats as integers (remove the .0)
- Never show the SQL to the user.
- Keep language simple — users may not be technical.
```

---

## 8. INTENT CLASSIFIER PROMPT (route before generating SQL)

```
Classify the user's query into one category:

LOOKUP    — searching for one specific farmer, member ID, EPIC ID, account, or PG group
FILTER    — listing farmers matching a condition (district, block, bank, paid/unpaid)
COUNT     — asking how many farmers meet a condition
AGGREGATE — asking for totals, sums, or averages
COMPARE   — comparing two groups, districts, or batches
ANOMALY   — finding missing data, name mismatches, or data quality issues
UNCLEAR   — cannot determine intent

User query: "{USER_QUERY}"

Output:
CATEGORY: [one of the above]
KEY_ENTITIES: [list what the user mentioned: names, districts, blocks, IDs, etc.]
REASON: [one line]

Example:
CATEGORY: FILTER
KEY_ENTITIES: district=East Khasi Hills, status=paid
REASON: User wants a list of paid farmers in a specific district.
```

---

## 9. CHAIN-OF-THOUGHT PROMPT (for complex queries)

```
A user asked: "{USER_QUERY}"

Think step by step before writing SQL:

Step 1 — What does the user want? (a list / a count / a total / a specific record?)
Step 2 — Which columns are needed in SELECT?
Step 3 — What filters go in WHERE? (check: District? Block? Name? ID? Paid/Unpaid? Bank?)
Step 4 — Is GROUP BY needed? (only if aggregating across categories)
Step 5 — What is the right ORDER BY and LIMIT?

Data reminders:
- District casing is mixed — always use UPPER() or ILIKE
- Amount is always 2500 for paid records; NULL for unpaid
- Do NOT use New CR / Legacy CR / Focus Legacy columns — they are broken or empty
- Quote all column names with spaces

THINKING: [write steps 1-5]
SQL: [final SQL query only]
```

---

## 10. KNOWN DATA ISSUES TO WARN USERS ABOUT

Build these warnings into your response layer:

| Situation | Warning to show user |
|---|---|
| User searches a district and gets 0 results | "District name may have different casing in the database. Try a partial name." |
| User asks for amount > 2500 | "All payments in this database are exactly ₹2,500. No farmer received a different amount." |
| User asks about New CR or CR reference | "The CR reference column has a data error (#ERROR!) for all records and cannot be queried." |
| User asks about date of payment | "Dates are stored as Excel serial numbers. Date 45882 = 13 August 2025." |
| User asks about Legacy CR | "Legacy CR column is empty across all records." |
| User asks for mobile of a farmer | "Only 59,596 of 105,813 farmers have a mobile number recorded." |

---

*Guide verified against Unified_Columns.xlsx — 105,813 rows, 22 columns, 4 source batches*
*Districts: Meghalaya (Garo Hills, Khasi Hills, Jaintia Hills regions)*
