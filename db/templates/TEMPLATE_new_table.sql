-- TEMPLATE: how to add a table.
-- 1. Copy this file to db/init/03_<your_stage>.sql  (02 is taken; use the next free number).
-- 2. Rename example_items and change the columns to match YOUR output JSON.
-- 3. Keep "IF NOT EXISTS" so the file can be run more than once without errors.
--
-- Files in db/init run automatically ONLY when the Docker volume is first created.
-- On a database that already exists, apply your file by hand:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/03_<your_stage>.sql

CREATE TABLE IF NOT EXISTS example_items (
    item_id    TEXT PRIMARY KEY,                       -- use the ID your pipeline already creates
    filing_id  BIGINT REFERENCES filings(filing_id) ON DELETE CASCADE,  -- link to a filing if the row belongs to one
    name       TEXT   NOT NULL,
    score      NUMERIC,                                -- NULL allowed because there is no NOT NULL
    tags       TEXT[] NOT NULL DEFAULT '{}',           -- a list of values
    extra      JSONB  NOT NULL DEFAULT '{}'::jsonb     -- anything you did not give its own column
);

-- Add an index for columns you will filter or join on a lot.
CREATE INDEX IF NOT EXISTS example_items_filing_idx ON example_items (filing_id);
