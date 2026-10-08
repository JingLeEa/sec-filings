-- Tables for the disclosure extraction and disclosure alignment stages (schema version 2 of the DB).
-- Safe to run more than once. On a database that already exists, apply it with:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/02_disclosures_alignments.sql
-- (Files in db/init only run automatically when the Docker volume is first created.)

-- One row per extracted disclosure (disclosures.json and review_candidates.json, extraction schema "3").
CREATE TABLE IF NOT EXISTS disclosures (
    disclosure_id TEXT PRIMARY KEY,                    -- e.g. amd_2024_1A_D061
    filing_id     BIGINT NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    item          TEXT   NOT NULL,
    section       TEXT,                                -- full subsection path as one string
    sections      TEXT[] NOT NULL DEFAULT '{}',
    topic         TEXT,
    summary       TEXT,                                -- model-generated; not the original wording
    content       TEXT   NOT NULL,                     -- selected original text
    taxonomy      TEXT   NOT NULL CHECK (taxonomy IN (
                      'Strategy & Business Model', 'Operations & Capacity', 'Technology & AI',
                      'Cybersecurity & Data', 'Supply Chain & Third Parties',
                      'Regulation, Legal & Compliance', 'Financial & Capital Resources',
                      'Human Capital & Organization', 'Other / Unclassified')),
    review_status TEXT   NOT NULL CHECK (review_status IN ('accepted', 'review_candidate')),
    sources       JSONB  NOT NULL DEFAULT '[]'::jsonb, -- original evidence and paragraph context, kept as delivered
    verification  JSONB  NOT NULL DEFAULT '{}'::jsonb,
    extra         JSONB  NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS disclosures_filing_item_idx ON disclosures (filing_id, item);
CREATE INDEX IF NOT EXISTS disclosures_taxonomy_idx    ON disclosures (taxonomy);

-- One row per comparison run, e.g. amd 2024 vs 2025 (alignment schema "8").
CREATE TABLE IF NOT EXISTS alignment_runs (
    run_id           BIGSERIAL PRIMARY KEY,
    company          TEXT    NOT NULL,
    previous_year    INTEGER NOT NULL,
    current_year     INTEGER NOT NULL,
    schema_version   TEXT    NOT NULL,
    run_complete     BOOLEAN NOT NULL,
    comparison_scope TEXT,
    note             TEXT,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, previous_year, current_year)
);

-- One row per alignment decision (rows from alignments.json and needs_review.json).
-- match_id is only unique inside one run, so the key includes run_id.
CREATE TABLE IF NOT EXISTS alignments (
    run_id                 BIGINT NOT NULL REFERENCES alignment_runs(run_id) ON DELETE CASCADE,
    match_id               TEXT   NOT NULL,            -- e.g. amd_2024_2025_M0005
    status                 TEXT   NOT NULL CHECK (status IN ('auto_matched', 'ai_verified', 'unmatched', 'needs_review')),
    relationship           TEXT   NOT NULL CHECK (relationship IN (
                               'one_to_one', 'one_to_many', 'many_to_one', 'many_to_many', 'previous_only', 'current_only')),
    match_method           TEXT,                       -- exact_text, llm, exact_text_and_llm; may be absent
    explanation            TEXT,
    verifier_review_reason TEXT,
    review_reasons         TEXT[] NOT NULL DEFAULT '{}',
    unmatched_type         TEXT,                       -- introduced_disclosure or removed_disclosure
    unmatched_notes        TEXT[] NOT NULL DEFAULT '{}',
    source_file            TEXT   NOT NULL,            -- alignments or needs_review
    evidence               JSONB  NOT NULL DEFAULT '[]'::jsonb,
    grouping               JSONB,
    validation_errors      JSONB,
    change_analysis        JSONB  NOT NULL,            -- placeholder today; later stages update it
    extra                  JSONB  NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, match_id)
);
CREATE INDEX IF NOT EXISTS alignments_status_idx ON alignments (run_id, status);

-- Which disclosures take part in each alignment, one row per disclosure and side.
-- disclosure_id is not a foreign key on purpose: the alignment can be loaded before the disclosures are.
CREATE TABLE IF NOT EXISTS alignment_members (
    run_id            BIGINT NOT NULL,
    match_id          TEXT   NOT NULL,
    side              TEXT   NOT NULL CHECK (side IN ('previous', 'current')),
    disclosure_id     TEXT   NOT NULL,
    item              TEXT,
    section           TEXT,
    taxonomy          TEXT,
    summary           TEXT,
    extraction_status TEXT,
    PRIMARY KEY (run_id, match_id, side, disclosure_id),
    FOREIGN KEY (run_id, match_id) REFERENCES alignments(run_id, match_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS alignment_members_disclosure_idx ON alignment_members (disclosure_id);

INSERT INTO schema_info (key, value) VALUES ('schema_version', '2')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
