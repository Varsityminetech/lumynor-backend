-- Revenue Radar OS — Migration v3
-- Funding opportunities + market trend intelligence items

CREATE TABLE IF NOT EXISTS intelligence_items (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type         TEXT NOT NULL CHECK (type IN ('funding', 'trend')),
  product      TEXT NOT NULL,   -- district21 | agentforge | linkforge | all
  title        TEXT NOT NULL,
  summary      TEXT,
  source_url   TEXT,
  deadline     TEXT,            -- application deadline for funding, empty for trends
  importance   TEXT DEFAULT 'medium' CHECK (importance IN ('high', 'medium', 'low')),
  dismissed    BOOLEAN DEFAULT FALSE,
  detected_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intelligence_type       ON intelligence_items(type);
CREATE INDEX IF NOT EXISTS idx_intelligence_product    ON intelligence_items(product);
CREATE INDEX IF NOT EXISTS idx_intelligence_dismissed  ON intelligence_items(dismissed);
