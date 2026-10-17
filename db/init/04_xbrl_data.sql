-- Financial facts from the yearly api-metrics-1.0 exports (XBRL schema version 1).
-- Requires 01_schema.sql; independent of the disclosure/embedding migrations.
-- Runs automatically on a fresh Docker volume. For an existing database:
--   docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U sec -d sec_disclosure < db/init/04_xbrl_data.sql
-- Adds tables only; it does not load JSON or choose between conflicting facts.

BEGIN;

-- Explicit XBRL dimensions must have a nonempty axis/member and one member per axis.
-- Keep the original array order; comparison code can canonicalize it separately.
CREATE OR REPLACE FUNCTION xbrl_valid_dimensions(dimensions JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT
AS $$
DECLARE
    dimension JSONB;
    axes TEXT[] := '{}';
BEGIN
    IF jsonb_typeof(dimensions) <> 'array' THEN
        RETURN FALSE;
    END IF;
    FOR dimension IN SELECT jsonb_array_elements(dimensions) LOOP
        IF jsonb_typeof(dimension) <> 'object'
           OR jsonb_typeof(dimension->'axis') IS DISTINCT FROM 'string'
           OR jsonb_typeof(dimension->'member') IS DISTINCT FROM 'string'
           OR btrim(dimension->>'axis') = ''
           OR btrim(dimension->>'member') = ''
           OR (dimension->>'axis') = ANY (axes) THEN
            RETURN FALSE;
        END IF;
        axes := array_append(axes, dimension->>'axis');
    END LOOP;
    RETURN TRUE;
END;
$$;

-- One immutable snapshot per yearly export. Different exports for the same filing
-- can coexist; repeated loading of the same bytes should reuse extraction_id.
CREATE TABLE IF NOT EXISTS xbrl_extractions (
    extraction_id        BIGSERIAL PRIMARY KEY,
    filing_id            BIGINT NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    file_path            TEXT NOT NULL,
    file_sha256          TEXT NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    export_schema_version TEXT NOT NULL CHECK (export_schema_version = 'api-metrics-1.0'),
    exporter_version     TEXT NOT NULL,
    taxonomy_year        INTEGER NOT NULL,
    requested_items      TEXT[] NOT NULL CHECK (
                             cardinality(requested_items) > 0
                             AND requested_items <@ ARRAY['1', '1A', '7', '8']::TEXT[]
                             AND array_position(requested_items, NULL) IS NULL),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    loaded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, file_sha256)
);
CREATE INDEX IF NOT EXISTS xbrl_extractions_filing_idx ON xbrl_extractions (filing_id);

-- One concept per export, retaining that filing's taxonomy label and definition.
-- API groups are preserved as group names, not interpreted as printed tables.
CREATE TABLE IF NOT EXISTS xbrl_metrics (
    extraction_id        BIGINT NOT NULL REFERENCES xbrl_extractions(extraction_id) ON DELETE CASCADE,
    concept              TEXT NOT NULL CHECK (concept ~ '^us-gaap:[A-Za-z_][A-Za-z0-9_.-]*$'),
    metric_index         INTEGER NOT NULL CHECK (metric_index >= 0),
    query_name           TEXT NOT NULL CHECK (query_name = split_part(concept, ':', 2)),
    label                TEXT NOT NULL,
    definition           TEXT,
    period_type          TEXT NOT NULL CHECK (period_type IN ('instant', 'duration')),
    scope                TEXT NOT NULL CHECK (scope IN ('company_wide', 'dimension_only')),
    company_wide_annual_or_year_end_years INTEGER[] NOT NULL DEFAULT '{}',
    units                TEXT[] NOT NULL DEFAULT '{}',
    api_groups           TEXT[] NOT NULL DEFAULT '{}',
    items                TEXT[] NOT NULL,
    extra                JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(extra) = 'object'),
    PRIMARY KEY (extraction_id, concept),
    UNIQUE (extraction_id, metric_index)
);
CREATE INDEX IF NOT EXISTS xbrl_metrics_concept_idx ON xbrl_metrics (concept);

-- One exported value record, including every comparative period and accuracy variant.
-- The array index is local to an immutable export, not a cross-filing fact identity.
CREATE TABLE IF NOT EXISTS xbrl_values (
    extraction_id        BIGINT NOT NULL,
    concept              TEXT NOT NULL,
    value_index          INTEGER NOT NULL CHECK (value_index >= 0),
    value_text           TEXT,
    amount               NUMERIC GENERATED ALWAYS AS (value_text::NUMERIC) STORED,
    unit                 TEXT NOT NULL CHECK (btrim(unit) <> ''),
    status               TEXT NOT NULL CHECK (status IN ('reported', 'nil')),
    instant_date         DATE,
    period_start_date    DATE,
    period_end_date      DATE,
    dimensions           JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (xbrl_valid_dimensions(dimensions)),
    decimals             JSONB CHECK (decimals IS NULL OR jsonb_typeof(decimals) IN ('string', 'number', 'null')),
    precision            JSONB CHECK (precision IS NULL OR jsonb_typeof(precision) IN ('string', 'number', 'null')),
    items                TEXT[] NOT NULL,
    extra                JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(extra) = 'object'),
    PRIMARY KEY (extraction_id, concept, value_index),
    FOREIGN KEY (extraction_id, concept)
        REFERENCES xbrl_metrics(extraction_id, concept) ON DELETE CASCADE,
    CONSTRAINT xbrl_values_amount_status_check CHECK (
        (status = 'reported' AND value_text IS NOT NULL AND amount IS NOT NULL
            AND amount::TEXT NOT IN ('NaN', 'Infinity', '-Infinity'))
        OR (status = 'nil' AND value_text IS NULL AND amount IS NULL)),
    CONSTRAINT xbrl_values_period_check CHECK (
        (instant_date IS NOT NULL AND period_start_date IS NULL AND period_end_date IS NULL)
        OR (instant_date IS NULL AND period_start_date IS NOT NULL AND period_end_date IS NOT NULL
            AND period_end_date >= period_start_date))
);
CREATE INDEX IF NOT EXISTS xbrl_values_period_idx
    ON xbrl_values ((COALESCE(instant_date, period_end_date)));
CREATE INDEX IF NOT EXISTS xbrl_values_items_idx ON xbrl_values USING gin (items);
CREATE INDEX IF NOT EXISTS xbrl_values_dimensions_idx ON xbrl_values USING gin (dimensions);

-- One original filing occurrence per source_labels[] entry. Several occurrences
-- can support the same value. Fact IDs are local to a filing/document, not unique.
CREATE TABLE IF NOT EXISTS xbrl_value_sources (
    extraction_id        BIGINT NOT NULL,
    concept              TEXT NOT NULL,
    value_index          INTEGER NOT NULL,
    source_index         INTEGER NOT NULL CHECK (source_index >= 0),
    document_id          TEXT, -- NULL in a single-document export means the implicit primary document
    source               TEXT,
    page                 TEXT, -- printed labels can include F-1, not just integers
    source_fact_id       TEXT,
    source_locator       TEXT,
    row_label            TEXT,
    column_label         TEXT,
    label_method         TEXT,
    label_status         TEXT NOT NULL CHECK (label_status IN (
                             'resolved', 'partial', 'unresolved', 'not_in_table', 'source_not_found')),
    extra                JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(extra) = 'object'),
    PRIMARY KEY (extraction_id, concept, value_index, source_index),
    FOREIGN KEY (extraction_id, concept, value_index)
        REFERENCES xbrl_values(extraction_id, concept, value_index) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS xbrl_value_sources_fact_idx
    ON xbrl_value_sources (extraction_id, document_id, source_fact_id);

-- One row per VALUE, without expanding source occurrences and multiplying amounts.
CREATE OR REPLACE VIEW xbrl_value_context AS
SELECT f.company,
       f.fiscal_year AS source_filing_year,
       f.form_type,
       e.filing_id,
       e.extraction_id,
       e.taxonomy_year,
       m.concept,
       m.label,
       m.definition,
       m.period_type AS concept_period_type,
       m.scope AS metric_scope,
       v.value_index,
       v.value_text,
       v.amount,
       v.unit,
       v.status,
       v.instant_date,
       v.period_start_date,
       v.period_end_date,
       v.dimensions,
       v.decimals,
       v.precision,
       v.items
FROM xbrl_values v
JOIN xbrl_metrics m USING (extraction_id, concept)
JOIN xbrl_extractions e USING (extraction_id)
JOIN filings f USING (filing_id);

-- Track this independent feature without claiming the text/embedding migrations ran.
INSERT INTO schema_info (key, value) VALUES ('xbrl_schema_version', '1')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
