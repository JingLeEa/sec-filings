-- Example queries. Run any of them with, e.g.:
--   docker compose exec -T db psql -U sec -d sec_disclosure -f - < db/example_queries.sql
-- or open an interactive prompt:
--   docker compose exec db psql -U sec -d sec_disclosure

-- 1. What is loaded?
SELECT f.company, f.fiscal_year,
       (SELECT count(*) FROM chunks c    WHERE c.filing_id = f.filing_id) AS chunks,
       (SELECT count(*) FROM sentences s WHERE s.filing_id = f.filing_id) AS sentences
FROM filings f
ORDER BY f.company, f.fiscal_year;

-- 2. Sentences per Item for one company and year.
SELECT item, count(*) AS sentences
FROM sentence_context
WHERE company = 'unh' AND fiscal_year = 2025
GROUP BY item
ORDER BY item;

-- 3. All sentences in one section (metadata filter).
SELECT sentence_id, text
FROM sentence_context
WHERE company = 'unh' AND fiscal_year = 2025 AND item = '1A'
  AND section_path[1] = 'Risk Factors'
ORDER BY sentence_id
LIMIT 10;

-- 4. Keyword search (Postgres full text search).
SELECT sentence_id, left(text, 120) AS preview
FROM sentence_context
WHERE to_tsvector('english', text) @@ plainto_tsquery('english', 'cybersecurity incident')
  AND company = 'unh'
ORDER BY fiscal_year, sentence_id
LIMIT 10;

-- 5. Sentences that would exceed a typical embedding limit (long-sentence check).
SELECT sentence_id, char_count
FROM sentences
ORDER BY char_count DESC
LIMIT 5;

-- 6. Which sentences already have an embedding, per model?
SELECT m.name AS model, m.dimension, count(*) AS embedded_sentences
FROM sentence_embeddings e
JOIN embedding_models m USING (model_id)
GROUP BY m.name, m.dimension;

-- 7. Semantic search: the 5 sentences closest in meaning to a given sentence (needs embeddings).
--    <=> is cosine distance; smaller means more similar.
SELECT s.sentence_id, round((e.embedding <=> q.embedding)::numeric, 4) AS distance, left(s.text, 100) AS preview
FROM sentence_embeddings e
JOIN sentences s USING (sentence_id)
CROSS JOIN (SELECT embedding FROM sentence_embeddings
            WHERE sentence_id = 'unh_2025_1_P001_S001' LIMIT 1) q
WHERE s.sentence_id <> 'unh_2025_1_P001_S001'
ORDER BY e.embedding <=> q.embedding
LIMIT 5;
