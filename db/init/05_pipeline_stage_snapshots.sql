-- Durable stage inputs, including audit metadata not represented by result rows.
-- Written in the same transaction as the normalized records.
CREATE TABLE IF NOT EXISTS pipeline_stage_snapshots (
    company       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('raw', 'disclosures', 'alignments')),
    previous_year INTEGER NOT NULL,
    current_year  INTEGER NOT NULL CHECK (current_year >= previous_year),
    documents     JSONB NOT NULL CHECK (jsonb_typeof(documents) = 'object'),
    revision      TEXT NOT NULL CHECK (revision ~ '^[0-9a-f]{64}$'),
    saved_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company, kind, previous_year, current_year)
);
