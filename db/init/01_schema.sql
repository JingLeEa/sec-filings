-- Text-side schema for the SEC disclosure project (schema version 1).
-- Runs automatically the first time the Docker volume is created.
-- To re-apply after editing: docker compose down -v && docker compose up -d
--
-- Mirrors the preprocessing output (YEAR_chunks.json and YEAR_chunk_sentences.json).
-- The pipeline's own IDs are the primary keys, so reloading a filing is idempotent.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per company and fiscal year.
CREATE TABLE IF NOT EXISTS filings (
    filing_id             BIGSERIAL PRIMARY KEY,
    company               TEXT        NOT NULL,              -- e.g. 'unh' (same as the ID prefix)
    fiscal_year           INTEGER     NOT NULL,
    form_type             TEXT        NOT NULL DEFAULT '10-K',
    source                TEXT,                              -- URL or file the HTML came from
    xbrl_taxonomy_version TEXT,                              -- reserved for the table side (professor asked to keep this per filing)
    loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, fiscal_year, form_type)
);

-- One row per paragraph/block, e.g. unh_2025_1A_P012.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id           TEXT PRIMARY KEY,
    filing_id          BIGINT  NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE,
    item               TEXT    NOT NULL CHECK (item IN ('1', '1A', '7', '8', '15')),
    item_default_title TEXT,
    item_title         TEXT,                                 -- joined section hierarchy
    section_path       TEXT[]  NOT NULL DEFAULT '{}',        -- same hierarchy as a list
    item_chunk_index   INTEGER NOT NULL,                     -- the number in _P012
    text               TEXT    NOT NULL,
    html_list_depth    INTEGER,
    source             TEXT,
    extra              JSONB   NOT NULL DEFAULT '{}'::jsonb, -- any new field the extractor adds later
    UNIQUE (filing_id, item, item_chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_filing_item_idx ON chunks (filing_id, item);

-- One row per sentence, e.g. unh_2025_1A_P012_S003.
CREATE TABLE IF NOT EXISTS sentences (
    sentence_id        TEXT PRIMARY KEY,
    chunk_id           TEXT    NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    filing_id          BIGINT  NOT NULL REFERENCES filings(filing_id) ON DELETE CASCADE, -- copied from the chunk for fast filtering
    item               TEXT    NOT NULL,
    item_title         TEXT,
    section_path       TEXT[]  NOT NULL DEFAULT '{}',
    sentence_index     INTEGER NOT NULL,                     -- the number in _S003
    text               TEXT    NOT NULL,
    char_count         INTEGER GENERATED ALWAYS AS (char_length(text)) STORED,
    bullet_level       INTEGER,
    bullet_indent_pt   NUMERIC,
    html_list_depth    INTEGER,
    source_block_index TEXT,
    source             TEXT,
    extra              JSONB   NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (chunk_id, sentence_index)
);
CREATE INDEX IF NOT EXISTS sentences_chunk_idx        ON sentences (chunk_id);
CREATE INDEX IF NOT EXISTS sentences_filing_item_idx  ON sentences (filing_id, item);
CREATE INDEX IF NOT EXISTS sentences_fts_idx          ON sentences USING gin (to_tsvector('english', text));

-- Which embedding model produced a vector, and how many dimensions it has.
CREATE TABLE IF NOT EXISTS embedding_models (
    model_id  SERIAL PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE,                       -- e.g. 'bge-m3'
    dimension INTEGER NOT NULL,
    notes     TEXT
);
-- bge-m3 is what the semantic-retrieval branch currently uses (1024 dimensions).
INSERT INTO embedding_models (name, dimension, notes)
VALUES ('bge-m3', 1024, 'via SoCLaaS embeddings endpoint')
ON CONFLICT (name) DO NOTHING;

-- Embeddings live in their own table so that
--  (a) one sentence can have several embeddings (different models), and
--  (b) a sentence longer than the model limit can be embedded in parts
--      (part_index 0, 1, 2 ...) while the original sentence stays intact in `sentences`.
-- part_text is NULL when the whole sentence was embedded.
CREATE TABLE IF NOT EXISTS sentence_embeddings (
    sentence_id TEXT    NOT NULL REFERENCES sentences(sentence_id) ON DELETE CASCADE,
    model_id    INTEGER NOT NULL REFERENCES embedding_models(model_id),
    part_index  INTEGER NOT NULL DEFAULT 0,
    part_text   TEXT,
    embedding   vector(1024) NOT NULL,                       -- fixed size so it can be indexed; a model with another size needs its own column/table
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sentence_id, model_id, part_index)
);
CREATE INDEX IF NOT EXISTS sentence_embeddings_hnsw_idx
    ON sentence_embeddings USING hnsw (embedding vector_cosine_ops);

-- Convenience view: every sentence with its filing and section, ready to filter.
CREATE OR REPLACE VIEW sentence_context AS
SELECT s.sentence_id,
       s.chunk_id,
       f.company,
       f.fiscal_year,
       s.item,
       s.item_title,
       s.section_path,
       s.sentence_index,
       s.text,
       s.char_count
FROM sentences s
JOIN filings f USING (filing_id);

-- Records which schema version this database has.
CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_info (key, value) VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;
