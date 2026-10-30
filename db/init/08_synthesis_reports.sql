-- Chatbot read model over the synthesis agent's reports (schema version 2).
-- Safe to run more than once. The chatbot and scripts/load_synthesis_to_postgres.py apply it automatically when
-- its version is missing or older, or apply it by hand:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/08_synthesis_reports.sql
--
-- The synthesis agent (agents/synthesis.py, storage/synthesis.py) is the source of truth: after every run it
-- upserts the whole report JSON into synthesis_reports (db/init/06_synthesis_reports.sql on the synthesis-agent
-- branch). The chatbot only reads that table, through the views below.

-- v1 of this file created a chatbot-owned synthesis_reports with a different layout (report_id, ...), which
-- collides with the agent's table. Drop it if present; reload any report files with load_synthesis_to_postgres.py.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'synthesis_reports' AND column_name = 'report_id') THEN
        DROP TABLE IF EXISTS synthesis_trends, synthesis_findings CASCADE;
        DROP TABLE synthesis_reports CASCADE;
    END IF;
END $$;

-- Must stay identical to db/init/06_synthesis_reports.sql (synthesis-agent branch); whichever runs first wins.
CREATE TABLE IF NOT EXISTS synthesis_reports (
    company       TEXT NOT NULL CHECK (company = upper(company)),
    years         INTEGER[] NOT NULL CHECK (cardinality(years) >= 2),
    report        JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company, years)
);

-- One row per report. report_key = 'amd_2023_2024_2025' (lowercase ticker + years).
CREATE OR REPLACE VIEW chatbot_synthesis_reports AS
SELECT lower(s.company) || '_' || array_to_string(s.years, '_')            AS report_key,
       lower(s.company)                                                     AS company,
       s.years,
       (SELECT min(y) FROM unnest(s.years) AS y)                            AS start_year,
       (SELECT max(y) FROM unnest(s.years) AS y)                            AS end_year,
       s.report->'report'->>'executive_summary'                             AS executive_summary,
       s.report->'report'->>'longitudinal_analysis'                         AS longitudinal_analysis,
       s.report->'report'->>'financial_analysis'                            AS financial_analysis,
       ARRAY(SELECT jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(s.report->'report'->'limitations') = 'array'
                      THEN s.report->'report'->'limitations' ELSE '[]'::jsonb END)) AS limitations,
       (s.report->'validation'->>'valid')::boolean                          AS valid,
       s.report->'input_summary'->>'material_changes_selected'              AS material_changes_selected,
       s.report->'coverage_summary'->>'citation_coverage_pct'               AS citation_coverage_pct,
       CASE WHEN jsonb_typeof(s.report->'financial_metrics') = 'array'
            THEN jsonb_array_length(s.report->'financial_metrics') ELSE 0 END AS financial_metrics,
       s.generated_at                                                       AS loaded_at
FROM synthesis_reports s;

-- One row per key finding, with the alignment match_ids and XBRL fact ids it cites split out.
CREATE OR REPLACE VIEW chatbot_synthesis_findings AS
SELECT lower(s.company) || '_' || array_to_string(s.years, '_') AS report_key,
       lower(s.company) AS company,
       f.ordinal::int   AS ordinal,
       f.value->>'title'    AS title,
       f.value->>'analysis' AS analysis,
       ARRAY(SELECT jsonb_array_elements_text(ids.evidence))                       AS evidence_ids,
       ARRAY(SELECT e FROM jsonb_array_elements_text(ids.evidence) AS e
             WHERE e ~ '^[a-z]{1,6}_[0-9]{4}_[0-9]{4}_M[0-9]{3,5}$')               AS match_ids,
       ARRAY(SELECT e FROM jsonb_array_elements_text(ids.evidence) AS e
             WHERE e LIKE 'xbrl:%')                                                 AS metric_ids
FROM synthesis_reports s
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(s.report->'report'->'key_findings') = 'array'
         THEN s.report->'report'->'key_findings' ELSE '[]'::jsonb END) WITH ORDINALITY AS f(value, ordinal)
CROSS JOIN LATERAL (SELECT CASE WHEN jsonb_typeof(f.value->'evidence_ids') = 'array'
                                THEN f.value->'evidence_ids' ELSE '[]'::jsonb END AS evidence) AS ids;

-- Deterministic financial calculations (computed in code by the agent from XBRL facts, not by the LLM).
CREATE OR REPLACE VIEW chatbot_synthesis_trends AS
SELECT lower(s.company) || '_' || array_to_string(s.years, '_') AS report_key,
       lower(s.company) AS company,
       t.ordinal::int   AS ordinal,
       t.value->>'type' AS kind,
       coalesce(t.value->>'label', t.value->>'name', t.value->>'concept') AS metric,
       t.value->>'concept' AS concept,
       t.value->>'unit'    AS unit,
       (t.value->>'year')::int      AS year,
       (t.value->>'from_year')::int AS from_year,
       (t.value->>'to_year')::int   AS to_year,
       nullif(t.value->>'old_value', '')::numeric  AS old_value,
       nullif(t.value->>'new_value', '')::numeric  AS new_value,
       nullif(t.value->>'value', '')::numeric      AS value,
       nullif(t.value->>'growth_pct', '')::numeric AS growth_pct,
       t.value->>'formula' AS formula,
       ARRAY(SELECT jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(t.value->'source_ids') = 'array'
                      THEN t.value->'source_ids' ELSE '[]'::jsonb END)) AS source_ids
FROM synthesis_reports s
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(s.report->'financial_trends') = 'array'
         THEN s.report->'financial_trends' ELSE '[]'::jsonb END) WITH ORDINALITY AS t(value, ordinal)
WHERE t.value->>'type' IN ('year_over_year_change', 'calculated_metric');

INSERT INTO schema_info (key, value) VALUES ('synthesis_schema_version', '2')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
