-- Read model for the disclosure-change chatbot (schema version 6).
-- Safe to run more than once. The chatbot applies it automatically on start-up when the view is missing,
-- or apply it by hand on an existing database:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/06_chatbot.sql
-- Needs 01_schema.sql (embedding_models) and 02_disclosures_alignments.sql (alignments) first.

-- One row per alignment decision with the JSONB analysis flattened into typed columns.
-- Text comes from the alignment's own evidence (original filing sentences), so the view works
-- even when the disclosures table has not been loaded.
CREATE OR REPLACE VIEW alignment_changes AS
SELECT r.run_id,
       r.company,
       r.previous_year,
       r.current_year,
       a.match_id,
       a.status,
       a.relationship,
       a.match_method,
       a.source_file,
       m.item,
       m.section,
       m.disclosure_taxonomy,
       a.change_analysis->>'status'                                   AS analysis_status,
       a.change_analysis->>'final_taxonomy'                           AS change_type,
       a.change_analysis->'lexical'->>'change_taxonomy'               AS lexical_label,
       a.change_analysis->'semantic'->>'label'                        AS semantic_label,
       a.change_analysis->>'llm'                                      AS llm_label,
       (a.change_analysis->'semantic'->>'normalised_score')::float    AS semantic_score,
       (a.change_analysis->'lexical'->>'cosine_similarity')::float    AS lexical_cosine,
       (a.change_analysis->'lexical'->>'sequence_match_ratio')::float AS sequence_match_ratio,
       (a.change_analysis->'lexical'->>'length_ratio')::float         AS length_ratio,
       (a.change_analysis->'lexical'->>'word_count_delta')::float::int AS word_count_delta,
       a.change_analysis->'semantic'->>'rationale'                    AS semantic_rationale,
       a.extra->'materiality_analysis'->>'materiality'                AS material,
       (a.extra->'materiality_analysis'->>'materiality_score')::float AS materiality_score,
       (a.extra->'materiality_analysis'->>'confidence_score')::float  AS confidence_score,
       a.extra->'materiality_analysis'->>'reason'                     AS materiality_reason,
       a.extra->'materiality_analysis'->>'key_change'                 AS key_change,
       a.explanation,
       a.review_reasons,
       a.verifier_review_reason,
       a.unmatched_type,
       m.previous_ids,
       m.current_ids,
       m.previous_summary,
       m.current_summary,
       ev.previous_text,
       ev.current_text
FROM alignments a
JOIN alignment_runs r USING (run_id)
LEFT JOIN LATERAL (
    SELECT min(am.item)                                                          AS item,
           coalesce(min(am.section) FILTER (WHERE am.side = 'current'), min(am.section))   AS section,
           coalesce(min(am.taxonomy) FILTER (WHERE am.side = 'current'), min(am.taxonomy)) AS disclosure_taxonomy,
           coalesce(array_agg(am.disclosure_id ORDER BY am.disclosure_id) FILTER (WHERE am.side = 'previous'), '{}') AS previous_ids,
           coalesce(array_agg(am.disclosure_id ORDER BY am.disclosure_id) FILTER (WHERE am.side = 'current'), '{}')  AS current_ids,
           string_agg(am.summary, ' | ' ORDER BY am.disclosure_id) FILTER (WHERE am.side = 'previous') AS previous_summary,
           string_agg(am.summary, ' | ' ORDER BY am.disclosure_id) FILTER (WHERE am.side = 'current')  AS current_summary
    FROM alignment_members am
    WHERE am.run_id = a.run_id AND am.match_id = a.match_id
) m ON true
LEFT JOIN LATERAL (
    SELECT string_agg(s.sentence->>'text', ' ' ORDER BY e.ord, s.ord)
               FILTER (WHERE starts_with(e.block->>'disclosure_id', r.company || '_' || r.previous_year || '_')) AS previous_text,
           string_agg(s.sentence->>'text', ' ' ORDER BY e.ord, s.ord)
               FILTER (WHERE starts_with(e.block->>'disclosure_id', r.company || '_' || r.current_year || '_'))  AS current_text
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(a.evidence) = 'array' THEN a.evidence ELSE '[]'::jsonb END)
             WITH ORDINALITY AS e(block, ord)
    CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(e.block->'sentences') = 'array'
                                                 THEN e.block->'sentences' ELSE '[]'::jsonb END)
             WITH ORDINALITY AS s(sentence, ord)
) ev ON true;

-- Optional vectors for the alignment evidence, so the chatbot can run pgvector search on the
-- alignment fixtures even when the disclosures table (and disclosure_embeddings) is empty.
-- Filled by scripts/embed_alignment_evidence.py. Rows disappear with their alignment when a run is reloaded.
CREATE TABLE IF NOT EXISTS chatbot_evidence_embeddings (
    run_id     BIGINT  NOT NULL,
    match_id   TEXT    NOT NULL,
    side       TEXT    NOT NULL CHECK (side IN ('previous', 'current')),
    model_id   INTEGER NOT NULL REFERENCES embedding_models(model_id),
    text_hash  TEXT    NOT NULL,                       -- sha256 of the embedded text, to skip unchanged rows
    embedding  vector(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, match_id, side, model_id),
    FOREIGN KEY (run_id, match_id) REFERENCES alignments(run_id, match_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS chatbot_evidence_embeddings_hnsw_idx
    ON chatbot_evidence_embeddings USING hnsw (embedding vector_cosine_ops);

INSERT INTO schema_info (key, value) VALUES ('chatbot_schema_version', '1')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
