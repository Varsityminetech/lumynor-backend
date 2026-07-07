-- Lumy persistent memory: every conversation, across WhatsApp and dashboard
CREATE TABLE IF NOT EXISTS lumy_conversations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source      TEXT NOT NULL DEFAULT 'whatsapp',   -- 'whatsapp' | 'dashboard'
  role        TEXT NOT NULL,                       -- 'user' | 'assistant'
  content     TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lumy_conversations_created ON lumy_conversations(created_at DESC);

-- Lumy's private session notes: longitudinal wellbeing observations she writes
-- after personal/emotional conversations (mood, themes, risk, progress)
CREATE TABLE IF NOT EXISTS lumy_notes (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mood          TEXT,                              -- e.g. 'low', 'anxious', 'okay', 'good'
  themes        JSONB,                             -- e.g. ["sleep", "burnout", "family pressure"]
  observations  TEXT NOT NULL,                     -- her clinical-style note for this session
  risk_level    TEXT NOT NULL DEFAULT 'none',      -- none | mild | moderate | high
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lumy_notes_created ON lumy_notes(created_at DESC);
