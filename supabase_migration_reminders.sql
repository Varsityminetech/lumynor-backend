-- Lumy reminders: set via chat/WhatsApp, delivered to WhatsApp when due
CREATE TABLE IF NOT EXISTS lumy_reminders (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  text        TEXT NOT NULL,
  due_at      TIMESTAMPTZ NOT NULL,
  sent        BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lumy_reminders_due ON lumy_reminders(sent, due_at);
