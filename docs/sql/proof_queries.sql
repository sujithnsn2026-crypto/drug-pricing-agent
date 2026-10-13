-- Read-only queries that prove the README's numbers straight from the Unity Catalog tables.
-- Run in the Databricks SQL Editor (any SQL warehouse). Results are screenshots 31-40 in docs/screenshots.

-- 0. Row counts per table (screenshot 31)
SELECT 'silver.nadac_prices: rows' AS measure, COUNT(*) AS value FROM dbw_dpa.silver.nadac_prices
UNION ALL SELECT 'silver.nadac_prices: weekly releases', COUNT(DISTINCT as_of_date) FROM dbw_dpa.silver.nadac_prices
UNION ALL SELECT 'gold.nadac_price_history: rows', COUNT(*) FROM dbw_dpa.gold.nadac_price_history
UNION ALL SELECT 'gold.fact_price_change: rows', COUNT(*) FROM dbw_dpa.gold.fact_price_change
UNION ALL SELECT 'gold.drug_price_current: rows', COUNT(*) FROM dbw_dpa.gold.drug_price_current
UNION ALL SELECT 'gold.drug_price_current: in shortage', COUNT_IF(in_shortage) FROM dbw_dpa.gold.drug_price_current;

-- 1. Where the 145 weekly releases came from: files, releases and rows per year (screenshot 32)
SELECT year(as_of_date)               AS year,
       COUNT(DISTINCT _source_file)   AS cms_files,
       COUNT(DISTINCT as_of_date)     AS weekly_releases,
       COUNT(*)                       AS silver_rows,
       MIN(as_of_date)                AS first_release,
       MAX(as_of_date)                AS last_release
FROM dbw_dpa.silver.nadac_prices
GROUP BY 1
ORDER BY 1;

-- 2. The FDA shortage list as stored in Silver (screenshot 33)
SELECT generic_name, company_name, status, availability, shortage_reason
FROM dbw_dpa.silver.drug_shortages
LIMIT 15;

-- 3. One drug's full price history (SCD2) (screenshot 34)
SELECT ndc11, ndc_description, valid_from, valid_to, is_current, nadac_per_unit, pricing_unit
FROM dbw_dpa.gold.nadac_price_history
WHERE ndc_description LIKE 'VERAPAMIL ER 240%'
ORDER BY ndc11, valid_from DESC
LIMIT 20;

-- 4. Biggest generic price increases since July 2026, items under $0.05 skipped (screenshot 35)
SELECT change_date, ndc_description, old_price, new_price, pct_change, pricing_unit
FROM dbw_dpa.gold.fact_price_change
WHERE is_generic AND old_price >= 0.05 AND change_date >= '2026-07-01'
ORDER BY pct_change DESC
LIMIT 15;

-- 5. What the Ask agent answered from: top 90-day generic increases (screenshot 36)
SELECT ndc11, ndc_description, labeler_name, price_90d_ago, current_price, pct_change_90d, in_shortage
FROM dbw_dpa.gold.drug_price_current
WHERE is_generic
ORDER BY pct_change_90d DESC NULLS LAST
LIMIT 10;

-- 6. Drugs in FDA shortage with the biggest 1-year price change (screenshot 37)
SELECT ndc_description, labeler_name, current_price, pricing_unit, pct_change_1y
FROM dbw_dpa.gold.drug_price_current
WHERE in_shortage
ORDER BY pct_change_1y DESC NULLS LAST
LIMIT 10;

-- 7. Data-quality check results (screenshots 38-39)
SELECT * FROM dbw_dpa.gold.dq_results LIMIT 30;

-- 8. Every write the job made to the price history (screenshot 40)
DESCRIBE HISTORY dbw_dpa.gold.nadac_price_history LIMIT 10;
