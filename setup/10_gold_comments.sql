-- Run after the first successful pipeline run.
-- Table and column comments are not just documentation: in Phase 3 the agent's
-- SQL tool reads them to understand what each column means.

COMMENT ON TABLE dpa.gold.drug_price_current IS
  'One row per NDC currently listed in CMS NADAC: current acquisition cost per unit, 90-day and 1-year price change, FDA product details and current FDA shortage status. Public data only.';

ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN ndc11 COMMENT '11-digit National Drug Code (package level, no hyphens)';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN current_price COMMENT 'NADAC per unit in USD: average pharmacy acquisition cost';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN pricing_unit COMMENT 'Unit that current_price applies to: EA (each), ML or GM';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN pct_change_90d COMMENT 'Percent change in NADAC per unit vs 90 days before as_of_date; null if not listed then';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN pct_change_1y COMMENT 'Percent change in NADAC per unit vs 365 days before as_of_date; null if not listed then';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN in_shortage COMMENT 'True if this exact NDC is on the FDA drug shortage list with status Current';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN is_generic COMMENT 'True for generic, false for brand, based on NADAC rate-setting classification';
ALTER TABLE dpa.gold.drug_price_current ALTER COLUMN as_of_date COMMENT 'Date of the latest weekly NADAC file used';

COMMENT ON TABLE dpa.gold.nadac_price_history IS
  'SCD Type 2 NADAC price history: one row per price version per NDC, valid_from/valid_to inclusive, is_current marks the open version.';

COMMENT ON TABLE dpa.gold.fact_price_change IS
  'One row per NADAC price change per NDC, with old price, new price and percent change. Excludes NDCs simply reappearing after a gap.';
