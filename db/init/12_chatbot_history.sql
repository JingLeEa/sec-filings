-- Chatbot conversation history: every question and answer, so nothing is lost when the app or CLI restarts.
-- Safe to run more than once. The chatbot applies it automatically when its version is missing or older.
-- One database per person (local Docker), so conversations are not split by user.

CREATE TABLE IF NOT EXISTS chatbot_conversations (
    conversation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,                    -- the first question, shortened
    source          TEXT NOT NULL DEFAULT 'app' CHECK (source IN ('app', 'cli')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chatbot_conversations_updated ON chatbot_conversations (updated_at DESC);

-- One row per question. payload holds what the page needs to redraw the answer exactly (answer, citations,
-- evidence rows, SQL, grounding) plus the parsed plan, so a follow-up after a restart keeps its context.
CREATE TABLE IF NOT EXISTS chatbot_turns (
    turn_id         BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES chatbot_conversations (conversation_id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT,                             -- NULL when the question failed (error holds why)
    error           TEXT,
    intent          TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, position)
);

INSERT INTO schema_info (key, value) VALUES ('chatbot_history_schema_version', '1')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
