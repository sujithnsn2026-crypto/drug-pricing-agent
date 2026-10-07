-- Run once in a Databricks SQL editor or notebook (needs CREATE CATALOG permission).
-- If your metastore has no default storage, add:  MANAGED LOCATION 'abfss://<container>@<account>.dfs.core.windows.net/dpa'
CREATE CATALOG IF NOT EXISTS dpa COMMENT 'Drug Pricing Intelligence Agent - public US drug pricing data';

CREATE SCHEMA IF NOT EXISTS dpa.landing COMMENT 'Raw files exactly as downloaded';
CREATE SCHEMA IF NOT EXISTS dpa.bronze  COMMENT 'Raw files as tables, all strings, with lineage columns';
CREATE SCHEMA IF NOT EXISTS dpa.silver  COMMENT 'Typed, cleaned, de-duplicated, 11-digit NDC keys';
CREATE SCHEMA IF NOT EXISTS dpa.gold    COMMENT 'Business tables the agent queries';

CREATE VOLUME IF NOT EXISTS dpa.landing.raw COMMENT 'Landing zone for NADAC CSVs and openFDA JSON snapshots';
