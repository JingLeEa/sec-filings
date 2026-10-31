-- Durable cache, output artifacts, and pipeline progress. No local cache is required.
CREATE TABLE IF NOT EXISTS pipeline_artifacts (
    namespace TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'directory')),
    content BYTEA,
    modified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, path),
    CHECK ((kind = 'file' AND content IS NOT NULL) OR (kind = 'directory' AND content IS NULL))
);
