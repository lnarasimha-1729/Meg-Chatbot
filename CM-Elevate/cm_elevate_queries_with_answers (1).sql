-- ============================================================
-- CM ELEVATE - SQL QUERIES WITH ANSWERS (FIXED)
-- Table: cm_elevate | Total Records: 2,847
-- Active Records (excl. desanctioned & refused): 2,732
-- ============================================================
--
-- FIXES APPLIED:
--   [FIX 1] All queries exclude desanctioned rows (WHERE desanctioned IS NULL)
--   [FIX 2] All analytical queries exclude refused rows
--           (AND (refused_y_n IS NULL OR refused_y_n <> 'Y'))
--   [FIX 3] Pending queries catch NULL loan_disbursed
--           (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
--   [FIX 4] COALESCE(sanctioned, 0) used where sanctioned can be NULL
--   [FIX 5] Refusal queries (Q77-Q81) use full table intentionally
--   [FIX 6] Q89 returns empty — no district has 100% disbursement rate
-- ============================================================


-- ============================================================
-- SECTION 1: OVERVIEW / SUMMARY
-- ============================================================

-- Q1. Total number of beneficiaries
-- ANSWER: 2,742
SELECT COUNT(*) AS total_beneficiaries
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q2. Total number of schemes
-- ANSWER: 13
SELECT COUNT(DISTINCT scheme) AS total_schemes
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q3. Total number of districts covered
-- ANSWER: 12
SELECT COUNT(DISTINCT district) AS total_districts
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q4. Total number of blocks covered
-- ANSWER: 57
SELECT COUNT(DISTINCT block) AS total_blocks
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q5. Total number of villages covered
-- ANSWER: 1,134
SELECT COUNT(DISTINCT village) AS total_villages
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q6. Total sanctioned amount
-- ANSWER: ₹1,364,405,214.00
SELECT ROUND(SUM(COALESCE(sanctioned, 0))::numeric, 2) AS total_sanctioned
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q7. Total bank sanctioned amount
-- ANSWER: ₹393,167,798.40
SELECT ROUND(SUM(bank_santioned)::numeric, 2) AS total_bank_sanctioned
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q8. Total subsidy disbursed
-- ANSWER: ₹510,535,180.25
SELECT ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q9. Total loan disbursed
-- ANSWER: ₹298,553,351.50
SELECT ROUND(SUM(total_loan)::numeric, 2) AS total_loan_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q10. Total amount disbursed (subsidy + loan)
-- ANSWER: ₹809,088,531.75
SELECT ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q11. Average disbursement per beneficiary
-- ANSWER: ₹295,180.06
SELECT ROUND(AVG(total_disbursement)::numeric, 2) AS avg_disbursement_per_beneficiary
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q12. Average sanctioned amount per beneficiary
-- ANSWER: ₹497,594.90
SELECT ROUND(AVG(COALESCE(sanctioned, 0))::numeric, 2) AS avg_sanctioned
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q13. Total applications awaiting disbursement
-- ANSWER: 1,949
SELECT COUNT(*) AS awaiting_disbursement
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q14. Total applications disbursed
-- ANSWER: 793
SELECT COUNT(*) AS disbursed
FROM cm_elevate
WHERE loan_disbursed = 'disbursed'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q15. Total refused applications
-- ANSWER: 16
-- NOTE: No exclusion filter — this query IS about refused rows
SELECT COUNT(*) AS total_refused
FROM cm_elevate
WHERE refused_y_n = 'Y';

-- Q16. Overall summary (single query)
-- ANSWER:
--   total_beneficiaries : 2,742
--   total_schemes       : 13
--   total_districts     : 12
--   total_blocks        : 57
--   total_villages      : 1,134
--   total_sanctioned    : ₹1,364,405,214.00
--   total_subsidy       : ₹510,535,180.25
--   total_loan          : ₹298,553,351.50
--   total_disbursed     : ₹809,088,531.75
--   pending             : 1,949
--   disbursed           : 793
--   refused             : 16
SELECT
    SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN 1 ELSE 0 END) AS total_beneficiaries,
    COUNT(DISTINCT CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN scheme END) AS total_schemes,
    COUNT(DISTINCT CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN district END) AS total_districts,
    COUNT(DISTINCT CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN block END) AS total_blocks,
    COUNT(DISTINCT CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN village END) AS total_villages,
    ROUND(SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN COALESCE(sanctioned, 0) ELSE 0 END)::numeric, 2) AS total_sanctioned,
    ROUND(SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN COALESCE(total_subsidy_disbursement, 0) ELSE 0 END)::numeric, 2) AS total_subsidy,
    ROUND(SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN COALESCE(total_loan, 0) ELSE 0 END)::numeric, 2) AS total_loan,
    ROUND(SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') THEN COALESCE(total_disbursement, 0) ELSE 0 END)::numeric, 2) AS total_disbursed,
    SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') AND (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL) THEN 1 ELSE 0 END) AS pending,
    SUM(CASE WHEN desanctioned IS NULL AND (refused_y_n IS NULL OR refused_y_n <> 'Y') AND loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed,
    SUM(CASE WHEN refused_y_n = 'Y' THEN 1 ELSE 0 END) AS refused
FROM cm_elevate;


-- ============================================================
-- SECTION 2: SCHEME-WISE ANALYSIS
-- ============================================================

-- Q17. Beneficiaries per scheme
-- ANSWER:
--   Meghalaya Piggery Development Scheme               : 1,337
--   Meghalaya Poultry Farming Scheme                   :   449
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :   189
--   PRIME Agriculture Response Vehicle Scheme          :   181
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :   172
--   Meghalaya Dairy Development Scheme                 :   132
--   PRIME Tourism Vehicle Scheme                       :    93
--   Meghalaya Agriculture Warehouse Scheme             :    83
--   Meghalaya Goat Farming Scheme                      :    47
--   Meghalaya Any Business Venture Scheme              :    30
--   Meghalaya Common Facility Center Scheme            :    21
--   Meghalaya Sports & Wellness Scheme                 :     7
--   Meghalaya Motorcaravan Scheme                      :     1
SELECT scheme, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY beneficiaries DESC;

-- Q18. Total disbursement per scheme
-- ANSWER:
--   Meghalaya Agriculture Warehouse Scheme             : ₹215,690,869.25
--   PRIME Tourism Vehicle Scheme                       : ₹146,977,824.60
--   PRIME Agriculture Response Vehicle Scheme          : ₹139,953,043.90
--   Meghalaya Piggery Development Scheme               :  ₹94,178,750.00
--   Meghalaya Sports & Wellness Scheme                 :  ₹58,700,000.00
--   Meghalaya Any Business Venture Scheme              :  ₹53,831,590.00
--   Meghalaya Poultry Farming Scheme                   :  ₹39,857,000.00
--   Meghalaya Common Facility Center Scheme            :  ₹34,408,450.00
--   Meghalaya Dairy Development Scheme                 :  ₹12,710,752.00
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :   ₹5,874,220.00
--   Meghalaya Motorcaravan Scheme                      :   ₹5,000,000.00
--   Meghalaya Goat Farming Scheme                      :   ₹1,880,000.00
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :      ₹26,000.00
SELECT scheme, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY total_disbursed DESC;

-- Q19. Total subsidy per scheme
-- ANSWER:
--   Meghalaya Agriculture Warehouse Scheme             : ₹167,737,468.25
--   Meghalaya Piggery Development Scheme               :  ₹83,680,250.00
--   PRIME Tourism Vehicle Scheme                       :  ₹69,540,180.10
--   PRIME Agriculture Response Vehicle Scheme          :  ₹66,672,887.90
--   Meghalaya Sports & Wellness Scheme                 :  ₹35,200,000.00
--   Meghalaya Poultry Farming Scheme                   :  ₹26,717,000.00
--   Meghalaya Common Facility Center Scheme            :  ₹23,426,722.00
--   Meghalaya Any Business Venture Scheme              :  ₹18,196,420.00
--   Meghalaya Dairy Development Scheme                 :   ₹9,750,000.00
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :   ₹5,234,220.00
--   Meghalaya Motorcaravan Scheme                      :   ₹2,500,000.00
--   Meghalaya Goat Farming Scheme                      :   ₹1,880,000.00
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :           ₹0.00
SELECT scheme, ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY total_subsidy DESC;

-- Q20. Total loan per scheme
-- ANSWER:
--   PRIME Tourism Vehicle Scheme                       :  ₹77,437,644.50
--   PRIME Agriculture Response Vehicle Scheme          :  ₹73,280,156.00
--   Meghalaya Agriculture Warehouse Scheme             :  ₹47,953,401.00
--   Meghalaya Any Business Venture Scheme              :  ₹35,635,170.00
--   Meghalaya Sports & Wellness Scheme                 :  ₹23,500,000.00
--   Meghalaya Poultry Farming Scheme                   :  ₹13,140,000.00
--   Meghalaya Common Facility Center Scheme            :  ₹10,981,728.00
--   Meghalaya Piggery Development Scheme               :  ₹10,498,500.00
--   Meghalaya Dairy Development Scheme                 :   ₹2,960,752.00
--   Meghalaya Motorcaravan Scheme                      :   ₹2,500,000.00
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :     ₹640,000.00
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :      ₹26,000.00
--   Meghalaya Goat Farming Scheme                      :           ₹0.00
SELECT scheme, ROUND(SUM(total_loan)::numeric, 2) AS total_loan
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY total_loan DESC;

-- Q21. Average disbursement per scheme
-- ANSWER:
--   Meghalaya Sports & Wellness Scheme                 :  ₹8,385,714.29
--   Meghalaya Motorcaravan Scheme                      :  ₹5,000,000.00
--   Meghalaya Agriculture Warehouse Scheme             :  ₹2,598,685.62
--   Meghalaya Any Business Venture Scheme              :  ₹1,794,386.20
--   Meghalaya Common Facility Center Scheme            :  ₹1,638,497.57
--   PRIME Tourism Vehicle Scheme                       :  ₹1,580,406.72
--   PRIME Agriculture Response Vehicle Scheme          :    ₹773,221.24
--   Meghalaya Dairy Development Scheme                 :     ₹96,293.58
--   Meghalaya Poultry Farming Scheme                   :     ₹88,768.37
--   Meghalaya Piggery Development Scheme               :     ₹70,440.35
--   Meghalaya Goat Farming Scheme                      :     ₹40,000.00
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :     ₹31,080.53
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :        ₹152.05
SELECT scheme, ROUND(AVG(total_disbursement)::numeric, 2) AS avg_disbursement
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY avg_disbursement DESC;

-- Q22. Pending disbursements per scheme
-- ANSWER:
--   Meghalaya Piggery Development Scheme               : 1,127
--   Meghalaya Poultry Farming Scheme                   :   230
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :   172
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :   170
--   Meghalaya Dairy Development Scheme                 :   109
--   PRIME Agriculture Response Vehicle Scheme          :    48
--   Meghalaya Goat Farming Scheme                      :    47
--   Meghalaya Agriculture Warehouse Scheme             :    23
--   PRIME Tourism Vehicle Scheme                       :    13
--   Meghalaya Sports & Wellness Scheme                 :     4
--   Meghalaya Common Facility Center Scheme            :     3
--   Meghalaya Any Business Venture Scheme              :     3
SELECT scheme, COUNT(*) AS pending
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY pending DESC;

-- Q23. Disbursed count per scheme
-- ANSWER:
--   Meghalaya Poultry Farming Scheme                   : 219
--   Meghalaya Piggery Development Scheme               : 210
--   PRIME Agriculture Response Vehicle Scheme          : 133
--   PRIME Tourism Vehicle Scheme                       :  80
--   Meghalaya Agriculture Warehouse Scheme             :  60
--   Meghalaya Any Business Venture Scheme              :  27
--   Meghalaya Dairy Development Scheme                 :  23
--   Meghalaya Common Facility Center Scheme            :  18
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :  17
--   Meghalaya Sports & Wellness Scheme                 :   3
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :   2
--   Meghalaya Motorcaravan Scheme                      :   1
SELECT scheme, COUNT(*) AS disbursed
FROM cm_elevate
WHERE loan_disbursed = 'disbursed'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY disbursed DESC;

-- Q24. Disbursement rate (%) per scheme
-- ANSWER:
--   Meghalaya Motorcaravan Scheme                      : 100.0%
--   Meghalaya Any Business Venture Scheme              :  90.0%
--   PRIME Tourism Vehicle Scheme                       :  86.0%
--   Meghalaya Common Facility Center Scheme            :  85.7%
--   PRIME Agriculture Response Vehicle Scheme          :  73.5%
--   Meghalaya Agriculture Warehouse Scheme             :  72.3%
--   Meghalaya Poultry Farming Scheme                   :  48.8%
--   Meghalaya Sports & Wellness Scheme                 :  42.9%
--   Meghalaya Dairy Development Scheme                 :  17.4%
--   Meghalaya Piggery Development Scheme               :  15.7%
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :   9.0%
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :   1.2%
--   Meghalaya Goat Farming Scheme                      :   0.0%
SELECT scheme,
    COUNT(*) AS total,
    SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed,
    ROUND(100.0 * SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS disbursement_rate_pct
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY disbursement_rate_pct DESC;

-- Q25. Schemes with zero disbursement (some beneficiaries have ₹0 disbursed)
-- ANSWER:
--   Meghalaya Agriculture Warehouse Scheme
--   Meghalaya Common Facility Center Scheme
--   PRIME Agriculture Response Vehicle Scheme
--   PRIME Tourism Vehicle Scheme
--   Meghalaya Sericulture & Weaving Scheme (Weaving)
--   Meghalaya Sericulture & Weaving Scheme (Spinning)
SELECT DISTINCT scheme
FROM cm_elevate
WHERE (total_disbursement IS NULL OR total_disbursement = 0)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q26. Refused applications per scheme
-- ANSWER:
--   PRIME Tourism Vehicle Scheme                       : 7
--   Meghalaya Any Business Venture Scheme              : 3
--   Meghalaya Piggery Development Scheme               : 3
--   Meghalaya Sports & Wellness Scheme                 : 2
--   PRIME Agriculture Response Vehicle Scheme          : 1
SELECT scheme, COUNT(*) AS refused
FROM cm_elevate
WHERE refused_y_n = 'Y'
GROUP BY scheme
ORDER BY refused DESC;

-- Q27. Sanctioned vs disbursed gap per scheme
-- ANSWER (top gaps):
--   Meghalaya Agriculture Warehouse Scheme             : gap ₹203,662,655.75
--   Meghalaya Piggery Development Scheme               : gap  ₹72,946,250.00
--   PRIME Agriculture Response Vehicle Scheme          : gap  ₹59,582,767.10
--   Meghalaya Sports & Wellness Scheme                 : gap  ₹53,300,000.00
--   PRIME Tourism Vehicle Scheme                       : gap  ₹31,586,080.40
--   Meghalaya Motorcaravan Scheme                      : gap           ₹0.00
SELECT scheme,
    ROUND(SUM(COALESCE(sanctioned, 0))::numeric, 2) AS total_sanctioned,
    ROUND(SUM(COALESCE(total_disbursement, 0))::numeric, 2) AS total_disbursed,
    ROUND(SUM(COALESCE(sanctioned, 0) - COALESCE(total_disbursement, 0))::numeric, 2) AS gap
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY gap DESC;

-- Q28. Full scheme-wise summary
-- ANSWER: See Q17–Q27 above for individual breakdowns
SELECT scheme,
    COUNT(*) AS beneficiaries,
    ROUND(SUM(COALESCE(sanctioned, 0))::numeric, 2) AS sanctioned,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS loan,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    ROUND(AVG(total_disbursement)::numeric, 2) AS avg_disbursed,
    SUM(CASE WHEN loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL THEN 1 ELSE 0 END) AS pending,
    SUM(CASE WHEN refused_y_n = 'Y' THEN 1 ELSE 0 END) AS refused
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY total_disbursed DESC;


-- ============================================================
-- SECTION 3: DISTRICT-WISE ANALYSIS
-- ============================================================

-- Q29. Beneficiaries per district
-- ANSWER:
--   West Garo Hills             : 472    South Garo Hills            :  90
--   Ri Bhoi                     : 456    East Jaintia Hills          :  35
--   South West Garo Hills       : 453
--   East Khasi Hills            : 233
--   West Khasi Hills            : 216
--   Eastern West Khasi Hills    : 206
--   East Garo Hills             : 197
--   West Jaintia Hills          : 151
--   North Garo Hills            : 140
--   South West Khasi Hills      :  93
SELECT district, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY beneficiaries DESC;

-- Q30. Total disbursement per district
-- ANSWER:
--   East Khasi Hills            : ₹225,671,708.35
--   West Garo Hills             : ₹133,235,889.65
--   Ri Bhoi                     :  ₹68,762,289.50
--   South West Garo Hills       :  ₹60,863,092.00
--   West Khasi Hills            :  ₹59,370,778.00
--   West Jaintia Hills          :  ₹46,216,688.45
--   East Garo Hills             :  ₹44,939,067.80
--   South Garo Hills            :  ₹42,012,551.30
--   Eastern West Khasi Hills    :  ₹39,286,388.85
--   South West Khasi Hills      :  ₹39,174,752.90
--   North Garo Hills            :  ₹25,976,432.90
--   East Jaintia Hills          :  ₹23,578,892.05
SELECT district, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q31. Total subsidy per district
-- ANSWER:
--   East Khasi Hills            : ₹119,580,621.35
--   West Garo Hills             :  ₹88,312,003.65
--   South West Garo Hills       :  ₹47,186,933.00
--   Ri Bhoi                     :  ₹45,263,602.50
--   West Khasi Hills            :  ₹37,915,283.50
--   West Jaintia Hills          :  ₹33,944,022.45
--   South Garo Hills            :  ₹30,213,970.30
--   East Garo Hills             :  ₹29,144,114.80
--   South West Khasi Hills      :  ₹25,774,223.90
--   Eastern West Khasi Hills    :  ₹21,038,033.85
--   North Garo Hills            :  ₹16,656,823.90
--   East Jaintia Hills          :  ₹15,505,547.05
SELECT district, ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_subsidy DESC;

-- Q32. Total loan per district
-- ANSWER:
--   East Khasi Hills            : ₹106,091,087.00
--   West Garo Hills             :  ₹44,923,886.00
--   Ri Bhoi                     :  ₹23,498,687.00
--   West Khasi Hills            :  ₹21,455,494.50
--   Eastern West Khasi Hills    :  ₹18,248,355.00
--   East Garo Hills             :  ₹15,794,953.00
--   South West Garo Hills       :  ₹13,676,159.00
--   South West Khasi Hills      :  ₹13,400,529.00
--   West Jaintia Hills          :  ₹12,272,666.00
--   South Garo Hills            :  ₹11,798,581.00
--   North Garo Hills            :   ₹9,319,609.00
--   East Jaintia Hills          :   ₹8,073,345.00
SELECT district, ROUND(SUM(total_loan)::numeric, 2) AS total_loan
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_loan DESC;

-- Q33. Average disbursement per district
-- ANSWER:
--   East Khasi Hills            : ₹968,548.10
--   East Jaintia Hills          : ₹673,682.63
--   South Garo Hills            : ₹466,806.13
--   South West Khasi Hills      : ₹421,233.90
--   West Jaintia Hills          : ₹306,070.78
--   West Garo Hills             : ₹282,279.43
--   West Khasi Hills            : ₹274,864.71
--   East Garo Hills             : ₹228,117.10
--   Eastern West Khasi Hills    : ₹190,710.63
--   North Garo Hills            : ₹186,880.81
--   Ri Bhoi                     : ₹150,794.49
--   South West Garo Hills       : ₹134,355.61
SELECT district, ROUND(AVG(total_disbursement)::numeric, 2) AS avg_disbursement
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY avg_disbursement DESC;

-- Q34. Pending disbursements per district
-- ANSWER:
--   South West Garo Hills       : 418    North Garo Hills            : 104
--   West Garo Hills             : 387    Eastern West Khasi Hills    :  88
--   Ri Bhoi                     : 356    East Khasi Hills            :  83
--   East Garo Hills             : 153    South Garo Hills            :  64
--   West Jaintia Hills          : 117    South West Khasi Hills      :  62
--   West Khasi Hills            : 104    East Jaintia Hills          :  13
SELECT district, COUNT(*) AS pending
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY pending DESC;

-- Q35. Disbursement rate (%) per district
-- ANSWER:
--   East Khasi Hills            : 64.4%
--   East Jaintia Hills          : 62.9%
--   Eastern West Khasi Hills    : 57.3%
--   West Khasi Hills            : 51.9%
--   South West Khasi Hills      : 33.3%
--   South Garo Hills            : 28.9%
--   North Garo Hills            : 25.7%
--   West Jaintia Hills          : 22.5%
--   East Garo Hills             : 22.3%
--   Ri Bhoi                     : 21.9%
--   West Garo Hills             : 18.0%
--   South West Garo Hills       :  7.7%
SELECT district,
    COUNT(*) AS total,
    SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed,
    ROUND(100.0 * SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS disbursement_rate_pct
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY disbursement_rate_pct DESC;

-- Q36. Refused applications per district
-- ANSWER:
--   West Garo Hills : 4 | Ri Bhoi : 3 | West Khasi Hills : 3
--   South West Garo Hills : 2 | North Garo Hills, East Khasi Hills,
--   East Garo Hills, South West Khasi Hills : 1 each
SELECT district, COUNT(*) AS refused
FROM cm_elevate
WHERE refused_y_n = 'Y'
GROUP BY district
ORDER BY refused DESC;

-- Q37. District-wise scheme distribution
-- ANSWER: See full table output — 12 districts × 13 schemes breakdown
SELECT district, scheme, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district, scheme
ORDER BY district, beneficiaries DESC;

-- Q38. Full district-wise summary
-- ANSWER: See Q29–Q36 above for individual breakdowns
SELECT district,
    COUNT(*) AS beneficiaries,
    COUNT(DISTINCT scheme) AS schemes,
    COUNT(DISTINCT block) AS blocks,
    COUNT(DISTINCT village) AS villages,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS loan,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    SUM(CASE WHEN loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL THEN 1 ELSE 0 END) AS pending
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;


-- ============================================================
-- SECTION 4: SPECIFIC DISTRICT QUERIES
-- ============================================================

-- Q39. East Khasi Hills - full summary
-- ANSWER: beneficiaries=233 | disbursed=₹225,671,708.35
--         subsidy=₹119,580,621.35 | loan=₹106,091,087.00
SELECT ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS total_loan,
    COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE district = 'East Khasi Hills'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q40. North Garo Hills - full summary
-- ANSWER: beneficiaries=140 | disbursed=₹25,976,432.90
--         subsidy=₹16,656,823.90 | loan=₹9,319,609.00
SELECT ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS total_loan,
    COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE district = 'North Garo Hills'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q41. West Garo Hills - full summary
-- ANSWER: beneficiaries=472 | disbursed=₹133,235,889.65
--         subsidy=₹88,312,003.65 | loan=₹44,923,886.00
SELECT ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS total_loan,
    COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE district = 'West Garo Hills'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q42. Ri Bhoi - full summary
-- ANSWER: beneficiaries=456 | disbursed=₹68,762,289.50
--         subsidy=₹45,263,602.50 | loan=₹23,498,687.00
SELECT ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS total_loan,
    COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE district = 'Ri Bhoi'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q43. Scheme-wise breakdown for each district
-- ANSWER: See full table output
SELECT district, scheme, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district, scheme
ORDER BY district, total_disbursed DESC;


-- ============================================================
-- SECTION 5: BLOCK & VILLAGE ANALYSIS
-- ============================================================

-- Q44. Top 10 blocks by beneficiaries
-- ANSWER:
--   Zikzak (South West Garo Hills)           : 241
--   Rongram (West Garo Hills)                : 205
--   Umling (Ri Bhoi)                         : 200
--   Rerapara (South West Garo Hills)         : 132
--   Nongstoin (West Khasi Hills)             : 129
--   Mawthadraishan (Eastern West Khasi Hills): 116
--   Umsning (Ri Bhoi)                        : 103
--   Bhoirymbong (Ri Bhoi)                    :  94
--   Mairang (Eastern West Khasi Hills)       :  90
--   Tikrikilla (West Garo Hills)             :  87
SELECT block, district, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY block, district
ORDER BY beneficiaries DESC
LIMIT 10;

-- Q45. Top 10 villages by beneficiaries
-- ANSWER:
--   Others / Nongstoin / West Khasi Hills    :  48
--   Nandichar I / Zikzak                     :  31
--   Liarkhla / Bhoirymbong                   :  21
--   Bolchugre / Rerapara                     :  21
--   Nartap / Umling                          :  20
--   Samin Songkama / Songsak                 :  18
--   Lower Damalgre / Rerapara               :  15
--   Ampanggre / Rongram                      :  15
--   Others / Samanda                         :  15
--   Hatibelpara / Zikzak                     :  14
SELECT village, block, district, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY village, block, district
ORDER BY beneficiaries DESC
LIMIT 10;

-- Q46. Blocks with highest disbursement
-- ANSWER:
--   Mylliem (East Khasi Hills)               : ₹66,726,926.20
--   Rongram (West Garo Hills)                : ₹42,089,328.65
--   Mawpat (East Khasi Hills)                : ₹38,348,012.90
--   Zikzak (South West Garo Hills)           : ₹31,088,203.50
--   Dadenggiri (West Garo Hills)             : ₹31,074,581.00
--   Mawphlang (East Khasi Hills)             : ₹28,620,306.75
--   Tikrikilla (West Garo Hills)             : ₹23,558,280.00
--   Bhoirymbong (Ri Bhoi)                   : ₹22,419,096.00
--   Nongstoin (West Khasi Hills)             : ₹22,412,951.50
--   Mawkyrwat (South West Khasi Hills)       : ₹22,289,420.00
SELECT block, district, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY block, district
ORDER BY total_disbursed DESC
LIMIT 10;

-- Q47. Blocks with most pending disbursements
-- ANSWER:
--   Zikzak (South West Garo Hills)           : 220
--   Rongram (West Garo Hills)                : 168
--   Umling (Ri Bhoi)                         : 156
--   Rerapara (South West Garo Hills)         : 127
--   Bhoirymbong (Ri Bhoi)                   :  83
--   Tikrikilla (West Garo Hills)             :  75
--   Umsning (Ri Bhoi)                        :  74
--   Betasing (South West Garo Hills)         :  71
--   Nongstoin (West Khasi Hills)             :  70
--   Dambo Rongjeng (East Garo Hills)         :  59
SELECT block, district, COUNT(*) AS pending
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY block, district
ORDER BY pending DESC
LIMIT 10;

-- Q48. Villages with highest disbursement
-- ANSWER:
--   Mawdiang Diang / Mawpat / EKH            : ₹18,000,000.00
--   Madanriting (Ct) / Mylliem / EKH         : ₹17,500,000.00
--   Others / Nongstoin / WKH                 : ₹11,366,378.00
--   Sohram Lwai / Mawphlang / EKH            :  ₹9,270,000.00
--   Others / Mylliem / EKH                   :  ₹8,185,000.00
--   Songsak / Songsak / EGH                  :  ₹6,454,925.00
--   Ranikor / Ranikor / SWKH                 :  ₹6,400,627.95
--   Dambo Rongjeng / Dambo Rongjeng / EGH    :  ₹6,303,050.50
--   Mawmluh Shella Bholaganj / EKH           :  ₹6,254,889.00
--   Mawlai Nongkwar / Mawlai / EKH           :  ₹5,600,000.00
SELECT village, block, district, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY village, block, district
ORDER BY total_disbursed DESC
LIMIT 10;


-- ============================================================
-- SECTION 6: LOAN ENTITY ANALYSIS (LIFCOM vs BANK)
-- ============================================================

-- Q49. Beneficiaries by loan entity
-- ANSWER: Bank=1,709 | LIFCOM=672
SELECT loan_entity, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE loan_entity IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY loan_entity
ORDER BY beneficiaries DESC;

-- Q50. Total loan disbursed by entity
-- ANSWER: Bank=₹186,040,588.50 | LIFCOM=₹111,846,763.00
SELECT loan_entity, ROUND(SUM(total_loan)::numeric, 2) AS total_loan
FROM cm_elevate
WHERE loan_entity IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY loan_entity
ORDER BY total_loan DESC;

-- Q51. Average loan by entity
-- ANSWER: Bank=₹108,859.33 | LIFCOM=₹166,438.64
SELECT loan_entity, ROUND(AVG(total_loan)::numeric, 2) AS avg_loan
FROM cm_elevate
WHERE loan_entity IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY loan_entity;

-- Q52. Scheme-wise loan entity distribution
-- ANSWER (notable): Piggery→Bank(1,272)/LIFCOM(65) | Poultry→LIFCOM(446)/Bank(3)
--   Warehouse→LIFCOM(82)/Bank(1) | Dairy→Bank(130)/LIFCOM(2)
SELECT scheme, loan_entity, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE loan_entity IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme, loan_entity
ORDER BY scheme, beneficiaries DESC;

-- Q53. District-wise loan entity distribution
-- ANSWER: All 12 districts use both Bank and LIFCOM
SELECT district, loan_entity, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE loan_entity IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district, loan_entity
ORDER BY district, beneficiaries DESC;

-- Q54. LIFCOM vs Bank - full comparison
-- ANSWER:
--   Bank  : 1,709 beneficiaries | loan=₹186,040,588.50 | avg=₹108,859.33 | disbursed=451 | pending=1,258
--   LIFCOM:   672 beneficiaries | loan=₹111,846,763.00 | avg=₹166,438.64 | disbursed=323 | pending=349
SELECT loan_entity,
    COUNT(*) AS beneficiaries,
    ROUND(SUM(total_loan)::numeric, 2) AS total_loan,
    ROUND(AVG(total_loan)::numeric, 2) AS avg_loan,
    SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed,
    SUM(CASE WHEN loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL THEN 1 ELSE 0 END) AS pending
FROM cm_elevate
WHERE loan_entity IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY loan_entity;


-- ============================================================
-- SECTION 7: SCHEME COMPARISONS
-- ============================================================

-- Q55. Piggery vs Poultry full comparison
-- ANSWER:
--   Piggery: 1,337 beneficiaries | ₹94,178,750 disbursed | ₹83,680,250 subsidy
--            ₹10,498,500 loan | avg ₹70,440/beneficiary | 1,127 pending
--   Poultry:   449 beneficiaries | ₹39,857,000 disbursed | ₹26,717,000 subsidy
--              ₹13,140,000 loan | avg ₹88,768/beneficiary | 230 pending
SELECT scheme,
    COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed,
    ROUND(SUM(total_subsidy_disbursement)::numeric, 2) AS total_subsidy,
    ROUND(SUM(total_loan)::numeric, 2) AS total_loan,
    ROUND(AVG(total_disbursement)::numeric, 2) AS avg_disbursement,
    SUM(CASE WHEN loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL THEN 1 ELSE 0 END) AS pending
FROM cm_elevate
WHERE scheme IN ('Meghalaya Piggery Development Scheme', 'Meghalaya Poultry Farming Scheme')
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme;

-- Q56. Piggery - district-wise breakdown
-- ANSWER (top 3): West Garo Hills=278 bene/₹17.4M | South West Garo Hills=258/₹16.2M
--                 Eastern West Khasi Hills=122/₹10.8M
SELECT district, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme = 'Meghalaya Piggery Development Scheme'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q57. Poultry - district-wise breakdown
-- ANSWER (top 3): South West Garo Hills=124/₹8.2M | West Khasi Hills=69/₹6.9M
--                 Eastern West Khasi Hills=57/₹6.2M
SELECT district, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme = 'Meghalaya Poultry Farming Scheme'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q58. Dairy - district-wise breakdown
-- ANSWER (top 3): North Garo Hills=18/₹2.39M | East Khasi Hills=18/₹2.33M
--                 West Garo Hills=25/₹2.05M
SELECT district, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme = 'Meghalaya Dairy Development Scheme'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q59. Goat Farming - district-wise breakdown
-- ANSWER (top 3): South West Garo Hills=23/₹920,000 | East Garo Hills=5/₹200,000
--                 West Khasi Hills=4/₹160,000
SELECT district, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme = 'Meghalaya Goat Farming Scheme'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q60. PRIME Agriculture Vehicle - district-wise breakdown
-- ANSWER (top 3): East Khasi Hills=32/₹25.1M | West Garo Hills=28/₹20.9M
--                 Ri Bhoi=29/₹19.1M
SELECT district, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme = 'PRIME Agriculture Response Vehicle Scheme'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q61. PRIME Tourism Vehicle - district-wise breakdown
-- ANSWER (top 3): East Khasi Hills=37/₹70.0M | West Garo Hills=17/₹26.6M
--                 South West Garo Hills=5/₹8.9M
SELECT district, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme = 'PRIME Tourism Vehicle Scheme'
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed DESC;

-- Q62. Livestock schemes combined (Piggery + Poultry + Goat + Dairy)
-- ANSWER:
--   Piggery : 1,337 | ₹94,178,750.00
--   Poultry :   449 | ₹39,857,000.00
--   Dairy   :   132 | ₹12,710,752.00
--   Goat    :    47 |  ₹1,880,000.00
SELECT scheme,
    COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme IN (
    'Meghalaya Piggery Development Scheme',
    'Meghalaya Poultry Farming Scheme',
    'Meghalaya Goat Farming Scheme',
    'Meghalaya Dairy Development Scheme'
)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY total_disbursed DESC;

-- Q63. Vehicle schemes combined (PRIME Agri + PRIME Tourism + Motorcaravan)
-- ANSWER:
--   PRIME Tourism Vehicle Scheme               : 93 | ₹146,977,824.60
--   PRIME Agriculture Response Vehicle Scheme  : 181 | ₹139,953,043.90
--   Meghalaya Motorcaravan Scheme              :  1 |   ₹5,000,000.00
SELECT scheme,
    COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE scheme IN (
    'PRIME Agriculture Response Vehicle Scheme',
    'PRIME Tourism Vehicle Scheme',
    'Meghalaya Motorcaravan Scheme'
)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY total_disbursed DESC;


-- ============================================================
-- SECTION 8: MONTHLY & YEARLY TRENDS
-- ============================================================

-- Q64. Beneficiaries by year
-- ANSWER: 2024=2,248 | 2025=133 | (361 with no year recorded)
SELECT year, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE year IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY year
ORDER BY year;

-- Q65. Disbursement by year
-- ANSWER: 2024=₹584,368,595.25 | 2025=₹218,819,705.50
SELECT year, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE year IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY year
ORDER BY year;

-- Q66. Beneficiaries by month
-- ANSWER: Apr=1,136 | Nov=1,115 | Feb=105 | Mar=25
SELECT month, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE month IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY month
ORDER BY beneficiaries DESC;

-- Q67. Disbursement by month
-- ANSWER: Apr=₹411,626,306.75 | Nov=₹230,917,285.00
--         Feb=₹137,687,232.50 | Mar=₹22,957,507.50
SELECT month, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE month IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY month
ORDER BY total_disbursed DESC;

-- Q68. Month + Year trend
-- ANSWER:
--   2024 Apr : 1,108 bene | ₹330,493,832.25
--   2024 Mar :    25 bene |  ₹22,957,507.50
--   2024 Nov : 1,115 bene | ₹230,917,285.00
--   2025 Apr :    28 bene |  ₹81,132,474.50
--   2025 Feb :   105 bene | ₹137,687,232.50
SELECT year, month, COUNT(*) AS beneficiaries,
    ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE year IS NOT NULL
  AND month IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY year, month
ORDER BY year, month;

-- Q69. Scheme-wise year comparison
-- ANSWER (notable): PRIME Tourism Vehicle grew from 10 (2024) to 83 (2025)
--   Piggery and Poultry all in 2024; Agriculture Warehouse grew 57→26
SELECT scheme, year, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE year IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme, year
ORDER BY scheme, year;

-- Q70. District-wise year comparison
-- ANSWER: South West Garo Hills highest in 2024 (440); East Khasi Hills
--         highest growth into 2025 (38 new beneficiaries)
SELECT district, year, COUNT(*) AS beneficiaries
FROM cm_elevate
WHERE year IS NOT NULL
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district, year
ORDER BY district, year;


-- ============================================================
-- SECTION 9: DISBURSEMENT STATUS ANALYSIS
-- ============================================================

-- Q71. Overall disbursement status breakdown
-- ANSWER: disbursed=793 (28.9%) | not disbursed=1,948 (71.0%) | unknown=1 (0.0%)
SELECT
    COALESCE(loan_disbursed, 'unknown') AS loan_disbursed,
    COUNT(*) AS count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 1) AS pct
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY COALESCE(loan_disbursed, 'unknown');

-- Q72. Scheme-wise disbursement status
-- ANSWER (notable): Goat Farming 100% not disbursed | Motorcaravan 100% disbursed
--   Piggery: 210 disbursed / 1,127 not disbursed
SELECT scheme, COALESCE(loan_disbursed, 'unknown') AS loan_disbursed, COUNT(*) AS count
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme, COALESCE(loan_disbursed, 'unknown')
ORDER BY scheme, loan_disbursed;

-- Q73. District-wise disbursement status
-- ANSWER (notable): East Khasi Hills best at 150 disbursed / 83 pending
--   South West Garo Hills worst: 35 disbursed / 418 pending
SELECT district, COALESCE(loan_disbursed, 'unknown') AS loan_disbursed, COUNT(*) AS count
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district, COALESCE(loan_disbursed, 'unknown')
ORDER BY district, loan_disbursed;

-- Q74. Pending disbursement amount (sanctioned - disbursed for pending rows)
-- ANSWER: ₹340,831,658.00
SELECT ROUND(SUM(COALESCE(sanctioned, 0) - COALESCE(total_disbursement, 0))::numeric, 2) AS total_pending_amount
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q75. Scheme-wise pending amount
-- ANSWER (top 5):
--   Meghalaya Agriculture Warehouse Scheme             : 23 pending | ₹72,173,037
--   Meghalaya Piggery Development Scheme               : 1,127 pending | ₹70,437,500
--   PRIME Agriculture Response Vehicle Scheme          : 48 pending | ₹52,912,960
--   Meghalaya Sports & Wellness Scheme                 : 4 pending | ₹25,870,000
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   : 172 pending | ₹25,521,360
SELECT scheme,
    COUNT(*) AS pending_count,
    ROUND(SUM(COALESCE(sanctioned, 0) - COALESCE(total_disbursement, 0))::numeric, 2) AS pending_amount
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY pending_amount DESC;

-- Q76. District-wise pending amount
-- ANSWER (top 5):
--   Ri Bhoi                     : 356 pending | ₹84,292,700
--   West Garo Hills             : 387 pending | ₹43,328,374
--   East Khasi Hills            : 83 pending  | ₹39,183,404
--   South West Garo Hills       : 418 pending | ₹35,272,703
--   West Jaintia Hills          : 117 pending | ₹33,752,221
SELECT district,
    COUNT(*) AS pending_count,
    ROUND(SUM(COALESCE(sanctioned, 0) - COALESCE(total_disbursement, 0))::numeric, 2) AS pending_amount
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY pending_amount DESC;


-- ============================================================
-- SECTION 10: REFUSAL ANALYSIS
-- NOTE: Q77-Q81 use full table (no exclusion filters)
-- ============================================================

-- Q77. Total refused applications
-- ANSWER: 16
SELECT COUNT(*) AS total_refused
FROM cm_elevate
WHERE refused_y_n = 'Y';

-- Q78. Refused applications by scheme
-- ANSWER:
--   PRIME Tourism Vehicle Scheme               : 7
--   Meghalaya Any Business Venture Scheme      : 3
--   Meghalaya Piggery Development Scheme       : 3
--   Meghalaya Sports & Wellness Scheme         : 2
--   PRIME Agriculture Response Vehicle Scheme  : 1
SELECT scheme, COUNT(*) AS refused
FROM cm_elevate
WHERE refused_y_n = 'Y'
GROUP BY scheme
ORDER BY refused DESC;

-- Q79. Refused applications by district
-- ANSWER:
--   West Garo Hills : 4 | Ri Bhoi : 3 | West Khasi Hills : 3
--   South West Garo Hills : 2 | North Garo Hills : 1 | East Khasi Hills : 1
--   East Garo Hills : 1 | South West Khasi Hills : 1
SELECT district, COUNT(*) AS refused
FROM cm_elevate
WHERE refused_y_n = 'Y'
GROUP BY district
ORDER BY refused DESC;

-- Q80. Reasons for refusal
-- ANSWER:
--   Repeated name              : 9
--   Unable to connect          : 2
--   Family emergency           : 1
--   Economical reason          : 1
--   Government Employee        : 1
--   Wife is a government employee : 1
SELECT if_refused_why, COUNT(*) AS count
FROM cm_elevate
WHERE refused_y_n = 'Y'
  AND if_refused_why IS NOT NULL
GROUP BY if_refused_why
ORDER BY count DESC;

-- Q81. Refusal rate by scheme
-- ANSWER:
--   Meghalaya Sports & Wellness Scheme         : 22.2%
--   Meghalaya Any Business Venture Scheme      :  9.1%
--   PRIME Tourism Vehicle Scheme               :  6.4%
--   PRIME Agriculture Response Vehicle Scheme  :  0.5%
--   Meghalaya Piggery Development Scheme       :  0.2%
--   All others                                 :  0.0%
SELECT scheme,
    COUNT(*) AS total,
    SUM(CASE WHEN refused_y_n = 'Y' THEN 1 ELSE 0 END) AS refused,
    ROUND(100.0 * SUM(CASE WHEN refused_y_n = 'Y' THEN 1 ELSE 0 END) / COUNT(*), 1) AS refusal_rate_pct
FROM cm_elevate
GROUP BY scheme
ORDER BY refusal_rate_pct DESC;


-- ============================================================
-- SECTION 11: SUBSIDY INSTALLMENT ANALYSIS
-- ============================================================

-- Q82. How many received subsidy installment 1
-- ANSWER: 2,315
SELECT COUNT(*) AS received_subsidy_1
FROM cm_elevate
WHERE subsidy_disbursement_1 > 0
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q83. How many received subsidy installment 2
-- ANSWER: 1,843
SELECT COUNT(*) AS received_subsidy_2
FROM cm_elevate
WHERE subsidy_disbursement_2 > 0
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q84. How many received subsidy installment 3
-- ANSWER: 20
SELECT COUNT(*) AS received_subsidy_3
FROM cm_elevate
WHERE subsidy_disbursement_3 > 0
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y');

-- Q85. Scheme-wise subsidy installment progress
-- ANSWER (notable):
--   Piggery     : 1,337 / 1,337 / 3   (almost all got 1 & 2, only 3 got installment 3)
--   Poultry     :   448 / 442   / 0
--   Warehouse   :    76 /  55   / 0
--   CFC         :    17 /   8   / 16  (16 reached installment 3)
--   Dairy       :   130 /   1   / 1
--   Sericulture (Spinning): 0 / 0 / 0 (no subsidy disbursed at all)
SELECT scheme,
    SUM(CASE WHEN subsidy_disbursement_1 > 0 THEN 1 ELSE 0 END) AS got_installment_1,
    SUM(CASE WHEN subsidy_disbursement_2 > 0 THEN 1 ELSE 0 END) AS got_installment_2,
    SUM(CASE WHEN subsidy_disbursement_3 > 0 THEN 1 ELSE 0 END) AS got_installment_3
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY scheme;


-- ============================================================
-- SECTION 12: TOP / BOTTOM PERFORMERS
-- ============================================================

-- Q86. Top 10 beneficiaries by disbursement amount
-- ANSWER:
--   4 FOR ALL (MSWCS000042) - Sports & Wellness - EKH              : ₹18,000,000
--   M/s True North Fitness Centre (MSWCS000038) - Sports - EKH     : ₹17,500,000
--   EARMACS (MSWCS000056) - Sports & Wellness - EKH                :  ₹9,270,000
--   Eastlette.Inc (MSWCS000068) - Sports & Wellness - EKH          :  ₹5,600,000
--   Chudoang IVCS (MEWSR000040) - Agri Warehouse - WGH             :  ₹5,044,401
--   ALL GARO HILLS MCSL (MEWSR000007) - Agri Warehouse - WGH       :  ₹5,044,401
--   Sundare IVCS (MEWSR000134) - Agri Warehouse - WGH              :  ₹5,044,401
--   Kupar Star IVCS (MEWSR000108) - Agri Warehouse - SWKH          :  ₹5,044,401
--   NONGSLEH MAWSAW IVCS (MEWSR000094) - Agri Warehouse - WKH      :  ₹5,044,401
--   Nelco Sangma MMCS (MEWSR000012) - Agri Warehouse - WGH         :  ₹5,044,401
SELECT name, application_number, scheme, district,
    ROUND(total_disbursement::numeric, 2) AS disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
ORDER BY total_disbursement DESC
LIMIT 10;

-- Q87. Bottom 10 districts by disbursement
-- ANSWER (lowest to highest):
--   East Jaintia Hills      : ₹23,578,892.05
--   North Garo Hills        : ₹25,976,432.90
--   South West Khasi Hills  : ₹39,174,752.90
--   Eastern West Khasi Hills: ₹39,286,388.85
--   South Garo Hills        : ₹42,012,551.30
--   East Garo Hills         : ₹44,939,067.80
--   West Jaintia Hills      : ₹46,216,688.45
--   West Khasi Hills        : ₹59,370,778.00
--   South West Garo Hills   : ₹60,863,092.00
--   Ri Bhoi                 : ₹68,762,289.50
SELECT district, ROUND(SUM(total_disbursement)::numeric, 2) AS total_disbursed
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
ORDER BY total_disbursed ASC
LIMIT 10;

-- Q88. Highest sanctioned but not yet disbursed (top 10)
-- ANSWER:
--   Eastlette.Inc (MSWCS000068) - Sports - EKH        : ₹16,000,000 sanctioned
--   Wakatre Pde (MSWCS000029) - Sports - WJH          : ₹10,000,000
--   Donkupar John Bosco Wriang (MSWCS000010) - Sports : ₹10,000,000
--   Sonidan Service Coop (MEWSR000058) - Warehouse    :  ₹7,881,875
--   Eastern Ri-Bhoi Organic FPC (MEWSR000015)         :  ₹7,881,875
--   SOHPHOH IVCS (MEWSR000028)                        :  ₹7,881,875
--   Pyndengumiong MCS (MEWSR000013) - Warehouse       :  ₹5,285,795
--   Yousidora Byrsat (MEWSI000085)                    :  ₹5,285,795
--   Jacob Buongpui (MEWSI000093)                      :  ₹5,285,795
--   Klinsita Lyngdoh (MEWSI000076)                    :  ₹5,285,795
SELECT name, application_number, scheme, district,
    ROUND(COALESCE(sanctioned, 0)::numeric, 2) AS sanctioned
FROM cm_elevate
WHERE (loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL)
  AND desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
ORDER BY sanctioned DESC
LIMIT 10;

-- Q89. Districts with 100% disbursement rate
-- ANSWER: No district has achieved 100% disbursement.
--   Highest: East Khasi Hills (64.4%), East Jaintia Hills (62.9%),
--            Eastern West Khasi Hills (57.3%)
SELECT district,
    COUNT(*) AS total,
    SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) AS disbursed,
    ROUND(100.0 * SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS disbursement_rate_pct
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY district
HAVING ROUND(100.0 * SUM(CASE WHEN loan_disbursed = 'disbursed' THEN 1 ELSE 0 END) / COUNT(*), 1) = 100;

-- Q90. Schemes with highest pending sanctioned amount
-- ANSWER:
--   Meghalaya Piggery Development Scheme               : 1,127 pending | ₹140,875,000
--   Meghalaya Agriculture Warehouse Scheme             :    23 pending | ₹106,103,525
--   PRIME Agriculture Response Vehicle Scheme          :    48 pending |  ₹52,912,960
--   Meghalaya Sports & Wellness Scheme                 :     4 pending |  ₹39,800,000
--   Meghalaya Dairy Development Scheme                 :   109 pending |  ₹32,700,000
--   Meghalaya Sericulture & Weaving Scheme (Weaving)   :   172 pending |  ₹30,174,000
--   Meghalaya Poultry Farming Scheme                   :   230 pending |  ₹27,600,000
--   PRIME Tourism Vehicle Scheme                       :    13 pending |  ₹24,411,485
--   Meghalaya Sericulture & Weaving Scheme (Spinning)  :   170 pending |  ₹18,307,564
--   Meghalaya Any Business Venture Scheme              :     3 pending |   ₹6,802,838
--   Meghalaya Common Facility Center Scheme            :     3 pending |   ₹5,565,660
--   Meghalaya Goat Farming Scheme                      :    47 pending |   ₹4,700,000
--   Meghalaya Motorcaravan Scheme                      :     0 pending |           ₹0
SELECT scheme,
    SUM(CASE WHEN loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL THEN 1 ELSE 0 END) AS pending_count,
    ROUND(SUM(CASE WHEN loan_disbursed = 'not disbursed' OR loan_disbursed IS NULL THEN COALESCE(sanctioned, 0) ELSE 0 END)::numeric, 2) AS pending_sanctioned_amount
FROM cm_elevate
WHERE desanctioned IS NULL
  AND (refused_y_n IS NULL OR refused_y_n <> 'Y')
GROUP BY scheme
ORDER BY pending_sanctioned_amount DESC;
