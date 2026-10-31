-- Chatbot read model for peer-comparison agent output (agents/peer_comparison.py, report schema "2").
-- Safe to run more than once. The chatbot and the loader apply it automatically when its version is missing or
-- older, or apply it by hand on an existing database:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/10_chatbot_peer_comparisons.sql
--
-- Source of truth: peer_comparison_reports (db/init/07_peer_comparison_reports.sql on main), where the peer
-- comparison pipeline saves each whole report as JSONB. The tables below are a normalised copy for fast chatbot
-- queries, rebuilt from that table by storage/peer_comparisons.py (sync_peer_reports) whenever a report is added
-- or re-saved; the chatbot checks on start-up.
--
-- One run = one target company, one fiscal year, one peer set, one Item list and target selection.
-- Tickers are stored lowercase to match alignment_runs.company.

-- Identical to db/init/07_peer_comparison_reports.sql (main), for databases created before that file existed.
CREATE TABLE IF NOT EXISTS peer_comparison_reports (
    target_ticker   TEXT NOT NULL CHECK (target_ticker = upper(target_ticker)),
    fiscal_year     INTEGER NOT NULL CHECK (fiscal_year BETWEEN 1900 AND 2200),
    peer_tickers    TEXT[] NOT NULL CHECK (cardinality(peer_tickers) > 0),
    items           TEXT[] NOT NULL CHECK (cardinality(items) > 0),
    target_selection TEXT NOT NULL CHECK (target_selection IN ('material_changes', 'all')),
    run_complete    BOOLEAN NOT NULL,
    report          JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    saved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_ticker, fiscal_year, peer_tickers, items, target_selection)
);

CREATE TABLE IF NOT EXISTS peer_comparison_runs (
    peer_run_id     BIGSERIAL PRIMARY KEY,
    target          TEXT    NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    peers           TEXT[]  NOT NULL,                   -- sorted, lowercase
    items           TEXT[]  NOT NULL,
    schema_version  TEXT    NOT NULL,
    run_complete    BOOLEAN NOT NULL,
    jobs_completed  INTEGER NOT NULL,
    jobs_planned    INTEGER NOT NULL,
    policies        JSONB   NOT NULL DEFAULT '{}'::jsonb, -- candidate/alignment/agent policy text, kept as delivered
    token_usage     JSONB   NOT NULL DEFAULT '{}'::jsonb,
    source_file     TEXT,
    source_sha256   TEXT    NOT NULL,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (target, fiscal_year, peers)
);
-- v2: which target disclosures the agent compared ('material_changes' = only those marked material, or 'all').
ALTER TABLE peer_comparison_runs ADD COLUMN IF NOT EXISTS target_selection TEXT;

-- v3: one cached run per source report (same key as peer_comparison_reports), with the report's saved_at so a
-- re-saved report is re-synced. Runs loaded by the v2 loader have no source and are dropped by the next sync;
-- reload their files with scripts/load_peer_comparisons_to_postgres.py.
ALTER TABLE peer_comparison_runs ADD COLUMN IF NOT EXISTS source_saved_at TIMESTAMPTZ;
UPDATE peer_comparison_runs SET target_selection = 'all' WHERE target_selection IS NULL;
ALTER TABLE peer_comparison_runs DROP CONSTRAINT IF EXISTS peer_comparison_runs_target_fiscal_year_peers_key;
CREATE UNIQUE INDEX IF NOT EXISTS peer_comparison_runs_source_key
    ON peer_comparison_runs (target, fiscal_year, peers, items, target_selection);

-- One row per target disclosure the agent processed.
CREATE TABLE IF NOT EXISTS peer_comparison_targets (
    peer_run_id           BIGINT NOT NULL REFERENCES peer_comparison_runs(peer_run_id) ON DELETE CASCADE,
    target_disclosure_id  TEXT   NOT NULL,
    item                  TEXT   NOT NULL,
    target_topic          TEXT,
    target_summary        TEXT,
    target_source_status  TEXT,
    year_over_year        JSONB  NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (peer_run_id, target_disclosure_id)
);

-- One row per (target disclosure, peer): compared / no_comparable_candidate / no_candidate_retrieved.
CREATE TABLE IF NOT EXISTS peer_comparison_results (
    peer_run_id           BIGINT NOT NULL,
    target_disclosure_id  TEXT   NOT NULL,
    peer                  TEXT   NOT NULL,
    status                TEXT   NOT NULL CHECK (status IN ('compared', 'no_comparable_candidate', 'no_candidate_retrieved')),
    rationale             TEXT,
    agent_warnings        TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (peer_run_id, target_disclosure_id, peer),
    FOREIGN KEY (peer_run_id, target_disclosure_id)
        REFERENCES peer_comparison_targets(peer_run_id, target_disclosure_id) ON DELETE CASCADE
);

-- One row per supported comparison between a target disclosure and a peer disclosure.
CREATE TABLE IF NOT EXISTS peer_comparisons (
    comparison_id         BIGSERIAL PRIMARY KEY,
    peer_run_id           BIGINT NOT NULL,
    target_disclosure_id  TEXT   NOT NULL,
    peer                  TEXT   NOT NULL,
    peer_disclosure_id    TEXT   NOT NULL,
    relationship          TEXT   NOT NULL CHECK (relationship IN ('same_topic', 'related_topic')),
    comparison            TEXT   NOT NULL,
    peer_topic            TEXT,
    peer_summary          TEXT,
    peer_taxonomy         TEXT,
    peer_source_status    TEXT,
    retrieval_score       DOUBLE PRECISION,
    UNIQUE (peer_run_id, target_disclosure_id, peer_disclosure_id),
    FOREIGN KEY (peer_run_id, target_disclosure_id, peer)
        REFERENCES peer_comparison_results(peer_run_id, target_disclosure_id, peer) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS peer_comparisons_peer_disclosure_idx ON peer_comparisons (peer_disclosure_id);
CREATE INDEX IF NOT EXISTS peer_comparisons_target_disclosure_idx ON peer_comparisons (target_disclosure_id);

-- Cited source sentences for both sides of each comparison.
CREATE TABLE IF NOT EXISTS peer_comparison_evidence (
    comparison_id      BIGINT  NOT NULL REFERENCES peer_comparisons(comparison_id) ON DELETE CASCADE,
    side               TEXT    NOT NULL CHECK (side IN ('target', 'peer')),
    ticker             TEXT    NOT NULL,
    disclosure_id      TEXT    NOT NULL,
    ordinal            INTEGER NOT NULL,
    sentence_id        TEXT    NOT NULL,
    paragraph_id       TEXT,
    source_url         TEXT,
    text               TEXT    NOT NULL,
    visible_in_prompt  BOOLEAN,                          -- false: cited a real sentence the model was not shown
    PRIMARY KEY (comparison_id, side, sentence_id)
);

-- One row per comparison, both companies side by side, with quotes and a search document.
CREATE OR REPLACE VIEW peer_comparison_pairs AS
SELECT c.comparison_id,
       r.peer_run_id,
       r.fiscal_year,
       r.run_complete,
       t.item,
       r.target                 AS company_a,
       c.peer                   AS company_b,
       t.target_disclosure_id   AS disclosure_a,
       c.peer_disclosure_id     AS disclosure_b,
       t.target_topic           AS topic_a,
       c.peer_topic             AS topic_b,
       t.target_summary         AS summary_a,
       c.peer_summary           AS summary_b,
       c.peer_taxonomy          AS taxonomy_b,
       c.relationship,
       c.comparison,
       c.retrieval_score,
       ev.quote_a,
       ev.quote_b,
       ev.sentence_ids_a,
       ev.sentence_ids_b,
       ev.unseen_citations,
       r.target_selection,
       -- the same comparison also exists in a more recently saved run (e.g. a pilot later re-run with more peers)
       EXISTS (SELECT 1 FROM peer_comparisons c2 JOIN peer_comparison_runs r2 ON r2.peer_run_id = c2.peer_run_id
               WHERE c2.target_disclosure_id = c.target_disclosure_id AND c2.peer_disclosure_id = c.peer_disclosure_id
                 AND c2.peer_run_id <> c.peer_run_id
                 AND (coalesce(r2.source_saved_at, r2.loaded_at), r2.peer_run_id)
                     > (coalesce(r.source_saved_at, r.loaded_at), r.peer_run_id)) AS superseded
FROM peer_comparisons c
JOIN peer_comparison_targets t USING (peer_run_id, target_disclosure_id)
JOIN peer_comparison_runs r USING (peer_run_id)
LEFT JOIN LATERAL (
    SELECT string_agg(e.text, ' ' ORDER BY e.ordinal) FILTER (WHERE e.side = 'target') AS quote_a,
           string_agg(e.text, ' ' ORDER BY e.ordinal) FILTER (WHERE e.side = 'peer')   AS quote_b,
           array_agg(e.sentence_id ORDER BY e.ordinal) FILTER (WHERE e.side = 'target') AS sentence_ids_a,
           array_agg(e.sentence_id ORDER BY e.ordinal) FILTER (WHERE e.side = 'peer')   AS sentence_ids_b,
           count(*) FILTER (WHERE e.visible_in_prompt IS FALSE)                        AS unseen_citations
    FROM peer_comparison_evidence e WHERE e.comparison_id = c.comparison_id
) ev ON true;

INSERT INTO schema_info (key, value) VALUES ('peer_comparison_schema_version', '3')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
