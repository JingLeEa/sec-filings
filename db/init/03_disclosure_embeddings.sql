-- Embeddings for whole extracted disclosures (what the semantic-retrieval stage embeds).
-- Safe to run more than once. On an existing database apply with:
--   docker compose exec -T db psql -U sec -d sec_disclosure < db/init/03_disclosure_embeddings.sql
-- Needs 01_schema.sql (embedding_models) and 02_disclosures_alignments.sql (disclosures) first.

-- One row per disclosure, model and field. Semantic retrieval embeds three texts per disclosure:
-- its original content, its model-generated summary, and its section name.
CREATE TABLE IF NOT EXISTS disclosure_embeddings (
    disclosure_id TEXT    NOT NULL REFERENCES disclosures(disclosure_id) ON DELETE CASCADE,
    model_id      INTEGER NOT NULL REFERENCES embedding_models(model_id),
    field         TEXT    NOT NULL CHECK (field IN ('content', 'summary', 'section')),
    embedding     vector(1024) NOT NULL,                -- bge-m3 size; another model size needs its own column/table
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (disclosure_id, model_id, field)
);
-- Search index for the content vectors (the field similarity search is mostly run on).
CREATE INDEX IF NOT EXISTS disclosure_embeddings_content_hnsw_idx
    ON disclosure_embeddings USING hnsw (embedding vector_cosine_ops) WHERE field = 'content';

INSERT INTO schema_info (key, value) VALUES ('schema_version', '3')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
