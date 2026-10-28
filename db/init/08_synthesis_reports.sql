-- Synthesis agent output (agents/synthesis.py, *_synthesis_output.json), loaded for the chatbot.
-- Safe to run more than once. The chatbot and the loader apply it automatically when its version is missing,
-- or apply it by hand on an existing database:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/08_synthesis_reports.sql
--
-- One report = one company over a span of fiscal years (e.g. amd 2023-2025). Tickers are lowercase.

CREATE TABLE IF NOT EXISTS synthesis_reports (
    report_id              BIGSERIAL PRIMARY KEY,
    company                TEXT    NOT NULL,
    start_year             INTEGER NOT NULL,
    end_year               INTEGER NOT NULL CHECK (end_year > start_year),
    years                  INTEGER[] NOT NULL,
    executive_summary      TEXT    NOT NULL,
    longitudinal_analysis  TEXT,
    financial_analysis     TEXT,
    limitations            TEXT[]  NOT NULL DEFAULT '{}',
    valid                  BOOLEAN,                          -- the agent's own citation/coverage validation
    validation             JSONB   NOT NULL DEFAULT '{}'::jsonb,
    coverage               JSONB   NOT NULL DEFAULT '{}'::jsonb, -- input_summary + coverage_summary as delivered
    financial_metrics      JSONB   NOT NULL DEFAULT '[]'::jsonb, -- the XBRL facts the trends were computed from
    source_file            TEXT,
    source_sha256          TEXT    NOT NULL,
    loaded_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, start_year, end_year)
);

-- One row per key finding; match_ids are the alignment IDs it cites, metric_ids the XBRL facts it cites.
CREATE TABLE IF NOT EXISTS synthesis_findings (
    report_id     BIGINT  NOT NULL REFERENCES synthesis_reports(report_id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    analysis      TEXT    NOT NULL,
    evidence_ids  TEXT[]  NOT NULL DEFAULT '{}',
    match_ids     TEXT[]  NOT NULL DEFAULT '{}',
    metric_ids    TEXT[]  NOT NULL DEFAULT '{}',
    PRIMARY KEY (report_id, ordinal)
);
CREATE INDEX IF NOT EXISTS synthesis_findings_match_ids_idx ON synthesis_findings USING gin (match_ids);

-- Deterministic financial calculations (computed in code by the agent, not by the LLM).
CREATE TABLE IF NOT EXISTS synthesis_trends (
    report_id    BIGINT  NOT NULL REFERENCES synthesis_reports(report_id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    kind         TEXT    NOT NULL CHECK (kind IN ('year_over_year_change', 'calculated_metric')),
    metric       TEXT    NOT NULL,                  -- label (growth) or name (ratio), e.g. 'Revenue', 'Gross margin'
    concept      TEXT,                              -- XBRL concept for growth rows
    unit         TEXT,
    year         INTEGER,                           -- calculated_metric
    from_year    INTEGER,                           -- year_over_year_change
    to_year      INTEGER,
    old_value    NUMERIC,
    new_value    NUMERIC,
    value        NUMERIC,
    growth_pct   NUMERIC,
    formula      TEXT,
    source_ids   TEXT[]  NOT NULL DEFAULT '{}',
    PRIMARY KEY (report_id, ordinal)
);

INSERT INTO schema_info (key, value) VALUES ('synthesis_schema_version', '1')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
